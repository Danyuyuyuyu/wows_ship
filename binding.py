"""单进程 QQ 绑定原子存储和隔离候选会话。"""

import asyncio
from dataclasses import asdict, dataclass
from pathlib import Path
import time

from .api import integer
from .file_lock import async_file_lock
from .models import Account
from .storage import StorageError, atomic_write, read_json


@dataclass(frozen=True)
class BindingSession:
    bot_id: str
    user_id: str
    group_id: str | None
    session_id: str
    candidates: tuple[Account, ...]
    expires_at_monotonic: float


class BindingStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()
        self.file_lock_path = path.with_name(path.name + ".lock")

    def _read(self) -> dict[str, Account]:
        data = read_json(self.path)
        if data is None:
            return {}
        try:
            if not isinstance(data, dict):
                raise ValueError
            result = {}
            for user, row in data.items():
                if (
                    not isinstance(user, str)
                    or not user.isdecimal()
                    or not isinstance(row["server"], str)
                    or not isinstance(row["nickname"], str)
                    or not row["nickname"]
                ):
                    raise ValueError
                result[user] = Account(
                    row["server"],
                    integer(row["account_id"], minimum=1),
                    row["nickname"],
                )
            return result
        except (ValueError, KeyError, TypeError, RuntimeError) as error:
            raise StorageError(
                "绑定文件格式损坏，原文件已保留；请管理员检查"
            ) from error

    async def get(self, user_id: str) -> Account | None:
        async with self.lock:
            async with async_file_lock(self.file_lock_path):
                return (await asyncio.to_thread(self._read)).get(user_id)

    async def all(self) -> dict[str, Account]:
        async with self.lock:
            async with async_file_lock(self.file_lock_path):
                return await asyncio.to_thread(self._read)

    async def bind(self, user_id: str, account: Account) -> None:
        if (
            account.server != "asia"
            or not user_id.isdecimal()
            or type(account.account_id) is not int
            or account.account_id <= 0
        ):
            raise ValueError("仅允许 QQ 绑定亚服账号")
        async with self.lock:
            async with async_file_lock(self.file_lock_path):
                data = await asyncio.to_thread(self._read)
                data[user_id] = account
                await atomic_write(
                    self.path,
                    {user: asdict(value) for user, value in data.items()},
                    backup=True,
                )

    async def unbind(self, user_id: str) -> bool:
        async with self.lock:
            async with async_file_lock(self.file_lock_path):
                data = await asyncio.to_thread(self._read)
                if user_id not in data:
                    return False
                del data[user_id]
                await atomic_write(
                    self.path,
                    {user: asdict(value) for user, value in data.items()},
                    backup=True,
                )
                return True


class SessionError(ValueError):
    pass


class BindingSessions:
    def __init__(self):
        self.sessions: dict[tuple[str, str, str | None], BindingSession] = {}
        self.lock = asyncio.Lock()

    def _prune(self) -> None:
        now = time.monotonic()
        for key, session in tuple(self.sessions.items()):
            if session.expires_at_monotonic <= now:
                del self.sessions[key]

    async def begin(self, session: BindingSession) -> None:
        async with self.lock:
            self._prune()
            key = session.bot_id, session.user_id, session.group_id
            if key not in self.sessions and len(self.sessions) >= 1000:
                raise SessionError("绑定会话过多，请稍后再试")
            self.sessions[key] = session

    async def choose(
        self,
        bot_id: str,
        user_id: str,
        group_id: str | None,
        session_id: str,
        number: int,
    ) -> Account:
        self._prune()
        session = self.sessions.get((bot_id, user_id, group_id))
        if session is None or session.session_id != session_id:
            raise SessionError("绑定会话已过期或失效，请重新 wows绑定")
        if not 1 <= number <= len(session.candidates):
            raise SessionError("序号无效，请回复候选中的序号，或回复“取消”")
        return session.candidates[number - 1]

    def _cancel(self, bot_id, user_id, group_id, session_id) -> bool:
        key = bot_id, user_id, group_id
        session = self.sessions.get(key)
        if session is not None and session.session_id == session_id:
            del self.sessions[key]
            return True
        return False

    async def cancel(
        self, bot_id: str, user_id: str, group_id: str | None, session_id: str
    ) -> bool:
        async with self.lock:
            return self._cancel(bot_id, user_id, group_id, session_id)

    async def commit(
        self, store: BindingStore, session: BindingSession, number: int
    ) -> Account:
        async with self.lock:
            account = await self.choose(
                session.bot_id,
                session.user_id,
                session.group_id,
                session.session_id,
                number,
            )
            await store.bind(session.user_id, account)
            self._cancel(
                session.bot_id, session.user_id, session.group_id, session.session_id
            )
            return account

    async def unbind(self, store: BindingStore, user_id: str) -> bool:
        async with self.lock:
            removed = await store.unbind(user_id)
            for key in tuple(self.sessions):
                if key[1] == user_id:
                    del self.sessions[key]
            return removed

    def clear(self) -> None:
        self.sessions.clear()
