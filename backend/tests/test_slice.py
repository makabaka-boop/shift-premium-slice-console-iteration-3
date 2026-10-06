"""逐分钟对拍：用独立 oracle 重新裁决每一分钟，再与引擎分段结果核对。"""

from __future__ import annotations

from datetime import datetime, timedelta
from fractions import Fraction
from math import gcd

from fastapi.testclient import TestClient

from app.engine import BASIS_POINT_DENOMINATOR, MINUTES_PER_HOUR, Rule, slice_shift
from app.main import app

client = TestClient(app)


def oracle_cover(rule: dict, weekday: int, m: int) -> bool:
    """与引擎实现相互独立的覆盖判定（直接按定义写）。"""
    s, e = rule["start_minute"], rule["end_minute"]
    days = set(rule["weekdays"])
    if s < e:
        return s <= m < e and weekday in days
    # 跨午夜：当天 [s,1440) ∪ 次日 [0,e)
    return (m >= s and weekday in days) or (m < e and (weekday - 1) % 7 in days)


def oracle_minute(dt: datetime, rules: list[dict]) -> tuple[str | None, int]:
    wd, m = dt.weekday(), dt.hour * 60 + dt.minute
    hits = [r for r in rules if oracle_cover(r, wd, m)]
    if not hits:
        return None, 10000
    hits.sort(key=lambda r: (-r["priority"], r["id"].encode("utf-8")))
    win = hits[0]
    return win["id"], win["basis_points"]


def assert_matches_oracle(start, end, base, rules):
    """引擎结果必须与逐分钟 oracle 完全一致：分段边界、段内分钟、分数总额。"""
    engine_rules = [
        Rule(r["id"], frozenset(r["weekdays"]), r["start_minute"],
             r["end_minute"], r["priority"], r["basis_points"])
        for r in rules
    ]
    segments, total = slice_shift(start, end, base, engine_rules)

    # oracle 逐分钟跑一遍
    expected_runs: list[dict] = []
    cur = start
    while cur < end:
        rid, bp = oracle_minute(cur, rules)
        key = (cur.date(), rid, bp)
        if not expected_runs or expected_runs[-1]["key"] != key:
            expected_runs.append({"key": key, "start": cur, "minutes": 0, "bp": bp, "rid": rid})
        expected_runs[-1]["minutes"] += 1
        cur += timedelta(minutes=1)

    assert len(segments) == len(expected_runs), "分段数与 oracle 连跑不一致"
    expected_total = Fraction(0)
    for seg, run in zip(segments, expected_runs):
        assert seg.start == run["start"]
        assert seg.minutes == run["minutes"]
        assert seg.end == run["start"] + timedelta(minutes=run["minutes"])
        assert seg.rule_id == run["rid"]
        assert seg.basis_points == run["bp"]
        pay = Fraction(run["minutes"] * base * run["bp"],
                       BASIS_POINT_DENOMINATOR * MINUTES_PER_HOUR)
        assert seg.pay == pay
        expected_total += pay
    assert total == expected_total
    assert gcd(total.numerator, total.denominator) == 1
    return segments, total


def R(id, weekdays, s, e, priority=1, bp=10000):
    return {"id": id, "weekdays": weekdays, "start_minute": s,
            "end_minute": e, "priority": priority, "basis_points": bp}


def D(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi)


# ---------- 基础场景 ----------

def test_no_rules_one_hour():
    segments, total = assert_matches_oracle(D(2026, 9, 23, 10), D(2026, 9, 23, 11), 6000, [])
    assert len(segments) == 1
    assert segments[0].rule_id is None
    assert segments[0].basis_points == 10000
    assert total == Fraction(6000, 1)


def test_rule_partial_overlay_produces_three_segments():
    rules = [R("night", {0, 1, 2, 3, 4}, 22 * 60, 6 * 60, bp=15000)]
    segments, total = assert_matches_oracle(
        D(2026, 9, 21, 21), D(2026, 9, 22, 8), 6000, rules)  # 周一21点 -> 周二8点
    assert [s.rule_id for s in segments] == [None, "night", "night", None]
    assert [s.minutes for s in segments] == [60, 120, 360, 120]
    # 午夜必切：night 段在 00:00 断开
    night_segs = [s for s in segments if s.rule_id == "night"]
    assert len(night_segs) == 2
    assert night_segs[0].end == D(2026, 9, 22, 0)
    assert night_segs[1].start == D(2026, 9, 22, 0)
    # 1h 基础 + 8h×1.5 + 2h 基础 = 6000 + 72000 + 12000
    assert total == Fraction(90000, 1)


def test_same_rule_always_splits_at_midnight():
    rules = [R("allday", {0, 1, 2, 3, 4, 5, 6}, 0, 1440)]
    segments, _ = assert_matches_oracle(D(2026, 9, 22, 23), D(2026, 9, 23, 1), 1, rules)
    assert len(segments) == 2
    assert segments[0].end == D(2026, 9, 23, 0)
    assert segments[1].start == D(2026, 9, 23, 0)


def test_priority_wins_no_rate_stacking():
    """夜班+周末+专项同时命中：只取最高 priority，倍率不相加。"""
    rules = [
        R("night", range(7), 22 * 60, 6 * 60, priority=1, bp=12000),
        R("weekend", {5, 6}, 0, 1440, priority=2, bp=15000),
        R("special", {4}, 23 * 60, 1440, priority=3, bp=20000),
    ]
    # 周五 22:00 -> 周六 02:00
    # 周五22-23 仅 night(12000)；23-24 special(20000,priority 最高)；
    # 周六00-02 night 与 weekend 并列时间但 weekend priority 更高(15000)
    segments, total = assert_matches_oracle(D(2026, 9, 25, 22), D(2026, 9, 26, 2), 6000, rules)
    assert [s.rule_id for s in segments] == ["night", "special", "weekend"]
    assert [s.minutes for s in segments] == [60, 60, 120]
    assert total == Fraction(6000 * 12000, 10000) + Fraction(6000 * 20000, 10000) \
        + Fraction(6000 * 2 * 15000, 10000)
    assert total == Fraction(37200, 1)


def test_tie_breaks_by_utf8_id_ascii():
    rules = [R("b", {0}, 0, 1440, priority=1, bp=11000),
             R("a", {0}, 0, 1440, priority=1, bp=12000)]
    segments, _ = assert_matches_oracle(D(2026, 9, 21, 12), D(2026, 9, 21, 13), 1, rules)
    assert segments[0].rule_id == "a"


def test_tie_breaks_by_utf8_id_chinese():
    # "中" = E4 B8 AD，"夜" = E5 A4 9C，UTF-8 字节序 中 < 夜
    assert "中".encode("utf-8") < "夜".encode("utf-8")
    rules = [R("夜", range(7), 0, 1440, bp=11000),
             R("中", range(7), 0, 1440, bp=12000)]
    segments, _ = assert_matches_oracle(D(2026, 9, 21, 12), D(2026, 9, 21, 13), 1, rules)
    assert segments[0].rule_id == "中"


def test_single_minute_fraction_irreducible():
    # 1 分钟，时薪 100 分，10000bp -> 100/60 = 5/3
    _, total = assert_matches_oracle(D(2026, 9, 23, 10, 0), D(2026, 9, 23, 10, 1), 100, [])
    assert total == Fraction(5, 3)


def test_end_2400_covers_full_day():
    rules = [R("day", {2}, 0, 1440)]
    segments, total = assert_matches_oracle(D(2026, 9, 23, 0), D(2026, 9, 23, 23, 59), 60, rules)
    assert all(s.rule_id == "day" for s in segments)
    assert total == Fraction(60 * 1439, 60)


def test_boundary_minutes_half_open():
    """窗口半开：start 分钟命中，end 分钟不命中。"""
    rules = [R("win", {2}, 10 * 60, 11 * 60, bp=20000)]
    # 周三 09:59 - 11:01
    segments, _ = assert_matches_oracle(D(2026, 9, 23, 9, 59), D(2026, 9, 23, 11, 1), 6000, rules)
    assert [s.rule_id for s in segments] == [None, "win", None]
    assert segments[1].start == D(2026, 9, 23, 10, 0)
    assert segments[1].end == D(2026, 9, 23, 11, 0)
    assert segments[1].minutes == 60


# ---------- 随机化逐分钟对拍 ----------

def test_randomized_diff(random_case):
    start, end, base, rules = random_case
    assert_matches_oracle(start, end, base, rules)


# ---------- HTTP 层 ----------

def calc(payload):
    return client.post("/api/calculate", json=payload)


def test_api_happy_path():
    payload = {
        "start": "2026-09-25T22:00",
        "end": "2026-09-26T02:00",
        "baseRateCents": 6000,
        "rules": [
            {"id": "night", "weekdays": [0, 1, 2, 3, 4, 5, 6],
             "startMinute": 1320, "endMinute": 360, "priority": 1, "basisPoints": 12000},
            {"id": "weekend", "weekdays": [5, 6],
             "startMinute": 0, "endMinute": 1440, "priority": 2, "basisPoints": 15000},
        ],
    }
    resp = calc(payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["totalMinutes"] == 240
    # 周五22-24 night(午夜切在段尾) + 周六00-02 weekend(priority 更高)
    assert [s["ruleId"] for s in data["segments"]] == ["night", "weekend"]
    assert data["total"] == {"numerator": 32400, "denominator": 1,
                             "decimal": data["total"]["decimal"]}
    # 所有分数均不可约
    for f in [data["total"]] + [s["pay"] for s in data["segments"]]:
        assert gcd(f["numerator"], f["denominator"]) == 1


def test_api_invalid_date_is_422():
    resp = calc({"start": "2026-02-30T10:00", "end": "2026-03-01T10:00",
                 "baseRateCents": 100, "rules": []})
    assert resp.status_code == 422


def test_api_empty_weekdays_is_422():
    payload = {
        "start": "2026-09-23T10:00", "end": "2026-09-23T11:00",
        "baseRateCents": 100,
        "rules": [{"id": "x", "weekdays": [], "startMinute": 0,
                   "endMinute": 60, "priority": 1, "basisPoints": 10000}],
    }
    assert calc(payload).status_code == 422


def test_api_bad_interval_end_before_start_is_422():
    resp = calc({"start": "2026-09-23T11:00", "end": "2026-09-23T10:00",
                 "baseRateCents": 100, "rules": []})
    assert resp.status_code == 422


def test_api_over_36h_is_422():
    resp = calc({"start": "2026-09-23T00:00", "end": "2026-09-24T12:01",
                 "baseRateCents": 100, "rules": []})
    assert resp.status_code == 422


def test_api_exactly_36h_ok():
    resp = calc({"start": "2026-09-23T00:00", "end": "2026-09-24T12:00",
                 "baseRateCents": 100, "rules": []})
    assert resp.status_code == 200


def test_api_empty_window_is_422():
    payload = {
        "start": "2026-09-23T10:00", "end": "2026-09-23T11:00",
        "baseRateCents": 100,
        "rules": [{"id": "x", "weekdays": [0], "startMinute": 60,
                   "endMinute": 60, "priority": 1, "basisPoints": 10000}],
    }
    assert calc(payload).status_code == 422


def test_api_duplicate_ids_422():
    payload = {
        "start": "2026-09-23T10:00", "end": "2026-09-23T11:00",
        "baseRateCents": 100,
        "rules": [
            {"id": "x", "weekdays": [0], "startMinute": 0, "endMinute": 60,
             "priority": 1, "basisPoints": 10000},
            {"id": "x", "weekdays": [1], "startMinute": 0, "endMinute": 60,
             "priority": 1, "basisPoints": 10000},
        ],
    }
    assert calc(payload).status_code == 422


def test_api_too_many_rules_422():
    rules = [{"id": f"r{i}", "weekdays": [0], "startMinute": 0, "endMinute": 60,
              "priority": 1, "basisPoints": 10000} for i in range(21)]
    resp = calc({"start": "2026-09-23T10:00", "end": "2026-09-23T11:00",
                 "baseRateCents": 100, "rules": rules})
    assert resp.status_code == 422


def test_api_non_positive_rate_422():
    resp = calc({"start": "2026-09-23T10:00", "end": "2026-09-23T11:00",
                 "baseRateCents": 0, "rules": []})
    assert resp.status_code == 422


def test_api_seconds_precision_422():
    resp = calc({"start": "2026-09-23T10:00:30", "end": "2026-09-23T11:00",
                 "baseRateCents": 100, "rules": []})
    assert resp.status_code == 422
