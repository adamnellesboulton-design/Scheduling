"""Schedule generation.

CP-SAT (OR-Tools) builds the master schedule under the hard constraints
(coverage, Saturday cap, job share, >=1 Saturday/month, ...) and optimizes the
weighted soft objectives -- chiefly hitting each line's requested D10/D5 shift
counts. If no feasible solution is found, a diagnostic pass identifies the
binding requirement and a greedy fallback produces a best-effort schedule.

All hours are carried in integer half-hour units inside the model (9.5h -> 19,
5.0h -> 10) because CP-SAT is integer-only.
"""

from __future__ import annotations

import math
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from ortools.sat.python import cp_model

from .config import Config, Nurse, WEEKLY_FULL_TIME_HOURS
from .model import (
    OperatingDate,
    build_operating_dates,
    saturday_dates,
    nurse_eligible_for,
)
from .holidays import holidays_in_range


def stat_holiday_indices(cfg: Config, operating: list) -> list:
    """Indices of weekday statutory-holiday operating dates in the rotation.

    (Stat days reduce worked D10, so only weekday holidays are eligible.)
    """
    hols = {
        d.isoformat()
        for d, _ in holidays_in_range(cfg.start, cfg.start + timedelta(weeks=cfg.weeks))
    }
    return [oi for oi, od in enumerate(operating)
            if not od.is_saturday and od.iso in hols]


def effective_stat(nurse: Nurse, stat_ois: list, operating: list) -> int:
    """Stat days a nurse can actually take = min(entitlement, eligible holidays)."""
    unavail = set(nurse.unavailable_dates)
    elig = sum(1 for oi in stat_ois if operating[oi].iso not in unavail)
    return min(nurse.stat_days, elig)


# Soft-objective weights.
#
# Each line's requested SHIFT COUNTS (worked D10 + D5) are HARD constraints --
# guaranteed in every option. COVERAGE is SOFT: if the roster can't cover every
# day, shifts are left blank (under-staffed) rather than failing, and the gaps
# are flagged. W_SHORTFALL dominates the arrangement weights so the solver fills
# as many demanded slots as the fixed counts allow.
W_SHORTFALL = 8000  # penalty per unfilled (blank) shift -- minimized first
W_THREE_OF_FOUR = 1500  # low-FTE lines: push to work >=3 of every 4 weeks
W_EXTRA = 600  # penalty per extra (over-demand) nurse on a day

# Three options, each maximizing a different secondary goal. The dict gives the
# weight of each differentiating term per profile.
OBJECTIVE_PROFILES = {
    "preference": {
        "label": "Preference-maximizing",
        "pref": 900,        # satisfy ticked line preferences
        "wd_equity": 30,
        "sat_spread": 30,
        "cluster_all": 0,
        "pattern": 250,
    },
    "equity": {
        "label": "Equity-maximizing",
        "pref": 60,
        "wd_equity": 500,   # balance weekday types across nurses
        "sat_spread": 350,  # space each nurse's Saturdays evenly
        "cluster_all": 0,
        "pattern": 250,
    },
    "cluster": {
        "label": "Cluster-maximizing",
        "pref": 60,
        "wd_equity": 30,
        "sat_spread": 30,
        "cluster_all": 600,  # group everyone's shifts -> long days off
        "pattern": 150,
    },
}
PROFILE_ORDER = ["preference", "equity", "cluster"]

# Lines below this FTE should work in >=3 of every rolling 4 weeks (soft).
LOW_FTE_THRESHOLD = 0.30
THREE_OF_FOUR_WINDOW = 4
THREE_OF_FOUR_MIN_ACTIVE = 3

# Everyone (not waived) works >=1 Saturday per rolling 4-week window (H9).
SAT_PER_MONTH_WINDOW = 4

# The deterministic time limit governs the stopping point (reproducible). The
# wall-clock cap is a pure safety valve set well above it so it never fires on
# normal hardware and therefore never injects nondeterminism.
DET_TIME_LIMIT = 8.0  # deterministic time units (solution plateaus well before this)
SOLVER_TIME_LIMIT_S = 90.0  # wall-clock safety cap
RANDOM_SEED = 42
ALT_DET_TIME = 5.0  # per-option solve budget (three options solved in parallel)

# H2: max Saturdays per rolling 9-week window (>= 1 weekend off in 3).
SAT_MAX_PER_9WK = 6
SAT_WINDOW_WEEKS = 9
SAT_OFF_PER_WINDOW = 3


def half_hours(hours: float) -> int:
    return int(round(hours * 2))


@dataclass
class ScheduleResult:
    feasible: bool
    method: str  # "cp-sat", "cp-sat-relaxed", "greedy", "none"
    status: str
    assignments: dict = field(default_factory=dict)  # name -> {iso: code}
    operating: list = field(default_factory=list)  # list[OperatingDate]
    messages: list = field(default_factory=list)
    binding_constraints: list = field(default_factory=list)
    tolerance_used: float = 0.0
    drifted_nurses: list = field(default_factory=list)
    label: str = ""  # the objective-profile name when several options are produced


# --- Feasibility pre-checks (Section 7.3) ---------------------------------


@dataclass
class PreCheck:
    ok: bool
    messages: list = field(default_factory=list)


def config_integrity_check(cfg: Config) -> PreCheck:
    """Structural validation that must hold before any scheduling is attempted.

    Catches data-integrity problems (no roster, blank or duplicate names) that
    would otherwise silently corrupt the schedule -- assignments are keyed by
    name, so duplicates would overwrite each other.
    """
    msgs = []
    if not cfg.nurses:
        msgs.append("The roster is empty -- add at least one nurse.")
    if not cfg.operating_shifts:
        msgs.append("No operating shifts are defined.")
    blank = [i + 1 for i, n in enumerate(cfg.nurses) if not (n.name or "").strip()]
    if blank:
        msgs.append(f"Blank nurse name(s) in row(s): {blank}.")
    names = [(n.name or "").strip() for n in cfg.nurses]
    dupes = sorted({nm for nm in names if nm and names.count(nm) > 1})
    if dupes:
        msgs.append(
            "Duplicate nurse name(s): " + ", ".join(dupes)
            + ". Names must be unique (each line is keyed by name)."
        )
    if cfg.weeks < 1:
        msgs.append("Rotation length must be at least 1 week.")
    return PreCheck(not msgs, msgs)


def saturday_feasibility_check(
    cfg: Config, operating: list[OperatingDate]
) -> PreCheck:
    """Cheap Saturday capacity pre-check (Section 7.3).

    sat_demand x n_saturdays <= Sum_nurses (eligible_saturdays x 6/9)
    """
    sats = saturday_dates(operating)
    if not sats:
        return PreCheck(True)
    total_sat_demand = sum(od.demand for od in sats)

    capacity = 0.0
    for nurse in cfg.nurses:
        elig = sum(1 for od in sats if nurse_eligible_for(nurse, od))
        capacity += elig * (SAT_MAX_PER_9WK / SAT_WINDOW_WEEKS)

    msgs = []
    ok = True
    if total_sat_demand > capacity + 1e-9:
        ok = False
        msgs.append(
            "Saturday coverage is infeasible under the 25.06(E) weekend cap: "
            f"required Saturday-shifts over the period = {total_sat_demand}, but "
            f"the roster can supply at most {capacity:.1f} "
            "(sum of eligible Saturdays x 6/9). "
            "Reduce Saturday demand or add Saturday-eligible nurses."
        )
    return PreCheck(ok, msgs)


def coverage_feasibility_check(
    cfg: Config, operating: list[OperatingDate]
) -> PreCheck:
    """Per-day: enough simultaneously-available nurses to meet demand (H1, H8).

    A job-share group can supply at most one person on any given day, so its
    members count once toward a day's effective capacity.
    """
    msgs = []
    ok = True
    for od in operating:
        solo = 0
        group_has_elig: dict[str, bool] = {}
        for n in cfg.nurses:
            if not nurse_eligible_for(n, od):
                continue
            label = (n.job_share_group or "").strip()
            if label:
                group_has_elig[label] = True
            else:
                solo += 1
        effective = solo + sum(1 for v in group_has_elig.values() if v)
        if od.demand > effective:
            ok = False
            note = (
                " (job-share lines count once per day, reducing same-day capacity)"
                if group_has_elig else ""
            )
            msgs.append(
                f"{od.iso} ({od.weekday_name}) demands {od.demand} RNs but only "
                f"{effective} can work that day{note}. Lower demand for this day, "
                "widen availability, or remove a job share."
            )
    return PreCheck(ok, msgs)


def sat_per_month_feasibility_check(
    cfg: Config, operating: list[OperatingDate]
) -> PreCheck:
    """Every non-waived nurse must get >=1 Saturday per rolling 4-week window (H9).

    Infeasible if a window has fewer Saturday seats than non-waived nurses.
    """
    weeks = cfg.weeks
    n_required = len(cfg.nurses)  # everyone works Saturdays
    sat_seats_by_week: dict[int, int] = {}
    for od in operating:
        if od.is_saturday:
            sat_seats_by_week[od.week_index] = (
                sat_seats_by_week.get(od.week_index, 0) + od.demand
            )
    if weeks >= SAT_PER_MONTH_WINDOW:
        windows = [
            (ws, ws + SAT_PER_MONTH_WINDOW - 1)
            for ws in range(0, weeks - SAT_PER_MONTH_WINDOW + 1)
        ]
    else:
        windows = [(0, weeks - 1)]
    msgs = []
    ok = True
    for (ws, we) in windows:
        seats = sum(sat_seats_by_week.get(wk, 0) for wk in range(ws, we + 1))
        if n_required > seats:
            ok = False
            msgs.append(
                f"Weeks {ws + 1}-{we + 1}: {n_required} nurses each need a Saturday "
                f"but only {seats} Saturday seats exist in that month. Raise Saturday "
                "demand or shorten the rotation."
            )
            break  # one message is enough
    return PreCheck(ok, msgs)


def _h9_min_saturdays(weeks: int) -> int:
    """Fewest Saturdays one nurse must work so every rolling 4-week window has >=1.

    Greedy hitting set over the [ws, ws+3] windows.
    """
    if weeks < SAT_PER_MONTH_WINDOW:
        return 1
    placed: list[int] = []
    for ws in range(0, weeks - SAT_PER_MONTH_WINDOW + 1):
        end = ws + SAT_PER_MONTH_WINDOW - 1
        if not placed or placed[-1] < ws:  # last placed Saturday not in this window
            placed.append(end)
    return len(placed) or 1


def _period_max_saturdays(weeks: int) -> int:
    """Most Saturdays one nurse can work while keeping <=6 in every rolling
    9-week window (25.06(E)). Not simply 6 once the rotation exceeds 9 weeks.
    """
    if weeks < SAT_WINDOW_WEEKS:
        return (SAT_MAX_PER_9WK * weeks) // SAT_WINDOW_WEEKS
    full, rem = divmod(weeks, SAT_WINDOW_WEEKS)
    return full * SAT_MAX_PER_9WK + min(SAT_MAX_PER_9WK, rem)


def shift_count_feasibility_check(
    cfg: Config, operating: list[OperatingDate]
) -> PreCheck:
    """Validate the requested shift counts are satisfiable (they are now HARD).

    Catches the common ways exact counts conflict with coverage and the Saturday
    rules, with a clear message instead of an opaque 'infeasible'.
    """
    # Coverage is soft now (under/over-staffing -> blanks/extras), so this only
    # rejects per-line counts that are physically/contractually impossible.
    msgs = []
    sats = saturday_dates(operating)
    h9_min = _h9_min_saturdays(cfg.weeks)
    sat_max = _period_max_saturdays(cfg.weeks)  # most Saturdays one line may work

    for n in cfg.nurses:
        elig_sat = sum(1 for od in sats if od.iso not in n.unavailable_dates)
        if n.target_d5 > elig_sat:
            msgs.append(
                f"{n.name}: D5 count {n.target_d5} exceeds the {elig_sat} Saturdays "
                "available to them."
            )
        elif n.target_d5 < h9_min:
            msgs.append(
                f"{n.name}: D5 count {n.target_d5} is below the {h9_min} Saturdays "
                "needed to meet the >=1-per-month rule. Raise it."
            )
        elif n.target_d5 > sat_max:
            msgs.append(
                f"{n.name}: D5 count {n.target_d5} can't satisfy the 1-in-3 weekend "
                f"cap (<=6 per 9 weeks) over {cfg.weeks} weeks — the most workable "
                f"is {sat_max}. Lower it."
            )

        # A fixed Monday-off line must still hit its worked D10 target on the
        # remaining weekdays (Tue/Wed/Thu/Fri minus any approved leave).
        if n.fixed_off_mon:
            wd_slots = sum(
                1 for od in operating
                if not od.is_saturday and od.weekday != 0
                and od.iso not in n.unavailable_dates
            )
            if n.worked_d10() > wd_slots:
                msgs.append(
                    f"{n.name}: D10 count {n.worked_d10()} can't fit in the "
                    f"{wd_slots} non-Monday weekdays available (Mondays off is a "
                    "fixed guarantee). Lower the D10 count or untick Mondays off."
                )

        # A "work every week" line needs at least one shift for each week it has
        # any eligible day -- so its total shifts must reach that many weeks.
        if n.fixed_work_weekly:
            weeks_avail = len({
                od.week_index for od in operating
                if nurse_eligible_for(n, od)
                and not (n.fixed_off_mon and od.weekday == 0)
            })
            total_shifts = n.worked_d10() + n.target_d5
            if total_shifts < weeks_avail:
                msgs.append(
                    f"{n.name}: only {total_shifts} shifts can't cover every one of "
                    f"the {weeks_avail} weeks (work-every-week is a fixed guarantee). "
                    "Raise the D10/D5 counts or untick work every week."
                )

    # Job-share groups never work the same day, so a group's combined counts
    # cannot exceed the number of operating days of each type.
    n_weekday_days = sum(1 for od in operating if not od.is_saturday)
    n_saturdays = len(sats)
    js: dict[str, list] = {}
    for n in cfg.nurses:
        label = (n.job_share_group or "").strip()
        if label:
            js.setdefault(label, []).append(n)
    for label, members in js.items():
        if len(members) < 2:
            continue
        names = " + ".join(m.name for m in members)
        combined_fte = sum(m.target_fte for m in members)
        if combined_fte > 1.0 + 1e-9:
            msgs.append(
                f"Job share {label} ({names}): combined FTE {combined_fte:.2f} "
                "exceeds 1.0. A job share splits one full-time line — lower their "
                "shift counts so the two together are at most 1.0 FTE."
            )
        if sum(m.worked_d10() for m in members) > n_weekday_days:
            msgs.append(
                f"Job share {label} ({names}): combined weekday shifts "
                f"{sum(m.worked_d10() for m in members)} exceed the {n_weekday_days} "
                "weekday days available (partners never share a day). Lower their "
                "D10 counts — a job share splits one line between two part-timers."
            )
        if sum(m.target_d5 for m in members) > n_saturdays:
            msgs.append(
                f"Job share {label} ({names}): combined Saturday shifts exceed the "
                f"{n_saturdays} Saturdays available. Lower their D5 counts."
            )
    return PreCheck(not msgs, msgs)


# --- CP-SAT model ----------------------------------------------------------


def _sat_window_bounds(weeks: int) -> list[tuple[int, int]]:
    """Return [start_week, end_week_inclusive] spans for the H2 constraint."""
    if weeks >= SAT_WINDOW_WEEKS:
        return [
            (ws, ws + SAT_WINDOW_WEEKS - 1)
            for ws in range(0, weeks - SAT_WINDOW_WEEKS + 1)
        ]
    # Shorter than a full 9-week window: apply a single proportional cap.
    return [(0, weeks - 1)]


def _sat_cap_for_span(weeks: int, span_weeks: int) -> int:
    if span_weeks >= SAT_WINDOW_WEEKS:
        return SAT_MAX_PER_9WK
    # Proportional cap for short periods, e.g. 6 weeks -> floor(6*6/9) = 4.
    return int(math.floor(SAT_MAX_PER_9WK * span_weeks / SAT_WINDOW_WEEKS))


def _solve_cpsat(
    cfg: Config,
    operating: list[OperatingDate],
    profile: str = "preference",
    det_time: float = DET_TIME_LIMIT,
) -> ScheduleResult:
    prof = OBJECTIVE_PROFILES[profile]
    model = cp_model.CpModel()
    nurses = cfg.nurses
    weeks = cfg.weeks

    # x[(ni, oi)] binary, created only for eligible nurse/date pairs.
    x: dict[tuple[int, int], cp_model.IntVar] = {}
    for ni, nurse in enumerate(nurses):
        for oi, od in enumerate(operating):
            if not nurse_eligible_for(nurse, od):
                continue
            # Fixed Monday-off is a HARD guarantee: omit the variable entirely so
            # the line can never be scheduled a Monday (same mechanism as H3).
            if nurse.fixed_off_mon and od.weekday == 0:
                continue
            x[(ni, oi)] = model.NewBoolVar(f"x_{ni}_{oi}")

    # Statutory-holiday (ST) decision vars: the solver chooses which holidays
    # each nurse takes off (paid, not worked), up to their entitlement, spread to
    # keep coverage. st[(ni, oi)] = 1 -> nurse ni is ST (off) on holiday date oi.
    stat_ois = stat_holiday_indices(cfg, operating)
    st: dict[tuple[int, int], cp_model.IntVar] = {}
    eff_stat = {}
    for ni, nurse in enumerate(nurses):
        elig = [oi for oi in stat_ois
                if operating[oi].iso not in set(nurse.unavailable_dates)]
        eff_stat[ni] = min(nurse.stat_days, len(elig))
        for oi in elig:
            st[(ni, oi)] = model.NewBoolVar(f"st_{ni}_{oi}")
            if (ni, oi) in x:
                model.Add(x[(ni, oi)] + st[(ni, oi)] <= 1)  # ST => not working
        if elig:
            model.Add(sum(st[(ni, oi)] for oi in elig) == eff_stat[ni])

    # Worked D10 target per nurse (target minus the stat days actually taken).
    worked_d10_target = {
        ni: max(0, n.target_d10 - eff_stat[ni]) for ni, n in enumerate(nurses)
    }

    # H1: daily coverage. HARD when the roster's fixed counts can cover the
    # demand (no blanks, and the constraint prunes the search); SOFT only when a
    # shift type is genuinely short-staffed, in which case the unfillable shifts
    # are left blank (W_SHORTFALL is minimized first). Over-staffing is always a
    # light soft penalty.
    total_worked_d10 = sum(worked_d10_target.values())
    total_d5 = sum(n.target_d5 for n in nurses)
    wd_seats = sum(od.demand for od in operating if not od.is_saturday)
    sat_seats = sum(od.demand for od in operating if od.is_saturday)
    weekday_can_cover = total_worked_d10 >= wd_seats
    sat_can_cover = total_d5 >= sat_seats
    extra_terms, short_terms = [], []
    for oi, od in enumerate(operating):
        vars_for_day = [x[(ni, oi)] for ni in range(len(nurses)) if (ni, oi) in x]
        assigned = sum(vars_for_day)
        can_cover = sat_can_cover if od.is_saturday else weekday_can_cover
        # A specific day may be short even when the line totals add up — e.g. lots
        # of fixed Monday-off lines, or many on approved leave the same day. If too
        # few are even eligible, coverage must be soft (blanks) for that day.
        if len(vars_for_day) < od.demand:
            can_cover = False
        if can_cover:
            model.Add(assigned >= od.demand)  # hard: full coverage is achievable
        else:
            short = model.NewIntVar(0, od.demand, f"short_{oi}")
            model.Add(short >= od.demand - assigned)
            short_terms.append(short)
        if vars_for_day:
            extra = model.NewIntVar(0, len(vars_for_day), f"extra_{oi}")
            model.Add(extra >= assigned - od.demand)
            extra_terms.append(extra)

    # H6 is structural (one var per nurse-day). H3 handled by var omission.
    # H4 is structurally impossible to violate (longest run = Fri-Sat).

    # H2: rolling Saturday windows.
    sat_indices_by_week: dict[int, list[int]] = {}
    for oi, od in enumerate(operating):
        if od.is_saturday:
            sat_indices_by_week.setdefault(od.week_index, []).append(oi)

    for ni in range(len(nurses)):
        for (ws, we) in _sat_window_bounds(weeks):
            span = we - ws + 1
            cap = _sat_cap_for_span(weeks, span)
            window_vars = [
                x[(ni, oi)]
                for wk in range(ws, we + 1)
                for oi in sat_indices_by_week.get(wk, [])
                if (ni, oi) in x
            ]
            if window_vars:
                model.Add(sum(window_vars) <= cap)

    # Fixed "work every week": a HARD guarantee the line works >= 1 shift each
    # week (no fully-idle weeks). Weeks where the line has no eligible day (e.g.
    # fully on approved leave) are skipped -- you can't work when you're off.
    ois_by_week: dict[int, list[int]] = {}
    for oi, od in enumerate(operating):
        ois_by_week.setdefault(od.week_index, []).append(oi)
    for ni, nurse in enumerate(nurses):
        if not nurse.fixed_work_weekly:
            continue
        for wk, ois in ois_by_week.items():
            wk_vars = [x[(ni, oi)] for oi in ois if (ni, oi) in x]
            if wk_vars:
                model.Add(sum(wk_vars) >= 1)

    # H8: job share -- lines sharing a non-empty label never work the same day
    # (two people splitting one line). At most one member of the group may be
    # assigned on any operating day.
    js_groups: dict[str, list[int]] = {}
    for ni, nurse in enumerate(nurses):
        label = (nurse.job_share_group or "").strip()
        if label:
            js_groups.setdefault(label, []).append(ni)
    for label, members in js_groups.items():
        if len(members) < 2:
            continue
        for oi in range(len(operating)):
            day_vars = [x[(ni, oi)] for ni in members if (ni, oi) in x]
            if len(day_vars) > 1:
                model.Add(sum(day_vars) <= 1)

    # H9: everyone (not waived off Saturdays) works >= 1 Saturday per rolling
    # 4-week window ("at least one Saturday per month").
    if weeks >= SAT_PER_MONTH_WINDOW:
        sat_windows_4 = [
            (ws, ws + SAT_PER_MONTH_WINDOW - 1)
            for ws in range(0, weeks - SAT_PER_MONTH_WINDOW + 1)
        ]
    else:
        sat_windows_4 = [(0, weeks - 1)]
    for ni, nurse in enumerate(nurses):
        for (ws, we) in sat_windows_4:
            window_vars = [
                x[(ni, oi)]
                for wk in range(ws, we + 1)
                for oi in sat_indices_by_week.get(wk, [])
                if (ni, oi) in x
            ]
            if window_vars:
                model.Add(sum(window_vars) >= 1)

    # --- Soft objective terms ---------------------------------------------
    obj_terms = []

    # Coverage: fill demand (minimize blanks) first, then avoid over-staffing.
    for short in short_terms:
        obj_terms.append(W_SHORTFALL * short)
    for extra in extra_terms:
        obj_terms.append(W_EXTRA * extra)

    # 1. Shift-count matching (HARD): each line works exactly its requested
    #    worked-D10 (target minus paid stat days) and D5 counts. Guaranteed in
    #    every option -- the profiles only change which days fill those counts.
    for ni, nurse in enumerate(nurses):
        d10_actual = sum(
            x[(ni, oi)]
            for oi, od in enumerate(operating)
            if not od.is_saturday and (ni, oi) in x
        )
        d5_actual = sum(
            x[(ni, oi)]
            for oi, od in enumerate(operating)
            if od.is_saturday and (ni, oi) in x
        )
        model.Add(d10_actual == worked_d10_target[ni])
        model.Add(d5_actual == nurse.target_d5)

    # 2. Weekday equity within FTE class (balance each weekday across equals).
    #    Weight depends on the option profile (high for "equity-maximizing").
    weekday_codes = [s.weekday for s in cfg.operating_shifts if s.weekday != 5]
    groups: dict[float, list[int]] = {}
    for ni, nurse in enumerate(nurses):
        groups.setdefault(round(nurse.target_fte, 2), []).append(ni)
    for fte_val, members in groups.items():
        if len(members) < 2:
            continue
        for wd in weekday_codes:
            counts = []
            for ni in members:
                c = sum(
                    x[(ni, oi)]
                    for oi, od in enumerate(operating)
                    if od.weekday == wd and (ni, oi) in x
                )
                cv = model.NewIntVar(0, weeks, f"wd_{wd}_{ni}")
                model.Add(cv == c)
                counts.append(cv)
            gmax = model.NewIntVar(0, weeks, f"wdmax_{fte_val}_{wd}")
            gmin = model.NewIntVar(0, weeks, f"wdmin_{fte_val}_{wd}")
            model.AddMaxEquality(gmax, counts)
            model.AddMinEquality(gmin, counts)
            spread = model.NewIntVar(0, weeks, f"wdspread_{fte_val}_{wd}")
            model.Add(spread == gmax - gmin)
            if prof["wd_equity"]:
                obj_terms.append(prof["wd_equity"] * spread)

    # 2b. Per-nurse weekday-type balance: each nurse's Mon/Wed/Fri counts should
    #     be close, so nobody is stuck working only one weekday. Works for any
    #     roster (unlike the within-class term above, which needs equal FTEs).
    if prof["wd_equity"] and len(weekday_codes) > 1:
        for ni in range(len(nurses)):
            per_wd = []
            for wd in weekday_codes:
                cv = model.NewIntVar(0, weeks, f"nwd_{ni}_{wd}")
                model.Add(cv == sum(
                    x[(ni, oi)] for oi, od in enumerate(operating)
                    if od.weekday == wd and (ni, oi) in x
                ))
                per_wd.append(cv)
            hi = model.NewIntVar(0, weeks, f"nwdhi_{ni}")
            lo = model.NewIntVar(0, weeks, f"nwdlo_{ni}")
            model.AddMaxEquality(hi, per_wd)
            model.AddMinEquality(lo, per_wd)
            sp = model.NewIntVar(0, weeks, f"nwdsp_{ni}")
            model.Add(sp == hi - lo)
            obj_terms.append(prof["wd_equity"] * sp)

    # 3. Consistency: penalize week-over-week changes in the WEEKDAY line, so
    #    each nurse tends to work the same weekdays every week (a stable,
    #    predictable rotation). Saturdays are excluded -- the 25.06(E) cap makes
    #    a fixed weekly Saturday impossible, so Saturday cadence is governed by
    #    the equity term instead.
    by_week_wd: dict[tuple[int, int], int] = {}
    for oi, od in enumerate(operating):
        by_week_wd[(od.week_index, od.weekday)] = oi

    def slot_var(ni: int, wk: int, wd: int):
        oi = by_week_wd.get((wk, wd))
        if oi is None:
            return None
        return x.get((ni, oi))  # may be None if ineligible (treated as 0)

    weekday_only = [s.weekday for s in cfg.operating_shifts if s.weekday != 5]
    for ni in range(len(nurses)):
        for wd in weekday_only:
            for wk in range(weeks - 1):
                a = slot_var(ni, wk, wd)
                b = slot_var(ni, wk + 1, wd)
                if a is None and b is None:
                    continue
                a_expr = a if a is not None else 0
                b_expr = b if b is not None else 0
                diff = model.NewBoolVar(f"pat_{ni}_{wd}_{wk}")
                model.Add(diff >= a_expr - b_expr)
                model.Add(diff >= b_expr - a_expr)
                if prof["pattern"]:
                    obj_terms.append(prof["pattern"] * diff)

    # (FTE is now fixed by the hard shift counts, so there is no FTE smoother.)

    # 5. Line preferences (soft). Flat weight per honoured preference (no
    #    seniority). Strong in the "preference-maximizing" option, light in the
    #    others. Non-consecutive Saturdays and clustering also have GLOBAL forms
    #    (sat_spread / cluster_all) that apply to everyone in the equity and
    #    cluster options.
    operating_weekdays = sorted({s.weekday for s in cfg.operating_shifts})
    start = cfg.start
    n_days = 7 * weeks
    iso_to_oi = {od.iso: oi for oi, od in enumerate(operating)}
    pw = prof["pref"]

    def _off_off_pairs(ni: int):
        """Yield BoolVars that are 1 when this nurse is off on adjacent days."""
        on_expr = []
        for day_idx in range(n_days):
            d = start + timedelta(days=day_idx)
            oi = iso_to_oi.get(d.isoformat())
            on_expr.append(x[(ni, oi)] if (oi is not None and (ni, oi) in x) else 0)
        for k in range(n_days - 1):
            a, b = on_expr[k], on_expr[k + 1]
            if isinstance(a, int) and isinstance(b, int):
                continue
            off_pair = model.NewBoolVar(f"offpair_{ni}_{k}")
            model.Add(off_pair <= 1 - a)
            model.Add(off_pair <= 1 - b)
            yield off_pair

    def _consec_sat(ni: int):
        """Yield BoolVars that are 1 when this nurse works back-to-back Saturdays."""
        for wk in range(weeks - 1):
            a = slot_var(ni, wk, 5)
            b = slot_var(ni, wk + 1, 5)
            if a is None or b is None:
                continue
            both = model.NewBoolVar(f"consecsat_{ni}_{wk}")
            model.Add(both >= a + b - 1)
            yield both

    for ni, nurse in enumerate(nurses):
        if pw:
            # a) Off-day preferences: penalize working that weekday.
            for flag, wd in (
                (nurse.pref_off_wed, 2),
                (nurse.pref_off_fri, 4),
            ):
                if not flag:
                    continue
                for wk in range(weeks):
                    v = slot_var(ni, wk, wd)
                    if v is not None:
                        obj_terms.append(pw * v)
            # b) Non-consecutive Saturdays preferred.
            if nurse.pref_nonconsec_sat:
                for both in _consec_sat(ni):
                    obj_terms.append(pw * both)
            # c) Clustered shifts preferred (reward off/off adjacency).
            if nurse.pref_clustered:
                for off_pair in _off_off_pairs(ni):
                    obj_terms.append(-pw * off_pair)

        # GLOBAL equity: space everyone's Saturdays out (penalize back-to-back).
        if prof["sat_spread"]:
            for both in _consec_sat(ni):
                obj_terms.append(prof["sat_spread"] * both)
        # GLOBAL cluster: reward everyone's off/off adjacency -> long days off.
        if prof["cluster_all"]:
            for off_pair in _off_off_pairs(ni):
                obj_terms.append(-prof["cluster_all"] * off_pair)

    # 6. Low-FTE lines (< 0.30): strong push to work in >= 3 of every rolling
    #    4-week window, so a small line stays regularly engaged instead of
    #    bunching all its shifts together. Soft, so it never makes the schedule
    #    infeasible -- an under-supplied line simply incurs the penalty.
    for ni, nurse in enumerate(nurses):
        if nurse.target_fte >= LOW_FTE_THRESHOLD:
            continue
        active = []
        for wk in range(weeks):
            wvars = [
                v for wd in operating_weekdays
                if (v := slot_var(ni, wk, wd)) is not None
            ]
            a = model.NewBoolVar(f"active_{ni}_{wk}")
            if wvars:
                model.Add(a <= sum(wvars))  # active only if >=1 shift that week
            else:
                model.Add(a == 0)
            active.append(a)
        for ws in range(0, weeks - THREE_OF_FOUR_WINDOW + 1):
            window_active = sum(active[ws:ws + THREE_OF_FOUR_WINDOW])
            short = model.NewIntVar(0, THREE_OF_FOUR_MIN_ACTIVE, f"tof_{ni}_{ws}")
            model.Add(short >= THREE_OF_FOUR_MIN_ACTIVE - window_active)
            obj_terms.append(W_THREE_OF_FOUR * short)

    model.Minimize(sum(obj_terms))

    solver = cp_model.CpSolver()
    solver.parameters.random_seed = RANDOM_SEED
    # Reproducibility: a single worker plus a *deterministic* time limit makes
    # the stopping point independent of wall-clock speed, so identical inputs
    # always yield byte-identical schedules. A generous wall-clock cap guards
    # against pathological cases on slow hardware.
    solver.parameters.num_search_workers = 1
    solver.parameters.max_deterministic_time = det_time
    solver.parameters.max_time_in_seconds = SOLVER_TIME_LIMIT_S
    status = solver.Solve(model)

    status_name = solver.StatusName(status)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        assignments = _extract_assignments(cfg, operating, x, st, solver)
        return ScheduleResult(
            feasible=True,
            method="cp-sat",
            status=status_name,
            assignments=assignments,
            operating=operating,
            label=prof["label"],
        )
    return ScheduleResult(
        feasible=False,
        method="cp-sat",
        status=status_name,
        operating=operating,
    )


def _extract_assignments(cfg, operating, x, st, solver) -> dict:
    """Build {name: {iso: code}} where code is D10/D5 (worked) or ST (stat off)."""
    assignments: dict[str, dict[str, str]] = {n.name: {} for n in cfg.nurses}
    for ni, nurse in enumerate(cfg.nurses):
        for oi, od in enumerate(operating):
            if (ni, oi) in x and solver.Value(x[(ni, oi)]) == 1:
                assignments[nurse.name][od.iso] = od.shift.code
            elif (ni, oi) in st and solver.Value(st[(ni, oi)]) == 1:
                assignments[nurse.name][od.iso] = "ST"
    return assignments


# --- Diagnostic pass (Section 7.3) ----------------------------------------


def _diagnose(cfg: Config, operating: list[OperatingDate]) -> list[str]:
    """Drop one hard-constraint family at a time to find the binding one."""
    findings = []

    # H1 capacity already covered by coverage_feasibility_check; re-run it.
    cov = coverage_feasibility_check(cfg, operating)
    if not cov.ok:
        findings.append("H1 (daily coverage) is binding:")
        findings.extend("  - " + m for m in cov.messages)

    sat = saturday_feasibility_check(cfg, operating)
    if not sat.ok:
        findings.append("H2 (25.06(E) Saturday cap) is binding:")
        findings.extend("  - " + m for m in sat.messages)

    # Aggregate hours capacity vs FTE targets (H5).
    total_capacity_hours = sum(od.demand * od.paid_hours for od in operating)
    total_target_hours = sum(
        n.target_fte * WEEKLY_FULL_TIME_HOURS * cfg.weeks for n in cfg.nurses
    )
    if total_target_hours > total_capacity_hours + 1e-6:
        findings.append(
            "H5 (FTE targets) is binding: the sum of nurse target hours "
            f"({total_target_hours:.0f}) exceeds the total schedulable hours "
            f"({total_capacity_hours:.0f}). Lower target FTEs or raise demand."
        )
    elif total_target_hours < total_capacity_hours - 1e-6:
        findings.append(
            "H5 (FTE targets) may be binding: total demanded hours "
            f"({total_capacity_hours:.0f}) exceed the sum of nurse target hours "
            f"({total_target_hours:.0f}); the roster cannot cover demand within "
            "FTE tolerances. Raise target FTEs or add staff."
        )

    # Job-share tension: each shared day forces the non-shared lines to cover,
    # which can push their hours past their FTE flex even when per-day capacity
    # looks fine.
    js_labels = {
        (n.job_share_group or "").strip()
        for n in cfg.nurses
        if (n.job_share_group or "").strip()
    }
    if js_labels:
        max_weekday_demand = max(
            (od.demand for od in operating if not od.is_saturday), default=0
        )
        n_solo = sum(1 for n in cfg.nurses if not (n.job_share_group or "").strip())
        if max_weekday_demand >= n_solo + len(js_labels):
            findings.append(
                "H8 (job share) is likely binding: with job-share lines counting "
                "once per day, meeting weekday demand forces every non-shared line "
                "to work most days, which can exceed their FTE flex. Lower weekday "
                "demand, add a line, or reduce/remove a job share."
            )

    if not findings:
        findings.append(
            "No single hard-constraint family is individually infeasible; the "
            "combination is over-constrained. Try relaxing FTE flex, demand, "
            "Saturday waivers, or a job share."
        )
    return findings


# --- Greedy fallback (Section 1 / 7) --------------------------------------


def _greedy(cfg: Config, operating: list[OperatingDate]) -> ScheduleResult:
    """Best-effort greedy schedule respecting hard constraints H1-H4, H6.

    Fills each operating day's demand by choosing eligible nurses who are
    furthest below their target hours, while never exceeding the Saturday cap.
    FTE exactness (H5) is best-effort, not guaranteed; binding shortfalls are
    reported.
    """
    nurses = cfg.nurses
    weeks = cfg.weeks
    period_full = WEEKLY_FULL_TIME_HOURS * weeks
    target_hours = {n.name: n.target_fte * period_full for n in nurses}
    accrued = {n.name: 0.0 for n in nurses}
    assignments: dict[str, dict[str, str]] = {n.name: {} for n in nurses}
    sat_worked_by_week: dict[str, dict[int, int]] = {
        n.name: {} for n in nurses
    }
    binding = []

    sat_windows = _sat_window_bounds(weeks)

    def sat_ok(nurse: Nurse, od: OperatingDate) -> bool:
        # Check every rolling window containing this Saturday stays within cap.
        worked = sat_worked_by_week[nurse.name]
        for (ws, we) in sat_windows:
            if ws <= od.week_index <= we:
                cap = _sat_cap_for_span(weeks, we - ws + 1)
                current = sum(worked.get(w, 0) for w in range(ws, we + 1))
                if current + 1 > cap:
                    return False
        return True

    for od in sorted(operating, key=lambda o: (o.d, o.weekday)):
        candidates = [
            n
            for n in nurses
            if nurse_eligible_for(n, od)
            and od.iso not in assignments[n.name]
            and (not od.is_saturday or sat_ok(n, od))
        ]
        # Prefer nurses most below their target hours; name for a stable order.
        candidates.sort(
            key=lambda n: (-(target_hours[n.name] - accrued[n.name]), n.name)
        )
        # Pick up to demand, never putting two job-share partners on one day (H8).
        chosen = []
        used_groups = set()
        for n in candidates:
            if len(chosen) >= od.demand:
                break
            label = (n.job_share_group or "").strip()
            if label and label in used_groups:
                continue
            chosen.append(n)
            if label:
                used_groups.add(label)
        if len(chosen) < od.demand:
            binding.append(
                f"{od.iso} ({od.weekday_name}): only {len(chosen)} of {od.demand} "
                "required RNs could be assigned without violating hard constraints."
            )
        for n in chosen:
            assignments[n.name][od.iso] = od.shift.code
            accrued[n.name] += od.paid_hours
            if od.is_saturday:
                wk = od.week_index
                sat_worked_by_week[n.name][wk] = (
                    sat_worked_by_week[n.name].get(wk, 0) + 1
                )

    feasible = len(binding) == 0
    msgs = ["Greedy fallback used (CP-SAT found no feasible solution)."]
    if binding:
        msgs.append("Coverage could not be fully met:")
        msgs.extend("  - " + b for b in binding)
    return ScheduleResult(
        feasible=feasible,
        method="greedy",
        status="GREEDY_FEASIBLE" if feasible else "GREEDY_PARTIAL",
        assignments=assignments,
        operating=operating,
        messages=msgs,
        binding_constraints=binding,
        tolerance_used=cfg.fte_tolerance,
    )


# --- Public entry point ----------------------------------------------------


def generate_schedules(cfg: Config, profiles=None) -> list[ScheduleResult]:
    """Generate one schedule per objective profile.

    By default three options -- preference-, equity- and cluster-maximizing.
    Each satisfies all hard constraints and hits the requested shift counts; the
    profiles differ in which secondary goal they push. Returns a single
    infeasible / greedy result in a one-item list if no CP-SAT solution exists.
    """
    cfg.apply_derived_ftes()  # keep target_fte in sync with the shift counts
    profiles = profiles or PROFILE_ORDER

    integrity = config_integrity_check(cfg)
    if not integrity.ok:
        return [ScheduleResult(
            feasible=False,
            method="none",
            status="CONFIG_INVALID",
            operating=[],
            messages=integrity.messages,
            binding_constraints=integrity.messages,
        )]

    operating = build_operating_dates(cfg)

    # Coverage / Saturday-capacity are no longer hard (shortfalls -> blank
    # shifts), so only genuinely-impossible cases gate the solve: the
    # everyone-gets-a-monthly-Saturday rule (H9) and per-line count limits.
    month = sat_per_month_feasibility_check(cfg, operating)
    counts = shift_count_feasibility_check(cfg, operating)
    if not month.ok or not counts.ok:
        diag = _diagnose(cfg, operating)
        return [ScheduleResult(
            feasible=False,
            method="none",
            status="PRECHECK_INFEASIBLE",
            operating=operating,
            messages=month.messages + counts.messages,
            binding_constraints=diag,
        )]

    det = DET_TIME_LIMIT if len(profiles) <= 1 else ALT_DET_TIME
    # Solve the profiles in parallel -- OR-Tools releases the GIL during Solve,
    # so threads give a real ~Nx speedup while each solve stays deterministic.
    with ThreadPoolExecutor(max_workers=min(len(profiles), os.cpu_count() or 1)) as ex:
        raw = list(ex.map(
            lambda p: _solve_cpsat(cfg, operating, profile=p, det_time=det),
            profiles,
        ))

    results = [r for r in raw if r.feasible]
    if not results:  # rare after the pre-checks -> best-effort greedy
        diag = _diagnose(cfg, operating)
        greedy = _greedy(cfg, operating)
        greedy.binding_constraints = diag + greedy.binding_constraints
        greedy.messages = diag + greedy.messages
        greedy.label = "Best effort"
        return [greedy]
    return results


def generate_schedule(cfg: Config) -> ScheduleResult:
    """Single best schedule (back-compat wrapper) -- the preference profile."""
    return generate_schedules(cfg, profiles=["preference"])[0]
