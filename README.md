# Pediatric Dialysis Unit Scheduler

A scheduling app for the **BC Children's Hospital pediatric hemodialysis
unit**. It generates a compliant, equitable master work schedule under the
BCNU Provincial Collective Agreement (Articles 25 & 26) and exports it as a
formatted Excel workbook.

The unit runs a single day shift, four operating days a week:

| Day | Shift | Hours | Paid (unpaid meal) | Paid (designated-available meal) |
|-----|-------|-------|--------------------|----------------------------------|
| Mon / Wed / Fri | **D10** 0730–1730 | 10.0 | 9.5 | 10.0 |
| Sat | **D5** 0730–1230 | 5.0 | 5.0 | 5.0 |

Closed Sunday, Tuesday, Thursday. No evenings or nights.

---

## Quick start

```bash
pip install -r requirements.txt
streamlit run app.py
```

Then in the browser:

1. Set the **schedule period** (start date must be a Monday; rotation length is
   freely selectable, default 12 weeks, minimum 6), **daily demand** per
   M/W/F/S, **meal designation**, and **FTE tolerance** in the sidebar.
2. Edit the **roster** table. **Target FTE is entered freely** (each nurse's
   contracted line) and the generator schedules within **±tolerance** (default
   ±0.08) of it. An achievable-pattern menu is shown as a guide, and the app
   warns if a target exceeds what unit hours can reach (max 0.89, or 0.93 with
   a designated-available meal — full-time 1.0 is unreachable here).
3. Click **Generate schedule**. On success you get a styled grid preview,
   compliance badges, a per-nurse summary, and a **Download .xlsx** button. On
   infeasibility you get a red banner explaining which requirement binds.

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
`scheduled_fte = total_paid_hours / (37.5 × weeks)`.
The achievable menu enumerates every (weekday-shifts/2wk, Saturday-shifts/2wk)
pair, computes the FTE, dedupes and sorts. The maximum achievable is **0.89**
(unpaid meal) / **0.93** (designated-available meal); 1.0 cannot be reached on
operating hours alone.

### Hard constraints (always hold)

- **H1** Daily coverage equals demand.
- **H2** ≤ 6 Saturdays in every rolling 9-week window (25.06(E); for periods
  shorter than 9 weeks a proportional cap is applied).
- **H3** No assignment on unavailable dates, or Saturdays for
  `fixed_saturdays_off` nurses.
- **H4** ≤ 6 consecutive calendar days (structurally bounded to 2 here;
  asserted anyway).
- **H5** Scheduled FTE within ± tolerance of target, averaged over the period.
- **H6** One shift per nurse per day.

### Soft objectives (weighted, descending priority)

The objective is tuned to **maximize consistency and consecutive days off**:

1. **Consecutive days off** — reward every adjacent off/off calendar-day pair,
   so worked days cluster and off-stretches stay long and contiguous.
2. **Consistency** — penalize week-over-week changes in each nurse's weekday
   line, driving a stable repeating rotation (e.g. "always Mon/Wed/Fri").
   Saturdays are excluded because the 25.06(E) cap forbids a fixed weekly
   Saturday; their cadence is set by equity instead.
3. **Saturday equity** — FTE-proportional fair share (25.06(E) "fair and
   equitable").
4. **Weekday equity within an FTE class**.
5. **FTE deviation** — minimized even inside the tolerance band.

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
