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
   freely selectable, default 12 weeks, minimum 6) and the **daily demand** per
   M/W/F/S in the sidebar (default: weekday minimum **3**, Saturday exactly **2**).
   The sidebar also lists the **statutory holidays** (BCNU Art. 17) in the period.
2. Edit the **roster** table:
   - **D10 shifts (0–40)**, **D5 shifts (1–10)** and **Stat shifts (0–10)**: the
     10-hour weekday, 5-hour Saturday, and paid statutory-holiday counts each
     line works over the rotation. The counts are the **primary target**; stat
     shifts are paid (Art. 17) and reduce worked D10 shifts. **FTE** is derived
     (read-only). **Everyone works Saturdays** (D5 ≥ 1, plus ≥ 1 per month).
   - **Job share**: give two lines the same label (A/B/…) and they will never
     be scheduled on the same day (two people splitting one line).
   - **Line preferences** (tick boxes): non-consecutive Saturdays, clustered
     shifts, and off-day preferences for Monday / Wednesday / Friday — honoured
     most in the preference-maximizing option.
3. Press **GO** — nothing is scheduled until you do. You get **three options —
   preference-, equity- and cluster-maximizing** — in tabs; each has an
   **editable grid** (click a cell to move a shift, compliance + download update
   live) and its own **Download .xlsx**. On infeasibility you get a banner explaining which
   requirement binds.

The Excel output is **plain black-and-white** — colour is used only to flag
problems (coverage shortfalls and FAIL/WARN rows) so it prints cleanly.

Configuration is persisted to a local JSON file (`dialysis_config.json` by
default) so settings survive sessions — there is no database.

### Run the tests

```bash
python tests/test_smoke.py   # core: generation, validation, Excel, FTE menu
python tests/test_edge.py    # robustness: bad input, infeasibility, determinism
```

---

## How it works

| Module | Responsibility |
|--------|----------------|
| `dialysis_scheduler/config.py` | Config dataclasses, defaults, JSON load/save (tolerant of unknown keys) |
| `dialysis_scheduler/model.py` | Operating-date materialization (Friday-anchored weeks), eligibility |
| `dialysis_scheduler/holidays.py` | BC statutory-holiday dates (BCNU Art. 17) |
| `dialysis_scheduler/scheduler.py` | CP-SAT model, pre-checks, 3-option generation, greedy fallback |
| `dialysis_scheduler/validator.py` | Independent validation pass over the final schedule |
| `dialysis_scheduler/excel_export.py` | Four-sheet B/W workbook |
| `dialysis_scheduler/fte.py` | FTE maths helper (achievable-FTE menu, used by tests) |
| `app.py` | Streamlit UI |

Data flows one way: `config` → `model` (operating dates) → `scheduler`
(CP-SAT) → `validator` → `excel_export`. The validator re-derives every metric
from the assignments alone, so it is an **independent check** on the solver, not
a restatement of it.

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
- **H3** No assignment on a nurse's unavailable dates.
- **H4** ≤ 6 consecutive calendar days (structurally bounded to 2 here;
  asserted anyway).
- **H6** One shift per nurse per day.
- **H8** Job-share lines (same label) never work the same day.
- **H9** **Everyone** works **≥ 1 Saturday per rolling 4-week window** ("at
  least one Saturday per month") — there is no Saturday waiver.

(FTE is no longer a hard band — the shift counts are the target, with FTE flex
as a secondary, reported check.)

### Soft objectives — three options per run

Every option satisfies all hard constraints and hits each line's **shift counts**
(D10 weekday + D5 Saturday, Saturday weighted highest), minimizes weekday
over-staffing, keeps low-FTE lines engaged (≥3 of every 4 weeks), and applies a
minor FTE smoother. **Seniority is not used** in generation — lines are picked
by seniority afterward, so the schedule is re-chosen anyway.

The three options differ only in which **secondary goal** they push:

| Option | Maximizes |
|--------|-----------|
| **Preference-maximizing** | Satisfies the ticked line preferences (off Mon/Wed/Fri, non-consecutive Saturdays, clustered shifts). |
| **Equity-maximizing** | Gives each nurse a fair mix of weekday types (no one stuck with all Mondays) and spaces their Saturdays evenly. |
| **Cluster-maximizing** | Groups everyone's shifts so off-stretches are long and contiguous (most consecutive days off). |

Each is solved independently and **deterministically**, so the same inputs
reproduce the same three schedules. Pick one, optionally hand-edit it, download.

### Stat days (BCNU Art. 17)

`holidays.py` computes the BC statutory holidays per year. Each line has a
**stat-shift entitlement** (paid days off). A stat day reduces the line's
*worked* D10 target (`worked_d10 = max(0, target_d10 − stat_days)`) and is paid
but not scheduled. Because the unit still runs at full demand, stat time off
only materializes when the roster has slack (total worked targets exceed
weekday seats); otherwise coverage forces the shifts and the validator flags the
shortfall. The app's info note shows the worked-vs-seats balance.

### Generation flow (`generate_schedules`)

1. **Config integrity** — empty roster, blank or duplicate names, no shifts → a
   `CONFIG_INVALID` result (no solve attempted). Names must be unique because
   assignments are keyed by name.
2. **Cheap pre-checks** — per-day capacity (incl. job-share lines counting once),
   the 25.06(E) Saturday cap, and the ≥1-Saturday-per-month seat count. Any
   failure returns a plain-language reason without invoking the solver.
3. **CP-SAT × 3 profiles** — one solve per objective profile (preference /
   equity / cluster). Each is **deterministic** (single worker + deterministic
   time limit), so the same inputs reproduce the same three schedules
   byte-for-byte.
4. **Greedy fallback** — if CP-SAT finds nothing, a diagnostic pass names the
   binding constraint family and a greedy pass fills coverage best-effort while
   still honouring job share and the Saturday cap.

### Excel output (plain black-and-white)

`dialysis_schedule_<start>_<end>_<Option>.xlsx`, four sheets:

- **Schedule** — master grid (frozen panes, operating days only, shift codes,
  per-nurse totals, per-day coverage row, legend). No decorative fill; **red
  flags a coverage shortfall**.
- **Summary** — per-nurse worked-vs-target D10/D5, FTE, hours, Saturdays.
- **Compliance** — the validation report; **FAIL = red, WARN = amber**, PASS/INFO
  plain.
- **Config** — a snapshot of every input (auditability; the workbook is the
  durable record per 25.05).

### Failure handling

Every failure mode is surfaced, never silent:

| Situation | How it's handled |
|-----------|------------------|
| Empty roster / blank / duplicate names | `CONFIG_INVALID` + live UI error |
| Demand > available staff on a day | Pre-check fails with the date and counts |
| Job share leaves a day short | Pre-check counts job-share lines once; clear message |
| Everyone can't get a monthly Saturday | Pre-check compares nurses to Saturday seats |
| Saturday demand exceeds the 25.06(E) cap | Cheap capacity pre-check before solving |
| Solver finds no feasible schedule | Diagnostic + greedy fallback (job share & cap still respected) |
| Counts can't all be hit (e.g. ∑D5 ≠ seats) | Soft objective; reported as a WARN per line |
| Old / future config JSON | `from_dict` ignores unknown keys |
| Manual grid edit breaks coverage | Live re-validation flags it; download reflects edits |

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

## Toward a hospital-wide system

This unit scheduler is the seed for a broader system. The design choices that
make that extension safe:

- **One-way data flow + independent validator.** The validator never trusts the
  solver; it recomputes every metric from the assignments. Any future solver
  (or manual edit) is checked the same way.
- **Hard vs soft separation.** Operational/contractual musts are hard
  constraints with cheap pre-checks that fail fast and explain why; preferences
  and equity are weighted soft terms. New units add rules in the same two tiers.
- **Determinism.** Single-worker + deterministic time limit makes every run
  reproducible — essential for auditing and grievance defence.
- **Config tolerance.** `from_dict` ignores unknown keys, so saved schedules
  survive schema changes.

Known scaling considerations before multi-unit rollout:

- **Identity.** Lines are keyed by **name** today (duplicates are rejected). A
  hospital system needs stable employee **IDs**; swap the key and carry a display
  name.
- **Saturday-per-month (H9)** is feasible only when Saturday seats ≥ headcount
  in every 4-week window — true for a 5-nurse unit, not for large pools. Make the
  cadence per-unit configurable (e.g. ≥1 weekend in N).
- **Solver size.** Variables ≈ nurses × operating-days. A 12-week unit is tiny;
  hospital-wide needs per-unit decomposition or a longer/parallel solve budget
  (the three profiles solve independently and could run in parallel).
- **Shift model.** Two shift types (D10/D5) are hard-coded in places (Saturday =
  D5). Generalize to arbitrary shift definitions with per-shift demand.
- **Out-of-scope items below** (vacation, exchanges, premiums, payroll) become
  in-scope and need their own modules and data.

## Out of scope

- Vacation planning, shift exchanges (25.09), self-scheduling (25.04) — this
  produces the **master schedule** only.
- Payroll / overtime pricing (Art. 27) — flagged but not calculated.
- Internal vacancy / line-change processes (25.03).
- Shift/weekend premiums, rotation-change and three-different-shifts logic
  (25.06(F), 25.12, Art. 28) — out of scope on a single-day-shift unit.
