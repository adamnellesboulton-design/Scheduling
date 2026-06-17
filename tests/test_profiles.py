"""Profile-behaviour tests — confirm the three options each do their job.

Preference-maximizing honours ticked preferences; equity-maximizing balances
weekday types; cluster-maximizing groups shifts into longer days-off blocks.

Run: python tests/test_profiles.py
"""

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dialysis_scheduler.config import default_config
from dialysis_scheduler.scheduler import generate_schedules
from dialysis_scheduler.model import build_operating_dates, is_worked


def _friday(weeks_out=8):
    t = date.today()
    return (t + timedelta(days=(4 - t.weekday()) % 7, weeks=weeks_out)).isoformat()


def _by_label(opts):
    return {o.label.split("-")[0]: o for o in opts}


def _weekday_count(op, a, name, wd):
    return sum(1 for d in op if d.weekday == wd and is_worked(a[name].get(d.iso)))


def _wd_spread(cfg, op, opt):
    """Sum over nurses of (max-min of Mon/Wed/Fri counts) — lower = fairer."""
    tot = 0
    for n in cfg.nurses:
        c = [_weekday_count(op, opt.assignments, n.name, wd) for wd in (0, 2, 4)]
        tot += max(c) - min(c)
    return tot


def _off_off(cfg, op, opt):
    """Adjacent off/off calendar-day pairs across all nurses — higher = clustered."""
    start = cfg.start
    tot = 0
    for n in cfg.nurses:
        w = set(opt.assignments[n.name])
        on = [1 if is_worked(opt.assignments[n.name].get((start + timedelta(days=k)).isoformat())) else 0
              for k in range(7 * cfg.weeks)]
        tot += sum(1 for k in range(len(on) - 1) if on[k] == 0 and on[k + 1] == 0)
    return tot


def _consec_sat(op, a, name):
    sats = sorted(d.week_index for d in op if d.is_saturday and a[name].get(d.iso)=='D5')
    return sum(1 for x, y in zip(sats, sats[1:]) if y - x == 1)


def test_preference_option_honours_off_day():
    # Use Kaitlyn (12 D10): she has slack to avoid Fridays (fill from Mon/Wed).
    # A saturated nurse like Adam (31 of 36 weekday slots) is forced onto Fridays
    # regardless, so the off-Friday preference can't move them — not a useful probe.
    cfg = default_config(_friday())
    for n in cfg.nurses:
        if n.name == "Kaitlyn":
            n.pref_off_fri = True
    opts = _by_label(generate_schedules(cfg))
    op = build_operating_dates(cfg)
    pref_fri = _weekday_count(op, opts["Preference"].assignments, "Kaitlyn", 4)
    clus_fri = _weekday_count(op, opts["Cluster"].assignments, "Kaitlyn", 4)
    print(f"  Kaitlyn Fridays — preference={pref_fri}, cluster={clus_fri}")
    assert pref_fri < clus_fri, "preference option should reduce Kaitlyn's Fridays"
    assert pref_fri == 0, "with slack, the off-Friday wish should be fully met"


def test_equity_option_is_most_balanced():
    cfg = default_config(_friday())
    opts = _by_label(generate_schedules(cfg))
    op = build_operating_dates(cfg)
    sp = {k: _wd_spread(cfg, op, v) for k, v in opts.items()}
    print(f"  weekday spread (lower=fairer) — {sp}")
    assert sp["Equity"] <= sp["Cluster"]
    assert sp["Equity"] <= sp["Preference"]


def test_cluster_option_is_most_clustered():
    cfg = default_config(_friday())
    opts = _by_label(generate_schedules(cfg))
    op = build_operating_dates(cfg)
    oo = {k: _off_off(cfg, op, v) for k, v in opts.items()}
    print(f"  off/off pairs (higher=clustered) — {oo}")
    assert oo["Cluster"] >= oo["Equity"]
    assert oo["Cluster"] >= oo["Preference"]


def test_nonconsec_sat_preference_reduces_back_to_back():
    cfg = default_config(_friday())
    for n in cfg.nurses:
        if n.name == "Kathleen":
            n.pref_nonconsec_sat = True
    opts = _by_label(generate_schedules(cfg))
    op = build_operating_dates(cfg)
    pref_cs = _consec_sat(op, opts["Preference"].assignments, "Kathleen")
    print(f"  Kathleen back-to-back Saturdays in preference option = {pref_cs}")
    # With the preference on, the preference option should avoid them entirely.
    assert pref_cs == 0


def test_overstaffing_extra_avoids_wednesday():
    """When a week is overstaffed (109 D10 vs 108 seats), the extra shift lands on
    Monday or Friday, never mid-week Wednesday, in every option."""
    cfg = default_config(_friday())  # default roster: exactly one extra weekday
    op = build_operating_dates(cfg)
    for o in generate_schedules(cfg):
        wed_extra = 0
        for od in op:
            if od.is_saturday:
                continue
            assigned = sum(1 for n in cfg.nurses
                           if is_worked(o.assignments[n.name].get(od.iso)))
            if assigned > od.demand and od.weekday == 2:
                wed_extra += assigned - od.demand
        assert wed_extra == 0, f"{o.label}: {wed_extra} extra shift(s) on Wednesday"
    print("  overstaffing: extra shifts avoid Wednesday in all options")


def test_even_spread_preference_balances_weekdays():
    """Ticking 'prefer even spread' lowers that nurse's Mon/Wed/Fri imbalance in
    the Preference option (where global weekday-equity is otherwise low)."""
    def adam_spread(flag):
        cfg = default_config(_friday())
        for n in cfg.nurses:
            if n.name == "Adam":
                n.pref_even_spread = flag
        pref = _by_label(generate_schedules(cfg))["Preference"]
        op = build_operating_dates(cfg)
        c = [_weekday_count(op, pref.assignments, "Adam", wd) for wd in (0, 2, 4)]
        return max(c) - min(c)
    off, on = adam_spread(False), adam_spread(True)
    print(f"  even spread: Adam weekday spread off={off} on={on}")
    assert on < off, "even-spread preference should reduce the weekday imbalance"


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        print(fn.__name__)
        fn()
    print("\nALL PROFILE TESTS PASSED")
