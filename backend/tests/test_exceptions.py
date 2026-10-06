"""一次性例外（节假日临时值班通知）的逐分钟对拍。

与 ``test_slice.py`` 相同的策略：本文件内置一个与引擎实现相互独立的 oracle
（独立的真实时间线区间判定、独立的例外覆盖与压过逻辑），逐分钟跑一遍后与引擎
分段逐段对拍：边界、分钟数、被压过的周规则 id、倍率、例外 id/类型、分数工资、
跨午夜归属、不可约性。

覆盖需求要点：
- 跨午夜例外的次日部分归入实际次日（午夜必断）；
- 例外压过周末等高 priority 周规则（SUPPRESS=10000，REPLACE=自带倍率，绝不相加）；
- 相接（end==start）不算重叠，跨午夜相接同样允许；
- 真实时间线上正时长重叠整次请求拒绝，且拒绝结果与输入顺序无关；
- 未提交例外时旧结果逐项不变（旧契约字段逐一对比）。
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
    ExceptionOverlapError,
    ExceptionWindow,
    REPLACE,
    SUPPRESS,
    Rule,
    find_overlapping_exceptions,
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
    """例外在真实时间线上的半开区间：endMinute 超过 1440 的部分延伸到次日。"""
    lo = datetime.combine(exc["date"], time.min) + timedelta(minutes=exc["start_minute"])
    hi = lo + timedelta(minutes=exc["end_minute"] - exc["start_minute"])
    return lo, hi


def oracle_exc_covers(exc: dict, dt: datetime) -> bool:
    lo, hi = oracle_exc_interval(exc)
    return lo <= dt < hi


def oracle_overlapping_pairs(exceptions: list[dict]) -> list[tuple[str, str]]:
    """独立两两判定正时长重叠（相接不算），按 UTF-8 字节序报 id 对。"""
    pairs = []
    for i in range(len(exceptions)):
        ai, a_hi = oracle_exc_interval(exceptions[i])
        for j in range(i + 1, len(exceptions)):
            bi, b_hi = oracle_exc_interval(exceptions[j])
            if max(ai, bi) < min(a_hi, b_hi):
                x, y = exceptions[i]["id"], exceptions[j]["id"]
                pairs.append((x, y) if x.encode() <= y.encode() else (y, x))
    pairs.sort(key=lambda p: (p[0].encode(), p[1].encode()))
    return pairs


def oracle_minute(
    dt: datetime, rules: list[dict], exceptions: list[dict]
) -> tuple[str | None, int, str | None, str | None]:
    """返回 (被压过的周规则id, 采用倍率, 例外id, 例外类型)。"""
    weekly_id, _weekly_bp = oracle_weekly_winner(dt, rules)
    for exc in exceptions:  # 合法请求下至多命中一个
        if oracle_exc_covers(exc, dt):
            if exc["kind"] == SUPPRESS:
                return weekly_id, DEFAULT_BASIS_POINTS, exc["id"], SUPPRESS
            return weekly_id, exc["basis_points"], exc["id"], REPLACE
    return weekly_id, _weekly_bp, None, None


def assert_matches_oracle(start, end, base, rules, exceptions):
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

    # 重叠判定本身也要与 oracle 一致
    assert [(a, b) for a, b in find_overlapping_exceptions(engine_excs)] == \
        oracle_overlapping_pairs(exceptions)

    segments, total = slice_shift(start, end, base, engine_rules, engine_excs)

    expected_runs: list[dict] = []
    cur = start
    while cur < end:
        rid, bp, eid, ekind = oracle_minute(cur, rules, exceptions)
        key = (cur.date(), rid, bp, eid, ekind)
        if not expected_runs or expected_runs[-1]["key"] != key:
            expected_runs.append({"key": key, "start": cur, "minutes": 0,
                                  "rid": rid, "bp": bp, "eid": eid, "ekind": ekind})
        expected_runs[-1]["minutes"] += 1
        cur += timedelta(minutes=1)

    assert len(segments) == len(expected_runs), "分段数与 oracle 连跑不一致"
    expected_total = Fraction(0)
    for seg, run in zip(segments, expected_runs):
        assert seg.start == run["start"]
        assert seg.minutes == run["minutes"]
        assert seg.end == run["start"] + timedelta(minutes=run["minutes"])
        assert seg.weekday == run["start"].weekday()
        assert seg.rule_id == run["rid"]
        assert seg.basis_points == run["bp"]
        assert seg.exception_id == run["eid"]
        assert seg.exception_kind == run["ekind"]
        pay = Fraction(run["minutes"] * base * run["bp"],
                       BASIS_POINT_DENOMINATOR * MINUTES_PER_HOUR)
        assert seg.pay == pay
        assert gcd(seg.pay.numerator, seg.pay.denominator) == 1
        expected_total += pay
    assert total == expected_total
    assert gcd(total.numerator, total.denominator) == 1
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


def D(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi)


FRI = date(2026, 9, 25)
SAT = date(2026, 9, 26)
SUN = date(2026, 9, 27)


def engine_excs(exceptions):
    return [ExceptionWindow(e["id"], e["date"], e["start_minute"], e["end_minute"],
                            e["kind"], e.get("basis_points")) for e in exceptions]


# ---------------------------------------------------------------------------
# 1) 跨午夜：例外次日部分归入实际次日，午夜必断
# ---------------------------------------------------------------------------

def test_suppress_crosses_midnight_and_splits_into_next_day():
    # 周五 22:00 → 周六 02:00；night 规则 22:00-06:00（1.2×，priority 1）
    rules = [R("night", range(7), 22 * 60, 6 * 60, priority=1, bp=12000)]
    # SUPPRESS 周五 23:00 → 周六 02:00（endMinute 1560 = 1440+120）
    excs = [E("hol-suppress", FRI, 23 * 60, 26 * 60, kind=SUPPRESS)]

    segments, total = assert_matches_oracle(
        D(2026, 9, 25, 22), D(2026, 9, 26, 2), 6000, rules, excs)

    assert [s.rule_id for s in segments] == ["night", "night", "night"]
    assert [s.basis_points for s in segments] == [12000, 10000, 10000]
    assert [s.exception_id for s in segments] == [None, "hol-suppress", "hol-suppress"]
    assert [s.exception_kind for s in segments] == [None, SUPPRESS, SUPPRESS]
    assert [s.minutes for s in segments] == [60, 60, 120]

    # 跨午夜部分归入实际次日：第三段属于周六，且午夜 00:00 一定断开
    assert segments[1].end == D(2026, 9, 26, 0)
    assert segments[1].start.date() == FRI
    assert segments[2].start == D(2026, 9, 26, 0)
    assert segments[2].start.date() == SAT
    assert segments[2].weekday == SAT.weekday()

    # 60m×1.2× + 180m×1.0× = 7200 + 18000
    assert total == Fraction(25200, 1)


def test_replace_cross_midnight_uses_next_day_minutes():
    # 例外 周五23:00 → 周六02:00 按 2.0×；无周规则
    excs = [E("temp", FRI, 23 * 60, 26 * 60, kind=REPLACE, bp=20000)]
    segments, total = assert_matches_oracle(
        D(2026, 9, 25, 22), D(2026, 9, 26, 2), 6000, [], excs)
    assert [s.basis_points for s in segments] == [10000, 20000, 20000]
    assert [s.minutes for s in segments] == [60, 60, 120]
    assert segments[2].start.date() == SAT
    # 60m 基础 + 180m×2.0
    assert total == Fraction(6000) + Fraction(180 * 6000 * 20000, 600000)
    assert total == Fraction(42000, 1)


def test_exception_spilling_past_shift_end_is_clipped():
    # 例外延伸到 02:00，值班 01:00 结束：只按区间内分钟计，且不报错
    excs = [E("temp", FRI, 23 * 60, 26 * 60, kind=REPLACE, bp=20000)]
    segments, total = assert_matches_oracle(
        D(2026, 9, 25, 23), D(2026, 9, 26, 1), 6000, [], excs)
    assert [s.minutes for s in segments] == [60, 60]
    assert segments[-1].end == D(2026, 9, 26, 1)


def test_exception_outside_shift_has_no_effect():
    excs = [E("elsewhere", date(2026, 9, 28), 0, 60, kind=REPLACE, bp=30000)]
    segments, _ = assert_matches_oracle(
        D(2026, 9, 25, 10), D(2026, 9, 25, 11), 6000, [], excs)
    assert len(segments) == 1
    assert segments[0].exception_id is None
    assert segments[0].basis_points == 10000


# ---------------------------------------------------------------------------
# 2) 覆盖周末高 priority 规则
# ---------------------------------------------------------------------------

def test_replace_overrides_high_priority_weekend_rule():
    # 周六全天：night(priority 1, 1.2×) + weekend(priority 10, 1.5×)
    rules = [
        R("night", range(7), 22 * 60, 6 * 60, priority=1, bp=12000),
        R("weekend", {5, 6}, 0, 1440, priority=10, bp=15000),
    ]
    # 周六 10:00-12:00 临时通知 REPLACE 2.0×
    excs = [E("duty", SAT, 10 * 60, 12 * 60, kind=REPLACE, bp=20000)]
    segments, total = assert_matches_oracle(
        D(2026, 9, 26, 0), D(2026, 9, 27, 0), 6000, rules, excs)

    assert [s.exception_id for s in segments] == [None, "duty", None]
    assert [s.basis_points for s in segments] == [15000, 20000, 15000]
    assert [s.minutes for s in segments] == [600, 120, 720]
    # 被压过的周规则仍是 weekend（最高 priority），仅用于解释；倍率绝不相加
    assert segments[1].rule_id == "weekend"
    assert segments[1].exception_kind == REPLACE
    total_weekend = Fraction((600 + 720) * 6000 * 15000, 600000)
    total_duty = Fraction(120 * 6000 * 20000, 600000)
    assert total == total_weekend + total_duty


def test_suppress_overrides_high_priority_weekend_rule():
    rules = [R("weekend", {5, 6}, 0, 1440, priority=10, bp=15000)]
    excs = [E("cancel", SAT, 10 * 60, 12 * 60, kind=SUPPRESS)]
    segments, _ = assert_matches_oracle(
        D(2026, 9, 26, 9), D(2026, 9, 26, 13), 6000, rules, excs)
    assert [s.basis_points for s in segments] == [15000, 10000, 15000]
    assert [s.exception_kind for s in segments] == [None, SUPPRESS, None]
    assert segments[1].rule_id == "weekend"  # 记录被压过的周末规则
    assert [s.minutes for s in segments] == [60, 120, 60]


def test_exception_half_open_boundary_minutes():
    # [10:00, 11:00)：10:00 那一分钟归例外，11:00 那一分钟归周规则
    rules = [R("day", {5}, 0, 1440, priority=1, bp=15000)]
    excs = [E("win", SAT, 10 * 60, 11 * 60, kind=REPLACE, bp=20000)]
    segments, _ = assert_matches_oracle(
        D(2026, 9, 26, 9, 59), D(2026, 9, 26, 11, 1), 6000, rules, excs)
    assert [(s.exception_id, s.minutes) for s in segments] == [
        (None, 1), ("win", 60), (None, 1)]
    assert segments[1].start == D(2026, 9, 26, 10, 0)
    assert segments[1].end == D(2026, 9, 26, 11, 0)


def test_segments_split_at_weekly_winner_boundary_inside_exception():
    # 例外窗口 21:00-23:00 内部 22:00 处周规则裁决发生变化（night 开始生效）：
    # 后端在周规则边界重新切片（被压过的 rule_id 变化即使例外 id/倍率不变也断开）。
    rules = [R("night", range(7), 22 * 60, 6 * 60, priority=1, bp=15000)]
    excs = [E("long-replace", FRI, 21 * 60, 23 * 60, kind=REPLACE, bp=20000)]
    segments, _ = assert_matches_oracle(
        D(2026, 9, 25, 21), D(2026, 9, 25, 23), 6000, rules, excs)
    # 21-22 底下无周规则；22-23 底下是 night；例外 id/倍率不变也在边界断开
    assert [s.rule_id for s in segments] == [None, "night"]
    assert {s.exception_id for s in segments} == {"long-replace"}
    assert [s.basis_points for s in segments] == [20000, 20000]
    assert [s.minutes for s in segments] == [60, 60]


# ---------------------------------------------------------------------------
# 3) 相接但不重叠
# ---------------------------------------------------------------------------

def test_adjacent_windows_same_day_are_allowed():
    rules = [R("day", {5}, 0, 1440, priority=1, bp=15000)]
    excs = [
        E("a", SAT, 10 * 60, 11 * 60, kind=REPLACE, bp=20000),
        E("b", SAT, 11 * 60, 12 * 60, kind=SUPPRESS),
    ]
    segments, _ = assert_matches_oracle(
        D(2026, 9, 26, 9), D(2026, 9, 26, 13), 6000, rules, excs)
    # 09-10 weekend / 10-11 a(REPLACE) / 11-12 b(SUPPRESS) / 12-13 weekend
    assert [(s.exception_id, s.basis_points) for s in segments] == [
        (None, 15000), ("a", 20000), ("b", 10000), (None, 15000)]
    assert segments[1].end == segments[2].start == D(2026, 9, 26, 11)


def test_adjacent_single_minute_windows_are_allowed():
    excs = [
        E("a", SAT, 600, 601, kind=REPLACE, bp=20000),
        E("b", SAT, 601, 602, kind=REPLACE, bp=30000),
    ]
    segments, _ = assert_matches_oracle(
        D(2026, 9, 26, 9, 59), D(2026, 9, 26, 10, 3), 6000, [], excs)
    assert [(s.exception_id, s.minutes) for s in segments] == [
        (None, 1), ("a", 1), ("b", 1), (None, 1)]


def test_adjacent_windows_across_midnight_are_allowed():
    # a: 周五23:00-24:00；b: 周六00:00-02:00 —— 真实时间线在午夜相接
    excs = [
        E("a", FRI, 23 * 60, 24 * 60, kind=REPLACE, bp=20000),
        E("b", SAT, 0, 2 * 60, kind=SUPPRESS),
    ]
    segments, _ = assert_matches_oracle(
        D(2026, 9, 25, 22), D(2026, 9, 26, 2), 6000, [], excs)
    assert [(s.exception_id, s.exception_kind, s.minutes) for s in segments] == [
        (None, None, 60), ("a", REPLACE, 60), ("b", SUPPRESS, 120)]
    assert segments[1].end == D(2026, 9, 26, 0) == segments[2].start
    assert segments[2].start.date() == SAT  # 午夜后部分归次日


def test_cross_midnight_window_abutting_next_day_window_allowed():
    # a 跨午夜到周六 01:00；b 周六 01:00-02:00：恰好相接
    excs = [
        E("a", FRI, 23 * 60, 25 * 60, kind=REPLACE, bp=20000),
        E("b", SAT, 1 * 60, 2 * 60, kind=REPLACE, bp=30000),
    ]
    segments, _ = assert_matches_oracle(
        D(2026, 9, 25, 23), D(2026, 9, 26, 2), 6000, [], excs)
    assert [(s.exception_id, s.minutes) for s in segments] == [
        ("a", 60), ("a", 60), ("b", 60)]


# ---------------------------------------------------------------------------
# 4) 非法重叠：整次请求拒绝，与输入顺序无关
# ---------------------------------------------------------------------------

def test_overlapping_same_day_rejected_by_engine():
    excs = [
        E("a", SAT, 10 * 60, 12 * 60, kind=REPLACE, bp=20000),
        E("b", SAT, 11 * 60, 13 * 60, kind=SUPPRESS),
    ]
    with pytest.raises(ExceptionOverlapError) as ei:
        slice_shift(D(2026, 9, 26, 9), D(2026, 9, 26, 14), 6000, [], engine_excs(excs))
    assert ei.value.pairs == [("a", "b")]


def test_overlap_rejection_is_order_independent():
    a = E("zebra", SAT, 10 * 60, 12 * 60, kind=REPLACE, bp=20000)
    b = E("alpha", SAT, 11 * 60, 13 * 60, kind=SUPPRESS)

    err1 = err2 = None
    with pytest.raises(ExceptionOverlapError) as e1:
        slice_shift(D(2026, 9, 26, 9), D(2026, 9, 26, 14), 6000, [], engine_excs([a, b]))
    err1 = e1.value
    with pytest.raises(ExceptionOverlapError) as e2:
        slice_shift(D(2026, 9, 26, 9), D(2026, 9, 26, 14), 6000, [], engine_excs([b, a]))
    err2 = e2.value
    # id 对按 UTF-8 字节序规范化，两种输入顺序的拒绝结果完全相同
    assert err1.pairs == err2.pairs == [("alpha", "zebra")]


def test_cross_midnight_one_minute_overlap_rejected_but_abut_allowed():
    # a: 周五23:00 → 周六01:00（1500 分钟）；b: 周六 01:00 → 02:00：相接 → 允许
    abut = [
        E("a", FRI, 23 * 60, 25 * 60, kind=REPLACE, bp=20000),
        E("b", SAT, 1 * 60, 2 * 60, kind=REPLACE, bp=30000),
    ]
    segments, _ = slice_shift(D(2026, 9, 26, 1), D(2026, 9, 26, 2), 6000, [],
                              engine_excs(abut))
    assert segments[0].exception_id == "b"

    # b 提前一分钟到 00:59 → 与 a 重叠一分钟 → 拒绝（不能靠先后决定赢家）
    bad = [abut[0], E("b", SAT, 59, 2 * 60, kind=REPLACE, bp=30000)]
    with pytest.raises(ExceptionOverlapError) as ei:
        slice_shift(D(2026, 9, 26, 0), D(2026, 9, 26, 2), 6000, [], engine_excs(bad))
    assert ei.value.pairs == [("a", "b")]
    with pytest.raises(ExceptionOverlapError) as ei:
        slice_shift(D(2026, 9, 26, 0), D(2026, 9, 26, 2), 6000, [],
                    engine_excs(list(reversed(bad))))
    assert ei.value.pairs == [("a", "b")]


def test_overlap_reported_even_whole_overlap_lies_outside_shift():
    # 重叠发生在值班区间之外也必须拒绝（重叠是请求级合法性，不按覆盖到的分钟数决定）
    excs = [
        E("a", date(2026, 9, 28), 0, 120, kind=SUPPRESS),
        E("b", date(2026, 9, 28), 60, 180, kind=SUPPRESS),
    ]
    with pytest.raises(ExceptionOverlapError):
        slice_shift(D(2026, 9, 25, 10), D(2026, 9, 25, 11), 6000, [], engine_excs(excs))


def test_three_exceptions_all_overlapping_pairs_reported():
    excs = [
        E("c", SAT, 10 * 60, 11 * 60, kind=SUPPRESS),
        E("a", SAT, 10 * 60, 10 * 60 + 30, kind=REPLACE, bp=20000),
        E("b", SAT, 10 * 60 + 15, 11 * 60, kind=REPLACE, bp=30000),
    ]
    with pytest.raises(ExceptionOverlapError) as ei:
        slice_shift(D(2026, 9, 26, 9), D(2026, 9, 26, 12), 6000, [], engine_excs(excs))
    assert ei.value.pairs == [("a", "b"), ("a", "c"), ("b", "c")]


def test_non_overlapping_result_independent_of_input_order():
    rules = [R("day", {5}, 0, 1440, bp=15000)]
    excs = [
        E("b", SAT, 11 * 60, 12 * 60, kind=REPLACE, bp=30000),
        E("a", SAT, 10 * 60, 11 * 60, kind=SUPPRESS),
    ]
    s1, t1 = slice_shift(D(2026, 9, 26, 10), D(2026, 9, 26, 12), 6000,
                         [Rule("day", frozenset({5}), 0, 1440, 1, 15000)], engine_excs(excs))
    s2, t2 = slice_shift(D(2026, 9, 26, 10), D(2026, 9, 26, 12), 6000,
                         [Rule("day", frozenset({5}), 0, 1440, 1, 15000)],
                         engine_excs(list(reversed(excs))))
    assert t1 == t2
    assert [(x.start, x.end, x.exception_id, x.basis_points) for x in s1] == \
           [(x.start, x.end, x.exception_id, x.basis_points) for x in s2]


# ---------------------------------------------------------------------------
# 5) 未提交例外：旧结果逐项不变
# ---------------------------------------------------------------------------

OLD_CONTRACT_KEYS = {"start", "end", "minutes", "weekday", "ruleId", "basisPoints", "pay"}


def test_engine_output_unchanged_without_exceptions():
    rules = [
        R("night", range(7), 22 * 60, 6 * 60, priority=1, bp=12000),
        R("weekend", {5, 6}, 0, 1440, priority=2, bp=15000),
        R("special", {4}, 23 * 60, 1440, priority=3, bp=20000),
    ]
    engine_rules = [Rule(r["id"], frozenset(r["weekdays"]), r["start_minute"],
                         r["end_minute"], r["priority"], r["basis_points"]) for r in rules]
    old_segments, old_total = slice_shift(
        D(2026, 9, 25, 22), D(2026, 9, 26, 2), 6000, engine_rules)          # 旧调用方式
    new_segments, new_total = slice_shift(
        D(2026, 9, 25, 22), D(2026, 9, 26, 2), 6000, engine_rules, [])     # 显式空例外
    assert old_total == new_total
    assert len(old_segments) == len(new_segments)
    for a, b in zip(old_segments, new_segments):
        # 旧契约字段逐项一致
        assert (a.start, a.end, a.minutes, a.weekday, a.rule_id,
                a.basis_points, a.pay) == (b.start, b.end, b.minutes, b.weekday,
                                           b.rule_id, b.basis_points, b.pay)
        # 未提交例外时新字段只能是 None
        assert b.exception_id is None and b.exception_kind is None


def test_api_response_unchanged_without_exceptions():
    body = {
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
    r1 = client.post("/api/calculate", json=body)
    r2 = client.post("/api/calculate", json={**body, "exceptions": []})
    r3 = client.post("/api/calculate", json={**body, "exceptions": None})
    assert r1.status_code == r2.status_code == r3.status_code == 200
    j1, j2, j3 = r1.json(), r2.json(), r3.json()
    assert j1 == j2 == j3
    # README 记载的旧结果：三段 37200/1
    assert [s["ruleId"] for s in j1["segments"]] == ["night", "special", "weekend"]
    assert j1["total"]["numerator"] == 37200 and j1["total"]["denominator"] == 1
    for seg in j1["segments"]:
        # 无例外时每一段的旧契约字段保持原样，新字段为 null
        assert set(seg) >= OLD_CONTRACT_KEYS
        assert seg["exceptionId"] is None and seg["exceptionKind"] is None


# ---------------------------------------------------------------------------
# HTTP 层：例外生效 / 非法重叠 422 / 各类校验 422
# ---------------------------------------------------------------------------

def calc(payload):
    return client.post("/api/calculate", json=payload)


def test_api_replace_exception_happy_path():
    payload = {
        "start": "2026-09-26T00:00",
        "end": "2026-09-27T00:00",
        "baseRateCents": 6000,
        "rules": [{"id": "weekend", "weekdays": [5, 6], "startMinute": 0,
                   "endMinute": 1440, "priority": 10, "basisPoints": 15000}],
        "exceptions": [
            {"id": "duty", "date": "2026-09-26", "startMinute": 600,
             "endMinute": 720, "kind": "REPLACE", "basisPoints": 20000},
        ],
    }
    resp = calc(payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert [s["exceptionId"] for s in data["segments"]] == [None, "duty", None]
    middle = data["segments"][1]
    assert middle["exceptionKind"] == "REPLACE"
    assert middle["basisPoints"] == 20000
    assert middle["ruleId"] == "weekend"
    assert middle["minutes"] == 120
    for f in [data["total"]] + [s["pay"] for s in data["segments"]]:
        assert gcd(f["numerator"], f["denominator"]) == 1


def test_api_suppress_ignores_basis_points_field_when_absent():
    payload = {
        "start": "2026-09-26T10:00", "end": "2026-09-26T11:00",
        "baseRateCents": 6000, "rules": [],
        "exceptions": [{"id": "c", "date": "2026-09-26", "startMinute": 600,
                        "endMinute": 660, "kind": "SUPPRESS"}],
    }
    data = calc(payload)
    assert data.status_code == 200, data.text
    seg = data.json()["segments"][0]
    assert seg["exceptionKind"] == "SUPPRESS" and seg["basisPoints"] == 10000


def test_api_overlapping_exceptions_is_422():
    def payload(order):
        excs = [
            {"id": "a", "date": "2026-09-26", "startMinute": 600, "endMinute": 720,
             "kind": "REPLACE", "basisPoints": 20000},
            {"id": "b", "date": "2026-09-26", "startMinute": 660, "endMinute": 780,
             "kind": "SUPPRESS"},
        ]
        return {"start": "2026-09-26T09:00", "end": "2026-09-26T13:00",
                "baseRateCents": 6000, "rules": [], "exceptions": [excs[i] for i in order]}

    r1 = calc(payload([0, 1]))
    r2 = calc(payload([1, 0]))
    assert r1.status_code == r2.status_code == 422
    assert "重叠" in r1.text and "重叠" in r2.text


def test_api_cross_midnight_overlap_is_422():
    payload = {
        "start": "2026-09-25T22:00", "end": "2026-09-26T02:00",
        "baseRateCents": 6000, "rules": [],
        "exceptions": [
            {"id": "a", "date": "2026-09-25", "startMinute": 1380,
             "endMinute": 1500, "kind": "REPLACE", "basisPoints": 20000},
            {"id": "b", "date": "2026-09-26", "startMinute": 30,
             "endMinute": 120, "kind": "SUPPRESS"},
        ],
    }
    assert calc(payload).status_code == 422
    # b 改到 01:00 起即相接 → 200
    payload["exceptions"][1]["startMinute"] = 60
    assert calc(payload).status_code == 200


def test_api_suppress_with_basis_points_is_422():
    payload = {
        "start": "2026-09-26T10:00", "end": "2026-09-26T11:00",
        "baseRateCents": 6000, "rules": [],
        "exceptions": [{"id": "c", "date": "2026-09-26", "startMinute": 600,
                        "endMinute": 660, "kind": "SUPPRESS", "basisPoints": 12000}],
    }
    assert calc(payload).status_code == 422


def test_api_replace_without_basis_points_is_422():
    payload = {
        "start": "2026-09-26T10:00", "end": "2026-09-26T11:00",
        "baseRateCents": 6000, "rules": [],
        "exceptions": [{"id": "c", "date": "2026-09-26", "startMinute": 600,
                        "endMinute": 660, "kind": "REPLACE"}],
    }
    assert calc(payload).status_code == 422


def test_api_exception_empty_window_is_422():
    payload = {
        "start": "2026-09-26T10:00", "end": "2026-09-26T11:00",
        "baseRateCents": 6000, "rules": [],
        "exceptions": [{"id": "c", "date": "2026-09-26", "startMinute": 600,
                        "endMinute": 600, "kind": "SUPPRESS"}],
    }
    assert calc(payload).status_code == 422


def test_api_exception_end_before_start_is_422():
    payload = {
        "start": "2026-09-26T10:00", "end": "2026-09-26T11:00",
        "baseRateCents": 6000, "rules": [],
        "exceptions": [{"id": "c", "date": "2026-09-26", "startMinute": 700,
                        "endMinute": 660, "kind": "SUPPRESS"}],
    }
    assert calc(payload).status_code == 422


def test_api_exception_beyond_second_midnight_is_422():
    payload = {
        "start": "2026-09-26T10:00", "end": "2026-09-26T11:00",
        "baseRateCents": 6000, "rules": [],
        "exceptions": [{"id": "c", "date": "2026-09-26", "startMinute": 600,
                        "endMinute": 2881, "kind": "SUPPRESS"}],
    }
    assert calc(payload).status_code == 422


def test_api_duplicate_exception_id_is_422():
    payload = {
        "start": "2026-09-26T10:00", "end": "2026-09-26T13:00",
        "baseRateCents": 6000, "rules": [],
        "exceptions": [
            {"id": "x", "date": "2026-09-26", "startMinute": 600, "endMinute": 660,
             "kind": "SUPPRESS"},
            {"id": "x", "date": "2026-09-26", "startMinute": 660, "endMinute": 720,
             "kind": "SUPPRESS"},
        ],
    }
    assert calc(payload).status_code == 422


def test_api_exception_id_clashing_rule_is_422():
    payload = {
        "start": "2026-09-26T10:00", "end": "2026-09-26T11:00",
        "baseRateCents": 6000,
        "rules": [{"id": "x", "weekdays": [5], "startMinute": 0, "endMinute": 1440,
                   "priority": 1, "basisPoints": 15000}],
        "exceptions": [{"id": "x", "date": "2026-09-26", "startMinute": 600,
                        "endMinute": 660, "kind": "SUPPRESS"}],
    }
    assert calc(payload).status_code == 422


def test_api_too_many_exceptions_is_422():
    excs = [{"id": f"e{i}", "date": "2026-09-26", "startMinute": i * 15,
             "endMinute": i * 15 + 10, "kind": "SUPPRESS"} for i in range(11)]
    payload = {"start": "2026-09-26T00:00", "end": "2026-09-26T03:00",
               "baseRateCents": 6000, "rules": [], "exceptions": excs}
    assert calc(payload).status_code == 422


def test_api_bad_exception_date_is_422():
    payload = {
        "start": "2026-09-26T10:00", "end": "2026-09-26T11:00",
        "baseRateCents": 6000, "rules": [],
        "exceptions": [{"id": "c", "date": "2026-02-30", "startMinute": 600,
                        "endMinute": 660, "kind": "SUPPRESS"}],
    }
    assert calc(payload).status_code == 422


# ---------------------------------------------------------------------------
# 6) 随机化逐分钟对拍（含跨午夜 SUPPRESS/REPLACE 与周规则交错）
# ---------------------------------------------------------------------------

ID_POOL = ["night", "weekend", "special", "a", "b", "中", "夜", "z9"]
EXC_ID_POOL = ["hol", "temp", "duty", "甲", "乙", "x1"]


def make_exception_case(seed: int):
    rng = random.Random(seed)
    base_day = datetime(2026, 9, 14) + timedelta(days=rng.randrange(14),
                                                  hours=rng.randrange(24),
                                                  minutes=rng.choice([0, 15, 30, 45]))
    length = rng.randrange(1, 36 * 60 + 1)
    shift_end = base_day + timedelta(minutes=length)

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
    for _ in range(20):  # 至多尝试 20 次，贪心保留互不重叠的窗口
        if len(exceptions) >= 6:
            break
        eid = rng.choice([x for x in EXC_ID_POOL if x not in used_ids])
        anchor = (base_day.date() + timedelta(days=rng.randrange(-1, 3)))
        s = rng.randrange(0, 1440)
        e = s + rng.randrange(1, max(2, min(2 * 1440 - s, 1000)))
        kind = rng.choice([SUPPRESS, REPLACE])
        cand = {"id": eid, "date": anchor, "start_minute": s, "end_minute": e,
                "kind": kind,
                "basis_points": None if kind == SUPPRESS
                else rng.choice([5000, 10000, 12000, 18000, 25000])}
        if not oracle_overlapping_pairs(exceptions + [cand]):
            exceptions.append(cand)
            used_ids.add(eid)

    return (base_day, shift_end, rng.choice([1, 100, 6000, 12345]),
            rules, exceptions)


@pytest.mark.parametrize("seed", range(30))
def test_randomized_with_exceptions(seed):
    start, end, base, rules, excs = make_exception_case(20260925 + seed)
    assert_matches_oracle(start, end, base, rules, excs)
