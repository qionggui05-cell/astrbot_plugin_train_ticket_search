import asyncio
import datetime as dt
import json

from core.demo_provider import DemoProvider


def run(coro):
    return asyncio.run(coro)


def test_search_returns_trains_for_route_date():
    p = DemoProvider(config={}, plugin_dir=".")
    trains = run(p.search("北京南", "苏州北", dt.date(2026, 8, 10), ["G", "D"]))
    assert len(trains) > 0
    types = {t.train_type for t in trains}
    assert {"G", "D"} == types
    assert all(t.depart_station == "北京南" and t.arrive_station == "苏州北" for t in trains)
    assert all(t.depart_date == "2026-08-10" for t in trains)
    assert all(t.price > 0 for t in trains)


def test_type_filter_respected():
    p = DemoProvider(config={}, plugin_dir=".")
    trains = run(p.search("北京南", "苏州北", dt.date(2026, 8, 10), ["G"]))
    assert len(trains) > 0
    assert all(t.train_type == "G" for t in trains)


def test_deterministic_output():
    p = DemoProvider(config={}, plugin_dir=".")
    a = run(p.search("北京南", "苏州北", dt.date(2026, 8, 10), ["G"]))
    b = run(p.search("北京南", "苏州北", dt.date(2026, 8, 10), ["G"]))
    assert [(t.train_no, t.price, t.depart_time) for t in a] == [
        (t.train_no, t.price, t.depart_time) for t in b
    ]


def test_fixture_trains(tmp_path):
    fixture = tmp_path / "fixtures.json"
    fixture.write_text(
        json.dumps(
            {
                "flights": [
                    {
                        "date": "2026-09-05",
                        "train_type": "G",
                        "train_no": "G9801",
                        "depart_time": "16:45",
                        "arrive_time": "19:35",
                        "duration": "02:50",
                        "price": 460,
                        "depart": "北京南",
                        "arrive": "苏州北",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    p = DemoProvider(config={"demo_override_file": str(fixture)}, plugin_dir=str(tmp_path))
    trains = run(p.search("北京南", "苏州北", dt.date(2026, 9, 5), ["G"]))
    assert any(t.train_no == "G9801" and t.price == 460 for t in trains)
    # 未绑定线路时（缺 depart/arrive）任意线路都会出现
    fixture.write_text(
        json.dumps(
            {
                "flights": [
                    {
                        "date": "2026-09-05",
                        "train_type": "G",
                        "train_no": "G9801",
                        "depart_time": "16:45",
                        "arrive_time": "19:35",
                        "duration": "02:50",
                        "price": 460,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    p2 = DemoProvider(config={"demo_override_file": str(fixture)}, plugin_dir=str(tmp_path))
    assert any(
        t.train_no == "G9801"
        for t in run(p2.search("上海虹桥", "杭州东", dt.date(2026, 9, 5), ["G"]))
    )


def test_legacy_price_override(tmp_path):
    override = tmp_path / "overrides.json"
    p0 = DemoProvider(config={}, plugin_dir=str(tmp_path))
    trains = run(p0.search("北京南", "苏州北", dt.date(2026, 8, 10), ["G"]))
    target = trains[0]
    override.write_text(json.dumps({target.train_no: 199}), encoding="utf-8")
    p1 = DemoProvider(config={"demo_override_file": str(override)}, plugin_dir=str(tmp_path))
    trains2 = run(p1.search("北京南", "苏州北", dt.date(2026, 8, 10), ["G"]))
    match = next(t for t in trains2 if t.train_no == target.train_no)
    assert match.price == 199
