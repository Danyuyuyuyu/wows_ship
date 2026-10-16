"""Async wrapper for the plugin's cross-process file lease."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from .instance import InstanceLock


@asynccontextmanager
async def async_file_lock(path: Path):
    lock = InstanceLock(path)
    task = asyncio.create_task(asyncio.to_thread(lock.acquire))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise
    try:
        yield
    finally:
        await asyncio.to_thread(lock.release)
