import asyncio
import datetime as dt

# 查询日期动态取未来第 3 天，避免测试因日期过期而失效
QDATE = dt.date.today() + dt.timedelta(days=3)

from core.demo_provider import DemoProvider
from core.models import Lock
from core.scheduler import RefreshScheduler
from core.storage import LockStorage


async def async_nop(*_a, **_k):
    """异步空通知器：scheduler 会 await notifier(...)。"""

def run(coro):
    return asyncio.run(coro)


class FakeNumProvider:
    """按需返回固定车次与余票数量的数据源。"""

    def __init__(self, train_no, num):
        self.train_no = train_no
        self.num = num
        self.seat_name = "二等座"
        self.price = 627.0

    async def search(self, depart, arrive, date, train_types):
        from core.models import SeatPrice, Train

        return [
            Train(
                train_type="G",
                train_no=self.train_no,
                depart_station=depart,
                arrive_station=arrive,
                depart_date=date.isoformat(),
                depart_time="16:45",
                arrive_time="19:35",
                duration="02:50",
                seat_name=self.seat_name,
                price=self.price,
                num=self.num,
                prices=[
                    SeatPrice(
                        seat_name=self.seat_name,
                        price=self.price,
                        num=self.num,
                    )
                ],
            )
        ]


def make_lock(tmp_path):
    """用演示数据源找一个真实生成的车次号，构造一个锁定车次。"""
    p0 = DemoProvider(config={}, plugin_dir=str(tmp_path))
    trains = run(p0.search("北京南", "苏州北", QDATE, ["G"]))
    target = trains[0]
    future = (dt.date.today() + dt.timedelta(days=2)).isoformat()
    return Lock(
        user_id="u1",
        unified_msg_origin="origin",
        train_type=target.train_type,
        train_no=target.train_no,
        depart_station="北京南",
        arrive_station="苏州北",
        depart_date=future,
        ticket_alert_enabled=True,
        ticket_alert_threshold=10,
        last_price=target.price,
    )


def test_refresh_saves_num_price_and_alert_cycle(tmp_path):
    storage = LockStorage(str(tmp_path / "locks.json"))
    lock = make_lock(tmp_path)
    storage.add_lock(lock)
    messages = []

    async def notifier(origin, text):
        messages.append((origin, text))

    provider = FakeNumProvider(lock.train_no, "12")
    scheduler = RefreshScheduler(storage, provider, notifier)

    # 首次刷新：余票 12 >= 10，不提醒；价格/余票/时刻落盘并记录更新时间
    n = run(scheduler.refresh_once())
    assert n == 1
    saved = storage.all_locks()[0]
    assert saved.last_num == "12"
    assert saved.prices[0].num == "12"
    assert saved.last_price == 627.0
    assert saved.depart_time == "16:45"
    assert saved.arrive_time == "19:35"
    assert saved.updated_at is not None
    assert messages == []

    # 余票跌破 10：触发一次提醒并解除武装
    provider.num = "5"
    run(scheduler.refresh_once())
    saved = storage.all_locks()[0]
    assert len(messages) == 1
    assert "余票提醒" in messages[0][1] and "5" in messages[0][1]
    assert saved.alert_armed is False
    assert saved.last_num == "5"

    # 余票回升 >= 10：重新武装，不重复提醒
    provider.num = "12"
    run(scheduler.refresh_once())
    assert len(messages) == 1
    assert storage.all_locks()[0].alert_armed is True

    # 再次跌破：再次提醒
    provider.num = "3"
    run(scheduler.refresh_once())
    assert len(messages) == 2
    assert "余票提醒" in messages[1][1] and "3" in messages[1][1]

    # 重启后数据仍持久化
    storage2 = LockStorage(str(tmp_path / "locks.json"))
    reloaded = storage2.all_locks()[0]
    assert reloaded.last_num == "3"
    assert reloaded.last_price == 627.0
    assert reloaded.updated_at is not None


def test_refresh_disabled_alert_no_event(tmp_path):
    storage = LockStorage(str(tmp_path / "locks.json"))
    lock = make_lock(tmp_path)
    lock.ticket_alert_enabled = False
    storage.add_lock(lock)
    messages = []

    async def notifier(origin, text):
        messages.append((origin, text))

    provider = FakeNumProvider(lock.train_no, "2")
    scheduler = RefreshScheduler(storage, provider, notifier)
    run(scheduler.refresh_once())
    assert messages == []
    assert storage.all_locks()[0].last_num == "2"


def test_refresh_auto_unlocks_departed(tmp_path):
    storage = LockStorage(str(tmp_path / "locks.json"))
    lock = make_lock(tmp_path)
    lock.depart_date = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    lock.depart_time = "10:00"
    storage.add_lock(lock)

    searched = {"n": 0}
    provider = DemoProvider(config={}, plugin_dir=str(tmp_path))
    orig_search = provider.search

    async def counting_search(*args, **kwargs):
        searched["n"] += 1
        return await orig_search(*args, **kwargs)

    provider.search = counting_search
    scheduler = RefreshScheduler(storage, provider, async_nop)
    n = run(scheduler.refresh_once())
    assert n == 0
    assert storage.all_locks() == []
    assert searched["n"] == 0  # 已发车车次不调用 API


def test_refresh_no_locks_returns_zero(tmp_path):
    storage = LockStorage(str(tmp_path / "locks.json"))
    provider = DemoProvider(config={}, plugin_dir=str(tmp_path))
    scheduler = RefreshScheduler(storage, provider, async_nop)
    assert run(scheduler.refresh_once()) == 0


def test_refresh_seat_price_fallback(tmp_path):
    """锁定席别消失时回退到数据源跟踪席别价格。"""
    storage = LockStorage(str(tmp_path / "locks.json"))
    lock = make_lock(tmp_path)
    lock.seat_name = "商务座"
    storage.add_lock(lock)

    async def fake_search(depart, arrive, date, train_types):
        from core.models import SeatPrice, Train

        return [
            Train(
                train_type="G",
                train_no=lock.train_no,
                depart_station="北京南",
                arrive_station="苏州北",
                depart_date=date.isoformat(),
                depart_time="16:45",
                arrive_time="19:35",
                duration="02:50",
                seat_name="二等座",
                price=627,
                num="1",
                prices=[SeatPrice(seat_name="二等座", price=627, num="1")],
            )
        ]

    provider = DemoProvider(config={}, plugin_dir=str(tmp_path))
    provider.search = fake_search
    scheduler = RefreshScheduler(storage, provider, async_nop)
    run(scheduler.refresh_once())
    assert storage.all_locks()[0].last_price == 627


class MultiDateProvider:
    """按日期返回对应车次的数据源，记录每次 search 调用。"""

    def __init__(self):
        self.calls = []

    async def search(self, depart, arrive, date, train_types):
        from core.models import SeatPrice, Train

        # 按调用顺序交替返回 G1/G2（按日期奇偶会在跨月时错位）
        no = "G1" if len(self.calls) % 2 == 0 else "G2"
        self.calls.append(date.isoformat())
        return [
            Train(
                train_type="G",
                train_no=no,
                depart_station=depart,
                arrive_station=arrive,
                depart_date=date.isoformat(),
                depart_time="08:00",
                arrive_time="12:00",
                duration="04:00",
                seat_name="二等座",
                price=500,
                num="5",
                prices=[SeatPrice(seat_name="二等座", price=500, num="5")],
            )
        ]


def test_refresh_covers_multiple_dates_in_one_round(tmp_path):
    storage = LockStorage(str(tmp_path / "locks.json"))
    today = dt.date.today()
    d1 = (today + dt.timedelta(days=1)).isoformat()
    d2 = (today + dt.timedelta(days=2)).isoformat()
    for i, date_str in enumerate((d1, d2)):
        lock = make_lock(tmp_path)
        lock.depart_date = date_str
        lock.train_no = "G1" if i == 0 else "G2"
        storage.add_lock(lock)

    provider = MultiDateProvider()
    scheduler = RefreshScheduler(storage, provider, async_nop)
    n = run(scheduler.refresh_once())
    assert n == 2  # 两个日期的锁定车次在一次刷新中全部更新
    assert sorted(provider.calls) == sorted([d1, d2])
    by_date = {l.depart_date: l for l in storage.all_locks()}
    assert by_date[d1].last_num == "5"
    assert by_date[d2].last_num == "5"
    assert by_date[d1].depart_time == "08:00"


def test_refresh_skipped_when_already_running(tmp_path):
    storage = LockStorage(str(tmp_path / "locks.json"))
    lock = make_lock(tmp_path)
    storage.add_lock(lock)
    provider = MultiDateProvider()
    scheduler = RefreshScheduler(storage, provider, async_nop)

    # 模拟已有刷新任务在运行：并发调用应直接跳过，不重复调用 API
    scheduler._refreshing = True
    n = run(scheduler.refresh_once())
    assert n == 0
    assert provider.calls == []
    assert scheduler._refreshing is True  # 由正在运行的任务负责释放，跳过时不改动


def test_refresh_matches_by_train_no_when_stations_differ(tmp_path):
    """锁定车次站点与数据源返回不完全一致时，按车次号+日期回退匹配并刷新。"""
    storage = LockStorage(str(tmp_path / "locks.json"))
    lock = make_lock(tmp_path)
    lock.depart_station = "西安"
    lock.arrive_station = "上海"
    storage.add_lock(lock)

    async def search(depart, arrive, date, train_types):
        from core.models import SeatPrice, Train

        # 数据源返回更精确的站点名（西安北/上海虹桥）
        return [
            Train(
                train_type=lock.train_type,
                train_no=lock.train_no,
                depart_station="西安北",
                arrive_station="上海虹桥",
                depart_date=date.isoformat(),
                depart_time="06:13",
                arrive_time="13:22",
                duration="07:09",
                seat_name="二等座",
                price=676,
                num="2",
                prices=[SeatPrice(seat_name="二等座", price=676, num="2")],
            )
        ]

    provider = DemoProvider(config={}, plugin_dir=str(tmp_path))
    provider.search = search
    scheduler = RefreshScheduler(storage, provider, async_nop)
    n = run(scheduler.refresh_once())
    assert n == 1
    saved = storage.all_locks()[0]
    assert saved.last_num == "2"
    assert saved.depart_time == "06:13"


def test_refresh_missing_train_treated_as_sold_out_alerts(tmp_path):
    """查询成功但结果中缺失锁定车次（如已售罄未返回）时，
    按 0 票参与提醒，保证所有车次一起提醒、不漏 0 票车次。"""
    storage = LockStorage(str(tmp_path / "locks.json"))
    lock = make_lock(tmp_path)
    lock.train_no = "G1"
    storage.add_lock(lock)
    messages = []

    async def notifier(origin, text):
        messages.append((origin, text))

    async def search(depart, arrive, date, train_types):
        from core.models import SeatPrice, Train

        # 查询成功但返回的是另一车次（G1 未出现在结果中）
        return [
            Train(
                train_type="G",
                train_no="G2",
                depart_station=depart,
                arrive_station=arrive,
                depart_date=date.isoformat(),
                depart_time="08:00",
                arrive_time="12:00",
                duration="04:00",
                seat_name="二等座",
                price=500,
                num="5",
                prices=[SeatPrice(seat_name="二等座", price=500, num="5")],
            )
        ]

    provider = DemoProvider(config={}, plugin_dir=str(tmp_path))
    provider.search = search
    scheduler = RefreshScheduler(storage, provider, notifier)
    n = run(scheduler.refresh_once())
    assert n == 1  # 缺失车次按 0 票更新，仍计入刷新数量
    assert len(messages) == 1
    assert "余票提醒" in messages[0][1]
    saved = storage.all_locks()[0]
    assert saved.updated_at is not None
    assert saved.prices == []


def test_refresh_group_failure_skips_without_alert(tmp_path):
    """数据源整组查询失败时保留旧数据且不触发提醒，避免接口波动误报。"""
    storage = LockStorage(str(tmp_path / "locks.json"))
    lock = make_lock(tmp_path)
    storage.add_lock(lock)
    messages = []

    async def notifier(origin, text):
        messages.append((origin, text))

    async def failing_search(depart, arrive, date, train_types):
        from core.providers import ProviderError

        raise ProviderError("接口盒子余票接口返回错误：失败，请重试!")

    provider = DemoProvider(config={}, plugin_dir=str(tmp_path))
    provider.search = failing_search
    scheduler = RefreshScheduler(storage, provider, notifier)
    n = run(scheduler.refresh_once())
    assert n == 0
    assert messages == []
    saved = storage.all_locks()[0]
    assert saved.updated_at is None


def test_refresh_result_reports_updated_and_failed_groups(tmp_path):
    """刷新明细：成功刷新的车次 key 与查询失败的线路分开记录，
    供强制更新指令区分“已更新”与“保留旧数据”的车次。"""
    storage = LockStorage(str(tmp_path / "locks.json"))
    lock_ok = make_lock(tmp_path)
    lock_fail = make_lock(tmp_path)
    lock_fail.train_no = "G9999"
    lock_fail.depart_station = "上海"
    lock_fail.arrive_station = "北京"
    storage.add_lock(lock_ok)
    storage.add_lock(lock_fail)

    async def failing_search(depart, arrive, date, train_types):
        from core.providers import ProviderError

        raise ProviderError("接口频次限制")

    provider = DemoProvider(config={}, plugin_dir=str(tmp_path))
    real_search = provider.search

    async def selective_search(depart, arrive, date, train_types):
        if depart == "上海":
            raise ProviderError("接口频次限制")
        return await real_search(depart, arrive, date, train_types)

    provider.search = selective_search
    scheduler = RefreshScheduler(storage, provider, async_nop)
    n = run(scheduler.refresh_once())
    assert n == 1  # 只有 北京南→苏州北 的车次刷新成功
    result = scheduler.last_result
    assert result.updated_count == 1
    assert lock_ok.key() in result.updated_keys
    assert lock_fail.key() not in result.updated_keys
    assert result.failed_groups == [
        f"上海→北京 {lock_fail.depart_date}"
    ]
    assert result.skipped is False


def test_refresh_result_marks_skip_when_already_running(tmp_path):
    storage = LockStorage(str(tmp_path / "locks.json"))
    storage.add_lock(make_lock(tmp_path))
    provider = DemoProvider(config={}, plugin_dir=str(tmp_path))
    scheduler = RefreshScheduler(storage, provider, async_nop)

    started = asyncio.Event()

    async def slow_search(*_a):
        started.set()
        await asyncio.sleep(0.05)
        return []

    async def concurrent_refresh():
        await started.wait()
        return await scheduler.refresh_once()

    provider.search = slow_search
    run(_run_concurrent(scheduler, concurrent_refresh))


async def _run_concurrent(scheduler, coro_factory):
    first = asyncio.ensure_future(scheduler.refresh_once())
    second = asyncio.ensure_future(coro_factory())
    await asyncio.gather(first, second)
    assert scheduler.last_result.skipped is True
