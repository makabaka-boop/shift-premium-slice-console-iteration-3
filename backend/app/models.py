"""请求/响应模型。对外字段使用 camelCase。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .engine import (
    MAX_EXCEPTION_END_MINUTE,
    MINUTES_PER_DAY,
    BreakWindow,
    ExceptionWindow,
    REPLACE,
    SUPPRESS,
    breaks_outside_shift,
    find_overlapping_breaks,
    find_overlapping_exceptions,
)

MAX_RULES = 20
MAX_EXCEPTIONS = 10
MAX_BREAKS = 10


def to_camel(s: str) -> str:
    parts = s.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class RuleIn(CamelModel):
    id: str = Field(min_length=1)
    weekdays: list[int] = Field(min_length=1)
    start_minute: int = Field(ge=0, le=1439)
    end_minute: int = Field(ge=1, le=1440)
    priority: int
    basis_points: int = Field(ge=1)

    @field_validator("id")
    @classmethod
    def id_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("规则 id 不能为空串")
        return v

    @field_validator("weekdays")
    @classmethod
    def weekdays_valid(cls, v: list[int]) -> list[int]:
        for d in v:
            if d < 0 or d > 6:
                raise ValueError("星期取值必须在 0..6（0=周一）")
        if len(set(v)) != len(v):
            raise ValueError("星期集合不能重复")
        return v

    @model_validator(mode="after")
    def window_not_empty(self) -> "RuleIn":
        # start == end 是空窗口（不把 00:00-00:00 解释为全天，避免歧义）
        if self.start_minute == self.end_minute:
            raise ValueError("时间窗口不能为空（startMinute 不得等于 endMinute）")
        return self


class ExceptionIn(CamelModel):
    """锚定具体日期的一次性例外（节假日临时值班通知）。

    endMinute 允许超过 1440：超过 1440 的部分落在真实次日（最多到次日 24:00）。
    SUPPRESS 窗口按基础倍率 10000 计；REPLACE 窗口采用自带倍率。
    """

    id: str = Field(min_length=1)
    date: date
    start_minute: int = Field(ge=0, le=MINUTES_PER_DAY - 1)
    end_minute: int = Field(ge=1, le=MAX_EXCEPTION_END_MINUTE)
    kind: Literal["SUPPRESS", "REPLACE"]
    # SUPPRESS 不需要倍率；REPLACE 必须给出正整数倍率
    basis_points: Optional[int] = Field(default=None, ge=1)

    @field_validator("id")
    @classmethod
    def id_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("例外 id 不能为空串")
        return v

    @model_validator(mode="after")
    def window_valid(self) -> "ExceptionIn":
        if self.end_minute <= self.start_minute:
            raise ValueError("例外窗口结束分钟必须晚于开始分钟（相接但不重叠请使用两个窗口）")
        if self.kind == REPLACE and self.basis_points is None:
            raise ValueError("REPLACE 例外必须给出 basisPoints")
        if self.kind == SUPPRESS and self.basis_points is not None:
            raise ValueError("SUPPRESS 例外按基础倍率计，不得提供 basisPoints")
        return self

    def to_window(self) -> ExceptionWindow:
        return ExceptionWindow(
            id=self.id,
            date=self.date,
            start_minute=self.start_minute,
            end_minute=self.end_minute,
            kind=self.kind,
            basis_points=self.basis_points if self.kind == REPLACE else None,
        )


class BreakIn(CamelModel):
    """经批准的不计薪休息：真实本地分钟时间上的半开区间 [start, end)。

    休息没有倍率字段：休息分钟一律不计薪，但响应仍保留其原本命中的周规则/例外。
    必须整体落在值班区间内（含与值班起止相接）、精确到分钟、结束晚于开始。
    """

    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def naive_local(cls, v: datetime) -> datetime:
        if v.tzinfo is not None:
            raise ValueError("时间必须为不带时区的本地时间")
        if v.second != 0 or v.microsecond != 0:
            raise ValueError("时间精确到分钟")
        return v

    @model_validator(mode="after")
    def window_positive(self) -> "BreakIn":
        if self.end <= self.start:
            raise ValueError("休息结束时间必须晚于开始时间（相接请使用两段休息）")
        return self

    def to_window(self) -> BreakWindow:
        return BreakWindow(start=self.start, end=self.end)


class ShiftRequest(CamelModel):
    start: datetime
    end: datetime
    base_rate_cents: int = Field(ge=1)
    rules: list[RuleIn] = Field(max_length=MAX_RULES, default_factory=list)
    # 显式 null 等价于缺省（旧客户端不传该字段），统一归一化为空列表
    exceptions: list[ExceptionIn] = Field(max_length=MAX_EXCEPTIONS, default_factory=list)
    # 经批准的不计薪休息（至多 10 段）；缺省 / null / [] 均走旧流程
    breaks: list[BreakIn] = Field(max_length=MAX_BREAKS, default_factory=list)

    @field_validator("exceptions", mode="before")
    @classmethod
    def null_exceptions_means_empty(cls, v):
        return [] if v is None else v

    @field_validator("breaks", mode="before")
    @classmethod
    def null_breaks_means_empty(cls, v):
        return [] if v is None else v

    @field_validator("start", "end")
    @classmethod
    def naive_local(cls, v: datetime) -> datetime:
        if v.tzinfo is not None:
            raise ValueError("时间必须为不带时区的本地时间")
        if v.second != 0 or v.microsecond != 0:
            raise ValueError("时间精确到分钟")
        return v

    @model_validator(mode="after")
    def interval_valid(self) -> "ShiftRequest":
        if self.end <= self.start:
            raise ValueError("值班区间结束时间必须晚于开始时间")
        minutes = int((self.end - self.start).total_seconds() // 60)
        if minutes > 36 * 60:
            raise ValueError("值班区间不得超过 36 小时")
        rule_ids = [r.id for r in self.rules]
        if len(set(rule_ids)) != len(rule_ids):
            raise ValueError("规则 id 必须唯一")
        exc_ids = [e.id for e in self.exceptions]
        if len(set(exc_ids)) != len(exc_ids):
            raise ValueError("例外 id 必须唯一")
        if set(rule_ids) & set(exc_ids):
            raise ValueError("例外 id 不得与周规则 id 重名")
        # 真实时间线两两重叠 → 整次请求拒绝（与输入顺序无关）
        windows = [e.to_window() for e in self.exceptions]
        pairs = find_overlapping_exceptions(windows)
        if pairs:
            detail = "、".join(f"{a} ↔ {b}" for a, b in pairs)
            raise ValueError(f"一次性例外窗口在真实时间线上重叠（相接不视为重叠）：{detail}")
        # 休息：越界、正时长重叠 → 整次请求拒绝；相接合法
        break_windows = [b.to_window() for b in self.breaks]
        outside = breaks_outside_shift(self.start, self.end, break_windows)
        if outside:
            b = outside[0]
            raise ValueError(
                f"休息 [{b.start:%Y-%m-%d %H:%M}, {b.end:%Y-%m-%d %H:%M}) 越界："
                f"必须整体位于值班区间 [{self.start:%Y-%m-%d %H:%M}, {self.end:%Y-%m-%d %H:%M}) 内")
        break_pairs = find_overlapping_breaks(break_windows)
        if break_pairs:
            detail = "、".join(f"[{a:%m-%d %H:%M}, {b:%m-%d %H:%M})" for a, b in break_pairs)
            raise ValueError(f"休息时间在真实时间线上重叠（相接不视为重叠）：{detail}")
        return self


class FractionOut(CamelModel):
    numerator: int
    denominator: int
    decimal: str


class SegmentOut(CamelModel):
    start: datetime
    end: datetime
    minutes: int
    weekday: int
    rule_id: Optional[str]
    basis_points: int
    pay: FractionOut
    # 非空表示该段被一次性例外接管；SUPPRESS 时 basisPoints=10000，
    # ruleId 仍记录被压过的周规则（可能为 null），仅作解释用途
    exception_id: Optional[str] = None
    exception_kind: Optional[Literal["SUPPRESS", "REPLACE"]] = None
    # true 表示该段为经批准的不计薪休息：pay 恒为 0，其余字段保留该段分钟
    # 原本命中的周规则/例外（休息优先于一切倍率规则），仅供复核解释
    is_break: bool = False


class CalculateResponse(CamelModel):
    segments: list[SegmentOut]
    total: FractionOut
    total_minutes: int
    # 计薪分钟与休息分钟分项汇总（totalMinutes = paidMinutes + breakMinutes）
    paid_minutes: int = 0
    break_minutes: int = 0
