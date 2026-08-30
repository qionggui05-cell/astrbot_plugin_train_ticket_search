"""定时刷新调度器（APScheduler 封装）。

只允许在以下时机调用数据源（每次消耗聚合数据 API 额度）：
- 定时刷新（默认每 4 小时）；
- 机器人启动刷新（可选，默认开启）；
- 用户执行 强制更新 指令。
“快查（获取价格）”只读取本地已保存的价格，不会调用数据源。
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional, Set

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .alert_engine import evaluate
from .formatting import format_alert
from .models import Lock, SeatPrice, Train
from .providers import TicketPriceProvider
from .storage import LockStorage

Notifier = Callable[[str, str], Awaitable[None]]


@dataclass
class RefreshResult:
    """一次刷新的明细，供强制更新等指令输出成功/失败构成。"""

    updated_keys: Set[str] = field(default_factory=set)   # 本次成功刷新到的锁定车次 key
    failed_groups: List[str] = field(default_factory=list)  # 查询失败的线路描述
    skipped: bool = False   # True 表示已有刷新任务在运行，本次被跳过

    @property
    def updated_count(self) -> int:
        return len(self.updated_keys)


def _train_key_from_lock(lock: Lock) -> str:
    return (
        f"{lock.train_type}|{lock.train_no}|{lock.depart_station}|"
        f"{lock.arrive_station}|{lock.depart_date}"
    )


class RefreshScheduler:
    """定时刷新所有锁定车次余票与价格，并按余票量推送提醒。"""

    def __init__(
        self,
        storage: LockStorage,
        provider: TicketPriceProvider,
        notifier: Notifier,
        interval_hours: float = 4,
        logger: Optional[logging.Logger] = None,
    ):
        self.storage = storage
        self.provider = provider
        self.notifier = notifier
        self.interval_hours = max(0.25, float(interval_hours))
        self.logger = logger or logging.getLogger("train_scheduler")
        self._scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

    def start(self) -> None:
        """启动周期任务（幂等）；首个周期运行时间 = 当前时间 + interval，不立即触发。"""
        job = self._scheduler.get_job("train_refresh")
        if job is None:
            self._scheduler.add_job(
                self._run,
                "interval",
                hours=self.interval_hours,
                id="train_refresh",
                replace_existing=True,
                next_run_time=dt.datetime.now() + dt.timedelta(hours=self.interval_hours),
            )
        if not self._scheduler.running:
            self._scheduler.start()

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    async def _run(self) -> None:
        try:
            await self.refresh_once()
        except Exception as e:
            self.logger.exception(f"定时刷新失败: {e}")

    async def refresh_once(self) -> int:
        """刷新所有锁定车次余票与价格并检查提醒；部分线路失败时保留旧数据。

        返回本次实际刷新到数据的锁定车次数量（0 表示没有锁定车次或全部失败，
        没有锁定车次时不会调用 API）。

        同一时间只允许一个刷新在运行：定时刷新与用户强制更新/启动刷新重叠时，
        后发起的刷新直接返回 0，避免重复调用 API。
        """
        if getattr(self, "_refreshing", False):
            self.logger.info("已有刷新任务在运行，跳过本次刷新")
            self.last_result = RefreshResult(skipped=True)
            return 0
        self._refreshing = True
        try:
            return await self._refresh_once_locked()
        finally:
            self._refreshing = False

    async def _refresh_once_locked(self) -> int:
        locks = self.storage.all_locks()
        result = RefreshResult()
        self.last_result = result
        if not locks:
            return 0

        # 已发车的车次自动解除锁定，不再参与刷新与提醒
        active = [l for l in locks if not l.is_departed()]
        if len(active) != len(locks):
            self.storage.replace_all(active)
            self.logger.info(f"自动解除锁定已发车车次 {len(locks) - len(active)} 个")
        locks = active
        if not locks:
            return 0

        # 按（出发站, 到达站, 日期）去重：同一条线路只调用一次数据源
        groups: Dict[tuple, set] = {}
        for l in locks:
            key = (l.depart_station, l.arrive_station, l.depart_date)
            groups.setdefault(key, set()).add(l.train_type)

        train_map: Dict[str, Train] = {}
        by_no: Dict[str, List[Train]] = {}
        ok_groups: Set[tuple] = set()
        for (depart, arrive, date_str), types in groups.items():
            try:
                date = dt.date.fromisoformat(date_str)
            except ValueError:
                self.logger.warning(f"忽略无效日期: {date_str}")
                continue
            try:
                trains = await self.provider.search(
                    depart, arrive, date, sorted(types)
                )
                ok_groups.add((depart, arrive, date_str))
            except Exception as e:
                self.logger.exception(f"刷新 {depart}→{arrive} {date_str} 失败: {e}")
                result.failed_groups.append(f"{depart}→{arrive} {date_str}")
                continue
            for t in trains:
                train_map[t.key()] = t
                by_no.setdefault(t.train_no, []).append(t)

        new_seats: Dict[str, List[SeatPrice]] = {}
        for l in locks:
            t = train_map.get(_train_key_from_lock(l))
            if t is None:
                # 站点名称与数据源返回不完全一致时，按车次号+日期回退匹配，
                # 保证所有已锁定车次都能刷新到（无票车次同样更新）
                for cand in by_no.get(l.train_no, []):
                    if cand.depart_date == l.depart_date:
                        t = cand
                        break
            if t is None:
                # 查询成功但结果中没有该车次：按已售罄（余票 0）处理，
                # 保证余票为 0 / 接口未返回的车次同样参与提醒，不漏车次。
                group_key = (l.depart_station, l.arrive_station, l.depart_date)
                if group_key in ok_groups:
                    new_seats[l.key()] = []
                continue
            # 时刻信息随刷新同步更新，供快查展示
            l.depart_time = t.depart_time
            l.arrive_time = t.arrive_time
            new_seats[l.key()] = list(t.prices)
        result.updated_keys = set(new_seats)

        events = evaluate(locks, new_seats)
        # 每次更新后立即落盘保存（原子写入）
        self.storage.replace_all(locks)

        for ev in events:
            try:
                await self.notifier(
                    ev.lock.unified_msg_origin,
                    format_alert(ev.lock, ev.new_total),
                )
            except Exception:
                self.logger.exception("发送余票提醒失败")
        self.logger.info(
            f"刷新完成：{len(new_seats)} 个锁定车次更新数据，{len(events)} 条余票提醒"
        )
        return len(new_seats)

    def call_info(self) -> Dict[str, object]:
        """返回数据源调用统计，供输出展示（快查/强制更新结果）。"""
        info = getattr(self.provider, "call_info", lambda: {})()
        return dict(info or {})
