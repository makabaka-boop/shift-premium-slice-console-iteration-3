"""精确切片核心：逐分钟裁决规则/一次性例外，在午夜与边界分段。

约定：
- 星期用 0..6 表示，0 = 周一（与 Python ``date.weekday()`` 一致）。
- 周规则时间窗口为半开区间 ``[startMinute, endMinute)``，单位为当天 00:00 起的分钟数。
- ``endMinute < startMinute`` 表示跨午夜：当天 [start, 1440) 与次日 [0, end)。
- ``endMinute == 1440``（即 24:00）覆盖当天全部剩余分钟，不算跨午夜。
- 同一分钟命中多条周规则时取 priority 最大者；仍并列取 id 的 UTF-8 字节序最小者；无规则命中按 10000。

一次性例外（节假日临时值班通知）：
- 例外锚定具体日期，窗口同样是半开区间，``endMinute`` 可超过 1440，超过部分落在真实次日，
  归属实际发生的那一天（午夜照常断开）。
- ``SUPPRESS``：该窗口按基础倍率 10000 计，任何周规则（含最高 priority）都不生效。
- ``REPLACE``：该窗口采用例外自带倍率，任何周规则都不生效。
- 例外优先于一切周规则；窗口之外仍按周规则的 priority/id 裁决。
- 两个例外窗口在真实时间线上有正时长重叠（相接不算）时，整次请求拒绝，
  裁决绝不依赖输入顺序。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from fractions import Fraction
from typing import Optional

# 默认倍率（无规则命中 / SUPPRESS 例外）
DEFAULT_BASIS_POINTS = 10000
BASIS_POINT_DENOMINATOR = 10000
MINUTES_PER_HOUR = 60
MINUTES_PER_DAY = 24 * MINUTES_PER_HOUR
MAX_SHIFT_MINUTES = 36 * MINUTES_PER_HOUR

# 例外类型
SUPPRESS = "SUPPRESS"
REPLACE = "REPLACE"
EXCEPTION_KINDS = (SUPPRESS, REPLACE)
# 例外最晚结束于次日 23:59（endMinute 半开，故上界为 2880）
MAX_EXCEPTION_END_MINUTE = 2 * MINUTES_PER_DAY


@dataclass(frozen=True)
class Rule:
    id: str
    weekdays: frozenset[int]
    start_minute: int
    end_minute: int
    priority: int
    basis_points: int

    def covers(self, weekday: int, minute_of_day: int) -> bool:
        """该规则是否覆盖某个（星期几, 当天第几分钟）。"""
        m = minute_of_day
        if self.end_minute > self.start_minute:
            # 普通窗口（含 00:00-24:00 全天）：仅当天 [start, end)
            return self.start_minute <= m < self.end_minute and weekday in self.weekdays
        # 跨午夜窗口：当天 [start, 1440) ∪ 次日 [0, end)
        return (
            (m >= self.start_minute and weekday in self.weekdays)
            or (m < self.end_minute and (weekday - 1) % 7 in self.weekdays)
        )


@dataclass(frozen=True)
class ExceptionWindow:
    """锚定具体日期的一次性例外。

    ``end_minute > 1440`` 表示跨午夜：超出部分落在真实次日。例如
    2026-09-25 23:00 起 3 小时 → start_minute=1380, end_minute=1560，
    覆盖 [09-25 23:00, 09-26 02:00)。
    """

    id: str
    date: date
    start_minute: int
    end_minute: int
    kind: str
    basis_points: Optional[int]  # 仅 REPLACE 有值；SUPPRESS 恒为 None

    @property
    def start_dt(self) -> datetime:
        return datetime.combine(self.date, time.min) + timedelta(minutes=self.start_minute)

    @property
    def end_dt(self) -> datetime:
        return self.start_dt + timedelta(minutes=self.end_minute - self.start_minute)

    def interval(self) -> tuple[datetime, datetime]:
        """真实时间线上的半开区间（跨午夜部分自然延伸到次日）。"""
        return self.start_dt, self.end_dt

    def covers(self, minute: datetime) -> bool:
        lo, hi = self.interval()
        return lo <= minute < hi


class ExceptionOverlapError(ValueError):
    """两个例外窗口在真实时间线上正时长重叠。报错与输入顺序无关。"""

    def __init__(self, pairs: list[tuple[str, str]]):
        self.pairs = pairs
        detail = "、".join(f"{a} ↔ {b}" for a, b in pairs)
        super().__init__(f"一次性例外窗口在真实时间线上重叠（相接不视为重叠）：{detail}")


def find_overlapping_exceptions(exceptions: list[ExceptionWindow]) -> list[tuple[str, str]]:
    """返回所有真实时间线上正时长重叠的例外 id 对。

    相接（a.end == b.start）不算重叠。结果按 id 的 UTF-8 字节序排序，
    与例外在请求中的先后顺序完全无关。
    """
    pairs: list[tuple[str, str]] = []
    for i in range(len(exceptions)):
        ai, a_hi = exceptions[i].start_dt, exceptions[i].end_dt
        for j in range(i + 1, len(exceptions)):
            b = exceptions[j]
            bi, b_hi = b.start_dt, b.end_dt
            if max(ai, bi) < min(a_hi, b_hi):
                a, bid = exceptions[i].id, b.id
                if a.encode("utf-8") <= bid.encode("utf-8"):
                    pairs.append((a, bid))
                else:
                    pairs.append((bid, a))
    pairs.sort(key=lambda p: (p[0].encode("utf-8"), p[1].encode("utf-8")))
    return pairs


@dataclass(frozen=True)
class ChosenRule:
    """一分钟裁决结果。

    rule_id 为 None 且 exception_id 也为 None 表示无规则命中（按 10000）。
    exception_id 非空表示该分钟由一次性例外接管（此时 rule_id 仍记录被压过的
    周规则，仅作解释用途，倍率以例外为准）。
    """

    rule_id: Optional[str]
    basis_points: int
    exception_id: Optional[str] = None
    exception_kind: Optional[str] = None


def choose_weekly_rule(minute: datetime, rules: list[Rule]) -> ChosenRule:
    """仅在周规则范围内裁决（旧行为，例外叠加前的基础层）。"""
    weekday = minute.weekday()
    minute_of_day = minute.hour * MINUTES_PER_HOUR + minute.minute

    winner: Optional[Rule] = None
    for rule in rules:
        if not rule.covers(weekday, minute_of_day):
            continue
        if winner is None:
            winner = rule
            continue
        if rule.priority > winner.priority:
            winner = rule
        elif rule.priority == winner.priority and rule.id.encode("utf-8") < winner.id.encode("utf-8"):
            winner = rule

    if winner is None:
        return ChosenRule(rule_id=None, basis_points=DEFAULT_BASIS_POINTS)
    return ChosenRule(rule_id=winner.id, basis_points=winner.basis_points)


def choose_rule(
    minute: datetime,
    rules: list[Rule],
    exceptions: Optional[list[ExceptionWindow]] = None,
) -> ChosenRule:
    """对一分钟做完整裁决：例外优先于一切周规则，窗口之外按周规则优先级裁决。"""
    base = choose_weekly_rule(minute, rules)
    if not exceptions:
        return base
    for exc in exceptions:
        if not exc.covers(minute):
            continue
        # 例外窗口在进入引擎前已两两校验不重叠，故命中即唯一。
        if exc.kind == SUPPRESS:
            return ChosenRule(rule_id=base.rule_id, basis_points=DEFAULT_BASIS_POINTS,
                              exception_id=exc.id, exception_kind=SUPPRESS)
        return ChosenRule(rule_id=base.rule_id, basis_points=exc.basis_points,
                          exception_id=exc.id, exception_kind=REPLACE)
    return base


@dataclass(frozen=True)
class Segment:
    start: datetime
    end: datetime
    minutes: int
    weekday: int
    rule_id: Optional[str]
    basis_points: int
    pay: Fraction
    exception_id: Optional[str] = None
    exception_kind: Optional[str] = None


def _minute_pay(base_rate_cents: int, basis_points: int) -> Fraction:
    """单分钟工资 = 时薪 * 倍率 * (1/60)，全程分数精确。"""
    return Fraction(base_rate_cents * basis_points, BASIS_POINT_DENOMINATOR * MINUTES_PER_HOUR)


def slice_shift(
    start: datetime,
    end: datetime,
    base_rate_cents: int,
    rules: list[Rule],
    exceptions: Optional[list[ExceptionWindow]] = None,
) -> tuple[list[Segment], Fraction]:
    """把 [start, end) 逐分钟切片并归并相邻同类段。

    分段键包含日期，因此跨午夜时即使规则相同也会在 00:00 处断开（例外跨午夜
    延伸到次日的分钟因此自然归入实际次日）。周规则边界、例外边界同样断开。
    """
    exceptions = exceptions or []
    overlaps = find_overlapping_exceptions(exceptions)
    if overlaps:
        raise ExceptionOverlapError(overlaps)

    segments: list[Segment] = []
    total = Fraction(0, 1)

    current = start
    current_date = start.date()
    chosen = choose_rule(current, rules, exceptions)

    seg_start = current
    seg_minutes = 0

    while current < end:
        current_chosen = choose_rule(current, rules, exceptions)
        # 规则变化、例外变化 或 越过午夜 → 关闭旧段
        if (
            current_chosen.rule_id != chosen.rule_id
            or current_chosen.basis_points != chosen.basis_points
            or current_chosen.exception_id != chosen.exception_id
            or current_chosen.exception_kind != chosen.exception_kind
            or current.date() != current_date
        ):
            pay = Fraction(seg_minutes * base_rate_cents * chosen.basis_points,
                           BASIS_POINT_DENOMINATOR * MINUTES_PER_HOUR)
            segments.append(Segment(
                start=seg_start,
                end=current,
                minutes=seg_minutes,
                weekday=seg_start.weekday(),
                rule_id=chosen.rule_id,
                basis_points=chosen.basis_points,
                pay=pay,
                exception_id=chosen.exception_id,
                exception_kind=chosen.exception_kind,
            ))
            total += pay
            seg_start = current
            seg_minutes = 0
            chosen = current_chosen
            current_date = current.date()

        seg_minutes += 1
        current += timedelta(minutes=1)

    if seg_minutes > 0:
        pay = Fraction(seg_minutes * base_rate_cents * chosen.basis_points,
                       BASIS_POINT_DENOMINATOR * MINUTES_PER_HOUR)
        segments.append(Segment(
            start=seg_start,
            end=current,
            minutes=seg_minutes,
            weekday=seg_start.weekday(),
            rule_id=chosen.rule_id,
            basis_points=chosen.basis_points,
            pay=pay,
            exception_id=chosen.exception_id,
            exception_kind=chosen.exception_kind,
        ))
        total += pay

    return segments, total
