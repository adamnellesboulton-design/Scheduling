"""Schedule generation (Sections 5-7).

CP-SAT (OR-Tools) builds the master schedule under hard constraints H1-H6 and
optimizes the weighted soft objectives of Section 6. If no feasible solution
is found, FTE tolerance is relaxed; failing that a diagnostic pass identifies
the binding requirement and a greedy fallback produces a best-effort schedule.

All hours are carried in integer half-hour units inside the model (9.5h -> 19,
5.0h -> 10, 10.0h -> 20) because CP-SAT is integer-only.
"""

from __future__ import annotations

import math
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

# Soft-objective weights, descending priority.
#
# Tuned to MAXIMIZE consistency (a stable, repeating weekly line per nurse)
# while honouring per-line preferences (resolved by seniority) and keeping
# low-FTE lines working regularly. Clustering / consecutive days off is now an
# opt-in line preference rather than a global objective. Saturday equity stays
# meaningful but lower ("fair and equitable", 25.06(E)).
W_THREE_OF_FOUR = 2000  # low-FTE lines: strong push to work >=3 of every 4 weeks
W_PREF = 450  # per honoured line-preference unit, scaled (0,1] by seniority
W_PATTERN = 400  # penalty per week-over-week weekday change (consistency)
W_SAT_EQUITY = 120  # penalty per Saturday off the FTE-proportional fair share
W_WEEKDAY_EQUITY = 40  # penalty per weekday-count spread within an FTE class
W_FTE_DEV = 4  # penalty per half-hour of FTE deviation inside the band
W_SENIORITY_TIE = 1  # tie-break: senior nurses get first pick of off-Saturdays

# Lines below this FTE should work in >=3 of every rolling 4 weeks (soft).
LOW_FTE_THRESHOLD = 0.30
THREE_OF_FOUR_WINDOW = 4
THREE_OF_FOUR_MIN_ACTIVE = 3

RELAX_EXTRA = 0.05  # extra FTE flex added to every line if the base solve fails
# The deterministic time limit governs the stopping point (reproducible). The
# wall-clock cap is a pure safety valve set well above it so it never fires on
# normal hardware and therefore never injects nondeterminism.
DET_TIME_LIMIT = 12.0  # deterministic time units (solution plateaus well before this)
SOLVER_TIME_LIMIT_S = 90.0  # wall-clock safety cap
RANDOM_SEED = 42

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


# --- Feasibility pre-checks (Section 7.3) ---------------------------------


@dataclass
class PreCheck:
    ok: bool
    messages: list = field(default_factory=list)


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
            "Reduce Saturday demand, add Saturday-eligible nurses, or remove "
            "fixed_saturdays_off waivers."
        )
    return PreCheck(ok, msgs)


def coverage_feasibility_check(
    cfg: Config, operating: list[OperatingDate]
) -> PreCheck:
    """Per-day: enough eligible nurses to meet demand (H1)."""
    msgs = []
    ok = True
    for od in operating:
        elig = sum(1 for n in cfg.nurses if nurse_eligible_for(n, od))
        if od.demand > elig:
            ok = False
            msgs.append(
                f"{od.iso} ({od.weekday_name}) demands {od.demand} RNs but only "
                f"{elig} eligible nurses exist (unavailability / waivers reduce the "
                "pool). Lower demand for this day or widen availability."
            )
    return PreCheck(ok, msgs)


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
    extra_tol: float,
    relaxed: bool,
) -> ScheduleResult:
    model = cp_model.CpModel()
    nurses = cfg.nurses
    weeks = cfg.weeks

    # x[(ni, oi)] binary, created only for eligible nurse/date pairs.
    x: dict[tuple[int, int], cp_model.IntVar] = {}
    for ni, nurse in enumerate(nurses):
        for oi, od in enumerate(operating):
            if nurse_eligible_for(nurse, od):
                x[(ni, oi)] = model.NewBoolVar(f"x_{ni}_{oi}")

    # H1: daily coverage equality.
    for oi, od in enumerate(operating):
        vars_for_day = [x[(ni, oi)] for ni in range(len(nurses)) if (ni, oi) in x]
        model.Add(sum(vars_for_day) == od.demand)

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

    # H5: scheduled FTE within +/- tolerance, in half-hour units.
    period_full = WEEKLY_FULL_TIME_HOURS * weeks
    nurse_hours_expr = {}
    for ni, nurse in enumerate(nurses):
        expr = sum(
            x[(ni, oi)] * half_hours(operating[oi].paid_hours)
            for oi in range(len(operating))
            if (ni, oi) in x
        )
        nurse_hours_expr[ni] = expr
        # Per-line FTE flex (defaults to the config-wide tolerance), plus any
        # extra relaxation applied this pass.
        tol_n = nurse.tolerance(cfg.fte_tolerance) + extra_tol
        # Bound the band *strictly inside* the exact float tolerance band so the
        # float-precision validator can never flag a rounding-only violation:
        # lower bound rounds up, upper bound rounds down (in half-hour units).
        lower_hh = math.ceil((nurse.target_fte - tol_n) * period_full * 2)
        upper_hh = math.floor((nurse.target_fte + tol_n) * period_full * 2)
        model.Add(expr >= max(0, lower_hh))
        model.Add(expr <= upper_hh)

    # --- Soft objective terms ---------------------------------------------
    obj_terms = []

    sats = saturday_dates(operating)
    total_sat_demand = sum(od.demand for od in sats)
    # Non-exempt = can work at least one Saturday.
    sat_count: dict[int, object] = {}
    eligible_sat_nurses = []
    weight_sum = 0.0
    for ni, nurse in enumerate(nurses):
        n_elig_sat = sum(
            1 for oi, od in enumerate(operating) if od.is_saturday and (ni, oi) in x
        )
        if n_elig_sat > 0:
            eligible_sat_nurses.append(ni)
            weight_sum += max(nurse.target_fte, 1e-6)
        sat_count[ni] = sum(
            x[(ni, oi)]
            for oi, od in enumerate(operating)
            if od.is_saturday and (ni, oi) in x
        )

    # 1. Saturday equity: L1 deviation from FTE-proportional fair share.
    for ni in eligible_sat_nurses:
        nurse = nurses[ni]
        fair = total_sat_demand * max(nurse.target_fte, 1e-6) / weight_sum
        fair_int = int(round(fair))
        dev = model.NewIntVar(0, len(sats) + fair_int, f"satdev_{ni}")
        model.Add(dev >= sat_count[ni] - fair_int)
        model.Add(dev >= fair_int - sat_count[ni])
        obj_terms.append(W_SAT_EQUITY * dev)

        # Seniority tie-break: senior (low rank) nurses get first pick of
        # off-Saturdays -> small extra penalty for them working a Saturday.
        max_rank = max((nn.seniority_rank for nn in nurses), default=1)
        senior_weight = max_rank - nurse.seniority_rank + 1
        obj_terms.append(W_SENIORITY_TIE * senior_weight * sat_count[ni])

    # 2. Weekday equity within FTE class (balance each weekday across equals).
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
            obj_terms.append(W_WEEKDAY_EQUITY * spread)

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
                obj_terms.append(W_PATTERN * diff)

    # 4. FTE deviation (L1 in half-hours, even within tolerance band).
    for ni, nurse in enumerate(nurses):
        target_hh = half_hours(nurse.target_fte * period_full)
        max_hh = half_hours(period_full)
        dev = model.NewIntVar(0, max_hh + target_hh, f"ftedev_{ni}")
        model.Add(dev >= nurse_hours_expr[ni] - target_hh)
        model.Add(dev >= target_hh - nurse_hours_expr[ni])
        obj_terms.append(W_FTE_DEV * dev)

    # 5. Line preferences (soft; conflicts resolved by seniority). Each honoured
    #    preference is weighted W_PREF x a seniority factor in (0, 1] -- most
    #    senior (rank 1) carries full weight, so when two lines' wishes clash the
    #    senior nurse's preference prevails. Sits above equity but below the hard
    #    rules and the low-FTE 3-of-4 push.
    operating_weekdays = sorted({s.weekday for s in cfg.operating_shifts})
    max_rank = max((nn.seniority_rank for nn in nurses), default=1)
    start = cfg.start
    n_days = 7 * weeks
    iso_to_oi = {od.iso: oi for oi, od in enumerate(operating)}
    for ni, nurse in enumerate(nurses):
        senior_factor = (max_rank - nurse.seniority_rank + 1) / max_rank
        pw = max(1, round(W_PREF * senior_factor))  # integer objective coeff

        # a) Off-day preferences: penalize working that weekday.
        for flag, wd in (
            (nurse.pref_off_mon, 0),
            (nurse.pref_off_wed, 2),
            (nurse.pref_off_fri, 4),
        ):
            if not flag:
                continue
            for wk in range(weeks):
                v = slot_var(ni, wk, wd)
                if v is not None:
                    obj_terms.append(pw * v)

        # b) Non-consecutive Saturdays preferred: penalize back-to-back Sats.
        if nurse.pref_nonconsec_sat:
            for wk in range(weeks - 1):
                a = slot_var(ni, wk, 5)
                b = slot_var(ni, wk + 1, 5)
                if a is None or b is None:
                    continue
                both = model.NewBoolVar(f"consecsat_{ni}_{wk}")
                model.Add(both >= a + b - 1)
                obj_terms.append(pw * both)

        # c) Clustered shifts preferred: reward every adjacent pair of calendar
        #    days on which this nurse is off. With total off-days pinned by the
        #    FTE band, maximizing off/off adjacencies clusters the worked days
        #    (e.g. Fri+Sat together) and lengthens contiguous days off. Closure
        #    days (Tue/Thu/Sun) are always-off constants.
        if nurse.pref_clustered:
            on_expr = []
            for day_idx in range(n_days):
                d = start + timedelta(days=day_idx)
                oi = iso_to_oi.get(d.isoformat())
                if oi is not None and (ni, oi) in x:
                    on_expr.append(x[(ni, oi)])
                else:
                    on_expr.append(0)
            for k in range(n_days - 1):
                a, b = on_expr[k], on_expr[k + 1]
                if isinstance(a, int) and isinstance(b, int):
                    continue  # constant off/off pair -> no decision to make
                off_pair = model.NewBoolVar(f"clusteroff_{ni}_{k}")
                model.Add(off_pair <= 1 - a)
                model.Add(off_pair <= 1 - b)
                obj_terms.append(-pw * off_pair)  # reward (minimization)

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
    # Reproducibility (Section 7.4): a single worker plus a *deterministic*
    # time limit makes the stopping point independent of wall-clock speed, so
    # identical inputs always yield byte-identical schedules. A generous
    # wall-clock cap guards against pathological cases on slow hardware.
    solver.parameters.num_search_workers = 1
    solver.parameters.max_deterministic_time = DET_TIME_LIMIT
    solver.parameters.max_time_in_seconds = SOLVER_TIME_LIMIT_S
    status = solver.Solve(model)

    status_name = solver.StatusName(status)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        assignments = _extract_assignments(cfg, operating, x, solver)
        drifted = _drifted_nurses(cfg, operating, assignments)
        return ScheduleResult(
            feasible=True,
            method="cp-sat-relaxed" if relaxed else "cp-sat",
            status=status_name,
            assignments=assignments,
            operating=operating,
            tolerance_used=extra_tol,
            drifted_nurses=drifted if relaxed else [],
        )
    return ScheduleResult(
        feasible=False,
        method="cp-sat-relaxed" if relaxed else "cp-sat",
        status=status_name,
        operating=operating,
        tolerance_used=extra_tol,
    )


def _extract_assignments(cfg, operating, x, solver) -> dict:
    assignments: dict[str, dict[str, str]] = {n.name: {} for n in cfg.nurses}
    for ni, nurse in enumerate(cfg.nurses):
        for oi, od in enumerate(operating):
            if (ni, oi) in x and solver.Value(x[(ni, oi)]) == 1:
                assignments[nurse.name][od.iso] = od.shift.code
    return assignments


def _drifted_nurses(cfg, operating, assignments) -> list:
    """Nurses whose scheduled FTE is outside the *base* tolerance."""
    from .fte import scheduled_fte

    drifted = []
    for nurse in cfg.nurses:
        hrs = _nurse_total_hours(operating, assignments.get(nurse.name, {}))
        sf = scheduled_fte(hrs, cfg.weeks)
        if abs(sf - nurse.target_fte) > nurse.tolerance(cfg.fte_tolerance) + 1e-9:
            drifted.append((nurse.name, round(sf, 3), round(sf - nurse.target_fte, 3)))
    return drifted


def _nurse_total_hours(operating, day_map: dict) -> float:
    total = 0.0
    od_by_iso = {od.iso: od for od in operating}
    for iso in day_map:
        od = od_by_iso.get(iso)
        if od:
            total += od.paid_hours
    return total


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

    if not findings:
        findings.append(
            "No single hard-constraint family is individually infeasible; the "
            "combination is over-constrained. Try relaxing FTE tolerance, demand, "
            "or Saturday waivers."
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
        # Prefer nurses most below target (deficit), tie-break by seniority.
        candidates.sort(
            key=lambda n: (
                -(target_hours[n.name] - accrued[n.name]),
                n.seniority_rank,
            )
        )
        chosen = candidates[: od.demand]
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


def generate_schedule(cfg: Config) -> ScheduleResult:
    """Full Section 7 generation flow."""
    operating = build_operating_dates(cfg)

    # Cheap pre-checks first.
    cov = coverage_feasibility_check(cfg, operating)
    sat = saturday_feasibility_check(cfg, operating)
    if not cov.ok or not sat.ok:
        diag = _diagnose(cfg, operating)
        return ScheduleResult(
            feasible=False,
            method="none",
            status="PRECHECK_INFEASIBLE",
            operating=operating,
            messages=cov.messages + sat.messages,
            binding_constraints=diag,
        )

    # 1. CP-SAT at each line's base flex.
    res = _solve_cpsat(cfg, operating, extra_tol=0.0, relaxed=False)
    if res.feasible:
        return res

    # 2. Relax every line's flex by +0.05 to reach feasibility.
    res2 = _solve_cpsat(cfg, operating, extra_tol=RELAX_EXTRA, relaxed=True)
    if res2.feasible:
        res2.messages.append(
            f"Each line's FTE flex was widened by +{RELAX_EXTRA} to reach "
            "feasibility (26.01 averaging)."
        )
        if res2.drifted_nurses:
            res2.messages.append(
                "Nurses drifted beyond base tolerance: "
                + ", ".join(
                    f"{name} (FTE {sf:+.3f}, dev {dev:+.3f})"
                    for name, sf, dev in res2.drifted_nurses
                )
            )
        return res2

    # 3. Diagnostic pass + greedy fallback.
    diag = _diagnose(cfg, operating)
    greedy = _greedy(cfg, operating)
    greedy.binding_constraints = diag + greedy.binding_constraints
    greedy.messages = diag + greedy.messages
    return greedy
