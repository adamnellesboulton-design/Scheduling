# Developer handoff — Pediatric Dialysis Unit Scheduler

A code-level companion to the [README](README.md). The README is the
user/overview doc; this is the map for changing the code. Read the README's
**"Project status & where to pick up"** first, then this.

Branch: `claude/blissful-hypatia-abtscx` · Python 3.11 · Streamlit · OR-Tools
CP-SAT · openpyxl. No database (JSON config). Four test suites, all green.

For the algorithm itself — the ordered hard/soft layers, the exact objective
weights, why the three options differ, and the **BCNU contract-compliance
mapping** (what is contract vs unit policy vs interpretation) — see
[`SOLVER.md`](SOLVER.md). This file is the code map; `SOLVER.md` is the spec.

---

## 1. Repo map & key functions

```
app.py                         Streamlit UI (single file)
dialysis_scheduler/
  config.py                    dataclasses, defaults, JSON load/save
  model.py                     operating-date materialization, eligibility
  holidays.py                  BC statutory holidays
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
  pref_off_mon, pref_off_wed, pref_off_fri, fixed_off_mon, fixed_off_fri,
  fixed_work_weekly, fixed_fri_before_sat)` — the `fixed_*` flags are HARD per-line
  guarantees (never works a Monday / never a Friday / works a weekday every
  **business** week / Friday before each worked Saturday), enforced in all three
  options. `fixed_off_fri` excludes `fixed_fri_before_sat` (mutually exclusive).
  `pref_off_mon` is the SOFT
  Monday-off preference (cf. the hard `fixed_off_mon`). `.worked_d10()` =
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

Model + solve:
- `_build_core_model(cfg, operating) -> _CoreModel(model, x, st, short_terms,
  extra_terms, worked_d10_target, eff_stat)` — variables + **every hard
  constraint** (coverage relaxation, H2/H9, job share, exact counts, fixed
  guarantees). Shared by the full solve and the swap-repair.
- `_solve_cpsat(cfg, operating, profile, seconds)` — calls `_build_core_model`,
  adds the `OBJECTIVE_PROFILES[profile]` soft weights, solves (multi-worker LNS).
- `reoptimize_to_fit(cfg, operating, current, pins, seconds)` — the **minimal
  swap-repair**: `_build_core_model` + pins (hard) + a Hamming-distance objective
  to `current`; returns `RepairResult(ok, assignments, changed, message)`. Refuses
  a pin onto a non-existent var (fixed day off / unavailable).

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

**Per-line fixed options (HARD, opt-in checkboxes):**
- `fixed_off_mon` / `fixed_off_fri` — Monday / Friday `x` vars are **omitted** for
  that line (same mechanism as H3), so it can never be scheduled that weekday. A
  specific day also falls back to soft coverage when `len(vars_for_day) < demand`
  (heavy opt-out → blank shifts, never infeasible). `fixed_off_fri` is mutually
  exclusive with `fixed_fri_before_sat` (UI drops the latter; pre-check rejects a
  loaded config with both). The coverage pre-check and `_build_core_model` both
  skip these lines on the off weekday.
- `fixed_work_weekly` — `Σ weekday x over each BUSINESS week ≥ 1` for that line
  (Saturdays don't count; weeks with no eligible weekday are skipped). Business
  weeks are Mon–Fri (`business_week_index` in `model.py`): the rotation is
  Friday-anchored, so a block's Friday belongs to the *previous* Mon/Wed's week,
  and the cyclic `% weeks` wraps the trailing Mon/Wed of the last block together
  with the first Friday — no doubled/empty week at the seam. Pre-checked: worked
  D10 must reach the available business-week count, else a clear message.
- `fixed_fri_before_sat` — for each week, `x[Fri] ≥ x[Sat]` (every worked
  Saturday is preceded by its Friday); if the Friday is ineligible the Saturday
  is forced off. Pre-checked: worked D10 ≥ D5.

All three fixed guarantees are also honoured by the **greedy fallback**
(`_greedy`): `fixed_ok()` filters candidates for Monday-off and Fri-before-Sat,
and a post-fill repair pass adds a weekday for any `fixed_work_weekly` line that
would otherwise be idle a business week. So the guarantees hold on every solve path, not
just CP-SAT.

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
- **preferences** — off-day Wed/Fri (penalize working it), non-consec Sat
  (penalize back-to-back), cluster (reward off/off adjacency). `pref` weight, flat
  (no seniority). Monday-off is now a HARD `fixed_off_mon`, not a soft pref.
- **sat_spread** (global) — penalize back-to-back Saturdays for everyone.
- **cluster_all** (global) — reward off/off adjacency for everyone.

**Speed/quality:** `num_search_workers = SEARCH_WORKERS` (8) with a wall-clock
`max_time_in_seconds` budget (`PER_OPTION_SECONDS=4.0` when generating 3,
`SINGLE_OPTION_SECONDS=6.0` for one); options solved **sequentially** in
`generate_schedules`. The budget is an upper bound — CP-SAT returns early when it
proves OPTIMAL — but simple rosters typically converge to a near-optimal
incumbent (status `FEASIBLE`) well before the cap; the extra headroom mainly
helps the cluster option and larger / heavily-constrained rosters keep polishing.
Multi-worker LNS makes this faster and higher-quality than the old single-worker
deterministic solve, but it is **not** byte-reproducible. `random_seed = 42` is
still set but does not guarantee identical multi-worker runs.

---

## 3. App state & edit model

Session-state keys: `cfg`, `options` (list of `ScheduleResult`), `work`
(`{idx: assignments}` — the editable working copy), `_wb_cache`,
`swapA_{idx}`/`swapB_{idx}`, `setn_/setd_/setv_{idx}`, `pending_swap_{idx}`.

The grid is an **editable `st.data_editor`** (click a cell → dropdown). Its key
is versioned (`grid_{idx}_{gridver[idx]}`); the **Swap**/**Reset** buttons mutate
`work[idx]` and bump `gridver[idx]` so the editor remounts from the mutated copy.
Each render flushes the editor's cells back into `work[idx]` via
`_assignments_from_grid`. (True drag-and-drop isn't available in Streamlit.)

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
date. Pressing **Generate** clears `work` + swap/edit widget keys.

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
- **Stat days are solver-chosen decision vars** (`st[(ni, oi)]` over weekday
  statutory-holiday dates): each nurse takes `effective_stat = min(entitlement,
  eligible holidays)` of them as `ST` (paid, off), spread to keep coverage; ST is
  stored in `assignments` with code `"ST"`. **Everything that counts "worked"
  must use `model.is_worked(code)`** (D10/D5 only) — coverage, counts, Saturdays,
  consecutive-days, swap list, Excel totals. `worked_d10_target[ni] = target_d10
  - effective_stat`. Stat time off only materializes with roster slack; full
  coverage is still expected on holidays (blanks otherwise).
- **Friday start**; weeks anchored to the start weekday.
- **Excel is B/W**; colour only flags problems (for clean printing).
- **Multi-worker** solve (not byte-reproducible) for ~3× speed + better quality;
  re-runs stay feasible/compliant with identical exact counts.
- **Coverage is conditional** — hard when the roster can cover (guarantees full
  staffing, prunes the search), soft (blank shifts) only when short-staffed. This
  fixed a case where a fully-soft coverage + short solve budget left avoidable
  blanks.
- **The 3 profiles solve sequentially**, each with an 8-worker portfolio under a
  wall-clock budget (`PER_OPTION_SECONDS = 4.0`) — Generate ≈ 12 s for three
  options. Earlier this was thread-per-profile single-worker; multi-worker per
  solve already saturates the cores, so parallel profiles would only oversubscribe.
  The cap is an upper bound (early exit on proven OPTIMAL); the extra headroom over
  the old 2.5 s mainly polishes the cluster option and larger/constrained rosters.
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
- Counts hard, so adding stat days / unavailability without roster slack makes it
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
