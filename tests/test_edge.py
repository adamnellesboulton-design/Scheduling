"""Edge-case / robustness tests — the failure modes a base system must handle.

Run: python tests/test_edge.py
"""

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dialysis_scheduler.config import default_config, Config, Nurse
from dialysis_scheduler.scheduler import generate_schedule, generate_schedules
from dialysis_scheduler.model import is_worked
from dialysis_scheduler.validator import validate


def _friday(weeks_out=8):
    t = date.today()
    return (t + timedelta(days=(4 - t.weekday()) % 7, weeks=weeks_out)).isoformat()


def test_empty_roster():
    cfg = default_config(_friday())
    cfg.nurses = []
    r = generate_schedule(cfg)
    assert not r.feasible and r.status == "CONFIG_INVALID"


def test_duplicate_names_rejected():
    cfg = default_config(_friday())
    cfg.nurses = [
        Nurse("Sam", target_d10=20, target_d5=5),
        Nurse("Sam", target_d10=20, target_d5=5),
    ]
    r = generate_schedule(cfg)
    assert not r.feasible and r.status == "CONFIG_INVALID"
    assert any("Duplicate" in m for m in r.messages)


def test_blank_name_rejected():
    cfg = default_config(_friday())
    cfg.nurses.append(Nurse("  ", target_d10=10, target_d5=2))
    r = generate_schedule(cfg)
    assert not r.feasible and r.status == "CONFIG_INVALID"


def test_demand_exceeds_staff_leaves_blanks():
    # Coverage is soft: more demand than staff is feasible, with blank shifts.
    cfg = default_config(_friday())
    cfg.demand = {"Mon": 99, "Wed": 3, "Fri": 3, "Sat": 2}
    r = generate_schedule(cfg)
    assert r.feasible and r.method == "cp-sat"
    rep = validate(cfg, r)
    assert rep.unfilled_shifts > 0  # Mondays can't be fully covered


def test_saturday_per_month_infeasible_for_big_pool():
    cfg = default_config(_friday())
    cfg.nurses = [
        Nurse(f"N{i}", target_d10=10, target_d5=2)
        for i in range(20)
    ]
    r = generate_schedule(cfg)
    assert not r.feasible  # 20 nurses can't each get a Saturday in a 4-wk window


def test_legacy_and_future_json_keys_ignored():
    cfg = default_config(_friday())
    d = cfg.to_dict()
    d["nurses"][0]["fixed_saturdays_off"] = True   # removed field
    d["nurses"][0]["future_key"] = 123             # unknown field
    d["totally_new_top_level"] = "x"
    back = Config.from_dict(d)
    assert len(back.nurses) == len(cfg.nurses)


def test_stat_days_exceed_target_clamped():
    cfg = default_config(_friday())
    cfg.nurses[0].stat_days = 999
    assert cfg.nurses[0].worked_d10() == 0
    r = generate_schedule(cfg)
    validate(cfg, r)  # must not raise


def test_three_profile_options():
    cfg = default_config(_friday())
    a = generate_schedules(cfg)
    assert 1 <= len(a) <= 3
    # labelled by objective profile
    labels = [r.label for r in a]
    assert labels == ["Preference-maximizing", "Equity-maximizing",
                      "Cluster-maximizing"][:len(a)]


def test_job_share_never_same_day():
    # A realistic job share: two part-timers splitting one line. Counts are sized
    # so the hard shift-counts, coverage and Saturday rules are all satisfiable.
    cfg = default_config(_friday())
    cfg.demand = {"Mon": 2, "Wed": 2, "Fri": 2, "Sat": 2}
    cfg.nurses = [
        Nurse("JS_A", target_d10=10, target_d5=3, job_share_group="A"),
        Nurse("JS_B", target_d10=10, target_d5=3, job_share_group="A"),
        Nurse("N1", target_d10=18, target_d5=6),
        Nurse("N2", target_d10=18, target_d5=6),
        Nurse("N3", target_d10=16, target_d5=6),
    ]
    r = generate_schedule(cfg)
    assert r.feasible and r.method == "cp-sat"
    overlap = sum(
        1 for od in r.operating
        if is_worked(r.assignments['JS_A'].get(od.iso)) and is_worked(r.assignments['JS_B'].get(od.iso))
    )
    assert overlap == 0


def test_job_share_combined_over_capacity_rejected():
    # Two near-full lines can't job-share (combined > 1.0 FTE / too many days).
    cfg = default_config(_friday())
    cfg.demand = {"Mon": 2, "Wed": 2, "Fri": 2, "Sat": 2}
    cfg.nurses = [
        Nurse("JS_A", target_d10=24, target_d5=6, job_share_group="A"),
        Nurse("JS_B", target_d10=24, target_d5=6, job_share_group="A"),
        Nurse("N1", target_d10=12, target_d5=4),
        Nurse("N2", target_d10=12, target_d5=4),
        Nurse("N3", target_d10=12, target_d5=4),
    ]
    r = generate_schedule(cfg)
    assert not r.feasible
    assert any("Job share" in m for m in r.messages)


def test_job_share_both_stat_off_not_flagged():
    """Job-share partners may both be ST (stat-holiday off) on the same date --
    neither is working, so the validator must NOT report a same-day clash.
    Regression: the H8 check used to test bare dict membership, counting ST/LV
    as 'present' and false-FAILing a perfectly legal schedule."""
    from dataclasses import replace
    from dialysis_scheduler.model import build_operating_dates
    from dialysis_scheduler.scheduler import ScheduleResult

    cfg = default_config(_friday())
    cfg.nurses = [
        Nurse("JS_A", target_d10=10, target_d5=3, job_share_group="A"),
        Nurse("JS_B", target_d10=10, target_d5=3, job_share_group="A"),
        Nurse("N1", target_d10=18, target_d5=6),
    ]
    op = build_operating_dates(cfg)
    weekday = next(o for o in op if not o.is_saturday)
    # Both partners marked ST (off) on the same weekday -> must be allowed.
    assignments = {
        "JS_A": {weekday.iso: "ST"},
        "JS_B": {weekday.iso: "ST"},
        "N1": {},
    }
    res = ScheduleResult(feasible=True, method="cp-sat", status="FEASIBLE",
                         assignments=assignments, operating=op)
    report = validate(cfg, res)
    js_rule = next(r for r in report.rules
                   if r.rule == "Job-share partners never share a day")
    assert js_rule.status == "PASS", js_rule.detail


def test_reoptimize_refuses_hard_violation():
    """reoptimize_to_fit must REFUSE (not silently allow) a pin that breaks a
    hard guarantee -- e.g. placing a Mon-off nurse on a Monday."""
    from dialysis_scheduler.scheduler import generate_schedule, reoptimize_to_fit
    cfg = default_config(_friday())
    for n in cfg.nurses:
        if n.name == "Kathleen":
            n.fixed_off_mon = True
    r = generate_schedule(cfg)
    monday = next(od.iso for od in r.operating if od.weekday == 0)
    rep = reoptimize_to_fit(cfg, r.operating, r.assignments,
                            [("Kathleen", monday, True)], seconds=3.0)
    assert not rep.ok
    assert "Kathleen" in rep.message


def test_reoptimize_keeps_schedule_compliant():
    """A valid pinned move re-optimizes to a fully compliant schedule that still
    hits every nurse's exact counts and honours the pin."""
    from dataclasses import replace
    from dialysis_scheduler.scheduler import generate_schedule, reoptimize_to_fit
    from dialysis_scheduler.model import is_worked
    cfg = default_config(_friday())
    r = generate_schedule(cfg)
    op = r.operating
    # Find any nurse + weekday they don't currently work but could (eligible, not
    # Mon-off) -- robust to the non-deterministic layout.
    pin_name = pin_iso = None
    for od in op:
        if od.is_saturday:
            continue
        for n in cfg.nurses:
            if (not is_worked(r.assignments[n.name].get(od.iso))
                    and od.iso not in n.unavailable_dates
                    and not (n.fixed_off_mon and od.weekday == 0)):
                pin_name, pin_iso = n.name, od.iso
                break
        if pin_name:
            break
    assert pin_name, "no free weekday cell found"
    rep = reoptimize_to_fit(cfg, op, r.assignments,
                            [(pin_name, pin_iso, True)], seconds=4.0)
    assert rep.ok, rep.message
    assert is_worked(rep.assignments[pin_name].get(pin_iso))  # pin honoured
    report = validate(cfg, replace(r, assignments=rep.assignments))
    fails = [x.rule for x in report.rules if x.status == "FAIL"]
    assert not fails, fails
    # Exact counts preserved.
    for s in report.nurse_summaries:
        assert s.scheduled_d10 == s.target_d10 and s.scheduled_d5 == s.target_d5


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("OK", fn.__name__)
    print("\nALL EDGE TESTS PASSED")
