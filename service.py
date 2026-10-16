"""选船服务编排、权限和冷却；框架无关，便于集成测试。"""

import asyncio
import base64
from dataclasses import replace
import hashlib
import json
import logging
import math
from pathlib import Path
import secrets
import shlex
import time

from .api import HttpWowsApi
from .binding import BindingSession, BindingSessions, BindingStore
from .cache import BusyError
from .catalog import CatalogStore
from .config import Settings
from .health import HealthMonitor
from .history import HistoryStore
from .images import ImageStore
from .instance import InstanceLock
from .inventory import InventoryStore
from .maintenance import PlayerDataBackup, PublicDataRecovery
from .maintenance_storage import backup_players, disk_status, recover_public
from .models import ErrorCode, Pool, SelectionError
from .names import ClientNames
from .parser import parse_arguments
from .paths import WowsPaths
from .randomizer import matches, resolve_pool, select_ship
from .renderer import ShipRenderer
from .storage import StorageError, atomic_write, read_json
from .updater import DataUpdater

logger = logging.getLogger(__name__)


class WowsService:
    def __init__(
        self,
        settings: Settings,
        data_path: Path,
        *,
        api=None,
        renderer=None,
        notifier=None,
        translation_sync=None,
    ):
        self.settings = settings
        self.translation_sync = translation_sync
        self.paths = WowsPaths(data_path)
        try:
            self.names = ClientNames(
                data_path, settings.name_language, settings.language
            )
        except StorageError:
            self.names = ClientNames(
                data_path, settings.name_language, settings.language, staging=True
            )
        self.health = HealthMonitor(settings, data_path, notifier)
        self.public_data = PublicDataRecovery(
            settings,
            data_path,
            recover_fn=lambda: recover_public(settings, data_path),
            disk_status_fn=lambda: disk_status(data_path),
        )
        self.player_backup = PlayerDataBackup(
            settings,
            data_path,
            create_fn=lambda: backup_players(settings, data_path),
        )
        self.closed = False
        self._instance = InstanceLock(self.paths.root / "service.lock")
        self._maintenance_lock = asyncio.Lock()
        self._maintenance_task = None
        self._watchdog_task = None
        self._maintenance_retry_at = 0
        self._maintenance_failures = 0
        self.api = api or HttpWowsApi(settings)
        self.images = ImageStore(self.api, data_path)
        self.catalog = CatalogStore(self.api, settings, data_path)
        self.bindings = BindingStore(self.paths.bindings)
        self.updater = DataUpdater(
            self.api, settings, data_path, self.catalog, self.images
        )
        self.catalog.refresh_callback = self.updater.refresh_snapshot
        self.inventory = InventoryStore(data_path)
        self.history = HistoryStore(self.api, settings, data_path)
        self.history.on_refresh = self._history_changed
        self._unknown_ids = set()
        self._unknown_task = None
        self._unknown_lock = asyncio.Lock()
        self._unknown_sync_after = 0
        self._manual_history_after = {}
        self.sessions = BindingSessions()
        self.renderer = renderer or ShipRenderer(settings)
        self.cooldowns: dict[tuple[str, str], float] = {}
        self.tasks: set[asyncio.Task] = set()
        self._warmup_task: asyncio.Task | None = None
        self._history_task: asyncio.Task | None = None

    def _track(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    @property
    def watchdog_task(self):
        """Expose watchdog state to the host lifecycle manager."""
        return self._watchdog_task

    def start_warmup(self) -> None:
        if self.closed:
            raise BusyError("服务已关闭")
        self._instance.acquire()
        if self._warmup_task is None:
            self._warmup_task = self._track(self._warm_catalog())
        if self._history_task is None or self._history_task.done():
            self._history_task = self._track(self._history_loop())
        if self._maintenance_task is None or self._maintenance_task.done():
            self._maintenance_task = self._track(self._maintenance_loop())
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = self._track(self._watchdog_loop())

    async def _watchdog_loop(self):
        while not self.closed:
            for attribute, factory, label in (
                ("_history_task", self._history_loop, "history"),
                ("_maintenance_task", self._maintenance_loop, "maintenance"),
            ):
                task = getattr(self, attribute)
                if task is not None and task.done() and not self.closed:
                    if not task.cancelled():
                        task.exception()
                    await self.health.issue(
                        "task_" + label,
                        "后台任务意外停止，正在重建：" + label,
                        severity="critical",
                    )
                    setattr(self, attribute, self._track(factory()))
            await self.health.heartbeat("watchdog")
            await self.health.recover("task_watchdog")
            await asyncio.sleep(self.settings.watchdog_interval_seconds)

    def _translation_fingerprint(self):
        source = self.updater.translation_source()
        paths = [self.paths.names(self.settings.name_language)]
        if source:
            paths += [
                source[0] / self.settings.name_language / "LC_MESSAGES/global.mo",
                source[0] / "en/LC_MESSAGES/global.mo",
            ]
        digest = hashlib.sha256()
        for path in paths:
            if not path.is_file():
                continue
            if path.stat().st_size > 20 * 1024 * 1024:
                raise StorageError("客户端资源文件过大，请检查本地资源")
            raw = path.read_bytes()
            if path.suffix == ".json":
                payload = json.loads(raw)
                payload.pop("data_version", None)
                raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
            digest.update(str(path).encode())
            digest.update(raw)
        return digest.hexdigest()

    async def _recover_public(self):
        recovery = await self.public_data.recover()
        if recovery:
            self.catalog.snapshots.clear()
            self.names = ClientNames(
                self.paths.data_path,
                self.settings.name_language,
                self.settings.language,
            )
            if recovery.get("status") == "degraded":
                await self.health.issue(
                    "public_data",
                    "公共船库没有完整快照，已保全损坏索引，等待重新下载",
                    severity="critical",
                )
            else:
                await self.health.issue(
                    "public_recovery",
                    "公共船库损坏，已自动回退至完整快照",
                    severity="critical",
                )
                await self.health.recover("public_recovery")

    async def _sync_translations(self, *, force=False):
        if self.translation_sync is None or self.settings.client_translations_path:
            return
        try:
            disk = await asyncio.to_thread(self.public_data.disk_status)
            if disk["free_bytes"] < self.settings.disk_min_free_mb * 1024 * 1024:
                await self.health.issue(
                    "disk", "剩余磁盘空间不足，暂停客户端资源下载", severity="critical"
                )
                return
            result = await self.translation_sync(
                self.settings.name_language, force=force
            )
            if result is None:
                return
            source = result.get("source")
            self.updater.synced_translation_source = source
            self.health.state["translations"] = {
                "status": result["status"],
                "checked_at": result.get("checked_at", 0),
                "upstream_build": result.get("upstream_build"),
                "local_build": source[1] if source else None,
                "resource_root": str(source[0]) if source else None,
            }
            if result["status"] in {"cooldown", "unavailable"}:
                await self.health.issue(
                    "translation_sync", "客户端语言资源同步暂不可用，继续使用原名称表"
                )
            else:
                await self.health.success("translation_sync")
        except Exception as error:
            await self.health.issue(
                "translation_sync",
                "客户端语言同步失败，继续使用原名称表：" + type(error).__name__,
            )

    async def maintenance_once(self):
        async with self._maintenance_lock:
            await self.health.heartbeat("maintenance", "checking")
            await self._recover_public()
            disk = await asyncio.to_thread(self.public_data.disk_status)
            self.health.state["disk"] = disk
            if disk["free_bytes"] < self.settings.disk_min_free_mb * 1024 * 1024:
                await self.health.issue(
                    "disk", "剩余磁盘空间不足，暂停新增缓存与备份", severity="critical"
                )
                return {"status": "disk_low"}
            await self.health.recover("disk")
            await self._sync_translations()
            active = await asyncio.to_thread(self.paths.active, self.settings.language)
            now = time.time()
            fingerprint = await asyncio.to_thread(self._translation_fingerprint)
            before = self.health.state.get("translation_fingerprint")
            catalog_due = (
                active is None
                or now - active.get("checked_at", active["catalog_fetched_at"])
                >= self.settings.catalog_ttl_hours * 3600
            )
            verify_due = (
                active is None
                or now - active.get("last_verified_at", 0)
                >= self.settings.image_verify_days * 86400
            )
            names_changed = before != fingerprint
            images_due = (
                bool(active and active.get("failed_images", 0))
                and now - self.health.state.get("images_attempt_at", 0)
                >= self.settings.image_retry_minutes * 60
            )
            if (
                catalog_due or verify_due or names_changed or images_due
            ) and now >= self._maintenance_retry_at:
                try:
                    self.health.state["images_attempt_at"] = now
                    report = await self.updater.update(
                        force=catalog_due, verify=verify_due
                    )
                    self._maintenance_failures = 0
                    self._maintenance_retry_at = 0
                    self.health.state[
                        "translation_fingerprint"
                    ] = await asyncio.to_thread(self._translation_fingerprint)
                    await self.health.success("catalog_update")
                    await self.health.recover("public_data")
                    if report.failed_images:
                        await self.health.issue(
                            "images",
                            f"有 {report.failed_images} 张舰图未补齐，文字选船可用",
                        )
                    else:
                        await self.health.recover("images")
                except Exception as error:
                    self._maintenance_failures += 1
                    self._maintenance_retry_at = now + min(
                        21600, 60 * 2 ** min(self._maintenance_failures, 8)
                    )
                    await self.health.issue(
                        "catalog_update",
                        "船库巡检更新失败，继续使用已有资料：" + type(error).__name__,
                    )
            elif before is None:
                self.health.state["translation_fingerprint"] = fingerprint
            active = await asyncio.to_thread(self.paths.active, self.settings.language)
            if active and active.get("pending_names", 0):
                pending_since = self.health.state.setdefault("pending_names_since", now)
                if (
                    now - pending_since
                    >= self.settings.pending_names_alert_hours * 3600
                ):
                    await self.health.issue(
                        "pending_names",
                        f"有 {active['pending_names']} 艘船的客户端名称长期待补，"
                        "请同步本地语言资源",
                        severity="critical",
                    )
            else:
                self.health.state.pop("pending_names_since", None)
                await self.health.recover("pending_names")
            circuit = getattr(self.api, "circuit", None)
            if isinstance(circuit, dict):
                self.health.state["api_circuit"] = circuit.copy()
                if circuit.get("reason") in {
                    "INVALID_IP_ADDRESS",
                    "INVALID_APPLICATION_ID",
                    "APPLICATION_IS_BLOCKED",
                }:
                    await self.health.issue(
                        "api_config",
                        "WG 应用配置或 IP 白名单错误，已暂停重复请求",
                        severity="critical",
                    )
                elif circuit.get("last_success_at", 0) > self.health.state[
                    "issues"
                ].get("api_config", {}).get("last_seen", 0):
                    await self.health.recover("api_config")
            try:
                result = await self.player_backup.create()
                self.health.state["backup"] = result
                await self.health.success("backup")
            except Exception as error:
                await self.health.issue(
                    "backup", "定期备份失败：" + type(error).__name__
                )
            await self.health.success("maintenance")
            await self.health.recover("task_maintenance")
            await self.health.heartbeat("maintenance", "sleeping")
            return {
                "status": "checked",
                "catalog_due": catalog_due,
                "verify_due": verify_due,
            }

    async def _maintenance_loop(self):
        while not self.closed:
            try:
                await self.maintenance_once()
            except Exception as error:
                await self.health.issue(
                    "maintenance", "独立巡检失败：" + type(error).__name__
                )
            await asyncio.sleep(self.settings.maintenance_interval_minutes * 60)

    async def refresh_histories(self):
        accounts = await self.bindings.all()
        queue = asyncio.Queue()
        report = {"updated": 0, "failed": 0, "skipped": 0}
        for user_id, account in accounts.items():
            if account.server == "asia":
                queue.put_nowait((user_id, account))

        async def worker():
            while True:
                try:
                    user_id, account = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    if await self.bindings.get(user_id) != account:
                        report["skipped"] += 1
                        continue
                    updated = await self.history.refresh_if_due(user_id, account)
                    report["updated" if updated else "skipped"] += 1
                    stored = await self.history.read(user_id, account)
                    if stored and stored.result:
                        await self._observe_ship_ids(stored.result.ship_ids)
                except Exception as error:
                    report["failed"] += 1
                    logger.warning(
                        "玩家历史记录定时检查失败（%s）", type(error).__name__
                    )

        tasks = [asyncio.create_task(worker()) for _ in range(2)]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._observe_ship_ids(frozenset(self._unknown_ids))
        return report

    async def _history_changed(self, user_id, account, snapshot):
        if snapshot.result is not None:
            await self._observe_ship_ids(snapshot.result.ship_ids)

    async def _persist_unknown_pending(self):
        path = self.paths.unknown_sync(self.settings.language)
        try:
            row = await asyncio.to_thread(read_json, path)
            if not isinstance(row, dict) or row.get("schema_version") != 1:
                return
            pending = sorted(self._unknown_ids)
            if row.get("pending_ship_ids") != pending:
                await atomic_write(path, dict(row, pending_ship_ids=pending))
        except StorageError:
            logger.warning("未知船列表暂不能保存，玩家原始记录已保留")

    async def _observe_ship_ids(self, identifiers):
        if not identifiers:
            return None
        key = ("asia", self.settings.language)
        snapshot = self.catalog.snapshots.get(key)
        if snapshot is None:
            snapshot = await asyncio.to_thread(self.catalog.load_cached, key[0], key[1])
        ready = (
            {
                ship.ship_id
                for ship in snapshot.ships
                if ship.ship_id not in snapshot.pending_ship_ids
            }
            if snapshot
            else set()
        )
        async with self._unknown_lock:
            self._unknown_ids.update(set(identifiers) - ready)
            self._unknown_ids.difference_update(ready)
            if not self._unknown_ids:
                await self._persist_unknown_pending()
                return None
            if self._unknown_task is not None and not self._unknown_task.done():
                await self._persist_unknown_pending()
                return self._unknown_task
            if time.monotonic() < self._unknown_sync_after:
                await self._persist_unknown_pending()
                return None
            path = self.paths.unknown_sync(self.settings.language)
            try:
                saved = await asyncio.to_thread(read_json, path)
                if saved is not None and (
                    not isinstance(saved, dict) or saved.get("schema_version") != 1
                ):
                    raise StorageError("未知船同步记录格式无效")
                attempt = (
                    saved.get("last_attempt_at", 0) if isinstance(saved, dict) else 0
                )
                if (
                    type(attempt) not in {int, float}
                    or not math.isfinite(attempt)
                    or attempt < 0
                    or attempt > time.time() + 60
                ):
                    raise StorageError("未知船同步记录无效")
                remaining = (
                    attempt
                    + self.settings.unknown_ship_refresh_minutes * 60
                    - time.time()
                )
                if remaining > 0:
                    self._unknown_sync_after = time.monotonic() + remaining
                    await self._persist_unknown_pending()
                    return None
            except StorageError:
                logger.warning("未知船同步记录不可读取，保留原文件并稍后重试")
                self._unknown_sync_after = (
                    time.monotonic() + self.settings.unknown_ship_refresh_minutes * 60
                )
                return None
            self._unknown_sync_after = (
                time.monotonic() + self.settings.unknown_ship_refresh_minutes * 60
            )
            attempted = time.time()
            await atomic_write(
                path,
                {
                    "schema_version": 1,
                    "last_attempt_at": attempted,
                    "pending_ship_ids": sorted(self._unknown_ids),
                },
            )
            self._unknown_task = asyncio.create_task(
                self._sync_unknown_catalog(attempted)
            )
            self.tasks.add(self._unknown_task)
            self._unknown_task.add_done_callback(self.tasks.discard)
            return self._unknown_task

    async def _sync_unknown_catalog(self, attempted):
        error = None
        try:
            await self.updater.update(force=True)
        except Exception as caught:
            error = type(caught).__name__
            logger.warning("新船资料同步暂未完成（%s）", error)
        async with self._unknown_lock:
            current = self.catalog.snapshots.get(("asia", self.settings.language))
            if current:
                ready = {
                    ship.ship_id
                    for ship in current.ships
                    if ship.ship_id not in current.pending_ship_ids
                }
                self._unknown_ids.difference_update(ready)
            try:
                await atomic_write(
                    self.paths.unknown_sync(self.settings.language),
                    {
                        "schema_version": 1,
                        "last_attempt_at": attempted,
                        "pending_ship_ids": sorted(self._unknown_ids),
                        "last_error": error,
                    },
                )
            except StorageError:
                logger.warning("新船同步状态保存失败，已保存的玩家记录不受影响")

    async def refresh_my_history(self, user_id):
        account = await self.bindings.get(user_id)
        if account is None:
            raise SelectionError(ErrorCode.BINDING_REQUIRED)
        if account.server != "asia":
            raise SelectionError(ErrorCode.UNSUPPORTED_ACCOUNT)
        now = time.monotonic()
        self._manual_history_after = {
            user: until
            for user, until in self._manual_history_after.items()
            if until > now
        }
        if self._manual_history_after.get(user_id, 0) > now:
            raise BusyError("手动更新历史池每分钟限一次，请稍后再试。")
        self._manual_history_after[user_id] = now + 60
        return await self.history.refresh(user_id, account, force=True)

    async def _history_loop(self):
        while True:
            try:
                await self.health.heartbeat("history", "checking")
                report = await self.refresh_histories()
                if report["failed"]:
                    await self.health.issue(
                        "history",
                        f"有 {report['failed']} 个历史池刷新失败，保留已有记录",
                    )
                else:
                    await self.health.success("history")
                    await self.health.recover("task_history")
                await self.health.heartbeat("history", "sleeping")
            except Exception as error:
                await self.health.issue(
                    "history", "历史池后台检查失败：" + type(error).__name__
                )
            await asyncio.sleep(self.settings.history_check_interval_minutes * 60)

    async def _warm_catalog(self) -> None:
        try:
            async with self._maintenance_lock:
                await self._recover_public()
                self.names = ClientNames(
                    self.paths.data_path,
                    self.settings.name_language,
                    self.settings.language,
                )
            snapshot = await self.catalog.get_snapshot("asia", self.settings.language)
            if snapshot.data_version is None:
                await self.updater.update(force=False)
            logger.info("舰船目录预加载完成，共 %d 艘", len(snapshot.ships))
        except Exception as error:
            logger.warning(
                "舰船目录预加载失败，选船时可重试（%s）", type(error).__name__
            )

    def allowed(self, group_id: str | None) -> bool:
        if group_id is None:
            return self.settings.allow_private
        return (
            not self.settings.allowed_groups or group_id in self.settings.allowed_groups
        )

    def take_cooldown(self, bot_id: str, user_id: str) -> float:
        now = time.monotonic()
        self.cooldowns = {
            key: until for key, until in self.cooldowns.items() if until > now
        }
        key = bot_id, user_id
        remaining = self.cooldowns.get(key, now) - now
        if remaining > 0:
            return remaining
        if len(self.cooldowns) >= 10000:
            return self.settings.user_cooldown_seconds
        self.cooldowns[key] = now + self.settings.user_cooldown_seconds
        return 0

    async def binding_candidates(self, nickname: str):
        """Search accounts without exposing the API adapter to the command layer."""
        return await asyncio.wait_for(
            self.api.search_accounts(nickname),
            self.settings.operation_timeout_seconds,
        )

    async def begin_binding(
        self, bot_id: str, user_id: str, group_id: str | None, nickname: str
    ):
        """Create a short-lived binding session for a command scope."""
        candidates = await self.binding_candidates(nickname)
        if not candidates:
            return candidates, None
        session = BindingSession(
            bot_id,
            user_id,
            group_id,
            secrets.token_hex(16),
            candidates,
            time.monotonic() + self.settings.binding_timeout_seconds,
        )
        await self.sessions.begin(session)
        return candidates, session

    def pending_binding(self, bot_id: str, user_id: str, group_id: str | None):
        """Return the pending session for a scope, if one exists."""
        return self.sessions.sessions.get((bot_id, user_id, group_id))

    async def cancel_binding(
        self,
        bot_id: str,
        user_id: str,
        group_id: str | None,
        session_id: str,
    ) -> bool:
        return await self.sessions.cancel(bot_id, user_id, group_id, session_id)

    async def commit_binding(self, session: BindingSession, choice: int):
        return await self.sessions.commit(self.bindings, session, choice)

    async def unbind_account(self, user_id: str) -> bool:
        return await self.sessions.unbind(self.bindings, user_id)

    async def account_for(self, user_id: str):
        return await self.bindings.get(user_id)

    async def select(self, user_id: str, arguments: str, requester: str = ""):
        request = parse_arguments(arguments)
        deadline = time.monotonic() + self.settings.operation_timeout_seconds

        async def prepare():
            # 显式全库不依赖绑定文件和个人 API。
            account = (
                None
                if request.pool == Pool.CATALOG
                else await self.bindings.get(user_id)
            )
            initial_pool = resolve_pool(request.pool, account)
            inventory = (
                await self.inventory.get(user_id, account)
                if initial_pool == Pool.PERSONAL
                else None
            )
            pool = resolve_pool(request.pool, account, inventory)
            if pool == Pool.OWNED and inventory is not None and not inventory.ship_ids:
                raise SelectionError(ErrorCode.NO_OWNED_SHIPS)
            snapshot = await self.catalog.get_snapshot("asia", self.settings.language)
            history = (
                await self.history.get(user_id, account)
                if pool == Pool.PERSONAL
                else None
            )
            stats = history.result if history else None
            if stats is not None:
                unknown = stats.ship_ids - {ship.ship_id for ship in snapshot.ships}
                if unknown:
                    logger.info(
                        "个人战绩中 %d 艘舰船不在当前目录，已跳过", len(unknown)
                    )
            if inventory is not None:
                unknown = inventory.ship_ids - {ship.ship_id for ship in snapshot.ships}
                if unknown:
                    logger.info(
                        "持有清单中 %d 艘舰船不在当前目录，已跳过", len(unknown)
                    )
            personal_ids = (
                stats.ship_ids
                if stats is not None
                else (inventory.ship_ids if inventory else frozenset())
            )
            task = await self._observe_ship_ids(personal_ids) if personal_ids else None
            if personal_ids:
                snapshot = await self.catalog.get_snapshot(
                    "asia", self.settings.language
                )
            eligible = any(
                ship.ship_id in personal_ids
                and ship.ship_id not in snapshot.pending_ship_ids
                and matches(ship, request.filters)
                for ship in snapshot.ships
            )
            if task is not None and not eligible:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task), self.settings.unknown_ship_wait_seconds
                    )
                except Exception:
                    pass
                snapshot = await self.catalog.get_snapshot(
                    "asia", self.settings.language
                )
            selected = select_ship(
                request, snapshot, account, stats, inventory=inventory
            )
            if history is not None:
                selected = replace(
                    selected,
                    history_updated_at=history.updated_at,
                    history_stale=self.history.is_stale(history)
                    or bool(history.last_error),
                )
            return selected

        selection = await asyncio.wait_for(
            prepare(), self.settings.operation_timeout_seconds
        )
        selection = self.names.apply(selection)
        ship_name_en = self.names.english_for(selection.ship, selection.data_version)
        image_data = None
        image_url = selection.ship.image_url or selection.ship.contour_url
        if image_url:
            try:
                image = await asyncio.wait_for(
                    self.images.get(image_url),
                    min(
                        self.settings.request_timeout_seconds,
                        max(0, deadline - time.monotonic()),
                    ),
                )
                image_data = "data:image/png;base64," + base64.b64encode(image).decode(
                    "ascii"
                )
            except Exception as error:
                logger.warning("舰图下载失败，生成无图卡片（%s）", type(error).__name__)
        return await self.renderer.render(
            selection,
            image_data,
            requester=requester,
            ship_name_en=ship_name_en,
        )

    async def manage_inventory(self, user_id, arguments):
        account = await self.bindings.get(user_id)
        if account is None:
            raise SelectionError(ErrorCode.BINDING_REQUIRED)
        if account.server != "asia":
            raise SelectionError(ErrorCode.UNSUPPORTED_ACCOUNT)
        text = arguments.strip()
        action, _, remainder = text.partition(" ")
        actions = {
            "添加": "add",
            "移除": "remove",
            "设置": "set",
            "清空": "empty",
            "撤销": "forget",
            "查看": "view",
            "": "view",
        }
        if action not in actions:
            raise ValueError(
                "用法：wows持有清单 [查看|添加|移除|设置|清空|撤销] "
                "[船 ID 或准确客户端船名]"
            )
        action = actions[action]
        snapshot = await self.catalog.get_snapshot("asia", self.settings.language)
        labels = {}
        for ship in snapshot.ships:
            if ship.ship_id not in snapshot.pending_ship_ids:
                label = self.names.name_for(ship, snapshot.data_version)
                labels.setdefault(label.casefold(), []).append(ship.ship_id)
        ids = set()
        if action in {"add", "remove", "set"}:
            if not remainder.strip():
                raise ValueError(
                    "请提供船 ID 或准确客户端船名；空清单请明确使用“清空”。"
                )
            whole = labels.get(remainder.strip().casefold(), [])
            tokens = (
                [remainder.strip()]
                if len(whole) == 1
                else shlex.split(remainder.replace("，", " ").replace(",", " "))
            )
            for token in tokens:
                if token.isascii() and token.isdecimal() and int(token) > 0:
                    ids.add(int(token))
                elif len(labels.get(token.casefold(), [])) == 1:
                    ids.add(labels[token.casefold()][0])
                else:
                    raise ValueError("船名未能唯一识别，请使用准确客户端船名或船 ID。")
            owned = await self.inventory.modify_manual(user_id, account, action, ids)
        elif action == "view":
            if remainder.strip():
                raise ValueError("查看清单无需额外参数")
            owned = await self.inventory.get(user_id, account)
        else:
            if remainder.strip():
                raise ValueError("清空或撤销无需额外参数")
            owned = await self.inventory.modify_manual(user_id, account, action)
        if owned is None:
            return "尚未维护持有清单，默认使用历史池。"
        ready = {
            ship.ship_id: ship
            for ship in snapshot.ships
            if ship.ship_id not in snapshot.pending_ship_ids
        }
        lines = []
        for identifier in sorted(owned.ship_ids)[:30]:
            label = (
                self.names.name_for(ready[identifier], snapshot.data_version)
                if identifier in ready
                else "资料待补齐"
            )
            lines.append(f"{label}（{identifier}）")
        await self._observe_ship_ids(owned.ship_ids)
        return (
            f"持有清单{'' if action == 'view' else '已保存'}，"
            f"共 {len(owned.ship_ids)} 艘（由本人手动维护）。"
            + ("\n" + "\n".join(lines) if lines else "清单为空。")
            + ("\n仅展示前 30 艘。" if len(owned.ship_ids) > 30 else "")
        )

    async def refresh_data(self):
        async with self._maintenance_lock:
            await self._sync_translations(force=True)
            return await self.updater.update(force=True)

    async def verify_data(self):
        async with self._maintenance_lock:
            return await self.updater.update(force=False, verify=True)

    async def data_status(self):
        row = await asyncio.to_thread(self.paths.active, self.settings.language)
        if row is None:
            raise StorageError("尚未准备本地船库，请先更新船库。")
        return dict(
            row,
            health=self.health.to_dict(),
            cleanup_candidates=await asyncio.to_thread(self.updater.cleanup_candidates),
        )

    async def close(self) -> None:
        self.closed = True
        tasks = tuple(task for task in self.tasks if task is not asyncio.current_task())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            for resource in (
                self.history,
                self.renderer,
                self.updater,
                self.catalog,
                self.images,
                self.api,
            ):
                try:
                    await resource.close()
                except Exception as error:
                    logger.warning(
                        "选船资源关闭失败（%s），继续关闭其他资源", type(error).__name__
                    )
        finally:
            self.sessions.clear()
            self.cooldowns.clear()
            self._instance.release()
