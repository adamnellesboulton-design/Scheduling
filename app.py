"""Streamlit UI for the Pediatric Dialysis Unit Scheduler (Section 10).

Run with:  streamlit run app.py
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

from dialysis_scheduler.config import (
    Config,
    Nurse,
    DEFAULT_CONFIG_PATH,
    default_config,
)
from dialysis_scheduler.model import build_operating_dates, is_worked
from dialysis_scheduler.holidays import holidays_in_range
from dialysis_scheduler.scheduler import generate_schedule, generate_schedules
from dialysis_scheduler.validator import validate
from dialysis_scheduler.excel_export import workbook_bytes, output_filename

st.set_page_config(
    page_title="Dialysis Unit Scheduler",
    page_icon="🩺",
    layout="wide",
)


# --- look & feel -----------------------------------------------------------

# A calm, clinical visual layer: a teal hero band, card-like metrics, pill tabs
# and gentle section accents. Tuned to read as a professional hospital tool —
# restrained, high-contrast and easy on the eyes for non-technical clinical
# users — not flashy. Colours track the theme in .streamlit/config.toml.
_STYLE = """
<style>
  .block-container { padding-top: 2.2rem; max-width: 1380px; }

  /* Hero band */
  .ds-hero {
    background: linear-gradient(135deg, #0F6E6E 0%, #0B5563 100%);
    color: #fff; border-radius: 0.9rem;
    padding: 1.35rem 1.6rem; margin-bottom: 1.5rem;
    box-shadow: 0 6px 20px rgba(15, 110, 110, 0.20);
  }
  .ds-hero h1 {
    color: #fff; font-size: 1.7rem; line-height: 1.2;
    margin: 0 0 .3rem 0; font-weight: 700; letter-spacing: -0.01em;
  }
  .ds-hero p { color: #D7EAEA; margin: 0; font-size: 0.95rem; }
  .ds-hero .ds-tags { margin-top: .7rem; display: flex; gap: .4rem; flex-wrap: wrap; }
  .ds-hero .ds-tag {
    display: inline-block; background: rgba(255,255,255,0.14);
    border: 1px solid rgba(255,255,255,0.28); color: #EAF5F5;
    padding: .18rem .65rem; border-radius: 999px; font-size: 0.74rem; font-weight: 500;
  }

  /* Section headings: subtle teal left accent */
  [data-testid="stHeading"] h2 {
    border-left: 4px solid #0F6E6E; padding-left: .6rem;
    margin-top: .3rem; font-weight: 650;
  }

  /* Metric cards */
  [data-testid="stMetric"] {
    background: #fff; border: 1px solid #DCE4E5; border-radius: 0.7rem;
    padding: .7rem .95rem; box-shadow: 0 1px 2px rgba(31, 41, 51, 0.04);
  }
  [data-testid="stMetricValue"] { color: #0F6E6E; font-weight: 700; }
  [data-testid="stMetricLabel"] { color: #52616B; }

  /* Tabs as pills */
  .stTabs [data-baseweb="tab-list"] { gap: .4rem; border-bottom: none; }
  .stTabs [data-baseweb="tab"] {
    background: #F1F5F6; border-radius: 0.6rem; padding: .35rem 1rem;
    border: 1px solid #DCE4E5;
  }
  .stTabs [data-baseweb="tab"] p { font-weight: 600; color: #52616B; }
  .stTabs [aria-selected="true"] { background: #0F6E6E; border-color: #0F6E6E; }
  .stTabs [aria-selected="true"] p { color: #fff; }
  .stTabs [data-baseweb="tab-highlight"] { background: transparent; }

  /* Primary buttons a touch bolder */
  .stButton button[kind="primary"] { font-weight: 650; box-shadow: none; }

  /* Expanders read as light cards */
  [data-testid="stExpander"] details {
    border: 1px solid #DCE4E5; border-radius: 0.6rem; background: #FBFCFC;
  }
</style>
"""


def _inject_style():
    st.markdown(_STYLE, unsafe_allow_html=True)


def _hero():
    st.markdown(
        """
        <div class="ds-hero">
          <h1>🩺 Pediatric Dialysis Unit Scheduler</h1>
          <p>BC Children's Hospital · Hemodialysis · BCNU Provincial Collective
             Agreement (Art. 25–26)</p>
          <div class="ds-tags">
            <span class="ds-tag">Fri / Sat / Mon / Wed unit</span>
            <span class="ds-tag">Contract-compliant by construction</span>
            <span class="ds-tag">Three optimized options</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# --- session state ---------------------------------------------------------


def _init_state():
    if "cfg" not in st.session_state:
        st.session_state.cfg = default_config()


def _cfg() -> Config:
    return st.session_state.cfg


# --- sidebar ---------------------------------------------------------------


def sidebar():
    cfg = _cfg()
    st.sidebar.header("Configuration")
    st.sidebar.caption("Set the period and demand here; build the roster on the right.")

    # Load / save JSON config.
    with st.sidebar.expander("Load / save config", expanded=False):
        path = st.text_input("Config file path", value=DEFAULT_CONFIG_PATH)
        c1, c2 = st.columns(2)
        if c1.button("Load", width="stretch"):
            try:
                st.session_state.cfg = Config.load(path)
                st.success(f"Loaded {path}")
            except Exception as e:  # noqa: BLE001
                st.error(f"Load failed: {e}")
        if c2.button("Save", width="stretch"):
            try:
                cfg.save(path)
                st.success(f"Saved {path}")
            except Exception as e:  # noqa: BLE001
                st.error(f"Save failed: {e}")
        uploaded = st.file_uploader("…or upload a config JSON", type="json")
        if uploaded is not None:
            try:
                st.session_state.cfg = Config.from_dict(json.load(uploaded))
                st.success("Config loaded from upload.")
            except Exception as e:  # noqa: BLE001
                st.error(f"Invalid config: {e}")

    # Schedule period.
    st.sidebar.subheader("Schedule period")
    start = st.sidebar.date_input("Start date (must be a Friday)", value=cfg.start)
    if start.weekday() != 4:
        st.sidebar.error("Start date must be a Friday — the rotation starts Friday.")
    weeks = st.sidebar.number_input(
        "Rotation length (weeks)", min_value=6, max_value=52,
        value=int(cfg.weeks), step=1,
        help="The rotation repeats over this many weeks (default 12). "
             "Minimum 6 (25.05 posting). Common choices: 6, 9, 12, 18.",
    )
    cfg.start_date = start.isoformat()
    cfg.weeks = int(weeks)

    # Daily staffing demand.
    st.sidebar.subheader("Nurses needed per day")
    st.sidebar.caption("Weekdays are a minimum (extras allowed); Saturday is exact.")
    cols = st.sidebar.columns(4)
    for i, day in enumerate(["Mon", "Wed", "Fri", "Sat"]):
        cfg.demand[day] = int(
            cols[i].number_input(day, min_value=0, max_value=20,
                                 value=int(cfg.demand.get(day, 0)), step=1)
        )

    # Paid hours assume the 30-min unpaid meal (D10 = 9.5h); missed meals are
    # paid as overtime by default, so there is no meal-designation toggle.
    cfg.meal_designated_available = False
    # FTE flex is a fixed secondary check now (counts are the target); 0.08.
    cfg.fte_tolerance = 0.08

    # Statutory holidays in the rotation (BCNU Art. 17), for reference.
    stats = holidays_in_range(cfg.start, cfg.start + timedelta(weeks=cfg.weeks))
    st.sidebar.subheader("Statutory holidays")
    st.sidebar.caption(
        f"**{len(stats)}** fall in this rotation (BCNU Art. 17): "
        + (", ".join(f"{d.strftime('%d-%b')} {name}" for d, name in stats)
           or "none")
        + ". Set each line's stat-shift entitlement in the roster."
    )


# --- roster editor ---------------------------------------------------------


def roster_editor():
    cfg = _cfg()
    st.header("Nurse roster")

    st.caption(
        "Set each line's number of **10-hour weekday shifts (D10)**, "
        "**5-hour Saturday shifts (D5, ≥ 1)** and **stat shifts** over the whole "
        "rotation (no upper cap — scale them up for longer rotations). The counts "
        "are the primary target; stat shifts are paid statutory-holiday days "
        "(BCNU Art. 17) shown as **ST** on the actual holiday dates. **FTE** is "
        "derived (read-only). Preferences are honoured in the preference-maximizing "
        "option. Same **Job share** label = two lines never work the same day. "
        "**Everyone works Saturdays** (≥ 1 per month). Seniority is not used — "
        "lines are picked by seniority afterward."
    )

    rows = []
    for n in cfg.nurses:
        rows.append({
            "name": n.name,
            "d10": int(n.target_d10),
            "d5": int(n.target_d5),
            "stat": int(n.stat_days),
            "job_share": n.job_share_group,
            "pref_nonconsec_sat": n.pref_nonconsec_sat,
            "pref_clustered": n.pref_clustered,
            "fixed_off_mon": n.fixed_off_mon,
            "fixed_work_weekly": n.fixed_work_weekly,
            "fixed_fri_before_sat": n.fixed_fri_before_sat,
            "pref_off_mon": n.pref_off_mon,
            "pref_off_wed": n.pref_off_wed,
            "pref_off_fri": n.pref_off_fri,
            "unavailable_dates": ", ".join(n.unavailable_dates),
        })
    df = pd.DataFrame(rows)

    edited = st.data_editor(
        df,
        num_rows="dynamic",
        width="stretch",
        column_config={
            "name": st.column_config.TextColumn("Name", required=True),
            "d10": st.column_config.NumberColumn(
                "D10 shifts", min_value=0, step=1,
                help="Number of 10-hour weekday shifts over the whole rotation "
                     "(no upper cap — scale it up for longer rotations).",
            ),
            "d5": st.column_config.NumberColumn(
                "D5 shifts", min_value=1, step=1,
                help="Number of 5-hour Saturday shifts over the rotation "
                     "(>= 1; no upper cap). Everyone works some Saturdays.",
            ),
            "stat": st.column_config.NumberColumn(
                "Stat shifts", min_value=0, step=1,
                help="Paid statutory-holiday days (Art. 17); each reduces worked "
                     "D10 shifts by one. Capped at the holidays in the period.",
            ),
            "job_share": st.column_config.SelectboxColumn(
                "Job share", options=["", "A", "B", "C", "D"],
                help="Put the SAME label on two lines to job-share them — "
                     "they will never be scheduled on the same day.",
            ),
            "pref_nonconsec_sat": st.column_config.CheckboxColumn(
                "Non-consec Sat", help="Prefer to avoid back-to-back Saturdays"
            ),
            "pref_clustered": st.column_config.CheckboxColumn(
                "Cluster shifts",
                help="Group worked days (e.g. Fri+Sat) for longer consecutive days off"
            ),
            "fixed_off_mon": st.column_config.CheckboxColumn(
                "Mon off (fixed)",
                help="HARD guarantee: this line is NEVER scheduled a Monday, in "
                     "all three options. May leave a Monday short (blank shift) if "
                     "too many lines opt out.",
            ),
            "fixed_work_weekly": st.column_config.CheckboxColumn(
                "Work weekly",
                help="HARD guarantee: this line works at least one WEEKDAY shift "
                     "every BUSINESS week (Mon-Fri; Saturdays don't count). Needs "
                     "enough D10 shifts to cover every week.",
            ),
            "fixed_fri_before_sat": st.column_config.CheckboxColumn(
                "Fri before Sat",
                help="HARD guarantee: whenever this line works a Saturday, it also "
                     "works the preceding Friday. Needs D10 >= D5.",
            ),
            "pref_off_mon": st.column_config.CheckboxColumn(
                "Off Mon (soft)",
                help="SOFT preference: try to keep Mondays off (honoured in the "
                     "preference-maximizing option). Different from 'Mon off "
                     "(fixed)', which NEVER schedules a Monday in any option.",
            ),
            "pref_off_wed": st.column_config.CheckboxColumn(
                "Off Wed", help="Prefer Wednesdays off"
            ),
            "pref_off_fri": st.column_config.CheckboxColumn(
                "Off Fri", help="Prefer Fridays off"
            ),
            "unavailable_dates": st.column_config.TextColumn(
                "Unavailable dates", help="comma-separated YYYY-MM-DD (approved leave)"
            ),
        },
        key="roster_editor",
    )

    # Persist edits back into the config.
    new_nurses = []
    for _, r in edited.iterrows():
        name = str(r["name"]).strip()
        if not name:
            continue
        dates_raw = str(r.get("unavailable_dates") or "").strip()
        dates = _parse_dates(dates_raw)

        def _int(v, default=0):
            try:
                return int(round(float(v)))
            except (TypeError, ValueError):
                return default
        new_nurses.append(Nurse(
            name=name,
            target_d10=_int(r.get("d10")),
            target_d5=max(1, _int(r.get("d5"), 1)),  # everyone works Saturdays
            stat_days=_int(r.get("stat")),
            unavailable_dates=dates,
            job_share_group=str(r.get("job_share") or "").strip(),
            pref_nonconsec_sat=bool(r["pref_nonconsec_sat"]),
            pref_clustered=bool(r["pref_clustered"]),
            fixed_off_mon=bool(r.get("fixed_off_mon", False)),
            fixed_work_weekly=bool(r.get("fixed_work_weekly", False)),
            fixed_fri_before_sat=bool(r.get("fixed_fri_before_sat", False)),
            pref_off_mon=bool(r.get("pref_off_mon", False)),
            pref_off_wed=bool(r["pref_off_wed"]),
            pref_off_fri=bool(r["pref_off_fri"]),
        ))
    cfg.nurses = new_nurses
    cfg.apply_derived_ftes()

    # Live data-integrity warning (names must be unique; each line keyed by name).
    names = [n.name for n in cfg.nurses]
    dupes = sorted({nm for nm in names if names.count(nm) > 1})
    if dupes:
        st.error(f"Duplicate nurse name(s): {', '.join(dupes)}. Names must be unique.")

    # Sanity check: requested worked counts vs available seats in the rotation.
    op = build_operating_dates(cfg)
    total_sat = sum(1 for o in op if o.is_saturday)
    worked_d10 = sum(n.worked_d10() for n in cfg.nurses)  # stat days excluded
    sum_d5 = sum(n.target_d5 for n in cfg.nurses)
    sat_seats = sum(o.demand for o in op if o.is_saturday)
    wd_seats = sum(o.demand for o in op if not o.is_saturday)
    notes = []
    if worked_d10 < wd_seats:
        notes.append(
            f"Weekday: worked-D10 total {worked_d10} is below the {wd_seats} seats "
            f"needed — about {wd_seats - worked_d10} weekday shift(s) will be left "
            "blank (short-staffed)."
        )
    elif worked_d10 > wd_seats:
        notes.append(
            f"Weekday: worked-D10 total {worked_d10} exceeds {wd_seats} seats — "
            f"{worked_d10 - wd_seats} extra weekday shift(s) will be scheduled."
        )
    if sum_d5 < sat_seats:
        notes.append(
            f"Saturday: D5 total {sum_d5} is below the {sat_seats} seats "
            f"({total_sat} Saturdays × demand) — about {sat_seats - sum_d5} "
            "Saturday shift(s) will be left blank."
        )
    elif sum_d5 > sat_seats:
        notes.append(
            f"Saturday: D5 total {sum_d5} exceeds {sat_seats} seats — "
            f"{sum_d5 - sat_seats} extra Saturday shift(s) will be scheduled."
        )
    if notes:
        st.info("  \n".join(notes))


def _parse_dates(raw: str) -> list[str]:
    out = []
    for tok in raw.replace(";", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.append(datetime.strptime(tok, "%Y-%m-%d").date().isoformat())
        except ValueError:
            pass
    return out


# --- generation + results --------------------------------------------------


# Plain-language walkthrough of the solver, surfaced in the UI for transparency.
# The full technical spec lives in SOLVER.md; this mirrors it in order.
SOLVER_EXPLAINER = """
The schedule is built by a **constraint solver** (Google OR-Tools CP-SAT), not by
hand or by luck. Here is exactly what it does, in order — nothing else affects
the result.

**Stage 1 — Sanity checks (before anything is scheduled).**
It first rejects impossible inputs with a plain reason: empty/duplicate names,
more Saturdays demanded than anyone can supply, or shift counts that can't fit
the rules (e.g. a *work-weekly* line with too few D10s). If something can't work,
you get told *why* instead of a silent failure.

**Stage 2 — What it's allowed to decide.**
One yes/no choice per nurse, per operating day: *work it or not*. Two things are
locked out from the start so they can never happen — a nurse is never placed on a
date they're marked **unavailable**, and a **Mon-off (fixed)** line never gets a
Monday. Statutory holidays are handled separately: the solver picks which
holidays each line takes off (paid, not worked).

**Stage 3 — The hard rules (always true, in every option).**
These are guaranteed before any preference is considered:
- Each line works **exactly** its requested **D10** and **D5** counts.
- Every day is staffed to demand **where the roster can**; a genuinely
  uncoverable shift is left **blank and flagged**, never silently dropped.
- **≥ 1 Saturday per month** and **≤ 6 Saturdays per 9 weeks**, for everyone.
- **Job-share** partners never work the same day.
- Any ticked guarantee holds: **Work weekly** (one weekday shift every Mon–Fri
  week), **Fri before Sat**, **Mon off (fixed)**.

**Stage 4 — The preferences it then optimizes (soft).**
With the hard rules fixed, it *maximizes* a weighted scorecard — in priority
order: **fill coverage** → keep small lines **regularly active** → honour
**ticked preferences** → avoid **needless extra** staffing → **fairness**
(balanced weekdays, spaced-out Saturdays) → **clustering** → a **stable** weekly
pattern. A bigger weight wins when two goals tug against each other.

**Stage 5 — Why you get three options.**
It runs the whole solve **three times** with three different emphases —
**Preference-**, **Equity-** and **Cluster-maximizing**. All three obey every
hard rule and hit the exact counts; they differ **only** in *which days* fill
those counts. Pick the trade-off you like.

**A note on re-runs.** For speed the solver uses several workers at once, so
re-running the same inputs can give a slightly different — but equally valid and
equally count-exact — layout. Every hard rule still holds each time.

*Full technical specification, including the exact weights and constraint IDs, is
in **SOLVER.md** in the repository.*
"""


def generate_section():
    cfg = _cfg()
    st.header("Generate the schedule")

    with st.expander("How the solver builds your schedule — step by step"):
        st.markdown(SOLVER_EXPLAINER)

    # Compliance reminders, tucked away to keep the action area clean.
    lead_days = (cfg.start - date.today()).days
    if lead_days < 42:
        st.warning(
            f"25.05 posting: schedule starts in {lead_days} day(s) (< 6 weeks). "
            "The master schedule must be posted 6 weeks in advance."
        )
    with st.expander("Compliance reminders"):
        st.markdown(
            "- **Extended Work Day (25.11 / 26.01):** D10 is 10 h, beyond the 7.5 h "
            "normal daily shift — verify against your EWD Memorandum.\n"
            "- **Missed meals** are paid as overtime (Art. 27); payroll is not "
            "priced here.\n"
            "- **Off-duty consecutiveness (25.06(D))** can't be met on a "
            "Mon/Wed/Fri/Sat unit (isolated Tue/Thu closures) — written agreement "
            "recommended."
        )

    st.caption(
        "Nothing is scheduled until you press **GO** — three options "
        "(**preference-**, **equity-** and **cluster-maximizing**) are produced "
        "from the parameters and roster above."
    )
    if st.button("Generate three options", type="primary", width="stretch"):
        if cfg.start.weekday() != 4:
            st.error("Start date must be a Friday. Fix it in the sidebar.")
            return
        if not cfg.nurses:
            st.error("Add at least one nurse to the roster.")
            return
        with st.spinner("Solving three options…"):
            options = generate_schedules(cfg)
        st.session_state.options = options
        # Fresh working copies of each option's assignments (manual edits live
        # here); drop any prior edits / widget state when regenerating.
        st.session_state["work"] = {
            i: copy.deepcopy(o.assignments) for i, o in enumerate(options)
        }
        st.session_state["gridver"] = {i: 0 for i in range(len(options))}
        for k in list(st.session_state.keys()):
            if str(k).startswith(("swapA_", "swapB_", "pending_swap_",
                                  "pending_reset_", "grid_", "confirm_",
                                  "cancel_", "confirmreset_", "cancelreset_")):
                del st.session_state[k]
        st.session_state.pop("_wb_cache", None)

    options = st.session_state.get("options")
    if not options:
        st.info("Configure the parameters and roster above, then press **GO**.")
        return

    first = options[0]
    if not first.feasible and first.method == "none":
        st.error("**No feasible schedule** — nothing generated.")
        st.markdown("**Why, and how to fix it:**")
        for m in (first.binding_constraints or first.messages):
            st.markdown(f"- {m}")
        return

    if len(options) == 1 and options[0].method == "greedy":
        st.warning("No perfect schedule exists — a best-effort fallback is shown.")
        for m in options[0].messages:
            st.markdown(f"- {m}")

    # At-a-glance figures for the run.
    op = build_operating_dates(cfg)
    wd_shifts = sum(o.demand for o in op if not o.is_saturday)
    sat_shifts = sum(o.demand for o in op if o.is_saturday)
    n_stats = len(holidays_in_range(cfg.start, cfg.start + timedelta(weeks=cfg.weeks)))
    m = st.columns(5)
    m[0].metric("Options", len(options))
    m[1].metric("Weeks", cfg.weeks)
    m[2].metric("Weekday shifts", wd_shifts)
    m[3].metric("Saturday shifts", sat_shifts)
    m[4].metric("Stat holidays", n_stats)
    st.caption("Compare the options in the tabs below, then download your pick.")

    labels = [o.label or f"Option {chr(65 + i)}" for i, o in enumerate(options)]
    tabs = st.tabs(labels)
    for i, (tab, opt) in enumerate(zip(tabs, options)):
        with tab:
            _render_option(cfg, opt, i)


PROFILE_DESC = {
    "Preference-maximizing": "Arranges each nurse's required shifts to best "
        "satisfy the preferences ticked in the roster (off-days, non-consecutive "
        "Saturdays, clustered shifts).",
    "Equity-maximizing": "Spreads the work fairly — each nurse gets a balanced "
        "mix of Mondays / Wednesdays / Fridays and their Saturdays are evenly "
        "spaced through the rotation.",
    "Cluster-maximizing": "Groups each nurse's shifts together so their days off "
        "come in longer continuous blocks.",
}
GUARANTEES = (
    "Guaranteed in **every** option, before any preference is considered:\n\n"
    "- Each nurse works **exactly** their requested D10 / D5 counts.\n"
    "- Every operating day is staffed up to the available roster; any genuinely "
    "uncoverable shifts are left blank and flagged (never silently dropped).\n"
    "- Everyone works **≥ 1 Saturday per month** and **≤ 6 in any 9 weeks**.\n"
    "- Job-share partners never share a day; their combined FTE is ≤ 1.0.\n"
    "- No one is scheduled on a date they're marked unavailable.\n\n"
    "The three options differ **only** in how those fixed shifts are arranged "
    "across the calendar — never in how many each nurse works."
)


def _render_option(cfg: Config, opt, idx: int):
    """Render one option: read-only grid, swap/edit controls, live validation."""
    operating = opt.operating or build_operating_dates(cfg)
    work = st.session_state.setdefault("work", {})
    if idx not in work:
        work[idx] = copy.deepcopy(opt.assignments)
    assignments = work[idx]  # mutated in place by the controls below

    # De-blackboxing: explain what this option optimized and what's guaranteed.
    with st.expander("How this schedule was built"):
        st.markdown("**" + (opt.label or "This option") + ".** "
                    + PROFILE_DESC.get(opt.label, ""))
        st.markdown(GUARANTEES)

    gridver = st.session_state.setdefault("gridver", {})
    gridver.setdefault(idx, 0)

    status_slot = st.container()  # filled after edits are applied

    # --- Editable grid: click a cell to change it -------------------------
    st.caption(
        "**Click a cell** to change it — pick the shift code to staff it, or "
        "blank for off. (Dragging isn't supported; the name column is pinned.) "
        "Or use **Swap** below to trade two shifts in one step. Everything "
        "re-checks live."
    )
    base_df = _grid_df(cfg, assignments, operating)
    col_cfg = {"Nurse": st.column_config.TextColumn(
        "Nurse", disabled=True, pinned=True, width="small")}
    for od in operating:
        # ST (statutory holiday off) is only offered on weekdays.
        opts = ["", od.shift.code, "LV"] + ([] if od.is_saturday else ["ST"])
        col_cfg[_label(od)] = st.column_config.SelectboxColumn(
            _label(od), options=opts, width="small",
        )
    edited = st.data_editor(
        base_df, hide_index=True, num_rows="fixed", width="stretch",
        column_config=col_cfg, key=f"grid_{idx}_{gridver[idx]}",
    )
    # Persist cell edits into the working copy.
    work[idx] = _assignments_from_grid(edited, cfg, operating)
    assignments = work[idx]

    # --- Swap two shifts (one action, preserves counts) -------------------
    with st.expander("Swap two shifts / reset"):
        shifts = _worked_shifts(assignments, operating)
        labels = [s[0] for s in shifts]
        b_default = None
        if labels:
            od_by_iso = {od.iso: od for od in operating}
            a0 = shifts[0]
            a_code = od_by_iso[a0[2]].shift.code

            def _valid_b(s):
                return (od_by_iso[s[2]].shift.code == a_code and s[2] != a0[2]
                        and s[1] != a0[1] and a0[2] not in assignments.get(s[1], {})
                        and s[2] not in assignments.get(a0[1], {}))

            b_default = next((k for k, s in enumerate(shifts) if _valid_b(s)),
                             min(1, len(labels) - 1))
        c1, c2, c3 = st.columns([5, 5, 2])
        a = c1.selectbox("Shift A", labels, key=f"swapA_{idx}",
                         index=0 if labels else None)
        b = c2.selectbox("Shift B", labels, key=f"swapB_{idx}", index=b_default)
        c3.markdown("<div style='height:1.7em'></div>", unsafe_allow_html=True)
        pend_key = f"pending_swap_{idx}"
        if c3.button("Swap", key=f"swapbtn_{idx}", width="stretch") and labels:
            A = next(s for s in shifts if s[0] == a)
            B = next(s for s in shifts if s[0] == b)
            unavail = {n.name: set(n.unavailable_dates) for n in cfg.nurses}
            err = _swap_error(assignments, (A[1], A[2]), (B[1], B[2]), operating)
            if not err and B[2] in unavail.get(A[1], set()):
                err = f"{A[1]} is unavailable on that day — can't swap onto it."
            elif not err and A[2] in unavail.get(B[1], set()):
                err = f"{B[1]} is unavailable on that day — can't swap onto it."
            if err:
                st.warning(err)
            else:
                st.session_state[pend_key] = (A, B)

        pending = st.session_state.get(pend_key)
        if pending:
            A, B = pending
            st.info(f"Swap **{A[0]}** with **{B[0]}**? "
                    "The two nurses will trade these days.")
            # Preview the swap's compliance impact BEFORE applying it.
            trial = copy.deepcopy(assignments)
            _do_swap(trial, (A[1], A[2]), (B[1], B[2]), operating)
            trep = validate(cfg, replace(opt, assignments=trial))
            tfail = [r.rule for r in trep.rules if r.status == "FAIL"]
            twarn = [r.rule for r in trep.rules if r.status == "WARN"
                     and not r.rule.startswith("Daily coverage")]
            if tfail:
                st.error("After this swap it would **break compliance**: "
                         + "; ".join(tfail))
            elif twarn or trep.unfilled_shifts:
                bits = ([f"{trep.unfilled_shifts} blank shift(s)"]
                        if trep.unfilled_shifts else []) + twarn
                st.warning("After this swap, to review: " + "; ".join(bits))
            else:
                st.success("After this swap the schedule stays fully compliant.")
            cc1, cc2 = st.columns(2)
            if cc1.button("Confirm swap", key=f"confirm_{idx}",
                          type="primary", width="stretch"):
                msg = _do_swap(assignments, (A[1], A[2]), (B[1], B[2]), operating)
                st.session_state.pop(pend_key, None)
                gridver[idx] += 1  # remount the grid with the swapped data
                if msg:
                    st.warning(msg)
            if cc2.button("Cancel", key=f"cancel_{idx}", width="stretch"):
                st.session_state.pop(pend_key, None)

        # Reset to generated -- with a confirmation (it discards manual edits).
        reset_key = f"pending_reset_{idx}"
        if st.button("Reset to generated", key=f"reset_{idx}", width="stretch"):
            st.session_state[reset_key] = True
        if st.session_state.get(reset_key):
            st.warning("Discard **all manual edits** and restore the generated "
                       "schedule for this option?")
            rc1, rc2 = st.columns(2)
            if rc1.button("Confirm reset", key=f"confirmreset_{idx}",
                          type="primary", width="stretch"):
                work[idx] = copy.deepcopy(opt.assignments)
                assignments = work[idx]
                gridver[idx] += 1
                st.session_state.pop(reset_key, None)
            if rc2.button("Keep edits", key=f"cancelreset_{idx}", width="stretch"):
                st.session_state.pop(reset_key, None)

    # --- Re-validate the (possibly edited) schedule and draw the status ---
    result = replace(opt, assignments=assignments)
    report = validate(cfg, result)

    with status_slot:
        fails = [r for r in report.rules if r.status == "FAIL"]
        warns = [r for r in report.rules if r.status == "WARN"]
        if report.unfilled_shifts:
            st.warning(f"{report.unfilled_shifts} shift(s) left blank — not enough "
                       "staff to cover every day. Add staff/shifts to fill them.")
        other_warns = [r for r in warns if not r.rule.startswith("Daily coverage")]
        if fails:
            st.error(f"{len(fails)} issue(s) to fix: "
                     + "; ".join(r.rule for r in fails))
        elif other_warns:
            st.warning(f"{len(other_warns)} to review: "
                       + "; ".join(r.rule for r in other_warns))
        elif not report.unfilled_shifts:
            st.success("All checks pass — this schedule is compliant.")

    # Per-nurse summary.
    st.markdown("**Per-nurse summary**")
    sdf = pd.DataFrame([{
        "Nurse": s.name,
        "D10 (sched/target)": f"{s.scheduled_d10}/{s.target_d10}",
        "D5 (sched/target)": f"{s.scheduled_d5}/{s.target_d5}",
        "Counts met": "Yes" if (s.scheduled_d10 == s.target_d10
                              and s.scheduled_d5 == s.target_d5) else "No",
        "FTE": s.scheduled_fte, "Total hrs": s.total_hours,
        "Saturdays": f"{s.saturdays_worked}/{s.saturdays_in_period}",
        "Worst 9-wk Sat": s.worst_9wk_sat,
    } for s in report.nurse_summaries])
    st.dataframe(sdf, hide_index=True, width="stretch")

    # Full compliance detail (collapsed by default to keep the view clean).
    with st.expander("Full compliance report"):
        cdf = pd.DataFrame([{
            "Status": r.status, "Rule": r.rule, "Article": r.citation,
            "Detail": r.detail,
        } for r in report.rules])
        st.dataframe(cdf, hide_index=True, width="stretch")

    # Download reflects manual edits; rebuilt only when assignments change.
    data = _cached_workbook_bytes(idx, cfg, result, report, assignments)
    fn = output_filename(cfg).replace(".xlsx", f"_{(opt.label or 'A').split()[-1]}.xlsx")
    st.download_button(
        f"Download {opt.label or 'option'} (.xlsx)",
        data=data, file_name=fn,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary", width="stretch", key=f"dl_{idx}",
    )


def _worked_shifts(assignments: dict, operating) -> list:
    """List current worked shifts as (label, nurse, iso), chronologically."""
    od_by_iso = {od.iso: od for od in operating}
    items = []
    for name, days in assignments.items():
        for iso, code in days.items():
            od = od_by_iso.get(iso)
            if od and is_worked(code):  # ST/LV are not swappable shifts
                items.append((od.d, f"{name} — {od.d.strftime('%a %d-%b')} ({code})",
                              name, iso))
    items.sort(key=lambda t: (t[0], t[1]))
    return [(lbl, name, iso) for _d, lbl, name, iso in items]


def _assignments_from_grid(df: pd.DataFrame, cfg: Config, operating) -> dict:
    """Rebuild the assignments dict from the edited grid (a cell holding the
    day's shift code = working; blank / LV = off)."""
    lbl_to_od = {_label(od): od for od in operating}
    valid = {n.name for n in cfg.nurses}
    assignments = {n.name: {} for n in cfg.nurses}
    for _, row in df.iterrows():
        name = str(row.get("Nurse") or "").strip()
        if name not in valid:
            continue
        for lbl, od in lbl_to_od.items():
            v = str(row.get(lbl) or "").strip().upper()
            if v == "ST":
                assignments[name][od.iso] = "ST"
            elif v and v != "LV":
                assignments[name][od.iso] = od.shift.code
    return assignments


def _swap_error(assignments: dict, A, B, operating) -> str:
    """Validate a swap without mutating; return a message or '' if it's clean."""
    (nA, iA), (nB, iB) = A, B
    od_by_iso = {od.iso: od for od in operating}
    if nA == nB:
        return "Pick shifts from two different nurses."
    if iA == iB:
        return "Pick two different days."
    if iA not in assignments.get(nA, {}):
        return "Shift A is no longer in the schedule — re-pick it."
    if iB not in assignments.get(nB, {}):
        return "Shift B is no longer in the schedule — re-pick it."
    if od_by_iso[iA].shift.code != od_by_iso[iB].shift.code:
        return ("Swap two shifts of the same type (both weekday D10 or both "
                "Saturday D5) so the counts stay intact.")
    if iB in assignments.get(nA, {}):
        return f"{nA} already works that day — nothing to swap."
    if iA in assignments.get(nB, {}):
        return f"{nB} already works that day — nothing to swap."
    return ""


def _do_swap(assignments: dict, A, B, operating) -> str:
    """Swap two assignments: the two nurses trade days. Returns a warning or ''."""
    err = _swap_error(assignments, A, B, operating)
    if err:
        return err
    (nA, iA), (nB, iB) = A, B
    od_by_iso = {od.iso: od for od in operating}
    assignments[nA].pop(iA, None)
    assignments[nB].pop(iB, None)
    assignments[nA][iB] = od_by_iso[iB].shift.code
    assignments[nB][iA] = od_by_iso[iA].shift.code
    return ""


def _style_grid(df: pd.DataFrame):
    """Subtle on-screen shading for worked / leave cells (clean, low-contrast)."""
    def shade(v):
        if v in ("D10", "D5"):
            return "background-color:#e8f1f1"   # faint teal tint
        if v == "LV":
            return "background-color:#f2f2f2;color:#9aa0a6"
        return ""
    return df.style.map(shade, subset=[c for c in df.columns if c != "Nurse"])


def _cached_workbook_bytes(idx, cfg, result, report, assignments):
    sig = tuple(sorted(
        (name, tuple(sorted(days.items()))) for name, days in assignments.items()
    ))
    cache = st.session_state.setdefault("_wb_cache", {})
    if cache.get(f"sig_{idx}") != sig:
        cache[f"sig_{idx}"] = sig
        cache[f"bytes_{idx}"] = workbook_bytes(cfg, result, report)
    return cache[f"bytes_{idx}"]


def _label(od) -> str:
    return f"W{od.week_index + 1} {od.d.strftime('%a %d-%b')}"


def _grid_df(cfg: Config, assignments: dict, operating) -> pd.DataFrame:
    """One row per nurse; a 'Nurse' name column then one column per operating day."""
    unavail = {n.name: set(n.unavailable_dates) for n in cfg.nurses}
    rows = []
    for n in cfg.nurses:
        row = {"Nurse": n.name}
        for od in operating:
            code = assignments.get(n.name, {}).get(od.iso)
            if code:
                row[_label(od)] = code
            elif od.iso in unavail[n.name]:
                row[_label(od)] = "LV"
            else:
                row[_label(od)] = ""
        rows.append(row)
    cols = ["Nurse"] + [_label(od) for od in operating]
    return pd.DataFrame(rows, columns=cols)


# --- main ------------------------------------------------------------------


def main():
    _init_state()
    _inject_style()
    _hero()
    with st.expander("How this works", expanded=False):
        st.markdown(
            "- The unit runs **Fri / Sat / Mon / Wed** each week (rotation starts "
            "Friday). Saturdays are **D5** (5 h); weekdays are **D10** (10 h).\n"
            "- In the **roster**, give each nurse their **D10**, **D5** and "
            "**stat-shift** counts. The generator hits those counts while keeping "
            "the schedule compliant.\n"
            "- Press **GO** for **three options** — preference-, equity- and "
            "cluster-maximizing. Open a tab, review the status banner, optionally "
            "**edit** the grid by hand, then **download** the Excel.\n"
            "- Seniority isn't used to build the schedule — lines are picked by "
            "seniority afterward.\n"
            "- The Excel prints clean in black-and-white; colour flags only "
            "problems."
        )
    sidebar()
    st.divider()
    roster_editor()
    st.divider()
    generate_section()


if __name__ == "__main__":
    main()
