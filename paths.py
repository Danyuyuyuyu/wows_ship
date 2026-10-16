"""统一公共/玩家数据路径及已提交版本的安全定位。"""

from dataclasses import dataclass
from pathlib import Path
import re

from .storage import StorageError, read_json


@dataclass(frozen=True)
class WowsPaths:
    data_path: Path

    @property
    def root(self):
        return self.data_path / "wows_ship"

    def catalog(self, language):
        return self.root / "catalog" / "asia" / f"{language}.json"

    def names(self, language):
        return self.root / "names" / f"{language}.json"

    @property
    def images(self):
        return self.root / "images"

    @property
    def players(self):
        return self.root / "players"

    @property
    def bindings(self):
        return self.root / "bindings.json"

    @property
    def export(self):
        return self.root / "exports" / "ships.json"

    @property
    def manifest(self):
        return self.root / "manifests" / "download.json"

    def checks(self, language):
        self.bundle(language, "0" * 32)
        return self.root / "checks" / "asia" / f"{language}.json"

    def unknown_sync(self, language):
        self.bundle(language, "0" * 32)
        return self.root / "checks" / "asia" / f"{language}.unknown.json"

    @property
    def metadata(self):
        return self.root / "metadata.json"

    def bundle(self, language, version):
        if not re.fullmatch(r"[a-z]{2}(?:-[a-z]{2})?", language) or not re.fullmatch(
            r"[0-9a-f]{32}", version
        ):
            raise StorageError("公共数据版本路径无效")
        return self.root / "snapshots" / language / version

    def active(self, language):
        payload = read_json(self.metadata)
        if payload is None:
            return None
        try:
            if payload["schema_version"] != 1 or not isinstance(
                payload["active"], dict
            ):
                raise ValueError
            row = payload["active"].get(language)
            if row is None:
                return None
            self.bundle(language, row["data_version"])
            return row
        except (KeyError, ValueError, TypeError) as error:
            raise StorageError("公共数据版本记录无效，原文件已保留") from error
