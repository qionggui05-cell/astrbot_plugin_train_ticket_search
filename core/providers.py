"""数据源适配器抽象与注册表。"""

from __future__ import annotations

import abc
import datetime as dt
import logging
from typing import Dict, List, Optional, Type

from .models import Train


class ProviderError(Exception):
    """数据源返回的、可直接展示给用户的错误（如 API Key 无效、次数超限）。"""


class TicketPriceProvider(abc.ABC):
    """火车票价格数据源接口。

    新增真实数据源时：继承本类并实现 search，用 @register_provider 注册，
    然后在插件配置 data_source 中切换即可，无需改动命令与提醒逻辑。
    """

    name: str = "base"
    display_name: str = ""

    def __init__(
        self,
        config: Optional[dict] = None,
        plugin_dir: str = "",
        logger: Optional[logging.Logger] = None,
    ):
        # AstrBot 运行时由 main.py 注入插件专用 logger；独立测试/无 AstrBot 环境
        # 退化为标准 logging，保证核心模块可单独运行。
        self.logger = logger or logging.getLogger("astrbot_plugin_train_ticket_search")
        self.calls_today: int = 0  # 今日已调用 API 次数（成功请求）
        self.last_api_call_at: Optional[dt.datetime] = None  # 最近一次调用时间

    def record_call(self) -> None:
        """记录一次 API 调用；跨天自动重置今日计数。"""
        if not hasattr(self, "last_api_call_at"):
            self.last_api_call_at = None
        if not hasattr(self, "calls_today"):
            self.calls_today = 0
        now = dt.datetime.now().astimezone()
        if self.last_api_call_at is not None and (
            self.last_api_call_at.date() != now.date()
        ):
            self.calls_today = 0
        self.calls_today += 1
        self.last_api_call_at = now

    def call_info(self) -> Dict[str, object]:
        """供输出展示的调用统计信息。"""
        return {
            "source_name": self.display_name or self.name,
            "calls_today": int(self.calls_today or 0),
            "last_api_call_at": self.last_api_call_at,
        }

    @abc.abstractmethod
    async def search(
        self,
        depart: str,
        arrive: str,
        date: dt.date,
        train_types: Optional[List[str]],
    ) -> List[Train]:
        """查询指定出发站/到达站、日期、车次类型的车次列表。

        depart/arrive 为站点名称（如 北京南）或三字站点编码（如 VNP）。
        train_types 为车次类型字母列表（G/D/Z/T/K/O）；None 表示全部类型。
        无数据时返回空列表。
        """
        raise NotImplementedError


_PROVIDER_REGISTRY: Dict[str, Type[TicketPriceProvider]] = {}


def register_provider(cls: Type[TicketPriceProvider]) -> Type[TicketPriceProvider]:
    name = getattr(cls, "name", None)
    if not name:
        raise ValueError("数据源类必须定义 name 属性")
    _PROVIDER_REGISTRY[name] = cls
    return cls


def create_provider(
    name: str,
    config: dict,
    plugin_dir: str,
    logger: Optional[logging.Logger] = None,
) -> TicketPriceProvider:
    cls = _PROVIDER_REGISTRY.get(name)
    if cls is None:
        available = ", ".join(sorted(_PROVIDER_REGISTRY))
        raise ValueError(f"未知数据源 {name!r}，可用数据源：{available}")
    return cls(config=config or {}, plugin_dir=plugin_dir, logger=logger)
