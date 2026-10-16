"""有界 TTL 缓存；相同键共享任务，调用者取消不取消其他调用者。"""

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
import time
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class BusyError(RuntimeError):
    pass


class AsyncCache(Generic[K, V]):
    def __init__(self, ttl: float, capacity: int, max_pending: int = 64):
        self.ttl = ttl
        self.capacity = capacity
        self.max_pending = max_pending
        self.values: OrderedDict[K, tuple[float, V]] = OrderedDict()
        self.pending: dict[K, asyncio.Task[V]] = {}

    def invalidate(self, key: K) -> None:
        self.values.pop(key, None)

    async def get(self, key: K, factory: Callable[[], Awaitable[V]]) -> V:
        now = time.monotonic()
        for old in tuple(self.values):
            if self.values[old][0] <= now:
                del self.values[old]
        if key in self.values:
            self.values.move_to_end(key)
            return self.values[key][1]
        task = self.pending.get(key)
        if task is None:
            if len(self.pending) >= self.max_pending:
                raise BusyError("请求过多，请稍后再试")
            task = asyncio.create_task(self._fill(key, factory))
            self.pending[key] = task
            task.add_done_callback(lambda done: self._done(key, done))
        return await asyncio.shield(task)

    async def _fill(self, key: K, factory: Callable[[], Awaitable[V]]) -> V:
        value = await factory()
        self.values[key] = (time.monotonic() + self.ttl, value)
        self.values.move_to_end(key)
        while len(self.values) > self.capacity:
            self.values.popitem(last=False)
        return value

    def _done(self, key: K, task: asyncio.Task[V]) -> None:
        self.pending.pop(key, None)
        if not task.cancelled():
            task.exception()

    async def close(self) -> None:
        tasks = tuple(self.pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.pending.clear()
        self.values.clear()
