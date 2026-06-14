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
from dialysis_scheduler.model import build_operating_dates
from dialysis_scheduler.holidays import holidays_in_range
from dialysis_scheduler.scheduler import generate_schedule, generate_schedules
from dialysis_scheduler.validator import validate
from dialysis_scheduler.excel_export import workbook_bytes, output_filename

st.set_page_config(page_title="Dialysis Unit Scheduler", layout="wide")


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
        "Set each line's number of **10-hour weekday shifts (D10, 0–40)**, "
        "**5-hour Saturday shifts (D5, 1–10)** and **stat shifts (0–10)** over the "
        "rotation. The counts are the primary target; stat shifts are paid "
        "statutory-holiday days (BCNU Art. 17) that reduce worked D10 shifts. "
        "**FTE** is derived (read-only). Preferences are honoured in the "
        "preference-maximizing option. Same **Job share** label = two lines never "
        "work the same day. **Everyone works Saturdays** (≥1 per month). "
        "Seniority is not used — lines are picked by seniority afterward."
    )

    d10p, satp = cfg.d10_paid(), cfg.sat_paid()
    denom = cfg.weekly_full_time_hours * cfg.weeks

    rows = []
    for n in cfg.nurses:
        hrs = n.target_d10 * d10p + n.target_d5 * satp
        rows.append({
            "name": n.name,
            "d10": int(n.target_d10),
            "d5": int(n.target_d5),
            "stat": int(n.stat_days),
            "fte": round(hrs / denom, 3) if denom else 0.0,
            "job_share": n.job_share_group,
            "pref_nonconsec_sat": n.pref_nonconsec_sat,
            "pref_clustered": n.pref_clustered,
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
                "D10 shifts", min_value=0, max_value=40, step=1,
                help="Number of 10-hour weekday shifts over the rotation (0–40).",
            ),
            "d5": st.column_config.NumberColumn(
                "D5 shifts", min_value=1, max_value=10, step=1,
                help="Number of 5-hour Saturday shifts over the rotation (1–10). "
                     "Everyone works some Saturdays.",
            ),
            "stat": st.column_config.NumberColumn(
                "Stat shifts", min_value=0, max_value=10, step=1,
                help="Paid statutory-holiday days (Art. 17); each reduces worked "
                     "D10 shifts by one.",
            ),
            "fte": st.column_config.NumberColumn(
                "FTE (derived)", disabled=True, format="%.3f",
                help="Computed from the shift counts; not directly editable.",
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
            "pref_off_mon": st.column_config.CheckboxColumn(
                "Off Mon", help="Prefer Mondays off"
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
            pref_off_mon=bool(r["pref_off_mon"]),
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
    if sum_d5 != sat_seats:
        notes.append(
            f"Saturday: requested D5 total = {sum_d5} but the rotation has "
            f"{sat_seats} Saturday seats ({total_sat} Saturdays × demand). "
            "These must match for every line to hit its D5 count."
        )
    if worked_d10 < wd_seats:
        notes.append(
            f"Weekday: worked D10 total = {worked_d10} (after stat days) is below "
            f"the {wd_seats} weekday seats needed — coverage forces some lines to "
            "work above their target / stat days may not all be granted."
        )
    elif worked_d10 > wd_seats:
        notes.append(
            f"Weekday: worked D10 total = {worked_d10} exceeds {wd_seats} weekday "
            f"seats — {worked_d10 - wd_seats} extra weekday shift(s) will be scheduled."
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


def generate_section():
    cfg = _cfg()
    st.header("Generate the schedule")

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
        for k in list(st.session_state.keys()):
            if str(k).startswith(("swapA_", "swapB_", "setn_", "setd_", "setv_")):
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
    "- Every operating day is staffed — Saturday exactly, weekdays at least the "
    "required number.\n"
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

    # Visual slots are reserved first so the controls (processed below) can
    # mutate the schedule before the grid / summary are drawn into the slots.
    status_slot = st.container()
    grid_slot = st.container()

    # --- Adjust controls -------------------------------------------------
    with st.expander("Adjust this schedule (swap or edit shifts)"):
        shifts = _worked_shifts(assignments, operating)
        labels = [s[0] for s in shifts]
        st.caption("Swap two shifts — the two nurses trade days. Counts and "
                   "coverage stay intact, and everything re-checks below.")
        c1, c2, c3 = st.columns([5, 5, 2])
        a = c1.selectbox("Shift A", labels, key=f"swapA_{idx}",
                         index=0 if labels else None)
        b = c2.selectbox("Shift B", labels, key=f"swapB_{idx}",
                         index=min(1, len(labels) - 1) if labels else None)
        c3.markdown("<div style='height:1.7em'></div>", unsafe_allow_html=True)
        if c3.button("Swap", key=f"swapbtn_{idx}", width="stretch") and labels:
            A = next(s for s in shifts if s[0] == a)
            B = next(s for s in shifts if s[0] == b)
            msg = _do_swap(assignments, (A[1], A[2]), (B[1], B[2]), operating)
            if msg:
                st.warning(msg)

        st.divider()
        st.caption("Or set one cell directly.")
        d1, d2, d3, d4 = st.columns([4, 4, 3, 2])
        nm = d1.selectbox("Nurse", [n.name for n in cfg.nurses], key=f"setn_{idx}")
        od_labels = {_label(od): od for od in operating}
        dl = d2.selectbox("Day", list(od_labels), key=f"setd_{idx}")
        val = d3.selectbox("Set to", ["Working", "Off"], key=f"setv_{idx}")
        d4.markdown("<div style='height:1.7em'></div>", unsafe_allow_html=True)
        if d4.button("Apply", key=f"setbtn_{idx}", width="stretch"):
            od = od_labels[dl]
            if val == "Working":
                assignments[nm][od.iso] = od.shift.code
            else:
                assignments[nm].pop(od.iso, None)

        if st.button("Reset to generated", key=f"reset_{idx}"):
            work[idx] = copy.deepcopy(opt.assignments)
            assignments = work[idx]

    # --- Re-validate the (possibly edited) schedule and draw the slots ----
    result = replace(opt, assignments=assignments)
    report = validate(cfg, result)

    with status_slot:
        fails = [r for r in report.rules if r.status == "FAIL"]
        warns = [r for r in report.rules if r.status == "WARN"]
        if fails:
            st.error(f"{len(fails)} issue(s) to fix: "
                     + "; ".join(r.rule for r in fails))
        elif warns:
            st.warning(f"{len(warns)} to review: "
                       + "; ".join(r.rule for r in warns))
        else:
            st.success("All checks pass — this schedule is compliant.")

    with grid_slot:
        st.dataframe(
            _style_grid(_grid_df(cfg, assignments, operating)),
            hide_index=True, width="stretch",
            column_config={"Nurse": st.column_config.TextColumn(pinned=True)},
        )

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
            if od:
                items.append((od.d, f"{name} — {od.d.strftime('%a %d-%b')} ({code})",
                              name, iso))
    items.sort(key=lambda t: (t[0], t[1]))
    return [(lbl, name, iso) for _d, lbl, name, iso in items]


def _do_swap(assignments: dict, A, B, operating) -> str:
    """Swap two assignments: the two nurses trade days. Returns a warning or ''."""
    (nA, iA), (nB, iB) = A, B
    if nA == nB:
        return "Pick shifts from two different nurses."
    if iA == iB:
        return "Pick two different days."
    if iB in assignments.get(nA, {}):
        return f"{nA} already works that day — nothing to swap."
    if iA in assignments.get(nB, {}):
        return f"{nB} already works that day — nothing to swap."
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
    st.title("Pediatric Dialysis Unit Scheduler")
    st.markdown(
        "##### BC Children's Hospital · Hemodialysis · "
        "BCNU Provincial Collective Agreement (Art. 25–26)"
    )
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
