"""Contract-compliance tests.

Independently re-checks every hard rule (BCNU Art. 25/26 + unit policy) on the
actual generated schedules, across all three objective profiles and several
period lengths. This is a cross-check on the solver/validator, not a restatement.

Run: python tests/test_compliance.py
"""

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dialysis_scheduler.config import default_config, Nurse
from dialysis_scheduler.scheduler import generate_schedules
from dialysis_scheduler.model import build_operating_dates, is_worked


def _friday(weeks_out=8):
    t = date.today()
    return (t + timedelta(days=(4 - t.weekday()) % 7, weeks=weeks_out)).isoformat()


def _sat_by_week(op):
    d = {}
    for od in op:
        if od.is_saturday:
            d.setdefault(od.week_index, []).append(od.iso)
    return d


def check_contract(cfg, opt) -> list:
    """Return a list of contract violations (empty == fully compliant)."""
    op = build_operating_dates(cfg)
    a = opt.assignments
    W = cfg.weeks
    errs = []

    # H1 coverage: weekday >= demand, Saturday exact.
    for od in op:
        assigned = sum(1 for n in cfg.nurses if is_worked(a.get(n.name, {}).get(od.iso)))
        if od.is_saturday and assigned != od.demand:
            errs.append(f"Sat coverage {od.iso}: {assigned}/{od.demand}")
        if not od.is_saturday and assigned < od.demand:
            errs.append(f"Weekday coverage {od.iso}: {assigned}<{od.demand}")

    # H10 exact shift counts (worked D10 excludes stat days).
    for n in cfg.nurses:
        d10 = sum(1 for od in op if not od.is_saturday and a.get(n.name, {}).get(od.iso)=='D10')
        d5 = sum(1 for od in op if od.is_saturday and a.get(n.name, {}).get(od.iso)=='D5')
        if d10 != n.worked_d10():
            errs.append(f"{n.name} D10 {d10}!={n.worked_d10()}")
        if d5 != n.target_d5:
            errs.append(f"{n.name} D5 {d5}!={n.target_d5}")

    sbw = _sat_by_week(op)

    # H2 25.06(E): <=6 Saturdays per rolling 9-week window (proportional <9wk).
    windows9 = [(w, w + 8) for w in range(0, W - 8)] if W >= 9 else [(0, W - 1)]
    cap = 6 if W >= 9 else (6 * W) // 9
    for n in cfg.nurses:
        for ws, we in windows9:
            c = sum(1 for wk in range(ws, we + 1)
                    for iso in sbw.get(wk, []) if a.get(n.name, {}).get(iso)=='D5')
            if c > cap:
                errs.append(f"{n.name} Saturday cap {c}>{cap} (weeks {ws}-{we})")

    # H9: >=1 Saturday per rolling 4-week window.
    windows4 = [(w, w + 3) for w in range(0, W - 3)] if W >= 4 else [(0, W - 1)]
    for n in cfg.nurses:
        for ws, we in windows4:
            c = sum(1 for wk in range(ws, we + 1)
                    for iso in sbw.get(wk, []) if a.get(n.name, {}).get(iso)=='D5')
            if c < 1:
                errs.append(f"{n.name} 0 Saturdays in weeks {ws}-{we}")

    # H4: <=6 consecutive calendar days.
    for n in cfg.nurses:
        days = sorted(date.fromisoformat(i) for i,c in a.get(n.name, {}).items() if is_worked(c))
        best = run = 1 if days else 0
        for p, q in zip(days, days[1:]):
            run = run + 1 if (q - p).days == 1 else 1
            best = max(best, run)
        if best > 6:
            errs.append(f"{n.name} {best} consecutive days")

    # H3: never scheduled on an unavailable date.
    for n in cfg.nurses:
        for iso in n.unavailable_dates:
            if iso in a.get(n.name, {}):
                errs.append(f"{n.name} worked unavailable {iso}")

    # H8 job share: never same day; combined FTE <= 1.0.
    groups = {}
    for n in cfg.nurses:
        g = (n.job_share_group or "").strip()
        if g:
            groups.setdefault(g, []).append(n)
    for g, mem in groups.items():
        if len(mem) < 2:
            continue
        for od in op:
            on = [m.name for m in mem if is_worked(a.get(m.name, {}).get(od.iso))]
            if len(on) > 1:
                errs.append(f"Job share {g} same day {od.iso}: {on}")
        if sum(m.target_fte for m in mem) > 1.0 + 1e-9:
            errs.append(f"Job share {g} combined FTE > 1.0")

    return errs


def test_default_roster_all_profiles_all_periods():
    for weeks in (6, 9, 12, 18):
        cfg = default_config(_friday())
        cfg.weeks = weeks
        # scale default counts to the period so counts stay satisfiable
        scale = weeks / 12
        for n in cfg.nurses:
            n.target_d10 = max(1, round(n.target_d10 * scale))
            n.target_d5 = max(1, round(n.target_d5 * scale))
        # rebalance D5 to match Saturday seats exactly
        sat_seats = cfg.demand["Sat"] * weeks
        d5s = [n.target_d5 for n in cfg.nurses]
        diff = sat_seats - sum(d5s)
        i = 0
        while diff != 0 and cfg.nurses:
            step = 1 if diff > 0 else -1
            ni = cfg.nurses[i % len(cfg.nurses)]
            if ni.target_d5 + step >= 1:
                ni.target_d5 += step
                diff -= step
            i += 1
            if i > 1000:
                break
        # rebalance D10 to at least weekday seats
        wd_seats = (cfg.demand["Mon"] + cfg.demand["Wed"] + cfg.demand["Fri"]) * weeks
        d10diff = wd_seats - sum(n.worked_d10() for n in cfg.nurses)
        j = 0
        while d10diff > 0:
            cfg.nurses[j % len(cfg.nurses)].target_d10 += 1
            d10diff -= 1
            j += 1
        opts = generate_schedules(cfg)
        assert opts and opts[0].feasible, f"weeks={weeks} infeasible: {opts[0].messages}"
        for o in opts:
            errs = check_contract(cfg, o)
            assert not errs, f"weeks={weeks} {o.label}: {errs[:5]}"
        print(f"  weeks={weeks}: {len(opts)} options, all contract-compliant "
              f"({', '.join(o.label.split('-')[0] for o in opts)})")


def test_job_share_compliant():
    cfg = default_config(_friday())
    cfg.demand = {"Mon": 2, "Wed": 2, "Fri": 2, "Sat": 2}
    cfg.nurses = [
        Nurse("JS_A", target_d10=10, target_d5=3, job_share_group="A"),
        Nurse("JS_B", target_d10=10, target_d5=3, job_share_group="A"),
        Nurse("N1", target_d10=18, target_d5=6),
        Nurse("N2", target_d10=18, target_d5=6),
        Nurse("N3", target_d10=16, target_d5=6),
    ]
    opts = generate_schedules(cfg)
    for o in opts:
        assert not check_contract(cfg, o), o.label
    print(f"  job share: {len(opts)} options compliant, no same-day overlap")


def test_unavailable_and_stat_compliant():
    # Default demand (3/3/3/2 -> 108 weekday seats, 24 Saturday seats). Kathleen
    # carries extra D10 to absorb Adam's 2 stat days so totals still match.
    cfg = default_config(_friday())
    cfg.nurses = [
        Nurse("Kathleen", target_d10=26, target_d5=6,
              unavailable_dates=[cfg.start.isoformat()]),  # off the first Friday
        Nurse("Adam", target_d10=24, target_d5=5, stat_days=2),  # works 22 D10
        Nurse("Joane", target_d10=21, target_d5=5),
        Nurse("Leslie", target_d10=21, target_d5=4),
        Nurse("Kaitlyn", target_d10=18, target_d5=4),
    ]
    opts = generate_schedules(cfg)
    assert opts[0].feasible, opts[0].messages
    for o in opts:
        assert not check_contract(cfg, o), o.label
        # Kathleen never scheduled on her unavailable Friday.
        assert cfg.start.isoformat() not in o.assignments["Kathleen"]
        # Adam works exactly 22 worked D10 and is shown ST on 2 holiday dates.
        adam = o.assignments["Adam"]
        d10 = sum(1 for od in build_operating_dates(cfg)
                  if not od.is_saturday and adam.get(od.iso) == "D10")
        st = sum(1 for c in adam.values() if c == "ST")
        assert d10 == 22 and st == 2, f"{o.label} Adam D10 {d10} ST {st}"
    print(f"  unavailable + stat: {len(opts)} options compliant; "
          "Adam works 22/24 D10 (2 stat), Kathleen off her unavailable day")


def test_fixed_off_mon_guaranteed():
    """A line ticked 'Mon off (fixed)' never works a Monday in any option."""
    cfg = default_config(_friday())
    for n in cfg.nurses:
        if n.name == "Adam":
            n.fixed_off_mon = True
    opts = generate_schedules(cfg)
    op = build_operating_dates(cfg)
    mondays = [d.iso for d in op if d.weekday == 0]
    for o in opts:
        assert opts[0].feasible
        assert not check_contract(cfg, o), o.label
        worked = [d for d in mondays if is_worked(o.assignments["Adam"].get(d))]
        assert not worked, f"{o.label}: Adam worked Mondays {worked}"
    print(f"  fixed Mon off: Adam works 0 of {len(mondays)} Mondays in all options")


def test_fixed_work_weekly_guaranteed():
    """A line ticked 'Work weekly' works >=1 WEEKDAY shift every week (Saturdays
    don't count) in any option."""
    cfg = default_config(_friday())
    for n in cfg.nurses:
        if n.name == "Joane":
            n.fixed_work_weekly = True
    opts = generate_schedules(cfg)
    op = build_operating_dates(cfg)
    weeks = sorted({d.week_index for d in op})
    for o in opts:
        assert not check_contract(cfg, o), o.label
        for wk in weeks:
            worked = any(is_worked(o.assignments["Joane"].get(d.iso))
                         for d in op if d.week_index == wk and not d.is_saturday)
            assert worked, f"{o.label}: Joane no weekday in week {wk}"
    print(f"  work weekly: Joane works a weekday in all {len(weeks)} weeks")


def test_fixed_fri_before_sat_guaranteed():
    """A line ticked 'Fri before Sat' works the preceding Friday for every
    worked Saturday, in any option."""
    cfg = default_config(_friday())
    for n in cfg.nurses:
        if n.name == "Leslie":
            n.fixed_fri_before_sat = True
    opts = generate_schedules(cfg)
    op = build_operating_dates(cfg)
    fri_by_wk = {d.week_index: d.iso for d in op if d.weekday == 4}
    sbw = _sat_by_week(op)
    for o in opts:
        assert not check_contract(cfg, o), o.label
        a = o.assignments["Leslie"]
        for wk, sats in sbw.items():
            for s in sats:
                if a.get(s) == "D5":
                    fri = fri_by_wk.get(wk)
                    assert fri and is_worked(a.get(fri)), \
                        f"{o.label}: Sat {s} worked without its Friday"
    print("  fri-before-sat: every Leslie Saturday is preceded by its Friday")


def test_reruns_stay_compliant():
    # Multi-worker solving is no longer byte-reproducible, but every run must
    # still be feasible, contract-compliant and hit the exact shift counts.
    cfg = default_config(_friday())
    for run in (generate_schedules(cfg), generate_schedules(cfg)):
        assert run[0].feasible
        for o in run:
            assert not check_contract(cfg, o), o.label
    print("  re-runs: every option compliant (counts/rules stable, layout may vary)")


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        print(fn.__name__)
        fn()
    print("\nALL COMPLIANCE TESTS PASSED")
