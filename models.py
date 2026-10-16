"""共享领域数据；不依赖 NoneBot、网络或持久化。"""

from dataclasses import dataclass, field
from enum import Enum


class Pool(str, Enum):
    AUTO = "auto"
    PERSONAL = "personal"
    OWNED = "owned"
    CATALOG = "catalog"


class ShipType(str, Enum):
    BATTLESHIP = "Battleship"
    CRUISER = "Cruiser"
    DESTROYER = "Destroyer"
    CARRIER = "AirCarrier"
    SUBMARINE = "Submarine"


class ShipCategory(str, Enum):
    """Mutually exclusive display/filter categories for a ship.

    唯一的例外：官方数据里 10 级起没有 is_premium 的船（这类船统一标 is_special），
    所以 10 级的「金币」筛选会把特种一并算进来，见 randomizer._matches_category。
    """

    TECH = "tech"          # 研发/银币船
    PREMIUM = "premium"    # 金币船
    SPECIAL = "special"    # 特种船
    SUPER = "super"        # 超级战舰（XI级）


class StatsStatus(str, Enum):
    AVAILABLE = "available"
    EMPTY = "empty"
    HIDDEN = "hidden"


class ErrorCode(str, Enum):
    INVALID_ARGUMENT = "invalid_argument"
    BINDING_REQUIRED = "binding_required"
    UNSUPPORTED_ACCOUNT = "unsupported_account"
    NO_HISTORY = "no_history"
    NO_OWNED_SHIPS = "no_owned_ships"
    INVENTORY_UNAVAILABLE = "inventory_unavailable"
    HIDDEN_PROFILE = "hidden_profile"
    STATS_UNAVAILABLE = "stats_unavailable"
    EMPTY_POOL = "empty_pool"
    NEW_SHIP_PENDING = "new_ship_pending"


class SelectionError(ValueError):
    def __init__(self, code: ErrorCode, detail: str = "") -> None:
        self.code = code
        super().__init__(detail or code.value)


@dataclass(frozen=True)
class Ship:
    ship_id: int
    name: str
    tier: int
    nation: str
    ship_type: ShipType
    is_premium: bool = False
    is_special: bool = False
    contour_url: str | None = None
    image_url: str | None = None
    ship_code: str | None = None
    display_name: str | None = None

    @property
    def label(self) -> str:
        return self.display_name or self.name


@dataclass(frozen=True)
class Account:
    server: str
    account_id: int
    nickname: str


@dataclass(frozen=True)
class ShipFilter:
    nation: str | None = None
    tier: int | None = None
    ship_type: ShipType | None = None
    category: ShipCategory | None = None
    tier_min: int | None = None
    tier_max: int | None = None


@dataclass(frozen=True)
class SelectionRequest:
    pool: Pool = Pool.AUTO
    filters: ShipFilter = field(default_factory=ShipFilter)


@dataclass(frozen=True)
class StatsResult:
    server: str
    account_id: int
    status: StatsStatus
    ship_ids: frozenset[int] = field(default_factory=frozenset)


@dataclass(frozen=True)
class InventoryResult:
    server: str
    account_id: int
    user_id: str
    ship_ids: frozenset[int] = field(default_factory=frozenset)
    source: str = "manual"
    updated_at: float = 0


@dataclass(frozen=True)
class CatalogSnapshot:
    server: str
    language: str
    fetched_at: float
    ships: tuple[Ship, ...]
    stale: bool = False
    schema_version: int = 1
    data_version: str | None = None
    pending_ship_ids: frozenset[int] = field(default_factory=frozenset)
    checked_at: float | None = None


@dataclass(frozen=True)
class Selection:
    ship: Ship
    pool: Pool
    catalog_fetched_at: float
    catalog_stale: bool
    data_version: str | None = None
    history_updated_at: float | None = None
    history_stale: bool = False
    pending_ship_count: int = 0
