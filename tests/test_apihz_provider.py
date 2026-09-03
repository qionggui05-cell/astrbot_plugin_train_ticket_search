import asyncio
import datetime as dt

import pytest

from core.apihz_provider import ApiHzTrainProvider
from core.providers import ProviderError

# 查询日期动态取未来第 3 天，避免测试因日期过期而失效
QDATE = dt.date.today() + dt.timedelta(days=3)
QDATE_STR = QDATE.isoformat()
QDATE_PREV_STR = (QDATE - dt.timedelta(days=1)).isoformat()


def run(coro):
    return asyncio.run(coro)


def sample_avail():
    return {
        "code": 200,
        "td": "本地模式",
        "datas": [
            {
                "train_number": "G3286",
                "train_order": "76000G32860A",
                "depart_index": "04",
                "arrive_index": "24",
                "depart_name": "西安北",
                "arrive_name": "上海虹桥",
                "depart_time": "11:41",
                "arrive_time": "19:42",
                "duration": "08:01",
                "seatcode": "9MOO",
                "date": QDATE_STR,
                "seats": [
                    {"type": "商务座(特等座)", "stock": 14},
                    {"type": "一等座", "stock": 4},
                    {"type": "二等座(二等包座)", "stock": -1},
                    {"type": "高级软卧", "stock": 0},
                    {"type": "软卧(动卧一等卧)", "stock": 0},
                    {"type": "硬卧(二等卧)", "stock": 0},
                    {"type": "软座", "stock": 0},
                    {"type": "硬座", "stock": 0},
                    {"type": "无座", "stock": -1},
                    {"type": "优选一等座", "stock": 0},
                ],
            },
            {
                "train_number": "K284",
                "train_order": "760000K2840I",
                "depart_index": "03",
                "arrive_index": "25",
                "depart_name": "西安",
                "arrive_name": "上海",
                "depart_time": "20:28",
                "arrive_time": "06:53",
                "duration": "34:25",
                "seatcode": "1431",
                "date": QDATE_STR,
                "seats": [
                    {"type": "商务座(特等座)", "stock": 0},
                    {"type": "一等座", "stock": 0},
                    {"type": "二等座(二等包座)", "stock": 0},
                    {"type": "高级软卧", "stock": 0},
                    {"type": "软卧(动卧一等卧)", "stock": 16},
                    {"type": "硬卧(二等卧)", "stock": -1},
                    {"type": "软座", "stock": 0},
                    {"type": "硬座", "stock": -1},
                    {"type": "无座", "stock": -1},
                    {"type": "优选一等座", "stock": 0},
                ],
            },
        ],
    }


def sample_prices():
    return {
        "code": 200,
        "td": "本地模式",
        "datas": [
            {
                "train_number": "76000G32860A",
                "train_order": "G3286",
                "depart_name": "西安北",
                "arrive_name": "上海虹桥",
                "depart_time": "11:41",
                "arrive_time": "19:42",
                "day_difference": "0",
                "train_type": "高速",
                "alltime": "08:01",
                "edz": "666.00",
                "ydz": "1062.00",
                "tdz": "2068.00",
                "yz": "0.00",
                "yw": "0.00",
                "rw": "0.00",
                "wz": "666.00",
                "ydzs": "0.00",
            },
            {
                "train_number": "760000K2840I",
                "train_order": "K284",
                "depart_name": "西安",
                "arrive_name": "上海",
                "depart_time": "20:28",
                "arrive_time": "06:53",
                "day_difference": "2",
                "train_type": "快速",
                "alltime": "34:25",
                "edz": "0.00",
                "ydz": "0.00",
                "tdz": "0.00",
                "yz": "268.50",
                "yw": "487.50",
                "rw": "751.50",
                "wz": "0.00",
                "ydzs": "0.00",
            },
        ],
    }


def make_provider():
    return ApiHzTrainProvider(
        config={
            "apihz_id": "10019837",
            "apihz_key": "2b71a369ab75dbc3d968a0d52bc9b04e",
        },
        plugin_dir=".",
    )


def test_missing_credentials_raises():
    p = ApiHzTrainProvider(config={}, plugin_dir=".")
    with pytest.raises(ProviderError):
        run(p.search("西安北", "上海虹桥", QDATE, None))


def test_placeholder_credentials_raise():
    p = ApiHzTrainProvider(
        config={"apihz_id": "xxxxxxx", "apihz_key": "xxxxxxx"}, plugin_dir="."
    )
    with pytest.raises(ProviderError, match="apihz_id"):
        run(p.search("西安北", "上海虹桥", QDATE, None))


def test_parse_availability_and_prices(monkeypatch):
    p = make_provider()
    calls = []

    async def fake_get(url, params):
        calls.append(url)
        if url.endswith("/api.php"):
            return sample_avail()
        return sample_prices()

    monkeypatch.setattr(p, "_get_json", fake_get)
    trains = run(p.search("西安北", "上海虹桥", QDATE, None))

    # 默认开启余票票价补查：K284 的 无座 公示票价缺失，追加 api2 补查 1 次
    assert len(calls) == 3  # 余票 1 次 + 公示票价 1 次 + 无座票价补查 1 次
    assert any(url.endswith("/api2.php") for url in calls)
    assert len(trains) == 2
    g = next(t for t in trains if t.train_no == "G3286")
    assert g.train_type == "G"
    assert g.depart_time == "11:41"
    assert g.arrive_time == "19:42"
    assert g.duration == "08:01"
    seat_map = {s.seat_name: s for s in g.prices}
    assert seat_map["商务座"].price == 2068
    assert seat_map["商务座"].num == "14"
    assert seat_map["一等座"].num == "4"
    assert seat_map["二等座"].price == 666
    assert seat_map["二等座"].num == "有"  # stock=-1 显示为 有
    assert seat_map["无座"].num == "有"
    assert seat_map["无座"].price == 666  # 公示票价 wz 已知
    # G 字头车次只展示 二等座/一等座/商务座/无座，
    # 余票接口返回的幽灵席别（余票 无 且无票价）应全部隐藏
    assert set(seat_map) == {"二等座", "一等座", "商务座", "无座"}
    assert "高级软卧" not in seat_map
    assert "软卧" not in seat_map
    assert "硬卧" not in seat_map
    assert "硬座" not in seat_map
    assert "优选一等座" not in seat_map
    assert "软座" not in seat_map
    assert g.seat_name == "二等座"  # 跟踪席别优先二等座
    assert g.num == "有"

    k = next(t for t in trains if t.train_no == "K284")
    assert k.train_type == "K"
    seat_map_k = {s.seat_name: s for s in k.prices}
    assert seat_map_k["硬座"].price == 268.5
    assert seat_map_k["硬座"].num == "有"
    assert seat_map_k["硬卧"].num == "有"
    assert seat_map_k["软卧"].num == "16"


def test_train_type_filter(monkeypatch):
    p = make_provider()

    async def fake_get(url, params):
        return sample_avail() if url.endswith("/api.php") else sample_prices()

    monkeypatch.setattr(p, "_get_json", fake_get)
    trains = run(p.search("西安北", "上海虹桥", QDATE, ["G"]))
    assert [t.train_no for t in trains] == ["G3286"]


def test_price_api_failure_degrades(monkeypatch):
    p = make_provider()
    calls = {"n": 0}

    async def fake_get(url, params):
        if url.endswith("/api.php"):
            calls["n"] += 1
            return sample_avail()
        raise ProviderError("网络错误")

    monkeypatch.setattr(p, "_get_json", fake_get)
    trains = run(p.search("西安北", "上海虹桥", QDATE, None))
    assert len(trains) == 2
    g = next(t for t in trains if t.train_no == "G3286")
    assert g.prices[0].price == 0  # 票价接口失败，票价留空
    assert g.prices[0].num == "有"  # 余票信息仍完整（二等座 stock=-1）


def test_quota_error_raises_friendly(monkeypatch):
    p = make_provider()

    async def fake_get(url, params):
        return {"code": 400, "msg": "通讯秘钥错误。"}

    monkeypatch.setattr(p, "_get_json", fake_get)
    with pytest.raises(ProviderError, match="通讯秘钥错误"):
        run(p.search("西安北", "上海虹桥", QDATE, None))


def test_call_count_tracking(monkeypatch):
    p = make_provider()

    async def fake_get(url, params):
        return sample_avail() if url.endswith("/api.php") else sample_prices()

    monkeypatch.setattr(p, "_get_json", fake_get)
    run(p.search("西安北", "上海虹桥", QDATE, None))
    assert p.calls_today == 3  # 余票 + 公示票价 + 无座票价补查
    assert p.last_api_call_at is not None
    info = p.call_info()
    assert info["calls_today"] == 3
    assert "接口盒子" in str(info["source_name"])


def sample_sold_out():
    return {
        "code": 200,
        "datas": [
            {
                "train_number": "G300",
                "train_order": "76000G3000A",
                "depart_index": "04",
                "arrive_index": "24",
                "depart_name": "西安北",
                "arrive_name": "上海虹桥",
                "depart_time": "09:00",
                "arrive_time": "15:00",
                "duration": "06:00",
                "seatcode": "9MOO",
                "date": QDATE_STR,
                "seats": [
                    {"type": "商务座(特等座)", "stock": 0},
                    {"type": "一等座", "stock": 0},
                    {"type": "二等座(二等包座)", "stock": 0},
                    {"type": "高级软卧", "stock": 0},
                    {"type": "软卧(动卧一等卧)", "stock": 0},
                    {"type": "硬卧(二等卧)", "stock": 0},
                    {"type": "软座", "stock": 0},
                    {"type": "硬座", "stock": 0},
                    {"type": "无座", "stock": 0},
                    {"type": "优选一等座", "stock": 0},
                ],
            }
        ],
    }


def sample_sold_out_prices():
    return {
        "code": 200,
        "datas": [
            {
                "train_number": "76000G3000A",
                "train_order": "G300",
                "edz": "666.00",
                "ydz": "1062.00",
                "tdz": "2068.00",
                "yz": "0.00",
                "yw": "0.00",
                "rw": "0.00",
                "wz": "666.00",
            }
        ],
    }


def test_sold_out_train_seats_still_shown(monkeypatch):
    """已售罄车次：公示票价存在的席别即使余票为 无 也展示，避免无票车次消失。"""
    p = make_provider()

    async def fake_get(url, params):
        if url.endswith("/api.php"):
            return sample_sold_out()
        return sample_sold_out_prices()

    monkeypatch.setattr(p, "_get_json", fake_get)
    trains = run(p.search("西安北", "上海虹桥", QDATE, None))
    assert len(trains) == 1
    seats = {s.seat_name: s for s in trains[0].prices}
    assert set(seats) == {
        "商务座",
        "一等座",
        "二等座",
        "无座",
    }  # 无票车次仍显示 G 字头真实席别（公示票价已知），幽灵席别被过滤
    assert seats["二等座"].num == "无"
    assert seats["二等座"].price == 666
    assert "高级软卧" not in seats
    assert "优选一等座" not in seats


def test_detail_price_fallback_when_enabled(monkeypatch):
    """启用余票票价补查时，公示票价缺失但有余票的席别按 api2 返回票价。"""
    p = ApiHzTrainProvider(
        config={
            "apihz_id": "10019837",
            "apihz_key": "2b71a369ab75dbc3d968a0d52bc9b04e",
            "apihz_use_detail_price": True,
        },
        plugin_dir=".",
    )
    calls = []

    async def fake_get(url, params):
        calls.append(url)
        if url.endswith("/api.php"):
            return sample_avail()
        if url.endswith("/api4.php"):
            # 公示票价缺失 无座 字段（无座价格未知）
            data = sample_prices()
            for d in data["datas"]:
                d.pop("wz", None)
            return data
        # api2 余票票价：返回无座真实票价
        return {"code": 200, "train_order": params.get("train_order"), "wz": "¥666.0"}

    monkeypatch.setattr(p, "_get_json", fake_get)
    trains = run(p.search("西安北", "上海虹桥", QDATE, None))
    g = next(t for t in trains if t.train_no == "G3286")
    seat_map = {s.seat_name: s for s in g.prices}
    assert seat_map["无座"].num == "有"
    assert seat_map["无座"].price == 666  # 补查得到无座票价
    assert any(url.endswith("/api2.php") for url in calls)


def test_uses_query_date_not_api_date(monkeypatch):
    """接口返回的 datas.date 对部分车次（如凌晨发车的 D114）会错位成前一天，
    锁定/提醒应统一使用用户查询的日期，而不是接口的 date 字段。"""
    p = make_provider()
    avail = sample_avail()
    for item in avail["datas"]:
        item["date"] = QDATE_PREV_STR  # 模拟接口把 8-7 查询标成 8-6

    async def fake_get(url, params):
        return avail if url.endswith("/api.php") else sample_prices()

    monkeypatch.setattr(p, "_get_json", fake_get)
    trains = run(p.search("西安北", "上海虹桥", QDATE, None))
    assert len(trains) == 2
    assert all(t.depart_date == QDATE_STR for t in trains)


def test_wz_price_fallback_when_detail_missing(monkeypatch):
    """公示票价接口不返回无座（无 wz 字段）且未启用/取不到 api2 时，
    无座有余票也按硬座/二等座票价兜底，避免显示 0 元。"""
    p = ApiHzTrainProvider(
        config={
            "apihz_id": "10019837",
            "apihz_key": "2b71a369ab75dbc3d968a0d52bc9b04e",
            "apihz_use_detail_price": False,
        },
        plugin_dir=".",
    )
    prices = sample_prices()
    for d in prices["datas"]:
        d.pop("wz", None)  # 公示票价接口没有 wz 字段

    async def fake_get(url, params):
        return sample_avail() if url.endswith("/api.php") else prices

    monkeypatch.setattr(p, "_get_json", fake_get)
    trains = run(p.search("西安北", "上海虹桥", QDATE, None))
    g = next(t for t in trains if t.train_no == "G3286")
    k = next(t for t in trains if t.train_no == "K284")
    g_seats = {s.seat_name: s.price for s in g.prices}
    k_seats = {s.seat_name: s.price for s in k.prices}
    assert g_seats["无座"] == 666  # 高铁按二等座票价
    assert k_seats["无座"] == 268.5  # 普速按硬座票价


def test_availability_transient_error_retries(monkeypatch):
    """余票接口返回“失败，请重试!”时自动重试一次，成功后正常返回。"""
    p = make_provider()
    calls = {"n": 0}

    async def fake_get(url, params):
        calls["n"] += 1
        if url.endswith("/api.php"):
            if calls["n"] == 1:
                return {"code": 400, "msg": "失败，请重试!"}
            return sample_avail()
        return sample_prices()

    monkeypatch.setattr(p, "_get_json", fake_get)
    trains = run(p.search("西安北", "上海虹桥", QDATE, None))
    # 余票首次失败重试 1 次 + 公示票价 + K284 无座补查 = 4 次
    assert calls["n"] == 4
    assert len(trains) == 2


def test_availability_error_raises_after_retries(monkeypatch):
    """余票接口持续返回“失败，请重试!”时，重试一次后抛出友好错误。"""
    p = make_provider()
    calls = {"n": 0}

    async def fake_get(url, params):
        calls["n"] += 1
        return {"code": 400, "msg": "失败，请重试!"}

    monkeypatch.setattr(p, "_get_json", fake_get)
    with pytest.raises(ProviderError, match="失败，请重试"):
        run(p.search("西安北", "上海虹桥", QDATE, None))
    assert calls["n"] == 2


def test_detail_price_rate_limit_stops_further_calls(monkeypatch):
    """api2 返回频次限制错误时，本次查询停止后续补查（避免继续刷频次），
    无座仍按硬座/二等座票价兜底。"""
    p = make_provider()
    calls = {"api2": 0}

    async def fake_get(url, params):
        if url.endswith("/api.php"):
            return sample_avail()
        if url.endswith("/api4.php"):
            data = sample_prices()
            for d in data["datas"]:
                d.pop("wz", None)
            return data
        calls["api2"] += 1
        return {"code": 400, "msg": "调用频次过快，请56秒后再试，或购买钻石会员！"}

    monkeypatch.setattr(p, "_get_json", fake_get)
    trains = run(p.search("西安北", "上海虹桥", QDATE, None))
    assert calls["api2"] == 1  # 第一个车次触发限流后停止
    assert len(trains) == 2
    k = next(t for t in trains if t.train_no == "K284")
    k_seats = {s.seat_name: s for s in k.prices}
    assert k_seats["无座"].num == "有"
    assert k_seats["无座"].price == 268.5  # 兜底价仍生效


def test_detail_price_capped_per_search(monkeypatch):
    """每次查询 api2 补查不超过配置上限，避免瞬间打满接口频次。"""
    p = ApiHzTrainProvider(
        config={
            "apihz_id": "10019837",
            "apihz_key": "2b71a369ab75dbc3d968a0d52bc9b04e",
            "apihz_detail_price_max_calls": 1,
        },
        plugin_dir=".",
    )
    calls = {"api2": 0}

    async def fake_get(url, params):
        if url.endswith("/api.php"):
            return sample_avail()
        if url.endswith("/api4.php"):
            data = sample_prices()
            for d in data["datas"]:
                d.pop("wz", None)
            return data
        calls["api2"] += 1
        return {
            "code": 200,
            "train_order": params.get("train_order"),
            "wz": "666.0",
        }

    monkeypatch.setattr(p, "_get_json", fake_get)
    trains = run(p.search("西安北", "上海虹桥", QDATE, None))
    assert calls["api2"] == 1
    assert len(trains) == 2


def sample_d_train():
    return {
        "code": 200,
        "datas": [
            {
                "train_number": "D114",
                "train_order": "76000D1140A",
                "depart_index": "02",
                "arrive_index": "30",
                "depart_name": "西安",
                "arrive_name": "上海松江",
                "depart_time": "00:33",
                "arrive_time": "16:23",
                "duration": "15:50",
                "seatcode": "1410",
                "date": QDATE_STR,
                "seats": [
                    {"type": "商务座(特等座)", "stock": 0},
                    {"type": "一等座", "stock": 0},
                    {"type": "二等座(二等包座)", "stock": -1},
                    {"type": "软卧(动卧一等卧)", "stock": 3},
                    {"type": "硬卧(二等卧)", "stock": 0},
                    {"type": "软座", "stock": 0},
                    {"type": "硬座", "stock": 0},
                    {"type": "无座", "stock": -1},
                ],
            }
        ],
    }


def sample_d_prices():
    return {
        "code": 200,
        "datas": [
            {
                "train_number": "76000D1140A",
                "train_order": "D114",
                "edz": "240.00",
                "ydz": "0.00",
                "tdz": "0.00",
                "yz": "0.00",
                "yw": "400.00",
                "rw": "596.00",
                "wz": "0.00",
            }
        ],
    }


def test_d_train_seat_names_and_ghost_filter(monkeypatch):
    """D 字头车次只展示 二等座/二等卧/一等卧/无座；卧铺按动车席别命名，
    余票接口返回的幽灵席别（商务座/一等座/硬座等 stock=0）全部过滤。"""
    p = make_provider()

    async def fake_get(url, params):
        return sample_d_train() if url.endswith("/api.php") else sample_d_prices()

    monkeypatch.setattr(p, "_get_json", fake_get)
    trains = run(p.search("西安", "上海松江", QDATE, ["D"]))
    assert len(trains) == 1
    d = trains[0]
    assert d.train_type == "D"
    seat_map = {s.seat_name: s for s in d.prices}
    # D 类白名单：二等座/二等卧/一等卧/无座
    assert set(seat_map) == {"二等座", "二等卧", "一等卧", "无座"}
    assert seat_map["二等座"].price == 240
    assert seat_map["二等座"].num == "有"
    # 卧铺席别按动车命名展示，票价取对应公示字段（硬卧->yw、软卧->rw）
    assert seat_map["二等卧"].price == 400
    assert seat_map["二等卧"].num == "无"
    assert seat_map["一等卧"].price == 596
    assert seat_map["一等卧"].num == "3"
    # 无座有余票、公示票价缺失：api2 补查（fake 未提供）后按二等座兜底
    assert seat_map["无座"].num == "有"
    assert seat_map["无座"].price == 240
