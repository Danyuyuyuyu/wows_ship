"""领域错误与文本结果；渲染失败保持同一艘船和来源。"""

from datetime import datetime, timedelta, timezone

from .models import ErrorCode, Pool, Selection, SelectionError, ShipType

TYPE_LABELS = {
    ShipType.BATTLESHIP: "战列舰",
    ShipType.CRUISER: "巡洋舰",
    ShipType.DESTROYER: "驱逐舰",
    ShipType.CARRIER: "航空母舰",
    ShipType.SUBMARINE: "潜艇",
}
NATION_LABELS = {
    "japan": "日本",
    "usa": "美国",
    "germany": "德国",
    "ussr": "苏联",
    "uk": "英国",
    "france": "法国",
    "italy": "意大利",
    "pan_asia": "泛亚",
    "europe": "欧洲",
    "pan_america": "泛美",
    "netherlands": "荷兰",
    "spain": "西班牙",
    "commonwealth": "英联邦",
}
ERROR_MESSAGES = {
    ErrorCode.BINDING_REQUIRED: "请先 wows绑定 <亚服昵称>，或使用“全库”。",
    ErrorCode.UNSUPPORTED_ACCOUNT: "第一版仅支持亚服个人池，请使用“全库”。",
    ErrorCode.NO_OWNED_SHIPS: "你的持有清单为空，请更新清单或手动使用“全库”。",
    ErrorCode.INVENTORY_UNAVAILABLE: "持有清单暂不可用，请检查清单后重试。",
    ErrorCode.NO_HISTORY: "该账号没有历史舰船记录，可以使用“全库”。",
    ErrorCode.HIDDEN_PROFILE: "该账号战绩已隐藏，可以使用“全库”。",
    ErrorCode.STATS_UNAVAILABLE: "个人战绩暂不可用，请稍后再试或使用“全库”。",
    ErrorCode.EMPTY_POOL: "当前随机池没有符合条件的船。",
    ErrorCode.NEW_SHIP_PENDING: "有新船资料或客户端船名待补齐，请稍后再试。",
}


def source_label(pool: Pool) -> str:
    if pool == Pool.OWNED:
        return "亚服持有清单"
    return "亚服历史使用舰船" if pool == Pool.PERSONAL else "亚服全库"


def cache_notice(selection: Selection) -> str:
    notices = []
    zone = timezone(timedelta(hours=8))
    if selection.catalog_stale:
        date = datetime.fromtimestamp(selection.catalog_fetched_at, zone)
        notices.append(f"目录缓存日期：{date:%Y-%m-%d %H:%M}（北京时间，非实时）")
    if selection.history_stale and selection.history_updated_at is not None:
        date = datetime.fromtimestamp(selection.history_updated_at, zone)
        notices.append(
            f"历史池暂用本地旧记录，更新于：{date:%Y-%m-%d %H:%M}（北京时间）"
        )
    if selection.pending_ship_count:
        notices.append(
            f"有 {selection.pending_ship_count} 艘舰船资料待补齐，暂未参与随机"
        )
    return "；".join(notices)


def selection_text(selection: Selection) -> str:
    ship = selection.ship
    text = (
        f"{ship.label} · {ship.tier}级 · {TYPE_LABELS[ship.ship_type]} · "
        f"{NATION_LABELS.get(ship.nation, ship.nation)} · "
        f"{source_label(selection.pool)}"
    )
    notice = cache_notice(selection)
    return f"{text}\n{notice}" if notice else text


def error_text(error: SelectionError) -> str:
    if error.code == ErrorCode.INVALID_ARGUMENT:
        return str(error)
    return ERROR_MESSAGES.get(error.code, "选船暂不可用，请稍后再试。")
