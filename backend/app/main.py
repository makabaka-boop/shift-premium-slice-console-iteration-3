"""FastAPI 入口：POST /api/calculate 精确切片。"""

from __future__ import annotations

from fractions import Fraction

from fastapi import FastAPI

from .engine import Rule, Segment, slice_shift
from .models import CalculateResponse, FractionOut, SegmentOut, ShiftRequest

app = FastAPI(title="值班津贴复核台 API", version="1.0.0")


def _fraction_out(f: Fraction) -> FractionOut:
    return FractionOut(
        numerator=f.numerator,
        denominator=f.denominator,
        decimal=_decimal(f),
    )


def _decimal(f: Fraction, places: int = 6) -> str:
    """仅供页面阅读的小数（分数才是权威值，不参与中间计算）。"""
    q = 10**places
    rounded = (f.numerator * q + f.denominator // 2) // f.denominator
    whole, frac = divmod(rounded, q)
    return f"{whole}.{frac:0{places}d}"


def _segment_out(seg: Segment) -> SegmentOut:
    return SegmentOut(
        start=seg.start,
        end=seg.end,
        minutes=seg.minutes,
        weekday=seg.weekday,
        rule_id=seg.rule_id,
        basis_points=seg.basis_points,
        pay=_fraction_out(seg.pay),
        exception_id=seg.exception_id,
        exception_kind=seg.exception_kind,
        is_break=seg.is_break,
    )


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/calculate", response_model=CalculateResponse)
def calculate(req: ShiftRequest) -> CalculateResponse:
    rules = [
        Rule(
            id=r.id,
            weekdays=frozenset(r.weekdays),
            start_minute=r.start_minute,
            end_minute=r.end_minute,
            priority=r.priority,
            basis_points=r.basis_points,
        )
        for r in req.rules
    ]
    segments, total = slice_shift(
        req.start,
        req.end,
        req.base_rate_cents,
        rules,
        [e.to_window() for e in req.exceptions],
        [b.to_window() for b in req.breaks],
    )
    total_minutes = sum(s.minutes for s in segments)
    paid_minutes = sum(s.minutes for s in segments if not s.is_break)
    return CalculateResponse(
        segments=[_segment_out(s) for s in segments],
        total=_fraction_out(total),
        total_minutes=total_minutes,
        paid_minutes=paid_minutes,
        break_minutes=total_minutes - paid_minutes,
    )
