"""Validation pass (Section 8).

Always runs on the final schedule, even when the solver reports success.
Produces a per-rule PASS / FAIL / INFO report with article citations, plus
per-nurse FTE and Saturday detail for the Summary sheet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .config import Config
from .model import (
    OperatingDate,
    saturday_dates,
    is_worked,
    business_week_index,
)
from .fte import scheduled_fte
from .holidays import holidays_in_range
from .scheduler import (
    SAT_MAX_PER_9WK,
    SAT_WINDOW_WEEKS,
    SAT_PER_MONTH_WINDOW,
    LOW_FTE_THRESHOLD,
    THREE_OF_FOUR_WINDOW,
    THREE_OF_FOUR_MIN_ACTIVE,
    stat_holiday_indices,
    effective_stat,
    _sat_window_bounds,
    _sat_cap_for_span,
)


@dataclass
class RuleResult:
    rule: str
    citation: str
    status: str  # PASS / FAIL / INFO / WARN
    detail: str


@dataclass
class NurseSummary:
    name: str
    target_fte: float
    scheduled_fte: float
    deviation: float
    total_hours: float
    avg_weekly_hours: float
    saturdays_worked: int
    saturdays_in_period: int
    worst_9wk_sat: int
    within_tolerance: bool
    target_d10: int = 0
    scheduled_d10: int = 0
    target_d5: int = 0
    scheduled_d5: int = 0


@dataclass
class ValidationReport:
    rules: list = field(default_factory=list)  # list[RuleResult]
    nurse_summaries: list = field(default_factory=list)  # list[NurseSummary]
    max_consecutive_days: int = 0
    unfilled_shifts: int = 0


def _nurse_worked_dates(assignments: dict, name: str) -> set:
    """Dates the nurse actually works (D10/D5) -- excludes ST (stat off) and LV."""
    return {iso for iso, code in assignments.get(name, {}).items() if is_worked(code)}


def _max_consecutive_calendar_days(worked_isos: set) -> int:
    if not worked_isos:
        return 0
    days = sorted(date.fromisoformat(d) for d in worked_isos)
    best = run = 1
    for prev, cur in zip(days, days[1:]):
        if cur - prev == timedelta(days=1):
            run += 1
            best = max(best, run)
        else:
            run = 1
    return best


def _worst_rolling_9wk_sat(cfg: Config, operating, name: str, assignments) -> tuple:
    """Return (worst_count, cap_for_that_window) over all Saturday windows."""
    worked = _nurse_worked_dates(assignments, name)
    sat_by_week: dict[int, list] = {}
    for od in operating:
        if od.is_saturday:
            sat_by_week.setdefault(od.week_index, []).append(od.iso)

    worst = 0
    cap_used = SAT_MAX_PER_9WK
    for (ws, we) in _sat_window_bounds(cfg.weeks):
        cap = _sat_cap_for_span(cfg.weeks, we - ws + 1)
        count = sum(
            1
            for wk in range(ws, we + 1)
            for iso in sat_by_week.get(wk, [])
            if iso in worked
        )
        if count > worst:
            worst = count
            cap_used = cap
    return worst, cap_used


def validate(cfg: Config, result) -> ValidationReport:
    operating: list[OperatingDate] = result.operating
    assignments = result.assignments
    report = ValidationReport()

    # Stat days each nurse can actually take (entitlement capped at holidays);
    # the worked-D10 target is target_d10 minus that.
    _stat_ois = stat_holiday_indices(cfg, operating) if operating else []
    eff_stat = {n.name: effective_stat(n, _stat_ois, operating) for n in cfg.nurses}

    od_by_iso = {od.iso: od for od in operating}
    sats = saturday_dates(operating)
    n_saturdays = len(sats)

    # --- Coverage (H1, soft): demand met, or shifts left blank (under-staffed)
    short_days = []
    total_short = total_extra = 0
    extras = []
    for od in operating:
        assigned = sum(1 for name in assignments
                       if is_worked(assignments[name].get(od.iso)))
        if assigned < od.demand:
            total_short += od.demand - assigned
            short_days.append(f"{od.iso} ({od.weekday_name}): {assigned}/{od.demand}")
        elif assigned > od.demand:
            total_extra += assigned - od.demand
            extras.append(f"{od.iso} (+{assigned - od.demand})")
    report.unfilled_shifts = total_short
    if total_short:
        status, detail = "WARN", (
            f"{total_short} shift(s) left blank (not enough staff): "
            + "; ".join(short_days)
        )
    else:
        status = "PASS"
        detail = "All operating days fully staffed"
        detail += (f"; {total_extra} extra shift(s): {', '.join(extras)}."
                   if extras else ".")
    report.rules.append(
        RuleResult(
            "Daily coverage (blank shifts allowed when short-staffed)",
            "Operational (H1)",
            status,
            detail,
        )
    )

    # --- Unavailability respected (H3) ------------------------------------
    # Catches manual edits/swaps that would put a nurse on an unavailable date.
    unavail_bad = []
    for nurse in cfg.nurses:
        worked = _nurse_worked_dates(assignments, nurse.name)
        for iso in nurse.unavailable_dates:
            if iso in worked:
                unavail_bad.append(f"{nurse.name} on {iso}")
    report.rules.append(
        RuleResult(
            "No one works an unavailable date",
            "Approved leave / operational (H3)",
            "PASS" if not unavail_bad else "FAIL",
            "All unavailable dates respected."
            if not unavail_bad else "Scheduled on leave -> " + "; ".join(unavail_bad),
        )
    )

    # --- Fixed days off respected (Mon-off / Fri-off guarantees) ----------
    # Solver-enforced by variable omission, but re-checked so a manual grid edit
    # or swap onto a guaranteed-off weekday is caught.
    off_lines = [n for n in cfg.nurses if n.fixed_off_mon or n.fixed_off_fri]
    if off_lines:
        off_bad = []
        for nurse in off_lines:
            worked = _nurse_worked_dates(assignments, nurse.name)
            for od in operating:
                if od.iso not in worked:
                    continue
                if nurse.fixed_off_mon and od.weekday == 0:
                    off_bad.append(f"{nurse.name} on Monday {od.iso}")
                if nurse.fixed_off_fri and od.weekday == 4:
                    off_bad.append(f"{nurse.name} on Friday {od.iso}")
        report.rules.append(
            RuleResult(
                "Fixed days off respected (Mon/Fri off)",
                "Unit policy (hard)",
                "PASS" if not off_bad else "FAIL",
                "Lines with a fixed day off: "
                + ", ".join(
                    f"{n.name} ("
                    + "/".join(d for d, on in
                               (("Mon", n.fixed_off_mon), ("Fri", n.fixed_off_fri))
                               if on) + ")"
                    for n in off_lines)
                + ". "
                + ("All respected." if not off_bad
                   else "Violations -> " + "; ".join(off_bad)),
            )
        )

    # --- Job share: partners never work the same day (H8) -----------------
    js_groups: dict[str, list] = {}
    for nurse in cfg.nurses:
        label = (nurse.job_share_group or "").strip()
        if label:
            js_groups.setdefault(label, []).append(nurse)
    js_pairs = {g: m for g, m in js_groups.items() if len(m) >= 2}
    if js_pairs:
        js_ok = True
        clashes = []
        for label, members in js_pairs.items():
            for od in operating:
                both = [n.name for n in members
                        if is_worked(assignments.get(n.name, {}).get(od.iso))]
                if len(both) > 1:
                    js_ok = False
                    clashes.append(f"{label} on {od.iso}: {', '.join(both)}")
        names = "; ".join(
            f"{g} = {' + '.join(n.name for n in m)}" for g, m in js_pairs.items()
        )
        report.rules.append(
            RuleResult(
                "Job-share partners never share a day",
                "Unit policy (H8)",
                "PASS" if js_ok else "FAIL",
                f"Job shares: {names}. "
                + ("No overlaps." if js_ok else "Overlaps -> " + "; ".join(clashes)),
            )
        )

    # --- Max consecutive days (H4) ----------------------------------------
    overall_max = 0
    for nurse in cfg.nurses:
        m = _max_consecutive_calendar_days(
            _nurse_worked_dates(assignments, nurse.name)
        )
        overall_max = max(overall_max, m)
    report.max_consecutive_days = overall_max
    report.rules.append(
        RuleResult(
            "Max 6 consecutive scheduled days",
            "25.06(C) (H4)",
            "PASS" if overall_max <= 6 else "FAIL",
            f"Longest run across roster = {overall_max} day(s) "
            "(structurally bounded at 2 on a Mon/Wed/Fri/Sat unit).",
        )
    )

    # --- Rolling 9-week Saturday cap (H2) ---------------------------------
    sat_ok = True
    worst_lines = []
    for nurse in cfg.nurses:
        worst, cap = _worst_rolling_9wk_sat(cfg, operating, nurse.name, assignments)
        if worst > cap:
            sat_ok = False
            worst_lines.append(f"{nurse.name}: {worst} (cap {cap})")
    report.rules.append(
        RuleResult(
            "Off >=3 Saturdays per rolling 9-week window",
            "25.06(E)(i) (H2)",
            "PASS" if sat_ok else "FAIL",
            "All nurses within the weekend cap."
            if sat_ok
            else "Cap exceeded -> " + "; ".join(worst_lines),
        )
    )

    # --- Everyone works >=1 Saturday per month (H9) -----------------------
    sat_by_week_iso: dict[int, list] = {}
    for od in operating:
        if od.is_saturday:
            sat_by_week_iso.setdefault(od.week_index, []).append(od.iso)
    if cfg.weeks >= SAT_PER_MONTH_WINDOW:
        windows4 = [
            (ws, ws + SAT_PER_MONTH_WINDOW - 1)
            for ws in range(0, cfg.weeks - SAT_PER_MONTH_WINDOW + 1)
        ]
    else:
        windows4 = [(0, cfg.weeks - 1)]
    h9_ok = True
    h9_bad = []
    for nurse in cfg.nurses:
        worked = _nurse_worked_dates(assignments, nurse.name)
        for (ws, we) in windows4:
            got = sum(
                1 for wk in range(ws, we + 1)
                for iso in sat_by_week_iso.get(wk, []) if iso in worked
            )
            if got < 1:
                h9_ok = False
                h9_bad.append(f"{nurse.name}: 0 Saturdays in weeks {ws + 1}-{we + 1}")
                break
    report.rules.append(
        RuleResult(
            "Everyone works >=1 Saturday per month",
            "Unit policy (H9)",
            "PASS" if h9_ok else "FAIL",
            "All non-waived nurses have a Saturday in every 4-week window."
            if h9_ok else "Gaps -> " + "; ".join(h9_bad),
        )
    )

    # --- Work-every-week guarantee (per Mon-Fri business week) -------------
    ww_lines = [n for n in cfg.nurses if n.fixed_work_weekly]
    if ww_lines and operating:
        biz_weekday_iso: dict[int, list] = {}
        for od in operating:
            if not od.is_saturday:
                bw = business_week_index(od.d, cfg.start, cfg.weeks)
                biz_weekday_iso.setdefault(bw, []).append(od.iso)
        ww_ok = True
        ww_bad = []
        for nurse in ww_lines:
            worked = _nurse_worked_dates(assignments, nurse.name)
            for bw, isos in biz_weekday_iso.items():
                if not any(i in worked for i in isos):
                    ww_ok = False
                    ww_bad.append(f"{nurse.name}: no weekday in business week {bw + 1}")
        report.rules.append(
            RuleResult(
                "Work-every-week nurses work each Mon-Fri week",
                "Unit policy (hard)",
                "PASS" if ww_ok else "FAIL",
                "Nurses: " + ", ".join(n.name for n in ww_lines) + ". "
                + ("Each works >=1 weekday shift in every business week."
                   if ww_ok else "Gaps -> " + "; ".join(ww_bad)),
            )
        )

    # --- Fri-before-Sat guarantee (every worked Saturday has its Friday) ---
    # Solver-enforced; re-checked so a manual edit/swap that strands a Saturday
    # without its Friday is caught (and hard-blocks a swap -- see _swap_hard_blocks).
    fbs_lines = [n for n in cfg.nurses if n.fixed_fri_before_sat]
    if fbs_lines and operating:
        fri_iso_by_week = {od.week_index: od.iso for od in operating if od.weekday == 4}
        fbs_ok = True
        fbs_bad = []
        for nurse in fbs_lines:
            worked = _nurse_worked_dates(assignments, nurse.name)
            for od in operating:
                if od.is_saturday and od.iso in worked:
                    fri = fri_iso_by_week.get(od.week_index)
                    if not (fri and fri in worked):
                        fbs_ok = False
                        fbs_bad.append(f"{nurse.name}: Saturday {od.iso} without its Friday")
        report.rules.append(
            RuleResult(
                "Fri-before-Sat nurses work the preceding Friday",
                "Unit policy (hard)",
                "PASS" if fbs_ok else "FAIL",
                "Nurses: " + ", ".join(n.name for n in fbs_lines) + ". "
                + ("Every worked Saturday is preceded by its Friday."
                   if fbs_ok else "Gaps -> " + "; ".join(fbs_bad)),
            )
        )

    # --- Shift-count targets met (primary) --------------------------------
    sc_ok = True
    sc_lines = []
    for nurse in cfg.nurses:
        worked = _nurse_worked_dates(assignments, nurse.name)
        d10 = sum(1 for i in worked if i in od_by_iso and not od_by_iso[i].is_saturday)
        d5 = sum(1 for i in worked if i in od_by_iso and od_by_iso[i].is_saturday)
        # Worked D10 target excludes the stat days actually taken (ST off).
        es = eff_stat[nurse.name]
        wd10 = max(0, nurse.target_d10 - es)
        if d10 != wd10 or d5 != nurse.target_d5:
            sc_ok = False
            stat = f" ({es} ST)" if es else ""
            sc_lines.append(
                f"{nurse.name}: D10 {d10}/{wd10}{stat}, D5 {d5}/{nurse.target_d5}"
            )
    report.rules.append(
        RuleResult(
            "Shift-count targets met (D10 + D5 per nurse)",
            "Unit policy (hard)",
            "PASS" if sc_ok else "FAIL",
            "Every nurse hits their requested worked shift counts (stat days excluded)."
            if sc_ok else "Off target -> " + "; ".join(sc_lines),
        )
    )

    # --- FTE within flex (secondary; vs the *worked* target, stats excluded)
    d10_paid, sat_paid = cfg.d10_paid(), cfg.sat_paid()
    period_full = cfg.weekly_full_time_hours * cfg.weeks
    fte_all_ok = True
    fte_lines = []
    for nurse in cfg.nurses:
        worked = _nurse_worked_dates(assignments, nurse.name)
        total_hours = sum(od_by_iso[i].paid_hours for i in worked if i in od_by_iso)
        sf = scheduled_fte(total_hours, cfg.weeks)
        # Compare against the worked-hours target (stat days are paid separately,
        # not scheduled), so honouring stat time off does not read as under-FTE.
        nm_wd10 = max(0, nurse.target_d10 - eff_stat[nurse.name])
        worked_target_hours = nm_wd10 * d10_paid + nurse.target_d5 * sat_paid
        worked_target_fte = worked_target_hours / period_full if period_full else 0.0
        dev = sf - worked_target_fte
        line_tol = nurse.tolerance(cfg.fte_tolerance)
        within = abs(dev) <= line_tol + 1e-9
        if not within:
            fte_all_ok = False
            fte_lines.append(f"{nurse.name}: {sf:.3f} (dev {dev:+.3f}, flex ±{line_tol})")

        n_sat_worked = sum(
            1 for i in worked if i in od_by_iso and od_by_iso[i].is_saturday
        )
        n_d10 = sum(
            1 for i in worked if i in od_by_iso and not od_by_iso[i].is_saturday
        )
        elig_sat = sum(
            1 for od in sats if od.iso not in nurse.unavailable_dates
        )
        worst, _cap = _worst_rolling_9wk_sat(cfg, operating, nurse.name, assignments)
        report.nurse_summaries.append(
            NurseSummary(
                name=nurse.name,
                target_fte=round(worked_target_fte, 3),
                scheduled_fte=round(sf, 3),
                deviation=round(dev, 3),
                total_hours=round(total_hours, 1),
                avg_weekly_hours=round(total_hours / cfg.weeks, 2),
                saturdays_worked=n_sat_worked,
                saturdays_in_period=elig_sat,
                worst_9wk_sat=worst,
                within_tolerance=within,
                target_d10=nm_wd10,
                scheduled_d10=n_d10,
                target_d5=nurse.target_d5,
                scheduled_d5=n_sat_worked,
            )
        )
    report.rules.append(
        RuleResult(
            "Derived FTE within flex (secondary)",
            f"26.01 + config (default +/-{cfg.fte_tolerance}, per-nurse)",
            "PASS" if fte_all_ok else "WARN",
            "All nurses within their FTE flex (derived from shift counts)."
            if fte_all_ok
            else "Outside flex (shift counts take priority) -> " + "; ".join(fte_lines),
        )
    )

    # --- Low-FTE lines: 3 of every 4 weeks active (soft target) -----------
    low_lines = [n for n in cfg.nurses if n.target_fte < LOW_FTE_THRESHOLD]
    if low_lines:
        week_active: dict[str, list] = {}
        for nurse in cfg.nurses:
            worked = _nurse_worked_dates(assignments, nurse.name)
            flags = [False] * cfg.weeks
            for od in operating:
                if od.iso in worked:
                    flags[od.week_index] = True
            week_active[nurse.name] = flags

        worst_lines = []
        all_ok = True
        for nurse in low_lines:
            flags = week_active[nurse.name]
            worst = THREE_OF_FOUR_MIN_ACTIVE
            for ws in range(0, max(1, cfg.weeks - THREE_OF_FOUR_WINDOW + 1)):
                window = flags[ws:ws + THREE_OF_FOUR_WINDOW]
                worst = min(worst, sum(window))
            if worst < THREE_OF_FOUR_MIN_ACTIVE:
                all_ok = False
                worst_lines.append(f"{nurse.name}: worst window {worst}/4 active")
        report.rules.append(
            RuleResult(
                "Low-FTE nurses active >=3 of every 4 weeks",
                "Unit policy (soft)",
                "PASS" if all_ok else "WARN",
                f"Applies to nurses below {LOW_FTE_THRESHOLD:.2f} FTE: "
                + (", ".join(n.name for n in low_lines))
                + ". "
                + ("All meet the 3-of-4 target."
                   if all_ok else "Below target -> " + "; ".join(worst_lines)),
            )
        )

    # --- Statutory holidays in the period (BCNU stat-holidays article) -----
    end_excl = cfg.start + timedelta(weeks=cfg.weeks)
    stats = holidays_in_range(cfg.start, end_excl)
    stat_credit = ", ".join(
        f"{n.name} {n.stat_days}" for n in cfg.nurses if n.stat_days
    )
    report.rules.append(
        RuleResult(
            "Statutory holidays (paid entitlement)",
            "BCNU stat holidays",
            "INFO",
            f"{len(stats)} stat holiday(s) fall in this rotation: "
            + (", ".join(f"{d.strftime('%a %d-%b')} {name}" for d, name in stats)
               or "none")
            + ". Stat days are paid (a weekday-shift equivalent) and reduce worked "
            "D10 shifts. Per-nurse stat credit: " + (stat_credit or "none set") + ".",
        )
    )

    # --- Meal-window note for D10 (informational, 26.03/26.04) ------------
    report.rules.append(
        RuleResult(
            "Meal window for D10 shifts",
            "26.03 / 26.04",
            "INFO",
            "D10 30-min meal must begin no later than 1230 (<=5.0h after 0730 "
            "start). Two paid 15-min rest periods per D10; one per D5. Paid hours "
            "assume the 30-min unpaid meal (D10 = 9.5h); a missed meal is paid as "
            "overtime (Art. 27, flagged not priced). Breaks are not nurse-scheduled "
            "here; see the Schedule legend.",
        )
    )

    # --- 25.05 posting check ----------------------------------------------
    today = date.today()
    lead_days = (cfg.start - today).days
    if lead_days < 42:
        posting_status = "WARN"
        posting_detail = (
            f"Schedule starts in {lead_days} day(s) (< 6 weeks). 25.05 requires "
            "posting 6 weeks in advance."
        )
    else:
        posting_status = "PASS"
        posting_detail = f"Start is {lead_days} days out (>= 6 weeks)."
    report.rules.append(
        RuleResult("6-week posting lead time", "25.05", posting_status, posting_detail)
    )
    report.rules.append(
        RuleResult(
            "Short-notice change overtime",
            "25.08",
            "INFO",
            "Changes made within 10 calendar days of a shift trigger overtime on "
            "the first changed shift. The app flags but does not price this (Art. 27).",
        )
    )

    # --- Documented non-conformance (25.06(D)) ----------------------------
    report.rules.append(
        RuleResult(
            "Off-duty day consecutiveness",
            "25.06(D)",
            "INFO",
            "Off-duty days cannot all be consecutive on a Mon/Wed/Fri/Sat unit "
            "(Tue/Thu are isolated closure days); written employee agreement "
            "recommended. Not solved by design.",
        )
    )

    # --- EWD memorandum note (25.11 / 26.01) ------------------------------
    report.rules.append(
        RuleResult(
            "Extended Work Day verification",
            "25.11 / 26.01",
            "INFO",
            "D10 (10.0h elapsed) exceeds the 7.5h normal daily full shift; verify "
            "against your Extended Work Day Memorandum terms.",
        )
    )

    return report
