from core.formatting import (
    format_alert,
    format_call_info_line,
    format_lock_confirm,
    format_locks,
    format_query_results,
    format_quick,
    train_line,
)
from core.models import Lock, SeatPrice, Train


def make_train(price=627.0):
    return Train(
        train_type="G",
        train_no="G25",
        depart_station="北京南",
        arrive_station="苏州北",
        depart_date="2026-08-10",
        depart_time="18:04",
        arrive_time="22:32",
        duration="04:28",
        seat_name="二等座",
        price=price,
        num="1",
        prices=[
            SeatPrice(seat_name="二等座", price=price, num="1"),
            SeatPrice(seat_name="一等座", price=1003, num="无"),
        ],
    )


def test_train_line_contains_fields():
    text = train_line(make_train())
    assert "G25" in text
    assert "北京南" in text
    assert "苏州北" in text
    assert "18:04" in text
    assert "22:32" in text
    assert "¥627" in text


def test_format_query_results():
    text = format_query_results([make_train()], "2026-08-10")
    assert "高铁(G)" in text
    assert "G25" in text
    assert "¥627" in text
    assert "一等座" in text
    assert "聚合数据" in text
    assert "锁定" in text


def test_format_query_results_empty():
    text = format_query_results([], "2026-08-10")
    assert "没有查询到车次" in text


def test_format_alert():
    lock = Lock(
        user_id="u1",
        unified_msg_origin="o",
        train_type="G",
        train_no="G25",
        depart_station="北京南",
        arrive_station="苏州北",
        depart_date="2026-08-10",
        ticket_alert_enabled=True,
        ticket_alert_threshold=10,
    )
    text = format_alert(lock, 5)
    assert "G25" in text
    assert "5" in text
    assert "10" in text
    assert "余票提醒" in text


def test_format_quick_and_locks():
    lock = Lock(
        user_id="u1",
        unified_msg_origin="o",
        train_type="D",
        train_no="D3201",
        depart_station="北京南",
        arrive_station="苏州北",
        depart_date="2026-08-11",
        depart_time="14:20",
        arrive_time="17:05",
        seat_name="二等座",
        ticket_alert_enabled=True,
        ticket_alert_threshold=10,
        last_price=480,
        last_num="3",
        prices=[
            SeatPrice(seat_name="二等座", price=480, num="3"),
            SeatPrice(seat_name="一等座", price=1003, num="有"),
        ],
        updated_at="2026-08-03T12:00:00+08:00",
    )
    quick = format_quick([lock])
    assert "D3201" in quick
    assert "北京南 14:20 → 苏州北 17:05" in quick
    assert "08-11" in quick
    assert "¥480" in quick
    assert "一等座 ¥1003[余票 有]" in quick
    assert "不会调用 API" not in quick  # 缓存说明行已移除（避免强制更新输出误导）
    assert "余票提醒：开（>10张）" in quick
    assert "余票提醒：开（>10张）" in format_locks([lock])
    assert "已锁定以下车次" in format_lock_confirm([lock])


def test_locks_sorted_by_depart_time_and_numbered():
    base = dict(
        user_id="u1",
        unified_msg_origin="o",
        train_type="G",
        depart_station="西安北",
        arrive_station="上海虹桥",
        depart_date="2026-08-07",
        ticket_alert_enabled=True,
        ticket_alert_threshold=10,
        prices=[SeatPrice(seat_name="二等座", price=666, num="有")],
    )
    late = Lock(train_no="G1820", depart_time="11:10", arrive_time="18:10", **base)
    early = Lock(train_no="G1914", depart_time="06:13", arrive_time="13:22", **base)
    mid = Lock(train_no="G3286", depart_time="11:41", arrive_time="19:42", **base)
    quick = format_quick([late, early, mid])
    idx_early = quick.index("1. 08-07 G1914")
    idx_late = quick.index("2. 08-07 G1820")
    idx_mid = quick.index("3. 08-07 G3286")
    assert idx_early < idx_late < idx_mid  # 按发车时间排序并编号

    mine = format_locks([late, early, mid])
    assert "1. G1914" in mine and "2. G1820" in mine and "3. G3286" in mine


def test_format_quick_alert_off_shows_state():
    lock = Lock(
        user_id="u1",
        unified_msg_origin="o",
        train_type="G",
        train_no="G1914",
        depart_station="西安北",
        arrive_station="上海虹桥",
        depart_date="2026-08-07",
        depart_time="06:13",
        arrive_time="13:22",
        seat_name="二等座",
        ticket_alert_enabled=False,  # 已关闭余票提醒
        ticket_alert_threshold=10,
        prices=[
            SeatPrice(seat_name="商务座", price=2113, num="9"),
            SeatPrice(seat_name="一等座", price=1105, num="有"),
            SeatPrice(seat_name="二等座", price=676, num="有"),
            SeatPrice(seat_name="无座", price=676, num="有"),
        ],
        updated_at="2026-08-07T06:00:00+08:00",
    )
    quick = format_quick([lock])
    # 关闭余票提醒也要显示合计余票与阈值的关系
    assert "余票提醒：关（>10张）" in quick

    locks_txt = format_locks([lock])
    assert "G1914" in locks_txt
    assert "余票提醒：关（>10张）" in locks_txt
    assert "¥" not in locks_txt  # 我的视图不显示余票量/价格
    assert "余票" not in locks_txt.replace("余票提醒", "")


def test_format_call_info_line_and_quick_footer():
    lock = Lock(
        user_id="u1",
        unified_msg_origin="o",
        train_type="G",
        train_no="G25",
        depart_station="北京南",
        arrive_station="苏州北",
        depart_date="2026-08-11",
        depart_time="14:20",
        arrive_time="17:05",
        ticket_alert_enabled=True,
        ticket_alert_threshold=10,
        prices=[SeatPrice(seat_name="二等座", price=480, num="3")],
        updated_at="2026-08-07T06:00:00+08:00",
    )
    info = {
        "source_name": "接口盒子(apihz.cn)",
        "calls_today": 3,
        "last_api_call_at": None,
    }
    line = format_call_info_line(info, [lock])
    assert "接口盒子" in line
    assert "今日API调用次数：3 次" in line
    assert "2026-08-07 06:00:00" in line  # 无最后调用时间时回退到锁定数据更新时间

    quick = format_quick([lock], info)
    assert "今日API调用次数：3 次" in quick
    assert "数据更新时间" in quick
