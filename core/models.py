"""数据模型定义。"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import List, Optional


@dataclass
class SeatPrice:
    """车次的一个席别票价。"""

    seat_name: str = ""   # 席别名称，如 二等座
    price: float = 0.0    # 票价（元）
    num: str = ""         # 余票数量，如 "12" / "无"


@dataclass
class Train:
    """一次火车票查询结果（一个车次）。"""

    train_type: str          # 车次类型：G/D/Z/T/K/O
    train_no: str            # 车次号：G25
    depart_station: str      # 出发站：北京南
    arrive_station: str      # 到达站：苏州北
    depart_date: str         # 出发日期 YYYY-MM-DD
    depart_time: str         # 出发时刻 HH:MM
    arrive_time: str         # 到达时刻 HH:MM
    duration: str = ""       # 历时 HH:MM
    seat_name: str = ""      # 跟踪席别（如 二等座）
    price: float = 0.0       # 跟踪席别票价
    num: str = ""            # 跟踪席别余票
    prices: List[SeatPrice] = field(default_factory=list)  # 全部席别票价
    source_note: str = "聚合数据火车票查询API"

    def key(self) -> str:
        return (
            f"{self.train_type}|{self.train_no}|{self.depart_station}|"
            f"{self.arrive_station}|{self.depart_date}"
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Lock:
    """用户锁定的车次。"""

    user_id: str
    unified_msg_origin: str
    train_type: str          # 车次类型：G/D/Z/T/K/O
    train_no: str            # 车次号：G25
    depart_station: str      # 出发站：北京南
    arrive_station: str      # 到达站：苏州北
    depart_date: str         # 出发日期 YYYY-MM-DD
    depart_time: str = ""    # 出发时刻 HH:MM（用于快查展示）
    arrive_time: str = ""    # 到达时刻 HH:MM
    seat_name: str = "二等座"  # 跟踪席别
    ticket_alert_enabled: bool = True   # 余票提醒开关（默认开启）
    ticket_alert_threshold: int = 10    # 余票提醒阈值（张）
    alert_armed: bool = True            # 跌破提醒是否处于待触发状态
    last_price: Optional[float] = None  # 最近一次刷新到的价格
    last_num: str = ""                  # 最近一次刷新到的余票
    prices: List[SeatPrice] = field(default_factory=list)  # 全部席别票价
    updated_at: Optional[str] = None    # 最近一次价格更新时间 ISO8601
    locked_at: str = ""                 # 锁定时间 ISO8601

    def key(self) -> str:
        return (
            f"{self.user_id}|{self.train_type}|{self.train_no}|"
            f"{self.depart_station}|{self.arrive_station}|{self.depart_date}"
        )

    def train_ref(self) -> str:
        return f"{self.train_no} {self.depart_station}→{self.arrive_station} {self.depart_date}"

    def is_departed(self, now: Optional[dt.datetime] = None) -> bool:
        """出发时刻已过（含当日已过点）则视为已发车。"""
        now = now or dt.datetime.now()
        if getattr(now, "tzinfo", None) is not None:
            now = now.replace(tzinfo=None)
        try:
            d = dt.date.fromisoformat(self.depart_date)
        except ValueError:
            return False
        t = (self.depart_time or "").strip()
        if t and ":" in t:
            try:
                hh, mm = (int(x) for x in t.split(":")[:2])
                dep = dt.datetime(d.year, d.month, d.day, hh, mm)
                return dep <= now
            except (TypeError, ValueError):
                pass
        return d < now.date()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Lock":
        return cls(
            user_id=str(d.get("user_id", "")),
            unified_msg_origin=str(d.get("unified_msg_origin", "")),
            train_type=str(d.get("train_type", "") or ""),
            train_no=str(d.get("train_no", "")),
            depart_station=str(d.get("depart_station", "")),
            arrive_station=str(d.get("arrive_station", "")),
            depart_date=str(d.get("depart_date", "")),
            depart_time=str(d.get("depart_time", "") or ""),
            arrive_time=str(d.get("arrive_time", "") or ""),
            seat_name=str(d.get("seat_name", "二等座") or "二等座"),
            ticket_alert_enabled=bool(d.get("ticket_alert_enabled", True)),
            ticket_alert_threshold=int(
                d.get("ticket_alert_threshold", 10) or 10
            ),
            alert_armed=bool(d.get("alert_armed", True)),
            last_price=d.get("last_price"),
            last_num=str(d.get("last_num", "") or ""),
            prices=[
                SeatPrice(
                    seat_name=str(p.get("seat_name", "") or ""),
                    price=float(p.get("price", 0) or 0),
                    num=str(p.get("num", "") or ""),
                )
                for p in d.get("prices", []) or []
                if isinstance(p, dict)
            ],
            updated_at=d.get("updated_at"),
            locked_at=str(d.get("locked_at", "")),
        )
