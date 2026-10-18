# Stage 4 findings — why there is no candidate layer

Status: measured 2026-10-18 on the live recording, stage 4 **mothballed for lack of demonstrated
benefit**: the tested PV/load candidates produced no different executable dispatch, while price-side and
floor variants do differ, so this is a lack of demonstrated selection benefit and **not** a claim that
candidates cannot differ. This note exists so a future reader does not rebuild the layer without
re-running the probe. The probe is `tools/candidate_probe.py` (revision and digest pinned in (g)); the run is pinned
by the input store's SHA-256 and by that probe source. The raw output is deliberately kept outside the
tree, so this note stands alone: every number the finding relies on is in the tables below.

## (a) The question, and the plain answer

Stage 4 was going to add non-anticipative *candidate* policies (deterministic baseline, scenario
SAA, per-scenario policies, CVaR selector) and a selector over them. That design assumes candidates
produce *different plans* worth choosing between.

Measured, on this instance's contract: **no PV or load stress candidate changes a single dispatched
watt.** Power differences are exactly zero (unrounded maximum |Δ| = 0.0 W, not merely below the 1 W
threshold) in 56/56 windows — and in 41/41 of the windows whose baseline solve reproduces its
recorded plan exactly — including a PV sweep down to 0 % and a load sweep up to 5×, and including a
PV perturbation applied *under a binding sell floor* (pairwise, 56/56 windows: the perturbation adds
nothing to the floor-only plan). Only the action *labels* move (`pv_charge` → `charge`/`grid_charge`
for the same watts). The one configuration where a PV/load perturbation does change the power path is
export-disabled, and there both the reference and the perturbed plans are reported **infeasible**
(`grid_limits_infeasible`), so neither is an executable alternative (see (d)).

## (b) Sample and measured table

Tool: `tools/candidate_probe.py` — re-solves the *same* DP over recorded decisions, one transform per
solve, comparing whole slot grids (equal length and identical timestamps; no truncation) and
reporting the unrounded maximum power difference plus an explicit feasibility flag.

- 56 windows = every 4th of the 222 `plans` rows in the recording (selected windows span
  2026-10-16 08:22:26 → 12:03:40; the store holds 222 plans through 12:04:40), 16 distinct
  15-minute vintages (distinct `horizon_start` buckets), 45 distinct start-SoC readings.
- 12 transforms + 14 sweep solves per window = **1,456 solves**; ~14 ms per C-core solve (median);
  ~56 s wall, dominated by the core's built-in periodic C-vs-Python differential check.
- Store opened read-only; every solve input is as-of (`"as_of": true`: no `settings` row postdates
  the earliest selected plan). 14 `optimizer.*` keys were absent from the store and fell back to
  `DEFAULTS`; they are listed here: `optimizer.allow_grid_charge`, `optimizer.cycle_cost_eur_per_kwh`,
  `optimizer.horizon_hours`, `optimizer.max_grid_export_w`, `optimizer.max_grid_import_w`,
  `optimizer.min_spread_eur_per_kwh`, `optimizer.power_steps`, `optimizer.pv_charge_preference`,
  `optimizer.salvage_multiplier`, `optimizer.soc_step_kwh`, `optimizer.target_soc_pct_end`,
  `optimizer.terminal_mode`, `optimizer.terminal_tail_hours`, `optimizer.terminal_tail_minutes`.
- **Options checked against the recording:** every option a plan summary exposes — `slot_minutes`,
  `wear_eur_per_kwh`, `soc_step_kwh` (recorded as `effective_step_kwh`), `eta_charge`,
  `eta_discharge` (56/56 windows each) and `sell_floor_eur_per_kwh` (recorded in the 15 later plans;
  the other summaries store `null`). 0 mismatches; `windows_with_option_mismatches: 0`. The other 14
  reconstructed options are **not attested by any plan row** and are named in
  `options_not_attested_by_summary` (`allow_export`, `allow_grid_charge`, `horizon_hours`,
  `max_export_kw`, `max_import_kw`, `min_spread_eur_per_kwh`, `power_steps`, `pv_charge_preference`,
  `refuse_battery_export`, `salvage_multiplier`, `target_soc_pct_end`, `terminal_mode`,
  `terminal_tail_hours`, `terminal_tail_minutes`).

Power versus the `det` baseline, 56 windows. "zero-power" is grid-qualified: equal length,
identical timestamps, feasibility equal to the baseline's, and an unrounded maximum |Δpower| of
exactly 0.0 W. "identical" additionally requires every action label to match; "label-diff" counts
windows where at least one label differs:

| transform | zero-power windows | identical (incl. labels) | label-diff windows | any power diff | first-slot power differs (>1 W) | max unrounded \|ΔW\| | infeasible windows |
|---|---|---|---|---|---|---|
| `no_export` (`allow_export=false`) | 0/56 | 0/56 | 56 | 56 | 43 | 1000.0 | **56** `grid_limits_infeasible` |
| `pv×0.7` | **56/56** | 0/56 | 56 | 0 | 0 | 0.0 | 0 |
| `load×1.3` | **56/56** | 0/56 | 56 | 0 | 0 | 0.0 | 0 |
| `pv×0.7, load×1.3` | **56/56** | 0/56 | 56 | 0 | 0 | 0.0 | 0 |
| `sell×0.85` | 0/56 | 0/56 | 56 | 56 | 0 | 730.43 | 0 |
| `buy×1.15, sell×1.15` | 44/56 | 44/56 | 12 | 12 | 0 | 800.0 | 0 |
| `sell_floor` = window p90 sell | 3/56 | 3/56 | 53 | 53 | 1 | 800.0 | 0 |
| that floor + `pv×0.7` (vs `det`) | **3/56** | 0/56 | 56 | 53 | 1 | 800.0 | 0 |
| that floor + `pv×0.7, load×1.3` (vs `det`) | **3/56** | 0/56 | 56 | 53 | 1 | 800.0 | 0 |

The floor-plus-perturbation rows have the same three zero-power windows as the floor alone (the
windows where the floor does not bind); where it does bind, the perturbation adds no further power
change (pairwise table below) but does relabel actions.

Restricted to the **41 exactly-reproducing windows**: `pv×0.7`, `load×1.3` and the combined stress
are zero-power in 41/41; `no_export` still differs with first-slot power changes in 40/41;
`sell×0.85` in 41/41 (never the first slot); the binding floor in 41/41 — the three zero-power floor
windows of the full set are all among the 15 inexact ones.

PV/load perturbations *within* a PV/load-dependent feasible set (pairwise against their own
reference; the F2 test):

| perturbation | zero-power vs reference | first-slot power differs | max unrounded \|ΔW\| | feasible windows |
|---|---|---|---|---|
| `pv×0.7`, export off | 0/56 | 40 | 1000.0 | **0/56** |
| `pv×0.7, load×1.3`, export off | 0/56 | 46 | 1000.0 | **0/56** |
| `pv×0.7`, binding floor | **56/56** | 0 | 0.0 | 56/56 |
| `pv×0.7, load×1.3`, binding floor | **56/56** | 0 | 0.0 | 56/56 |

## (c) Severity sweep

Graded PV 0.9 → 0.0 (downward) and load 1.1 → 5.0 (upward) over the same 56 windows, with **every
sweep solve checked exactly like a main transform** (equal length, identical timestamps, feasibility,
unrounded maximum): at all 14 levels **56/56 windows are power-exact-zero** with
`grid_mismatch_windows: 0`, `infeasible_windows: 0` and `max_abs_diff_exact_w: 0.0`. Action labels
still move: label-identical (`exact_equal_windows`) is 56/56 only at the trivial PV 0.9 and load 1.1
levels, 4/56 at PV 0.8, and 0/56 at every stronger level, with the median relabelled slots rising
0 → 9 as PV falls and 0 → 17 as load rises. Even removing all PV for the whole 30 h horizon leaves
the power path exactly unchanged. 784 sweep solves (14 per window).

## (d) Mechanism — separability plus non-decisive constraints, not a contract theorem

Equal buy and sell prices alone do **not** establish invariance; the measured invariance is the joint
consequence of two properties, and either can fail:

1. **Objective separability.** With `buy == sell == p` in every slot, the grid cost is
   `p·(load − pv) + p·p_batt`: PV and load enter only as an additive, action-independent term, so they
   cannot change the argmin. This requires that no other term prices PV or load energy
   action-dependently — `optimizer.pv_charge_preference` other than `auto`, a different PV-charge
   shadow price, or any nonlinear/step tariff can break it.
2. **Feasibility that does not move the optimum when PV/load are perturbed.** The feasible grid window
   may depend on `base = load − pv` — it does, with `allow_export = false` (lower bound `−base`), a
   binding sell floor (`−max(0, base)` and `0`), grid import/export caps, and `allow_grid_charge` — and
   then separability alone no longer guarantees invariance: the argmin *can* move. Whether it does is
   an empirical question per window: a constraint can depend on PV/load and still leave the selected
   optimum where it was.

The two clauses have to be read as a conjunction, and the measurements show one decisive constraint
that is still perturbation-insensitive, plus one place where the question cannot be tested:

- **The binding floor is decisive for the optimum and still perturbation-insensitive.** The floor
  changes the plan against `det` in 53/56 windows (so the floor *is* decisive for the optimum), yet the
  *additional* PV/load perturbation under that floor adds exactly nothing — power-exact-zero 56/56 and
  0.0 W pairwise against the floor-only plan (it does relabel actions). That is measured perturbation
  **insensitivity inside a PV/load-dependent feasible set**, not an insensitive constraint.
- **With export off** the model has no curtailment variable for a PV surplus, so the reference and the
  perturbed plans both come back `grid_limits_infeasible`; the perturbation changes that (infeasible)
  path in 56/56 windows, which cannot be read as evidence for or against a feasible alternative.

The sufficient guarantee is therefore the **conjunction** of both clauses — objective separability
**together with** feasibility that stays PV/load-independent, or at least does not move the optimum
under the perturbation — and neither clause carries it alone: separability does not survive a decisive
PV/load-dependent constraint, and a PV/load-independent feasible set does not survive an
action-dependent PV/load cost term. These windows are consistent with the conjunction holding, but
unchanged optima alone do not establish either structural condition, and they do not establish an unchanged feasible
set. None of this is a statement about the optimizer's general behaviour.

## (e) The transforms that do move plans, and the ex-post numbers

- `no_export`: the plan differs in 56/56 windows (first slot in 43/56, max 1000 W) — but every one of
  those plans is **infeasible** (`grid_limits_infeasible`, 56/56), because the DP cannot curtail PV.
  It is an existing setting, not a candidate policy, and it is not an executable alternative here;
  it is also not needed to find a dispatch-changing variant.
- `sell×0.85` (breaks `sell == buy`): **feasible in 56/56**, and it changes later power in 56/56
  windows (median 10 % of slots, max 730.43 W), never in the first slot. `buy/sell×1.15`:
  **feasible**, 12/56 windows, max 800 W, never the first slot.
- `sell_floor` raised to the window's p90 sell: **feasible in 56/56**, differs in 53/56 windows
  (median 2.5 % of slots, median moved 28 W, first slot 1/56).

So the feasible dispatch-changing variants in this sample are the **price-side transforms** (both of
them, on later slots) plus the binding floor; an earlier draft that called the export switch the only
feasible one was wrong. None of them varies the PV/load dimension a candidate selector would select
over, and none changes the first slot except the infeasible export switch and one floor window.

Ex-post re-pricing (open-loop, pinned-candidate, realized prices and pv/load): **this is an observed
**partial-prefix** difference over only the covered slots, not a value per horizon** — the windows
overlap heavily, shared realized periods are counted once per window, and no terminal energy is
valued. Coverage is a median 8.5 of 120 slots (≥4 slots in 47/56 windows). Over those windows,
`det` is cheaper than the export-off plan by 12.17 EUR total (median 0.32 EUR/window) — computed on
an *infeasible* plan, so it is not an achievable saving — and cheaper than the floor-at-p90 plan by
0.15 EUR total (median 0.004 EUR/window). All PV/load and price-scaling transforms: 0.00 EUR, since
their differing slots lie outside the covered span or do not exist. These numbers bracket nothing in
particular and are reported only for completeness.

## (f) Scope limits — do not over-read this

One sunny midday (08:22–12:04) on one day, 16 vintages, `sell == buy` in every recorded slot,
feed-in wiring off, a 2.1 kWh pack, floor sometimes absent in early plans. The result does **not** say
forecasts are worthless in general, that PV/load uncertainty never matters for battery control, or
that the optimizer is correct. It says that *on this contract*, over *this* recording, PV/load stress
cannot produce a different feasible dispatch — so a selector over such candidates has nothing to
select. Re-measure on representative new data (evenings, winter, other tariffs) or after a contract
change (`sell < buy`, feed-in wiring on, curtailment modelling, caps).

## (g) Reproducibility

Probe revision `313522f99eccf9b91091962900b82e7b5945b9a8`; probe file SHA-256
`871162235ebc02f047c641bc6513164ff48fd91ddd016b4b1b0d63f65a37c05a`. The probe source is committed
together with this note, so that digest pins the committed file. C-core build `019c1d04194f45e7`
(equal to the source SHA-256 prefix, so the loaded artifact matches `_core.c`).

The **measurement input** is identified, not preserved: a checksum lets a later reader *recognise* the
measured state if those bytes are still at hand, and that is all it does. The measured bytes were a
snapshot of the live store, `/tmp/live-store/energy_manager.db` on the phone host (outside the repo),
SHA-256 `f7ceb2ed7dfbf3765b24c9205e23c7d134030a2858fec561e0363d55117e207c`, 14,057,472 bytes, mtime
2026-10-16T14:07:15+02:00, WAL 0 bytes (so this single file is the whole state). The procedure below —
the documented way to take a snapshot, from `docs/REPLAY_HARNESS.md` §*The store* — makes a **new**
snapshot from the live store; it does **not** retrieve those historical bytes:

```sh
sqlite3 -readonly <live add-on store> ".backup <snapshot path>"   # never a bare file copy
```

The live store keeps growing (222 `plans` rows when measured) and the probe samples every 4th row, so a
later snapshot is a **different input** and yields a different sample, not a rerun of this one. So
**historical reruns become unavailable once that copy is lost**; the honest options then are to keep a
preserved copy, or to re-measure on whatever the store holds and label the result as a new measurement
rather than quoting these tables. Preserve the snapshot (or an export with enough retention) if a
rerun of these exact numbers is wanted.

Build and run:

```sh
cd energy_manager/app/optimize && \
gcc -O3 -ffp-contract=off -fPIC -shared \
    -DCORE_BUILD_SHA="\"$(sha256sum _core.c | cut -c1-16)\"" \
    $(python3-config --includes) \
    -o "_core$(python3-config --extension-suffix)" _core.c

cd /path/to/repo && \
python3 tools/candidate_probe.py --store /path/to/energy_manager.db --stride 4 --sweep \
    --results /tmp/stage4_probe_results.json --json
```

The results path is deliberately **outside** the repository: the PII audit (`tools/audit.sh`, SPEC.md
R1) scans untracked files and reads a float with five or more fractional digits in lat/long range as a
coordinate, and the raw output carries precise power and price series — scanned in the tree it
produced 291 findings on the artifact alone, i.e. it would break that gate. So the full output was
produced and reviewed during the measurement but is not committed: a rerun needs the preserved
snapshot plus this probe source, and every number this note relies on is printed in the tables above,
so nothing here depends on opening that file. The probe warns when `--results` resolves inside
the repository; keep any output outside the worktree.

`-ffp-contract=off` is load-bearing: without it GCC contracts float expressions differently and the
argmins are no longer identical to the Python reference. Rerun comparability requires the *same* input
bytes: if the historical copy is still at hand, check it with `sha256sum` against the pinned value
above; a fresh `.backup` will **not** match it, and that mismatch is itself the sign that the run is a
new measurement rather than this one. The probe refuses
to run without the C core, refuses a non-as-of store unless `--allow-non-as-of` is passed (then it
marks the result), and writes
nothing except the requested `--results` file. `--stride N`, `--limit N` and `--sweep` change the
sample and the severity grid; every run prints its own sample description.

## (h) Fidelity of the baseline solve (and its limits)

The `det` re-solve reproduces the recorded plan **exactly in 41/56 windows** (identical slot grid,
zero power error, zero action mismatches; feasibility true in 56/56). In the other 15 the grid still
matches and the differences are confined to PV-surplus charging slots (the horizon's first slot and
the 13:15 saturation tail): maximum single-slot error 826.09 W, per-window mean ≤7.06 W, at most 2
action labels differ, and the re-solve is *cheaper* than the recorded plan in all 15 (by 1e-5 to
0.0213 EUR, median 0.0072 EUR). Those 15 windows are one contiguous block (11:03–12:03) during PV charging, where the start
SoC used for the re-solve is read 37–287 s before the plan was written; with `actuals` sampled every
few minutes the recorded plan could also reflect a partly executed/commanded schedule. The probe
cannot separate those two causes, so the PV/load-invariance claim is published for the exact subset
(41/41, 0.0 W) as well as for all 56 windows (0.0 W). Both agree. Note also that this section is about
reproducing the *baseline*: it says nothing about whether a candidate could differ (see (e) and (i)).

## (i) What this means for stage 4

`docs/STAGE4_DESIGN.md` premised the layer on candidates differing and a selector choosing among them.
What the measurement shows is narrower than "the premise is false", and the claim has to be
chaptered:

- **Measured — the tested PV/load candidates:** no dispatched watt changed (0.0 W exactly, all
  severities, under a binding floor, 41/41 in the exactly-reproducing subset). The scenario dimension
  the layer wanted to select over was never measurable here, because that machinery was gated behind
  this first dimension. So what is established is that the tested PV/load dimension supplied **no
  demonstrated selection benefit** in this recording.
- **Not measured, and not claimed impossible:** candidates *can* differ. Price-side transforms change
  later power (12/56 and 56/56 windows above) and the binding floor changes 53/56 — all feasible plans;
  the export switch changes the plan but is infeasible in 56/56.

So stage 4 is **mothballed for lack of demonstrated benefit, not because candidates cannot differ**:
no candidate layer, no scenario-archive requirement, no CVaR/lambda tuning, no live risk switch.

This retirement is not a permanent claim about uncertainty-aware control, and reconsideration does
**not** require a contract change: representative new data (evenings, winter, other tariffs) or a
materially different price/risk objective is enough, because the tested probes do not cover
price-distribution policies, scenario objectives or closed-loop uncertainty — which are exactly the
places a benefit would have to show up. Re-run `tools/candidate_probe.py` on the new data or
objective; if candidate-induced dispatch differences appear in the dimension a selector would select
over, revisit stage 4. **These reconsideration conditions live in this section**, and the spec's §5
points here for them.
