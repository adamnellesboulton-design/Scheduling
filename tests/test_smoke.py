"""Smoke tests for the scheduler core (no Streamlit needed)."""

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dialysis_scheduler.config import default_config, Config, default_operating_shifts
from dialysis_scheduler.fte import achievable_fte_menu, max_achievable_fte, scheduled_fte
from dialysis_scheduler.scheduler import generate_schedule
from dialysis_scheduler.validator import validate
from dialysis_scheduler.excel_export import build_workbook, output_filename


def _monday(weeks_out=8):
    # The rotation now starts on a Friday (weekday 4).
    today = date.today()
    days_ahead = (4 - today.weekday()) % 7
    start = today + timedelta(days=days_ahead, weeks=weeks_out)
    return start.isoformat()


def test_fte_menu():
    cfg = default_config(_monday())
    menu = achievable_fte_menu(cfg)
    ftes = [o.fte for o in menu]
    assert ftes == sorted(ftes)
    assert len(ftes) == len(set(ftes)), "FTE menu must be deduped"
    assert max_achievable_fte(cfg) == 0.89, max_achievable_fte(cfg)
    # designated-available meal raises the cap to 0.93
    cfg.meal_designated_available = True
    assert max_achievable_fte(cfg) == 0.93
    print(f"FTE menu ({len(menu)} options): {[o.label for o in menu]}")


def test_generate_and_validate():
    cfg = default_config(_monday())
    res = generate_schedule(cfg)
    print("method:", res.method, "status:", res.status, "feasible:", res.feasible)
    for m in res.messages:
        print("  msg:", m)
    assert res.feasible, res.messages + res.binding_constraints
    report = validate(cfg, res)
    statuses = {r.rule: r.status for r in report.rules}
    print("rule statuses:", statuses)
    # Hard-rule rows must not FAIL.
    hard = [
        "Daily coverage (blank shifts allowed when short-staffed)",
        "Max 6 consecutive scheduled days",
        "Off >=3 Saturdays per rolling 9-week window",
        "Everyone works >=1 Saturday per month",
    ]
    for h in hard:
        assert statuses[h] == "PASS", f"{h} -> {statuses[h]}"
    # Default roster is sized to hit its shift counts exactly.
    assert statuses["Shift-count targets met (D10 + D5 per nurse)"] == "PASS"
    assert report.max_consecutive_days <= 6

    for s in report.nurse_summaries:
        print(f"  {s.name}: D10 {s.scheduled_d10}/{s.target_d10} "
              f"D5 {s.scheduled_d5}/{s.target_d5} fte {s.scheduled_fte} "
              f"sats {s.saturdays_worked}/{s.saturdays_in_period}")


def test_excel_output():
    cfg = default_config(_monday())
    res = generate_schedule(cfg)
    report = validate(cfg, res)
    wb = build_workbook(cfg, res, report)
    assert wb.sheetnames == ["Schedule", "Summary", "Compliance", "Config"]
    fn = output_filename(cfg)
    out = os.path.join(os.path.dirname(__file__), fn)
    wb.save(out)
    print("wrote", out, os.path.getsize(out), "bytes")


def test_short_staffed_leaves_blanks():
    # Coverage is soft now: Saturday demand beyond capacity is feasible, with
    # the unfillable Saturday shifts left blank (flagged), not an error.
    cfg = default_config(_monday())
    cfg.demand["Sat"] = 6
    res = generate_schedule(cfg)
    print("short-staffed test:", res.status, res.method)
    assert res.feasible and res.method == "cp-sat"
    rep = validate(cfg, res)
    assert rep.unfilled_shifts > 0
    print("  unfilled shifts:", rep.unfilled_shifts)


if __name__ == "__main__":
    test_fte_menu()
    print("--- generate/validate ---")
    test_generate_and_validate()
    print("--- excel ---")
    test_excel_output()
    print("--- infeasible ---")
    test_short_staffed_leaves_blanks()
    print("\nALL SMOKE TESTS PASSED")
