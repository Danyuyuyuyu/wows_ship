from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import stat
import tempfile
import time
from typing import Any
import uuid

from .api import ApiError, parse_ship
from .cache import BusyError
from .catalog import catalog_payload
from .instance import InstanceLock
from .models import CatalogSnapshot
from .names import ClientNames
from .paths import WowsPaths
from .storage import StorageError

_VERSION = re.compile(r"[0-9a-f]{32}\Z")
_PLAYER_FILE = re.compile(r"[1-9][0-9]*(?:\.history)?\.json(?:\.bak)?\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _valid_stamp(value: object, *, allow_zero: bool = False) -> bool:
    return (
        type(value) in {int, float}
        and math.isfinite(value)
        and (value >= 0 if allow_zero else value > 0)
        and value <= time.time() + 60
    )


def _read_regular_bytes(path: Path) -> bytes | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise StorageError("数据文件无法读取") from error
    if not stat.S_ISREG(info.st_mode):
        raise StorageError("数据文件不是普通文件")
    try:
        return path.read_bytes()
    except OSError as error:
        raise StorageError("数据文件无法读取") from error


def _read_json_bytes(path: Path) -> Any:
    raw = _read_regular_bytes(path)
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
        if value is None:
            raise ValueError("null document")
        return value
    except (UnicodeDecodeError, ValueError, TypeError) as error:
        raise StorageError("数据文件无法读取或格式损坏") from error


def _is_real_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except (FileNotFoundError, OSError):
        return False


def _snapshot_chain_is_real(paths: WowsPaths, language: str) -> bool:
    return all(
        _is_real_directory(path)
        for path in (
            paths.root,
            paths.root / "snapshots",
            paths.root / "snapshots" / language,
        )
    )


def _validate_name_payload(
    paths: WowsPaths, language: str, client_language: str, version: str
) -> dict:
    """Reuse ClientNames' production schema validation against this bundle."""
    try:
        payload = ClientNames.load_bundle_payload(
            paths, language, client_language, version
        )
    except StorageError:
        raise
    if payload.get("language") != client_language:
        raise StorageError("快照客户端语言不匹配")
    return payload


def _validate_bundle(
    paths: WowsPaths, language: str, client_language: str, version: str
) -> dict | None:
    if not _VERSION.fullmatch(version):
        return None
    try:
        bundle = paths.bundle(language, version)
        if not _snapshot_chain_is_real(paths, language):
            return None
        if not _is_real_directory(bundle) or not _is_real_directory(bundle / "names"):
            return None
        catalog = _read_json_bytes(bundle / "catalog.json")
        exported = _read_json_bytes(bundle / "ships.json")
        download = _read_json_bytes(bundle / "download.json")
        names = _validate_name_payload(paths, language, client_language, version)
        if not all(isinstance(row, dict) for row in (catalog, exported, download)):
            return None
        if (
            type(catalog.get("schema_version")) is not int
            or catalog.get("schema_version") != 1
            or catalog.get("data_version") != version
            or catalog.get("server") != "asia"
            or catalog.get("language") != language
            or type(exported.get("schema_version")) is not int
            or exported.get("schema_version") != 1
            or exported.get("data_version") != version
            or exported.get("server") != "asia"
            or exported.get("api_language") != language
            or exported.get("client_language") != client_language
            or type(download.get("schema_version")) is not int
            or download.get("schema_version") != 1
            or download.get("data_version") != version
            or download.get("server") != "asia"
            or names.get("data_version") != version
        ):
            return None
        fetched_at = catalog.get("fetched_at")
        if not _valid_stamp(fetched_at):
            return None
        raw_ships = catalog.get("ships")
        if not isinstance(raw_ships, dict) or not raw_ships:
            return None
        ships = []
        for key, raw_ship in raw_ships.items():
            if not isinstance(key, str) or not key.isascii() or not key.isdecimal():
                return None
            if str(int(key)) != key:
                return None
            ships.append(parse_ship(raw_ship, key))
        ship_ids = {ship.ship_id for ship in ships}
        if len(ship_ids) != len(ships):
            return None

        pending = catalog.get("pending_ship_ids", [])
        exported_pending = exported.get("pending_ship_ids", [])
        unmapped = names.get("unmapped_ids", [])
        if any(
            not isinstance(value, list)
            for value in (pending, exported_pending, unmapped)
        ):
            return None
        for identifiers in (pending, exported_pending, unmapped):
            if (
                any(type(value) is not int or value <= 0 for value in identifiers)
                or len(set(identifiers)) != len(identifiers)
                or not set(identifiers) <= ship_ids
            ):
                return None
        if set(pending) != set(exported_pending) or set(pending) != set(unmapped):
            return None

        exported_ships = exported.get("ships")
        images = download.get("images")
        if not isinstance(exported_ships, dict) or not isinstance(images, dict):
            return None
        if set(exported_ships) != {str(value) for value in ship_ids}:
            return None
        pending_set = set(pending)
        for key, row in exported_ships.items():
            if (
                not isinstance(row, dict)
                or type(row.get("ship_id")) is not int
                or row.get("ship_id") != int(key)
            ):
                return None
            if "selectable" in row and (
                type(row["selectable"]) is not bool
                or row["selectable"] != (int(key) not in pending_set)
            ):
                return None

        # Failed image rows remain valid; they affect availability, not integrity.
        timestamp = download.get("updated_at", fetched_at)
        if not _valid_stamp(timestamp):
            timestamp = fetched_at
        snapshot = CatalogSnapshot(
            "asia",
            language,
            fetched_at,
            tuple(ships),
            data_version=version,
            pending_ship_ids=frozenset(pending),
            checked_at=fetched_at,
        )
        return {
            "bundle": bundle,
            "catalog": catalog,
            "ships": exported,
            "download": download,
            "names": names,
            "snapshot": snapshot,
            "timestamp": timestamp,
        }
    except (StorageError, ApiError, OSError, KeyError, TypeError, ValueError):
        return None


def _metadata_state(paths: WowsPaths) -> tuple[bytes | None, Any, bool]:
    try:
        root_mode = paths.root.lstat().st_mode
    except FileNotFoundError:
        return None, None, False
    except OSError as error:
        raise StorageError("公共数据目录无法读取") from error
    if not stat.S_ISDIR(root_mode):
        raise StorageError("公共数据目录无效")
    raw = _read_regular_bytes(paths.metadata)
    if raw is None:
        return None, None, False
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError):
        return raw, None, False
    valid = (
        isinstance(payload, dict)
        and type(payload.get("schema_version")) is int
        and payload.get("schema_version") == 1
        and isinstance(payload.get("active"), dict)
    )
    return raw, payload, valid


def _active_is_healthy(
    paths: WowsPaths, metadata: Any, language: str, client_language: str
) -> bool:
    if not isinstance(metadata, dict) or not isinstance(metadata.get("active"), dict):
        return False
    row = metadata["active"].get(language)
    if not isinstance(row, dict) or row.get("client_language") != client_language:
        return False
    version = row.get("data_version")
    return (
        isinstance(version, str)
        and _validate_bundle(paths, language, client_language, version) is not None
    )


def _latest_valid_bundle(
    paths: WowsPaths, language: str, client_language: str
) -> dict | None:
    base = paths.root / "snapshots" / language
    if not _snapshot_chain_is_real(paths, language):
        return None
    candidates = []
    try:
        entries = tuple(base.iterdir())
    except OSError as error:
        raise StorageError("公共快照目录无法读取") from error
    for entry in entries:
        if not _VERSION.fullmatch(entry.name) or not _is_real_directory(entry):
            continue
        row = _validate_bundle(paths, language, client_language, entry.name)
        if row is not None:
            candidates.append(
                (row["snapshot"].fetched_at, row["timestamp"], entry.name, row)
            )
    return max(candidates, default=(0, 0, "", None), key=lambda item: item[:3])[3]


def _atomic_bytes(path: Path, data: bytes) -> None:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            pass
    except OSError as error:
        raise StorageError("存储文件写入失败") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, payload: Any) -> None:
    try:
        raw = (
            json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise StorageError("存储清单格式无效") from error
    _atomic_bytes(path, raw)


def _quarantine_metadata(paths: WowsPaths, raw: bytes) -> Path:
    target_dir = paths.root / "quarantine"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        if not _is_real_directory(target_dir):
            raise StorageError("隔离目录无效")
        for _ in range(10):
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            target = target_dir / f"metadata-{stamp}-{uuid.uuid4().hex}.json"
            try:
                with target.open("xb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                return target
            except FileExistsError:
                continue
    except OSError as error:
        raise StorageError("损坏的公共版本记录无法保存到隔离目录") from error
    raise StorageError("损坏的公共版本记录隔离文件名冲突")


def _content_hash(snapshot: CatalogSnapshot, names: dict) -> str:
    payload = {
        "ships": catalog_payload(snapshot)["ships"],
        "pending_ship_ids": sorted(snapshot.pending_ship_ids),
        "names": {key: value for key, value in names.items() if key != "data_version"},
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _needs_degraded_metadata(
    metadata: Any, valid_metadata: bool, language: str
) -> bool:
    if not valid_metadata:
        return True
    active = metadata["active"]
    return language in active and active[language] is not None


def _metadata_without_current_language(
    metadata: Any, valid_metadata: bool, language: str
) -> dict:
    if valid_metadata:
        result = dict(metadata)
        result["active"] = dict(metadata["active"])
    elif isinstance(metadata, dict) and isinstance(metadata.get("active"), dict):
        result = {"schema_version": 1, "active": dict(metadata["active"])}
        updated_at = metadata.get("updated_at")
        if _valid_stamp(updated_at):
            result["updated_at"] = updated_at
    else:
        result = {"schema_version": 1, "active": {}}
    result["active"].pop(language, None)
    return result


def _remove_stale_metadata_lock(lock: Path) -> bool:
    """Remove only an unchanged metadata lease older than the updater's 300s limit."""
    try:
        first = lock.lstat()
    except FileNotFoundError:
        return True
    except OSError as error:
        raise StorageError("无法检查公共版本锁") from error
    if time.time() - first.st_mtime <= 300:
        return False

    try:
        current = lock.lstat()
    except FileNotFoundError:
        return True
    except OSError as error:
        raise StorageError("无法检查公共版本锁") from error
    if (current.st_dev, current.st_ino, current.st_mtime_ns) != (
        first.st_dev,
        first.st_ino,
        first.st_mtime_ns,
    ) or time.time() - current.st_mtime <= 300:
        return False
    try:
        lock.unlink()
    except FileNotFoundError:
        return True
    except OSError as error:
        raise StorageError("无法回收过期公共版本锁") from error
    return True


def _acquire_metadata_lock(lock: Path) -> tuple[int, int]:
    """Acquire the same bounded O_EXCL lease used by the catalog updater."""
    for _ in range(150):
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if _remove_stale_metadata_lock(lock):
                continue
            time.sleep(0.1)
            continue
        except OSError as error:
            raise StorageError("无法锁定公共版本记录") from error
        try:
            info = os.fstat(descriptor)
            return info.st_dev, info.st_ino
        finally:
            os.close(descriptor)
    raise StorageError("公共数据正在提交，自动恢复暂时跳过")


def _release_metadata_lock(lock: Path, identity: tuple[int, int]) -> None:
    try:
        info = lock.lstat()
        if (info.st_dev, info.st_ino) == identity:
            lock.unlink()
    except OSError:
        pass


def _recover_public(settings: Any, data_path: Path) -> dict | None:
    paths = WowsPaths(data_path)
    language = settings.language
    client_language = settings.name_language
    raw, metadata, valid_metadata = _metadata_state(paths)
    if valid_metadata and _active_is_healthy(
        paths, metadata, language, client_language
    ):
        return None
    candidate = _latest_valid_bundle(paths, language, client_language)
    if candidate is None and not (
        raw is not None and _needs_degraded_metadata(metadata, valid_metadata, language)
    ):
        return None

    lock = paths.root / "metadata.lock"
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise StorageError("无法锁定公共版本记录") from error
    lock_identity = _acquire_metadata_lock(lock)
    try:
        raw, metadata, valid_metadata = _metadata_state(paths)
        if valid_metadata and _active_is_healthy(
            paths, metadata, language, client_language
        ):
            return None
        candidate = _latest_valid_bundle(paths, language, client_language)
        if candidate is None:
            if raw is None or not _needs_degraded_metadata(
                metadata, valid_metadata, language
            ):
                return None
            quarantined = _quarantine_metadata(paths, raw)
            new_metadata = _metadata_without_current_language(
                metadata, valid_metadata, language
            )
            _atomic_json(paths.metadata, new_metadata)
            return {
                "status": "degraded",
                "reason": "no_valid_snapshot",
                "language": language,
                "quarantined_metadata": str(quarantined),
            }

        bundle_version = candidate["snapshot"].data_version
        original_row = (
            metadata.get("active", {}).get(language)
            if isinstance(metadata, dict) and isinstance(metadata.get("active"), dict)
            else None
        )
        same_version = (
            isinstance(original_row, dict)
            and original_row.get("data_version") == bundle_version
        )
        fetched_at = candidate["snapshot"].fetched_at
        checked_at = original_row.get("checked_at") if same_version else fetched_at
        if not _valid_stamp(checked_at):
            checked_at = fetched_at
        updated_at = candidate["timestamp"]
        names = candidate["names"]
        snapshot = candidate["snapshot"]
        row = dict(original_row) if same_version else {}
        download = candidate["download"]
        download_images = download.get("total_images", len(download.get("images", {})))
        failed_images = download.get("failed", 0)
        if type(download_images) is not int or download_images < 0:
            download_images = len(download.get("images", {}))
        if type(failed_images) is not int or failed_images < 0:
            failed_images = 0
        row.update(
            {
                "data_version": bundle_version,
                "client_language": client_language,
                "client_build": names["source"]["game_build"],
                "catalog_fetched_at": fetched_at,
                "checked_at": checked_at,
                "updated_at": updated_at,
                "content_hash": _content_hash(snapshot, names),
                "last_verified_at": (
                    original_row.get("last_verified_at", 0)
                    if same_version and isinstance(original_row, dict)
                    else 0
                ),
                "ships": len(snapshot.ships),
                "images": download_images,
                "pending_names": len(snapshot.pending_ship_ids),
                "failed_images": failed_images,
            }
        )
        if not _valid_stamp(row["last_verified_at"], allow_zero=True):
            row["last_verified_at"] = 0

        if valid_metadata:
            new_metadata = dict(metadata)
            new_metadata["active"] = dict(metadata["active"])
        elif isinstance(metadata, dict) and isinstance(metadata.get("active"), dict):
            new_metadata = {"schema_version": 1, "active": dict(metadata["active"])}
        else:
            new_metadata = {"schema_version": 1, "active": {}}
        new_metadata["active"][language] = row
        top_stamp = metadata.get("updated_at") if isinstance(metadata, dict) else None
        new_metadata["updated_at"] = (
            top_stamp if _valid_stamp(top_stamp) else updated_at
        )

        quarantined = None
        if raw is not None and not valid_metadata:
            quarantined = _quarantine_metadata(paths, raw)
        _atomic_json(paths.metadata, new_metadata)
        return {
            "status": "recovered",
            "language": language,
            "data_version": bundle_version,
            "checked_at": checked_at,
            "quarantined_metadata": str(quarantined) if quarantined else None,
        }
    finally:
        _release_metadata_lock(lock, lock_identity)


async def _run_in_thread(function: Callable[..., Any], *args: Any) -> Any:
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


async def recover_public(settings: Any, data_path: Path) -> dict | None:
    """Restore active metadata from the newest complete, validated immutable bundle."""
    return await _run_in_thread(_recover_public, settings, Path(data_path))


def disk_status(data_path: Path) -> dict[str, int]:
    """Return filesystem byte counts without changing or cleaning any files."""
    usage = shutil.disk_usage(Path(data_path))
    return {
        "free_bytes": usage.free,
        "total_bytes": usage.total,
        "used_bytes": usage.used,
    }


def _safe_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except (FileNotFoundError, OSError):
        return False


def _collect_sources(paths: WowsPaths, metadata: Any) -> list[tuple[str, Path]]:
    sources: dict[str, Path] = {}
    root = paths.root
    if not _is_real_directory(root):
        return []
    for relative in ("bindings.json", "bindings.json.bak", "metadata.json"):
        path = root / relative
        if _safe_regular_file(path):
            sources[f"wows_ship/{relative}"] = path

    players = paths.players
    if _is_real_directory(players):
        try:
            users = tuple(players.iterdir())
        except OSError as error:
            raise StorageError("玩家数据目录无法读取") from error
        for user in users:
            if (
                not user.name.isascii()
                or not user.name.isdecimal()
                or not _is_real_directory(user)
            ):
                continue
            asia = user / "asia"
            if not _is_real_directory(asia):
                continue
            try:
                files = tuple(asia.iterdir())
            except OSError as error:
                raise StorageError("玩家账号目录无法读取") from error
            for path in files:
                if _PLAYER_FILE.fullmatch(path.name) and _safe_regular_file(path):
                    relative = path.relative_to(paths.data_path).as_posix()
                    sources[relative] = path

    if isinstance(metadata, dict) and isinstance(metadata.get("active"), dict):
        for language, row in metadata["active"].items():
            if not isinstance(language, str) or not isinstance(row, dict):
                continue
            version = row.get("data_version")
            if not isinstance(version, str) or not _VERSION.fullmatch(version):
                continue
            try:
                bundle = paths.bundle(language, version)
            except StorageError:
                continue
            if not _snapshot_chain_is_real(paths, language):
                continue
            if not _is_real_directory(bundle) or not _is_real_directory(
                bundle / "names"
            ):
                continue
            client_language = row.get("client_language")
            if not isinstance(client_language, str) or not client_language:
                continue
            for relative in (
                "catalog.json",
                f"names/{client_language}.json",
                "ships.json",
                "download.json",
            ):
                path = bundle / relative
                if _safe_regular_file(path):
                    key = path.relative_to(paths.data_path).as_posix()
                    sources[key] = path
    return sorted(sources.items())


def _read_sources(paths: WowsPaths) -> list[tuple[str, bytes]]:
    for _ in range(3):
        try:
            metadata_bytes = _read_regular_bytes(paths.metadata)
        except StorageError:
            metadata_bytes = None
        metadata = None
        if metadata_bytes is not None:
            try:
                metadata = json.loads(metadata_bytes.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, TypeError):
                pass
        sources = _collect_sources(paths, metadata)
        results = []
        for relative, source in sources:
            if relative == "wows_ship/metadata.json":
                raw = metadata_bytes
                if raw is None:
                    continue
            else:
                try:
                    mode = source.lstat().st_mode
                    if not stat.S_ISREG(mode):
                        continue
                    raw = source.read_bytes()
                except FileNotFoundError:
                    continue
                except OSError as error:
                    raise StorageError("备份源文件无法读取") from error
            results.append((relative, raw))
        try:
            current_metadata_bytes = _read_regular_bytes(paths.metadata)
        except StorageError:
            current_metadata_bytes = None
        if current_metadata_bytes == metadata_bytes:
            return results
    raise StorageError("备份期间公共版本指针持续变化")


def _source_fingerprint(files: list[tuple[str, bytes]]) -> tuple[str, list[dict]]:
    entries = [
        {
            "source": relative,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        for relative, raw in files
    ]
    canonical = json.dumps(
        entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), entries


def _load_backup_state(path: Path) -> dict | None:
    try:
        payload = _read_json_bytes(path)
    except StorageError:
        return None
    if (
        not isinstance(payload, dict)
        or type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != 1
    ):
        return None
    if (
        not isinstance(payload.get("fingerprint"), str)
        or not _SHA256.fullmatch(payload["fingerprint"])
        or not isinstance(payload.get("backup_name"), str)
        or Path(payload["backup_name"]).name != payload["backup_name"]
        or not _valid_stamp(payload.get("successful_at"))
    ):
        return None
    return payload


def _verify_backup(backup_path: Path) -> bool:
    root = Path(backup_path)
    if not _is_real_directory(root):
        return False
    try:
        manifest = _read_json_bytes(root / "manifest.json")
    except (StorageError, OSError):
        return False
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != 1
        or manifest.get("complete") is not True
        or not isinstance(manifest.get("files"), list)
        or not isinstance(manifest.get("fingerprint"), str)
        or not _SHA256.fullmatch(manifest["fingerprint"])
    ):
        return False
    try:
        root_resolved = root.resolve(strict=True)
        seen_paths = set()
        seen_sources = set()
        for entry in manifest["files"]:
            if not isinstance(entry, dict):
                return False
            relative = entry.get("path")
            source = entry.get("source")
            size = entry.get("bytes")
            digest = entry.get("sha256")
            if (
                not isinstance(relative, str)
                or "\\" in relative
                or not isinstance(source, str)
                or "\\" in source
                or type(size) is not int
                or size < 0
                or not isinstance(digest, str)
                or not _SHA256.fullmatch(digest)
            ):
                return False
            posix = PurePosixPath(relative)
            windows = PureWindowsPath(relative)
            src_posix = PurePosixPath(source)
            src_windows = PureWindowsPath(source)
            if (
                posix.is_absolute()
                or windows.is_absolute()
                or windows.drive
                or any(part in {"", ".", ".."} for part in posix.parts)
                or not posix.parts
                or posix.parts[0] != "files"
                or src_posix.is_absolute()
                or src_windows.is_absolute()
                or src_windows.drive
                or any(part in {"", ".", ".."} for part in src_posix.parts)
                or not src_posix.parts
                or relative in seen_paths
                or source in seen_sources
            ):
                return False
            seen_paths.add(relative)
            seen_sources.add(source)
            candidate = root.joinpath(*posix.parts)
            cursor = root
            for part in posix.parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    return False
            if not candidate.is_file() or not candidate.resolve(
                strict=True
            ).is_relative_to(root_resolved):
                return False
            raw = candidate.read_bytes()
            if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
                return False
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _cleanup_backups(
    backups: Path, current_name: str, retention_days: float, keep_count: int
) -> int:
    """Remove only verified, old backup directories beyond the safety keep count."""
    try:
        entries = [
            path
            for path in backups.iterdir()
            if path.is_dir() and not path.is_symlink() and path.name != current_name
        ]
    except OSError:
        return 0
    entries = sorted(
        ((path.stat().st_mtime, path) for path in entries),
        key=lambda item: item[0],
        reverse=True,
    )
    cutoff = time.time() - float(retention_days) * 86400
    protected = {path for _, path in entries[:keep_count]}
    removed = 0
    for stamp, path in entries:
        if path in protected or stamp >= cutoff or not _verify_backup(path):
            continue
        try:
            shutil.rmtree(path)
        except OSError:
            continue
        removed += 1
    return removed


def _atomic_create_file(path: Path, raw: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        raise StorageError("备份文件写入失败") from error


def _backup_players(settings: Any, data_path: Path) -> dict:
    paths = WowsPaths(data_path)
    backups = paths.root / "backups"
    if paths.root.exists() and paths.root.is_symlink():
        raise StorageError("数据目录不能是符号链接")
    if backups.exists() and backups.is_symlink():
        raise StorageError("备份目录不能是符号链接")
    try:
        backups.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise StorageError("备份目录无法创建") from error
    if not _is_real_directory(backups):
        raise StorageError("备份目录无效")

    backup_lock = InstanceLock(backups / "backup.lock")
    try:
        backup_lock.acquire()
    except BusyError as error:
        raise StorageError("另一个备份仍在运行") from error
    try:
        files = _read_sources(paths)
        fingerprint, entries = _source_fingerprint(files)
        state_path = backups / "state.json"
        state = _load_backup_state(state_path)
        interval_hours = getattr(settings, "backup_interval_hours", 24)
        min_free_mb = getattr(settings, "disk_min_free_mb", 512)
        if (
            isinstance(interval_hours, bool)
            or not isinstance(interval_hours, int | float)
            or not math.isfinite(interval_hours)
            or interval_hours < 0
            or isinstance(min_free_mb, bool)
            or not isinstance(min_free_mb, int | float)
            or not math.isfinite(min_free_mb)
            or min_free_mb < 0
        ):
            raise StorageError("备份设置无效")

        last_backup = backups / state["backup_name"] if state else None
        prior_backup_valid = last_backup is not None and _verify_backup(last_backup)
        if state and prior_backup_valid and state["fingerprint"] == fingerprint:
            return {
                "status": "skipped",
                "reason": "unchanged",
                "fingerprint": fingerprint,
                "backup_path": str(last_backup),
                "files": len(entries),
            }
        now = time.time()
        interval_seconds = float(interval_hours) * 3600
        if (
            state
            and prior_backup_valid
            and now - state["successful_at"] < interval_seconds
        ):
            return {
                "status": "skipped",
                "reason": "interval",
                "fingerprint": fingerprint,
                "backup_path": str(last_backup),
                "files": len(entries),
            }

        try:
            free = shutil.disk_usage(data_path).free
        except OSError as error:
            raise StorageError("无法读取磁盘剩余空间") from error
        required = sum(entry["bytes"] for entry in entries)
        minimum = int(float(min_free_mb) * 1024 * 1024)
        if free < required + minimum:
            raise StorageError("剩余空间不足，已跳过备份")

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_dir = None
        for _ in range(10):
            candidate = backups / f"{timestamp}-{uuid.uuid4().hex}"
            try:
                candidate.mkdir()
                backup_dir = candidate
                break
            except FileExistsError:
                continue
            except OSError as error:
                raise StorageError("无法创建独立备份目录") from error
        if backup_dir is None:
            raise StorageError("无法分配唯一备份目录")

        manifest_entries = []
        for entry, (source, raw) in zip(entries, files, strict=True):
            relative = PurePosixPath("files") / PurePosixPath(source)
            destination = backup_dir.joinpath(*relative.parts)
            _atomic_create_file(destination, raw)
            manifest_entries.append(
                {
                    "source": source,
                    "path": relative.as_posix(),
                    "bytes": entry["bytes"],
                    "sha256": entry["sha256"],
                }
            )
        manifest = {
            "schema_version": 1,
            "complete": True,
            "created_at": time.time(),
            "fingerprint": fingerprint,
            "files": manifest_entries,
        }
        _atomic_json(backup_dir / "manifest.json", manifest)
        if not _verify_backup(backup_dir):
            raise StorageError("新建备份校验失败")
        _atomic_json(
            state_path,
            {
                "schema_version": 1,
                "successful_at": manifest["created_at"],
                "fingerprint": fingerprint,
                "backup_name": backup_dir.name,
            },
        )
        removed = _cleanup_backups(
            backups,
            backup_dir.name,
            getattr(settings, "backup_retention_days", 30),
            getattr(settings, "backup_keep_count", 3),
        )
        return {
            "status": "created",
            "reason": None,
            "fingerprint": fingerprint,
            "backup_path": str(backup_dir),
            "files": len(manifest_entries),
            "created_at": manifest["created_at"],
            "removed_backups": removed,
        }
    finally:
        backup_lock.release()


async def backup_players(settings: Any, data_path: Path) -> dict:
    """Create an immutable byte-level backup without contacting the game API."""
    return await _run_in_thread(_backup_players, settings, Path(data_path))


def verify_backup(backup_path: Path) -> bool:
    """Verify a completed backup manifest, safe relative paths, sizes, and hashes."""
    return _verify_backup(Path(backup_path))
