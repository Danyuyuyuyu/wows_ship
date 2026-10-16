"""完整目录快照：首次等待抓取，过期先服务旧数据并后台刷新。"""

import asyncio
from dataclasses import replace
import logging
from pathlib import Path
import time

from .api import ApiError, WowsApi, parse_ship
from .config import Settings
from .models import CatalogSnapshot
from .paths import WowsPaths
from .storage import StorageError, atomic_write, read_json

logger = logging.getLogger(__name__)


class CatalogStore:
    def __init__(self, api: WowsApi, settings: Settings, data_path: Path):
        self.api, self.settings, self.data_path = api, settings, data_path
        self.paths = WowsPaths(data_path)
        self.refresh_callback = None
        self.snapshots: dict[tuple[str, str], CatalogSnapshot] = {}
        self.pending: dict[tuple[str, str], asyncio.Task[CatalogSnapshot]] = {}
        self.retry_after: dict[tuple[str, str], float] = {}

    def _key(self, server: str, language: str) -> tuple[str, str]:
        if server != "asia" or language != self.settings.language:
            raise ApiError("目录服务器或语言不匹配")
        return server, language

    def load_cached(self, server: str, language: str) -> CatalogSnapshot | None:
        """Load a validated snapshot from disk without starting a refresh."""
        return self._load(self._key(server, language))

    def _load(self, key) -> CatalogSnapshot | None:
        try:
            active = self.paths.active(key[1])
            version = None
            path = self.settings.catalog_path(self.data_path)
            if active and active["client_language"] == self.settings.name_language:
                version = active["data_version"]
                path = self.paths.bundle(key[1], version) / "catalog.json"
            raw = read_json(path)
            if raw is None:
                return None
            if (
                not isinstance(raw, dict)
                or
                (version is not None and raw.get("data_version") != version)
                or raw["schema_version"] != 1
                or (raw["server"], raw["language"]) != key
                or not isinstance(raw["ships"], dict)
            ):
                raise ValueError
            stamp = raw["fetched_at"]
            if type(stamp) not in {int, float} or not 0 < stamp <= time.time() + 60:
                raise ValueError
            ships = tuple(
                parse_ship(value, ship_id) for ship_id, value in raw["ships"].items()
            )
            if not ships:
                raise ValueError
            pending = raw.get("pending_ship_ids", [])
            if (
                not isinstance(pending, list)
                or any(type(value) is not int or value <= 0 for value in pending)
                or not set(pending) <= {ship.ship_id for ship in ships}
            ):
                raise ValueError
            checked = active.get("checked_at", stamp) if version else stamp
            if type(checked) not in {int, float} or not 0 < checked <= time.time() + 60:
                raise ValueError
            return CatalogSnapshot(
                *key,
                stamp,
                ships,
                data_version=version,
                pending_ship_ids=frozenset(pending),
                checked_at=checked,
            )
        except (StorageError, ApiError, ValueError, KeyError, TypeError):
            logger.warning("目录缓存无效，保留原文件并重新抓取")
            return None

    def _start(self, key) -> asyncio.Task[CatalogSnapshot]:
        task = self.pending.get(key)
        if task is None:
            task = asyncio.create_task(
                asyncio.wait_for(
                    self.refresh_callback()
                    if self.refresh_callback
                    else self._refresh(key),
                    self.settings.update_timeout_seconds
                    if self.refresh_callback
                    else self.settings.operation_timeout_seconds,
                )
            )
            self.pending[key] = task

            def done(completed):
                self.pending.pop(key, None)
                if not completed.cancelled() and completed.exception() is not None:
                    self.retry_after[key] = time.monotonic() + 60
                    logger.warning(
                        "目录刷新失败，保留旧目录（%s）",
                        type(completed.exception()).__name__,
                    )
                elif not completed.cancelled():
                    self.retry_after.pop(key, None)

            task.add_done_callback(done)
        return task

    async def get_snapshot(self, server: str, language: str) -> CatalogSnapshot:
        key = self._key(server, language)
        try:
            active = await asyncio.to_thread(self.paths.active, language)
            current = self.snapshots.get(key)
            changed = (
                active
                and active["client_language"] == self.settings.name_language
                and (current is None or current.data_version != active["data_version"])
            )
        except (StorageError, KeyError, TypeError):
            active = None
            changed = False
            logger.warning("公共版本记录无效，继续使用已加载目录")
        if key not in self.snapshots or changed:
            snapshot = await asyncio.to_thread(self._load, key)
            if snapshot is not None:
                self.snapshots[key] = snapshot
        snapshot = self.snapshots.get(key)
        if (
            snapshot is not None
            and active
            and snapshot.data_version == active.get("data_version")
        ):
            checked = active.get("checked_at", snapshot.fetched_at)
            if type(checked) in {int, float} and 0 < checked <= time.time() + 60:
                snapshot = replace(snapshot, checked_at=checked)
                self.snapshots[key] = snapshot
        if snapshot is None:
            return await asyncio.shield(self._start(key))
        stale = (
            time.time() - (snapshot.checked_at or snapshot.fetched_at)
            >= self.settings.catalog_ttl_hours * 3600
        )
        if stale and time.monotonic() >= self.retry_after.get(key, 0):
            self._start(key)
        return replace(snapshot, stale=stale)

    async def refresh(self, server: str, language: str) -> CatalogSnapshot:
        return await asyncio.shield(self._start(self._key(server, language)))

    async def _refresh(self, key) -> CatalogSnapshot:
        snapshot = await self.fetch_snapshot(*key)
        await atomic_write(
            self.settings.catalog_path(self.data_path), catalog_payload(snapshot)
        )
        self.snapshots[key] = snapshot
        return snapshot

    async def fetch_snapshot(self, server: str, language: str) -> CatalogSnapshot:
        """完整取回候选目录，不覆盖磁盘或当前内存版本。"""
        key = self._key(server, language)
        first = await self.api.fetch_ship_page(1, key[1])
        if first.page_no != 1 or first.page_total != (first.total + 99) // 100:
            raise ApiError("目录分页数量不一致")
        ships = {}
        for number in range(1, first.page_total + 1):
            page = (
                first if number == 1 else await self.api.fetch_ship_page(number, key[1])
            )
            if (
                page.page_no != number
                or page.total != first.total
                or page.page_total != first.page_total
            ):
                raise ApiError("目录抓取期间分页发生变化")
            expected = min(100, first.total - (number - 1) * 100)
            if len(page.ships) != expected:
                raise ApiError("目录分页不完整")
            for ship in page.ships:
                if ship.ship_id in ships:
                    raise ApiError("目录存在重复舰船 ID")
                ships[ship.ship_id] = ship
        if len(ships) != first.total:
            raise ApiError("目录总数校验失败")
        return CatalogSnapshot(*key, time.time(), tuple(ships.values()))

    async def close(self) -> None:
        tasks = tuple(self.pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.pending.clear()


def catalog_payload(snapshot: CatalogSnapshot) -> dict:
    return {
        "schema_version": 1,
        "data_version": snapshot.data_version,
        "server": snapshot.server,
        "language": snapshot.language,
        "fetched_at": snapshot.fetched_at,
        "pending_ship_ids": sorted(snapshot.pending_ship_ids),
        "ships": {
            str(ship.ship_id): {
                "ship_id": ship.ship_id,
                "ship_id_str": ship.ship_code,
                "name": ship.name,
                "tier": ship.tier,
                "nation": ship.nation,
                "type": ship.ship_type.value,
                "is_premium": ship.is_premium,
                "is_special": ship.is_special,
                "images": {"contour": ship.contour_url, "large": ship.image_url},
            }
            for ship in snapshot.ships
        },
    }
