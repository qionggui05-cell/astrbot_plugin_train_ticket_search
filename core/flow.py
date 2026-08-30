"""交互查询流程核心逻辑（不依赖 AstrBot，便于单元测试）。"""

from __future__ import annotations

import datetime as dt
from typing import Awaitable, Callable, List, Optional

from .formatting import format_query_results
from .models import Train
from .parsing import parse_date, resolve_station
from .providers import ProviderError
from .session import (
    STEP_ARRIVE,
    STEP_DATE,
    STEP_DEPART,
    STEP_DONE,
    STEP_RETRY,
    QuerySession,
)

SearchFunc = Callable[[str, str, dt.date, Optional[List[str]]], Awaitable[List[Train]]]
CallInfoGetter = Callable[[], Optional[dict]]

# 查询失败后的重试指令（任意一个即可按上次输入重新请求）
RETRY_KEYWORDS = ("重试", "重试查询", "重新查询", "再试一次", "继续查询")


class FlowEngine:
    """驱动会话状态机：日期 → 出发站 → 到达站 → 查询结果。"""

    def __init__(
        self,
        search: SearchFunc,
        call_info_getter: Optional[CallInfoGetter] = None,
    ):
        self.search = search
        self.call_info_getter = call_info_getter

    async def _execute_search(self, session: QuerySession) -> str:
        """按会话中已记录的输入执行查询；失败时给出错误与重试指引。"""
        try:
            trains = await self.search(
                session.depart_station,
                session.arrive_station,
                session.date,
                session.train_types,
            )
        except ProviderError as e:
            session.step = STEP_RETRY
            return self._retry_hint(session, str(e))
        except Exception:
            session.step = STEP_RETRY
            return self._retry_hint(session, "查询失败，请稍后重试。")
        session.last_query = trains
        session.step = STEP_DONE
        call_info = (
            self.call_info_getter()
            if self.call_info_getter is not None
            else None
        )
        return format_query_results(
            trains, session.date.isoformat(), call_info=call_info
        )

    @staticmethod
    def _retry_hint(session: QuerySession, reason: str) -> str:
        """组装错误信息 + 重试指令（说明将按用户已输入的信息再次请求）。"""
        scope = (
            f"{session.date.isoformat()} "
            f"{session.depart_station} → {session.arrive_station}"
        )
        return (
            f"{reason}\n"
            f"请回复「重试」按刚才的输入重新请求（{scope}），"
            "或直接输入新的到达站；回复「取消」可退出本次查询。"
        )

    async def handle_reply(self, session: QuerySession, text: str) -> Optional[str]:
        """处理用户的一轮回复，返回需要发送的文本；None 表示无需回复。"""
        if session.step == STEP_DATE:
            d, err = parse_date(text)
            if err:
                return err
            session.date = d
            session.step = STEP_DEPART
            return "请输入出发站（如：北京南，也可输入站点编码如 VNP）："

        if session.step == STEP_DEPART:
            station, err = resolve_station(text)
            if err:
                return err
            session.depart_station = station
            session.step = STEP_ARRIVE
            return "请输入到达站（如：苏州北）："

        if session.step == STEP_ARRIVE:
            station, err = resolve_station(text)
            if err:
                return err
            if station.upper() == session.depart_station.upper():
                return "出发站与到达站相同，请重新输入到达站（如：苏州北）："
            session.arrive_station = station
            return await self._execute_search(session)

        if session.step == STEP_RETRY:
            # 回复重试指令：按上次输入原样再次请求
            if text.strip() in RETRY_KEYWORDS:
                return await self._execute_search(session)
            # 其他输入视为重新输入到达站
            station, err = resolve_station(text)
            if err:
                return err
            if station.upper() == session.depart_station.upper():
                return "出发站与到达站相同，请重新输入到达站（如：苏州北）："
            session.arrive_station = station
            return await self._execute_search(session)

        # STEP_DONE：查询已完成，不消费普通消息
        return None
