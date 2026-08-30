"""接口盒子（apihz.cn）12306 火车票数据源。

接口文档：
- 余票信息查询 https://www.apihz.cn/api/12306api.html
      GET/POST https://cn.apihz.cn/api/12306/api.php
      参数：id/key/add/end/y/m/d（日期拆成年月日）
      返回 datas[]，每条含 train_number/train_order/depart_index/arrive_index/
      depart_name/arrive_name/depart_time/arrive_time/duration/seatcode/date/seats[]，
      seats[].type 为席别（如“二等座(二等包座)”），seats[].stock 为余票
      （-1=有票充足，0=无票，>0=余票数量）。
- 公示票价查询 https://www.apihz.cn/api/12306api4.html
      GET/POST https://cn.apihz.cn/api/12306/api4.php
      参数同上；返回 datas[]，每条含 train_order（车次号）、
      edz/ydz/tdz/yz/yw/rw 等席别票价（0.00 表示无该席别）。
- 余票票价查询 https://www.apihz.cn/api/12306api2.html
      GET/POST https://cn.apihz.cn/api/12306/api2.php
      需先调用余票接口，按车次逐个查票价（train_order/depart_index/arrive_index/
      seatcode/y/m/d）；可选启用，用于补查“公示票价缺失但有余票”的席别（如无座）。

一次 search() 会调用余票接口 1 次 + 公示票价接口 1 次（票价接口失败时降级，
仅返回余票信息、票价留空）；启用余票票价补查时，对公示票价缺失但有余票的
车次逐车次追加调用（默认开启，每次查询最多补查 8 个车次，触发接口频次限制
后自动停止；无座票价仍缺失时按硬座/二等座票价兜底）。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
from typing import Dict, List, Optional

from .juhe_provider import (
    PLACEHOLDER_KEYS,
    classify_train_type,
    _pick_seat,
    fill_wz_price_fallback,
)
from .models import SeatPrice, Train
from .providers import ProviderError, TicketPriceProvider, register_provider

logger = logging.getLogger("train_apihz")

DEFAULT_AVAIL_URL = "https://cn.apihz.cn/api/12306/api.php"
DEFAULT_PRICE_URL = "https://cn.apihz.cn/api/12306/api4.php"
DEFAULT_DETAIL_PRICE_URL = "https://cn.apihz.cn/api/12306/api2.php"
DEFAULT_MAX_AHEAD_DAYS = 15
TIMEOUT_SECONDS = 20
# 余票票价接口（api2）对免费用户限频（约 10 次/分钟），
# 每次查询最多补查的车次数，避免一次大查询瞬间打满频次。
DEFAULT_DETAIL_PRICE_MAX_CALLS = 8
DETAIL_PRICE_CALL_DELAY_SECONDS = 0.5
RATE_LIMIT_MARKERS = ("调用频次过快", "频次限制")
# 余票接口瞬时失败（如平台返回“失败，请重试!”）时自动重试一次，
# 仍失败则抛出错误，由交互流程提示用户回复「重试」再次请求。
AVAIL_RETRY_ATTEMPTS = 2
AVAIL_RETRY_DELAY_SECONDS = 1.0

# 余票接口席别名 -> 公示票价接口字段名
SEAT_FIELD_MAP = {
    "商务座(特等座)": "tdz",
    "一等座": "ydz",
    "二等座(二等包座)": "edz",
    "高级软卧": "rws",
    "软卧(动卧一等卧)": "rw",
    "硬卧(二等卧)": "yw",
    "软座": "rz",
    "硬座": "yz",
    "无座": "wz",
    "优选一等座": "ydzs",
}

# 展示时优先按该顺序排列席别（仅用于无法识别类型的兜底分支）
DISPLAY_SEAT_ORDER = [
    "商务座",
    "一等座",
    "二等座",
    "高级软卧",
    "软卧",
    "硬卧",
    "软座",
    "硬座",
    "无座",
    "优选一等座",
]

# 各车次类型真实存在的席别白名单，元素为 (展示名, 余票接口可能的原名, 票价字段)。
# 展示规则：名单内席别有余票或已知票价才展示；名单外席别仅在接口明确报出
# 余票时才展示（兜底防止漏掉真实余票）。这样余票接口对不存在席别返回的
# stock=0“幽灵席别”（显示为 0 元、无票）会被全部过滤。
# D 字头卧铺席别按动车习惯展示为 二等卧/一等卧（接口原名 硬卧/软卧）。
TRAIN_TYPE_SEATS = {
    "G": (
        ("二等座", ("二等座",), "edz"),
        ("一等座", ("一等座",), "ydz"),
        ("商务座", ("商务座",), "tdz"),
        ("无座", ("无座",), "wz"),
    ),
    "D": (
        ("二等座", ("二等座",), "edz"),
        ("二等卧", ("二等卧", "硬卧"), "yw"),
        ("一等卧", ("一等卧", "软卧"), "rw"),
        ("无座", ("无座",), "wz"),
    ),
    "Z": (
        ("硬座", ("硬座",), "yz"),
        ("硬卧", ("硬卧", "二等卧"), "yw"),
        ("软卧", ("软卧", "一等卧"), "rw"),
        ("无座", ("无座",), "wz"),
    ),
}
TRAIN_TYPE_SEATS["T"] = TRAIN_TYPE_SEATS["K"] = TRAIN_TYPE_SEATS["Z"]


def _stock_field_pairs(spec, seat_stock: Dict[str, str]):
    """产出 (席别展示名, 票价字段) 对，用于判断是否需要 api2 补查。

    spec 为空（未识别车次类型）时按余票接口实际返回的席别名给出。
    """
    if spec:
        for _display, sources, field in spec:
            present = next((src for src in sources if src in seat_stock), None)
            if present is not None:
                yield present, field
    else:
        for name in seat_stock:
            yield name, SEAT_FIELD_MAP.get(name, "")


def _normalize_seat_name(raw: str) -> str:
    """将余票接口的席别名规整为展示名：二等座(二等包座) -> 二等座。"""
    name = (raw or "").strip()
    if not name:
        return ""
    if "(" in name:
        name = name.split("(", 1)[0].strip()
    if "（" in name:
        name = name.split("（", 1)[0].strip()
    return name


def _stock_to_text(stock) -> str:
    """余票数值转显示文本：-1=有（充足），0/空=无，>0=数量。"""
    try:
        v = int(stock)
    except (TypeError, ValueError):
        return "无"
    if v < 0:
        return "有"
    if v == 0:
        return "无"
    return str(v)


def _price_value(raw) -> float:
    """票价字段解析：空/null/非数字按 0 处理。"""
    if raw is None:
        return 0.0
    try:
        return float(str(raw).replace("￥", "").replace("¥", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _normalize_time(raw) -> str:
    """时刻规整：08:32 -> 08:32；纯数字不足 4 位补零。"""
    t = (raw or "").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", t)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    m = re.match(r"^(\d{1,2})(\d{2})$", t)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return t or "--:--"


def _duration_text(depart_time: str, arrive_time: str, raw_duration: str = "") -> str:
    """优先使用接口返回的历时，其次按起止时刻推算。"""
    raw = (raw_duration or "").strip()
    if raw:
        if re.match(r"^\d{1,2}:\d{2}$", raw):
            return raw
        if re.match(r"^\d{1,3}:\d{2}$", raw):
            return raw
    try:
        h1, m1 = (int(x) for x in depart_time.split(":")[:2])
        h2, m2 = (int(x) for x in arrive_time.split(":")[:2])
        mins = (h2 * 60 + m2 - h1 * 60 - m1) % (24 * 60)
        return f"{mins // 60:02d}:{mins % 60:02d}"
    except (TypeError, ValueError):
        return ""


@register_provider
class ApiHzTrainProvider(TicketPriceProvider):
    """接口盒子 12306 余票 + 公示票价数据源。"""

    name = "apihz"
    display_name = "接口盒子(apihz.cn)"

    def __init__(self, config: Optional[dict] = None, plugin_dir: str = ""):
        self.config = config or {}
        self.app_id = (self.config.get("apihz_id") or "").strip()
        self.app_key = (self.config.get("apihz_key") or "").strip()
        self.avail_url = (
            self.config.get("apihz_api_url") or DEFAULT_AVAIL_URL
        ).strip()
        self.price_url = (
            self.config.get("apihz_price_url") or DEFAULT_PRICE_URL
        ).strip()
        self.detail_price_url = (
            self.config.get("apihz_detail_price_url") or DEFAULT_DETAIL_PRICE_URL
        ).strip()
        # 默认开启余票票价补查：公示票价接口（api4）不返回“无座”票价，
        # 有余票的席别需按车次调用余票票价接口（api2）补查，避免 0 元误导。
        self.use_detail_price = bool(
            self.config.get("apihz_use_detail_price", True)
        )
        try:
            self.detail_price_max_calls = int(
                self.config.get(
                    "apihz_detail_price_max_calls",
                    DEFAULT_DETAIL_PRICE_MAX_CALLS,
                )
                or DEFAULT_DETAIL_PRICE_MAX_CALLS
            )
        except (TypeError, ValueError):
            self.detail_price_max_calls = DEFAULT_DETAIL_PRICE_MAX_CALLS
        self.cookie = (self.config.get("apihz_cookie") or "").strip()
        try:
            self.max_ahead_days = int(
                self.config.get("apihz_max_days_ahead", DEFAULT_MAX_AHEAD_DAYS)
                or DEFAULT_MAX_AHEAD_DAYS
            )
        except (TypeError, ValueError):
            self.max_ahead_days = DEFAULT_MAX_AHEAD_DAYS

    def _check_credentials(self) -> None:
        if (
            not self.app_id
            or self.app_id.strip().lower() in PLACEHOLDER_KEYS
            or not self.app_key
            or self.app_key.strip().lower() in PLACEHOLDER_KEYS
        ):
            raise ProviderError(
                "未配置接口盒子开发者ID/Key：默认值 xxxxxxx 为占位符，"
                "请在 AstrBot 管理面板的插件配置中填写 apihz_id 与 apihz_key。"
            )

    def _base_params(self, depart: str, arrive: str, date: dt.date) -> Dict[str, str]:
        return {
            "id": self.app_id,
            "key": self.app_key,
            "add": depart,
            "end": arrive,
            "y": str(date.year),
            "m": str(date.month),
            "d": str(date.day),
        }

    def _get_json(self, url: str, params: Dict[str, str]) -> dict:
        """同步 HTTP 请求（requests），在事件循环外通过线程池调用。"""
        import requests

        if self.cookie:
            params = dict(params)
            params["ck"] = self.cookie
        try:
            resp = requests.post(url, data=params, timeout=TIMEOUT_SECONDS)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            raise ProviderError(f"接口盒子接口请求失败：{e}") from e

    def _fetch_availability(
        self, depart: str, arrive: str, date: dt.date
    ) -> List[dict]:
        params = self._base_params(depart, arrive, date)
        last_error = "未知错误"
        for attempt in range(AVAIL_RETRY_ATTEMPTS):
            try:
                data = self._get_json(self.avail_url, params)
            except Exception as e:
                last_error = str(e)
                if attempt + 1 < AVAIL_RETRY_ATTEMPTS:
                    time.sleep(AVAIL_RETRY_DELAY_SECONDS)
                    continue
                raise
            if int(data.get("code", 400) or 400) == 200:
                return list(data.get("datas") or [])
            msg = str(data.get("msg") or "未知错误")
            last_error = msg
            if attempt + 1 < AVAIL_RETRY_ATTEMPTS and (
                "重试" in msg or "失败" in msg
            ):
                time.sleep(AVAIL_RETRY_DELAY_SECONDS)
                continue
            break
        raise ProviderError(f"接口盒子余票接口返回错误：{last_error}")

    def _fetch_prices(
        self, depart: str, arrive: str, date: dt.date
    ) -> Dict[str, dict]:
        """公示票价：train_order（车次号）-> 票价字段字典。失败时返回空字典。"""
        try:
            params = self._base_params(depart, arrive, date)
            data = self._get_json(self.price_url, params)
        except Exception as e:
            logger.warning(f"公示票价接口查询失败，票价留空继续：{e}")
            return {}
        if int(data.get("code", 400) or 400) != 200:
            logger.warning(
                f"公示票价接口返回错误，票价留空继续：{data.get('msg') or '未知错误'}"
            )
            return {}
        return {str(x.get("train_order") or ""): x for x in data.get("datas") or []}

    def _fetch_detail_price(
        self,
        train_order: str,
        depart_index: str,
        arrive_index: str,
        seatcode: str,
        date: dt.date,
    ) -> tuple:
        """按车次查询余票票价（api2）。

        返回 (数据字典, 错误信息)；成功时错误信息为空串，失败时数据为空字典。
        """
        params = {
            "id": self.app_id,
            "key": self.app_key,
            "train_order": train_order,
            "depart_index": depart_index,
            "arrive_index": arrive_index,
            "seatcode": seatcode,
            "y": str(date.year),
            "m": str(date.month),
            "d": str(date.day),
        }
        try:
            data = self._get_json(self.detail_price_url, params)
        except Exception as e:
            return {}, str(e)
        if int(data.get("code", 400) or 400) != 200:
            return {}, str(data.get("msg") or "未知错误")
        return data, ""

    def _price_for_seat(
        self, seat_name: str, price_row: dict, detail_row: Optional[dict]
    ) -> float:
        """优先使用公示票价；公示票价缺失或为 0 时，有余票票价则采用。"""
        field = next(
            (
                f
                for raw, f in SEAT_FIELD_MAP.items()
                if _normalize_seat_name(raw) == seat_name
            ),
            None,
        )
        return self._field_price(field, price_row, detail_row)

    @staticmethod
    def _field_price(field: Optional[str], price_row: dict, detail_row: Optional[dict]) -> float:
        """按公示票价字段取价；缺失或为 0 时回退余票票价（api2）字段。"""
        if not field:
            return 0.0
        price = _price_value(price_row.get(field))
        if price <= 0 and detail_row is not None:
            price = _price_value(detail_row.get(field))
        return price

    async def _build_seats(
        self,
        item: dict,
        price_row: dict,
        date: dt.date,
        train_type: str,
        detail_ctx: dict,
    ) -> List[SeatPrice]:
        """合并余票与票价，生成席别列表。

        规则：
        - 仅展示该车次类型真实存在的席别（G=二等座/一等座/商务座/无座，
          D=二等座/二等卧/一等卧/无座，K/Z/T=硬座/硬卧/软卧/无座），
          余票接口对不存在席别返回的 stock=0 幽灵席别一律过滤；
        - 名单内席别有余票、或公示/补查票价已知（含已售罄）时展示；
          无余票且票价未知（会显示成 0 元）的不展示；
        - 名单外席别仅当接口明确报出余票时才展示，避免漏掉真实余票。
        """
        seat_stock: Dict[str, str] = {}
        for s in item.get("seats") or []:
            if not isinstance(s, dict):
                continue
            name = _normalize_seat_name(str(s.get("type") or ""))
            if name:
                seat_stock[name] = _stock_to_text(s.get("stock"))

        spec = TRAIN_TYPE_SEATS.get(train_type.upper())
        if spec:
            fields = [field for _, _, field in spec]
        else:
            fields = list(SEAT_FIELD_MAP.values())
        published = {field: _price_value(price_row.get(field)) for field in fields}

        detail_row: Optional[dict] = None
        # 任一席别有余票但公示票价缺失时，需要调用 api2 补查票价
        need_detail = any(
            seat_stock.get(name, "无") != "无" and published.get(field, 0) <= 0
            for name, field in _stock_field_pairs(spec, seat_stock)
        )
        if (
            self.use_detail_price
            and need_detail
            and detail_ctx["remaining"] > 0
            and not detail_ctx["rate_limited"]
        ):
            if detail_ctx["made_calls"] > 0:
                await asyncio.sleep(DETAIL_PRICE_CALL_DELAY_SECONDS)
            data, err_msg = await asyncio.to_thread(
                self._fetch_detail_price,
                str(item.get("train_order") or ""),
                str(item.get("depart_index") or ""),
                str(item.get("arrive_index") or ""),
                str(item.get("seatcode") or ""),
                date,
            )
            detail_ctx["made_calls"] += 1
            detail_ctx["remaining"] -= 1
            if data:
                detail_row = data
                self.record_call()
            elif any(marker in err_msg for marker in RATE_LIMIT_MARKERS):
                # 触发频次限制后停止本次查询的补查，避免继续刷频次与日志
                detail_ctx["rate_limited"] = True
                if not detail_ctx["rate_limit_logged"]:
                    logger.warning(
                        f"余票票价接口调用频次受限，本次查询停止补查：{err_msg}"
                    )
                    detail_ctx["rate_limit_logged"] = True
            else:
                logger.warning(
                    f"余票票价查询失败（{item.get('train_order') or ''}）：{err_msg}"
                )

        seats: List[SeatPrice] = []
        covered: set = set()
        if spec:
            for display, sources, field in spec:
                covered.update(sources)
                covered.add(display)
                values = [seat_stock[src] for src in sources if src in seat_stock]
                in_stock = [v for v in values if v != "无"]
                stock_txt = in_stock[0] if in_stock else (values[0] if values else "无")
                price = self._field_price(field, price_row, detail_row)
                if stock_txt != "无" or price > 0:
                    seats.append(SeatPrice(seat_name=display, price=price, num=stock_txt))
        else:
            # 未识别的车次类型：按接口实际返回展示，但仍过滤无票且无价的幽灵席别
            published_names = {
                _normalize_seat_name(raw)
                for raw, field in SEAT_FIELD_MAP.items()
                if _price_value(price_row.get(field)) > 0
            }
            names = [
                n for n in DISPLAY_SEAT_ORDER
                if n in seat_stock or n in published_names
            ]
            names += [n for n in seat_stock if n not in names]
            for name in names:
                stock_txt = seat_stock.get(name, "无")
                price = self._price_for_seat(name, price_row, detail_row)
                if stock_txt != "无" or price > 0:
                    covered.add(name)
                    seats.append(SeatPrice(seat_name=name, price=price, num=stock_txt))
        # 白名单之外但接口明确报出余票的席别仍展示，避免漏掉真实余票
        for name, stock_txt in seat_stock.items():
            if name in covered or stock_txt == "无":
                continue
            seats.append(
                SeatPrice(
                    seat_name=name,
                    price=self._price_for_seat(name, price_row, detail_row),
                    num=stock_txt,
                )
            )
        # 无座有余票但票价仍为 0（api4 无 wz 字段、api2 有时返回空）时，
        # 按硬座/二等座票价兜底，避免“有余票却显示 0 元”。
        fill_wz_price_fallback(seats)
        return seats

    async def search(
        self,
        depart: str,
        arrive: str,
        date: dt.date,
        train_types: Optional[List[str]],
    ) -> List[Train]:
        self._check_credentials()
        today = dt.date.today()
        if date < today:
            raise ProviderError("不能查询过去的日期。")
        if date > today + dt.timedelta(days=self.max_ahead_days):
            raise ProviderError(
                f"接口盒子接口仅支持查询未来 {self.max_ahead_days} 天内的车次。"
            )
        depart_text = (depart or "").strip()
        arrive_text = (arrive or "").strip()
        if not depart_text or not arrive_text:
            raise ProviderError("出发站/到达站不能为空。")

        raw_trains = await asyncio.to_thread(
            self._fetch_availability, depart_text, arrive_text, date
        )
        self.record_call()
        price_map = await asyncio.to_thread(
            self._fetch_prices, depart_text, arrive_text, date
        )
        if price_map:
            self.record_call()

        # 本次查询内共享的 api2 补查限额/限流状态
        detail_ctx = {
            "remaining": self.detail_price_max_calls,
            "made_calls": 0,
            "rate_limited": False,
            "rate_limit_logged": False,
        }
        wanted_types = set(train_types) if train_types else None
        trains: List[Train] = []
        for item in raw_trains:
            train_no = str(item.get("train_number") or item.get("train_order") or "")
            train_no = re.sub(r"^\d+", "", train_no).strip().upper()
            if not train_no:
                continue
            ttype = classify_train_type(train_no)
            if wanted_types is not None and ttype not in wanted_types:
                continue

            price_row = price_map.get(train_no) or {}
            prices = await self._build_seats(
                item, price_row, date, ttype, detail_ctx
            )

            depart_time = _normalize_time(str(item.get("depart_time") or ""))
            arrive_time = _normalize_time(str(item.get("arrive_time") or ""))
            picked = _pick_seat(prices)
            trains.append(
                Train(
                    train_type=ttype,
                    train_no=train_no,
                    depart_station=str(
                        item.get("depart_name") or depart_text
                    ),
                    arrive_station=str(item.get("arrive_name") or arrive_text),
                    # 统一使用查询日期作为出发日期：接口返回的 datas.date 对部分
                    # 车次（如凌晨发车的 D114）会错位成前一天，导致锁定/提醒日期错误。
                    depart_date=date.isoformat(),
                    depart_time=depart_time,
                    arrive_time=arrive_time,
                    duration=_duration_text(
                        depart_time,
                        arrive_time,
                        str(item.get("duration") or ""),
                    ),
                    seat_name=picked.seat_name,
                    price=picked.price,
                    num=picked.num,
                    prices=prices,
                    source_note="接口盒子12306余票+公示票价API",
                )
            )
        trains.sort(key=lambda t: (t.depart_time, t.train_no))
        return trains
