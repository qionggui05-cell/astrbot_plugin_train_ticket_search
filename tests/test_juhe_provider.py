import asyncio
import datetime as dt

import pytest

from core.juhe_provider import JuheTrainProvider
from core.providers import ProviderError

# 查询日期动态取未来第 3 天，避免测试因日期过期而失效
QDATE = dt.date.today() + dt.timedelta(days=3)


def run(coro):
    return asyncio.run(coro)


def sample_result():
    return {
        "reason": "success.",
        "error_code": 0,
        "result": [
            {
                "train_no": "G25",
                "departure_station": "北京南",
                "arrival_station": "苏州北",
                "departure_time": "18:04",
                "arrival_time": "22:32",
                "duration": "04:28",
                "prices": [
                    {"seat_name": "商务座", "price": 2194, "num": "无"},
                    {"seat_name": "一等座", "price": 1003, "num": "无"},
                    {"seat_name": "二等座", "price": 627, "num": "1"},
                ],
            },
            {
                "train_no": "K101",
                "departure_station": "北京南",
                "arrival_station": "苏州北",
                "departure_time": "05:30",
                "arrival_time": "12:10",
                "duration": "06:40",
                "prices": [
                    {"seat_name": "硬座", "price": 156, "num": "有"},
                    {"seat_name": "硬卧", "price": 265, "num": "有"},
                ],
            },
        ],
    }


def test_pick_preferred_seat_and_type_filter(monkeypatch):
    p = JuheTrainProvider(config={"juhe_appkey": "key"}, plugin_dir=".")
    monkeypatch.setattr(p, "_get_json", lambda params: sample_result())
    trains = run(p.search("北京南", "苏州北", QDATE, ["G"]))
    assert len(trains) == 1
    t = trains[0]
    assert t.train_no == "G25"
    assert t.seat_name == "二等座"
    assert t.price == 627
    assert t.num == "1"
    assert t.train_type == "G"


def test_all_types_without_filter(monkeypatch):
    p = JuheTrainProvider(config={"juhe_appkey": "key"}, plugin_dir=".")
    monkeypatch.setattr(p, "_get_json", lambda params: sample_result())
    trains = run(p.search("北京南", "苏州北", QDATE, None))
    assert {t.train_no for t in trains} == {"G25", "K101"}
    k101 = next(t for t in trains if t.train_no == "K101")
    assert k101.seat_name == "硬座"
    assert k101.price == 156


def test_station_code_uses_search_type_2(monkeypatch):
    p = JuheTrainProvider(config={"juhe_appkey": "key"}, plugin_dir=".")
    seen = {}

    def fake_get(params):
        seen.update(params)
        return sample_result()

    monkeypatch.setattr(p, "_get_json", fake_get)
    run(p.search("VNP", "OHH", QDATE, None))
    assert seen["search_type"] == "2"
    assert seen["departure_station"] == "VNP"
    assert seen["enable_booking"] == "2"


def test_missing_key_raises():
    p = JuheTrainProvider(config={}, plugin_dir=".")
    with pytest.raises(ProviderError):
        run(p.search("北京南", "苏州北", QDATE, None))


def test_placeholder_key_raises():
    p = JuheTrainProvider(config={"juhe_appkey": "xxxxxxx"}, plugin_dir=".")
    with pytest.raises(ProviderError, match="juhe_appkey"):
        run(p.search("北京南", "苏州北", QDATE, None))


def test_date_beyond_limit_raises(monkeypatch):
    p = JuheTrainProvider(config={"juhe_appkey": "key"}, plugin_dir=".")
    monkeypatch.setattr(p, "_get_json", lambda params: sample_result())
    far = dt.date.today() + dt.timedelta(days=20)
    with pytest.raises(ProviderError):
        run(p.search("北京南", "苏州北", far, None))


def test_quota_error_raises_friendly(monkeypatch):
    p = JuheTrainProvider(config={"juhe_appkey": "key"}, plugin_dir=".")
    monkeypatch.setattr(
        p,
        "_get_json",
        lambda params: {"error_code": 10012, "reason": "请求超过次数限制"},
    )
    with pytest.raises(ProviderError, match="次数已达上限"):
        run(p.search("北京南", "苏州北", QDATE, None))


def test_no_result_returns_empty(monkeypatch):
    p = JuheTrainProvider(config={"juhe_appkey": "key"}, plugin_dir=".")
    monkeypatch.setattr(p, "_get_json", lambda params: {"error_code": 0, "result": []})
    trains = run(p.search("北京南", "苏州北", QDATE, None))
    assert trains == []


def test_sold_out_trains_still_listed(monkeypatch):
    p = JuheTrainProvider(config={"juhe_appkey": "key"}, plugin_dir=".")
    result = {
        "error_code": 0,
        "result": [
            {
                "train_no": "G1",
                "departure_station": "北京南",
                "arrival_station": "苏州北",
                "departure_time": "07:00",
                "arrival_time": "11:00",
                "duration": "04:00",
                "prices": [
                    {"seat_name": "二等座", "price": 666, "num": "无"},
                    {"seat_name": "无座", "price": 666, "num": "无"},
                ],
            }
        ],
    }
    monkeypatch.setattr(p, "_get_json", lambda params: result)
    trains = run(p.search("北京南", "苏州北", QDATE, None))
    assert len(trains) == 1
    assert trains[0].train_no == "G1"
    assert trains[0].num == "无"
    assert trains[0].price == 666


def test_sold_out_train_with_zero_prices_still_listed(monkeypatch):
    """已售罄且票价为 0 的车次也应返回并保留余票信息（可显示、可提醒）。"""
    p = JuheTrainProvider(config={"juhe_appkey": "key"}, plugin_dir=".")
    result = {
        "error_code": 0,
        "result": [
            {
                "train_no": "D114",
                "departure_station": "西安",
                "arrival_station": "上海松江",
                "departure_time": "00:33",
                "arrival_time": "16:23",
                "duration": "15:50",
                "prices": [
                    {"seat_name": "二等座", "price": 0, "num": "无"},
                    {"seat_name": "无座", "price": 0, "num": "无"},
                ],
            }
        ],
    }
    monkeypatch.setattr(p, "_get_json", lambda params: result)
    trains = run(p.search("西安", "上海松江", QDATE, None))
    assert len(trains) == 1
    assert trains[0].train_no == "D114"
    assert trains[0].num == "无"
    assert [s.num for s in trains[0].prices] == ["无", "无"]


def test_wz_price_fallback_when_zero_with_stock(monkeypatch):
    """无座有余票但票价为 0 时按硬座票价兜底，避免有余票却显示 0 元。"""
    p = JuheTrainProvider(config={"juhe_appkey": "key"}, plugin_dir=".")
    result = {
        "error_code": 0,
        "result": [
            {
                "train_no": "K200",
                "departure_station": "西安",
                "arrival_station": "上海",
                "departure_time": "09:00",
                "arrival_time": "20:00",
                "duration": "11:00",
                "prices": [
                    {"seat_name": "硬座", "price": 156, "num": "无"},
                    {"seat_name": "无座", "price": 0, "num": "有"},
                ],
            }
        ],
    }
    monkeypatch.setattr(p, "_get_json", lambda params: result)
    trains = run(p.search("西安", "上海", QDATE, None))
    wz = next(s for s in trains[0].prices if s.seat_name == "无座")
    assert wz.num == "有"
    assert wz.price == 156
