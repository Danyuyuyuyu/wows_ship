"""客户端名称覆盖层；只按 ship_id/ship_id_str 对应，不替换 API 原始名。"""

from dataclasses import replace
import hashlib
import json
import logging
from pathlib import Path
import re
import struct

from .models import Selection, Ship
from .paths import WowsPaths
from .storage import StorageError

logger = logging.getLogger(__name__)
CLIENT_LANGUAGES = frozenset(
    {
        "cs",
        "de",
        "en",
        "es",
        "es_mx",
        "fr",
        "it",
        "ja",
        "ko",
        "nl",
        "pl",
        "pt",
        "pt_br",
        "ru",
        "th",
        "tr",
        "uk",
        "zh",
        "zh_sg",
        "zh_tw",
    }
)
SHIP_KEY = re.compile(r"IDS_(P[A-Z]{3}[0-9]{3})$")


def read_ship_messages(path: Path) -> dict[str, str]:
    """游戏 MO 无 charset 头；验证标准偏移表并显式按 UTF-8 解码。"""
    raw = path.read_bytes()
    if len(raw) < 28 or len(raw) > 20 * 1024 * 1024:
        raise ValueError("客户端语言文件大小无效")
    if raw[:4] == b"\xde\x12\x04\x95":
        order = "<"
    elif raw[:4] == b"\x95\x04\x12\xde":
        order = ">"
    else:
        raise ValueError("客户端语言文件不是 MO 格式")
    revision, count, originals, translations = struct.unpack_from(
        order + "IIII", raw, 4
    )
    if revision >> 16 > 1 or count > 200000:
        raise ValueError("客户端语言文件版本或条数无效")
    for table in (originals, translations):
        if table < 28 or table + count * 8 > len(raw):
            raise ValueError("客户端语言偏移表无效")
    result = {}
    for index in range(count):
        length, offset = struct.unpack_from(order + "II", raw, originals + index * 8)
        size, position = struct.unpack_from(order + "II", raw, translations + index * 8)
        if offset + length > len(raw) or position + size > len(raw):
            raise ValueError("客户端语言条目越界")
        key = raw[offset : offset + length].decode("utf-8").split("\0")[0]
        match = SHIP_KEY.fullmatch(key)
        if match is not None:
            name = (
                raw[position : position + size].decode("utf-8").split("\0")[0].strip()
            )
            if name and name != key:
                result[match[1]] = name
    if not result:
        raise ValueError("客户端语言文件没有舰船名称")
    return result


def build_name_map(
    catalog: dict, translation: Path, english: Path, language: str, game_build: str
) -> dict:
    if language not in CLIENT_LANGUAGES:
        raise ValueError("客户端语言不受支持")
    names = read_ship_messages(translation)
    english_names = read_ship_messages(english)
    identities = {}
    missing = []
    for key, row in catalog["ships"].items():
        if not key.isascii() or not key.isdecimal() or int(key) != row["ship_id"]:
            raise ValueError("目录舰船 ID 不一致")
        code = row.get("ship_id_str")
        if not isinstance(code, str) or not SHIP_KEY.fullmatch("IDS_" + code):
            raise ValueError("目录缺少有效 ship_id_str；请先刷新目录")
        identities[key] = code
        if code not in names:
            missing.append(int(key))
    return {
        "schema_version": 1,
        "language": language,
        "source": {
            "kind": "client_global.mo",
            "game_build": game_build,
            "resource": f"{language}/LC_MESSAGES/global.mo",
            "sha256": hashlib.sha256(translation.read_bytes()).hexdigest(),
        },
        "names": names,
        "english_names": english_names,
        "ship_ids": identities,
        "unmapped_ids": missing,
    }


class ClientNames:
    def __init__(
        self,
        data_path: Path,
        language: str,
        api_language: str | None = None,
        *,
        staging: bool = False,
        version: str | None = None,
    ):
        self.paths = WowsPaths(data_path)
        self.api_language = api_language or {"zh_sg": "zh-cn", "zh_tw": "zh-tw"}.get(
            language, language.replace("_", "-")
        )
        self.staging = staging
        self.path = self.paths.names(language)
        self.bundled = (
            Path(__file__).parent / "resources" / "names" / f"{language}.json"
        )
        self.language = language
        self.signature = None
        self.names: dict[str, str] = {}
        self.english: dict[str, str] = {}
        self.identities: dict[str, str] = {}
        self.game_build = ""
        self.payload = {}
        self._reload(version)

    @classmethod
    def load_bundle_payload(
        cls, paths: WowsPaths, api_language: str, language: str, version: str
    ) -> dict:
        """Load and validate one immutable bundle through the normal name loader."""
        instance = cls(
            paths.data_path,
            language,
            api_language,
            version=version,
        )
        return instance.payload

    def reload(self, version: str | None = None) -> None:
        """Reload names for an optional immutable bundle version."""
        self._reload(version)

    def _reload(self, version: str | None = None) -> None:
        paths = (self.path, self.bundled)
        if not self.staging:
            if version is None:
                active = self.paths.active(self.api_language)
                if active and active["client_language"] == self.language:
                    version = active["data_version"]
            if version is not None:
                paths = (
                    self.paths.bundle(self.api_language, version)
                    / "names"
                    / f"{self.language}.json",
                )
        for path in paths:
            try:
                info = path.stat()
                signature = (str(path), info.st_mtime_ns, info.st_size)
                if signature == self.signature:
                    return
                payload = json.loads(path.read_text(encoding="utf-8"))
                if (
                    not isinstance(payload, dict)
                    or (version is not None and payload.get("data_version") != version)
                    or payload["schema_version"] != 1
                    or payload["language"] != self.language
                ):
                    raise ValueError
                names, english, identities = (
                    payload["names"],
                    payload["english_names"],
                    payload["ship_ids"],
                )
                for dictionary in (names, english):
                    if not isinstance(dictionary, dict) or not dictionary:
                        raise ValueError
                    if any(
                        not SHIP_KEY.fullmatch("IDS_" + key)
                        or not isinstance(value, str)
                        or not value.strip()
                        for key, value in dictionary.items()
                    ):
                        raise ValueError
                if not isinstance(identities, dict) or any(
                    not key.isascii()
                    or not key.isdecimal()
                    or not isinstance(code, str)
                    or not SHIP_KEY.fullmatch("IDS_" + code)
                    for key, code in identities.items()
                ):
                    raise ValueError
                game_build = payload["source"]["game_build"]
                if not isinstance(game_build, str) or not game_build.strip():
                    raise ValueError
                self.names, self.english, self.identities = names, english, identities
                self.game_build = game_build
                self.payload = payload
                self.signature = signature
                return
            except FileNotFoundError as error:
                if version is not None:
                    raise StorageError("已提交版本的客户端名称表缺失") from error
                continue
            except (OSError, ValueError, KeyError, TypeError) as error:
                if version is not None:
                    raise StorageError("已提交版本的客户端名称表损坏") from error
                logger.warning("客户端名称表无效，尝试已有或随插件名称表")
                if self.signature is not None:
                    return
        if not self.names:
            logger.warning("缺少客户端名称表，尚无法覆盖 API 名称")

    def name_for(self, ship: Ship, data_version: str | None = None) -> str:
        self._reload(data_version)
        code = ship.ship_code or self.identities.get(str(ship.ship_id))
        if code is None:
            return ship.name
        return self.names.get(code) or self.english.get(code) or ship.name

    def english_for(self, ship: Ship, data_version: str | None = None) -> str | None:
        """卡片副标题用的客户端英文名；缺失时返回 None，由展示层省略这一行。"""
        try:
            self._reload(data_version)
        except StorageError:
            logger.warning("客户端英文名表暂不可用，卡片省略英文副标题")
            return None
        code = ship.ship_code or self.identities.get(str(ship.ship_id))
        if code is None:
            return None
        name = self.english.get(code)
        return name.strip() if isinstance(name, str) and name.strip() else None

    def apply(self, selection: Selection) -> Selection:
        name = self.name_for(selection.ship, selection.data_version)
        return replace(selection, ship=replace(selection.ship, display_name=name))
