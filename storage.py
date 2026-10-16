"""同目录临时文件原子提交；取消时等待正在写盘的线程退出。"""

import asyncio
import json
import os
from pathlib import Path
import tempfile
from typing import Any


class StorageError(RuntimeError):
    pass


def _fsync_parent(path: Path) -> None:
    """Best-effort directory sync so rename survives a sudden restart."""
    try:
        descriptor = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def read_json(path: Path) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if value is None:
            raise ValueError("null is not a storage document")
        return value
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise StorageError("数据文件无法读取或格式损坏，原文件已保留") from error


def _write(path: Path, data: Any, backup: bool = False) -> None:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if backup and path.is_file():
            _write_bytes(path.with_name(path.name + ".bak"), path.read_bytes())
        os.replace(temporary, path)
        _fsync_parent(path)
    except (OSError, ValueError, TypeError) as error:
        raise StorageError("数据写入失败，原文件已保留") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def atomic_write(path: Path, data: Any, *, backup: bool = False) -> None:
    task = asyncio.create_task(asyncio.to_thread(_write, path, data, backup))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def _write_bytes(path: Path, data: bytes) -> None:
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
        _fsync_parent(path)
    except OSError as error:
        raise StorageError("二进制数据写入失败，原文件已保留") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def atomic_write_bytes(path: Path, data: bytes) -> None:
    task = asyncio.create_task(asyncio.to_thread(_write_bytes, path, data))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise
