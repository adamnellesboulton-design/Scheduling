# How the solver works

This is the single authoritative description of how a schedule is produced. It
spells out, in order, every stage the generator runs — what is decided, what is
**guaranteed** (hard), what is merely **optimized** (soft), and how the three
options differ. It is written to be read top to bottom.

All of it lives in `dialysis_scheduler/scheduler.py`; the constant names and
function names below are searchable there. The independent re-check lives in
`dialysis_scheduler/validator.py`.

---

## 0. The one-paragraph summary

The unit operates four days a week — **Fri, Sat, Mon, Wed** — with **D10**
(10-hour) weekday shifts and **D5** (5-hour) Saturday shifts. The generator
builds a master schedule with Google OR-Tools **CP-SAT**, a constraint solver.
It treats each nurse's **requested shift counts as hard** (everyone works exactly
their D10/D5 numbers) and all the **contract rules as hard** (Saturday caps,
one Saturday a month, job-share separation, the opt-in per-nurse guarantees).
**Coverage is soft**: if the roster genuinely cannot fill a day, that shift is
left **blank and flagged** rather than failing. Everything else — fairness,
preferences, clustering — is a **soft objective** the solver maximizes. It runs
the solve three times with three different objective weightings to give you
three genuinely different but equally-compliant **options**.

---

## 1. Inputs

From the roster and the sidebar:

- **Period**: a start date (a Friday) and a number of `weeks`.
- **Operating days & demand**: how many nurses are needed each Mon/Wed/Fri/Sat.
- **Per nurse**: name, `target_d10`, `target_d5`, `stat_days` (paid statutory
  days off), `unavailable_dates`, an optional `job_share_group`, soft
  **preferences**, and opt-in **hard guarantees**. (FTE is *derived* from the
  counts, not an input.)

`build_operating_dates()` expands the period into the concrete list of operating
dates, each tagged with its rotation week, weekday, demand and paid hours.

---

## 2. Stage A — feasibility pre-checks (the gate)

Before any solving, cheap checks reject inputs that are *impossible* with a clear
message instead of an opaque "infeasible". Two of these **gate** the solve:

- `config_integrity_check` — non-empty roster, unique non-blank names, ≥1 week.
- `sat_per_month_feasibility_check` — enough Saturday seats that **everyone** can
  get one in every rolling 4-week window (the H9 rule below).
- `shift_count_feasibility_check` — each nurse's requested counts are physically
  and contractually reachable. It catches, with a specific message: D5 above the
  available Saturdays; D5 below the H9 minimum or above the H2 maximum; a
  Monday-off nurse whose D10 can't fit the remaining weekdays; a **work-weekly**
  nurse whose D10 can't cover every business week; a Friday-before-Saturday nurse
  with D10 < D5; and job-share groups whose combined counts overflow the
  available days or exceed 1.0 FTE.

Coverage capacity is **not** a gate (it's soft — see §4.1). If a gate fails, the
generator returns the diagnosis from `_diagnose()` and stops.

---

## 3. Stage B — decision variables

For every **eligible** (nurse, operating-date) pair the model creates one binary:

- `x[ni, oi] = 1` means nurse `ni` works date `oi`.

Two structural guarantees are baked in by simply **omitting** variables, so they
can never be violated:

- A nurse marked unavailable on a date gets no variable there (**H3**).
- A `fixed_off_mon` nurse gets no variable on any Monday (the hard "never Mondays").

Statutory holidays get their own binaries `st[ni, oi]` — the solver **chooses**
which holidays each nurse takes off (paid, not worked), up to entitlement, spread
to protect coverage. `x + st ≤ 1` on a holiday (you can't work a day you're off),
and each nurse takes exactly its effective entitlement. A taken stat day reduces
that nurse's **worked** D10 target by one.

---

## 4. Stage C — hard constraints (always hold)

These are non-negotiable. The solver will leave coverage shifts blank before it
breaks any of them.

### 4.1 Coverage — H1 (hard where possible, else soft)
For each operating day, `Σ x over that day`:
- If the roster's fixed counts **can** cover the day's demand, it's **hard**:
  `assigned ≥ demand` (full staffing, and it prunes the search).
- If a day is genuinely short (too few eligible nurses, or the nurse totals don't
  add up to total demand), coverage becomes **soft** for that day: a `short`
  variable measures the gap and is penalized heavily in the objective, so the
  solver fills as many slots as the fixed counts allow and leaves the rest blank.
- Over-staffing is always allowed but lightly penalized (`extra`).

### 4.2 Exact shift counts (hard)
Each nurse works **exactly** its worked-D10 (target minus taken stat days) and
**exactly** its D5. This is the primary promise and is identical in all three
options — the options only rearrange *which* days fill those counts.

### 4.3 Saturday rules
- **H2 — rolling weekend cap (25.06(E)):** ≤ 6 Saturdays in any 9-week window
  (proportional for shorter periods). Guarantees ≥ 1 weekend off in 3.
- **H9 — a Saturday a month:** ≥ 1 Saturday in every rolling 4-week window, for
  everyone.

### 4.4 Job share — H8
Nurses sharing a `job_share_group` label are two people splitting **one** line, so
**at most one** of them is assigned on any given day.

### 4.5 Opt-in per-nurse guarantees (hard when ticked)
- **Mon off (fixed):** never a Monday (done by variable omission, §3).
- **Work weekly:** ≥ 1 **weekday** shift in every **Mon–Fri business week**.
  Business weeks are *not* the Friday-anchored rotation week — see
  [§7](#7-business-weeks-the-work-weekly-subtlety).
- **Fri before Sat:** every worked Saturday is preceded by its Friday
  (`x[Fri] ≥ x[Sat]`); if the Friday isn't workable, the Saturday is forced off.

*(H4 "≤ 6 consecutive days" and H6 "≤ 1 shift/day" can't be violated on a
Mon/Wed/Fri/Sat unit, so they need no explicit constraint — the longest possible
run is Fri+Sat.)*

---

## 5. Stage D — the soft objective

Everything that isn't hard is folded into a **single weighted sum that CP-SAT
minimizes**. It has two tiers. The first is **fixed** — the same weights in all
three options; these dominate and behave like a priority order:

| Term | Weight | What it does |
|------|-------:|--------------|
| `W_SHORTFALL` | **8000** | Penalty per **blank** shift. Dominant — fill coverage first. |
| `W_THREE_OF_FOUR` | **1500** | Low-FTE nurses (< 0.30 FTE) should be active in ≥ 3 of every 4 weeks. |
| `W_EXTRA` | **600** | Penalty per **over-staffed** slot (avoid needless extras). |

The second tier is **per-option**: these weights change with the chosen profile,
and **their relative order is not fixed** — re-ordering them is exactly what makes
the three options differ (see §6). Do not read this table as a global ranking.

| Term | Pref. | Equity | Cluster | What it does |
|------|------:|-------:|--------:|--------------|
| `pref` | 900 | 60 | 60 | Honour each nurse's ticked preferences (off-days, spread Saturdays, clustering). |
| `wd_equity` | 30 | 500 | 30 | Balance each weekday type across equal-FTE nurses and within each nurse. |
| `sat_spread` | 30 | 350 | 30 | Space each nurse's Saturdays out (penalize back-to-back). |
| `cluster_all` | 0 | 0 | 600 | Reward everyone's adjacent days off (longer blocks off). |
| `pattern` | 250 | 250 | 150 | Keep each nurse's weekday pattern stable week to week (predictability). |

Hard constraints are *not* in this sum — they're enforced separately (§4) and
always hold regardless of weights.

---

## 6. Stage E — the three options (objective profiles)

The same hard model is solved **three times**, each maximizing a different
secondary goal by re-weighting the soft terms (`OBJECTIVE_PROFILES`):

- **Preference-maximizing** — `pref` is high (900); best satisfies the ticked
  preferences. (`pref 900, wd_equity 30, sat_spread 30, cluster 0, pattern 250`.)
- **Equity-maximizing** — `wd_equity` (500) and `sat_spread` (350) are high;
  spreads weekday types and Saturdays fairly. (`pref 60, … cluster 0, pattern 250`.)
- **Cluster-maximizing** — `cluster_all` (600) is high; groups each nurse's shifts
  so days off come in longer blocks. (`pref 60, wd_equity 30, sat_spread 30,
  cluster 600, pattern 150`.)

Because the counts and every hard rule are identical across all three, the
options **never** differ in *how many* shifts anyone works — only in *which
days*. They are solved **sequentially** (each solve already saturates the cores).

---

## 7. Business weeks — the "work weekly" subtlety

The rotation is **Friday-anchored**: each 7-day block is `[Fri, Sat, Mon, Wed]`.
That Friday closes the **previous** Mon–Fri week, while the block's Mon/Wed open
the **next** one — so a single Mon–Fri *business* week's operating days straddle
the rotation-week boundary (Mon + Wed of one block, Fri of the *next*).

`business_week_index()` maps each date to its Mon–Fri week and **wraps
cyclically** (`% weeks`): the trailing Mon/Wed of the final block share a business
week with the first block's Friday. The schedule loops straight back into its own
start, so this is correct — and it stops the seam artefact where one end of the
period gets a doubled-up week and the other an empty one. The "work weekly"
guarantee (§4.5), its pre-check (§2) and the validator all group by this index.

---

## 8. Stage F — search

Each solve runs CP-SAT as an **8-worker portfolio** (`SEARCH_WORKERS = 8`) under
a wall-clock cap (`PER_OPTION_SECONDS = 4.0` for three options,
`SINGLE_OPTION_SECONDS = 6.0` for one). The extra workers run **LNS** (Large
Neighbourhood Search), which improves the incumbent far faster than a single
worker. The cap is an upper bound — CP-SAT returns early the instant it proves a
solution **optimal**; simple rosters usually reach a near-optimal incumbent well
before the cap, while the headroom lets harder rosters and the cluster option
keep polishing.

Trade-off, stated plainly: multi-worker LNS is **not byte-reproducible**.
`random_seed = 42` is set, but re-running identical inputs can yield a slightly
different — equally compliant, equally count-exact — layout. Every hard rule and
exact count still holds on every run.

---

## 9. Stage G — fallback (rare)

If, after the pre-checks pass, CP-SAT still finds no feasible solution for any
profile (unusual), the generator:
1. runs `_diagnose()` to name the binding constraint family, and
2. produces a **best-effort greedy** schedule (`_greedy`) that fills each day
   with the eligible nurses furthest below their target hours, while honouring the
   Saturday cap, job-share separation and the per-nurse guarantees (including the
   business-week work-weekly repair). Any unmet coverage is reported, not hidden.

---

## 10. Stage H — independent validation

Whatever path produced the schedule, `validate()` **re-checks it from scratch**
and is what the UI status banner and the Excel Compliance sheet report. It is a
genuine cross-check (separate code from the solver), covering: coverage / blanks,
unavailability, job-share separation, max consecutive days, the H2 and H9
Saturday rules, the work-weekly business-week guarantee, exact shift counts,
derived FTE within flex, the low-FTE 3-of-4 target, and statutory-holiday credit.
Manual grid edits and swaps are re-validated live through the same function.

---

## 11. Glossary of constraint IDs

| ID | Rule | Hard/Soft | Where |
|----|------|-----------|-------|
| H1 | Daily coverage meets demand | Hard if coverable, else soft (blanks) | §4.1 |
| H2 | ≤ 6 Saturdays per rolling 9 weeks (25.06(E)) | Hard | §4.3 |
| H3 | Never works an unavailable date | Hard (variable omission) | §3 |
| H4 | ≤ 6 consecutive days (25.06(C)) | Structural (can't occur) | §4.5 |
| H6 | ≤ 1 shift per day | Structural (one var/day) | §4.5 |
| H8 | Job-share partners never share a day | Hard | §4.4 |
| H9 | ≥ 1 Saturday per rolling 4 weeks | Hard | §4.3 |
| — | Exact D10 / D5 counts | Hard | §4.2 |
| — | Mon off / Work weekly / Fri before Sat | Hard when ticked | §4.5 |

---

## 12. Contract mapping (BCNU / NBA Provincial Collective Agreement)

This separates what the tool treats as a **contract rule** from what is **unit
policy** or a **modeling interpretation**, so the compliance story is honest.

> **Verification limit.** These article numbers are the *modeling intent*. They
> should be confirmed against your unit's current NBA Provincial Collective
> Agreement and your local Extended Work Day (EWD) Memorandum — automated
> retrieval of the agreement PDFs was blocked, so the numbers below were not
> re-checked against the live text. The two worth checking **first**, because
> they most affect compliance, are the **25.06(E) weekend formula** and the
> **37.5 h full-time week (26.01)**.

| Modeled as | Cited | What the tool does | Category |
|------------|-------|--------------------|----------|
| Full-time week = 37.5 h | 26.01 | FTE = paid hours ÷ (37.5 × weeks) | Contract constant |
| Master schedule posted ≥ 6 weeks ahead | 25.05 | Warns if the start is < 42 days out | Contract (advisory) |
| ≤ 6 consecutive days worked | 25.06(C) | Structurally impossible here (longest run is Fri+Sat = 2); still validated | Contract |
| Off-duty-day consecutiveness | 25.06(D) | Cannot be met on a Mon/Wed/Fri/Sat unit (isolated Tue/Thu closures); flagged as documented non-conformance, not solved | Contract (needs written agreement) |
| Weekend off ≥ 1 in 3 | 25.06(E) | **Interpreted** as ≤ 6 Saturdays per rolling 9-week window (H2) | Contract, interpreted — **verify** |
| Short-notice change overtime | 25.08 | Flagged informationally, not priced | Contract (informational) |
| Extended Work Day (D10 = 10 h > 7.5 h normal) | 25.11 / 26.01 | Flagged; defers to your EWD Memorandum | Contract — confirm EWD memo |
| Meal / rest periods | 26.03 / 26.04 | Paid hours assume a 30-min unpaid meal (D10 = 9.5 h paid); a missed meal is overtime (Art. 27), flagged not priced | Contract (informational) |
| Statutory holidays | Art. 17 | 13 BC stats; an ST day is paid, replaces a worked D10, and is placed on the real holiday date | Contract — confirm article + list |
| ≥ 1 Saturday per rolling 4 weeks ("a Saturday a month") | — | Hard constraint (H9) | **Unit policy, not contract** |
| Job share: never the same day, combined ≤ 1.0 FTE | — | Hard constraints (H8 + precheck) | **Unit policy** |
| Exact D10 / D5 counts per nurse | — | Hard | **Unit target** |
| Mon off / Work weekly / Fri before Sat | — | Hard when ticked | **Per-nurse opt-in, not contract** |

What the tool does **not** price or decide: overtime pay (Art. 27), seniority
order (nurses pick lines by seniority *after* generation), or anything requiring
the EWD Memorandum's specific terms.
