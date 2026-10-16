"""空白分隔参数的纯解析：重复同义词合并，冲突和未知词报错。"""

import unicodedata

from .models import (
    ErrorCode,
    Pool,
    SelectionError,
    SelectionRequest,
    ShipCategory,
    ShipFilter,
    ShipType,
)

NATION_ALIASES = {
    alias: nation
    for nation, aliases in {
        "japan": ("日本", "日", "日系"),
        "usa": ("美国", "美", "美系"),
        "germany": ("德国", "德", "德系"),
        "ussr": ("苏联", "苏", "苏系"),
        "uk": ("英国", "英", "英系"),
        "france": ("法国", "法", "法系"),
        "italy": ("意大利", "意", "意系"),
        "pan_asia": ("泛亚", "泛亚系"),
        "europe": ("欧洲", "泛欧", "欧系"),
        "pan_america": ("泛美", "泛美系"),
        "netherlands": ("荷兰", "荷系"),
        "spain": ("西班牙", "西系"),
        "commonwealth": ("英联邦",),
    }.items()
    for alias in aliases
}
TYPE_ALIASES = {
    alias: ship_type
    for ship_type, aliases in {
        ShipType.BATTLESHIP: ("战列舰", "战列", "bb"),
        ShipType.CRUISER: ("巡洋舰", "巡洋", "ca"),
        ShipType.DESTROYER: ("驱逐舰", "驱逐", "dd"),
        ShipType.CARRIER: ("航空母舰", "航母", "cv"),
        ShipType.SUBMARINE: ("潜艇", "潜水艇", "ss"),
    }.items()
    for alias in aliases
}
ROMAN_TIERS = dict(
    zip(
        ("i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi"),
        range(1, 12),
    )
)
CATEGORY_ALIASES = {
    alias: category
    for category, aliases in {
        ShipCategory.TECH: ("研发船", "研发", "银币船", "银币", "tech"),
        ShipCategory.PREMIUM: ("金币船", "金币", "premium"),
        ShipCategory.SPECIAL: ("特种船", "特种", "special"),
        ShipCategory.SUPER: ("超级战舰", "超级", "超战", "super"),
    }.items()
    for alias in aliases
}

DEFAULT_TIER_MIN = 5
DEFAULT_TIER_MAX = 10

TIER_SUFFIXES = ("级", "級")
TIER_RANGE_ERROR = "等级必须为 1–11 或 I–XI"
FILTER_HINT = (
    "可用条件：我的/全库、国家（日本/美国/德国…）、舰种（驱逐/巡洋/战列/航母/潜艇）、"
    "分类（研发/金币/特种/超级战舰）、等级（写 8 或 VIII，也接受 8级）"
)


def _strip_tier_suffix(token: str) -> str:
    for suffix in TIER_SUFFIXES:
        if token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def _tier_from_token(token: str) -> int | None:
    """等级写法 → 1–11（8 / VIII / 8级 / VIII级）；不是等级写法或超出范围返回 None"""
    base = _strip_tier_suffix(token)
    if base in ROMAN_TIERS:
        return ROMAN_TIERS[base]
    if len(base) <= 2 and base.isascii() and base.isdecimal():
        value = int(base)
        if 1 <= value <= 11:
            return value
    return None


def _is_numeric_tier(token: str) -> bool:
    """纯 ASCII 数字（含超范围的 12/999）：报更具体的范围错误，而不是「无法识别」"""
    base = _strip_tier_suffix(token)
    return base.isascii() and base.isdecimal()


def parse_arguments(text: str) -> SelectionRequest:
    pool = Pool.AUTO
    nation = None
    tier = None
    ship_type = None
    category = None
    for token in unicodedata.normalize("NFKC", text).casefold().split():
        if token in {"我的", "全库"}:
            new_pool = Pool.PERSONAL if token == "我的" else Pool.CATALOG
            if pool not in {Pool.AUTO, new_pool}:
                raise SelectionError(
                    ErrorCode.INVALID_ARGUMENT, "我的和全库不能同时指定"
                )
            pool = new_pool
        elif token in NATION_ALIASES:
            new_nation = NATION_ALIASES[token]
            if nation is not None and nation != new_nation:
                raise SelectionError(ErrorCode.INVALID_ARGUMENT, "只能指定一个国家")
            nation = new_nation
        elif token in CATEGORY_ALIASES:
            new_category = CATEGORY_ALIASES[token]
            if category is not None and category != new_category:
                raise SelectionError(ErrorCode.INVALID_ARGUMENT, "只能指定一种船只分类")
            category = new_category
        elif token in TYPE_ALIASES:
            new_type = TYPE_ALIASES[token]
            if ship_type is not None and ship_type != new_type:
                raise SelectionError(ErrorCode.INVALID_ARGUMENT, "只能指定一个舰种")
            ship_type = new_type
        elif (new_tier := _tier_from_token(token)) is not None:
            if tier is not None and tier != new_tier:
                raise SelectionError(ErrorCode.INVALID_ARGUMENT, "只能指定一个等级")
            tier = new_tier
        elif _is_numeric_tier(token):
            raise SelectionError(ErrorCode.INVALID_ARGUMENT, TIER_RANGE_ERROR)
        else:
            raise SelectionError(
                ErrorCode.INVALID_ARGUMENT, f"无法识别过滤条件：{token}（{FILTER_HINT}）"
            )
    # 默认只抽 5–10 级；“超级战舰”本身代表 XI 级，因此不受默认范围限制。
    default_min = DEFAULT_TIER_MIN if tier is None and category != ShipCategory.SUPER else None
    default_max = DEFAULT_TIER_MAX if tier is None and category != ShipCategory.SUPER else None
    return SelectionRequest(
        pool,
        ShipFilter(nation, tier, ship_type, category, default_min, default_max),
    )
