import datetime as dt
import json
import logging
import os
import re
from typing import List, Optional

from astrbot.api.all import AstrMessageEvent, Context, Star, command_group, register
from astrbot.api.event import MessageChain, filter

from .core import create_provider
from .core.flow import FlowEngine
from .core.formatting import format_lock_confirm, format_locks, format_quick
from .core.models import Lock
from .core.parsing import (
    parse_date,
    parse_train_type_tokens,
)
from .core.providers import ProviderError
from .core.scheduler import RefreshScheduler
from .core.session import SessionManager
from .core.storage import LockStorage

KNOWN_SOURCES = ("juhe", "apihz", "demo")

HELP_TEXT = "\n".join(
    [
        "=== 火车票查询插件 ===",
        "/火车票 查票 [类型...]    交互式查询车次与票价（可选 G高铁/D动车/Z直达/T特快/K快速/O其他）",
        "/火车票 锁定 <车次号...>  锁定查询结果中的车次（多个用空格分隔）",
        "/火车票 余票提醒 <开|关> [车次号...] [日期]  开关余票提醒（默认开启，余票低于阈值时提醒；日期可选，如 2026-08-07）",
        "/火车票 快查              查看已锁定车次的缓存价格（不调用API）",
        "/火车票 强制更新          立即调用API刷新所有已锁定车次的价格",
        "/火车票 解锁 <车次号...>  解锁车次",
        "/火车票 我的              查看我的锁定车次",
        "/火车票 数据源 [名称]     查看或切换数据源：juhe=聚合数据 / apihz=接口盒子 / demo=演示数据",
        "/火车票 帮助              显示本帮助",
        "说明：价格默认每 4 小时自动刷新一次，且仅在刷新/强制更新/查票时调用数据源API。",
        "注意：接口盒子(apihz)返回的是未打折公示票价，一切价格以购票处实际为准。",
    ]
)


@register(
    "astrbot_plugin_train_ticket_search",
    "deeps",
    "火车票查询插件：聚合数据API查票、锁定、余票提醒、缓存快查与强制更新",
    "v1.4.0",
    "https://github.com/example/astrbot_plugin_train_ticket_search",
)
class TrainTicketSearchPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.logger = logging.getLogger("train_ticket_search")
        self.config = config or {}
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.data_dir = os.path.join(self.plugin_dir, "data")
        os.makedirs(self.data_dir, exist_ok=True)

        self.command_prefix = self.config.get("command_prefix", "/") or "/"
        self.max_locks = int(
            self.config.get("max_locked_trains_per_user", 20) or 20
        )
        self.startup_refresh = bool(self.config.get("startup_refresh", True))
        self.ticket_alert_threshold = int(
            self.config.get("ticket_alert_threshold", 10) or 10
        )

        self.storage = LockStorage(os.path.join(self.data_dir, "locks.json"))
        # 数据源：聊天指令切换的结果保存在 data/runtime_config.json，
        # 优先于管理面板配置（重启后仍生效）。
        source_name = self._load_runtime_source() or (
            (self.config.get("data_source", "juhe") or "juhe").strip().lower()
        )
        if source_name not in KNOWN_SOURCES:
            self.logger.warning(f"配置的数据源 {source_name!r} 无效，回退为 juhe")
            source_name = "juhe"
        try:
            self.provider = create_provider(source_name, self.config, self.plugin_dir)
        except Exception:
            self.logger.exception(f"创建数据源 {source_name} 失败，回退为 demo")
            self.provider = create_provider("demo", self.config, self.plugin_dir)
        self.sessions = SessionManager(timeout_seconds=600)
        self.flow = FlowEngine(
            search=self.provider.search,
            call_info_getter=self.provider.call_info,
        )
        self.scheduler = RefreshScheduler(
            self.storage,
            self.provider,
            self._send_text,
            interval_hours=float(self.config.get("refresh_interval_hours", 4) or 4),
            logger=self.logger,
        )
        self._started = False
        self.logger.info(
            f"火车票插件初始化完成：数据源={self.provider.name}，"
            f"刷新间隔={self.scheduler.interval_hours}小时"
        )

    async def initialize(self) -> None:
        # AstrBot 生命周期钩子：启动时刷新一次（有锁定车次才调用 API），并启动定时刷新
        if self.startup_refresh:
            try:
                await self.scheduler.refresh_once()
            except Exception as e:
                self.logger.exception(f"启动刷新失败: {e}")
        if not self._started:
            self.scheduler.start()
            self._started = True
        self.logger.info(
            f"火车票插件调度器已启动（每 {self.scheduler.interval_hours:g} 小时刷新一次）"
        )

    async def terminate(self) -> None:
        try:
            self.scheduler.shutdown()
        except Exception:
            pass

    async def _send_text(self, unified_msg_origin: str, text: str) -> None:
        await self.context.send_message(
            unified_msg_origin, MessageChain().message(text)
        )

    def _runtime_config_path(self) -> str:
        return os.path.join(self.data_dir, "runtime_config.json")

    def _load_runtime_source(self) -> str:
        """读取聊天指令切换后保存的数据源名；未切换过返回空串。"""
        try:
            with open(self._runtime_config_path(), encoding="utf-8") as f:
                name = str(json.load(f).get("data_source") or "").strip().lower()
        except (OSError, ValueError):
            return ""
        return name if name in KNOWN_SOURCES else ""

    def _save_runtime_source(self, name: str) -> None:
        try:
            with open(self._runtime_config_path(), "w", encoding="utf-8") as f:
                json.dump({"data_source": name}, f, ensure_ascii=False)
        except OSError:
            self.logger.exception("保存运行时数据源配置失败")

    def _build_provider(self, name: str):
        """创建数据源并提前校验凭据，避免切换到未配置的数据源后使用时才报错。"""
        provider = create_provider(name, self.config, self.plugin_dir)
        if provider.name == "juhe":
            from .core.juhe_provider import PLACEHOLDER_KEYS

            key = (getattr(provider, "appkey", "") or "").strip()
            if not key or key.lower() in PLACEHOLDER_KEYS:
                raise ProviderError(
                    "聚合数据源未配置 juhe_appkey（仍为占位符），"
                    "请先在管理面板的插件配置中填写后再切换。"
                )
        elif provider.name == "apihz":
            provider._check_credentials()
        return provider

    def _switch_provider(self, name: str) -> None:
        provider = self._build_provider(name)
        self.provider = provider
        # 流程引擎与调度器都持有 provider 引用，切换后同步替换
        self.flow = FlowEngine(
            search=provider.search, call_info_getter=provider.call_info
        )
        self.scheduler.provider = provider
        self._save_runtime_source(name)
        self.logger.info(f"数据源已切换为 {name}（{provider.display_name}）")

    def _tokens(self, event: AstrMessageEvent) -> List[str]:
        text = (getattr(event, "message_str", "") or "").strip()
        text = re.sub(r"^\s*" + re.escape(self.command_prefix), "", text)
        return [t for t in re.split(r"\s+", text) if t]

    @command_group("火车票")
    def train_group(self):
        # 火车票相关指令组。用法：/火车票 <子指令>
        pass

    @train_group.command("帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result(HELP_TEXT)

    @train_group.command("查票")
    async def cmd_search(self, event: AstrMessageEvent):
        tokens = self._tokens(event)
        train_types, err = parse_train_type_tokens(tokens[2:])
        if err:
            yield event.plain_result(err)
            return
        session = self.sessions.start(
            str(event.get_sender_id()), event.unified_msg_origin, train_types
        )
        yield event.plain_result(
            "请输入目标日期（今天/明天/后天 或 YYYY-MM-DD，"
            f"聚合数据接口限未来 {15} 天内）："
        )

    @train_group.command("锁定")
    async def cmd_lock(self, event: AstrMessageEvent):
        sender = str(event.get_sender_id())
        tokens = self._tokens(event)
        train_tokens = [t.upper() for t in tokens[2:]]
        if not train_tokens:
            yield event.plain_result(
                "用法：/火车票 锁定 <车次号> [车次号...]（先 /火车票 查票 查询后再锁定）"
            )
            return
        session = self.sessions.get(sender, event.unified_msg_origin)
        if session is None or not session.last_query:
            yield event.plain_result(
                "没有可锁定的车次。请先使用 /火车票 查票 查询目标线路的车次。"
            )
            return
        by_no = {t.train_no: t for t in session.last_query}
        unknown = [t for t in train_tokens if t not in by_no]
        if unknown:
            yield event.plain_result(
                "以下车次号不在最近查询结果中：" + "、".join(unknown)
                + "。请先 /火车票 查票 后再锁定。"
            )
            return
        existing = {l.key() for l in self.storage.locks_by_user(sender)}
        added: List[Lock] = []
        for t in train_tokens:
            tr = by_no[t]
            lock = Lock(
                user_id=sender,
                unified_msg_origin=event.unified_msg_origin,
                train_type=tr.train_type,
                train_no=tr.train_no,
                depart_station=tr.depart_station,
                arrive_station=tr.arrive_station,
                depart_date=tr.depart_date,
                depart_time=tr.depart_time,
                arrive_time=tr.arrive_time,
                seat_name=tr.seat_name or "二等座",
                ticket_alert_enabled=True,
                ticket_alert_threshold=self.ticket_alert_threshold,
                last_price=tr.price if tr.price > 0 else None,
                last_num=tr.num,
                prices=list(tr.prices),
                locked_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            )
            if lock.key() in existing:
                continue
            if len(existing) + len(added) >= self.max_locks:
                yield event.plain_result(
                    f"每个用户最多锁定 {self.max_locks} 个车次，已达上限。"
                    "可用 /火车票 解锁 释放。"
                )
                return
            if self.storage.add_lock(lock):
                added.append(lock)
                existing.add(lock.key())
        if not added:
            yield event.plain_result("这些车次已经锁定过了。")
        else:
            yield event.plain_result(format_lock_confirm(added))

    @train_group.command("解锁")
    async def cmd_unlock(self, event: AstrMessageEvent):
        sender = str(event.get_sender_id())
        tokens = self._tokens(event)
        train_tokens: List[str] = []
        date_filter: Optional[str] = None
        for t in tokens[2:]:
            d, err = parse_date(t)
            if err is None and d is not None:
                date_filter = d.isoformat()
            else:
                train_tokens.append(t.upper())
        if not train_tokens:
            yield event.plain_result("用法：/火车票 解锁 <车次号> [车次号...] [日期]")
            return
        removed = self.storage.remove_locks(sender, set(train_tokens), date_filter)
        if removed:
            yield event.plain_result(f"已解锁 {removed} 个车次。")
        else:
            yield event.plain_result("没有找到匹配的锁定车次。")

    @train_group.command("余票提醒")
    async def cmd_ticket_alert(self, event: AstrMessageEvent):
        sender = str(event.get_sender_id())
        tokens = self._tokens(event)
        args = tokens[2:]
        if not args or args[0] not in ("开", "开启", "关", "关闭", "on", "off"):
            yield event.plain_result(
                "用法：/火车票 余票提醒 <开|关> [车次号...] [日期]，"
                "如：/火车票 余票提醒 关 G25 2026-08-07"
            )
            return
        enabled = args[0] in ("开", "开启", "on")
        date_filter: Optional[str] = None
        train_nos: set = set()
        for t in args[1:]:
            d, err = parse_date(t)
            if err is None and d is not None:
                date_filter = d.isoformat()
            else:
                train_nos.add(t.upper())
        train_nos = train_nos or None
        locks = self.storage.locks_by_user(sender)
        if not locks:
            yield event.plain_result(
                "你还没有锁定车次，请先 /火车票 查票 查询并锁定后再设置余票提醒。"
            )
            return
        n = self.storage.set_ticket_alert(sender, enabled, train_nos, date_filter)
        if n == 0:
            suffix = "：" + "、".join(sorted(train_nos)) if train_nos else ""
            if date_filter:
                suffix += f"（日期 {date_filter}）"
            yield event.plain_result("没有找到匹配的锁定车次" + suffix + "。")
            return
        action = "开启" if enabled else "关闭"
        threshold = max(int(self.ticket_alert_threshold or 10), 1)
        detail = f"（余票低于 {threshold} 张时提醒）" if enabled else ""
        scope = f"，车次：{'、'.join(sorted(train_nos))}" if train_nos else ""
        if date_filter:
            scope += f"，日期：{date_filter}"
        yield event.plain_result(
            f"已为 {n} 个锁定车次{action}余票提醒{detail}{scope}。"
        )

    @train_group.command("快查")
    async def cmd_quick(self, event: AstrMessageEvent):
        # 获取价格指令：只读取本地已保存的缓存价格，不调用 API
        sender = str(event.get_sender_id())
        removed = self.storage.remove_departed(sender)
        locks = self.storage.locks_by_user(sender)
        call_info = self.provider.call_info()
        if removed:
            text = "您关注的车次已发车，已将其自动解除锁定。\n\n" + format_quick(
                locks, call_info
            )
        else:
            text = format_quick(locks, call_info)
        yield event.plain_result(text)

    @train_group.command("强制更新")
    async def cmd_force_update(self, event: AstrMessageEvent):
        sender = str(event.get_sender_id())
        locks = self.storage.locks_by_user(sender)
        if not locks:
            yield event.plain_result(
                "你还没有锁定任何车次，无需强制更新。请先 /火车票 查票 查询并锁定。"
            )
            return
        try:
            await self.scheduler.refresh_once()
        except Exception as e:
            self.logger.exception(f"强制更新失败: {e}")
            yield event.plain_result(f"强制更新失败：{e}")
            return
        result = self.scheduler.last_result
        locks = self.storage.locks_by_user(sender)
        my_keys = {l.key() for l in locks}
        if result is None:
            updated, failed = my_keys, []
        elif result.skipped:
            yield event.plain_result(
                "已有刷新任务在运行，本次强制更新被跳过，请稍后再试。\n\n"
                + format_quick(locks, self.provider.call_info())
            )
            return
        else:
            updated = my_keys & result.updated_keys
            failed = my_keys - result.updated_keys
        lines = [f"已强制更新 {len(updated)} 个锁定车次的价格。"]
        if failed:
            failed_names = "、".join(
                sorted(l.train_no for l in locks if l.key() in failed)
            )
            lines.append(
                f"注意：{len(failed)} 个车次（{failed_names}）本次未刷新到数据，"
                "已保留上次价格，可稍后重试。"
            )
        lines.append(format_quick(locks, self.provider.call_info()))
        yield event.plain_result("\n".join(lines))

    @train_group.command("我的")
    async def cmd_mine(self, event: AstrMessageEvent):
        sender = str(event.get_sender_id())
        self.storage.remove_departed(sender)
        locks = self.storage.locks_by_user(sender)
        yield event.plain_result(format_locks(locks))

    @train_group.command("数据源")
    async def cmd_data_source(self, event: AstrMessageEvent):
        tokens = self._tokens(event)
        arg = (tokens[2] if len(tokens) > 2 else "").strip().lower()
        if not arg:
            yield event.plain_result(
                f"当前数据源：{self.provider.name}（{self.provider.display_name}）\n"
                "用法：/火车票 数据源 <juhe|apihz|demo>\n"
                "juhe=聚合数据（每日限量）｜apihz=接口盒子（免费无上限）｜demo=演示数据\n"
                "切换后全局生效并保存，重启后仍有效。"
            )
            return
        if arg not in KNOWN_SOURCES:
            yield event.plain_result(
                f"未知数据源：{arg}。可用值：juhe / apihz / demo。"
            )
            return
        if arg == self.provider.name:
            yield event.plain_result(
                f"当前已在使用 {arg}（{self.provider.display_name}）数据源。"
            )
            return
        try:
            self._switch_provider(arg)
        except Exception as e:
            yield event.plain_result(f"切换数据源失败：{e}")
            return
        tip = ""
        if arg == "apihz":
            tip = "\n注意：接口盒子返回的是未打折公示票价，一切价格以购票处实际为准。"
        yield event.plain_result(
            f"已切换数据源为 {arg}（{self.provider.display_name}）。{tip}"
        )

    @filter.regex(r".+")
    async def on_plain_reply(self, event: AstrMessageEvent):
        # 多轮交互：捕获普通消息并消费进行中的查询会话；无会话或命令消息则放行
        text = (getattr(event, "message_str", "") or "").strip()
        if not text or text.startswith(self.command_prefix):
            return
        sender = str(event.get_sender_id())
        session = self.sessions.get(sender, event.unified_msg_origin)
        if session is None:
            return
        if text in ("取消", "取消查询", "退出"):
            self.sessions.clear(sender, event.unified_msg_origin)
            yield event.plain_result("已取消本次查询。")
            return
        reply = await self.flow.handle_reply(session, text)
        if reply is not None:
            yield event.plain_result(reply)
