import asyncio
import datetime as dt

from core.flow import FlowEngine
from core.models import Train
from core.parsing import parse_amount, parse_date, parse_train_type_tokens, resolve_station
from core.providers import ProviderError
from core.session import SessionManager

# 交互测试用的日期动态取未来第 2 天，避免测试因日期过期而失效
FDATE = dt.date.today() + dt.timedelta(days=2)
FDATE_STR = FDATE.isoformat()


def run(coro):
    return asyncio.run(coro)


class FakeSearch:
    def __init__(self):
        self.calls = []

    async def __call__(self, depart, arrive, date, train_types):
        self.calls.append((depart, arrive, date, list(train_types) if train_types else None))
        return [
            Train(
                train_type="G",
                train_no="G25",
                depart_station=depart,
                arrive_station=arrive,
                depart_date=date.isoformat(),
                depart_time="18:04",
                arrive_time="22:32",
                duration="04:28",
                seat_name="二等座",
                price=627,
                num="1",
            )
        ]


def test_parse_date_variants():
    today = dt.date(2026, 8, 2)
    d, err = parse_date("明天", today)
    assert err is None and d == dt.date(2026, 8, 3)
    d, err = parse_date("2026-08-10", today)
    assert err is None and d == dt.date(2026, 8, 10)
    d, err = parse_date("昨天", today)
    assert d is None and err is not None
    d, err = parse_date("随便", today)
    assert d is None and err is not None
    # 聚合数据接口限 15 天
    d, err = parse_date((today + dt.timedelta(days=16)).isoformat(), today)
    assert d is None and "15 天" in err


def test_parse_amount():
    assert parse_amount("600") == (600.0, None)
    assert parse_amount("abc")[0] is None
    assert parse_amount("-5")[0] is None


def test_parse_train_type_tokens():
    types, err = parse_train_type_tokens(["高铁", "D"])
    assert err is None and types == ["G", "D"]
    types, err = parse_train_type_tokens([])
    assert err is None and types is None
    _, err = parse_train_type_tokens(["磁悬浮"])
    assert err is not None


def test_resolve_station():
    assert resolve_station("北京南")[0] == "北京南"
    assert resolve_station("vnp")[0] == "VNP"
    assert resolve_station("")[0] is None
    assert resolve_station(" 苏州北 ")[0] == "苏州北"


def test_full_interactive_flow():
    search = FakeSearch()
    engine = FlowEngine(search=search)
    mgr = SessionManager(timeout_seconds=600)
    session = mgr.start("u1", "origin", None)

    reply = run(engine.handle_reply(session, FDATE_STR))
    assert "出发站" in reply
    reply = run(engine.handle_reply(session, "北京南"))
    assert "到达站" in reply
    reply = run(engine.handle_reply(session, "苏州北"))
    assert "G25" in reply and "¥627" in reply
    assert session.step == "done"
    assert search.calls == [("北京南", "苏州北", FDATE, None)]
    assert session.last_query[0].train_no == "G25"


def test_invalid_input_keeps_step():
    search = FakeSearch()
    engine = FlowEngine(search=search)
    mgr = SessionManager()
    session = mgr.start("u1", "origin", ["G"])

    reply = run(engine.handle_reply(session, "不是日期"))
    assert "无法识别的日期" in reply
    assert session.step == "date"

    reply = run(engine.handle_reply(session, FDATE_STR))
    assert "出发站" in reply
    reply = run(engine.handle_reply(session, ""))
    assert "站点不能为空" in reply
    assert session.step == "depart"

    reply = run(engine.handle_reply(session, "北京南"))
    assert "到达站" in reply
    reply = run(engine.handle_reply(session, "北京南"))
    assert "相同" in reply
    assert session.step == "arrive"


def test_session_timeout():
    mgr = SessionManager(timeout_seconds=10)
    session = mgr.start("u1", "origin", None)
    session.updated_at -= 60
    assert mgr.get("u1", "origin") is None


def test_session_clear():
    mgr = SessionManager()
    mgr.start("u1", "origin", None)
    mgr.clear("u1", "origin")
    assert mgr.get("u1", "origin") is None


class FlakySearch:
    """前 failures 次调用抛“接口盒子余票接口返回错误：失败，请重试!”，之后成功。"""

    def __init__(self, failures=1):
        self.calls = []
        self.failures = failures

    async def __call__(self, depart, arrive, date, train_types):
        self.calls.append(
            (depart, arrive, date, list(train_types) if train_types else None)
        )
        if len(self.calls) <= self.failures:
            raise ProviderError("接口盒子余票接口返回错误：失败，请重试!")
        return [
            Train(
                train_type="G",
                train_no="G25",
                depart_station=depart,
                arrive_station=arrive,
                depart_date=date.isoformat(),
                depart_time="18:04",
                arrive_time="22:32",
                duration="04:28",
                seat_name="二等座",
                price=627,
                num="1",
            )
        ]


def test_search_failure_offers_retry_and_retries_with_same_input():
    search = FlakySearch(failures=1)
    engine = FlowEngine(search=search)
    mgr = SessionManager()
    session = mgr.start("u1", "origin", None)

    run(engine.handle_reply(session, FDATE_STR))
    run(engine.handle_reply(session, "北京南"))
    reply = run(engine.handle_reply(session, "苏州北"))
    # 失败时给出错误与重试指引，并保留用户已输入的信息
    assert "失败，请重试" in reply
    assert "重试" in reply
    assert FDATE_STR in reply and "北京南" in reply and "苏州北" in reply
    assert session.step == "retry"

    reply = run(engine.handle_reply(session, "重试"))
    assert "G25" in reply and "¥627" in reply
    assert session.step == "done"
    assert search.calls == [
        ("北京南", "苏州北", FDATE, None),
        ("北京南", "苏州北", FDATE, None),
    ]


def test_retry_after_failure_accepts_new_arrival_station():
    search = FlakySearch(failures=1)
    engine = FlowEngine(search=search)
    mgr = SessionManager()
    session = mgr.start("u1", "origin", None)

    run(engine.handle_reply(session, FDATE_STR))
    run(engine.handle_reply(session, "北京南"))
    run(engine.handle_reply(session, "苏州北"))
    assert session.step == "retry"

    reply = run(engine.handle_reply(session, "上海虹桥"))
    assert "G25" in reply
    assert search.calls[-1][1] == "上海虹桥"
    assert session.step == "done"


def test_repeated_failure_keeps_retry_state():
    search = FlakySearch(failures=10)
    engine = FlowEngine(search=search)
    mgr = SessionManager()
    session = mgr.start("u1", "origin", None)

    run(engine.handle_reply(session, FDATE_STR))
    run(engine.handle_reply(session, "北京南"))
    run(engine.handle_reply(session, "苏州北"))
    assert session.step == "retry"

    reply = run(engine.handle_reply(session, "重试"))
    assert "失败" in reply and "重试" in reply
    assert session.step == "retry"
