# Pediatric Dialysis Unit Scheduler

A scheduling app for the **BC Children's Hospital pediatric hemodialysis
unit**. It generates a compliant, equitable master work schedule under the
BCNU Provincial Collective Agreement (Articles 25 & 26) and exports it as a
formatted Excel workbook.

The unit runs a single day shift, four operating days a week:

| Day | Shift | Elapsed | Paid hours |
|-----|-------|---------|-----------|
| Mon / Wed / Fri | **D10** 0730–1730 | 10.0 | 9.5 (30-min unpaid meal) |
| Sat | **D5** 0730–1230 | 5.0 | 5.0 |

Closed Sunday, Tuesday, Thursday. No evenings or nights. Paid hours assume the
30-min unpaid meal; a **missed meal is paid as overtime** (Art. 27, flagged not
priced), so there is no meal-designation toggle. **The rotation starts on a
Friday.**

---

## Run it in the browser — no install (GitHub Codespaces)

If you can't install software locally (e.g. a locked-down work computer), run
the app entirely in your browser:

1. On the GitHub repo page, click the green **Code** button → **Codespaces**
   tab → **Create codespace** (on this branch).
2. Wait ~1 minute while it installs dependencies — this is automatic
   (`.devcontainer/devcontainer.json`).
3. The app **starts itself** and opens in a preview tab. If it doesn't pop up,
   open the **Ports** tab and click the globe icon next to port **8501**.

The Codespace is private to your GitHub account (fine for real roster data) and
the free tier covers ~60 hours/month. To restart the app manually in the
Codespace terminal: `streamlit run app.py`.

## Quick start (local install)

Requires Python 3.11+.

```bash
pip install -r requirements.txt
streamlit run app.py
```

Then in the browser:

1. Set the **schedule period** (start date must be a Friday; rotation length is
   freely selectable, default 12 weeks, minimum 6), **daily demand** per
   M/W/F/S, and the **default FTE flex** in the sidebar.
2. Edit the **roster** table:
   - **D10 shifts (0–40)** and **D5 shifts (0–10)**: the number of 10-hour
     weekday and 5-hour Saturday shifts each line should work over the rotation.
     These counts are the **primary target**, prioritized over the FTE flex.
     **FTE** is derived from the counts (read-only).
   - **FTE flex ±** is per line, defaulting to the sidebar value (±0.08).
   - **Job share**: give two lines the same label (A/B/…) and they will never
     be scheduled on the same day (two people splitting one line).
   - **Line preferences** (tick boxes): non-consecutive Saturdays, clustered
     shifts, and off-day preferences for Monday / Wednesday / Friday. These are
     soft and **conflicts are resolved by seniority** (rank 1 wins).
3. Press **GO** — nothing is scheduled until you do. On success you get a styled
   grid preview, compliance badges, a per-nurse summary, and a **Download .xlsx**
   button. On infeasibility you get a red banner explaining which requirement binds.

Configuration is persisted to a local JSON file (`dialysis_config.json` by
default) so settings survive sessions — there is no database.

### Run the tests

```bash
python tests/test_smoke.py
```

---

## How it works

| Module | Responsibility |
|--------|----------------|
| `dialysis_scheduler/config.py` | Config dataclasses, defaults, JSON load/save |
| `dialysis_scheduler/fte.py` | FTE maths and the achievable-FTE menu (§4) |
| `dialysis_scheduler/model.py` | Operating-date materialization, eligibility |
| `dialysis_scheduler/scheduler.py` | CP-SAT model + greedy fallback (§5–7) |
| `dialysis_scheduler/validator.py` | Validation pass (§8) |
| `dialysis_scheduler/excel_export.py` | Four-sheet workbook (§9) |
| `app.py` | Streamlit UI (§10) |

### FTE mathematics

`weekly_full_time_hours = 37.5` (Art. 26.01).
Each line's target is its **shift counts**; the derived FTE is
`(D10 × 9.5 + D5 × 5.0) / (37.5 × weeks)`, and
`scheduled_fte = total_paid_hours / (37.5 × weeks)`. FTE is shown for reference
and checked secondarily; the shift counts are what the generator targets.

### Hard constraints (always hold)

- **H1** Coverage: weekdays meet **or exceed** demand (extras tolerated and
  penalized); Saturday is **exact** (no extras).
- **H2** ≤ 6 Saturdays in every rolling 9-week window (25.06(E); for periods
  shorter than 9 weeks a proportional cap is applied).
- **H3** No assignment on unavailable dates, or Saturdays for
  `fixed_saturdays_off` nurses.
- **H4** ≤ 6 consecutive calendar days (structurally bounded to 2 here;
  asserted anyway).
- **H6** One shift per nurse per day.
- **H8** Job-share lines (same label) never work the same day.
- **H9** Everyone (not waived off Saturdays) works **≥ 1 Saturday per rolling
  4-week window** ("at least one Saturday per month").

(FTE is no longer a hard band — the shift counts are the target, with FTE flex
as a secondary, reported check.)

### Soft objectives (weighted, descending priority)

1. **Shift-count matching (primary)** — hit each line's requested D10 (weekday)
   and D5 (Saturday) counts. Saturday counts carry the most weight; this is
   prioritized over the FTE flex.
2. **Minimize weekday extras** — over-staffing is tolerated off Saturday but
   avoided where possible.
3. **Low-FTE engagement** — lines below 0.30 derived FTE are pushed to work in
   ≥3 of every rolling 4 weeks.
4. **Line preferences** — each ticked preference (non-consecutive Saturdays,
   clustered shifts → longer consecutive days off, off Mon/Wed/Fri) is rewarded,
   weighted by seniority so the **senior nurse wins when two preferences
   conflict**.
5. **Consistency** — penalize week-over-week changes in each nurse's weekday
   line, driving a stable repeating rotation.
6. **Weekday equity within an FTE class**, then minor **derived-FTE deviation**.

Ties break by seniority (senior nurses get first pick of off-Saturdays).

### Generation flow

CP-SAT solves under H1–H6 with a **deterministic** time limit (single worker →
the same inputs always reproduce the same schedule). If infeasible, FTE
tolerance is relaxed to ±0.13 (reporting which nurses drifted). If still
infeasible, a diagnostic pass names the binding requirement — most often
demand vs roster capacity, or Saturday demand vs the 25.06(E) cap (checked up
front with a cheap pre-check) — and a greedy fallback produces a best-effort
schedule.

### Excel output

`dialysis_schedule_<start>_<end>.xlsx` with four sheets:

- **Schedule** — master grid (frozen panes, operating days only, Saturday
  columns shaded, shift color codes, per-nurse totals, per-day coverage row,
  and a legend with meal/rest entitlements and the EWD note).
- **Summary** — per-nurse target/scheduled FTE, deviation, hours, Saturdays.
- **Compliance** — the validation report with PASS/FAIL/INFO and article cites.
- **Config** — a snapshot of every input (auditability; the workbook is the
  durable record per 25.05).

---

## Compliance notes

- **Extended Work Day:** D10 (10.0 h) exceeds the 7.5 h normal daily full shift
  (26.01); it operates under the Extended Work Day Memorandum (25.11). The UI
  shows a non-blocking banner to verify shift lengths against your EWD terms.
- **Off-duty consecutiveness (25.06(D)):** cannot be satisfied on a
  Mon/Wed/Fri/Sat unit because Tue/Thu are isolated closure days. This is a
  documented, intentional non-conformance (written employee agreement
  recommended) and is **not** solved by the generator.
- **Posting (25.05):** the app warns if the start date is < 6 weeks out and
  notes that changes within 10 calendar days trigger overtime (25.08).

## Out of scope

- Vacation planning, shift exchanges (25.09), self-scheduling (25.04) — this
  produces the **master schedule** only.
- Payroll / overtime pricing (Art. 27) — flagged but not calculated.
- Internal vacancy / line-change processes (25.03).
- Shift/weekend premiums, rotation-change and three-different-shifts logic
  (25.06(F), 25.12, Art. 28) — out of scope on a single-day-shift unit.
