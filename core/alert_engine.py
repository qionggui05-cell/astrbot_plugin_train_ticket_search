"""余票量提醒判定。"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Dict, List, Optional

from .models import Lock, SeatPrice

# 余票显示为"有"时，按大量余票计（> 阈值，不触发提醒）
BIG_NUM = 9999


@dataclass
class AlertEvent:
    lock: Lock
    new_total: int


def parse_num(text: str) -> int:
    """将余票文本解析为数量："有"视为大量（>10 张），"无"视为 0，其余按数字解析。"""
    t = (text or "").strip()
    if t in ("有", "充足", "余票充足"):
        return BIG_NUM
    if not t or t in ("无", "无票", "-"):
        return 0
    try:
        return max(int(t), 0)
    except (TypeError, ValueError):
        return 0


def total_tickets(prices: List[SeatPrice]) -> int:
    """全部席别余票之和（"有"按大量计，无法解析按 0 计）。"""
    total = 0
    for s in prices or []:
        total += parse_num(s.num)
    return total


def evaluate(
    locks: List[Lock], new_seats: Dict[str, List[SeatPrice]]
) -> List[AlertEvent]:
    """根据最新席别余票更新锁定车次并返回需要推送的提醒事件。

    提醒依据为**全部席别余票总和**：
    - 合计 >= 阈值：重新武装（下次低于阈值会再次提醒）；
    - 合计 < 阈值 且已武装：产生一次提醒事件并解除武装，未回升前不重复；
    - 未刷新到该车次数据：跳过，保留旧状态；
    - 余票提醒已关闭：只更新数据，不产生提醒。
    注意：本函数会直接修改传入的 Lock 对象，调用方负责持久化。
    """
    events: List[AlertEvent] = []
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    for lock in locks:
        seats = new_seats.get(lock.key())
        if seats is None:
            continue
        lock.prices = list(seats)
        # 同步跟踪席别的价格与余票
        tracked = next(
            (s for s in seats if s.seat_name and s.seat_name == lock.seat_name),
            seats[0] if seats else None,
        )
        if tracked is not None:
            lock.last_price = tracked.price
            lock.last_num = tracked.num
        lock.updated_at = now
        if not lock.ticket_alert_enabled:
            continue
        total = total_tickets(seats)
        threshold = max(int(lock.ticket_alert_threshold or 10), 1)
        if total < threshold:
            if lock.alert_armed:
                events.append(AlertEvent(lock=lock, new_total=total))
                lock.alert_armed = False
        else:
            lock.alert_armed = True
    return events
