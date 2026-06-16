"""Shared schedule-model primitives: the list of operating dates.

Closure days (Sun/Tue/Thu) never appear. Only Mon/Wed/Fri/Sat operating
dates are materialized, each carrying its week index, shift, demand and paid
hours so the solver, validator and exporter all share one view of the period.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .config import Config, ShiftDef

# Cell codes that count as a *worked* shift (vs "ST" stat-holiday off / "LV").
WORK_CODES = ("D10", "D5")


def is_worked(code) -> bool:
    return code in WORK_CODES


@dataclass
class OperatingDate:
    d: date
    week_index: int  # 0-based
    weekday: int  # 0=Mon ... 5=Sat
    weekday_name: str
    shift: ShiftDef
    demand: int
    paid_hours: float

    @property
    def iso(self) -> str:
        return self.d.isoformat()

    @property
    def is_saturday(self) -> bool:
        return self.weekday == 5


def build_operating_dates(cfg: Config) -> list[OperatingDate]:
    """Materialize every operating date in the period in chronological order.

    Weeks are 7-day blocks anchored to the rotation start weekday (a Friday),
    so each block still contains the four operating days (Fri, Sat, Mon, Wed)
    even though the rotation no longer starts on a Monday.
    """
    out: list[OperatingDate] = []
    start = cfg.start
    start_wd = start.weekday()
    for wk in range(cfg.weeks):
        block_start = start + timedelta(weeks=wk)
        for shift in sorted(cfg.operating_shifts, key=lambda s: s.weekday):
            offset = (shift.weekday - start_wd) % 7
            d = block_start + timedelta(days=offset)
            out.append(
                OperatingDate(
                    d=d,
                    week_index=wk,
                    weekday=shift.weekday,
                    weekday_name=shift.weekday_name,
                    shift=shift,
                    demand=cfg.demand_for(wk, shift.weekday_name),
                    paid_hours=shift.paid_hours(cfg.meal_designated_available),
                )
            )
    out.sort(key=lambda od: (od.d, od.weekday))
    return out


def saturday_dates(operating: list[OperatingDate]) -> list[OperatingDate]:
    return [od for od in operating if od.is_saturday]


def business_week_index(d: date, start: date, weeks: int) -> int:
    """Map a date to its Mon-Fri *business* week (0..weeks-1).

    A business week runs Monday->Sunday. The rotation is anchored on a Friday,
    so within one 7-day rotation block the Friday closes the business week that
    its Mon/Wed (three days earlier) opened. In other words a single business
    week's operating days are the Mon + Wed of one block and the Friday of the
    *next* block -- they straddle the rotation-week boundary.

    The rotation is cyclic (it loops straight back into its own start), so the
    trailing Mon/Wed of the final block and the leading Friday of the first
    block belong to the same business week; the `% weeks` wraps them together.
    Grouping the "work every week" guarantee by this index (instead of by the
    Friday-anchored rotation week) is what makes it mean "one shift per Mon-Fri
    business week", and stops the seam artefact where one end of the rotation
    gets a doubled-up week and the other gets an empty one.
    """
    first_monday_offset = (-start.weekday()) % 7  # days from start to first Monday
    days_from_start = (d - start).days
    return ((days_from_start - first_monday_offset) // 7) % weeks


def business_weekday_ois_by_week(operating, start: date, weeks: int) -> dict:
    """{business_week_index: [operating indices of its weekday (non-Sat) days]}."""
    groups: dict[int, list[int]] = {}
    for oi, od in enumerate(operating):
        if od.is_saturday:
            continue
        bw = business_week_index(od.d, start, weeks)
        groups.setdefault(bw, []).append(oi)
    return groups


def nurse_eligible_for(cfg_nurse, od: OperatingDate) -> bool:
    """True if the nurse may be assigned this operating date (H3).

    Honours only unavailable_dates -- everyone works Saturdays (there is no
    blanket Saturday waiver; the >=1 Saturday/month rule applies to all).
    """
    return od.iso not in set(cfg_nurse.unavailable_dates)
