"""只负责池选择、交集、过滤及等概率随机；不做网络和缓存写入。"""

import random

from .models import (
    Account,
    CatalogSnapshot,
    ErrorCode,
    InventoryResult,
    Pool,
    Selection,
    SelectionError,
    SelectionRequest,
    Ship,
    ShipCategory,
    ShipFilter,
    StatsResult,
    StatsStatus,
)


def resolve_pool(
    requested: Pool, account: Account | None, inventory: InventoryResult | None = None
) -> Pool:
    if requested == Pool.CATALOG:
        return Pool.CATALOG
    if requested in {Pool.PERSONAL, Pool.OWNED}:
        if account is None:
            raise SelectionError(ErrorCode.BINDING_REQUIRED)
        if account.server != "asia":
            raise SelectionError(ErrorCode.UNSUPPORTED_ACCOUNT)
    elif account is None or account.server != "asia":
        return Pool.CATALOG
    if inventory is not None:
        return Pool.OWNED
    if requested == Pool.OWNED:
        raise SelectionError(ErrorCode.INVENTORY_UNAVAILABLE)
    return Pool.PERSONAL


# 官方数据里从 10 级起没有 is_premium 的船（这类船统一标 is_special），
# 所以「金币」在这个等级起把特种一并算进来，见 _matches_category
SPECIAL_AS_PREMIUM_FROM_TIER = 10


def _matches_category(ship: Ship, category: ShipCategory | None) -> bool:
    if category is None:
        return True
    if category == ShipCategory.SUPER:
        return ship.tier >= 11
    if category == ShipCategory.SPECIAL:
        return ship.tier < 11 and ship.is_special
    if category == ShipCategory.PREMIUM:
        if ship.tier >= 11:
            return False
        if ship.tier >= SPECIAL_AS_PREMIUM_FROM_TIER:
            # 这个等级起没有 is_premium 的船，把特种一并算进来，避免「10 级金币船」必然空池
            return ship.is_premium or ship.is_special
        return ship.is_premium and not ship.is_special
    return ship.tier < 11 and not ship.is_premium and not ship.is_special


def matches(ship: Ship, filters: ShipFilter) -> bool:
    tier_matches = (
        filters.tier is None or ship.tier == filters.tier
    ) and (
        filters.tier_min is None or ship.tier >= filters.tier_min
    ) and (
        filters.tier_max is None or ship.tier <= filters.tier_max
    )
    return (
        (filters.nation is None or ship.nation == filters.nation)
        and tier_matches
        and (filters.ship_type is None or ship.ship_type == filters.ship_type)
        and _matches_category(ship, filters.category)
    )


def select_ship(
    request: SelectionRequest,
    snapshot: CatalogSnapshot,
    account: Account | None = None,
    stats: StatsResult | None = None,
    *,
    inventory: InventoryResult | None = None,
    rng: random.Random | None = None,
) -> Selection:
    if snapshot.server != "asia":
        raise SelectionError(ErrorCode.UNSUPPORTED_ACCOUNT)
    pool = resolve_pool(request.pool, account, inventory)
    personal_ids = frozenset()
    if pool == Pool.OWNED:
        if (
            inventory is None
            or account is None
            or (inventory.server, inventory.account_id)
            != (account.server, account.account_id)
        ):
            raise SelectionError(ErrorCode.INVENTORY_UNAVAILABLE)
        if not inventory.ship_ids:
            raise SelectionError(ErrorCode.NO_OWNED_SHIPS)
        personal_ids = inventory.ship_ids
    elif pool == Pool.PERSONAL:
        if (
            stats is None
            or account is None
            or (stats.server, stats.account_id) != (account.server, account.account_id)
        ):
            raise SelectionError(ErrorCode.STATS_UNAVAILABLE)
        if stats.status == StatsStatus.HIDDEN:
            raise SelectionError(ErrorCode.HIDDEN_PROFILE)
        if stats.status == StatsStatus.EMPTY or not stats.ship_ids:
            raise SelectionError(ErrorCode.NO_HISTORY)
        personal_ids = stats.ship_ids
    candidates = {
        ship.ship_id: ship
        for ship in snapshot.ships
        if ship.ship_id not in snapshot.pending_ship_ids
        and matches(ship, request.filters)
        and (pool == Pool.CATALOG or ship.ship_id in personal_ids)
    }
    pending = (
        snapshot.pending_ship_ids
        if pool == Pool.CATALOG
        else personal_ids
        - {
            ship.ship_id
            for ship in snapshot.ships
            if ship.ship_id not in snapshot.pending_ship_ids
        }
    )
    if not candidates:
        pending_matches = any(
            ship.ship_id in snapshot.pending_ship_ids
            and (pool == Pool.CATALOG or ship.ship_id in personal_ids)
            and matches(ship, request.filters)
            for ship in snapshot.ships
        )
        any_ready_personal = any(
            ship.ship_id in personal_ids
            and ship.ship_id not in snapshot.pending_ship_ids
            for ship in snapshot.ships
        )
        if pending_matches or (
            pool != Pool.CATALOG and pending and not any_ready_personal
        ):
            raise SelectionError(ErrorCode.NEW_SHIP_PENDING)
        raise SelectionError(ErrorCode.EMPTY_POOL)
    chooser = rng if rng is not None else random
    ship = chooser.choice(tuple(candidates.values()))
    return Selection(
        ship,
        pool,
        snapshot.fetched_at,
        snapshot.stale,
        snapshot.data_version,
        pending_ship_count=len(pending),
    )
