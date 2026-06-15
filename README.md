# Pediatric Dialysis Unit Scheduler

A scheduling app for the **BC Children's Hospital pediatric hemodialysis
unit**. It generates compliant, equitable master work schedules under the BCNU
Provincial Collective Agreement (Articles 25 & 26) and exports them as clean,
print-ready Excel workbooks. It's built to grow into a hospital-wide system.

> **Resuming this project?** Jump to [Project status & where to pick up](#project-status--where-to-pick-up),
> and see [`DEVELOPMENT.md`](DEVELOPMENT.md) for the code-level developer map
> (function index, the CP-SAT model, decision log, debugging recipes).

---

## The unit at a glance

The unit runs a single day shift, four operating days a week, and **the rotation
starts on a Friday**:

| Day | Shift | Elapsed | Paid |
|-----|-------|---------|------|
| Fri / Mon / Wed | **D10** 0730–1730 | 10.0 h | 9.5 h (30-min unpaid meal) |
| Sat | **D5** 0730–1230 | 5.0 h | 5.0 h |

Closed Sunday, Tuesday, Thursday. No evenings or nights. A **missed meal is paid
as overtime** (Art. 27, flagged not priced), so there's no meal-designation
toggle. Weeks are 7-day blocks anchored to the Friday start (Fri → Sat → Mon →
Wed).

---

## Quick start

### In the browser, no install (GitHub Codespaces)
1. On the repo page: green **Code** → **Codespaces** → **Create codespace**.
2. Wait ~1 min — dependencies install automatically (`.devcontainer/`), the app
   starts itself and opens in a preview tab (or open port **8501** in **Ports**).

Private to your account (fine for real data); free tier ~60 h/month. Restart with
`streamlit run app.py`.

### Local (Python 3.11+)
```bash
pip install -r requirements.txt
streamlit run app.py
```

### Tests
```bash
python tests/test_smoke.py        # core: generation, validation, Excel
python tests/test_edge.py         # robustness: bad input, infeasibility, determinism
python tests/test_compliance.py   # re-checks every BCNU rule on real output (all profiles, 6/9/12/18 wk)
python tests/test_profiles.py     # the three options each do their job
```

---

## How you use it

1. **Sidebar** — start date (must be a Friday), rotation length (6–52 wk,
   default 12), and **nurses needed per day** (weekday is a minimum, Saturday is
   exact; default 3/3/3/2). Statutory holidays in the period are listed for
   reference. Load/save config as JSON.
2. **Roster table** — one row per nurse:
   - **D10 (0–40)**, **D5 (1–10)**, **Stat (0–10)** shift counts over the
     rotation. These counts are the **hard target**; **FTE** is derived
     (read-only). Stat shifts are paid statutory-holiday days that reduce worked
     D10. **Everyone works Saturdays** (D5 ≥ 1).
   - **Job share** label — two lines with the same label never work the same day
     (two part-timers splitting **one** full-time line; combined FTE ≤ 1.0).
   - **Preferences** (tick boxes) — non-consecutive Saturdays, clustered shifts,
     off Mon/Wed/Fri. Honoured most in the preference option.
   - **Unavailable dates** — comma-separated `YYYY-MM-DD`.
   - **Seniority is not used** — lines are picked by seniority *after*
     generation, so it's left out of the build.
3. **GO** — produces **three options** in tabs:
   - **Preference-maximizing** — best satisfies the ticked preferences.
   - **Equity-maximizing** — each nurse gets a fair mix of Mon/Wed/Fri and their
     Saturdays are evenly spaced.
   - **Cluster-maximizing** — groups each nurse's shifts so days off come in
     longer continuous blocks.

   Each tab has a **"How this schedule was built"** explainer, the schedule grid,
   a per-nurse summary, the compliance report, an **Adjust** panel (see below),
   and a **Download .xlsx** that reflects any edits.

### Adjusting a schedule (manual edits)
- **Click a cell** in the grid to change it — pick the day's shift code to staff
  it or blank for off. (Streamlit can't do drag-and-drop; click-to-edit is the
  direct equivalent.)
- **Swap two shifts** — pick Shift A and Shift B; **a confirmation appears**, and
  on confirm the two nurses trade days (counts/coverage preserved; swapping onto
  an unavailable date is blocked).
- **Reset to generated** — discard manual edits.

Everything **re-validates live** — the status banner and compliance report update
instantly, and the download reflects the edits. Edits live in a session working
copy; pressing GO again starts fresh.

---

## Architecture

One-way data flow; the validator independently re-derives every metric from the
assignments, so it's a **check on** the solver, not a restatement of it.

```
config  →  model (operating dates)  →  scheduler (CP-SAT × 3 profiles)  →  validator  →  excel_export
```

| Module | Responsibility |
|--------|----------------|
| `dialysis_scheduler/config.py` | Dataclasses (`ShiftDef`, `Nurse`, `Config`), defaults, JSON load/save (tolerant of unknown keys), `apply_derived_ftes`, `Nurse.worked_d10` |
| `dialysis_scheduler/model.py` | `build_operating_dates` (Friday-anchored weeks), eligibility |
| `dialysis_scheduler/holidays.py` | BC statutory-holiday dates (BCNU Art. 17) |
| `dialysis_scheduler/scheduler.py` | Pre-checks, CP-SAT model per profile, greedy fallback, `generate_schedules` / `generate_schedule` |
| `dialysis_scheduler/validator.py` | Independent validation pass + per-nurse summary |
| `dialysis_scheduler/excel_export.py` | Four-sheet black-and-white workbook |
| `dialysis_scheduler/fte.py` | FTE maths / achievable-FTE menu (reference + tests) |
| `app.py` | Streamlit UI |

### FTE & counts
`weekly_full_time_hours = 37.5` (Art. 26.01). Each line's **shift counts** are
the target; the derived FTE is `(D10 × 9.5 + D5 × 5.0) / (37.5 × weeks)`, shown
for reference. `worked_d10 = max(0, target_d10 − stat_days)` (stat days are paid
but not worked).

### Hard constraints (always hold)
- **H1** Coverage — **hard when the roster can cover** (no blanks); **soft when
  short-staffed**, leaving the unfillable shifts **blank** (flagged) rather than
  failing. Over-staffing is a light soft penalty.
- **H2** ≤ 6 Saturdays in every rolling **9-week** window (25.06(E); proportional
  below 9 weeks). Note: over a longer rotation a nurse may work **more than 6**
  total (e.g. up to 12 in 18 weeks) — see `_period_max_saturdays`.
- **H3** No assignment on a nurse's unavailable dates.
- **H4** ≤ 6 consecutive calendar days (structurally ≤ 2 here; asserted anyway).
- **H6** One shift per nurse per day.
- **H8** Job-share lines never work the same day; combined FTE ≤ 1.0.
- **H9** **Everyone** works **≥ 1 Saturday per rolling 4-week window**
  ("≥ 1 Saturday/month") — no Saturday waiver.
- **H10** Each line works **exactly** its requested worked-D10 / D5 counts.
  Guaranteed in every option; the profiles only change *which* days fill the
  counts.

### Soft objectives (three profiles)
All three satisfy H1–H10 and minimize weekday over-staffing; they differ only in
the secondary goal they push (weights in `OBJECTIVE_PROFILES`):

| Option | Pushes |
|--------|--------|
| Preference-maximizing | ticked line preferences (off-days, non-consec Sat, cluster) |
| Equity-maximizing | per-nurse weekday-type balance + evenly-spaced Saturdays |
| Cluster-maximizing | global off/off adjacency → long days-off blocks |

Each profile is solved **independently and deterministically** (single worker +
deterministic time limit), so identical inputs reproduce the same three
schedules byte-for-byte.

### Statutory holidays (BCNU Art. 17)
`holidays.py` computes the BC stat holidays per year. A line's **stat-shift
entitlement** is shown on the grid as **`ST`** (paid, not worked) on the **actual
statutory-holiday dates** in the rotation — **the solver chooses which holidays
each nurse takes** so coverage stays balanced. Each ST day reduces that line's
worked D10 target. Full coverage is still expected on holidays, so if too many
take the same day off the grid shows a blank. The entitlement is capped at the
number of (weekday) holidays in the period.

### Generation flow (`generate_schedules`)
1. **Config integrity** — empty roster, blank/duplicate names → `CONFIG_INVALID`
   (names key the assignments, so duplicates are rejected).
2. **Pre-checks** (clear messages, no solve): per-day capacity (job-share lines
   count once), the 25.06(E) Saturday cap, ≥ 1-Saturday-per-month seats, and the
   shift-count feasibility check (∑D5 = Saturday seats; each D5 within the
   monthly minimum and the period Saturday max; ∑worked-D10 ≥ weekday seats;
   job-share combined counts/FTE fit).
3. **CP-SAT × 3 profiles** — one deterministic solve each, **run in parallel**
   (OR-Tools releases the GIL, so threads give a real ~3× speedup; GO ≈ 18 s).
4. **Greedy fallback** — if CP-SAT finds nothing, a diagnostic names the binding
   rule and a greedy pass fills coverage best-effort (still honouring job share &
   the Saturday cap).

### Excel output (plain black-and-white)
`dialysis_schedule_<start>_<end>_<Option>.xlsx`, landscape + fit-to-width, with a
printed title header (unit · sheet · period · option) and page footer. Four
sheets: **Schedule** (grid; red flags a coverage shortfall — the only colour),
**Summary** (worked vs target counts, FTE, Saturdays), **Compliance** (the
validation report; FAIL = red, WARN = amber), **Config** (full input snapshot).

### Failure handling — nothing is silent
| Situation | Handling |
|-----------|----------|
| Empty / blank / duplicate roster | `CONFIG_INVALID` + live UI error |
| Not enough staff to cover every day | Generates anyway; unfillable shifts left **blank**, flagged with the count and days |
| ∑D5 ≠ Saturday seats; D5 below monthly min or above the period Saturday max | Shift-count pre-check, specific message |
| Job share over capacity / combined FTE > 1.0 | Pre-check message |
| ≥ 1 Saturday/month infeasible for the pool | Pre-check compares nurses to seats |
| Solver finds nothing | Diagnostic + greedy fallback |
| Manual edit breaks a rule (coverage, counts, unavailability) | Live re-validation flags it; download reflects edits |
| Old / future config JSON | `from_dict` ignores unknown keys |

---

## Project status & where to pick up

**State:** feature-complete for the single unit; presented to staff/managers.
Branch: `claude/blissful-hypatia-abtscx`. All four test suites pass; output is
deterministic and contract-compliant across 6/9/12/18-week rotations and all
three profiles.

### Implementation gotchas (read before changing solver code)
- **Determinism** comes from `num_search_workers = 1` + a **deterministic** time
  limit (`DET_TIME_LIMIT` / `ALT_DET_TIME`), not wall-clock. Don't switch to a
  wall-clock limit or multiple workers without re-checking reproducibility.
- **Counts are HARD** (`H10`). Secondary objectives can't change them — verified
  by `test_compliance` / `test_profiles`. If you re-soften them, the validator
  rule must move back to WARN.
- **Saturday max per period** is `_period_max_saturdays(weeks)`
  (`{6:4, 9:6, 12:9, 18:12, 24:18}`) — *not* a flat 6. The ≥1/month minimum is
  `_h9_min_saturdays(weeks)`.
- **Stat days** only free up time off when the roster has slack; otherwise
  coverage forces the shifts (correct, and flagged).
- **App edits** use a session working copy (`st.session_state["work"][idx]`);
  controls mutate it and the grid/summary read from it. Visual slots are reserved
  before the controls so a mutation shows the same run.
- Avoid `st.rerun()` inside a tab (it can reset the active tab) — the current code
  relies on natural button reruns instead.

### Recommended next features (priority order)
1. **Skill / role mix** — currently all RNs are interchangeable. Add roles
   (charge nurse, certification, preceptor) and per-day skill requirements.
   *(Noted as out of scope for this small unit, but the #1 gap for larger ones.)*
2. **Seniority line-assignment (bidding) module** — the missing other half:
   nurses claim generated lines in seniority order.
3. **Vacation/leave at scale** — concurrent-vacation caps, blackouts, accrual
   (today: a flat unavailable-dates list).
4. **Fairness dashboard + per-nurse `.ics`/calendar export** — adoption & union
   transparency; builds on data already computed.
5. **pytest + GitHub Actions CI**, **ruff/mypy**, **pydantic config validation**,
   `pyproject.toml` packaging.
6. **Hospital-wide foundations** — stable employee **IDs** (not names), a
   database + schedule versioning/approval, **rules-as-data** per unit, a
   **generalized shift model** (days/eves/nights, arbitrary lengths), auth/roles.

### Out of scope (by design, this unit)
- Vacation planning, shift exchanges (25.09), self-scheduling (25.04).
- Payroll / overtime pricing (Art. 27) — flagged, not priced.
- Shift/weekend premiums, rotation-change & three-different-shifts logic
  (25.06(F), 25.12, Art. 28).
- Off-duty-day consecutiveness (25.06(D)) — impossible on a Mon/Wed/Fri/Sat unit
  (isolated Tue/Thu closures); surfaced as a documented non-conformance.

## Compliance notes
- **Extended Work Day:** D10 (10 h) exceeds the 7.5 h normal daily shift (26.01);
  verify against your Extended Work Day Memorandum (25.11) — non-blocking banner.
- **Posting (25.05):** the app warns if the start date is < 6 weeks out; changes
  within 10 days trigger overtime (25.08).
