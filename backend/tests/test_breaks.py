"""不计薪休息（经批准的跨午夜休息）的逐分钟对拍。

与 ``test_exceptions.py`` 相同的策略：本文件内置一个与引擎实现相互独立的 oracle
（独立的真实本地分钟半开区间覆盖判定、独立的重叠/越界校验），逐分钟跑一遍后与
引擎分段逐段对拍：休息覆盖（工资为零但保留原本命中的周规则/例外）、例外压过、
午夜断段、相接与重叠、越界拒绝、以及未提交休息时旧结果逐项不变。

关键约定（被测需求）：
- 休息优先于一切倍率规则（含一次性例外）：休息分钟工资为零；
- 分段仍保留该分钟原本命中的周规则 id 与例外 id/类型，供复核员解释；
- 计薪与休息片段在同一次逐分钟裁决中生成，总额是各段精确分数之和，
  不是「旧规则总额减去一个近似值」；
- 休息跨午夜仍在 00:00 断段；相接允许，正时长重叠/越界整次拒绝。
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
    BreakOutOfRangeError,
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
    """休息是真实本地分钟时间的半开区间 [start, end)。"""
    return brk["start"] <= dt < brk["end"]


def oracle_overlapping_break_pairs(breaks: list[dict]) -> list[tuple[tuple, tuple]]:
    """独立两两判定正时长重叠（相接不算），按 (start, end) 规范化排序。"""
    pairs = []
    for i in range(len(breaks)):
        a = (breaks[i]["start"], breaks[i]["end"])
        for j in range(i + 1, len(breaks)):
            b = (breaks[j]["start"], breaks[j]["end"])
            if max(a[0], b[0]) < min(a[1], b[1]):
                pairs.append((a, b) if a <= b else (b, a))
    pairs.sort()
    return pairs


def oracle_minute(
    dt: datetime, rules: list[dict], exceptions: list[dict], breaks: list[dict]
) -> tuple[str | None, int, str | None, str | None, bool]:
    """返回 (被压过的周规则id, 采用倍率, 例外id, 例外类型, 是否休息)。

    休息优先于一切倍率规则：休息分钟倍率为 0，但原本命中的周规则与例外
    原样保留（先按 例外>周规则 算出「原本会采用的裁决」，再盖上休息）。
    """
    weekly_id, weekly_bp = oracle_weekly_winner(dt, rules)
    rid, bp, eid, ekind = weekly_id, weekly_bp, None, None
    for exc in exceptions:  # 合法请求下至多命中一个
        if oracle_exc_covers(exc, dt):
            if exc["kind"] == SUPPRESS:
                rid, bp, eid, ekind = weekly_id, DEFAULT_BASIS_POINTS, exc["id"], SUPPRESS
            else:
                rid, bp, eid, ekind = weekly_id, exc["basis_points"], exc["id"], REPLACE
            break
    if any(oracle_break_covers(b, dt) for b in breaks):
        return rid, 0, eid, ekind, True
    return rid, bp, eid, ekind, False


def engine_breaks(breaks: list[dict]) -> list[BreakWindow]:
    return [BreakWindow(b["start"], b["end"]) for b in breaks]


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

    # 休息重叠判定本身也要与 oracle 一致
    engine_pairs = find_overlapping_breaks(engine_breaks(breaks))
    assert [((a.start, a.end), (b.start, b.end)) for a, b in engine_pairs] == \
        oracle_overlapping_break_pairs(breaks)

    segments, total = slice_shift(start, end, base, engine_rules, engine_excs,
                                  engine_breaks(breaks))

    expected_runs: list[dict] = []
    cur = start
    while cur < end:
        rid, bp, eid, ekind, is_break = oracle_minute(cur, rules, exceptions, breaks)
        key = (cur.date(), rid, bp, eid, ekind, is_break)
        if not expected_runs or expected_runs[-1]["key"] != key:
            expected_runs.append({"key": key, "start": cur, "minutes": 0, "rid": rid,
                                  "bp": bp, "eid": eid, "ekind": ekind, "brk": is_break})
        expected_runs[-1]["minutes"] += 1
        cur += timedelta(minutes=1)

    assert len(segments) == len(expected_runs), "分段数与 oracle 连跑不一致"
    expected_total = Fraction(0)
    expected_paid = 0
    for seg, run in zip(segments, expected_runs):
        assert seg.start == run["start"]
        assert seg.minutes == run["minutes"]
        assert seg.end == run["start"] + timedelta(minutes=run["minutes"])
        assert seg.weekday == run["start"].weekday()
        assert seg.rule_id == run["rid"]
        assert seg.basis_points == run["bp"]
        assert seg.exception_id == run["eid"]
        assert seg.exception_kind == run["ekind"]
        assert seg.is_break == run["brk"]
        pay = Fraction(run["minutes"] * base * run["bp"],
                       BASIS_POINT_DENOMINATOR * MINUTES_PER_HOUR)
        assert seg.pay == pay
        assert gcd(seg.pay.numerator, seg.pay.denominator) == 1
        if run["brk"]:
            assert seg.pay == 0, "休息段工资必须精确为零"
        else:
            expected_paid += run["minutes"]
        expected_total += pay
    assert total == expected_total
    assert gcd(total.numerator, total.denominator) == 1
    # 计薪/休息时长在同一次裁决中分别汇总
    assert sum(s.minutes for s in segments if not s.is_break) == expected_paid
    assert sum(s.minutes for s in segments if s.is_break) == \
        sum(r["minutes"] for r in expected_runs if r["brk"])
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


def B(start, end):
    return {"start": start, "end": end}


def D(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi)


FRI = date(2026, 9, 25)
SAT = date(2026, 9, 26)


def engine_excs(exceptions):
    return [ExceptionWindow(e["id"], e["date"], e["start_minute"], e["end_minute"],
                            e["kind"], e.get("basis_points")) for e in exceptions]


def engine_rules(rules):
    return [Rule(r["id"], frozenset(r["weekdays"]), r["start_minute"],
                 r["end_minute"], r["priority"], r["basis_points"]) for r in rules]


# ---------------------------------------------------------------------------
# 1) 休息覆盖：工资为零，原本命中的周规则保留，跨午夜断段
# ---------------------------------------------------------------------------

def test_break_zeroes_pay_but_keeps_weekly_rule_and_splits_at_midnight():
    # 周五 22:00 → 周六 02:00；night 规则 22:00-06:00（1.2×）
    rules = [R("night", range(7), 22 * 60, 6 * 60, priority=1, bp=12000)]
    # 休息 23:00 → 次日 01:00（跨午夜）
    breaks = [B(D(2026, 9, 25, 23), D(2026, 9, 26, 1))]

    segments, total = assert_matches_oracle(
        D(2026, 9, 25, 22), D(2026, 9, 26, 2), 6000, rules, [], breaks)

    assert [s.is_break for s in segments] == [False, True, True, False]
    assert [s.rule_id for s in segments] == ["night"] * 4  # 原本命中的周规则保留
    assert [s.basis_points for s in segments] == [12000, 0, 0, 12000]
    assert [s.minutes for s in segments] == [60, 60, 60, 60]
    assert [s.pay for s in segments] == [Fraction(7200), Fraction(0), Fraction(0),
                                         Fraction(7200)]
    # 休息跨午夜仍在 00:00 断段，后半归入实际次日
    assert segments[1].end == D(2026, 9, 26, 0)
    assert segments[2].start == D(2026, 9, 26, 0)
    assert segments[2].start.date() == SAT
    assert segments[2].weekday == SAT.weekday()
    # 总额 = 两段计薪之和（不是旧总额 28800 扣近似值）
    assert total == Fraction(14400, 1)


def test_break_with_no_rule_hit_keeps_null_rule():
    breaks = [B(D(2026, 9, 25, 10), D(2026, 9, 25, 10, 30))]
    segments, total = assert_matches_oracle(
        D(2026, 9, 25, 10), D(2026, 9, 25, 11), 6000, [], [], breaks)
    assert [s.is_break for s in segments] == [True, False]
    assert segments[0].rule_id is None
    assert segments[0].exception_id is None
    assert segments[0].pay == 0
    assert total == Fraction(30 * 6000, 60)  # 仅后 30 分钟计薪


def test_single_minute_break_is_exact_zero_fraction():
    # 1 分钟休息：工资精确 0/1；相邻计薪分钟分数不可约
    breaks = [B(D(2026, 9, 23, 10, 1), D(2026, 9, 23, 10, 2))]
    segments, total = assert_matches_oracle(
        D(2026, 9, 23, 10), D(2026, 9, 23, 10, 3), 100, [], [], breaks)
    assert [s.minutes for s in segments] == [1, 1, 1]
    assert segments[1].pay == Fraction(0, 1)
    assert total == Fraction(10, 3)  # 2 个计薪分钟 × 5/3


def test_break_covering_whole_shift_pays_zero():
    breaks = [B(D(2026, 9, 25, 22), D(2026, 9, 26, 2))]  # 与值班区间完全重合（允许）
    segments, total = assert_matches_oracle(
        D(2026, 9, 25, 22), D(2026, 9, 26, 2), 6000,
        [R("night", range(7), 22 * 60, 6 * 60, bp=12000)], [], breaks)
    assert all(s.is_break for s in segments)
    assert total == 0
    assert sum(s.minutes for s in segments) == 240


# ---------------------------------------------------------------------------
# 2) 休息压过一次性例外：例外 id/类型保留，工资为零
# ---------------------------------------------------------------------------

def test_break_overrides_replace_exception_and_keeps_it():
    # 例外 REPLACE 23:00 → 次日 02:00（2.0×）；休息 23:30 → 次日 00:30
    excs = [E("temp", FRI, 23 * 60, 26 * 60, kind=REPLACE, bp=20000)]
    breaks = [B(D(2026, 9, 25, 23, 30), D(2026, 9, 26, 0, 30))]
    segments, total = assert_matches_oracle(
        D(2026, 9, 25, 22), D(2026, 9, 26, 2), 6000, [], excs, breaks)

    assert [(s.is_break, s.exception_id, s.basis_points, s.minutes) for s in segments] == [
        (False, None, 10000, 60),   # 22:00-23:00 无规则
        (False, "temp", 20000, 30),  # 23:00-23:30 例外 2.0×
        (True, "temp", 0, 30),      # 23:30-24:00 休息（原本命中例外 temp）
        (True, "temp", 0, 30),      # 00:00-00:30 休息，跨午夜断段归次日
        (False, "temp", 20000, 90),  # 00:30-02:00 例外 2.0×
    ]
    assert segments[2].exception_kind == REPLACE
    assert segments[3].start.date() == SAT
    # 60m×1.0 + 30m×2.0 + 90m×2.0 = 6000 + 6000 + 18000
    assert total == Fraction(30000, 1)


def test_break_overrides_suppress_exception_and_keeps_kind():
    rules = [R("weekend", {5, 6}, 0, 1440, priority=10, bp=15000)]
    excs = [E("holiday", SAT, 0, 1440, kind=SUPPRESS)]
    breaks = [B(D(2026, 9, 26, 10), D(2026, 9, 26, 11))]
    segments, _ = assert_matches_oracle(
        D(2026, 9, 26, 9), D(2026, 9, 26, 12), 6000, rules, excs, breaks)
    assert [(s.is_break, s.exception_kind, s.basis_points) for s in segments] == [
        (False, SUPPRESS, 10000), (True, SUPPRESS, 0), (False, SUPPRESS, 10000)]
    # 休息段保留被压过的周规则与例外，供复核解释
    assert segments[1].rule_id == "weekend"
    assert segments[1].exception_id == "holiday"


def test_break_beats_every_multiplier_layer_at_once():
    # 同一分钟同时命中：高 priority 周末规则 + REPLACE 例外 + 休息 → 休息赢，工资为零
    rules = [R("weekend", {5, 6}, 0, 1440, priority=10, bp=15000)]
    excs = [E("duty", SAT, 10 * 60, 12 * 60, kind=REPLACE, bp=30000)]
    breaks = [B(D(2026, 9, 26, 10, 30), D(2026, 9, 26, 11, 30))]
    segments, total = assert_matches_oracle(
        D(2026, 9, 26, 10), D(2026, 9, 26, 12), 6000, rules, excs, breaks)
    assert [(s.is_break, s.basis_points, s.minutes) for s in segments] == [
        (False, 30000, 30), (True, 0, 60), (False, 30000, 30)]
    brk = segments[1]
    assert brk.rule_id == "weekend" and brk.exception_id == "duty"
    assert brk.exception_kind == REPLACE and brk.pay == 0
    # 仅两侧 60 分钟按 3.0× 计：60 × 6000 × 3 / 60 = 18000
    assert total == Fraction(18000, 1)


def test_break_inside_exception_still_splits_at_weekly_boundary():
    # 休息 21:00-23:00 内部 22:00 处周规则裁决变化（night 生效）→ 休息段也在边界断开，
    # 复核员能看到休息每一分钟原本命中哪条规则
    rules = [R("night", range(7), 22 * 60, 6 * 60, priority=1, bp=15000)]
    breaks = [B(D(2026, 9, 25, 21), D(2026, 9, 25, 23))]
    segments, total = assert_matches_oracle(
        D(2026, 9, 25, 21), D(2026, 9, 25, 23), 6000, rules, [], breaks)
    assert [s.rule_id for s in segments] == [None, "night"]
    assert all(s.is_break and s.pay == 0 for s in segments)
    assert total == 0


# ---------------------------------------------------------------------------
# 3) 相接允许（含跨午夜相接）
# ---------------------------------------------------------------------------

def test_adjacent_breaks_same_day_allowed():
    rules = [R("day", {5}, 0, 1440, priority=1, bp=15000)]
    breaks = [
        B(D(2026, 9, 26, 10), D(2026, 9, 26, 10, 30)),
        B(D(2026, 9, 26, 10, 30), D(2026, 9, 26, 11)),  # 与上一段相接
    ]
    segments, total = assert_matches_oracle(
        D(2026, 9, 26, 9), D(2026, 9, 26, 12), 6000, rules, [], breaks)
    # 两段相接休息、底下规则相同 → 归并为一段休息
    assert [(s.is_break, s.minutes) for s in segments] == [
        (False, 60), (True, 60), (False, 60)]
    assert total == Fraction((60 + 60) * 6000 * 15000, 600000)


def test_adjacent_breaks_across_midnight_allowed():
    # a: 23:00-24:00；b: 次日 00:00-01:00 —— 在午夜相接，合法；仍按午夜断段
    breaks = [
        B(D(2026, 9, 25, 23), D(2026, 9, 26, 0)),
        B(D(2026, 9, 26, 0), D(2026, 9, 26, 1)),
    ]
    segments, _ = assert_matches_oracle(
        D(2026, 9, 25, 22), D(2026, 9, 26, 2), 6000, [], [], breaks)
    assert [(s.is_break, s.minutes) for s in segments] == [
        (False, 60), (True, 60), (True, 60), (False, 60)]
    assert segments[1].end == D(2026, 9, 26, 0) == segments[2].start
    assert segments[2].start.date() == SAT


def test_adjacent_single_minute_breaks_allowed():
    breaks = [
        B(D(2026, 9, 26, 10, 0), D(2026, 9, 26, 10, 1)),
        B(D(2026, 9, 26, 10, 1), D(2026, 9, 26, 10, 2)),
    ]
    segments, total = assert_matches_oracle(
        D(2026, 9, 26, 9, 59), D(2026, 9, 26, 10, 3), 6000, [], [], breaks)
    assert [(s.is_break, s.minutes) for s in segments] == [
        (False, 1), (True, 2), (False, 1)]
    assert total == Fraction(2 * 6000, 60)


# ---------------------------------------------------------------------------
# 4) 正时长重叠 / 越界：整次请求拒绝，与输入顺序无关
# ---------------------------------------------------------------------------

def test_overlapping_breaks_rejected_by_engine():
    breaks = [
        B(D(2026, 9, 26, 10), D(2026, 9, 26, 12)),
        B(D(2026, 9, 26, 11), D(2026, 9, 26, 13)),
    ]
    with pytest.raises(BreakOverlapError) as ei:
        slice_shift(D(2026, 9, 26, 9), D(2026, 9, 26, 14), 6000, [], [],
                    engine_breaks(breaks))
    assert len(ei.value.pairs) == 1


def test_break_overlap_rejection_is_order_independent():
    a = B(D(2026, 9, 26, 10), D(2026, 9, 26, 12))
    b = B(D(2026, 9, 26, 11), D(2026, 9, 26, 13))
    with pytest.raises(BreakOverlapError) as e1:
        slice_shift(D(2026, 9, 26, 9), D(2026, 9, 26, 14), 6000, [], [],
                    engine_breaks([a, b]))
    with pytest.raises(BreakOverlapError) as e2:
        slice_shift(D(2026, 9, 26, 9), D(2026, 9, 26, 14), 6000, [], [],
                    engine_breaks([b, a]))
    key = lambda p: ((p[0].start, p[0].end), (p[1].start, p[1].end))
    assert [key(p) for p in e1.value.pairs] == [key(p) for p in e2.value.pairs]
    assert str(e1.value) == str(e2.value)


def test_cross_midnight_one_minute_break_overlap_rejected_but_abut_allowed():
    # a: 23:00 → 次日 01:00；b: 次日 01:00 → 02:00：相接 → 允许
    abut = [B(D(2026, 9, 25, 23), D(2026, 9, 26, 1)),
            B(D(2026, 9, 26, 1), D(2026, 9, 26, 2))]
    segments, _ = slice_shift(D(2026, 9, 25, 22), D(2026, 9, 26, 2), 6000, [], [],
                              engine_breaks(abut))
    # 相接的两段休息裁决相同：跨午夜仍在 00:00 断段，午夜后相邻休息分钟归并
    assert [(s.is_break, s.minutes) for s in segments] == [
        (False, 60), (True, 60), (True, 120)]
    assert segments[2].start == D(2026, 9, 26, 0)

    # b 提前一分钟到 00:59 → 与 a 重叠一分钟 → 拒绝（两种输入顺序结果相同）
    bad = [abut[0], B(D(2026, 9, 26, 0, 59), D(2026, 9, 26, 2))]
    for order in (bad, list(reversed(bad))):
        with pytest.raises(BreakOverlapError):
            slice_shift(D(2026, 9, 25, 22), D(2026, 9, 26, 2), 6000, [], [],
                        engine_breaks(order))


def test_break_out_of_range_rejected():
    shift = (D(2026, 9, 25, 22), D(2026, 9, 26, 2))
    # 开始早于值班开始 / 结束晚于值班结束 / 完全在区间外，一律越界拒绝
    for brk in [
        B(D(2026, 9, 25, 21, 59), D(2026, 9, 25, 23)),
        B(D(2026, 9, 26, 1), D(2026, 9, 26, 2, 1)),
        B(D(2026, 9, 26, 3), D(2026, 9, 26, 4)),
        B(D(2026, 9, 25, 20), D(2026, 9, 25, 21)),
    ]:
        with pytest.raises(BreakOutOfRangeError):
            slice_shift(*shift, 6000, [], [], engine_breaks([brk]))
    # 恰好等于值班边界 → 允许
    segments, total = slice_shift(*shift, 6000, [], [], engine_breaks([B(*shift)]))
    assert total == 0 and all(s.is_break for s in segments)


def test_break_overlap_reported_even_outside_shift_is_impossible():
    # 休息必须落在值班区间内，故「区间外重叠」必然先因越界被拒
    with pytest.raises((BreakOutOfRangeError, BreakOverlapError)):
        slice_shift(D(2026, 9, 25, 10), D(2026, 9, 25, 11), 6000, [], [],
                    engine_breaks([B(D(2026, 9, 28, 0), D(2026, 9, 28, 2)),
                                   B(D(2026, 9, 28, 1), D(2026, 9, 28, 3))]))


def test_empty_break_window_rejected_by_engine():
    with pytest.raises(ValueError):
        slice_shift(D(2026, 9, 26, 9), D(2026, 9, 26, 12), 6000, [], [],
                    engine_breaks([B(D(2026, 9, 26, 10), D(2026, 9, 26, 10))]))


# ---------------------------------------------------------------------------
# 5) 未提交休息：旧分段与总额逐项不变
# ---------------------------------------------------------------------------

OLD_CONTRACT_KEYS = {"start", "end", "minutes", "weekday", "ruleId", "basisPoints", "pay"}


def test_engine_output_unchanged_without_breaks():
    rules = [
        R("night", range(7), 22 * 60, 6 * 60, priority=1, bp=12000),
        R("weekend", {5, 6}, 0, 1440, priority=2, bp=15000),
        R("special", {4}, 23 * 60, 1440, priority=3, bp=20000),
    ]
    excs = [E("holiday", FRI, 23 * 60, 26 * 60, kind=SUPPRESS)]
    er, ee = engine_rules(rules), engine_excs(excs)
    old_segments, old_total = slice_shift(D(2026, 9, 25, 22), D(2026, 9, 26, 2),
                                          6000, er, ee)                 # 旧调用方式
    new_segments, new_total = slice_shift(D(2026, 9, 25, 22), D(2026, 9, 26, 2),
                                          6000, er, ee, [])             # 显式空休息
    assert old_total == new_total
    assert len(old_segments) == len(new_segments)
    for a, b in zip(old_segments, new_segments):
        assert (a.start, a.end, a.minutes, a.weekday, a.rule_id, a.basis_points,
                a.pay, a.exception_id, a.exception_kind) == \
               (b.start, b.end, b.minutes, b.weekday, b.rule_id, b.basis_points,
                b.pay, b.exception_id, b.exception_kind)
        assert b.is_break is False


def test_api_response_unchanged_without_breaks():
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
    r2 = client.post("/api/calculate", json={**body, "breaks": []})
    r3 = client.post("/api/calculate", json={**body, "breaks": None})
    assert r1.status_code == r2.status_code == r3.status_code == 200
    j1, j2, j3 = r1.json(), r2.json(), r3.json()
    assert j1 == j2 == j3
    # README 记载的旧结果：三段 37200/1，逐项不变
    assert [s["ruleId"] for s in j1["segments"]] == ["night", "special", "weekend"]
    assert j1["total"]["numerator"] == 37200 and j1["total"]["denominator"] == 1
    assert j1["totalMinutes"] == 240
    assert j1["paidMinutes"] == 240 and j1["breakMinutes"] == 0
    for seg in j1["segments"]:
        assert set(seg) >= OLD_CONTRACT_KEYS
        assert seg["isBreak"] is False


# ---------------------------------------------------------------------------
# HTTP 层：休息生效 / 各类非法 422
# ---------------------------------------------------------------------------

def calc(payload):
    return client.post("/api/calculate", json=payload)


def test_api_break_happy_path_aggregation():
    payload = {
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
        "breaks": [{"start": "2026-09-25T23:30", "end": "2026-09-26T00:30"}],
    }
    resp = calc(payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # 22-23 night / 23-23:30 special / 23:30-24 休息(special) / 00-00:30 休息(weekend) / 00:30-02 weekend
    assert [s["isBreak"] for s in data["segments"]] == [False, False, True, True, False]
    assert [s["ruleId"] for s in data["segments"]] == \
        ["night", "special", "special", "weekend", "weekend"]
    assert [s["basisPoints"] for s in data["segments"]] == [12000, 20000, 0, 0, 15000]
    # 休息跨午夜断段
    assert data["segments"][2]["end"] == "2026-09-26T00:00:00"
    assert data["segments"][3]["start"] == "2026-09-26T00:00:00"
    # 总时长 240 = 计薪 180 + 休息 60；总额 = 7200+6000+13500 = 26700
    assert data["totalMinutes"] == 240
    assert data["paidMinutes"] == 180
    assert data["breakMinutes"] == 60
    assert data["total"] == {"numerator": 26700, "denominator": 1,
                             "decimal": data["total"]["decimal"]}
    # 总额严格等于各段分数之和（同一次裁决生成，非旧总额扣近似值）
    acc = Fraction(0)
    for s in data["segments"]:
        f = Fraction(s["pay"]["numerator"], s["pay"]["denominator"])
        assert gcd(s["pay"]["numerator"], s["pay"]["denominator"]) == 1
        acc += f
    assert acc == Fraction(26700)


def test_api_break_overlapping_is_422_order_independent():
    def payload(order):
        brks = [
            {"start": "2026-09-25T23:00", "end": "2026-09-26T00:30"},
            {"start": "2026-09-26T00:00", "end": "2026-09-26T01:00"},
        ]
        return {"start": "2026-09-25T22:00", "end": "2026-09-26T02:00",
                "baseRateCents": 6000, "rules": [], "breaks": [brks[i] for i in order]}

    r1 = calc(payload([0, 1]))
    r2 = calc(payload([1, 0]))
    assert r1.status_code == r2.status_code == 422
    assert "重叠" in r1.text and "重叠" in r2.text
    # b 改到 00:30 起即相接 → 200
    ok = payload([0, 1])
    ok["breaks"][1]["start"] = "2026-09-26T00:30"
    assert calc(ok).status_code == 200


def test_api_break_out_of_range_is_422():
    base = {"start": "2026-09-25T22:00", "end": "2026-09-26T02:00",
            "baseRateCents": 6000, "rules": []}
    for brk in [
        {"start": "2026-09-25T21:00", "end": "2026-09-25T23:00"},   # 越界（早）
        {"start": "2026-09-26T01:00", "end": "2026-09-26T03:00"},   # 越界（晚）
        {"start": "2026-09-27T10:00", "end": "2026-09-27T11:00"},   # 完全在外
    ]:
        resp = calc({**base, "breaks": [brk]})
        assert resp.status_code == 422, brk
        assert "越出值班区间" in resp.text


def test_api_break_empty_or_reversed_window_is_422():
    base = {"start": "2026-09-25T22:00", "end": "2026-09-26T02:00",
            "baseRateCents": 6000, "rules": []}
    assert calc({**base, "breaks": [{"start": "2026-09-25T23:00",
                                     "end": "2026-09-25T23:00"}]}).status_code == 422
    assert calc({**base, "breaks": [{"start": "2026-09-26T00:00",
                                     "end": "2026-09-25T23:00"}]}).status_code == 422


def test_api_break_seconds_precision_is_422():
    payload = {"start": "2026-09-25T22:00", "end": "2026-09-26T02:00",
               "baseRateCents": 6000, "rules": [],
               "breaks": [{"start": "2026-09-25T23:00:30", "end": "2026-09-26T00:00"}]}
    assert calc(payload).status_code == 422


def test_api_break_with_timezone_is_422():
    payload = {"start": "2026-09-25T22:00", "end": "2026-09-26T02:00",
               "baseRateCents": 6000, "rules": [],
               "breaks": [{"start": "2026-09-25T23:00+08:00", "end": "2026-09-26T00:00"}]}
    assert calc(payload).status_code == 422


def test_api_too_many_breaks_is_422():
    brks = [{"start": f"2026-09-25T{22 + (i // 6):02d}:{(i % 6) * 10:02d}",
             "end": f"2026-09-25T{22 + (i // 6):02d}:{(i % 6) * 10 + 5:02d}"}
            for i in range(11)]
    payload = {"start": "2026-09-25T22:00", "end": "2026-09-26T02:00",
               "baseRateCents": 6000, "rules": [], "breaks": brks}
    assert calc(payload).status_code == 422


def test_api_exactly_ten_breaks_ok():
    brks = [{"start": f"2026-09-25T{22 + (i // 6):02d}:{(i % 6) * 10:02d}",
             "end": f"2026-09-25T{22 + (i // 6):02d}:{(i % 6) * 10 + 5:02d}"}
            for i in range(10)]
    payload = {"start": "2026-09-25T22:00", "end": "2026-09-26T02:00",
               "baseRateCents": 6000, "rules": [], "breaks": brks}
    resp = calc(payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["breakMinutes"] == 50
    assert data["paidMinutes"] == data["totalMinutes"] - 50


def test_api_break_bad_datetime_is_422():
    payload = {"start": "2026-09-25T22:00", "end": "2026-09-26T02:00",
               "baseRateCents": 6000, "rules": [],
               "breaks": [{"start": "2026-02-30T23:00", "end": "2026-09-26T00:00"}]}
    assert calc(payload).status_code == 422


# ---------------------------------------------------------------------------
# 6) 随机化逐分钟对拍（周规则 + 例外 + 休息三层交错）
# ---------------------------------------------------------------------------

ID_POOL = ["night", "weekend", "special", "a", "b", "中", "夜", "z9"]
EXC_ID_POOL = ["hol", "temp", "duty", "甲", "乙", "x1"]


def make_break_case(seed: int):
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
    for _ in range(20):
        if len(exceptions) >= 6:
            break
        eid = rng.choice([x for x in EXC_ID_POOL if x not in used_ids])
        anchor = base_day.date() + timedelta(days=rng.randrange(-1, 3))
        s = rng.randrange(0, 1440)
        e = s + rng.randrange(1, max(2, min(2 * 1440 - s, 1000)))
        kind = rng.choice([SUPPRESS, REPLACE])
        cand = {"id": eid, "date": anchor, "start_minute": s, "end_minute": e,
                "kind": kind,
                "basis_points": None if kind == SUPPRESS
                else rng.choice([5000, 10000, 12000, 18000, 25000])}
        lo, hi = oracle_exc_interval(cand)
        ok = True
        for x in exceptions:
            xlo, xhi = oracle_exc_interval(x)
            if max(lo, xlo) < min(hi, xhi):
                ok = False
                break
        if ok:
            exceptions.append(cand)
            used_ids.add(eid)

    breaks: list[dict] = []
    for _ in range(30):  # 贪心保留互不重叠且落在值班区间内的休息
        if len(breaks) >= 8:
            break
        off_s = rng.randrange(0, length)
        off_e = off_s + rng.randrange(1, min(length - off_s, 600) + 1)
        cand = {"start": base_day + timedelta(minutes=off_s),
                "end": base_day + timedelta(minutes=off_e)}
        if not oracle_overlapping_break_pairs(breaks + [cand]):
            breaks.append(cand)

    return (base_day, shift_end, rng.choice([1, 100, 6000, 12345]),
            rules, exceptions, breaks)


@pytest.mark.parametrize("seed", range(30))
def test_randomized_with_breaks(seed):
    start, end, base, rules, excs, breaks = make_break_case(20261006 + seed)
    assert_matches_oracle(start, end, base, rules, excs, breaks)
