# Developer handoff — Pediatric Dialysis Unit Scheduler

A code-level companion to the [README](README.md). The README is the
user/overview doc; this is the map for changing the code. Read the README's
**"Project status & where to pick up"** first, then this.

Branch: `claude/blissful-hypatia-abtscx` · Python 3.11 · Streamlit · OR-Tools
CP-SAT · openpyxl. No database (JSON config). Four test suites, all green.

---

## 1. Repo map & key functions

```
app.py                         Streamlit UI (single file)
dialysis_scheduler/
  config.py                    dataclasses, defaults, JSON load/save
  model.py                     operating-date materialization, eligibility
  holidays.py                  BC statutory holidays (Art. 17)
  scheduler.py                 pre-checks + CP-SAT (3 profiles) + greedy fallback
  validator.py                 independent validation pass
  excel_export.py              4-sheet B/W workbook
  fte.py                       FTE maths / achievable-FTE menu (reference + tests)
tests/
  test_smoke.py                generation/validation/Excel happy path
  test_edge.py                 bad input, infeasibility, determinism, job share
  test_compliance.py           re-checks every BCNU rule on real output
  test_profiles.py             each option does its job
.streamlit/config.toml         minimal theme (teal accent, minimal toolbar)
.devcontainer/devcontainer.json  Codespaces auto-install + auto-launch
```

### config.py
- `ShiftDef(weekday, code, start, end, elapsed_hours, paid_hours_unpaid_meal,
  paid_hours_designated_meal)` — `.weekday_name`, `.is_saturday`,
  `.paid_hours(meal_flag)`.
- `Nurse(name, target_fte, target_d10, target_d5, stat_days, unavailable_dates,
  fte_tolerance, job_share_group, pref_nonconsec_sat, pref_clustered,
  pref_off_mon, pref_off_wed, pref_off_fri)` — `.worked_d10()` =
  `max(0, target_d10 - stat_days)`; `.target_hours(d10_paid, sat_paid)`.
  **No `seniority_rank`, no `fixed_saturdays_off`** (both removed).
- `Config(start_date, weeks, demand, weekly_demand_override,
  meal_designated_available, fte_tolerance, weekly_full_time_hours,
  operating_shifts, nurses)` — `.start` (date), `.shift_for_weekday`,
  `.d10_paid()`/`.sat_paid()`, `.apply_derived_ftes()` (sets each
  `nurse.target_fte` from counts), `.demand_for(week, weekday_name)`,
  `to_dict`/`from_dict` (**from_dict filters unknown keys**), `save`/`load`.
- `default_operating_shifts()`, `default_nurses()` (Kathleen/Adam/Joane/Leslie/
  Kaitlyn), `default_config()`, `next_start_day()` (next Friday).
- Constants: `WEEKLY_FULL_TIME_HOURS=37.5`, `START_WEEKDAY=4` (Friday),
  `ALLOWED_WEEKS=[6,9,12,18]`, `DEFAULT_CONFIG_PATH`.

### model.py
- `OperatingDate(d, week_index, weekday, weekday_name, shift, demand,
  paid_hours)` — `.iso`, `.is_saturday`.
- `build_operating_dates(cfg)` — **weeks are 7-day blocks anchored to the start
  weekday** (offset = `(shift.weekday - start.weekday()) % 7`), so a Friday start
  yields Fri/Sat/Mon/Wed per block.
- `nurse_eligible_for(nurse, od)` — only checks `unavailable_dates` (everyone
  works Saturdays).

### scheduler.py — the core
Pre-checks (each returns `PreCheck(ok, messages)`):
- `config_integrity_check` — empty roster, blank/duplicate names, no shifts.
- `coverage_feasibility_check` — per-day, demand vs simultaneously-available
  (job-share lines count once).
- `saturday_feasibility_check` — `sat_demand × n_sat ≤ Σ eligible_sat × 6/9`.
- `sat_per_month_feasibility_check` — every 4-week window has ≥ headcount seats.
- `shift_count_feasibility_check` — **∑D5 = Saturday seats; each D5 in
  `[_h9_min_saturdays, _period_max_saturdays]` and ≤ eligible Saturdays;
  ∑worked-D10 ≥ weekday seats; job-share combined FTE ≤ 1.0 and combined counts
  ≤ available days.**

Helpers: `_sat_window_bounds(weeks)`, `_sat_cap_for_span(weeks, span)`,
`_h9_min_saturdays(weeks)` (greedy hitting set), `_period_max_saturdays(weeks)`
(`{6:4,9:6,12:9,18:12,24:18}`), `half_hours(h)` (CP-SAT is integer; hours ×2).

Model + solve: `_solve_cpsat(cfg, operating, profile, det_time)` builds the
model, sets `OBJECTIVE_PROFILES[profile]` weights, solves deterministically,
returns a `ScheduleResult(label=profile_label)`.

Entry points:
- `generate_schedules(cfg, profiles=PROFILE_ORDER)` → integrity → pre-checks →
  one `_solve_cpsat` per profile → list of results (or `[infeasible]` /
  `[greedy]`).
- `generate_schedule(cfg)` → `generate_schedules(cfg, ["preference"])[0]`.

`ScheduleResult(feasible, method, status, assignments, operating, messages,
binding_constraints, tolerance_used, drifted_nurses, label)`. `assignments` is
`{nurse_name: {iso_date: shift_code}}`.

### validator.py
`validate(cfg, result) -> ValidationReport(rules, nurse_summaries,
max_consecutive_days)`. Each rule is `RuleResult(rule, citation, status, detail)`
with status PASS/FAIL/WARN/INFO. `NurseSummary` carries scheduled vs target
D10/D5, FTE, hours, Saturdays, worst-9-week count. **Re-derives everything from
`assignments` alone** — independent of the solver.

### excel_export.py
`build_workbook(cfg, result, report)` → 4 sheets; `workbook_bytes(...)` →
`bytes`; `output_filename(cfg)`. `_print_setup` adds landscape/fit-to-width + a
printed header/footer (no cell shifting). **B/W**: only `FILL_SHORT` (coverage
shortfall), `FILL_FAIL`, `FILL_WARN` are coloured; all decorative fills are
`None`.

### app.py
`main()` → `sidebar()` → `roster_editor()` → `generate_section()`. Per option:
`_render_option(cfg, opt, idx)`. Edit helpers: `_worked_shifts`, `_swap_error`,
`_do_swap`, `_style_grid`, `_grid_df`, `_cached_workbook_bytes`, `_label`,
`_parse_dates`. Profile copy: `PROFILE_DESC`, `GUARANTEES`.

---

## 2. The CP-SAT model (`_solve_cpsat`)

Variables: `x[(ni, oi)] ∈ {0,1}` for each eligible nurse × operating date.

**Hard constraints**
| # | Rule | Code |
|---|------|------|
| H1 | coverage: **hard** (`assigned ≥ demand`) when total counts ≥ seats, else **soft** (shortfall → blank shifts); extras light soft | coverage loop, `W_SHORTFALL`/`W_EXTRA` |
| H2 | ≤ `_sat_cap_for_span` Saturdays per rolling 9-week window | sat-window loop |
| H8 | job-share group: ≤ 1 member per day | `js_groups` loop |
| H9 | ≥ 1 Saturday per rolling 4-week window, per nurse | `sat_windows_4` loop |
| H10 | `Σ weekday x == worked_d10`, `Σ Saturday x == target_d5`, per nurse | section "1." |

(H3 = var omission for unavailable dates; H4/H6 structural.)

**Soft objective** = `Σ obj_terms` (minimized). Per-profile weights in
`OBJECTIVE_PROFILES`:

```
preference: pref=900 wd_equity=30  sat_spread=30  cluster_all=0   pattern=250
equity:     pref=60  wd_equity=500 sat_spread=350 cluster_all=0   pattern=250
cluster:    pref=60  wd_equity=30  sat_spread=30  cluster_all=600 pattern=150
```
Always-on: `W_EXTRA=600` (weekday over-staffing), `W_THREE_OF_FOUR=1500`
(low-FTE < 0.30 active ≥ 3 of every 4 weeks). Terms:
- **weekday equity** — within-FTE-class spread **and** per-nurse Mon/Wed/Fri
  balance (the latter makes equity meaningful when all FTEs differ).
- **consistency (pattern)** — penalize week-over-week weekday changes.
- **preferences** — off-day (penalize working it), non-consec Sat (penalize
  back-to-back), cluster (reward off/off adjacency). `pref` weight, flat (no
  seniority).
- **sat_spread** (global) — penalize back-to-back Saturdays for everyone.
- **cluster_all** (global) — reward off/off adjacency for everyone.

**Determinism:** `num_search_workers = 1`, `max_deterministic_time = det_time`
(`ALT_DET_TIME=8` when generating 3, `DET_TIME_LIMIT=12` for one),
`random_seed = 42`. Do **not** rely on `max_time_in_seconds` for the stop point.

---

## 3. App state & edit model

Session-state keys: `cfg`, `options` (list of `ScheduleResult`), `work`
(`{idx: assignments}` — the editable working copy), `_wb_cache`,
`swapA_{idx}`/`swapB_{idx}`, `setn_/setd_/setv_{idx}`, `pending_swap_{idx}`.

`_render_option` pattern:
1. "How this schedule was built" expander (de-blackboxing).
2. **Reserve `status_slot` / `grid_slot` containers** *before* the Adjust
   controls, so a mutation in the controls is reflected when the slots are filled
   later **in the same run**.
3. Adjust expander: **Swap** (sets `pending_swap` → renders Confirm/Cancel →
   `_do_swap` on confirm), **Set cell**, **Reset**.
4. `validate` the working copy → fill `status_slot`, `grid_slot`, summary,
   compliance, download (`_cached_workbook_bytes`, memoized per assignments hash).

**Swap guards** (`_swap_error`): different nurses, different days, both shifts
still present, **same shift type** (so counts are preserved), neither already
working the other's day; the caller also blocks swapping onto an unavailable
date. Pressing **GO** clears `work` + swap/edit widget keys.

Gotcha: avoid `st.rerun()` inside a tab (resets the active tab); rely on the
natural rerun a button click triggers.

---

## 4. Decision log (why it is the way it is)

- **Shift counts are HARD (H10), not soft.** Earlier they were weighted; the
  cluster objective could then pull a nurse off-target (11/21 vs 31/24). Made
  hard so all three options share identical per-nurse counts and differ only in
  arrangement. `test_compliance`/`test_profiles` guard this.
- **Seniority removed from generation.** Lines are picked by seniority *after*,
  so baking it in (tie-breaks, preference weighting) was redundant — dropped the
  field and logic.
- **Three named profiles** (preference/equity/cluster) instead of generic
  "diverse" solutions, each maximizing one secondary goal. Replaced an earlier
  Hamming-diversity approach.
- **Everyone works Saturdays** (removed the `fixed_saturdays_off` waiver; D5 ≥ 1;
  H9 applies to all).
- **Saturday max per period** = `_period_max_saturdays`, not a flat 6 — 25.06(E)
  is per rolling 9-week window, so longer rotations allow more total Saturdays.
  (Bug found by `test_compliance` on the 18-week case.)
- **Stat days** reduce *worked* D10 (paid but not worked); only yield real time
  off when the roster has slack.
- **Friday start**; weeks anchored to the start weekday.
- **Excel is B/W**; colour only flags problems (for clean printing).
- **Deterministic** via single worker + deterministic time limit.
- **Coverage is conditional** — hard when the roster can cover (guarantees full
  staffing, prunes the search), soft (blank shifts) only when short-staffed. This
  fixed a case where a fully-soft coverage + short solve budget left avoidable
  blanks.
- **The 3 profiles solve in parallel** (`ThreadPoolExecutor`; OR-Tools releases
  the GIL during `Solve`) — GO dropped ~70 s → ~18 s. Each solve is still
  single-worker + deterministic-time, so reproducibility holds.
- **No meal-designation toggle** — missed meals are OT (Art. 27).

---

## 5. Recipes

**Run a thing**
```bash
streamlit run app.py
python tests/test_compliance.py        # ~2-3 min (many CP-SAT solves)
```

**Add a hard constraint:** add the `model.Add(...)` in `_solve_cpsat`; add a
cheap **pre-check** with a clear message; add a **validator rule**; add a
`check_contract` assertion in `test_compliance.py`.

**Add a soft preference:** add a `Nurse` bool field; a roster column in
`roster_editor`; an objective term in `_solve_cpsat` (weight from the profile
dict); a `test_profiles.py` behaviour check.

**Reproduce determinism:** `generate_schedules(cfg)` twice → `assignments`
equal. If it ever differs, suspect a wall-clock stop or `num_search_workers > 1`.

**Common pitfalls**
- Counts hard ⇒ adding stat days / unavailability without roster slack makes it
  infeasible (correct — the pre-check explains). Tests must use feasible configs.
- `test_compliance` is slow; it solves 4 period lengths × 3 profiles.
- Assignments are keyed by **name**; duplicate names are rejected up front.

---

## 6. Next steps

See the README's **"Recommended next features"**. Top three: **skill/role mix**
(safety — currently all RNs interchangeable), **seniority line-assignment
(bidding) module** (the missing other half), **vacation/leave at scale**. Then
**pytest + CI** and the **hospital-wide foundations** (employee IDs, DB,
rules-as-data, generalized shift model).
