"""输入解析与校验：日期、站点、车次类型。"""

from __future__ import annotations

import datetime as dt
import re
from typing import List, Optional, Tuple

# 聚合数据「火车订票查询」接口仅允许查询 15 天内的日期
MAX_AHEAD_DAYS = 15

TRAIN_TYPE_NAMES = {
    "G": "高铁",
    "D": "动车",
    "Z": "直达特快",
    "T": "特快",
    "K": "快速",
    "O": "其他",
}

TRAIN_TYPE_ALIASES = {
    "G": "G",
    "高铁": "G",
    "高鐵": "G",
    "D": "D",
    "动车": "D",
    "動車": "D",
    "Z": "Z",
    "直达": "Z",
    "直達": "Z",
    "直达特快": "Z",
    "直達特快": "Z",
    "T": "T",
    "特快": "T",
    "K": "K",
    "快速": "K",
    "O": "O",
    "其他": "O",
}

_STATION_CODE_RE = re.compile(r"^[A-Za-z]{3}$")


def supported_types_text() -> str:
    return "、".join(f"{name}({code})" for code, name in TRAIN_TYPE_NAMES.items())


def parse_date(
    text: str, today: Optional[dt.date] = None
) -> Tuple[Optional[dt.date], Optional[str]]:
    t = (text or "").strip()
    today = today or dt.date.today()
    if t in ("今天", "今日"):
        d = today
    elif t in ("明天", "明日"):
        d = today + dt.timedelta(days=1)
    elif t == "后天":
        d = today + dt.timedelta(days=2)
    else:
        try:
            d = dt.date.fromisoformat(t.replace("/", "-").replace(".", "-"))
        except ValueError:
            return (
                None,
                f"无法识别的日期：{text}。请输入 今天/明天/后天 或 YYYY-MM-DD。",
            )
    if d < today:
        return None, "日期不能早于今天，请重新输入。"
    if d > today + dt.timedelta(days=MAX_AHEAD_DAYS):
        return None, (
            f"日期超出可查询范围（聚合数据接口最多查询未来 {MAX_AHEAD_DAYS} 天），请重新输入。"
        )
    return d, None


def resolve_station(text: str) -> Tuple[Optional[str], Optional[str]]:
    """解析站点输入：三字编码（如 VNP）转大写，其余按站点名称原样返回。"""
    t = (text or "").strip()
    if not t:
        return None, "站点不能为空，请重新输入。"
    if len(t) > 12:
        return None, f"站点名称过长：{t}。请重新输入（如 北京南）。"
    if _STATION_CODE_RE.match(t):
        return t.upper(), None
    return t, None


def parse_amount(text: str) -> Tuple[Optional[float], Optional[str]]:
    try:
        v = float((text or "").strip())
    except ValueError:
        return None, f"无法识别的金额：{text}。请输入数字，如 600。"
    if v <= 0:
        return None, "金额必须大于 0。"
    return v, None


def parse_train_type_tokens(
    tokens: List[str],
) -> Tuple[Optional[List[str]], Optional[str]]:
    """解析车次类型过滤：['高铁', 'D'] -> ['G', 'D']；空列表返回 (None, None)。"""
    if not tokens:
        return None, None
    types: List[str] = []
    for t in tokens:
        code = TRAIN_TYPE_ALIASES.get((t or "").strip().upper())
        if not code:
            return None, (f"无法识别的车次类型：{t}。支持：{supported_types_text()}。")
        if code not in types:
            types.append(code)
    return types, None
