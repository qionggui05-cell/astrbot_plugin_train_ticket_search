from core.alert_engine import BIG_NUM, evaluate, parse_num, total_tickets
from core.models import Lock, SeatPrice


def make_lock(alert=True, threshold=10, last_num="", armed=True):
    return Lock(
        user_id="u1",
        unified_msg_origin="origin",
        train_type="G",
        train_no="G25",
        depart_station="北京南",
        arrive_station="苏州北",
        depart_date="2026-08-10",
        ticket_alert_enabled=alert,
        ticket_alert_threshold=threshold,
        alert_armed=armed,
        last_num=last_num,
    )


def seats(*nums):
    return [
        SeatPrice(seat_name=f"席{i + 1}", price=100, num=n)
        for i, n in enumerate(nums)
    ]


def test_parse_num():
    assert parse_num("无") == 0
    assert parse_num("5") == 5
    assert parse_num("有") == BIG_NUM
    assert parse_num("有") > 10
    assert parse_num("") == 0
    assert parse_num("abc") == 0


def test_total_tickets_sums_all_seats():
    assert total_tickets(seats("无", "3", "有")) == 3 + BIG_NUM
    assert total_tickets(seats("无", "4")) == 4
    assert total_tickets([]) == 0


def test_trigger_once_when_total_below_threshold():
    lock = make_lock(last_num="12")
    events = evaluate([lock], {lock.key(): seats("5", "无")})
    assert len(events) == 1
    assert events[0].new_total == 5
    assert lock.alert_armed is False
    assert lock.prices[0].num == "5"
    assert lock.updated_at is not None


def test_sold_out_triggers():
    lock = make_lock()
    events = evaluate([lock], {lock.key(): seats("无", "无")})
    assert len(events) == 1
    assert events[0].new_total == 0
    assert lock.alert_armed is False


def test_any_seat_has_tickets_means_enough():
    """任一座位显示"有"或合计 >= 阈值时不触发提醒。"""
    lock = make_lock()
    events = evaluate([lock], {lock.key(): seats("无", "有")})
    assert events == []
    assert lock.alert_armed is True


def test_no_repeat_while_below():
    lock = make_lock(armed=False, last_num="5")
    events = evaluate([lock], {lock.key(): seats("4")})
    assert events == []
    assert lock.last_num == "4"


def test_rearm_when_above_threshold():
    lock = make_lock(armed=False, last_num="5")
    evaluate([lock], {lock.key(): seats("12")})
    assert lock.alert_armed is True
    events = evaluate([lock], {lock.key(): seats("8")})
    assert len(events) == 1


def test_disabled_alert_updates_data_only():
    lock = make_lock(alert=False, last_num="5")
    events = evaluate([lock], {lock.key(): seats("3", "无")})
    assert events == []
    assert lock.prices[0].num == "3"
    assert lock.last_num == "3"
    assert lock.updated_at is not None


def test_missing_data_keeps_state():
    lock = make_lock(armed=True, last_num="5")
    events = evaluate([lock], {})
    assert events == []
    assert lock.alert_armed is True
    assert lock.last_num == "5"
