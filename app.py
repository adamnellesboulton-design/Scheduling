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
    ALLOWED_WEEKS,
    DEFAULT_CONFIG_PATH,
    default_config,
    default_operating_shifts,
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
    weeks = st.sidebar.selectbox(
        "Length (weeks)", ALLOWED_WEEKS, index=ALLOWED_WEEKS.index(cfg.weeks)
        if cfg.weeks in ALLOWED_WEEKS else 2,
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

    # Meal designation flag.
    st.sidebar.subheader("Meal designation")
    cfg.meal_designated_available = st.sidebar.checkbox(
        "Designated-available meal (D10 paid = 10.0h, 26.03(B)(1))",
        value=cfg.meal_designated_available,
    )

    # FTE tolerance.
    st.sidebar.subheader("FTE tolerance")
    cfg.fte_tolerance = st.sidebar.slider(
        "± tolerance (averaged over period, 26.01)",
        min_value=0.02, max_value=0.20, value=float(cfg.fte_tolerance), step=0.01,
    )


# --- roster editor ---------------------------------------------------------


def roster_editor():
    cfg = _cfg()
    st.subheader("Nurse roster")

    menu = achievable_fte_menu(cfg)
    fte_values = [o.fte for o in menu]
    fte_labels = {o.fte: o.label for o in menu}
    st.caption(
        "Target FTE is chosen from the **achievable** menu below (whole shifts "
        f"only). Max achievable = {max_achievable_fte(cfg):.2f} on unit operating "
        "hours — full-time (1.0) is unreachable here."
    )
    with st.expander("Achievable FTE menu (for reference)"):
        st.dataframe(
            pd.DataFrame(
                [{"FTE": o.fte, "Pattern": o.label.split(" - ", 1)[1]} for o in menu]
            ),
            hide_index=True, width="stretch",
        )

    rows = []
    for n in cfg.nurses:
        rows.append({
            "name": n.name,
            "target_fte": n.target_fte if n.target_fte in fte_values
            else min(fte_values, key=lambda v: abs(v - n.target_fte)),
            "fixed_saturdays_off": n.fixed_saturdays_off,
            "seniority_rank": n.seniority_rank,
            "unavailable_dates": ", ".join(n.unavailable_dates),
        })
    df = pd.DataFrame(rows)

    edited = st.data_editor(
        df,
        num_rows="dynamic",
        width="stretch",
        column_config={
            "name": st.column_config.TextColumn("Name", required=True),
            "target_fte": st.column_config.SelectboxColumn(
                "Target FTE", options=fte_values,
                help="Achievable FTE (see menu above)",
            ),
            "fixed_saturdays_off": st.column_config.CheckboxColumn(
                "Fixed Sat off", help="25.06(B)/(E) waiver — never assigned Saturdays"
            ),
            "seniority_rank": st.column_config.NumberColumn(
                "Seniority", min_value=1, step=1,
                help="1 = most senior; tie-breaking only (25.03 ethos)",
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
            tf = float(r["target_fte"])
        except (TypeError, ValueError):
            tf = fte_values[0]
        new_nurses.append(Nurse(
            name=name,
            target_fte=tf,
            fixed_saturdays_off=bool(r["fixed_saturdays_off"]),
            seniority_rank=int(r["seniority_rank"]) if pd.notna(r["seniority_rank"]) else 1,
            unavailable_dates=dates,
        ))
    cfg.nurses = new_nurses

    # Show legend in fte_labels for the chosen FTEs.
    if cfg.nurses:
        chips = " · ".join(
            f"**{n.name}**: {fte_labels.get(n.target_fte, f'{n.target_fte:.2f}')}"
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
