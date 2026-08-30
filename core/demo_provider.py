"""演示数据源：确定性生成演示车次与价格，支持 JSON 文件覆盖。"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Dict, List, Optional

from .models import SeatPrice, Train
from .parsing import TRAIN_TYPE_NAMES
from .providers import TicketPriceProvider, register_provider

DEFAULT_TRAINS_PER_TYPE = 2


@dataclass
class TrainFixture:
    """测试车次夹具：按（日期, 车次号）精确匹配，可选绑定站点。"""

    date: str
    train_type: str
    train_no: str
    depart_time: str
    arrive_time: str
    duration: str
    price: float
    seat_name: str = "二等座"
    depart: str = ""
    arrive: str = ""


@register_provider
class DemoProvider(TicketPriceProvider):
    """演示数据源（不消耗 API 次数）。

    对任意（站点, 日期）确定性生成演示车次与价格，保证相同输入得到相同输出；
    可通过 JSON 覆盖文件配置：
    - 测试车次：{"flights": [{"date": "...", "train_no": "G9801",
      "train_type": "G", "depart_time": "16:45", "arrive_time": "19:35",
      "duration": "02:50", "price": 460, "depart": "北京南", "arrive": "苏州北"}]}
      按日期+车次号精确匹配；depart/arrive 可选，缺省时对任意站点生效。
    - 旧格式价格覆盖：{"G9801": 560} 按车次号覆盖价格。
    """

    name = "demo"
    display_name = "演示数据(不消耗API次数)"

    def __init__(self, config: Optional[dict] = None, plugin_dir: str = ""):
        super().__init__(config=config, plugin_dir=plugin_dir)
        self.config = config or {}
        override_path = self.config.get("demo_override_file") or os.path.join(
            plugin_dir, "data", "demo_overrides.json"
        )
        self.override_path = override_path
        self.overrides: Dict[str, float] = {}
        self.fixtures: List[TrainFixture] = []
        self._load_overrides()

    def _load_overrides(self) -> None:
        self.overrides = {}
        self.fixtures = []
        try:
            if os.path.exists(self.override_path):
                with open(self.override_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for item in data.get("flights", []) or []:
                        if not isinstance(item, dict):
                            continue
                        try:
                            self.fixtures.append(
                                TrainFixture(
                                    date=str(item["date"]),
                                    train_type=str(item.get("train_type") or "G")
                                    .strip()
                                    .upper(),
                                    train_no=str(item["train_no"]).strip().upper(),
                                    depart_time=str(item.get("depart_time") or "--:--"),
                                    arrive_time=str(item.get("arrive_time") or "--:--"),
                                    duration=str(item.get("duration") or ""),
                                    price=float(item["price"]),
                                    seat_name=str(item.get("seat_name") or "二等座"),
                                    depart=str(item.get("depart", "") or ""),
                                    arrive=str(item.get("arrive", "") or ""),
                                )
                            )
                        except (KeyError, TypeError, ValueError):
                            continue
                    # 兼容旧的 {车次号: 价格} 覆盖格式
                    self.overrides = {
                        str(k).strip().upper(): float(v)
                        for k, v in data.items()
                        if k != "flights" and isinstance(v, (int, float))
                    }
        except Exception:
            self.overrides = {}
            self.fixtures = []

    async def search(
        self,
        depart: str,
        arrive: str,
        date: dt.date,
        train_types: Optional[List[str]],
    ) -> List[Train]:
        types = list(train_types) if train_types else list(TRAIN_TYPE_NAMES)
        trains: List[Train] = []
        for ttype in types:
            # 优先应用测试车次夹具：命中（日期+车次号）后完全替代该类型生成车次
            fixtures = [
                fx
                for fx in self.fixtures
                if fx.train_type == ttype
                and fx.date == date.isoformat()
                and (not fx.depart or (fx.depart == depart and fx.arrive == arrive))
            ]
            for fx in fixtures:
                price = self.overrides.get(fx.train_no, fx.price)
                trains.append(
                    Train(
                        train_type=ttype,
                        train_no=fx.train_no,
                        depart_station=depart,
                        arrive_station=arrive,
                        depart_date=date.isoformat(),
                        depart_time=fx.depart_time,
                        arrive_time=fx.arrive_time,
                        duration=fx.duration,
                        seat_name=fx.seat_name,
                        price=float(price),
                        num="有",
                        prices=[
                            SeatPrice(
                                seat_name=fx.seat_name,
                                price=float(price),
                                num="有",
                            )
                        ],
                        source_note="演示数据（非真实票价，不消耗API次数）",
                    )
                )
            for i in range(DEFAULT_TRAINS_PER_TYPE):
                seed = f"{ttype}|{depart}|{arrive}|{date.isoformat()}|{i}"
                h = int(hashlib.md5(seed.encode("utf-8")).hexdigest(), 16)
                train_no = f"{ttype}{1000 + (h % 9000)}"
                depart_hour = 6 + ((i * 5 + h) % 16)
                depart_minute = ((h // 3) % 4) * 10
                duration_min = 60 + (h % 180)
                arrive_total = depart_hour * 60 + depart_minute + duration_min
                arrive_hour = (arrive_total % 1440) // 60
                arrive_minute = arrive_total % 60
                base = {"G": 480, "D": 380, "Z": 300, "T": 220, "K": 140}.get(
                    ttype, 120
                )
                price = float(base + (h % 120))
                price = round(price / 10) * 10
                price = self.overrides.get(train_no, price)
                trains.append(
                    Train(
                        train_type=ttype,
                        train_no=train_no,
                        depart_station=depart,
                        arrive_station=arrive,
                        depart_date=date.isoformat(),
                        depart_time=f"{depart_hour:02d}:{depart_minute:02d}",
                        arrive_time=f"{arrive_hour:02d}:{arrive_minute:02d}",
                        duration=f"{duration_min // 60:02d}:{duration_min % 60:02d}",
                        seat_name="二等座",
                        price=price,
                        num="有",
                        prices=[SeatPrice(seat_name="二等座", price=price, num="有")],
                        source_note="演示数据（非真实票价，不消耗API次数）",
                    )
                )
        trains.sort(key=lambda t: (t.depart_time, t.train_no))
        return trains
