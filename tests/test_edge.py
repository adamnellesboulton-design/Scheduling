"""Edge-case / robustness tests — the failure modes a base system must handle.

Run: python tests/test_edge.py
"""

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dialysis_scheduler.config import default_config, Config, Nurse
from dialysis_scheduler.scheduler import generate_schedule, generate_schedules
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
        Nurse("Sam", target_d10=20, target_d5=5, seniority_rank=1),
        Nurse("Sam", target_d10=20, target_d5=5, seniority_rank=2),
    ]
    r = generate_schedule(cfg)
    assert not r.feasible and r.status == "CONFIG_INVALID"
    assert any("Duplicate" in m for m in r.messages)


def test_blank_name_rejected():
    cfg = default_config(_friday())
    cfg.nurses.append(Nurse("  ", target_d10=10, target_d5=2, seniority_rank=6))
    r = generate_schedule(cfg)
    assert not r.feasible and r.status == "CONFIG_INVALID"


def test_demand_exceeds_staff():
    cfg = default_config(_friday())
    cfg.demand = {"Mon": 99, "Wed": 1, "Fri": 1, "Sat": 1}
    r = generate_schedule(cfg)
    assert not r.feasible
    assert any("demands" in m for m in r.messages)


def test_saturday_per_month_infeasible_for_big_pool():
    cfg = default_config(_friday())
    cfg.nurses = [
        Nurse(f"N{i}", target_d10=10, target_d5=2, seniority_rank=i + 1)
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


def test_three_options_distinct_and_deterministic():
    cfg = default_config(_friday())
    a = generate_schedules(cfg)
    b = generate_schedules(cfg)
    assert 1 <= len(a) <= 3
    # reproducible
    assert all(x.assignments == y.assignments for x, y in zip(a, b))
    # distinct
    if len(a) >= 2:
        assert a[0].assignments != a[1].assignments


def test_job_share_never_same_day():
    cfg = default_config(_friday())
    cfg.demand = {"Mon": 2, "Wed": 2, "Fri": 2, "Sat": 1}
    for n in cfg.nurses[:2]:
        n.job_share_group = "A"
    r = generate_schedule(cfg)
    if r.feasible and r.method == "cp-sat":
        a, b = cfg.nurses[0].name, cfg.nurses[1].name
        overlap = sum(
            1 for od in r.operating
            if od.iso in r.assignments[a] and od.iso in r.assignments[b]
        )
        assert overlap == 0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("OK", fn.__name__)
    print("\nALL EDGE TESTS PASSED")
