"""Application-facing maintenance seams for public data and player backups."""

from collections.abc import Callable
from pathlib import Path

from .maintenance_storage import backup_players, disk_status, recover_public


class PublicDataRecovery:
    """Recover and inspect the immutable public ship-data set."""

    def __init__(
        self,
        settings,
        data_path: Path,
        *,
        recover_fn: Callable | None = None,
        disk_status_fn: Callable | None = None,
    ):
        self.settings = settings
        self.data_path = Path(data_path)
        self._recover_fn = recover_fn
        self._disk_status_fn = disk_status_fn

    async def recover(self):
        if self._recover_fn is not None:
            return await self._recover_fn()
        return await recover_public(self.settings, self.data_path)

    def disk_status(self):
        if self._disk_status_fn is not None:
            return self._disk_status_fn()
        return disk_status(self.data_path)


class PlayerDataBackup:
    """Create verified player-data backups without exposing storage helpers."""

    def __init__(self, settings, data_path: Path, *, create_fn: Callable | None = None):
        self.settings = settings
        self.data_path = Path(data_path)
        self._create_fn = create_fn

    async def create(self):
        if self._create_fn is not None:
            return await self._create_fn()
        return await backup_players(self.settings, self.data_path)
