"""Shared schedule-model primitives: the list of operating dates.

Closure days (Sun/Tue/Thu) never appear. Only Mon/Wed/Fri/Sat operating
dates are materialized, each carrying its week index, shift, demand and paid
hours so the solver, validator and exporter all share one view of the period.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .config import Config, ShiftDef


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
    """Materialize every operating date in the period in chronological order."""
    out: list[OperatingDate] = []
    start = cfg.start
    for wk in range(cfg.weeks):
        week_monday = start + timedelta(weeks=wk)
        for shift in sorted(cfg.operating_shifts, key=lambda s: s.weekday):
            d = week_monday + timedelta(days=shift.weekday)
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


def nurse_eligible_for(cfg_nurse, od: OperatingDate) -> bool:
    """True if the nurse may legally be assigned this operating date (H3).

    Honours unavailable_dates and the fixed_saturdays_off waiver.
    """
    if od.iso in set(cfg_nurse.unavailable_dates):
        return False
    if od.is_saturday and cfg_nurse.fixed_saturdays_off:
        return False
    return True
