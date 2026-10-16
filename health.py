"""持久化健康状态、连续失败计数和严重故障/恢复通知。"""

import asyncio
from copy import deepcopy
import logging
import math
import os
from pathlib import Path
import socket
import time

from .storage import StorageError, atomic_write, read_json

logger = logging.getLogger(__name__)


class HealthMonitor:
    def __init__(self, settings, data_path: Path, notifier=None):
        self.settings, self.notifier = settings, notifier
        self.path = data_path / "wows_ship" / "health.json"
        self.lock = asyncio.Lock()
        self.state = {
            "schema_version": 1,
            "issues": {},
            "tasks": {},
            "last_success": {},
            "pid": os.getpid(),
            "host": socket.gethostname(),
        }
        try:
            saved = read_json(self.path)
            if (
                isinstance(saved, dict)
                and saved.get("schema_version") == 1
                and all(
                    isinstance(saved.get(key), dict)
                    for key in ("issues", "tasks", "last_success")
                )
            ):
                self.state.update(saved, pid=os.getpid(), host=socket.gethostname())
                for code, issue in list(self.state["issues"].items()):
                    if not isinstance(issue, dict):
                        self.state["issues"].pop(code)
                        continue
                    for key in (
                        "failures",
                        "first_seen",
                        "last_seen",
                        "last_notified_at",
                    ):
                        value = issue.get(key, 0)
                        if (
                            type(value) not in {int, float}
                            or not math.isfinite(value)
                            or value < 0
                        ):
                            issue[key] = 0
                    issue["failures"] = int(issue.get("failures", 0))
        except StorageError:
            logger.warning("旧健康状态不可读取，本轮从新状态开始")

    async def _save(self):
        self.state["updated_at"] = time.time()
        try:
            await atomic_write(self.path, self.state)
        except StorageError:
            logger.warning("健康状态无法写盘，继续提供服务")

    async def _notify(self, text):
        if not self.settings.notify_superusers or self.notifier is None:
            return False
        try:
            return bool(await asyncio.wait_for(self.notifier(text), 10))
        except Exception:
            logger.warning("故障通知暂未送达，下次巡检重试")
            return False

    async def issue(self, code, message, severity="error"):
        async with self.lock:
            now = time.time()
            old = self.state["issues"].get(code, {})
            if not isinstance(old, dict) or not old.get("active"):
                old = {}
            current = dict(
                old,
                message=message,
                severity=severity,
                failures=old.get("failures", 0) + 1,
                last_seen=now,
                first_seen=old.get("first_seen", now),
                active=True,
            )
            self.state["issues"][code] = current
            urgent = severity == "critical" or (
                severity == "error"
                and current["failures"] >= self.settings.health_failure_threshold
            )
            if (
                urgent
                and now - current.get("last_notified_at", 0)
                >= self.settings.alert_repeat_hours * 3600
            ):
                if await self._notify(f"战舰选船插件故障：{message}"):
                    current["last_notified_at"] = now
                    current["notified"] = True
            await self._save()

    async def recover(self, code):
        async with self.lock:
            current = self.state["issues"].get(code)
            if not isinstance(current, dict) or not current.get("active"):
                return
            if current.get("notified") and not await self._notify(
                f"战舰选船插件已恢复：{current['message']}"
            ):
                current["recovery_pending"] = True
                await self._save()
                return
            current.update(
                active=False,
                failures=0,
                recovered_at=time.time(),
                notified=False,
                recovery_pending=False,
                last_notified_at=0,
            )
            await self._save()

    async def heartbeat(self, name, status="running"):
        async with self.lock:
            self.state["tasks"][name] = {"heartbeat_at": time.time(), "status": status}
            await self._save()

    async def success(self, name):
        async with self.lock:
            self.state["last_success"][name] = time.time()
            await self._save()
        await self.recover(name)

    def to_dict(self):
        return deepcopy(self.state)
