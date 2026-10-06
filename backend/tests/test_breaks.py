"""经批准的不计薪休息的逐分钟对拍。

本文件内置第三个与引擎实现相互独立的 oracle（独立的休息覆盖判定、独立的
休息压过判定），逐分钟跑一遍后与引擎分段逐段对拍：边界、分钟数、被压过的
周规则 id、例外 id/类型、**休息标记与零工资**、保留的原始倍率、跨午夜断段、
分数不可约性，以及 API 层的计薪/休息分钟汇总。

覆盖需求要点：
- 至多 10 段休息，半开区间，使用真实本地分钟时间，必须整体落在值班区间内；
- 休息优先于所有倍率规则（含一次性例外）：休息分钟工资为 0，
  但结果仍保留该分钟原本命中的周规则及例外（ruleId/basisPoints/exception*）；
- 相接休息允许；正时长重叠、越界、非法时间整次拒绝（422）；
- 休息跨午夜仍在午夜断段；
- 计薪段与休息段在同一次分钟裁决中一起产生（总额直接只累计计薪分钟，
  绝不先按旧规则算总额再扣近似值）；
- 未提交休息（缺省 / null / []）时旧分段与总额逐项不变。
"""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta
from fractions import Fraction
from math import gcd

import pytest
from fastapi.testclient import TestClient

from app.engine import (
    BASIS_POINT_DENOMINATOR,
    DEFAULT_BASIS_POINTS,
    MINUTES_PER_HOUR,
    BreakOverlapError,
    BreakWindow,
    ExceptionWindow,
    REPLACE,
    SUPPRESS,
    Rule,
    find_overlapping_breaks,
    slice_shift,
)
from app.main import app

client = TestClient(app)


# ---------------------------------------------------------------------------
# 独立 oracle（不 import 引擎的 covers/choose 逻辑，全部按定义直接写）
# ---------------------------------------------------------------------------

def oracle_weekly_cover(rule: dict, weekday: int, m: int) -> bool:
    s, e = rule["start_minute"], rule["end_minute"]
    days = set(rule["weekdays"])
    if s < e:
        return s <= m < e and weekday in days
    return (m >= s and weekday in days) or (m < e and (weekday - 1) % 7 in days)


def oracle_weekly_winner(dt: datetime, rules: list[dict]) -> tuple[str | None, int]:
    wd, m = dt.weekday(), dt.hour * 60 + dt.minute
    hits = [r for r in rules if oracle_weekly_cover(r, wd, m)]
    if not hits:
        return None, DEFAULT_BASIS_POINTS
    hits.sort(key=lambda r: (-r["priority"], r["id"].encode("utf-8")))
    return hits[0]["id"], hits[0]["basis_points"]


def oracle_exc_interval(exc: dict) -> tuple[datetime, datetime]:
    lo = datetime.combine(exc["date"], time.min) + timedelta(minutes=exc["start_minute"])
    hi = lo + timedelta(minutes=exc["end_minute"] - exc["start_minute"])
    return lo, hi


def oracle_exc_covers(exc: dict, dt: datetime) -> bool:
    lo, hi = oracle_exc_interval(exc)
    return lo <= dt < hi


def oracle_break_covers(brk: dict, dt: datetime) -> bool:
    return brk["start"] <= dt < brk["end"]


def oracle_overlapping_breaks(breaks: list[dict]) -> list[tuple[datetime, datetime]]:
    """独立两两判定正时长重叠（相接不算），端点对按真实时间规范化排序。"""
    pairs = []
    for i in range(len(breaks)):
        ai, a_hi = breaks[i]["start"], breaks[i]["end"]
        for j in range(i + 1, len(breaks)):
            bi, b_hi = breaks[j]["start"], breaks[j]["end"]
            if max(ai, bi) < min(a_hi, b_hi):
                lo, hi = (ai, a_hi) if (ai, a_hi) <= (bi, b_hi) else (bi, b_hi)
                pairs.append((lo, hi))
    return sorted(pairs)


def oracle_minute(
    dt: datetime, rules: list[dict], exceptions: list[dict], breaks: list[dict]
) -> tuple[str | None, int, str | None, str | None, bool]:
    """返回 (被压过的周规则id, 原本倍率, 例外id, 例外类型, 是否休息)。"""
    weekly_id, weekly_bp = oracle_weekly_winner(dt, rules)
    rid, bp, eid, ekind = weekly_id, weekly_bp, None, None
    for exc in exceptions:  # 合法请求下至多命中一个
        if oracle_exc_covers(exc, dt):
            if exc["kind"] == SUPPRESS:
                rid, bp, eid, ekind = weekly_id, DEFAULT_BASIS_POINTS, exc["id"], SUPPRESS
            else:
                rid, bp, eid, ekind = weekly_id, exc["basis_points"], exc["id"], REPLACE
    # 休息覆盖在最上层：不改写任何原始裁决，只把该分钟标记为不计薪
    is_break = any(oracle_break_covers(b, dt) for b in breaks)
    return rid, bp, eid, ekind, is_break


def assert_matches_oracle(start, end, base, rules, exceptions, breaks):
    engine_rules = [
        Rule(r["id"], frozenset(r["weekdays"]), r["start_minute"],
             r["end_minute"], r["priority"], r["basis_points"])
        for r in rules
    ]
    engine_excs = [
        ExceptionWindow(e["id"], e["date"], e["start_minute"], e["end_minute"],
                        e["kind"], e.get("basis_points"))
        for e in exceptions
    ]
    engine_breaks = [BreakWindow(b["start"], b["end"]) for b in breaks]

    # 重叠判定本身也要与 oracle 一致
    assert find_overlapping_breaks(engine_breaks) == oracle_overlapping_breaks(breaks)

    segments, total = slice_shift(start, end, base, engine_rules, engine_excs, engine_breaks)

    expected_runs: list[dict] = []
    cur = start
    while cur < end:
        rid, bp, eid, ekind, is_break = oracle_minute(cur, rules, exceptions, breaks)
        key = (cur.date(), rid, bp, eid, ekind, is_break)
        if not expected_runs or expected_runs[-1]["key"] != key:
            expected_runs.append({"key": key, "start": cur, "minutes": 0,
                                  "rid": rid, "bp": bp, "eid": eid,
                                  "ekind": ekind, "is_break": is_break})
        expected_runs[-1]["minutes"] += 1
        cur += timedelta(minutes=1)

    assert len(segments) == len(expected_runs), "分段数与 oracle 连跑不一致"
    expected_total = Fraction(0)
    expected_paid = expected_break = 0
    for seg, run in zip(segments, expected_runs):
        assert seg.start == run["start"]
        assert seg.minutes == run["minutes"]
        assert seg.end == run["start"] + timedelta(minutes=run["minutes"])
        assert seg.weekday == run["start"].weekday()
        assert seg.rule_id == run["rid"]
        # 休息分钟仍保留原本命中的倍率
        assert seg.basis_points == run["bp"]
        assert seg.exception_id == run["eid"]
        assert seg.exception_kind == run["ekind"]
        assert seg.is_break == run["is_break"]
        if run["is_break"]:
            # 休息工资恒为 0，且 0 也是不可约分数 0/1
            assert seg.pay == Fraction(0, 1)
            expected_break += run["minutes"]
        else:
            pay = Fraction(run["minutes"] * base * run["bp"],
                           BASIS_POINT_DENOMINATOR * MINUTES_PER_HOUR)
            assert seg.pay == pay
            expected_total += pay
            expected_paid += run["minutes"]
        assert gcd(seg.pay.numerator, seg.pay.denominator) == 1
    assert total == expected_total
    assert gcd(total.numerator, total.denominator) == 1

    paid = sum(s.minutes for s in segments if not s.is_break)
    rested = sum(s.minutes for s in segments if s.is_break)
    assert (paid, rested) == (expected_paid, expected_break)
    assert paid + rested == sum(s.minutes for s in segments)
    return segments, total


# ---------------------------------------------------------------------------
# 构造小工具
# ---------------------------------------------------------------------------

def R(id, weekdays, s, e, priority=1, bp=10000):
    return {"id": id, "weekdays": weekdays, "start_minute": s,
            "end_minute": e, "priority": priority, "basis_points": bp}


def E(id, d, s, e, kind=REPLACE, bp=None):
    return {"id": id, "date": d, "start_minute": s, "end_minute": e,
            "kind": kind, "basis_points": bp}


def B(s: datetime, e: datetime) -> dict:
    return {"start": s, "end": e}


def D(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi)


FRI = date(2026, 9, 25)
SAT = date(2026, 9, 26)

DEFAULT_RULES = [
    R("night", range(7), 22 * 60, 6 * 60, priority=1, bp=12000),
    R("weekend", {5, 6}, 0, 1440, priority=2, bp=15000),
    R("special", {4}, 23 * 60, 1440, priority=3, bp=20000),
]


def engine_breaks(breaks):
    return [BreakWindow(b["start"], b["end"]) for b in breaks]


# ---------------------------------------------------------------------------
# 1) 休息覆盖周规则：零工资，但保留原本命中的规则/倍率
# ---------------------------------------------------------------------------

def test_break_overrides_weekly_rule_zero_pay_but_keeps_rule():
    # 周五 22:00 → 周六 02:00；23:00-24:00 是经批准的休息
    breaks = [B(D(2026, 9, 25, 23), D(2026, 9, 26, 0))]
    segments, total = assert_matches_oracle(
        D(2026, 9, 25, 22), D(2026, 9, 26, 2), 6000, DEFAULT_RULES, [], breaks)

    # night(60 计薪) / 休息压在 special 上(60 零工资) / weekend(120 计薪)
    assert [s.rule_id for s in segments] == ["night", "special", "weekend"]
    assert [s.is_break for s in segments] == [False, True, False]
    assert [s.minutes for s in segments] == [60, 60, 120]
    mid = segments[1]
    assert mid.pay == 0 and mid.basis_points == 20000 and mid.exception_id is None
    # 只计 60m night + 120m weekend（special 的一小时被休息覆盖）
    assert total == Fraction(7200 + 18000, 1)


def test_break_inside_default_minutes_keeps_null_rule_and_basis():
    # 无规则区间内休息：ruleId 仍为 None、保留倍率 10000、工资为 0
    breaks = [B(D(2026, 9, 23, 10, 30), D(2026, 9, 23, 11))]
    segments, total = assert_matches_oracle(
        D(2026, 9, 23, 10), D(2026, 9, 23, 12), 6000, [], [], breaks)
    assert [(s.rule_id, s.basis_points, s.is_break, s.minutes, s.pay) for s in segments] == [
        (None, 10000, False, 30, Fraction(3000, 1)),
        (None, 10000, True, 30, Fraction(0, 1)),
        (None, 10000, False, 60, Fraction(6000, 1)),
    ]
    assert total == Fraction(9000, 1)  # 90 计薪分钟，绝不先算 2 小时再扣 3000


# ---------------------------------------------------------------------------
# 2) 休息优先于一次性例外：零工资，但保留例外 id/类型与倍率
# ---------------------------------------------------------------------------

def test_break_overrides_replace_exception_but_keeps_exception_fields():
    rules = [
        R("night", range(7), 22 * 60, 6 * 60, priority=1, bp=12000),
        R("weekend", {5, 6}, 0, 1440, priority=10, bp=15000),
    ]
    excs = [E("duty", SAT, 10 * 60, 12 * 60, kind=REPLACE, bp=20000)]
    # 周六 11:00-12:00 休息：压过 REPLACE 例外（例外又压过 weekend）
    breaks = [B(D(2026, 9, 26, 11), D(2026, 9, 26, 12))]
    segments, total = assert_matches_oracle(
        D(2026, 9, 26, 10), D(2026, 9, 26, 13), 6000, rules, excs, breaks)

    assert [s.is_break for s in segments] == [False, True, False]
    assert [s.minutes for s in segments] == [60, 60, 60]
    rest = segments[1]
    assert rest.pay == 0
    # 休息分钟仍保留：例外 duty(REPLACE, 20000)，被压过的周规则 weekend
    assert rest.exception_id == "duty"
    assert rest.exception_kind == REPLACE
    assert rest.basis_points == 20000
    assert rest.rule_id == "weekend"
    # 60m duty@2.0× + 60m weekend@1.5×；中间休息 60m 为 0
    assert total == Fraction(60 * 6000 * 20000, 600000) \
        + Fraction(60 * 6000 * 15000, 600000)


def test_break_overrides_suppress_exception():
    excs = [E("cancel", SAT, 10 * 60, 12 * 60, kind=SUPPRESS)]
    breaks = [B(D(2026, 9, 26, 11), D(2026, 9, 26, 12))]
    segments, _ = assert_matches_oracle(
        D(2026, 9, 26, 10), D(2026, 9, 26, 13), 6000,
        [R("weekend", {5, 6}, 0, 1440, priority=10, bp=15000)], excs, breaks)
    assert [(s.exception_id, s.basis_points, s.is_break, s.minutes) for s in segments] == [
        ("cancel", 10000, False, 60),
        ("cancel", 10000, True, 60),
        (None, 15000, False, 60),
    ]
    assert segments[1].pay == 0 and segments[1].rule_id == "weekend"


# ---------------------------------------------------------------------------
# 3) 跨午夜：休息在午夜断段
# ---------------------------------------------------------------------------

def test_break_across_midnight_splits_at_midnight():
    # 周五 22:00 → 周六 02:00；休息 23:30 → 周六 00:30
    breaks = [B(D(2026, 9, 25, 23, 30), D(2026, 9, 26, 0, 30))]
    segments, total = assert_matches_oracle(
        D(2026, 9, 25, 22), D(2026, 9, 26, 2), 6000, [], [], breaks)
    flags = [(s.is_break, s.minutes, s.start.date(), s.end) for s in segments]
    assert flags == [
        (False, 90, FRI, D(2026, 9, 25, 23, 30)),
        (True, 30, FRI, D(2026, 9, 26, 0)),
        (True, 30, SAT, D(2026, 9, 26, 0, 30)),
        (False, 90, SAT, D(2026, 9, 26, 2)),
    ]
    # 午夜两侧是两个休息段（午夜必断），工资均为 0
    assert segments[1].pay == segments[2].pay == 0
    assert segments[1].end == D(2026, 9, 26, 0) == segments[2].start
    assert total == Fraction(180 * 6000, 60)


def test_break_split_at_weekly_boundary_inside_break():
    # 休息 21:30-22:30，22:00 起 night 生效：被压过的 ruleId 变化也要断段
    rules = [R("night", range(7), 22 * 60, 6 * 60, priority=1, bp=15000)]
    breaks = [B(D(2026, 9, 25, 21, 30), D(2026, 9, 25, 22, 30))]
    segments, _ = assert_matches_oracle(
        D(2026, 9, 25, 21), D(2026, 9, 25, 23), 6000, rules, [], breaks)
    assert [s.rule_id for s in segments] == [None, None, "night", "night"]
    assert [s.is_break for s in segments] == [False, True, True, False]
    assert [s.minutes for s in segments] == [30, 30, 30, 30]
    assert all(s.pay == 0 for s in segments if s.is_break)


# ---------------------------------------------------------------------------
# 4) 相接允许 / 正时长重叠整次拒绝（与输入顺序无关）
# ---------------------------------------------------------------------------

def test_adjacent_breaks_same_underlying_rule_merge():
    # 相接且裁决相同 → 归并为一个休息段
    breaks = [
        B(D(2026, 9, 26, 10), D(2026, 9, 26, 11)),
        B(D(2026, 9, 26, 11), D(2026, 9, 26, 12)),
    ]
    segments, _ = assert_matches_oracle(
        D(2026, 9, 26, 9), D(2026, 9, 26, 13), 6000, [], [], breaks)
    assert [(s.is_break, s.minutes) for s in segments] == [(False, 60), (True, 120), (False, 60)]


def test_adjacent_breaks_across_midnight_allowed():
    # 23:00-24:00 与 00:00-01:00 在午夜相接：允许，午夜仍断成两个休息段
    breaks = [
        B(D(2026, 9, 25, 23), D(2026, 9, 26, 0)),
        B(D(2026, 9, 26, 0), D(2026, 9, 26, 1)),
    ]
    segments, _ = assert_matches_oracle(
        D(2026, 9, 25, 22), D(2026, 9, 26, 2), 6000, [], [], breaks)
    assert [(s.is_break, s.minutes, s.start.date()) for s in segments] == [
        (False, 60, FRI), (True, 60, FRI), (True, 60, SAT), (False, 60, SAT)]


def test_adjacent_breaks_different_underlying_rules_stay_split():
    rules = [R("night", range(7), 22 * 60, 6 * 60, priority=1, bp=15000)]
    breaks = [
        B(D(2026, 9, 25, 21, 30), D(2026, 9, 25, 22)),
        B(D(2026, 9, 25, 22), D(2026, 9, 25, 22, 30)),
    ]
    segments, _ = assert_matches_oracle(
        D(2026, 9, 25, 21), D(2026, 9, 25, 23), 6000, rules, [], breaks)
    # 相接但底下规则在 22:00 变化（None → night）：两个休息段不断开相接关系但各自独立
    assert [(s.rule_id, s.is_break, s.minutes) for s in segments] == [
        (None, False, 30), (None, True, 30), ("night", True, 30), ("night", False, 30)]


def test_overlapping_breaks_rejected_by_engine():
    breaks = [
        B(D(2026, 9, 26, 10), D(2026, 9, 26, 12)),
        B(D(2026, 9, 26, 11), D(2026, 9, 26, 13)),
    ]
    with pytest.raises(BreakOverlapError) as ei:
        slice_shift(D(2026, 9, 26, 9), D(2026, 9, 26, 14), 6000, [],
                    [], engine_breaks(breaks))
    assert ei.value.pairs == [(D(2026, 9, 26, 10), D(2026, 9, 26, 12))]


def test_overlap_rejection_is_order_independent():
    a = B(D(2026, 9, 26, 10), D(2026, 9, 26, 12))
    b = B(D(2026, 9, 26, 11, 59), D(2026, 9, 26, 12, 30))
    with pytest.raises(BreakOverlapError) as e1:
        slice_shift(D(2026, 9, 26, 9), D(2026, 9, 26, 14), 6000, [],
                    [], engine_breaks([a, b]))
    with pytest.raises(BreakOverlapError) as e2:
        slice_shift(D(2026, 9, 26, 9), D(2026, 9, 26, 14), 6000, [],
                    [], engine_breaks([b, a]))
    # 规范化后的端点对与输入顺序无关（一分钟正时长重叠也要拒绝）
    assert e1.value.pairs == e2.value.pairs == [(D(2026, 9, 26, 10), D(2026, 9, 26, 12))]


def test_one_minute_overlap_rejected_but_abut_allowed():
    abut = [
        B(D(2026, 9, 25, 23), D(2026, 9, 26, 1)),
        B(D(2026, 9, 26, 1), D(2026, 9, 26, 2)),
    ]
    segments, _ = slice_shift(D(2026, 9, 26, 0), D(2026, 9, 26, 2), 6000, [],
                              [], engine_breaks(abut))
    assert all(s.is_break for s in segments)

    bad = [abut[0], B(D(2026, 9, 26, 0, 59), D(2026, 9, 26, 2))]
    with pytest.raises(BreakOverlapError):
        slice_shift(D(2026, 9, 26, 0), D(2026, 9, 26, 2), 6000, [],
                    [], engine_breaks(bad))


def test_break_covers_whole_shift_total_is_zero():
    breaks = [B(D(2026, 9, 26, 0), D(2026, 9, 26, 3))]
    segments, total = assert_matches_oracle(
        D(2026, 9, 26, 0), D(2026, 9, 26, 3), 6000,
        [R("weekend", {5, 6}, 0, 1440, priority=10, bp=15000)], [], breaks)
    assert len(segments) == 1 and segments[0].is_break
    assert total == 0 and segments[0].pay == 0
    assert segments[0].rule_id == "weekend"  # 原本命中仍保留


# ---------------------------------------------------------------------------
# 5) 未提交休息：旧结果逐项不变
# ---------------------------------------------------------------------------

def test_engine_output_unchanged_without_breaks():
    engine_rules = [Rule(r["id"], frozenset(r["weekdays"]), r["start_minute"],
                         r["end_minute"], r["priority"], r["basis_points"])
                    for r in DEFAULT_RULES]
    old_segments, old_total = slice_shift(
        D(2026, 9, 25, 22), D(2026, 9, 26, 2), 6000, engine_rules)        # 旧调用
    new_segments, new_total = slice_shift(
        D(2026, 9, 25, 22), D(2026, 9, 26, 2), 6000, engine_rules,
        [], [])                                                            # 显式空休息
    assert old_total == new_total
    assert len(old_segments) == len(new_segments)
    for a, b in zip(old_segments, new_segments):
        assert (a.start, a.end, a.minutes, a.weekday, a.rule_id,
                a.basis_points, a.pay, a.exception_id, a.exception_kind) == (
            b.start, b.end, b.minutes, b.weekday, b.rule_id,
            b.basis_points, b.pay, b.exception_id, b.exception_kind)
        assert b.is_break is False


def calc(payload):
    return client.post("/api/calculate", json=payload)


BASE_BODY = {
    "start": "2026-09-25T22:00",
    "end": "2026-09-26T02:00",
    "baseRateCents": 6000,
    "rules": [
        {"id": "night", "weekdays": [0, 1, 2, 3, 4, 5, 6], "startMinute": 1320,
         "endMinute": 360, "priority": 1, "basisPoints": 12000},
        {"id": "weekend", "weekdays": [5, 6], "startMinute": 0,
         "endMinute": 1440, "priority": 2, "basisPoints": 15000},
        {"id": "special", "weekdays": [4], "startMinute": 1380,
         "endMinute": 1440, "priority": 3, "basisPoints": 20000},
    ],
}


def test_api_response_unchanged_without_breaks():
    r1 = calc(BASE_BODY)
    r2 = calc({**BASE_BODY, "breaks": []})
    r3 = calc({**BASE_BODY, "breaks": None})
    assert r1.status_code == r2.status_code == r3.status_code == 200
    j1, j2, j3 = r1.json(), r2.json(), r3.json()
    assert j1 == j2 == j3
    assert [s["ruleId"] for s in j1["segments"]] == ["night", "special", "weekend"]
    assert j1["total"]["numerator"] == 37200 and j1["total"]["denominator"] == 1
    assert all(s["isBreak"] is False for s in j1["segments"])
    assert j1["paidMinutes"] == j1["totalMinutes"] == 240
    assert j1["breakMinutes"] == 0


# ---------------------------------------------------------------------------
# 6) HTTP 层：休息生效（含汇总）/ 各类非法休息 422
# ---------------------------------------------------------------------------

def test_api_break_happy_path_summaries():
    payload = {
        **BASE_BODY,
        "breaks": [
            {"start": "2026-09-25T23:00", "end": "2026-09-26T00:00"},
            {"start": "2026-09-26T01:00", "end": "2026-09-26T01:30"},
        ],
    }
    resp = calc(payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert [s["isBreak"] for s in data["segments"]] == [False, True, False, True, False]
    assert [s["minutes"] for s in data["segments"]] == [60, 60, 60, 30, 30]
    # 休息段工资均为 0/1；保留原始命中
    rest = [s for s in data["segments"] if s["isBreak"]]
    assert [s["pay"] for s in rest] == [
        {"numerator": 0, "denominator": 1, "decimal": "0.000000"},
        {"numerator": 0, "denominator": 1, "decimal": "0.000000"},
    ]
    assert rest[0]["ruleId"] == "special" and rest[0]["basisPoints"] == 20000
    assert rest[1]["ruleId"] == "weekend" and rest[1]["basisPoints"] == 15000
    # 汇总：240 = 150 计薪 + 90 休息；总额只算计薪分钟
    assert data["totalMinutes"] == 240
    assert data["paidMinutes"] == 150 and data["breakMinutes"] == 90
    # 60m night + 60m weekend(00-01) + 30m weekend(01:30-02)
    assert data["total"]["numerator"] == 7200 + 9000 + 4500
    assert data["total"]["denominator"] == 1
    # 各段分数工资之和必须恰好等于总额（同一份响应自洽）
    assert sum(Fraction(s["pay"]["numerator"], s["pay"]["denominator"])
               for s in data["segments"]) == Fraction(data["total"]["numerator"],
                                                      data["total"]["denominator"])


def test_api_break_abutting_shift_bounds_ok():
    payload = {
        **BASE_BODY,
        "breaks": [
            {"start": "2026-09-25T22:00", "end": "2026-09-25T22:30"},  # 与起点相接
            {"start": "2026-09-26T01:30", "end": "2026-09-26T02:00"},  # 与终点相接
        ],
    }
    assert calc(payload).status_code == 200


def test_api_break_start_before_shift_is_422():
    payload = {**BASE_BODY,
               "breaks": [{"start": "2026-09-25T21:59", "end": "2026-09-25T22:30"}]}
    r = calc(payload)
    assert r.status_code == 422 and "越界" in r.text


def test_api_break_end_after_shift_is_422():
    payload = {**BASE_BODY,
               "breaks": [{"start": "2026-09-26T01:30", "end": "2026-09-26T02:01"}]}
    r = calc(payload)
    assert r.status_code == 422 and "越界" in r.text


def test_api_empty_break_is_422():
    payload = {**BASE_BODY,
               "breaks": [{"start": "2026-09-26T01:00", "end": "2026-09-26T01:00"}]}
    assert calc(payload).status_code == 422


def test_api_break_end_before_start_is_422():
    payload = {**BASE_BODY,
               "breaks": [{"start": "2026-09-26T01:30", "end": "2026-09-26T01:00"}]}
    assert calc(payload).status_code == 422


def test_api_overlapping_breaks_is_422():
    def payload(order):
        bs = [
            {"start": "2026-09-26T00:00", "end": "2026-09-26T01:00"},
            {"start": "2026-09-26T00:30", "end": "2026-09-26T02:00"},
        ]
        return {**BASE_BODY, "breaks": [bs[i] for i in order]}

    r1, r2 = calc(payload([0, 1])), calc(payload([1, 0]))
    assert r1.status_code == r2.status_code == 422
    assert "重叠" in r1.text and "重叠" in r2.text


def test_api_adjacent_breaks_are_200():
    payload = {**BASE_BODY, "breaks": [
        {"start": "2026-09-26T00:00", "end": "2026-09-26T01:00"},
        {"start": "2026-09-26T01:00", "end": "2026-09-26T02:00"},
    ]}
    assert calc(payload).status_code == 200


def test_api_break_seconds_precision_is_422():
    payload = {**BASE_BODY,
               "breaks": [{"start": "2026-09-25T23:00:30", "end": "2026-09-25T23:30"}]}
    assert calc(payload).status_code == 422


def test_api_break_timezone_aware_is_422():
    payload = {**BASE_BODY,
               "breaks": [{"start": "2026-09-25T23:00+08:00",
                           "end": "2026-09-25T23:30+08:00"}]}
    assert calc(payload).status_code == 422


def test_api_break_invalid_calendar_date_is_422():
    payload = {**BASE_BODY,
               "breaks": [{"start": "2026-02-30T23:00", "end": "2026-03-01T00:00"}]}
    assert calc(payload).status_code == 422


def test_api_too_many_breaks_is_422():
    # 11 段一分钟、互不重叠：超过 10 段上限
    bs = [{"start": f"2026-09-26T00:{i:02d}",
           "end": f"2026-09-26T00:{i + 1:02d}"} for i in range(11)]
    payload = {"start": "2026-09-26T00:00", "end": "2026-09-26T01:00",
               "baseRateCents": 6000, "rules": [], "breaks": bs}
    assert calc(payload).status_code == 422


def test_api_failure_keeps_no_old_conclusion():
    # 先成功（无休息），再提交非法休息：失败响应不得携带旧结果由前端展示；
    # 后端层面校验失败即 422，不会返回任何分段/总额
    ok = calc(BASE_BODY)
    assert ok.status_code == 200
    bad = calc({**BASE_BODY,
                "breaks": [{"start": "2026-09-26T01:00", "end": "2026-09-26T03:00"}]})
    assert bad.status_code == 422
    body = bad.json()
    assert "segments" not in body and "total" not in body


# ---------------------------------------------------------------------------
# 7) 随机化逐分钟对拍（周规则 + 一次性例外 + 休息交错，含跨午夜）
# ---------------------------------------------------------------------------

ID_POOL = ["night", "weekend", "special", "a", "b", "中", "夜", "z9"]
EXC_ID_POOL = ["hol", "temp", "duty", "甲", "乙", "x1"]


def make_break_case(seed: int):
    rng = random.Random(seed)
    start = (datetime(2026, 9, 14)
             + timedelta(days=rng.randrange(14),
                         hours=rng.randrange(24),
                         minutes=rng.choice([0, 15, 30, 45])))
    length = rng.randrange(1, 36 * 60 + 1)
    end = start + timedelta(minutes=length)

    rules = []
    for i in range(rng.randrange(0, 6)):
        s = rng.randrange(0, 1440)
        e = rng.choice([m for m in range(1, 1441) if m != s])
        rules.append({"id": ID_POOL[i],
                      "weekdays": rng.sample(range(7), rng.randrange(1, 4)),
                      "start_minute": s, "end_minute": e,
                      "priority": rng.randrange(0, 5),
                      "basis_points": rng.choice([5000, 10000, 12000, 15000, 20000])})

    exceptions: list[dict] = []
    used_ids: set[str] = set()
    for _ in range(20):  # 贪心保留互不重叠的窗口
        if len(exceptions) >= 6:
            break
        eid = rng.choice([x for x in EXC_ID_POOL if x not in used_ids])
        anchor = start.date() + timedelta(days=rng.randrange(-1, 3))
        s = rng.randrange(0, 1440)
        e = s + rng.randrange(1, max(2, min(2 * 1440 - s, 1000)))
        kind = rng.choice([SUPPRESS, REPLACE])
        cand = {"id": eid, "date": anchor, "start_minute": s, "end_minute": e,
                "kind": kind,
                "basis_points": None if kind == SUPPRESS
                else rng.choice([5000, 10000, 12000, 18000, 25000])}
        lo, hi = oracle_exc_interval(cand)
        if all(not (max(lo, oracle_exc_interval(x)[0])
                    < min(hi, oracle_exc_interval(x)[1])) for x in exceptions):
            exceptions.append(cand)
            used_ids.add(eid)

    # 休息：整体位于值班区间内、分钟对齐、贪心相接可保留、重叠即丢弃候选
    breaks: list[dict] = []
    for _ in range(30):
        if len(breaks) >= 8:
            break
        off = rng.randrange(0, length)
        span = rng.randrange(1, min(length - off, 240) + 1)
        cand = B(start + timedelta(minutes=off), start + timedelta(minutes=off + span))
        if not oracle_overlapping_breaks(breaks + [cand]):
            breaks.append(cand)

    return (start, end, rng.choice([1, 100, 6000, 12345]), rules, exceptions, breaks)


@pytest.mark.parametrize("seed", range(30))
def test_randomized_with_breaks(seed):
    start, end, base, rules, excs, breaks = make_break_case(20261006 + seed)
    assert_matches_oracle(start, end, base, rules, excs, breaks)
