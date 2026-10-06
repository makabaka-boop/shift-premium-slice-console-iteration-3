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

不计薪休息（经批准的休息）：
- 休息是真实本地分钟时间的半开区间 ``[start, end)``，必须整段落在值班区间内。
- 休息优先于一切倍率规则（含一次性例外）：休息分钟工资为零，但裁决结果仍保留
  该分钟原本命中的周规则与例外，供复核员解释「为什么这一分钟没有计费」。
- 计薪与休息片段在同一次逐分钟裁决中生成：休息分钟的倍率直接按 0 计入，
  总额是各段精确分数之和，绝不先按旧规则算总额再扣近似值。
- 相接（end==start）的休息允许；正时长重叠或越出值班区间整次请求拒绝，
  判定与输入顺序无关；休息跨午夜仍在 00:00 断段。
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
class BreakWindow:
    """经批准的不计薪休息：真实本地分钟时间的半开区间 [start, end)。

    休息窗口必须整段落在值班区间内（越界整次拒绝）。休息分钟工资为零，
    但该分钟原本命中的周规则/例外仍随分段返回，供复核员解释。
    """

    start: datetime
    end: datetime

    def interval(self) -> tuple[datetime, datetime]:
        return self.start, self.end

    def covers(self, minute: datetime) -> bool:
        return self.start <= minute < self.end


def _fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


class BreakOverlapError(ValueError):
    """两段休息在真实时间线上正时长重叠。报错与输入顺序无关。"""

    def __init__(self, pairs: list[tuple[BreakWindow, BreakWindow]]):
        self.pairs = pairs
        detail = "、".join(
            f"[{_fmt_dt(a.start)}, {_fmt_dt(a.end)}) ↔ [{_fmt_dt(b.start)}, {_fmt_dt(b.end)})"
            for a, b in pairs
        )
        super().__init__(f"休息窗口在真实时间线上重叠（相接不视为重叠）：{detail}")


class BreakOutOfRangeError(ValueError):
    """休息窗口越出值班区间（休息必须整段落在值班区间内）。"""

    def __init__(self, window: BreakWindow, shift_start: datetime, shift_end: datetime):
        self.window = window
        super().__init__(
            f"休息窗口 [{_fmt_dt(window.start)}, {_fmt_dt(window.end)}) 越出值班区间 "
            f"[{_fmt_dt(shift_start)}, {_fmt_dt(shift_end)})"
        )


def find_overlapping_breaks(breaks: list[BreakWindow]) -> list[tuple[BreakWindow, BreakWindow]]:
    """返回所有真实时间线上正时长重叠的休息窗口对。

    相接（a.end == b.start）不算重叠。窗口对按 (start, end) 规范化并排序，
    与休息在请求中的先后顺序完全无关。
    """
    pairs: list[tuple[BreakWindow, BreakWindow]] = []
    for i in range(len(breaks)):
        a = breaks[i]
        for j in range(i + 1, len(breaks)):
            b = breaks[j]
            if max(a.start, b.start) < min(a.end, b.end):
                lo, hi = (a, b) if (a.start, a.end) <= (b.start, b.end) else (b, a)
                pairs.append((lo, hi))
    pairs.sort(key=lambda p: (p[0].start, p[0].end, p[1].start, p[1].end))
    return pairs


def validate_breaks(
    breaks: list[BreakWindow], shift_start: datetime, shift_end: datetime
) -> None:
    """休息窗口整体校验：正时长、整段落在值班区间内、两两不正时长重叠。"""
    for b in breaks:
        if b.end <= b.start:
            raise ValueError("休息窗口结束时间必须晚于开始时间")
        if b.start < shift_start or b.end > shift_end:
            raise BreakOutOfRangeError(b, shift_start, shift_end)
    pairs = find_overlapping_breaks(breaks)
    if pairs:
        raise BreakOverlapError(pairs)


@dataclass(frozen=True)
class ChosenRule:
    """一分钟裁决结果。

    rule_id 为 None 且 exception_id 也为 None 表示无规则命中（按 10000）。
    exception_id 非空表示该分钟由一次性例外接管（此时 rule_id 仍记录被压过的
    周规则，仅作解释用途，倍率以例外为准）。
    is_break 为 True 表示该分钟处于经批准的休息内：倍率按 0 计（工资为零），
    rule_id / exception_id / exception_kind 仍记录该分钟原本命中的周规则与
    例外，仅供复核员解释，不参与计费。
    """

    rule_id: Optional[str]
    basis_points: int
    exception_id: Optional[str] = None
    exception_kind: Optional[str] = None
    is_break: bool = False


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
    breaks: Optional[list[BreakWindow]] = None,
) -> ChosenRule:
    """对一分钟做完整裁决：休息 > 一次性例外 > 周规则。

    休息分钟倍率按 0 计（工资为零），但保留原本命中的周规则与例外供复核；
    例外优先于一切周规则，窗口之外仍按周规则优先级裁决。
    """
    base = choose_weekly_rule(minute, rules)
    chosen = base
    if exceptions:
        for exc in exceptions:
            if not exc.covers(minute):
                continue
            # 例外窗口在进入引擎前已两两校验不重叠，故命中即唯一。
            if exc.kind == SUPPRESS:
                chosen = ChosenRule(rule_id=base.rule_id, basis_points=DEFAULT_BASIS_POINTS,
                                    exception_id=exc.id, exception_kind=SUPPRESS)
            else:
                chosen = ChosenRule(rule_id=base.rule_id, basis_points=exc.basis_points,
                                    exception_id=exc.id, exception_kind=REPLACE)
            break
    if breaks:
        for b in breaks:
            if b.covers(minute):
                # 休息优先于所有倍率规则：工资为零，原本命中的规则/例外原样保留。
                return ChosenRule(rule_id=chosen.rule_id, basis_points=0,
                                  exception_id=chosen.exception_id,
                                  exception_kind=chosen.exception_kind,
                                  is_break=True)
    return chosen


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
    # True 表示该段处于经批准的休息内：pay 恒为 0，rule_id/exception 字段
    # 仍记录段内分钟原本命中的周规则与例外，仅供复核解释
    is_break: bool = False


def _minute_pay(base_rate_cents: int, basis_points: int) -> Fraction:
    """单分钟工资 = 时薪 * 倍率 * (1/60)，全程分数精确。"""
    return Fraction(base_rate_cents * basis_points, BASIS_POINT_DENOMINATOR * MINUTES_PER_HOUR)


def slice_shift(
    start: datetime,
    end: datetime,
    base_rate_cents: int,
    rules: list[Rule],
    exceptions: Optional[list[ExceptionWindow]] = None,
    breaks: Optional[list[BreakWindow]] = None,
) -> tuple[list[Segment], Fraction]:
    """把 [start, end) 逐分钟切片并归并相邻同类段。

    分段键包含日期，因此跨午夜时即使规则相同也会在 00:00 处断开（例外跨午夜
    延伸到次日的分钟因此自然归入实际次日，休息跨午夜同样在 00:00 断段）。
    周规则边界、例外边界、休息边界同样断开。

    计薪与休息片段在同一次逐分钟裁决中生成：休息分钟倍率为 0，段工资精确为
    0，总额是各段分数之和——绝不先按旧规则算总额再扣一个近似值。
    """
    exceptions = exceptions or []
    overlaps = find_overlapping_exceptions(exceptions)
    if overlaps:
        raise ExceptionOverlapError(overlaps)

    breaks = breaks or []
    validate_breaks(breaks, start, end)

    segments: list[Segment] = []
    total = Fraction(0, 1)

    def close_segment(seg_start: datetime, seg_end: datetime,
                      seg_minutes: int, chosen: ChosenRule) -> None:
        nonlocal total
        pay = Fraction(seg_minutes * base_rate_cents * chosen.basis_points,
                       BASIS_POINT_DENOMINATOR * MINUTES_PER_HOUR)
        segments.append(Segment(
            start=seg_start,
            end=seg_end,
            minutes=seg_minutes,
            weekday=seg_start.weekday(),
            rule_id=chosen.rule_id,
            basis_points=chosen.basis_points,
            pay=pay,
            exception_id=chosen.exception_id,
            exception_kind=chosen.exception_kind,
            is_break=chosen.is_break,
        ))
        total += pay

    current = start
    current_date = start.date()
    chosen = choose_rule(current, rules, exceptions, breaks)

    seg_start = current
    seg_minutes = 0

    while current < end:
        current_chosen = choose_rule(current, rules, exceptions, breaks)
        # 规则变化、例外变化、休息状态变化 或 越过午夜 → 关闭旧段
        if (
            current_chosen.rule_id != chosen.rule_id
            or current_chosen.basis_points != chosen.basis_points
            or current_chosen.exception_id != chosen.exception_id
            or current_chosen.exception_kind != chosen.exception_kind
            or current_chosen.is_break != chosen.is_break
            or current.date() != current_date
        ):
            close_segment(seg_start, current, seg_minutes, chosen)
            seg_start = current
            seg_minutes = 0
            chosen = current_chosen
            current_date = current.date()

        seg_minutes += 1
        current += timedelta(minutes=1)

    if seg_minutes > 0:
        close_segment(seg_start, current, seg_minutes, chosen)

    return segments, total
