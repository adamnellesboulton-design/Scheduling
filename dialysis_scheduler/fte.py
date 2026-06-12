"""FTE mathematics and the achievable-FTE dropdown menu (Section 4).

weekly_full_time_hours = 37.5 (Art. 26.01)
scheduled_fte(nurse) = total_paid_hours_assigned / (37.5 * weeks_in_period)

The achievable-FTE menu enumerates every (weekday_shifts_per_2wk,
saturday_shifts_per_2wk) combination, because only whole shifts can be
assigned. We present FTE values that the unit's operating hours can actually
produce -- full-time (1.0) is unreachable on Mon/Wed/Fri/Sat hours alone.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config, WEEKLY_FULL_TIME_HOURS

# A 2-week reference window: 6 possible weekday shifts (3/wk over Mon/Wed/Fri)
# and 2 possible Saturday shifts. Denominator is 37.5 * 2 = 75 paid hours.
TWO_WEEK_HOURS = WEEKLY_FULL_TIME_HOURS * 2  # 75.0
MAX_WEEKDAY_SHIFTS_PER_2WK = 6
MAX_SATURDAY_SHIFTS_PER_2WK = 2


@dataclass(frozen=True)
class FteOption:
    fte: float
    weekday_per_2wk: int
    saturday_per_2wk: int
    label: str


def _pattern_description(w: int, s: int) -> str:
    """Human label for a (weekday/2wk, sat/2wk) pattern, Section 4 style."""
    parts = []
    # weekdays per week (w is per 2 weeks; average per week = w/2)
    if w == 0:
        parts.append("0 weekdays")
    elif w % 2 == 0:
        parts.append(f"{w // 2} weekdays/wk")
    else:
        parts.append(f"{w} weekdays/2wk")
    if s == 0:
        parts.append("no Saturdays")
    elif s == 1:
        parts.append("alt Saturdays")
    elif s == 2:
        parts.append("every Saturday")
    return " + ".join(parts)


def achievable_fte_menu(cfg: Config) -> list[FteOption]:
    """Enumerate, dedupe and sort all achievable FTE values.

    Uses the configured D10 paid hours (which depend on the meal designation
    flag) and the D5 paid hours. Multiple (w, s) combinations can yield the
    same FTE; we keep the first (lowest-shift) representative for the label.
    """
    d10 = cfg.shift_for_weekday(0)  # Monday D10 is the weekday template
    sat = cfg.shift_for_weekday(5)
    d10_paid = d10.paid_hours(cfg.meal_designated_available) if d10 else 9.5
    sat_paid = sat.paid_hours(cfg.meal_designated_available) if sat else 5.0

    seen: dict[float, FteOption] = {}
    for w in range(0, MAX_WEEKDAY_SHIFTS_PER_2WK + 1):
        for s in range(0, MAX_SATURDAY_SHIFTS_PER_2WK + 1):
            hours = w * d10_paid + s * sat_paid
            fte = round(hours / TWO_WEEK_HOURS, 2)
            if fte not in seen:
                label = f"{fte:.2f} - {_pattern_description(w, s)}"
                seen[fte] = FteOption(fte, w, s, label)
    return sorted(seen.values(), key=lambda o: o.fte)


def max_achievable_fte(cfg: Config) -> float:
    menu = achievable_fte_menu(cfg)
    return max(o.fte for o in menu) if menu else 0.0


def max_achievable_fte_paid(cfg: Config) -> float:
    """Max achievable FTE if the designated-available meal were elected.

    Independent of the current meal flag, so the UI can show the higher cap as
    an option (e.g. 0.93 vs 0.89).
    """
    d10 = cfg.shift_for_weekday(0)
    sat = cfg.shift_for_weekday(5)
    d10_paid = d10.paid_hours_designated_meal if d10 else 10.0
    sat_paid = sat.paid_hours_designated_meal if sat else 5.0
    hours = MAX_WEEKDAY_SHIFTS_PER_2WK * d10_paid + MAX_SATURDAY_SHIFTS_PER_2WK * sat_paid
    return round(hours / TWO_WEEK_HOURS, 2)


def scheduled_fte(total_paid_hours: float, weeks: int) -> float:
    """scheduled_fte = total_paid_hours / (37.5 * weeks)."""
    denom = WEEKLY_FULL_TIME_HOURS * weeks
    if denom == 0:
        return 0.0
    return total_paid_hours / denom


def target_total_hours(target_fte: float, weeks: int) -> float:
    """Paid hours a nurse should accumulate over the period for a target FTE."""
    return target_fte * WEEKLY_FULL_TIME_HOURS * weeks
