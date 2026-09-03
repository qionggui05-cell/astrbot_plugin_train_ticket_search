"""聚合数据「火车订票查询」数据源。

接口文档（https://www.juhe.cn/docs/api/id/817）：

    GET https://apis.juhe.cn/fapigw/train/query
        key                 必填  申请的 API Key
        search_type         必填  1=按站点名称，2=按站点编码
        departure_station   必填  出发站，如 北京南 / VNP
        arrival_station     必填  到达站，如 苏州北 / OHH
        date                必填  出发日期 YYYY-MM-DD（仅限 15 天内）
        enable_booking      可选  1=仅返回可预定班次，2=全部（含已售罄，默认 2）
        filter              可选  车次筛选（G/D/Z/T/K/O/F/S）
        departure_time_range 可选 凌晨/上午/下午/晚上

返回：error_code / reason / result[]，result 中每个元素为一个车次，包含
train_no、departure_station、arrival_station、departure_time、arrival_time、
duration、enable_booking、prices[]（seat_name/price/num）等字段。
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Dict, List, Optional

import httpx

from .models import SeatPrice, Train
from .parsing import TRAIN_TYPE_NAMES
from .providers import ProviderError, TicketPriceProvider, register_provider

DEFAULT_API_URL = "https://apis.juhe.cn/fapigw/train/query"
DEFAULT_MAX_AHEAD_DAYS = 15
TIMEOUT_SECONDS = 20

# 跟踪席别优先级：优先二等座，其次一等座……最后取最低价席别
PREFERRED_SEATS = [
    "二等座",
    "一等座",
    "商务座",
    "优选一等座",
    "特等座",
    "硬座",
    "软座",
    "硬卧",
    "软卧",
    "高级软卧",
    "无座",
]

_STATION_CODE_RE = re.compile(r"^[A-Z]{3}$")

# 未填写时的占位符/提示值，一律视为“未配置”
PLACEHOLDER_KEYS = {
    "xxxxxxx",
    "xxxxx",
    "yourkey",
    "your-key",
    "your_api_key",
    "你的key",
    "请填写",
}


def looks_like_station_code(text: str) -> bool:
    return bool(_STATION_CODE_RE.match((text or "").strip().upper()))


def classify_train_type(train_no: str) -> str:
    """根据车次号首字母归类车次类型，无法识别时归为 O（其他）。"""
    head = (train_no or "")[:1].upper()
    if head in TRAIN_TYPE_NAMES:
        return head
    return "O"


def _parse_seat_prices(raw: Optional[list]) -> List[SeatPrice]:
    seats: List[SeatPrice] = []
    for p in raw or []:
        try:
            name = str(p.get("seat_name") or "")
            price = float(p.get("price") or 0)
            num = str(p.get("num") or "")
        except (TypeError, ValueError):
            continue
        if not name:
            continue
        # 保留全部席别（含票价为 0 的已售罄席别），保证无票车次可显示、可提醒
        if price > 0 or num:
            seats.append(SeatPrice(seat_name=name, price=price, num=num))
    fill_wz_price_fallback(seats)
    return seats


def _pick_seat(seats: List[SeatPrice]) -> SeatPrice:
    if not seats:
        return SeatPrice()
    # 优先取票价已知的常用席别，避免跟踪席别落在 0 元占位价上
    for pref in PREFERRED_SEATS:
        for s in seats:
            if s.seat_name == pref and s.price > 0:
                return s
    for pref in PREFERRED_SEATS:
        for s in seats:
            if s.seat_name == pref:
                return s
    priced = [s for s in seats if s.price > 0]
    if priced:
        return min(priced, key=lambda s: s.price)
    return seats[0]


def fill_wz_price_fallback(seats: List[SeatPrice]) -> None:
    """无座有余票但票价为 0 时，按铁路票价规则兜底补价。

    无座票价按硬座票价执行；高铁/动车无硬座时按二等座票价执行。
    接口（聚合数据/接口盒子）有时对无座返回 0 或空，导致有余票却显示 0 元，
    这里用同车次硬座/二等座票价补齐，避免 0 元误导。
    """
    wz = next((s for s in seats if s.seat_name == "无座"), None)
    if wz is None or wz.price > 0 or wz.num in ("", "无"):
        return
    for fallback_name in ("硬座", "二等座"):
        ref = next(
            (s for s in seats if s.seat_name == fallback_name and s.price > 0),
            None,
        )
        if ref is not None:
            wz.price = ref.price
            return


@register_provider
class JuheTrainProvider(TicketPriceProvider):
    """聚合数据火车票查询数据源。

    每次 search() 消耗一次聚合数据接口调用额度（每日有限），因此只在
    查票 / 定时刷新 / 强制更新时调用；快查（获取价格）不经过本数据源。
    """

    name = "juhe"
    display_name = "聚合数据(juhe.cn)"

    def __init__(
        self,
        config: Optional[dict] = None,
        plugin_dir: str = "",
        logger: Optional[logging.Logger] = None,
    ):
        super().__init__(config=config, plugin_dir=plugin_dir, logger=logger)
        self.config = config or {}
        self.appkey = (self.config.get("juhe_appkey") or "").strip()
        self.api_url = (self.config.get("juhe_api_url") or DEFAULT_API_URL).strip()
        try:
            self.max_ahead_days = int(
                self.config.get("juhe_max_days_ahead", DEFAULT_MAX_AHEAD_DAYS)
                or DEFAULT_MAX_AHEAD_DAYS
            )
        except (TypeError, ValueError):
            self.max_ahead_days = DEFAULT_MAX_AHEAD_DAYS

    async def _get_json(self, params: Dict[str, str]) -> dict:
        """异步 HTTP 请求（httpx）。"""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                resp = await client.get(self.api_url, params=params)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPError as e:
            raise ProviderError(f"聚合数据接口请求失败：{e}") from e

    async def search(
        self,
        depart: str,
        arrive: str,
        date: dt.date,
        train_types: Optional[List[str]],
    ) -> List[Train]:
        if not self.appkey or self.appkey.strip().lower() in PLACEHOLDER_KEYS:
            raise ProviderError(
                "未配置聚合数据 API Key：默认值 xxxxxxx 为占位符，"
                "请在 AstrBot 管理面板的插件配置中填写你自己申请的 juhe_appkey。"
            )
        today = dt.date.today()
        if date < today:
            raise ProviderError("不能查询过去的日期。")
        if date > today + dt.timedelta(days=self.max_ahead_days):
            raise ProviderError(
                f"聚合数据接口仅支持查询未来 {self.max_ahead_days} 天内的车次。"
            )
        depart_text = (depart or "").strip()
        arrive_text = (arrive or "").strip()
        if not depart_text or not arrive_text:
            raise ProviderError("出发站/到达站不能为空。")

        both_codes = looks_like_station_code(depart_text) and looks_like_station_code(
            arrive_text
        )
        params: Dict[str, str] = {
            "key": self.appkey,
            "search_type": "2" if both_codes else "1",
            "departure_station": depart_text,
            "arrival_station": arrive_text,
            "date": date.isoformat(),
            # 2=全部班次（含已售罄），便于列表展示并监控余票回升
            "enable_booking": "2",
        }

        data = await self._get_json(params)
        self.record_call()
        error_code = data.get("error_code")
        if error_code not in (0, None):
            reason = data.get("reason") or "未知错误"
            if error_code in (10012, 10013):
                reason = "今日调用次数已达上限，请明天再试或减少刷新频率"
            raise ProviderError(f"聚合数据接口返回错误（{error_code}）：{reason}")

        wanted_types = set(train_types) if train_types else None
        trains: List[Train] = []
        for item in data.get("result") or []:
            train_no = str(item.get("train_no") or "")
            if not train_no:
                continue
            ttype = classify_train_type(train_no)
            if wanted_types is not None and ttype not in wanted_types:
                continue
            seats = _parse_seat_prices(item.get("prices") or [])
            picked = _pick_seat(seats)
            trains.append(
                Train(
                    train_type=ttype,
                    train_no=train_no,
                    depart_station=str(item.get("departure_station") or depart_text),
                    arrive_station=str(item.get("arrival_station") or arrive_text),
                    depart_date=date.isoformat(),
                    depart_time=str(item.get("departure_time") or "--:--"),
                    arrive_time=str(item.get("arrival_time") or "--:--"),
                    duration=str(item.get("duration") or ""),
                    seat_name=picked.seat_name,
                    price=picked.price,
                    num=picked.num,
                    prices=seats,
                    source_note="聚合数据火车票查询API",
                )
            )
        trains.sort(key=lambda t: (t.depart_time, t.train_no))
        return trains
