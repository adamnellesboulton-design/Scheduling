"""Configuration model, defaults, and JSON persistence.

All operating-model numbers (shift lengths, paid hours, demand, FTE
tolerance) are defaults that the user may edit in the UI. They are stored
here as dataclasses and serialized to / from a local JSON file so settings
survive sessions (Section 1: "no database; config persisted to a local JSON
file").
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict, fields as dataclass_fields
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
    """A roster member.

    The primary targets are explicit shift counts over the rotation:
    `target_d10` (number of 10-hour weekday shifts) and `target_d5` (number of
    5-hour Saturday shifts). `target_fte` is derived from those counts for
    display / secondary reporting and is kept in sync by Config.apply_derived_ftes.
    """

    name: str
    target_fte: float = 0.0  # derived from the shift counts (see above)
    target_d10: int = 0  # desired # of 10-hour weekday shifts (0-40)
    target_d5: int = 0  # desired # of 5-hour Saturday shifts (0-10)
    stat_days: int = 0  # paid statutory-holiday days (Art. 17): reduce worked D10
    unavailable_dates: list[str] = field(default_factory=list)  # ISO dates
    # Per-line FTE flex (± tolerance). None -> use the config-wide default.
    fte_tolerance: Optional[float] = None
    # Job-share label: lines sharing the same non-empty label never work the
    # same day (two people splitting one line). Empty = no job share.
    job_share_group: str = ""
    # Line preferences (soft).
    pref_nonconsec_sat: bool = False  # avoid back-to-back Saturdays
    pref_clustered: bool = False  # prefer worked days grouped (e.g. Fri+Sat)
    pref_off_mon: bool = False  # prefer Mondays off
    pref_off_wed: bool = False  # prefer Wednesdays off
    pref_off_fri: bool = False  # prefer Fridays off

    def tolerance(self, default: float) -> float:
        return self.fte_tolerance if self.fte_tolerance is not None else default

    def worked_d10(self) -> int:
        """Weekday shifts actually scheduled (stat days are paid but not worked)."""
        return max(0, self.target_d10 - self.stat_days)

    def target_hours(self, d10_paid: float, sat_paid: float) -> float:
        return self.target_d10 * d10_paid + self.target_d5 * sat_paid


@dataclass
class Config:
    """Full schedule-generation configuration."""

    start_date: str  # ISO date, must be a Monday (Section 3.1)
    weeks: int = 12  # allowed: 6, 9, 12, 18
    # Daily staffing demand by weekday short-name (Section 3.2).
    demand: dict = field(
        default_factory=lambda: {"Mon": 3, "Wed": 3, "Fri": 3, "Sat": 2}
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

    def d10_paid(self) -> float:
        s = self.shift_for_weekday(0)
        return s.paid_hours(self.meal_designated_available) if s else 9.5

    def sat_paid(self) -> float:
        s = self.shift_for_weekday(5)
        return s.paid_hours(self.meal_designated_available) if s else 5.0

    def apply_derived_ftes(self) -> None:
        """Recompute each nurse's target_fte from their shift counts."""
        denom = self.weekly_full_time_hours * self.weeks
        d10p, satp = self.d10_paid(), self.sat_paid()
        for n in self.nurses:
            hours = n.target_hours(d10p, satp)
            n.target_fte = round(hours / denom, 3) if denom else 0.0

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
        # Tolerate unknown / removed keys (forward & backward compatibility) by
        # filtering each dict to the fields the dataclass actually declares.
        def _only(cl, raw):
            allowed = {f.name for f in dataclass_fields(cl)}
            return {k: v for k, v in (raw or {}).items() if k in allowed}

        shifts = [ShiftDef(**_only(ShiftDef, s)) for s in d.get("operating_shifts", [])]
        nurses = [Nurse(**_only(Nurse, n)) for n in d.get("nurses", [])]
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
    """The unit roster, pre-populated for the app (display order only).

    Shift-count targets are sized to match the default demand over 12 weeks
    (Mon/Wed/Fri = 3 -> 108 D10 shifts; Sat = 2 -> 24 D5 shifts) so the default
    schedule needs no extra coverage. (FTE is derived from the counts.)
    """
    return [
        Nurse("Kathleen", target_d10=24, target_d5=6),
        Nurse("Adam", target_d10=24, target_d5=5),
        Nurse("Joane", target_d10=21, target_d5=5),
        Nurse("Leslie", target_d10=21, target_d5=4),
        Nurse("Kaitlyn", target_d10=18, target_d5=4),
    ]


START_WEEKDAY = 4  # Friday: the rotation starts on a Friday


def next_start_day(today: Optional[date] = None) -> date:
    """The next rotation-start weekday (Friday) on/after today."""
    today = today or date.today()
    days_ahead = (START_WEEKDAY - today.weekday()) % 7
    return today.fromordinal(today.toordinal() + days_ahead)


def default_config(start_date: Optional[str] = None) -> Config:
    """Build a fully-populated default Config.

    If no start date is supplied, the next Friday on/after today is used.
    """
    if start_date is None:
        start_date = next_start_day().isoformat()
    cfg = Config(
        start_date=start_date,
        weeks=12,
        operating_shifts=default_operating_shifts(),
        nurses=default_nurses(),
    )
    cfg.apply_derived_ftes()
    return cfg


ALLOWED_WEEKS = [6, 9, 12, 18]
