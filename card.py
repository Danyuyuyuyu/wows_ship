"""选船卡片的展示数据映射；不依赖 NoneBot、网络或持久化。"""

from __future__ import annotations

from .card_assets import CARD_ASSETS
from .messages import (
    NATION_LABELS,
    TYPE_LABELS,
    cache_notice,
    source_label,
)
from .models import Selection, Ship, ShipType

SHIP_TYPE_CODES = {
    ShipType.DESTROYER: "DD",
    ShipType.CRUISER: "CA",
    ShipType.BATTLESHIP: "BB",
    ShipType.CARRIER: "CV",
    ShipType.SUBMARINE: "SS",
}
ASSET_NATIONS = {
    "pan_asia": "panasia",
    "pan_america": "panamerica",
}
CATEGORY_LABELS = {
    "tech": "研发船",
    "premium": "金币船",
    "special": "特种船",
    "super": "超级战舰",
}
ROMAN = (
    "",
    "I",
    "II",
    "III",
    "IV",
    "V",
    "VI",
    "VII",
    "VIII",
    "IX",
    "X",
    "XI",
)
EMPTY_REQUESTER = "这位舰长"


def ship_category(ship: Ship) -> str:
    if ship.tier >= 11:
        return "super"
    if ship.is_special:
        return "special"
    return "premium" if ship.is_premium else "tech"


def ship_type_code(ship: Ship) -> str:
    return SHIP_TYPE_CODES[ship.ship_type]


def asset_nation(ship: Ship) -> str | None:
    nation = ASSET_NATIONS.get(ship.nation, ship.nation)
    return nation if f"flag_{nation}.png" in CARD_ASSETS else None


def tier_label(ship: Ship) -> str:
    number = ROMAN[ship.tier] if 0 < ship.tier < len(ROMAN) else str(ship.tier)
    return f"★ {number}" if ship_category(ship) == "super" else number


def asset(name: str) -> str | None:
    return CARD_ASSETS.get(name)


def card_context(
    selection: Selection,
    *,
    ship_name: str | None = None,
    ship_name_en: str | None = None,
    image_data_uri: str | None = None,
    requester: str = "",
) -> dict:
    """把一艘选中的船展开为模板变量；缺失素材自动降级为纯文字标签。"""
    ship = selection.ship
    category = ship_category(ship)
    type_code = ship_type_code(ship)
    nation = asset_nation(ship)
    name = (ship_name or ship.label).strip()
    english = (ship_name_en or "").strip().upper()
    if not english or english == name.upper() or english == ship.name.strip().upper():
        english = ""
    requester = requester.strip()
    return {
        "category": category,
        "category_label": CATEGORY_LABELS[category],
        "category_icon": asset(f"category_{category}.svg"),
        "requester": requester or EMPTY_REQUESTER,
        "name": name,
        "name_en": english,
        "tier_label": tier_label(ship),
        "is_super": category == "super",
        "type_label": TYPE_LABELS[ship.ship_type],
        "type_icon": asset(f"shiptype_{type_code}_{category}.png"),
        "nation_label": NATION_LABELS.get(ship.nation, ship.nation),
        "nation_icon": asset(f"flag_{nation}.png") if nation else None,
        "image_url": image_data_uri,
        "is_contour": ship.image_url is None,
        "pool_label": source_label(selection.pool),
        "cache_notice": cache_notice(selection),
    }
