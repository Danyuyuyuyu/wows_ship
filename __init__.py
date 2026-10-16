"""亚服持有清单优先、历史舰船兜底的选船插件。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import logging
import math
import time

from nonebot import get_bots, get_driver, get_plugin, on_command, on_message, require
from nonebot.adapters import Bot as BaseBot
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import ActionFailed, Bot, MessageEvent, MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule, to_me
from nonebot.typing import T_State

from zhenxun.configs.config import BotConfig
from zhenxun.configs.path_config import DATA_PATH
from zhenxun.configs.utils import Command, Example, PluginExtraData

from .api import ApiError
from .binding import SessionError
from .cache import BusyError
from .config import load_settings, register_config
from .messages import error_text
from .models import SelectionError
from .service import WowsService
from .storage import StorageError
from .updater import UpdateError

require("nonebot_plugin_htmlrender")
logger = logging.getLogger(__name__)
register_config()
__plugin_meta__ = PluginMetadata(
    name="这把玩什么船",
    description="默认从 5–10 级船中随机；支持研发/银币、金币、特种和超级战舰分类筛选",
    usage=(
        "@真寻 这把玩什么船 [我的|全库] [国家] [1–11|I–XI] [舰种] [研发/金币/特种/超级]\n"
        "wows绑定 <亚服昵称> / wows解绑 / wows我的账号\n"
        "wows更新历史池 / wows船库状态 / wows持有清单\n"
        "超级用户：wows更新船库 / wows校验船库"
    ),
    supported_adapters={"~onebot.v11"},
    extra=PluginExtraData(
        author="zhenxun-user",
        version="1.0.0",
        commands=[
            Command(
                command="这把玩什么船",
                description=(
                    "从你的持有舰船（未绑定时用历史池）里随机抽一艘并发卡片，默认只抽 5–10 级；"
                    "支持我的/全库、国家、舰种、研发/金币/特种/超级战舰和等级筛选"
                ),
                params=[
                    "我的：只用持有清单；全库：强制从全部舰船里抽",
                    "国家：日本/美国/德国/苏联/英国/法国/意大利/泛亚/欧洲/泛美/荷兰/西班牙/英联邦",
                    "舰种：驱逐(dd)/巡洋(ca)/战列(bb)/航母(cv)/潜艇(ss)",
                    "分类：研发(银币)/金币/特种/超级战舰；10 级没有直售金币船（数据里标为特种），10 级的「金币」会把特种一并算进来",
                    "等级：写 8 或 VIII（也接受 8级），1–11；不写默认 5–10",
                ],
                examples=[
                    Example(
                        exec="这把玩什么船 全库 日本 10 bb 金币",
                        description="全库里抽一艘 10 级日本金币战列舰",
                    )
                ],
            ),
            Command(command="wows绑定"),
            Command(command="wows解绑"),
            Command(command="wows我的账号"),
            Command(command="wows更新船库"),
            Command(command="wows校验船库"),
            Command(command="wows船库状态"),
            Command(command="wows更新历史池"),
            Command(command="wows持有清单"),
        ],
    ).to_dict(),
)
driver = get_driver()
_service: WowsService | None = None
_service_lock = asyncio.Lock()
_last_config_error: str | None = None
_manager_task: asyncio.Task | None = None
_stopping = False
_notification_deliveries: dict[str, set[str]] = {}
COMMANDS = (
    "这把玩什么船",
    "随机船",
    "选船",
    "wows绑定",
    "wows解绑",
    "wows我的账号",
    "wows更新船库",
    "wows校验船库",
    "wows船库状态",
    "wows更新历史池",
    "wows持有清单",
)


async def notify_superusers(text: str) -> bool:
    configured = BotConfig.get_superuser("qq") or driver.config.superusers
    recipients = {
        str(value)
        for value in configured
        if str(value).isascii() and str(value).isdecimal()
    }
    bots = [bot for bot in get_bots().values() if isinstance(bot, Bot)]
    if not recipients or not bots:
        return False
    delivered = _notification_deliveries.setdefault(text, set())
    for user_id in recipients - delivered:
        for bot in bots:
            try:
                await bot.send_private_msg(user_id=int(user_id), message=text)
                delivered.add(user_id)
                break
            except Exception:
                logger.warning("超级用户故障通知暂未送达，尝试其他机器人")
    if recipients <= delivered:
        _notification_deliveries.pop(text, None)
        return True
    return False


async def sync_replay_translations(language, *, force=False):
    plugin = get_plugin("wows_replay")
    if plugin is None:
        return None
    return await plugin.module.translations.sync_latest(language, force=force)


async def get_service() -> WowsService | None:
    global _service, _last_config_error
    async with _service_lock:
        if _stopping:
            return None
        try:
            settings = load_settings()
        except (ValueError, TypeError) as error:
            message = str(error)
            if message != _last_config_error:
                logger.warning("wows_ship 配置无效：%s", message)
                _last_config_error = message
            if _service is not None:
                await _service.health.issue(
                    "config",
                    "插件配置校验失败，继续使用最近有效配置",
                    severity="critical",
                )
            return _service
        _last_config_error = None
        if not settings.configured:
            if _service is not None:
                await _service.close()
                _service = None
            return None
        if _service is None or _service.settings != settings:
            if _service is not None:
                await _service.close()
                _service = None
            candidate = None
            try:
                candidate = WowsService(
                    settings,
                    DATA_PATH,
                    notifier=notify_superusers,
                    translation_sync=sync_replay_translations,
                )
                candidate.start_warmup()
            except (StorageError, BusyError, OSError) as error:
                if candidate is not None:
                    await candidate.close()
                logger.warning(
                    "wows_ship 启动暂不可用（%s），管理任务稍后重试",
                    type(error).__name__,
                )
                return None
            _service = candidate
        await _service.health.recover("config")
        return _service


async def manager_loop():
    while not _stopping:
        try:
            service = await get_service()
            if service is not None:
                task = service.watchdog_task
                if task is not None and task.done():
                    if not task.cancelled():
                        task.exception()
                    await service.health.issue(
                        "task_watchdog",
                        "后台监督任务意外退出，正在重建",
                        severity="critical",
                    )
                    service.start_warmup()
        except Exception as error:
            logger.warning("插件管理巡检失败（%s）", type(error).__name__)
        await asyncio.sleep(60)


@driver.on_startup
async def startup() -> None:
    global _manager_task, _stopping
    _stopping = False
    service = await get_service()
    if _manager_task is None or _manager_task.done():
        _manager_task = asyncio.create_task(manager_loop())
    logger.info("wows_ship 已启用" if service else "wows_ship 未启用或暂不可用")


@driver.on_shutdown
async def shutdown() -> None:
    global _service, _stopping, _manager_task
    _stopping = True
    if _manager_task is not None:
        _manager_task.cancel()
        await asyncio.gather(_manager_task, return_exceptions=True)
        _manager_task = None
    async with _service_lock:
        if _service is not None:
            await _service.close()
            _service = None


def event_group(event: MessageEvent) -> str | None:
    value = getattr(event, "group_id", None)
    return str(value) if value is not None else None


def scope(bot: BaseBot, event: MessageEvent) -> tuple[str, str, str | None]:
    return (
        bot.self_id,
        str(event.user_id),
        str(event.group_id) if hasattr(event, "group_id") else None,
    )


def requester_name(event: MessageEvent) -> str:
    """卡片顶部的 @ 名字：群名片优先，空则用 QQ 昵称。"""
    sender = getattr(event, "sender", None)
    for attribute in ("card", "nickname"):
        value = getattr(sender, attribute, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


async def command_allowed(bot: BaseBot, event: Event) -> bool:
    if not isinstance(bot, Bot) or not isinstance(event, MessageEvent):
        return False
    service = await get_service()
    return service is not None and service.allowed(scope(bot, event)[2])


def is_binding_reply(text: str) -> bool:
    """只把“取消”或 1–2 位数字视为候选回复，其他消息一律放行给别的插件。"""
    return text == "取消" or (text.isascii() and text.isdecimal() and len(text) <= 2)


async def pending_reply(bot: BaseBot, event: Event, state: T_State) -> bool:
    if not isinstance(bot, Bot) or not isinstance(event, MessageEvent):
        return False
    text = event.get_plaintext().strip()
    if not is_binding_reply(text):
        return False
    service = await get_service()
    if service is None or not service.allowed(scope(bot, event)[2]):
        return False
    session = service.pending_binding(*scope(bot, event))
    if session is None:
        return False
    state["wows_session"] = session
    return True


@asynccontextmanager
async def operation(service):
    task = asyncio.current_task()
    if task is not None:
        service.tasks.add(task)
    try:
        yield
    finally:
        if task is not None:
            service.tasks.discard(task)


def failure_text(error: Exception) -> str:
    if isinstance(error, SelectionError):
        return error_text(error)
    if isinstance(error, StorageError | BusyError | SessionError | UpdateError):
        return str(error)
    if isinstance(error, ApiError):
        if str(error) == "INVALID_IP_ADDRESS":
            return "WG IP 白名单不匹配，请管理员更新应用白名单。"
        if str(error) in {"INVALID_APPLICATION_ID", "APPLICATION_IS_BLOCKED"}:
            return "WG 应用配置不可用，请管理员检查 WOWS_APP_ID。"
        if error.http_status == 429 or str(error) == "REQUEST_LIMIT_EXCEEDED":
            return "WG 请求暂时受限，请稍后再试。"
        if str(error).startswith("请输入"):
            return str(error)
    if isinstance(error, asyncio.TimeoutError):
        return "操作超时，请稍后再试。"
    logger.warning("选船操作失败（%s）", type(error).__name__)
    return "舰船数据暂不可用，请稍后再试；个人选船也可改用“全库”。"


async def finish_card(matcher: Matcher, card) -> None:
    if card.png is not None:
        try:
            await matcher.finish(MessageSegment.image(card.png))
        except ActionFailed:
            logger.warning("舰船图片发送失败，改用相同结果文本")
    await matcher.finish(card.text)


rule = Rule(command_allowed) & to_me()
select_command = on_command(
    "这把玩什么船", aliases={"随机船", "选船"}, rule=rule, priority=5, block=True
)
bind_command = on_command("wows绑定", rule=rule, priority=5, block=True)
unbind_command = on_command("wows解绑", rule=rule, priority=5, block=True)
account_command = on_command("wows我的账号", rule=rule, priority=5, block=True)
update_command = on_command(
    "wows更新船库", rule=rule, permission=SUPERUSER, priority=5, block=True
)
verify_command = on_command(
    "wows校验船库", rule=rule, permission=SUPERUSER, priority=5, block=True
)
status_command = on_command("wows船库状态", rule=rule, priority=5, block=True)
inventory_command = on_command("wows持有清单", rule=rule, priority=5, block=True)
history_command = on_command("wows更新历史池", rule=rule, priority=5, block=True)
binding_reply = on_message(rule=Rule(pending_reply), priority=4, block=True)


@select_command.handle()
async def handle_select(
    bot: Bot, event: MessageEvent, matcher: Matcher, args=CommandArg()
):
    service = await get_service()
    if service is None or not service.allowed(event_group(event)):
        return
    bot_id, user_id, _ = scope(bot, event)
    remaining = service.take_cooldown(bot_id, user_id)
    if remaining > 0:
        await matcher.finish(f"请等待 {math.ceil(remaining)} 秒再选船。")
    try:
        async with operation(service):
            card = await service.select(
                user_id, args.extract_plain_text(), requester_name(event)
            )
    except Exception as error:
        await matcher.finish(failure_text(error))
        return
    await finish_card(matcher, card)


@bind_command.handle()
async def handle_bind(
    bot: Bot, event: MessageEvent, matcher: Matcher, args=CommandArg()
):
    service = await get_service()
    if service is None or not service.allowed(event_group(event)):
        return
    nickname = args.extract_plain_text().strip()
    if not nickname:
        await matcher.finish("用法：wows绑定 <亚服游戏昵称>；其他服暂时使用全库随机。")
    try:
        async with operation(service):
            candidates, session = await service.begin_binding(
                *scope(bot, event), nickname
            )
    except Exception as error:
        await matcher.finish(failure_text(error))
        return
    if not candidates:
        await matcher.finish("未找到亚服账号，请检查昵称；其他服暂时全库随机。")
    listing = "\n".join(
        f"{index}. {account.nickname}（{account.account_id}）"
        for index, account in enumerate(candidates, 1)
    )
    await matcher.finish(
        f"亚服账号候选：\n{listing}\n"
        f"请在 {service.settings.binding_timeout_seconds:g} 秒内回复序号，"
        "或回复“取消”。"
    )


@binding_reply.handle()
async def handle_binding_reply(
    bot: Bot, event: MessageEvent, matcher: Matcher, state: T_State
):
    service = await get_service()
    if service is None or not service.allowed(scope(bot, event)[2]):
        return
    session = state["wows_session"]
    bot_id, user_id, group_id = scope(bot, event)
    text = event.get_plaintext().strip()
    if text == "取消":
        cancelled = await service.cancel_binding(
            bot_id, user_id, group_id, session.session_id
        )
        await matcher.finish(
            "绑定已取消，原绑定保持不变。" if cancelled else "绑定会话已结束。"
        )
    if time.monotonic() >= session.expires_at_monotonic:
        await service.cancel_binding(bot_id, user_id, group_id, session.session_id)
        await matcher.finish("绑定会话已超时，请重新 wows绑定。")
    if not text.isascii() or not text.isdecimal() or len(text) > 2:
        await matcher.finish("请回复候选序号，或回复“取消”。")
    try:
        async with operation(service):
            account = await service.commit_binding(session, int(text))
    except Exception as error:
        await matcher.finish(failure_text(error))
        return
    await matcher.finish(
        f"已绑定亚服账号 {account.nickname}（{account.account_id}）。"
        "以后默认优先持有清单；未提供清单时从历史使用舰船中选。"
    )


@unbind_command.handle()
async def handle_unbind(bot: Bot, event: MessageEvent, matcher: Matcher):
    service = await get_service()
    if service is None or not service.allowed(event_group(event)):
        return
    user_id = str(event.user_id)
    try:
        async with operation(service):
            removed = await service.unbind_account(user_id)
    except Exception as error:
        await matcher.finish(failure_text(error))
        return
    await matcher.finish(
        "已解绑，之后默认全库随机。" if removed else "尚未绑定账号，默认全库随机。"
    )


@account_command.handle()
async def handle_account(event: MessageEvent, matcher: Matcher):
    service = await get_service()
    if service is None or not service.allowed(event_group(event)):
        return
    try:
        async with operation(service):
            account = await service.account_for(str(event.user_id))
    except Exception as error:
        await matcher.finish(failure_text(error))
        return
    if account is None:
        await matcher.finish(
            "尚未绑定账号，默认全库随机。可用 wows绑定 <亚服昵称> 关联账号。"
        )
    await matcher.finish(
        f"当前绑定：{account.nickname}（{account.account_id}） · {account.server}\n"
        "亚服默认优先持有清单，未提供时选历史舰船；其他服暂时全库随机。"
    )


@update_command.handle()
async def handle_update(
    bot: Bot, event: MessageEvent, matcher: Matcher, args=CommandArg()
):
    if not await SUPERUSER(bot, event):
        await matcher.finish("仅机器人超级用户可以更新船库。")
    service = await get_service()
    if service is None or not service.allowed(event_group(event)):
        return
    if args.extract_plain_text().strip():
        await matcher.finish("用法：wows更新船库")
    try:
        async with operation(service):
            report = await service.refresh_data()
    except Exception as error:
        await matcher.finish(f"船库更新未完成：{failure_text(error)} 原版本继续使用。")
        return
    await matcher.finish(update_text(report, "船库更新完成"))


def update_text(report, title):
    changed = (
        "新目录已生效。"
        if getattr(report, "published", True)
        else "资料无变化，继续使用当前版本。"
    )
    return (
        f"{title}：共 {report.total_ships} 艘，新增 {report.new_ships} 艘，"
        f"修改 {getattr(report, 'modified_ships', 0)} 艘，"
        f"移出当前目录 {getattr(report, 'removed_ships', 0)} 艘，"
        f"补齐 {report.new_images} 张图片。\n"
        f"待补船名 {getattr(report, 'pending_names', 0)} 艘，"
        f"图片未完成 {getattr(report, 'failed_images', 0)} 张。{changed}"
    )


@verify_command.handle()
async def handle_verify(
    bot: Bot, event: MessageEvent, matcher: Matcher, args=CommandArg()
):
    if not await SUPERUSER(bot, event):
        await matcher.finish("仅机器人超级用户可以校验船库。")
    service = await get_service()
    if service is None or not service.allowed(event_group(event)):
        return
    if args.extract_plain_text().strip():
        await matcher.finish("用法：wows校验船库")
    try:
        async with operation(service):
            report = await service.verify_data()
    except Exception as error:
        await matcher.finish(f"船库校验未完成：{failure_text(error)}")
        return
    await matcher.finish(update_text(report, "船库校验完成"))


@status_command.handle()
async def handle_status(event: MessageEvent, matcher: Matcher, args=CommandArg()):
    service = await get_service()
    if service is None or not service.allowed(event_group(event)):
        return
    if args.extract_plain_text().strip():
        await matcher.finish("用法：wows船库状态")
    try:
        row = await service.data_status()
    except Exception as error:
        await matcher.finish(failure_text(error))
        return
    checked = datetime.fromtimestamp(
        row.get("checked_at", row["catalog_fetched_at"]), timezone(timedelta(hours=8))
    )
    active_issues = sum(
        bool(issue.get("active"))
        for issue in row.get("health", {}).get("issues", {}).values()
        if isinstance(issue, dict)
    )
    await matcher.finish(
        f"当前船库：{row['ships']} 艘；待补船名 {row.get('pending_names', 0)} 艘；"
        f"图片未完成 {row.get('failed_images', 0)} 张。\n"
        f"上次目录检查：{checked:%Y-%m-%d %H:%M}（北京时间）\n"
        f"客户端名称版本：{row['client_build']}；"
        f"可清理旧快照 {len(row['cleanup_candidates'])} 份（保留未删除）。\n"
        f"后台告警 {active_issues} 项；"
        f"巡检间隔 {service.settings.maintenance_interval_minutes:g} 分钟，"
        f"历史池每 {service.settings.history_refresh_days:g} 天刷新。"
    )


@history_command.handle()
async def handle_history(event: MessageEvent, matcher: Matcher, args=CommandArg()):
    service = await get_service()
    if service is None or not service.allowed(event_group(event)):
        return
    if args.extract_plain_text().strip():
        await matcher.finish("用法：wows更新历史池（只更新本人已绑定的亚服账号）")
    try:
        async with operation(service):
            snapshot = await service.refresh_my_history(str(event.user_id))
    except Exception as error:
        await matcher.finish(failure_text(error))
        return
    if snapshot.result.status.value == "hidden":
        await matcher.finish("最新战绩为隐藏状态，历史池暂不可用。")
    if snapshot.result.status.value == "empty":
        await matcher.finish("历史池已更新：该账号没有有效战斗记录。")
    await matcher.finish(
        f"历史池已更新，共 {len(snapshot.result.ship_ids)} 艘使用过的船。"
        "新船资料未齐时会后台补齐；持有清单仍需单独维护。"
    )


@inventory_command.handle()
async def handle_inventory(event: MessageEvent, matcher: Matcher, args=CommandArg()):
    service = await get_service()
    if service is None or not service.allowed(event_group(event)):
        return
    try:
        async with operation(service):
            response = await service.manage_inventory(
                str(event.user_id), args.extract_plain_text()
            )
    except ValueError as error:
        await matcher.finish(str(error))
        return
    except Exception as error:
        await matcher.finish(failure_text(error))
        return
    await matcher.finish(response)

