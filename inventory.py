"""按 QQ 与亚服账号隔离的本地持有清单；未提供与已确认空分开处理。"""

import asyncio
import math
from pathlib import Path
import time

from .file_lock import async_file_lock
from .models import Account, InventoryResult
from .paths import WowsPaths
from .storage import StorageError, atomic_write, read_json


class InventoryStore:
    def __init__(self, data_path: Path):
        self.root = WowsPaths(data_path).players
        self.lock = asyncio.Lock()

    def path_for(self, user_id: str, account: Account) -> Path:
        if (
            not isinstance(user_id, str)
            or not user_id.isascii()
            or not user_id.isdecimal()
            or account.server != "asia"
            or type(account.account_id) is not int
            or account.account_id <= 0
        ):
            raise ValueError("持有清单必须关联有效 QQ 和亚服账号")
        return self.root / user_id / account.server / f"{account.account_id}.json"

    @staticmethod
    def _result(payload: dict, user_id: str, account: Account):
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
            owned = payload.get("owned")
            if owned is None:
                return None
            if not isinstance(owned, dict):
                raise ValueError
            identifiers = owned["ship_ids"]
            stamp, source = owned["updated_at"], owned["source"]
            if (
                not isinstance(identifiers, list)
                or any(type(value) is not int or value <= 0 for value in identifiers)
                or type(stamp) not in {int, float}
                or not math.isfinite(stamp)
                or not 0 < stamp <= time.time() + 60
                or source not in {"manual", "wg_private_port"}
            ):
                raise ValueError
            return InventoryResult(
                account.server,
                account.account_id,
                user_id,
                frozenset(identifiers),
                source,
                stamp,
            )
        except (KeyError, ValueError, TypeError, OverflowError) as error:
            raise StorageError(
                "持有清单格式损坏，原文件已保留；请管理员检查。"
            ) from error

    def _read(self, path: Path, user_id: str, account: Account):
        payload = read_json(path)
        if payload is not None:
            self._result(payload, user_id, account)
        return payload

    async def get(self, user_id: str, account: Account) -> InventoryResult | None:
        path = self.path_for(user_id, account)
        async with self.lock:
            async with async_file_lock(path.with_name(path.name + ".lock")):
                payload = await asyncio.to_thread(self._read, path, user_id, account)
                return (
                    None
                    if payload is None
                    else self._result(payload, user_id, account)
                )

    async def set_manual(self, user_id: str, account: Account, ship_ids) -> None:
        path = self.path_for(user_id, account)
        identifiers = tuple(ship_ids)
        if any(type(value) is not int or value <= 0 for value in identifiers):
            raise ValueError("舰船 ID 必须为正整数")
        async with self.lock:
            async with async_file_lock(path.with_name(path.name + ".lock")):
                payload = await asyncio.to_thread(self._read, path, user_id, account)
                if payload is None:
                    payload = {
                        "schema_version": 1,
                        "user_id": user_id,
                        "server": account.server,
                        "account_id": account.account_id,
                    }
                payload["owned"] = {
                    "source": "manual",
                    "updated_at": time.time(),
                    "ship_ids": sorted(set(identifiers)),
                }
                await atomic_write(path, payload, backup=True)

    async def modify_manual(self, user_id, account, action, identifiers=()):
        identifiers = frozenset(identifiers)
        if (
            any(type(value) is not int or value <= 0 for value in identifiers)
            or len(identifiers) > 2000
        ):
            raise ValueError("船 ID 无效或一次提交过多")
        path = self.path_for(user_id, account)
        async with self.lock:
            async with async_file_lock(path.with_name(path.name + ".lock")):
                payload = await asyncio.to_thread(self._read, path, user_id, account)
                old = self._result(payload, user_id, account) if payload else None
                if action == "remove" and old is None:
                    raise ValueError("尚未维护持有清单，请先添加船只。")
                if payload is None:
                    payload = {
                        "schema_version": 1,
                        "user_id": user_id,
                        "server": account.server,
                        "account_id": account.account_id,
                    }
                current = old.ship_ids if old else frozenset()
                if action == "add":
                    current = current | identifiers
                elif action == "remove":
                    current = current - identifiers
                elif action == "set":
                    current = identifiers
                elif action == "empty":
                    current = frozenset()
                elif action == "forget":
                    payload["owned"] = None
                else:
                    raise ValueError("不支持的清单操作")
                if len(current) > 2000:
                    raise ValueError("持有清单过大，请检查输入")
                if action != "forget":
                    payload["owned"] = {
                        "source": "manual",
                        "updated_at": time.time(),
                        "ship_ids": sorted(current),
                    }
                await atomic_write(path, payload, backup=True)
                return self._result(payload, user_id, account)

    async def forget(self, user_id: str, account: Account) -> bool:
        path = self.path_for(user_id, account)
        async with self.lock:
            async with async_file_lock(path.with_name(path.name + ".lock")):
                payload = await asyncio.to_thread(self._read, path, user_id, account)
                if payload is None or payload.get("owned") is None:
                    return False
                payload["owned"] = None
                await atomic_write(path, payload, backup=True)
                return True
