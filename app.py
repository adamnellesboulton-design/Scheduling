"""Streamlit UI for the Pediatric Dialysis Unit Scheduler (Section 10).

Run with:  streamlit run app.py
"""

from __future__ import annotations

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

STATUS_EMOJI = {"PASS": "✅", "FAIL": "❌", "INFO": "ℹ️", "WARN": "⚠️"}


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

    # Load / save JSON config.
    with st.sidebar.expander("Load / save config (JSON)", expanded=False):
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
    st.sidebar.subheader("Daily staffing demand (RNs)")
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
    st.subheader("Nurse roster")

    st.caption(
        "Set each line's number of **10-hour weekday shifts (D10, 0–40)**, "
        "**5-hour Saturday shifts (D5, 1–10)** and **stat shifts (0–10)** over the "
        "rotation. The counts are the primary target; stat shifts are paid "
        "statutory-holiday days (BCNU Art. 17) that reduce worked D10 shifts. "
        "**FTE** is derived (read-only). Preferences are soft and resolved by "
        "**seniority**. Same **Job share** label = two lines never work the same "
        "day. **Everyone works Saturdays** (≥1 per month)."
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
            "seniority_rank": n.seniority_rank,
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
            "seniority_rank": st.column_config.NumberColumn(
                "Seniority", min_value=1, step=1,
                help="1 = most senior; resolves preference conflicts (25.03 ethos)",
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
            seniority_rank=int(r["seniority_rank"]) if pd.notna(r["seniority_rank"]) else 1,
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
        st.error(f"⛔ Duplicate nurse name(s): {', '.join(dupes)}. Names must be unique.")

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
        st.info("ℹ️ " + "  \n".join(notes))


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

    # Compliance banners (non-blocking).
    st.warning(
        "**Extended Work Day:** D10 (10.0h) exceeds the 7.5h normal daily full "
        "shift (26.01). Verify shift lengths against your Extended Work Day "
        "Memorandum terms (25.11).",
        icon="⚠️",
    )
    lead_days = (cfg.start - date.today()).days
    if lead_days < 42:
        st.warning(
            f"**25.05 posting:** schedule starts in {lead_days} day(s) (< 6 weeks). "
            "The master schedule must be posted 6 weeks in advance.",
            icon="⚠️",
        )

    st.caption(
        "Nothing is scheduled until you press **GO**. Enter all parameters and the "
        "roster first, then click. Three best-fit options (A/B/C) are produced."
    )
    if st.button("🟢 GO — generate 3 options", type="primary", width="stretch"):
        if cfg.start.weekday() != 4:
            st.error("Start date must be a Friday. Fix it in the sidebar.")
            return
        if not cfg.nurses:
            st.error("Add at least one nurse to the roster.")
            return
        with st.spinner("Solving (three options)…"):
            options = generate_schedules(cfg)
        st.session_state.options = options
        # Drop any prior manual edits when regenerating.
        for k in list(st.session_state.keys()):
            if str(k).startswith("grid_"):
                del st.session_state[k]

    options = st.session_state.get("options")
    if not options:
        st.info("Configure the parameters and roster above, then press GO.")
        return

    first = options[0]
    if not first.feasible and first.method == "none":
        st.error("❌ **No feasible schedule** — generation aborted.", icon="❌")
        for m in first.messages:
            st.markdown(f"- {m}")
        if first.binding_constraints:
            st.markdown("**Diagnostic (which requirement is unsatisfiable):**")
            for b in first.binding_constraints:
                st.markdown(f"> {b}")
        return

    if len(options) == 1 and options[0].method == "greedy":
        st.warning("⚠️ CP-SAT infeasible — greedy fallback used (see diagnostics).")
        for m in options[0].messages:
            st.markdown(f"- {m}")

    labels = [o.label or f"Option {chr(65 + i)}" for i, o in enumerate(options)]
    tabs = st.tabs(labels)
    for i, (tab, opt) in enumerate(zip(tabs, options)):
        with tab:
            _render_option(cfg, opt, i)


def _render_option(cfg: Config, opt, idx: int):
    """Render one option: editable grid -> live re-validation -> download."""
    operating = opt.operating or build_operating_dates(cfg)
    st.caption(
        "Edit the grid to move shifts around — pick a cell's value (the day's "
        "shift code, LV, or blank). Compliance and the download update live."
    )

    base_df = _grid_df(cfg, opt.assignments, operating)
    col_cfg = {}
    for od in operating:
        lbl = _label(od)
        col_cfg[lbl] = st.column_config.SelectboxColumn(
            lbl, options=["", od.shift.code, "LV"], width="small",
        )
    edited = st.data_editor(
        base_df, width="stretch", column_config=col_cfg, key=f"grid_{idx}",
    )

    # Reconstruct assignments from the (possibly edited) grid and re-validate.
    assignments = _assignments_from_grid(edited, cfg, operating)
    result = replace(opt, assignments=assignments)
    report = validate(cfg, result)

    # Compliance badges (issues first).
    st.markdown("**Compliance**")
    issues = [r for r in report.rules if r.status in ("FAIL", "WARN")]
    if not issues:
        st.success("All checks pass.")
    bcols = st.columns(2)
    for j, rule in enumerate(report.rules):
        with bcols[j % 2]:
            st.markdown(
                f"{STATUS_EMOJI.get(rule.status, '')} **{rule.status}** — "
                f"{rule.rule}  \n<small>{rule.citation}: {rule.detail}</small>",
                unsafe_allow_html=True,
            )

    # Per-nurse summary.
    sdf = pd.DataFrame([{
        "Nurse": s.name,
        "D10": f"{s.scheduled_d10}/{s.target_d10}",
        "D5": f"{s.scheduled_d5}/{s.target_d5}",
        "Counts met": "yes" if (s.scheduled_d10 == s.target_d10
                                and s.scheduled_d5 == s.target_d5) else "NO",
        "FTE": s.scheduled_fte, "Total hrs": s.total_hours,
        "Sats": f"{s.saturdays_worked}/{s.saturdays_in_period}",
        "Worst 9-wk Sat": s.worst_9wk_sat,
    } for s in report.nurse_summaries])
    st.dataframe(sdf, hide_index=True, width="stretch")

    # Download (reflects manual edits). Rebuild the workbook only when this
    # option's assignments actually change, so the three tabs don't each rebuild
    # an .xlsx on every rerun.
    data = _cached_workbook_bytes(idx, cfg, result, report, assignments)
    fn = output_filename(cfg).replace(".xlsx", f"_{(opt.label or 'A').split()[-1]}.xlsx")
    st.download_button(
        f"⬇️ Download {opt.label or 'option'} (.xlsx)",
        data=data, file_name=fn,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary", width="stretch", key=f"dl_{idx}",
    )


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
    unavail = {n.name: set(n.unavailable_dates) for n in cfg.nurses}
    index = [n.name for n in cfg.nurses]
    data = {}
    for od in operating:
        lbl = _label(od)
        col = []
        for n in cfg.nurses:
            code = assignments.get(n.name, {}).get(od.iso)
            if code:
                col.append(code)
            elif od.iso in unavail[n.name]:
                col.append("LV")
            else:
                col.append("")
        data[lbl] = col
    return pd.DataFrame(data, index=index)


def _assignments_from_grid(df: pd.DataFrame, cfg: Config, operating) -> dict:
    lbl_to_od = {_label(od): od for od in operating}
    assignments = {n.name: {} for n in cfg.nurses}
    for lbl in df.columns:
        od = lbl_to_od.get(lbl)
        if od is None:
            continue
        for name in df.index:
            v = str(df.at[name, lbl] or "").strip().upper()
            if v and v != "LV":  # any work code -> assign the day's shift
                assignments.setdefault(name, {})[od.iso] = od.shift.code
    return assignments


# --- main ------------------------------------------------------------------


def main():
    _init_state()
    st.title("🩺 Pediatric Dialysis Unit Scheduler")
    st.caption(
        "BC Children's Hospital hemodialysis unit · BCNU Provincial Collective "
        "Agreement (Art. 25, 26). Generates a compliant master schedule and "
        "exports a formatted Excel workbook."
    )
    sidebar()
    roster_editor()
    st.divider()
    generate_section()


if __name__ == "__main__":
    main()
