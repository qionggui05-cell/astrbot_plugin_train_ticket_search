import datetime as dt

from core.models import Lock, SeatPrice
from core.storage import LockStorage

# 锁定日期动态取未来日期，避免测试因日期过期而失效
FD1 = (dt.date.today() + dt.timedelta(days=2)).isoformat()
FD2 = (dt.date.today() + dt.timedelta(days=3)).isoformat()


def make_lock(user="u1", train="G25"):
    return Lock(
        user_id=user,
        unified_msg_origin="origin",
        train_type="G",
        train_no=train,
        depart_station="北京南",
        arrive_station="苏州北",
        depart_date=FD1,
        depart_time="18:04",
        arrive_time="22:32",
        ticket_alert_enabled=True,
        ticket_alert_threshold=10,
        last_price=560,
        prices=[
            SeatPrice(seat_name="二等座", price=560, num="3"),
            SeatPrice(seat_name="一等座", price=1003, num="有"),
        ],
    )


def test_roundtrip(tmp_path):
    path = str(tmp_path / "locks.json")
    s = LockStorage(path)
    assert s.add_lock(make_lock())
    s2 = LockStorage(path)
    locks = s2.all_locks()
    assert len(locks) == 1
    assert locks[0].train_no == "G25"
    assert locks[0].depart_time == "18:04"
    assert locks[0].arrive_time == "22:32"
    assert locks[0].ticket_alert_enabled is True
    assert locks[0].ticket_alert_threshold == 10
    assert locks[0].prices[0].seat_name == "二等座"
    assert locks[0].prices[1].num == "有"
    assert locks[0].last_price == 560
    assert locks[0].unified_msg_origin == "origin"


def test_duplicate_not_added(tmp_path):
    s = LockStorage(str(tmp_path / "locks.json"))
    assert s.add_lock(make_lock())
    assert not s.add_lock(make_lock())
    assert len(s.all_locks()) == 1


def test_corrupt_file_backed_up(tmp_path):
    path = tmp_path / "locks.json"
    path.write_text("{broken json", encoding="utf-8")
    s = LockStorage(str(path))
    assert s.all_locks() == []
    assert len(list(tmp_path.glob("locks.json.corrupt-*"))) == 1


def test_remove_and_set_ticket_alert(tmp_path):
    s = LockStorage(str(tmp_path / "locks.json"))
    s.add_lock(make_lock(train="G25"))
    s.add_lock(make_lock(train="D3201"))
    assert len(s.all_locks()) == 2
    assert s.remove_locks("u1", {"G25"}) == 1
    assert len(s.all_locks()) == 1
    assert s.set_ticket_alert("u1", False, None) == 1
    assert s.all_locks()[0].ticket_alert_enabled is False
    assert s.all_locks()[0].alert_armed is True


def test_remove_with_date_filter(tmp_path):
    s = LockStorage(str(tmp_path / "locks.json"))
    s.add_lock(make_lock(train="G25"))
    lock2 = make_lock(train="G25")
    lock2.depart_date = FD2
    s.add_lock(lock2)
    assert s.remove_locks("u1", {"G25"}, date_filter=FD1) == 1
    assert len(s.all_locks()) == 1
    assert s.all_locks()[0].depart_date == FD2


def test_set_ticket_alert_with_date_filter(tmp_path):
    s = LockStorage(str(tmp_path / "locks.json"))
    s.add_lock(make_lock(train="G25"))
    lock2 = make_lock(train="G25")
    lock2.depart_date = FD2
    s.add_lock(lock2)

    # 只关闭 2026-08-10 那天的余票提醒
    assert s.set_ticket_alert("u1", False, {"G25"}, date_filter=FD1) == 1
    by_date = {l.depart_date: l for l in s.all_locks()}
    assert by_date[FD1].ticket_alert_enabled is False
    assert by_date[FD2].ticket_alert_enabled is True


def test_remove_departed(tmp_path):
    s = LockStorage(str(tmp_path / "locks.json"))
    departed = make_lock(train="G25")
    departed.depart_date = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    departed.depart_time = "10:00"
    s.add_lock(departed)
    s.add_lock(make_lock(train="D3201"))

    removed = s.remove_departed("u1")
    assert len(removed) == 1
    assert removed[0].train_no == "G25"
    assert len(s.all_locks()) == 1
    assert s.all_locks()[0].train_no == "D3201"
