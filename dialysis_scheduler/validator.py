"""Validation pass (Section 8).

Always runs on the final schedule, even when the solver reports success.
Produces a per-rule PASS / FAIL / INFO report with article citations, plus
per-nurse FTE and Saturday detail for the Summary sheet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .config import Config
from .model import OperatingDate, saturday_dates
from .fte import scheduled_fte
from .holidays import holidays_in_range
from .scheduler import (
    SAT_MAX_PER_9WK,
    SAT_WINDOW_WEEKS,
    SAT_PER_MONTH_WINDOW,
    LOW_FTE_THRESHOLD,
    THREE_OF_FOUR_WINDOW,
    THREE_OF_FOUR_MIN_ACTIVE,
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


def _nurse_worked_dates(assignments: dict, name: str) -> set:
    return set(assignments.get(name, {}).keys())


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

    od_by_iso = {od.iso: od for od in operating}
    sats = saturday_dates(operating)
    n_saturdays = len(sats)

    # --- Coverage (H1): weekdays >= demand (extras OK), Saturday == demand --
    coverage_ok = True
    bad_days = []
    extras = []
    total_extra = 0
    for od in operating:
        assigned = sum(
            1 for name in assignments if od.iso in assignments[name]
        )
        if od.is_saturday:
            if assigned != od.demand:
                coverage_ok = False
                bad_days.append(f"{od.iso} (Sat): {assigned}/{od.demand}")
        else:
            if assigned < od.demand:
                coverage_ok = False
                bad_days.append(f"{od.iso} ({od.weekday_name}): {assigned}/{od.demand}")
            elif assigned > od.demand:
                total_extra += assigned - od.demand
                extras.append(f"{od.iso} (+{assigned - od.demand})")
    detail = "All operating days meet demand"
    detail += f"; {total_extra} extra weekday shift(s): {', '.join(extras)}." if extras else "."
    if not coverage_ok:
        detail = "Demand not met / Saturday over-staffed: " + "; ".join(bad_days)
    report.rules.append(
        RuleResult(
            "Daily coverage (weekday >= demand, Saturday exact)",
            "Operational (H1)",
            "PASS" if coverage_ok else "FAIL",
            detail,
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
                both = [n.name for n in members if od.iso in assignments.get(n.name, {})]
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
            "All non-waived lines have a Saturday in every 4-week window."
            if h9_ok else "Gaps -> " + "; ".join(h9_bad),
        )
    )

    # --- Shift-count targets met (primary) --------------------------------
    sc_ok = True
    sc_lines = []
    for nurse in cfg.nurses:
        worked = _nurse_worked_dates(assignments, nurse.name)
        d10 = sum(1 for i in worked if i in od_by_iso and not od_by_iso[i].is_saturday)
        d5 = sum(1 for i in worked if i in od_by_iso and od_by_iso[i].is_saturday)
        # Worked D10 target excludes paid stat days (those reduce worked shifts).
        wd10 = nurse.worked_d10()
        if d10 != wd10 or d5 != nurse.target_d5:
            sc_ok = False
            stat = f" (+{nurse.stat_days} stat)" if nurse.stat_days else ""
            sc_lines.append(
                f"{nurse.name}: D10 {d10}/{wd10}{stat}, D5 {d5}/{nurse.target_d5}"
            )
    report.rules.append(
        RuleResult(
            "Shift-count targets met (D10 + D5 per line)",
            "Unit policy (hard)",
            "PASS" if sc_ok else "FAIL",
            "Every line hits its requested worked shift counts (stat days excluded)."
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
        worked_target_hours = nurse.worked_d10() * d10_paid + nurse.target_d5 * sat_paid
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
                target_d10=nurse.worked_d10(),
                scheduled_d10=n_d10,
                target_d5=nurse.target_d5,
                scheduled_d5=n_sat_worked,
            )
        )
    report.rules.append(
        RuleResult(
            "Derived FTE within flex (secondary)",
            f"26.01 + config (default +/-{cfg.fte_tolerance}, per-line)",
            "PASS" if fte_all_ok else "WARN",
            "All lines within their FTE flex (derived from shift counts)."
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
                "Low-FTE lines active >=3 of every 4 weeks",
                "Unit policy (soft)",
                "PASS" if all_ok else "WARN",
                f"Applies to lines below {LOW_FTE_THRESHOLD:.2f} FTE: "
                + (", ".join(n.name for n in low_lines))
                + ". "
                + ("All meet the 3-of-4 target."
                   if all_ok else "Below target -> " + "; ".join(worst_lines)),
            )
        )

    # --- Statutory holidays in the period (Art. 17) -----------------------
    end_excl = cfg.start + timedelta(weeks=cfg.weeks)
    stats = holidays_in_range(cfg.start, end_excl)
    stat_credit = ", ".join(
        f"{n.name} {n.stat_days}" for n in cfg.nurses if n.stat_days
    )
    report.rules.append(
        RuleResult(
            "Statutory holidays (paid entitlement)",
            "BCNU Art. 17",
            "INFO",
            f"{len(stats)} stat holiday(s) fall in this rotation: "
            + (", ".join(f"{d.strftime('%a %d-%b')} {name}" for d, name in stats)
               or "none")
            + ". Stat days are paid (a weekday-shift equivalent) and reduce worked "
            "D10 shifts. Per-line stat credit: " + (stat_credit or "none set") + ".",
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
