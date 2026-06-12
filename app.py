"""Streamlit UI for the Pediatric Dialysis Unit Scheduler (Section 10).

Run with:  streamlit run app.py
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

from dialysis_scheduler.config import (
    Config,
    Nurse,
    DEFAULT_CONFIG_PATH,
    default_config,
)
from dialysis_scheduler.fte import achievable_fte_menu, max_achievable_fte
from dialysis_scheduler.model import build_operating_dates
from dialysis_scheduler.scheduler import generate_schedule
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
    start = st.sidebar.date_input("Start date (must be a Monday)", value=cfg.start)
    if start.weekday() != 0:
        st.sidebar.error("Start date must be a Monday (Section 3.1).")
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

    # Default FTE flex (per-line overrides live in the roster table).
    st.sidebar.subheader("Default FTE flex")
    cfg.fte_tolerance = st.sidebar.slider(
        "± default flex (averaged over period, 26.01)",
        min_value=0.02, max_value=0.20, value=float(cfg.fte_tolerance), step=0.01,
        help="Applied to any line that doesn't set its own flex in the roster.",
    )


# --- roster editor ---------------------------------------------------------


def roster_editor():
    cfg = _cfg()
    st.subheader("Nurse roster")

    menu = achievable_fte_menu(cfg)
    max_ach = max_achievable_fte(cfg)
    st.caption(
        "Enter each nurse's **target FTE** (their contracted line). The "
        f"generator schedules each nurse within **±{cfg.fte_tolerance:.2f}** of it, "
        "averaged over the rotation. The achievable-pattern menu below is a guide "
        f"— the most a single line can reach on unit operating hours is "
        f"**{max_ach:.2f}** (full-time 1.0 is unreachable here)."
    )
    with st.expander("Achievable FTE patterns (for reference)"):
        st.dataframe(
            pd.DataFrame(
                [{"FTE": o.fte, "Pattern": o.label.split(" - ", 1)[1]} for o in menu]
            ),
            hide_index=True, width="stretch",
        )

    st.caption(
        "Per-line **preferences** are soft and resolved by **seniority** when they "
        "conflict (rank 1 wins). **FTE flex** defaults to the sidebar value but can "
        "be overridden per line. Give two lines the same **Job share** label to "
        "stop them ever working the same day. Saturdays are balanced by FTE first."
    )

    rows = []
    for n in cfg.nurses:
        rows.append({
            "name": n.name,
            "target_fte": float(n.target_fte),
            "fte_flex": float(n.fte_tolerance) if n.fte_tolerance is not None
            else float(cfg.fte_tolerance),
            "job_share": n.job_share_group,
            "fixed_saturdays_off": n.fixed_saturdays_off,
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
            "target_fte": st.column_config.NumberColumn(
                "Target FTE", min_value=0.0, max_value=1.0, step=0.01,
                format="%.2f",
                help="The nurse's contracted FTE; scheduled within its flex.",
            ),
            "fte_flex": st.column_config.NumberColumn(
                "FTE flex ±", min_value=0.0, max_value=0.30, step=0.01,
                format="%.2f",
                help="Allowed deviation from target FTE for this line "
                     "(defaults to the sidebar value).",
            ),
            "job_share": st.column_config.SelectboxColumn(
                "Job share", options=["", "A", "B", "C", "D"],
                help="Put the SAME label on two lines to job-share them — "
                     "they will never be scheduled on the same day.",
            ),
            "fixed_saturdays_off": st.column_config.CheckboxColumn(
                "Fixed Sat off", help="25.06(B)/(E) waiver — never assigned Saturdays"
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
        try:
            tf = round(float(r["target_fte"]), 2)
        except (TypeError, ValueError):
            tf = 0.0
        try:
            flex = round(float(r["fte_flex"]), 2)
        except (TypeError, ValueError):
            flex = float(cfg.fte_tolerance)
        new_nurses.append(Nurse(
            name=name,
            target_fte=tf,
            fixed_saturdays_off=bool(r["fixed_saturdays_off"]),
            seniority_rank=int(r["seniority_rank"]) if pd.notna(r["seniority_rank"]) else 1,
            unavailable_dates=dates,
            fte_tolerance=flex,
            job_share_group=str(r.get("job_share") or "").strip(),
            pref_nonconsec_sat=bool(r["pref_nonconsec_sat"]),
            pref_clustered=bool(r["pref_clustered"]),
            pref_off_mon=bool(r["pref_off_mon"]),
            pref_off_wed=bool(r["pref_off_wed"]),
            pref_off_fri=bool(r["pref_off_fri"]),
        ))
    cfg.nurses = new_nurses

    # Warn about targets the unit's hours cannot reach within the line's flex.
    unreachable = [
        n.name for n in cfg.nurses
        if n.target_fte - n.tolerance(cfg.fte_tolerance) > max_ach + 1e-9
    ]
    if unreachable:
        st.warning(
            f"⚠️ Target FTE unreachable on unit hours for: {', '.join(unreachable)}. "
            f"The most a single line can reach is {max_ach:.2f} (full-time 1.0 is "
            "not attainable on Mon/Wed/Fri/Sat hours). These nurses will be "
            "scheduled as close as possible.",
        )

    # Show each nurse's nearest achievable pattern as a hint.
    if cfg.nurses and menu:
        def nearest(fte):
            o = min(menu, key=lambda o: abs(o.fte - fte))
            return o.label.split(" - ", 1)[1]
        chips = " · ".join(
            f"**{n.name}** {n.target_fte:.2f} (~{nearest(n.target_fte)})"
            for n in cfg.nurses
        )
        st.caption(chips)


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

    if st.button("⚙️ Generate schedule", type="primary", width="stretch"):
        if cfg.start.weekday() != 0:
            st.error("Start date must be a Monday. Fix it in the sidebar.")
            return
        if not cfg.nurses:
            st.error("Add at least one nurse to the roster.")
            return
        with st.spinner("Solving (CP-SAT, 30s limit)…"):
            result = generate_schedule(cfg)
            report = validate(cfg, result) if result.operating else None
        st.session_state.result = result
        st.session_state.report = report

    result = st.session_state.get("result")
    report = st.session_state.get("report")
    if result is None:
        return

    if not result.feasible and result.method == "none":
        # Section 10.4: red banner, no file.
        st.error("❌ **No feasible schedule** — generation aborted.", icon="❌")
        for m in result.messages:
            st.markdown(f"- {m}")
        if result.binding_constraints:
            st.markdown("**Diagnostic (which requirement is unsatisfiable):**")
            for b in result.binding_constraints:
                st.markdown(f"> {b}")
        return

    # Feasible (possibly greedy / relaxed).
    method_msg = {
        "cp-sat": "✅ Solved with CP-SAT at base FTE tolerance.",
        "cp-sat-relaxed": "⚠️ Solved with CP-SAT after relaxing FTE tolerance to ±0.13.",
        "greedy": "⚠️ CP-SAT infeasible — greedy fallback used.",
    }.get(result.method, result.status)
    (st.success if result.method == "cp-sat" else st.warning)(method_msg)
    for m in result.messages:
        st.markdown(f"- {m}")
    if result.binding_constraints and result.method == "greedy":
        st.markdown("**Binding constraints / diagnostics:**")
        for b in result.binding_constraints:
            st.markdown(f"> {b}")

    # Compliance badges.
    if report:
        st.markdown("#### Compliance summary")
        bcols = st.columns(min(4, len(report.rules)))
        for i, rule in enumerate(report.rules):
            with bcols[i % len(bcols)]:
                st.markdown(
                    f"{STATUS_EMOJI.get(rule.status, '')} **{rule.status}** — "
                    f"{rule.rule}  \n<small>{rule.citation}</small>",
                    unsafe_allow_html=True,
                )

    # Grid preview.
    st.markdown("#### Schedule preview")
    grid = _grid_dataframe(cfg, result)
    st.dataframe(_style_grid(grid), width="stretch")

    # Per-nurse summary.
    if report:
        st.markdown("#### Per-nurse summary")
        sdf = pd.DataFrame([{
            "Nurse": s.name, "Target": s.target_fte, "Scheduled": s.scheduled_fte,
            "Deviation": s.deviation, "Total hrs": s.total_hours,
            "Avg hrs/wk": s.avg_weekly_hours,
            "Sats": f"{s.saturdays_worked}/{s.saturdays_in_period}",
            "Worst 9-wk Sat": s.worst_9wk_sat,
            "In tol?": "✅" if s.within_tolerance else "❌",
        } for s in report.nurse_summaries])
        st.dataframe(sdf, hide_index=True, width="stretch")

    # Download button.
    if report:
        data = workbook_bytes(cfg, result, report)
        st.download_button(
            "⬇️ Download .xlsx",
            data=data,
            file_name=output_filename(cfg),
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
            width="stretch",
        )


def _label(od) -> str:
    return f"W{od.week_index + 1} {od.d.strftime('%a %d-%b')}"


def _grid_dataframe(cfg: Config, result) -> pd.DataFrame:
    operating = result.operating or build_operating_dates(cfg)
    labels = [_label(od) for od in operating]
    data = {label: [] for label in labels}
    index = []
    unavail_by_nurse = {n.name: set(n.unavailable_dates) for n in cfg.nurses}
    for n in cfg.nurses:
        index.append(n.name)
        worked = result.assignments.get(n.name, {})
        for od in operating:
            label = _label(od)
            code = worked.get(od.iso)
            if code:
                data[label].append(code)
            elif od.iso in unavail_by_nurse[n.name]:
                data[label].append("LV")
            else:
                data[label].append("")
    return pd.DataFrame(data, index=index)


def _style_grid(df: pd.DataFrame):
    def color(v):
        if v == "D10":
            return "background-color:#ADD8E6"
        if v == "D5":
            return "background-color:#90EE90"
        if v == "LV":
            return "background-color:#FFFF00"
        return ""
    return df.style.map(color)


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
