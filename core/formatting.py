"""消息文本格式化。"""

from __future__ import annotations

import datetime as dt
from typing import Dict, List

from .alert_engine import total_tickets
from .models import Lock, SeatPrice, Train
from .parsing import TRAIN_TYPE_NAMES


def train_type_display(ttype: str) -> str:
    name = TRAIN_TYPE_NAMES.get(ttype, ttype)
    return f"{name}({ttype})"


def train_line(train: Train) -> str:
    duration = f"（历时 {train.duration}）" if train.duration else ""
    num_txt = f"[余票 {train.num}]" if train.num else ""
    if train.price > 0:
        price_txt = f"{train.seat_name or '票价'} ¥{train.price:.0f}"
    elif train.seat_name:
        price_txt = f"{train.seat_name}（票价待查）"
    else:
        price_txt = "票价待查"
    return (
        f"{train.train_no} {train.depart_station} {train.depart_time} → "
        f"{train.arrive_station} {train.arrive_time}{duration} "
        f"{price_txt}{num_txt}"
    )


def seat_line(seat: SeatPrice) -> str:
    price_txt = f" ¥{seat.price:.0f}" if seat.price > 0 else ""
    return f"{seat.seat_name}{price_txt}[余票 {seat.num or '无'}]"


def format_query_results(
    trains: List[Train], date_str: str, call_info: Optional[dict] = None
) -> str:
    if not trains:
        return f"{date_str} 没有查询到车次。"
    lines = [f"===== {date_str} 车次与票价（含已售罄车次） ====="]
    by_type: Dict[str, List[Train]] = {}
    for t in trains:
        by_type.setdefault(t.train_type, []).append(t)
    for ttype in sorted(by_type):
        lines.append(f"【{train_type_display(ttype)}】")
        for t in sorted(by_type[ttype], key=lambda x: (x.depart_time, x.train_no)):
            lines.append(f"  {train_line(t)}")
            seats = " · ".join(seat_line(s) for s in t.prices if s.seat_name)
            if seats:
                lines.append(f"      {seats}")
    lines.append("数据来源：" + (trains[0].source_note or "聚合数据火车票查询API"))
    lines.append(format_call_info_line(call_info))
    lines.append(f"共 {len(trains)} 个车次（已按发车时间排序）")
    lines.append(
        "提示：回复 /火车票 锁定 <车次号> 可锁定车次（多个车次号用空格分隔）。"
    )
    return "\n".join(lines)


def format_alert(lock: Lock, total: int) -> str:
    threshold = max(int(lock.ticket_alert_threshold or 10), 1)
    return (
        f"【火车票余票提醒】{lock.train_ref()} 当前全部席别余票合计 {total} 张，"
        f"低于提醒线 {threshold} 张，请及时购票。"
    )


def _price_txt(lock: Lock) -> str:
    # 0 元与未知等价（票价未查到时兜底为 0），均显示为暂无报价
    if not lock.last_price:
        return "暂无报价"
    num_txt = f" [余票 {lock.last_num}]" if lock.last_num else ""
    return f"¥{lock.last_price:.0f}{num_txt}"


def _station_pair(lock: Lock):
    """返回带时刻的站点文本：(出发站 出发时刻, 到达站 到达时刻)。"""
    dep = f"{lock.depart_station} {lock.depart_time}".strip()
    arr = f"{lock.arrive_station} {lock.arrive_time}".strip()
    return dep, arr


def _date_md(date_str: str) -> str:
    try:
        d = dt.date.fromisoformat(date_str)
        return f"{d.month:02d}-{d.day:02d}"
    except (TypeError, ValueError):
        return date_str


def _alert_status_txt(lock: Lock) -> str:
    """余票提醒状态：开关 + 当前全部席别合计余票与阈值的关系。"""
    switch = "开" if lock.ticket_alert_enabled else "关"
    threshold = max(int(lock.ticket_alert_threshold or 10), 1)
    total = total_tickets(lock.prices)
    if total < threshold:
        return f"余票提醒：{switch}（<{threshold}张）"
    return f"余票提醒：{switch}（>{threshold}张）"


def _sorted_locks(locks: List[Lock]) -> List[Lock]:
    """按出发日期 + 发车时刻 + 车次号排序（发车时间早的排在前面）。"""
    return sorted(
        locks,
        key=lambda x: (
            x.depart_date or "",
            x.depart_time or "99:99",
            x.train_no or "",
        ),
    )


def _dt_display(ts: Optional[str]) -> str:
    """ISO 时间字符串转显示格式 YYYY-MM-DD HH:MM:SS。"""
    if not ts:
        return "暂无"
    try:
        d = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if d.tzinfo is not None:
            d = d.astimezone()
        return d.strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return str(ts)


def _latest_updated_at(locks: List[Lock]) -> str:
    ts = None
    for l in locks:
        if l.updated_at and (ts is None or l.updated_at > ts):
            ts = l.updated_at
    return _dt_display(ts)


def format_call_info_line(
    call_info: Optional[dict], locks: Optional[List[Lock]] = None
) -> str:
    """数据更新时间 + API 调用次数展示行。"""
    locks = locks or []
    info = call_info or {}
    source = str(info.get("source_name") or "未知数据源")
    calls = int(info.get("calls_today") or 0)
    updated = _dt_display(info.get("last_api_call_at"))
    if not updated or updated == "暂无":
        updated = _latest_updated_at(locks)
    return (
        f"数据来源：{source} ｜ 今日API调用次数：{calls} 次 ｜ 数据更新时间：{updated}"
    )


def format_quick(locks: List[Lock], call_info: Optional[dict] = None) -> str:
    if not locks:
        return "你还没有锁定任何车次，请先使用 /火车票 查票 查询并锁定。"
    lines = [
        "===== 已锁定车次价格（缓存） =====",
    ]
    sorted_locks = _sorted_locks(locks)
    for i, l in enumerate(sorted_locks, 1):
        dep, arr = _station_pair(l)
        seats = " · ".join(seat_line(s) for s in l.prices if s.seat_name)
        if not seats:
            seats = f"{l.seat_name or '票价'} {_price_txt(l)}"
        lines.append(
            f"{i}. {_date_md(l.depart_date)} {l.train_no} {dep} → {arr} ："
            f"{seats}，{_alert_status_txt(l)}"
        )
    lines.append(format_call_info_line(call_info, locks))
    return "\n".join(lines)


def format_locks(locks: List[Lock]) -> str:
    if not locks:
        return "你还没有锁定任何车次。"
    lines = ["===== 我的锁定车次 =====", "说明：仅显示已锁定车次与余票提醒开关状态。"]
    for i, l in enumerate(_sorted_locks(locks), 1):
        dep, arr = _station_pair(l)
        lines.append(
            f"{i}. {l.train_no} {dep} → {arr} {l.depart_date}，{_alert_status_txt(l)}"
        )
    return "\n".join(lines)


def format_lock_confirm(new_locks: List[Lock]) -> str:
    lines = ["已锁定以下车次："]
    for i, l in enumerate(_sorted_locks(new_locks), 1):
        price_txt = f"¥{l.last_price:.0f}" if l.last_price else "暂无报价"
        lines.append(
            f"  {i}. {train_type_display(l.train_type)} {l.train_ref()} "
            f"（当前 {price_txt}，余票提醒：默认开启）"
        )
    lines.append(
        "可用 /火车票 快查 查看缓存价格，用 /火车票 余票提醒 关 [车次号] 关闭余票提醒。"
    )
    return "\n".join(lines)
