"""Configuration model, defaults, and JSON persistence.

All operating-model numbers (shift lengths, paid hours, demand, FTE
tolerance) are defaults that the user may edit in the UI. They are stored
here as dataclasses and serialized to / from a local JSON file so settings
survive sessions (Section 1: "no database; config persisted to a local JSON
file").
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Optional

# --- Constants from the collective agreement ------------------------------

WEEKLY_FULL_TIME_HOURS = 37.5  # Art. 26.01
NORMAL_DAILY_FULL_SHIFT = 7.5  # Art. 26.01 (D10 exceeds this -> EWD memo 25.11)
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Default JSON config location (Section 1).
DEFAULT_CONFIG_PATH = "dialysis_config.json"


@dataclass
class ShiftDef:
    """A single operating-day shift definition."""

    weekday: int  # 0=Mon ... 6=Sun
    code: str  # "D10" or "D5"
    start: str  # "07:30"
    end: str  # "17:30"
    elapsed_hours: float
    paid_hours_unpaid_meal: float
    paid_hours_designated_meal: float

    @property
    def weekday_name(self) -> str:
        return WEEKDAY_NAMES[self.weekday]

    @property
    def is_saturday(self) -> bool:
        return self.weekday == 5

    def paid_hours(self, meal_designated_available: bool) -> float:
        """Paid hours for this shift given the meal designation flag.

        D5 is exactly 5.0 consecutive hours with no meal period (26.03(A)
        only triggers when working *longer than* 5 consecutive hours), so the
        designation flag never changes its paid hours.
        """
        if meal_designated_available:
            return self.paid_hours_designated_meal
        return self.paid_hours_unpaid_meal


@dataclass
class Nurse:
    """A roster member."""

    name: str
    target_fte: float
    fixed_saturdays_off: bool = False  # 25.06(B)/(E) waiver
    seniority_rank: int = 1  # 1 = most senior; tie-breaking only (25.03 ethos)
    unavailable_dates: list[str] = field(default_factory=list)  # ISO dates


@dataclass
class Config:
    """Full schedule-generation configuration."""

    start_date: str  # ISO date, must be a Monday (Section 3.1)
    weeks: int = 12  # allowed: 6, 9, 12, 18
    # Daily staffing demand by weekday short-name (Section 3.2).
    demand: dict = field(
        default_factory=lambda: {"Mon": 4, "Wed": 4, "Fri": 4, "Sat": 2}
    )
    # Optional per-week override: {week_index(int): {"Mon": n, ...}}.
    weekly_demand_override: dict = field(default_factory=dict)
    meal_designated_available: bool = False  # Section 2 / 26.03(B)(1)
    fte_tolerance: float = 0.08  # Section 3.5 / 25.03 tolerance band
    weekly_full_time_hours: float = WEEKLY_FULL_TIME_HOURS
    operating_shifts: list[ShiftDef] = field(default_factory=list)
    nurses: list[Nurse] = field(default_factory=list)

    # -- derived helpers ---------------------------------------------------

    @property
    def start(self) -> date:
        return datetime.strptime(self.start_date, "%Y-%m-%d").date()

    def shift_for_weekday(self, weekday: int) -> Optional[ShiftDef]:
        for s in self.operating_shifts:
            if s.weekday == weekday:
                return s
        return None

    def demand_for(self, week_index: int, weekday_name: str) -> int:
        """Demand for a given (0-based) week and weekday, honouring overrides."""
        ov = self.weekly_demand_override.get(week_index)
        if ov is None:
            ov = self.weekly_demand_override.get(str(week_index))
        if ov and weekday_name in ov:
            return int(ov[weekday_name])
        return int(self.demand.get(weekday_name, 0))

    # -- serialization -----------------------------------------------------

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        shifts = [ShiftDef(**s) for s in d.get("operating_shifts", [])]
        nurses = [Nurse(**n) for n in d.get("nurses", [])]
        known = {
            "start_date",
            "weeks",
            "demand",
            "weekly_demand_override",
            "meal_designated_available",
            "fte_tolerance",
            "weekly_full_time_hours",
        }
        kwargs = {k: d[k] for k in known if k in d}
        cfg = cls(operating_shifts=shifts, nurses=nurses, **kwargs)
        if not cfg.operating_shifts:
            cfg.operating_shifts = default_operating_shifts()
        return cfg

    def save(self, path: str = DEFAULT_CONFIG_PATH) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2)

    @classmethod
    def load(cls, path: str = DEFAULT_CONFIG_PATH) -> "Config":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))


def default_operating_shifts() -> list[ShiftDef]:
    """The unit's default operating model (Section 2).

    Mon/Wed/Fri D10 0730-1730 (10.0 elapsed; 9.5 paid unpaid-meal / 10.0 paid
    designated-available-meal). Sat D5 0730-1230 (5.0 elapsed / paid).
    """
    d10 = dict(
        code="D10",
        start="07:30",
        end="17:30",
        elapsed_hours=10.0,
        paid_hours_unpaid_meal=9.5,
        paid_hours_designated_meal=10.0,
    )
    return [
        ShiftDef(weekday=0, **d10),  # Monday
        ShiftDef(weekday=2, **d10),  # Wednesday
        ShiftDef(weekday=4, **d10),  # Friday
        ShiftDef(
            weekday=5,
            code="D5",
            start="07:30",
            end="12:30",
            elapsed_hours=5.0,
            paid_hours_unpaid_meal=5.0,
            paid_hours_designated_meal=5.0,
        ),  # Saturday
    ]


def default_nurses() -> list[Nurse]:
    """A sample roster so the app is usable out of the box.

    Targets are chosen to be exactly satisfiable against the default demand
    (Mon/Wed/Fri = 4, Sat = 2): over a 12-week period the unit needs 144
    weekday-shifts and 24 Saturday-shifts; these six nurses' patterns sum to
    exactly that, with no nurse exceeding the 25.06(E) Saturday cap.
    """
    return [
        Nurse("Avery", 0.83, seniority_rank=1),  # 3 wd/wk + ~alt Sat
        Nurse("Blake", 0.76, seniority_rank=2),  # 3 wd/wk, no Sat
        Nurse("Casey", 0.57, seniority_rank=3),  # 2 wd/wk + ~alt Sat
        Nurse("Dana", 0.57, seniority_rank=4),  # 2 wd/wk + ~alt Sat
        Nurse("Eden", 0.32, seniority_rank=5),  # 1 wd/wk + ~alt Sat
        Nurse("Finley", 0.25, seniority_rank=6, fixed_saturdays_off=True),
    ]


def default_config(start_date: Optional[str] = None) -> Config:
    """Build a fully-populated default Config.

    If no start date is supplied, the next Monday on/after today is used.
    """
    if start_date is None:
        today = date.today()
        # 0 = Monday
        days_ahead = (0 - today.weekday()) % 7
        start = today.fromordinal(today.toordinal() + days_ahead)
        start_date = start.isoformat()
    return Config(
        start_date=start_date,
        weeks=12,
        operating_shifts=default_operating_shifts(),
        nurses=default_nurses(),
    )


ALLOWED_WEEKS = [6, 9, 12, 18]
