"""配置定义和快照；由后续命令接入层显式注册、读取。"""

from copy import deepcopy
from dataclasses import dataclass, field
import math
from pathlib import Path
import re

MODULE = "wows_ship"
API_BASE_URL = "https://api.worldofwarships.asia/wows/"
LANGUAGES = frozenset(
    {
        "zh-cn",
        "zh-tw",
        "en",
        "ja",
        "de",
        "fr",
        "it",
        "es",
        "pl",
        "ru",
        "th",
        "tr",
        "cs",
        "pt-br",
        "es-mx",
    }
)
CONFIG_DEFAULTS = {
    "ENABLE": True,
    "WOWS_APP_ID": "",
    "SERVER": "asia",
    "LANGUAGE": "zh-cn",
    "CLIENT_LANGUAGE": "auto",
    "CATALOG_TTL_HOURS": 24,
    "UPDATE_TIMEOUT_SECONDS": 1800,
    "IMAGE_VERIFY_DAYS": 7,
    "SNAPSHOT_RETENTION_DAYS": 7,
    "SNAPSHOT_KEEP_COUNT": 3,
    "UNKNOWN_SHIP_REFRESH_MINUTES": 60,
    "UNKNOWN_SHIP_WAIT_SECONDS": 5,
    "CLIENT_TRANSLATIONS_PATH": "",
    "STATS_TTL_MINUTES": 10,
    "HISTORY_REFRESH_DAYS": 3,
    "HISTORY_CHECK_INTERVAL_MINUTES": 60,
    "HISTORY_RETRY_MINUTES": 60,
    "MIN_REQUEST_INTERVAL_MS": 200,
    "ALLOWED_GROUPS": [],
    "ALLOW_PRIVATE": True,
    "REQUEST_TIMEOUT_SECONDS": 10,
    "OPERATION_TIMEOUT_SECONDS": 60,
    "MAX_RETRIES": 2,
    "USER_COOLDOWN_SECONDS": 5,
    "MAX_RENDER_CONCURRENCY": 2,
    "MAX_RENDER_QUEUE": 5,
    "RENDER_TIMEOUT_SECONDS": 15,
    "BINDING_TIMEOUT_SECONDS": 60,
    "MAINTENANCE_INTERVAL_MINUTES": 60,
    "WATCHDOG_INTERVAL_SECONDS": 60,
    "HEALTH_FAILURE_THRESHOLD": 3,
    "ALERT_REPEAT_HOURS": 24,
    "PENDING_NAMES_ALERT_HOURS": 24,
    "BACKUP_INTERVAL_HOURS": 24,
    "BACKUP_RETENTION_DAYS": 30,
    "BACKUP_KEEP_COUNT": 3,
    "DISK_MIN_FREE_MB": 512,
    "IMAGE_RETRY_MINUTES": 10,
    "API_CIRCUIT_BASE_SECONDS": 60,
    "API_CIRCUIT_MAX_HOURS": 6,
    "API_CONFIG_RETRY_MINUTES": 60,
    "NOTIFY_SUPERUSERS": True,
}


def register_config() -> None:
    """接入阶段显式调用，沿用仓库 Config 管理器。"""
    from zhenxun.configs.config import Config

    for key, default in CONFIG_DEFAULTS.items():
        Config.add_plugin_config(
            MODULE, key, deepcopy(default), default_value=deepcopy(default)
        )


@dataclass(frozen=True)
class Settings:
    app_id: str = field(default="", repr=False)
    enabled: bool = True
    server: str = "asia"
    language: str = "zh-cn"
    client_language: str = "auto"
    catalog_ttl_hours: float = 24
    update_timeout_seconds: float = 1800
    image_verify_days: float = 7
    snapshot_retention_days: float = 7
    snapshot_keep_count: int = 3
    unknown_ship_refresh_minutes: float = 60
    unknown_ship_wait_seconds: float = 5
    client_translations_path: str = ""
    stats_ttl_minutes: float = 10
    history_refresh_days: float = 3
    history_check_interval_minutes: float = 60
    history_retry_minutes: float = 60
    min_request_interval_ms: float = 200
    allowed_groups: frozenset[str] = field(default_factory=frozenset)
    allow_private: bool = True
    request_timeout_seconds: float = 10
    operation_timeout_seconds: float = 60
    max_retries: int = 2
    user_cooldown_seconds: float = 5
    max_render_concurrency: int = 2
    max_render_queue: int = 5
    render_timeout_seconds: float = 15
    binding_timeout_seconds: float = 60
    maintenance_interval_minutes: float = 60
    watchdog_interval_seconds: float = 60
    health_failure_threshold: int = 3
    alert_repeat_hours: float = 24
    pending_names_alert_hours: float = 24
    backup_interval_hours: float = 24
    backup_retention_days: float = 30
    backup_keep_count: int = 3
    disk_min_free_mb: float = 512
    image_retry_minutes: float = 10
    api_circuit_base_seconds: float = 60
    api_circuit_max_hours: float = 6
    api_config_retry_minutes: float = 60
    notify_superusers: bool = True

    def __post_init__(self) -> None:
        if self.server != "asia":
            raise ValueError("第一版 SERVER 仅支持 asia")
        if self.language not in LANGUAGES:
            raise ValueError("LANGUAGE 不受支持")
        from .names import CLIENT_LANGUAGES

        if (
            self.client_language != "auto"
            and self.client_language not in CLIENT_LANGUAGES
        ):
            raise ValueError("CLIENT_LANGUAGE 不受支持")
        if self.app_id and not re.fullmatch(r"[0-9a-fA-F]{32}", self.app_id):
            raise ValueError("WOWS_APP_ID 必须为 32 位十六进制字符串")
        for name in (
            "catalog_ttl_hours",
            "update_timeout_seconds",
            "image_verify_days",
            "snapshot_retention_days",
            "unknown_ship_refresh_minutes",
            "unknown_ship_wait_seconds",
            "stats_ttl_minutes",
            "history_refresh_days",
            "history_check_interval_minutes",
            "history_retry_minutes",
            "min_request_interval_ms",
            "request_timeout_seconds",
            "operation_timeout_seconds",
            "render_timeout_seconds",
            "binding_timeout_seconds",
            "maintenance_interval_minutes",
            "watchdog_interval_seconds",
            "alert_repeat_hours",
            "pending_names_alert_hours",
            "backup_interval_hours",
            "backup_retention_days",
            "disk_min_free_mb",
            "image_retry_minutes",
            "api_circuit_base_seconds",
            "api_circuit_max_hours",
            "api_config_retry_minutes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} 必须为有限正数")
        cooldown = self.user_cooldown_seconds
        if isinstance(cooldown, bool) or not math.isfinite(cooldown) or cooldown < 0:
            raise ValueError("user_cooldown_seconds 必须为有限非负数")
        for name, minimum in (
            ("max_retries", 0),
            ("health_failure_threshold", 1),
            ("snapshot_keep_count", 3),
            ("backup_keep_count", 1),
            ("max_render_concurrency", 1),
            ("max_render_queue", 0),
        ):
            value = getattr(self, name)
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name} 必须为不小于 {minimum} 的整数")

    @property
    def name_language(self) -> str:
        if self.client_language != "auto":
            return self.client_language
        return {"zh-cn": "zh_sg", "zh-tw": "zh_tw"}.get(
            self.language, self.language.replace("-", "_")
        )

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.app_id)

    def catalog_path(self, data_path: Path) -> Path:
        from .paths import WowsPaths

        return WowsPaths(data_path).catalog(self.language)


def load_settings() -> Settings:
    """从真寻配置系统构建严格校验的运行快照。"""
    from zhenxun.configs.config import Config

    values = {
        key: Config.get_config(MODULE, key, deepcopy(default))
        for key, default in CONFIG_DEFAULTS.items()
    }

    def boolean(key: str) -> bool:
        value = values[key]
        if type(value) is bool:
            return value
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            return value.lower() == "true"
        raise ValueError(f"{key} 必须为布尔值")

    groups = values["ALLOWED_GROUPS"]
    if not isinstance(groups, list | tuple | set):
        raise ValueError("ALLOWED_GROUPS 必须为群号列表")
    if any(not str(group).isascii() or not str(group).isdecimal() for group in groups):
        raise ValueError("ALLOWED_GROUPS 必须仅包含群号")
    kwargs = {
        "app_id": str(values["WOWS_APP_ID"] or "").strip(),
        "enabled": boolean("ENABLE"),
        "server": str(values["SERVER"]).strip().lower(),
        "language": str(values["LANGUAGE"]).strip().lower(),
        "client_language": str(values["CLIENT_LANGUAGE"]).strip().lower(),
        "client_translations_path": str(
            values["CLIENT_TRANSLATIONS_PATH"] or ""
        ).strip(),
        "allowed_groups": frozenset(str(group) for group in groups),
        "allow_private": boolean("ALLOW_PRIVATE"),
        "notify_superusers": boolean("NOTIFY_SUPERUSERS"),
    }
    integers = {
        "MAX_RETRIES",
        "MAX_RENDER_CONCURRENCY",
        "MAX_RENDER_QUEUE",
        "SNAPSHOT_KEEP_COUNT",
        "BACKUP_KEEP_COUNT",
        "HEALTH_FAILURE_THRESHOLD",
    }
    for key in CONFIG_DEFAULTS:
        if key in {
            "ENABLE",
            "WOWS_APP_ID",
            "SERVER",
            "LANGUAGE",
            "CLIENT_LANGUAGE",
            "CLIENT_TRANSLATIONS_PATH",
            "ALLOWED_GROUPS",
            "ALLOW_PRIVATE",
            "NOTIFY_SUPERUSERS",
        }:
            continue
        value = values[key]
        if isinstance(value, bool):
            raise ValueError(f"{key} 必须为数值")
        try:
            number = float(value)
            if key in integers:
                if not number.is_integer():
                    raise ValueError
                number = int(number)
        except (ValueError, TypeError, OverflowError) as error:
            raise ValueError(f"{key} 数值无效") from error
        kwargs[key.lower()] = number
    return Settings(**kwargs)
