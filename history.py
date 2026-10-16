"""玩家历史记录持久化：三天刷新、旧数据先服务、失败退避。"""

import asyncio
from dataclasses import dataclass
import logging
import math
from pathlib import Path
import time

from .cache import BusyError
from .config import Settings
from .models import Account, ErrorCode, SelectionError, StatsResult, StatsStatus
from .paths import WowsPaths
from .storage import StorageError, atomic_write, read_json

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HistorySnapshot:
    result: StatsResult | None
    updated_at: float | None
    last_attempt_at: float
    next_retry_at: float = 0
    last_error: str | None = None


class HistoryStore:
    def __init__(self, api, settings: Settings, data_path: Path):
        self.api, self.settings = api, settings
        self.paths = WowsPaths(data_path)
        self.pending = {}
        self.pending_force = {}
        self.on_refresh = None
        self.retry_after = {}
        self.closed = False

    def path_for(self, user_id: str, account: Account) -> Path:
        if (
            not isinstance(user_id, str)
            or not user_id.isascii()
            or not user_id.isdecimal()
            or account.server != "asia"
            or type(account.account_id) is not int
            or account.account_id <= 0
        ):
            raise ValueError("历史记录必须关联有效 QQ 和亚服账号")
        return (
            self.paths.players / user_id / "asia" / f"{account.account_id}.history.json"
        )

    @staticmethod
    def _stamp(value):
        return type(value) in {int, float} and math.isfinite(value) and value > 0

    def _read(self, user_id, account):
        payload = read_json(self.path_for(user_id, account))
        if payload is None:
            return None
        try:
            if (
                not isinstance(payload, dict)
                or type(payload["schema_version"]) is not int
                or payload["schema_version"] != 1
                or payload["user_id"] != user_id
                or payload["server"] != account.server
                or type(payload["account_id"]) is not int
                or payload["account_id"] != account.account_id
            ):
                raise ValueError
            history = payload["history"]
            identifiers = history["ship_ids"]
            updated, attempt = history["updated_at"], history["last_attempt_at"]
            retry, error = history["next_retry_at"], history["last_error"]
            if (
                not isinstance(identifiers, list)
                or any(type(item) is not int or item <= 0 for item in identifiers)
                or not self._stamp(attempt)
                or attempt > time.time() + 60
                or type(retry) not in {int, float}
                or not math.isfinite(retry)
                or retry < 0
                or (error is not None and not isinstance(error, str))
            ):
                raise ValueError
            status = history["status"]
            result = None
            if status is None:
                if identifiers or updated is not None or not error:
                    raise ValueError
            else:
                status = StatsStatus(status)
                if (
                    not self._stamp(updated)
                    or updated > attempt + 60
                    or (status == StatsStatus.AVAILABLE) != bool(identifiers)
                ):
                    raise ValueError
                result = StatsResult(
                    account.server, account.account_id, status, frozenset(identifiers)
                )
            return HistorySnapshot(result, updated, attempt, retry, error)
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise StorageError(
                "历史记录格式损坏，原文件已保留；请管理员检查。"
            ) from error

    async def read(self, user_id, account):
        return await asyncio.to_thread(self._read, user_id, account)

    def is_stale(self, snapshot):
        return snapshot.updated_at is None or (
            time.time() - snapshot.updated_at
            >= self.settings.history_refresh_days * 86400
        )

    def _can_retry(self, key, snapshot):
        return time.monotonic() >= self.retry_after.get(key, 0) and (
            snapshot is None or time.time() >= snapshot.next_retry_at
        )

    def _start(self, user_id, account, force=False):
        if self.closed:
            raise BusyError("历史记录服务已关闭。")
        key = (user_id, account.server, account.account_id)
        task = self.pending.get(key)
        if task is None:
            if len(self.pending) >= 64:
                raise BusyError("历史记录更新请求较多，请稍后再试。")
            task = asyncio.create_task(self._refresh(user_id, account, force))
            self.pending[key] = task
            self.pending_force[key] = force

            def done(completed):
                if self.pending.get(key) is completed:
                    self.pending.pop(key, None)
                    self.pending_force.pop(key, None)
                if not completed.cancelled() and completed.exception() is not None:
                    now = time.monotonic()
                    self.retry_after = {
                        entry: until
                        for entry, until in self.retry_after.items()
                        if until > now
                    }
                    self.retry_after[key] = (
                        now + self.settings.history_retry_minutes * 60
                    )
                    logger.warning(
                        "历史记录刷新失败，保留已有记录（%s）",
                        type(completed.exception()).__name__,
                    )
                elif not completed.cancelled():
                    self.retry_after.pop(key, None)

            task.add_done_callback(done)
        return task

    async def get(self, user_id, account):
        if self.closed:
            raise BusyError("历史记录服务已关闭。")
        snapshot = await self.read(user_id, account)
        key = (user_id, account.server, account.account_id)
        if snapshot is not None and snapshot.result is not None:
            if self.is_stale(snapshot) and self._can_retry(key, snapshot):
                try:
                    self._start(user_id, account)
                except BusyError:
                    pass
            return snapshot
        if not self._can_retry(key, snapshot):
            raise SelectionError(ErrorCode.STATS_UNAVAILABLE)
        return await asyncio.shield(self._start(user_id, account))

    async def refresh(self, user_id, account, *, force=False):
        self.path_for(user_id, account)
        key = (user_id, account.server, account.account_id)
        while force and key in self.pending and not self.pending_force.get(key):
            previous = self.pending[key]
            try:
                await asyncio.shield(previous)
            except Exception:
                pass
            if self.pending.get(key) is previous and previous.done():
                self.pending.pop(key, None)
                self.pending_force.pop(key, None)
        return await asyncio.shield(self._start(user_id, account, force))

    async def refresh_if_due(self, user_id, account):
        snapshot = await self.read(user_id, account)
        key = (user_id, account.server, account.account_id)
        if (
            snapshot is not None and not self.is_stale(snapshot)
        ) or not self._can_retry(key, snapshot):
            return False
        await self.refresh(user_id, account)
        return True

    def _payload(self, user_id, account, snapshot):
        result = snapshot.result
        return {
            "schema_version": 1,
            "user_id": user_id,
            "server": account.server,
            "account_id": account.account_id,
            "history": {
                "source": "wg_public_stats",
                "status": result.status.value if result else None,
                "ship_ids": sorted(result.ship_ids) if result else [],
                "updated_at": snapshot.updated_at,
                "last_attempt_at": snapshot.last_attempt_at,
                "next_retry_at": snapshot.next_retry_at,
                "last_error": snapshot.last_error,
            },
        }

    async def _refresh(self, user_id, account, force=False):
        path = self.path_for(user_id, account)
        old = await self.read(user_id, account)
        try:
            result = await asyncio.wait_for(
                self.api.fetch_stats(account, force=True)
                if force
                else self.api.fetch_stats(account),
                self.settings.operation_timeout_seconds,
            )
            if (
                not isinstance(result, StatsResult)
                or result.server != account.server
                or type(result.account_id) is not int
                or result.account_id != account.account_id
                or not isinstance(result.status, StatsStatus)
                or not isinstance(result.ship_ids, frozenset)
                or any(type(item) is not int or item <= 0 for item in result.ship_ids)
                or (result.status == StatsStatus.AVAILABLE) != bool(result.ship_ids)
            ):
                raise ValueError("历史记录响应不匹配")
        except Exception as error:
            stamp = time.time()
            failed = HistorySnapshot(
                old.result if old else None,
                old.updated_at if old else None,
                stamp,
                stamp + self.settings.history_retry_minutes * 60,
                type(error).__name__,
            )
            try:
                await atomic_write(path, self._payload(user_id, account, failed))
            except StorageError:
                logger.warning("历史记录失败状态无法保存，原文件已保留")
            raise
        stamp = time.time()
        snapshot = HistorySnapshot(result, stamp, stamp)
        await atomic_write(path, self._payload(user_id, account, snapshot), backup=True)
        if self.on_refresh is not None:
            try:
                await self.on_refresh(user_id, account, snapshot)
            except Exception as error:
                logger.warning(
                    "历史已保存，新船资料检查稍后重试（%s）", type(error).__name__
                )
        return snapshot

    async def close(self):
        self.closed = True
        tasks = tuple(self.pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.pending.clear()
        self.pending_force.clear()
        self.retry_after.clear()
