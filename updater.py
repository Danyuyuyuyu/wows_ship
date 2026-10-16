"""完整抓取后比较变化；名称缺失隔离，图片增量补齐，原子启用版本。"""

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import time
import uuid

from .cache import BusyError
from .catalog import catalog_payload
from .instance import InstanceLock
from .names import ClientNames, build_name_map, read_ship_messages
from .paths import WowsPaths
from .storage import StorageError, atomic_write, atomic_write_bytes, read_json

logger = logging.getLogger(__name__)


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class UpdateReport:
    data_version: str
    total_ships: int
    new_ships: int
    total_images: int
    new_images: int
    client_build: str
    modified_ships: int = 0
    removed_ships: int = 0
    pending_names: int = 0
    failed_images: int = 0
    published: bool = True


def build_order(build):
    return tuple(int(value) for value in re.findall(r"[0-9]+", build))


def content_hash(snapshot, names):
    clean_names = {key: value for key, value in names.items() if key != "data_version"}
    payload = {
        "ships": catalog_payload(snapshot)["ships"],
        "pending_ship_ids": sorted(snapshot.pending_ship_ids),
        "names": clean_names,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class DataUpdater:
    def __init__(
        self, api, settings, data_path, catalog, images, progress=None, workers=4
    ):
        self.api, self.settings = api, settings
        self.catalog, self.images = catalog, images
        self.paths = WowsPaths(data_path)
        if not 1 <= workers <= 8:
            raise ValueError("图片下载并发数必须为 1–8")
        self.workers, self.progress = workers, progress
        self.pending = None
        self.pending_force = False
        self.pending_verify = False
        self.synced_translation_source = None
        self._process_lock = InstanceLock(self.paths.root / "update.lock")

    async def _run_update(self, force, verify):
        await asyncio.to_thread(self._process_lock.acquire)
        try:
            return await self._update(force, verify)
        finally:
            await asyncio.to_thread(self._process_lock.release)

    async def update(self, force=True, verify=False):
        while self.pending is not None and (
            (force and not self.pending_force) or (verify and not self.pending_verify)
        ):
            previous = self.pending
            await asyncio.shield(previous)
            if self.pending is previous and previous.done():
                self.pending = None
        if self.pending is None:
            task = asyncio.create_task(
                asyncio.wait_for(
                    self._run_update(force, verify),
                    self.settings.update_timeout_seconds,
                )
            )
            self.pending = task
            self.pending_force, self.pending_verify = force, verify
            task.add_done_callback(self._done)
        return await asyncio.shield(self.pending)

    def _done(self, task):
        if self.pending is task:
            self.pending = None
        if not task.cancelled():
            task.exception()

    async def refresh_snapshot(self):
        await self.update(force=True)
        return self.catalog.snapshots[("asia", self.settings.language)]

    def _valid_translation_source(self, root):
        try:
            for language in {self.settings.name_language, "en"}:
                read_ship_messages(root / language / "LC_MESSAGES/global.mo")
            return True
        except (OSError, ValueError):
            return False

    def translation_source(self):
        """Return the currently selected client translation source."""
        return self._translation_source()

    def _translation_source(self):
        if self.settings.client_translations_path:
            root = Path(self.settings.client_translations_path)
            if not root.is_absolute():
                root = self.paths.data_path.parent / root
            return root, root.parent.name
        base = self.paths.data_path.parent / "wows_replay_data"
        choices = []
        if base.is_dir():
            for build in base.iterdir():
                root = build / "translations"
                if (
                    re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+_[0-9]+", build.name)
                    and (
                        root / self.settings.name_language / "LC_MESSAGES/global.mo"
                    ).is_file()
                    and (root / "en/LC_MESSAGES/global.mo").is_file()
                    and self._valid_translation_source(root)
                ):
                    choices.append((root, build.name))
        synced = self.synced_translation_source
        if synced is not None and self._valid_translation_source(synced[0]):
            choices.append(synced)
        return max(
            choices,
            key=lambda item: (build_order(item[1]), item == synced),
            default=None,
        )

    def _names(self, snapshot):
        fallback = ClientNames(
            self.paths.data_path,
            self.settings.name_language,
            self.settings.language,
            staging=True,
        )
        source = self._translation_source()
        if source and (
            self.settings.client_translations_path
            or build_order(source[1]) >= build_order(fallback.game_build)
        ):
            root, build = source
            payload = (
                build_name_map(
                    catalog_payload(snapshot),
                    root / self.settings.name_language / "LC_MESSAGES/global.mo",
                    root / "en/LC_MESSAGES/global.mo",
                    self.settings.name_language,
                    build,
                )
                if all(ship.ship_code for ship in snapshot.ships)
                else deepcopy(fallback.payload)
            )
        else:
            payload = deepcopy(fallback.payload)
        if not payload:
            raise UpdateError("缺少客户端名称表，请先导入客户端语言文件。")
        missing = sorted(
            ship.ship_id
            for ship in snapshot.ships
            if ship.ship_code and ship.ship_code not in payload["names"]
        )
        old_ids = payload.get("ship_ids", {})
        payload["ship_ids"] = {
            str(ship.ship_id): ship.ship_code or old_ids[str(ship.ship_id)]
            for ship in snapshot.ships
            if ship.ship_code or str(ship.ship_id) in old_ids
        }
        payload["unmapped_ids"] = missing
        return payload

    @staticmethod
    def _display(ship, names):
        code = ship.ship_code or names["ship_ids"].get(str(ship.ship_id))
        if ship.ship_code and code not in names["names"]:
            return None
        return names["names"].get(code) or names["english_names"].get(code) or ship.name

    def _previous(self, snapshot):
        if snapshot is None or snapshot.data_version is None:
            return None, {}, None, False
        try:
            bundle = self.paths.bundle(snapshot.language, snapshot.data_version)
            names = ClientNames(
                self.paths.data_path,
                self.settings.name_language,
                self.settings.language,
            )
            names.reload(snapshot.data_version)
            payloads = [
                read_json(bundle / item)
                for item in ("catalog.json", "ships.json", "download.json")
            ]
            if any(
                not isinstance(row, dict)
                or row.get("data_version") != snapshot.data_version
                for row in payloads
            ):
                raise StorageError("已启用版本文件不完整")
            if catalog_payload(snapshot)["ships"] != payloads[0]["ships"]:
                raise StorageError("已启用目录与磁盘记录不匹配")
            manifest = payloads[2]
            checks = read_json(self.paths.checks(snapshot.language))
            if (
                isinstance(checks, dict)
                and checks.get("data_version") == snapshot.data_version
            ):
                manifest = checks
            return (
                content_hash(snapshot, names.payload),
                manifest.get("images", {}),
                names.payload,
                True,
            )
        except (StorageError, KeyError, TypeError, ValueError):
            logger.warning("旧版本文件需要修复，将准备新快照")
            return None, {}, None, False

    async def _images(self, snapshot, previous, verify):
        urls = sorted(
            {
                url
                for ship in snapshot.ships
                for url in (ship.image_url, ship.contour_url)
                if url
            }
        )
        queue = asyncio.Queue()
        for url in urls:
            self.images.path_for(url)
            queue.put_nowait(url)
        rows, repaired = {}, 0

        async def worker():
            nonlocal repaired
            while True:
                try:
                    url = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                path = self.images.path_for(url)
                try:
                    prior = previous.get(url, {})
                    info = (
                        await asyncio.to_thread(path.stat) if path.is_file() else None
                    )
                    reusable = (
                        not verify
                        and info is not None
                        and prior.get("ok") is True
                        and info.st_size == prior.get("bytes")
                        and info.st_mtime_ns == prior.get("mtime_ns")
                        and isinstance(prior.get("sha256"), str)
                    )
                    if reusable:
                        rows[url] = deepcopy(prior)
                    else:
                        raw = await asyncio.to_thread(self.images.read_cached, path)
                        missing = raw is None
                        if missing:
                            raw = await self.images.get(url)
                            await atomic_write_bytes(path, raw)
                        info = await asyncio.to_thread(path.stat)
                        if missing:
                            repaired += 1
                        rows[url] = {
                            "local_path": path.relative_to(self.paths.root).as_posix(),
                            "ok": True,
                            "bytes": len(raw),
                            "mtime_ns": info.st_mtime_ns,
                            "sha256": hashlib.sha256(raw).hexdigest(),
                        }
                except Exception as error:
                    rows[url] = {
                        "local_path": path.relative_to(self.paths.root).as_posix(),
                        "ok": False,
                        "bytes": 0,
                        "error": type(error).__name__,
                    }
                    logger.warning(
                        "舰图暂未补齐，可使用无图结果（%s）", type(error).__name__
                    )
                if self.progress and len(rows) % 50 == 0:
                    self.progress({"completed": len(rows), "total_images": len(urls)})

        tasks = [asyncio.create_task(worker()) for _ in range(self.workers)]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return rows, repaired

    async def _publish_metadata(self, row, manifest=None, allow_reuse=True):
        lock = self.paths.root / "metadata.lock"
        acquired = False
        try:
            for _ in range(150):
                try:
                    descriptor = os.open(
                        lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                    )
                    os.close(descriptor)
                    acquired = True
                    break
                except FileExistsError:
                    try:
                        if time.time() - lock.stat().st_mtime > 300:
                            lock.unlink()
                            continue
                    except FileNotFoundError:
                        continue
                    await asyncio.sleep(0.1)
            if not acquired:
                raise BusyError("另一个船库更新正在提交，请稍后重试。")
            metadata = read_json(self.paths.metadata)
            if metadata is None:
                metadata = {"schema_version": 1, "active": {}}
            if (
                not isinstance(metadata, dict)
                or metadata.get("schema_version") != 1
                or not isinstance(metadata.get("active"), dict)
            ):
                raise StorageError("公共版本记录损坏，原版本保留。")
            current = metadata["active"].get(self.settings.language)
            if (
                current
                and current.get("checked_at", current["catalog_fetched_at"])
                > row["checked_at"]
            ):
                raise UpdateError("已有更新的船库检查结果，本次较旧结果未启用。")
            published = not current or current["data_version"] != row["data_version"]
            if (
                current
                and allow_reuse
                and current.get("content_hash") == row["content_hash"]
            ):
                row = dict(
                    row,
                    data_version=current["data_version"],
                    catalog_fetched_at=current["catalog_fetched_at"],
                )
                published = False
            metadata["active"][self.settings.language] = row
            metadata["updated_at"] = time.time()
            await atomic_write(self.paths.metadata, metadata)
            if manifest is not None:
                manifest["data_version"] = row["data_version"]
                for path in (
                    self.paths.checks(self.settings.language),
                    self.paths.manifest,
                    self.paths.root / "download_manifest.json",
                ):
                    try:
                        await atomic_write(path, manifest)
                    except StorageError:
                        logger.warning("船库已检查，最新图片状态副本写入失败")
            return row, published
        finally:
            if acquired:
                lock.unlink(missing_ok=True)

    def cleanup_candidates(self):
        """仅列出超过保留期的版本，不删除任何快照、图片或玩家记录。"""
        base = self.paths.root / "snapshots" / self.settings.language
        if not base.is_dir():
            return []
        active = self.paths.active(self.settings.language)
        current = active["data_version"] if active else None
        minimum_age = max(
            self.settings.snapshot_retention_days * 86400,
            self.settings.operation_timeout_seconds
            + self.settings.render_timeout_seconds
            + self.settings.update_timeout_seconds
            + 60,
        )
        candidates = [
            (path.stat().st_mtime, path.name)
            for path in base.iterdir()
            if re.fullmatch(r"[0-9a-f]{32}", path.name)
            and path.is_dir()
            and not path.is_symlink()
        ]
        candidates.sort(reverse=True)
        kept = {name for _, name in candidates[: self.settings.snapshot_keep_count]}
        return [
            name
            for stamp, name in candidates
            if name != current
            and name not in kept
            and time.time() - stamp >= minimum_age
        ]

    async def _update(self, force, verify):
        ancestor = self.paths.root
        while not ancestor.exists():
            ancestor = ancestor.parent
        free = (await asyncio.to_thread(shutil.disk_usage, ancestor)).free
        if free < self.settings.disk_min_free_mb * 1024 * 1024:
            raise StorageError("剩余磁盘空间不足，暂停船库下载")
        language = self.settings.language
        old = self.catalog.snapshots.get(("asia", language))
        disk = await asyncio.to_thread(self.catalog.load_cached, "asia", language)
        if disk is not None:
            old = disk
        snapshot = (
            await self.catalog.fetch_snapshot("asia", language)
            if force or old is None
            else old
        )
        checked_at = snapshot.checked_at or snapshot.fetched_at
        names = await asyncio.to_thread(self._names, snapshot)
        snapshot = replace(
            snapshot, pending_ship_ids=frozenset(names["unmapped_ids"]), stale=False
        )
        digest = content_hash(snapshot, names)
        old_digest, previous, old_names, healthy = await asyncio.to_thread(
            self._previous, old
        )
        active = await asyncio.to_thread(self.paths.active, language)
        last_verified = active.get("last_verified_at", 0) if active else 0
        full_verify = (
            verify
            or time.time() - last_verified >= self.settings.image_verify_days * 86400
        )
        rows, repaired = await self._images(snapshot, previous, full_verify)
        failures = sum(row["ok"] is False for row in rows.values())
        version = (
            old.data_version if healthy and digest == old_digest else uuid.uuid4().hex
        )
        snapshot = replace(snapshot, data_version=version, checked_at=checked_at)
        if healthy and version == old.data_version:
            snapshot = replace(snapshot, fetched_at=old.fetched_at)
        names["data_version"] = version
        old_ships = {ship.ship_id: ship for ship in old.ships} if old else {}
        current_ships = {ship.ship_id: ship for ship in snapshot.ships}
        new = len(current_ships.keys() - old_ships.keys())
        removed = len(old_ships.keys() - current_ships.keys())
        modified = sum(
            old_ships[key] != current_ships[key]
            or (
                old_names is not None
                and self._display(old_ships[key], old_names)
                != self._display(current_ships[key], names)
            )
            for key in old_ships.keys() & current_ships.keys()
        )
        manifest = {
            "schema_version": 1,
            "data_version": version,
            "server": "asia",
            "updated_at": time.time(),
            "complete": failures == 0,
            "ships": len(snapshot.ships),
            "total_images": len(rows),
            "completed": len(rows),
            "successful": len(rows) - failures,
            "failed": failures,
            "bytes": sum(row["bytes"] for row in rows.values()),
            "images": rows,
        }
        exported = {
            "schema_version": 1,
            "data_version": version,
            "server": "asia",
            "api_language": language,
            "client_language": self.settings.name_language,
            "client_build": names["source"]["game_build"],
            "catalog_fetched_at": snapshot.fetched_at,
            "exported_at": time.time(),
            "pending_ship_ids": sorted(snapshot.pending_ship_ids),
            "ships": {
                str(ship.ship_id): {
                    "ship_id": ship.ship_id,
                    "ship_id_str": ship.ship_code,
                    "name": self._display(ship, names),
                    "api_name": ship.name,
                    "selectable": ship.ship_id not in snapshot.pending_ship_ids,
                    "tier": ship.tier,
                    "nation": ship.nation,
                    "type": ship.ship_type.value,
                    "is_premium": ship.is_premium,
                    "is_special": ship.is_special,
                    "images": {
                        kind: {
                            "url": url,
                            "local_path": rows[url]["local_path"],
                            "downloaded": rows[url]["ok"],
                        }
                        for kind, url in (
                            ("large", ship.image_url),
                            ("contour", ship.contour_url),
                        )
                        if url
                    },
                }
                for ship in snapshot.ships
            },
        }
        raw_catalog = catalog_payload(snapshot)
        if not healthy or version != old.data_version:
            bundle = self.paths.bundle(language, version)
            for path, payload in (
                (bundle / "catalog.json", raw_catalog),
                (bundle / "names" / f"{self.settings.name_language}.json", names),
                (bundle / "ships.json", exported),
                (bundle / "download.json", manifest),
            ):
                await atomic_write(path, payload)
        row = {
            "data_version": version,
            "client_language": self.settings.name_language,
            "client_build": names["source"]["game_build"],
            "catalog_fetched_at": snapshot.fetched_at,
            "checked_at": checked_at,
            "updated_at": time.time(),
            "content_hash": digest,
            "last_verified_at": time.time()
            if full_verify and not failures
            else last_verified,
            "ships": len(snapshot.ships),
            "images": len(rows),
            "pending_names": len(snapshot.pending_ship_ids),
            "failed_images": failures,
        }
        row, published = await self._publish_metadata(row, manifest, healthy)
        snapshot = replace(
            snapshot,
            data_version=row["data_version"],
            fetched_at=row["catalog_fetched_at"],
        )
        self.catalog.snapshots[("asia", language)] = snapshot
        await self._mirror(row, names, snapshot, exported)
        return UpdateReport(
            snapshot.data_version,
            len(snapshot.ships),
            new,
            len(rows),
            repaired,
            names["source"]["game_build"],
            modified,
            removed,
            len(snapshot.pending_ship_ids),
            failures,
            published,
        )

    async def _mirror(self, row, names, snapshot, exported):
        lock = self.paths.root / "metadata.lock"
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            logger.info("另一个更新正在提交，跳过兼容副本写入")
            return
        os.close(descriptor)
        try:
            current = self.paths.active(self.settings.language)
            if (
                not current
                or current["data_version"] != row["data_version"]
                or current["checked_at"] != row["checked_at"]
            ):
                return
            names["data_version"] = snapshot.data_version
            exported["data_version"] = snapshot.data_version
            exported["catalog_fetched_at"] = snapshot.fetched_at
            for path, payload in (
                (self.paths.export, exported),
                (self.paths.catalog(self.settings.language), catalog_payload(snapshot)),
                (self.paths.names(self.settings.name_language), names),
                (self.paths.root / "ships.json", exported),
            ):
                try:
                    await atomic_write(path, payload)
                except StorageError:
                    logger.warning("船库版本已启用，兼容导出副本写入失败")
        finally:
            lock.unlink(missing_ok=True)

    async def close(self):
        if self.pending is not None:
            self.pending.cancel()
            await asyncio.gather(self.pending, return_exceptions=True)
