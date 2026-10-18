# Optimizer v2 — design proposal (rev 7)

Status: proposal for review. `SPEC.md` stays authoritative until an amendment lands.
Rev 1 rejected. Rev 2 accepted as a direction. Rev 3–4 iterated. Rev 5 accepted by
github-copilot/gpt-6.1-sol, then blocked by the single final github-copilot/claude-opus-5.5 pass
on six findings; rev 6 applied all six. Rev 7 responds to the owner's two decisions: the C core is
a **stage-1 deliverable with a hard < 20 ms budget for the 120-slot solve** (the owner wants many
small plan variants compared per replan), and the feed-in model must be ready for the 2027
`sell = buy − tax` regime while today's contract is still `sell = buy`.

## 0. Decision summary

1. **Keep the deterministic DP** as the scheduler; it is already optimal for a known price path
   and already schedules multiple cycles.
2. **C core first (stage 1a)** with a hard budget of **< 20 ms for a 120-slot solve** on the live
   amd64 host, so many small plan variants can be generated and compared per replan. It is a
   drop-in: identical semantics to the Python reference, verified differentially, runtime-validated,
   with a Python fallback. No behaviour change on its own.
3. **Contract-correct feed-in accounting (stage 1b)**, with a complete settlement function and a
   reachability test that decides whether the billing state must be carried. Contracts the solver
   cannot price exactly are refused on the export leg, never approximated silently. Ready for
   2027 `sell = buy − tax`.
4. **Replay harness before enabling any new optimizer**, with paired counterfactuals, common
   accounting and dependence-aware uncertainty; prospective forecast capture where vintages are
   missing.
5. **Candidate policies (stage 3)**: the many-variants search the C core exists for. Built and
   tested behind a default-off flag; enabled only when the replay shows a material gain.
6. **Terminal value vector (stage 4)** only under a structural sufficiency condition; otherwise
   the existing `salvage` stays.

## 1. The incident, quantified

Live 2026-10-15 18:18Z: buy = sell = 0.3972 EUR/kWh, pack 68 %, house importing 0.29 kW,
cost-basis floor 0.3066, plan action `hold`. The DP holds for tomorrow's day-ahead peak
(0.4381–0.4821). Re-solving the live 120 slots reproduces the hold with and without the floor; a
counterfactual with only the current slot above the floor discharges. The floor is a lower bound,
not a trigger, so it never fires.

The owner's cycle (sell now, buy back at the horizon minimum 0.3237, sell at the peak 0.4821)
nets ~0.4566 per delivered AC kWh versus ~0.4621 for holding (round trip 0.8464, wear 0.0201).
At ~1.20 deliverable kWh the difference is **~0.0066 EUR**. A verified 0.10 EUR/kWh marginal
export error moves the same energy by **~0.12 EUR**. Tariff correctness dominates search.

## 2. Contract-correct feed-in accounting (stage 1)

### 2.1 Supported contracts and their complete settlement functions

Let `buy(t)` be the all-in consumer price, `import(t)`/`export(t)` the metered flows, `I`/`E` the
period import/export totals, `fee` a per-kWh redelivery charge, `vat_share` the VAT fraction the
contract excludes, `tax` the non-refunded energy-tax component, and `feed_in(t)` the
post-saldering feed-in price.

- **`explicit`** — `credit = Σ_t p(t)·export(t)` with `p(t)` from an entity or a flat tariff.
  Exogenous; energy-only state; exact.
- **`buy_minus_fee`** (fixed fee) — `credit = Σ_t (buy(t)·(1 − vat_share) − tax − fee)·export(t)`.
  Exogenous; energy-only state; exact. (Tiered or volume-dependent `fee` is not this contract.)
  This is the **2027 preparation**: when saldering ends, the owner turns
  `price.feed_in_energy_tax_refund` off and states the energy-tax component (the Zonneplan entity
  carries no per-slot tax component, so `price.energy_tax_eur_per_kwh` must be set; otherwise the
  derivation reports that it has nothing to subtract instead of silently pricing `sell = buy`).
  **Unknown is not zero.** A stated component is only usable when it is *confirmed*: the new
  `price.energy_tax_confirmed` flag (default false) distinguishes a confirmed value, including a
  confirmed `0.0`, from the default that has never been stated. With `refund = false` and the
  component unconfirmed, the derivation **refuses the battery-sourced export leg**
  (`Reason.UNPRICED_EXPORT`, visible in the plan and UI) rather than silently producing
  `sell = buy`. Regressions: refund off with an unconfirmed/default component refuses; refund off
  with a confirmed component (zero and non-zero) prices `sell = buy − tax` for every slot.
- **`tiered`** — `credit = Σ_k r_k·(volume in tier k)`, with tier boundaries `T_k`. The marginal
  rate depends on cumulative volume; §2.3 resolves that by pricing it exactly when the threshold is
  unreachable over the horizon and refusing the battery-sourced export leg otherwise.
- **`saldering`** (one supported convention, everything else rejected) — per-export-kWh credit at
  `buy(t)` while the period allowance is positive, else at `feed_in(t)`. Within a slot the metered
  flows are settled in a fixed order — **import first, then export** — so the slot's export is
  split at the allowance boundary, using the raw (unclamped) period balance so an outstanding
  export deficit from an exhausted period is retained:
  `eligible = min(export(t), max(0, I(t) + import(t) − E(t)))`, `excess = export(t) − eligible`,
  `credit(t) = buy(t)·eligible + feed_in(t)·excess`, `debit(t) = buy(t)·import(t)`.
  Transitions `I' = I + import`, `E' = E + export` (so charging raises the future netting
  allowance and self-consumption lowers it by avoided import). A crossing export is never credited
  wholly at the netting rate; a regression covers imports after allowance exhaustion
  (`I = 10, E = 12, import = 3, export = 2` credits exactly one unit at `buy`).
  This convention credits the export in the slot it happens. A contract that nets the whole period
  **at settlement** (statutory annual saldering) makes today's marginal export value depend on the
  expected period-end net position, not on today's running balance: it falls under the rejected
  "netting after settlement" and requires an explicit export price. Stage 1 gate 1 must record
  evidence of which convention the live contract uses before `saldering` is enabled.
  Other netting conventions — annual re-pricing, netting at a single tariff — are likewise
  **rejected**: require an explicit export price instead.
  The allowance state comes from configured period import/export entities. If it is unavailable,
  `saldering` cannot be priced: the export leg is refused until the owner supplies an explicit
  export price or explicitly opts into the documented assumption `saldering_assume_nettable`
  ("this period still has nettable import"), which the UI shows as an assumption, never a fact.

Unsupported formulas require an explicit export price, or the export leg is refused.

### 2.2 Convexity guard

Any convexity shortcut assumes `buy ≥ sell`. Negative prices and `sell > buy` are tested and fall
back to the finite DP's non-convex evaluation.

### 2.3 Billing state: reachability decides

Compute a bound on the reachable cumulative **metered** export over the horizon, including both
paths and **not** capped by the pack's energy:
`max_export = Σ_slots min(grid/inverter export limit, max(0, pv_forecast − load) + max_discharge_power) · dt`.
PV surplus that never touches the battery can export, and grid charging followed by discharging can
export more than one pack's worth, so a pack-energy bound is unsound. A regression combines a PV
surplus with a grid-recharge cycle. Then:

- **Energy-only, exact:** the marginal credit rate is constant over every reachable path
  (`explicit`, `buy_minus_fee`; `tiered` with `V + max_export ≤ next tier boundary above V`;
  `saldering` with the allowance `A` staying positive for all reachable paths, i.e.
  `E + max_export ≤ I` under the supported in-slot convention). The state stays the energy grid
  and the per-slot `sell` is exogenous.
- **Refusal:** otherwise the marginal rate is not constant over the reachable paths and the
  **battery-sourced** export leg is refused for that plan (`Reason.UNPRICED_EXPORT`); charging and
  self-consumption are unaffected. Refusal cannot stop PV surplus from exporting when the battery
  is full or charge-limited, so **forced PV export is still priced** — but at a *justified
  worst-case lower bound*, never a convenient zero. The floor is the minimum over **every
  settlement rate reachable over the horizon**, not a single formula: for each slot, the applicable
  rate set is `{p(t)}` for `explicit`, `{buy(t)·(1 − vat_share) − tax − fee}` for
  `buy_minus_fee`, `{r_k}` for every tier of a `tiered` contract, and `{buy(t), feed_in(t)}` for
  `saldering` (both the netting and the post-saldering rate are reachable). The floor is the
  minimum of that union, so it bounds negative wholesale prices too: with `buy = −0.20`,
  `vat_share = 0.21` and zero tax/fee, the saldering floor is at most `−0.20`, never `−0.158`.
  The cost-basis ledger prices PV charge at that same floor and the UI flags it as a conservative
  bound. If any applicable rate is unknown or unbounded (unsupported contract, unconfirmed
  component, no justified bound), the plan performs **no tariff-dependent optimisation** and uses
  the documented fail-safe (hold, or self-consumption-only where a hold is unsafe). Regressions
  cover negative feed-in for forced export and the PV charging opportunity cost, including the
  `buy = −0.20` saldering case and a tiered contract whose rates fall below the single-formula
  value.

**No extended billing-state DP is shipped.** A value function over cumulative volume is not
exactly priced by a grid holding only the tariff thresholds: a future export of `q` shifts every
settlement kink to `T_k − q`, and those Bellman-induced breakpoints accumulate across slots, so
such a grid can select the wrong schedule. Exact alternatives (propagating every induced
breakpoint, or enumerating all reachable volume states for the finite action model) are not
justified for a single-home horizon today; they are a future option, not a stage.

Stage 1 therefore ships **exact-or-refuse**: the energy-only exact cases above, and
`UNPRICED_EXPORT` for every reachable-threshold configuration until the owner supplies an explicit
export price. It never prices a threshold crossing at a constant rate, and it covers the live
saldering setup only once gate 1 has recorded evidence of the contract's netting convention and
the allowance state.

The cost-basis ledger keeps pricing PV charge at the slot's `sell` and grid charge at `buy`; the
floor stays an *eligibility* constraint, never a trigger. Historical break-even versus future
opportunity value is shown separately in the UI.

## 3. Replay harness (stage 2)

### 3.1 Experiments (paired counterfactuals, same window, same initial state, same actuator limits)

- **E1 — fidelity:** replayed engine vs. the recorded live decisions, within declared tolerances.
- **E2 — tariff error:** deterministic DP with the old vs. the new contract model.
- **E3 — terminal error:** `salvage` vs. terminal vector.
- **E4 — policy gain:** deterministic vs. expected-cost SAA vs. the candidate policies.

### 3.2 Accounting

- Initial and terminal pack energy are valued at one declared price (the cost basis), identical in
  every arm; profit = revenue − cost + Δ(stored value) at that valuation, reported both with and
  without the terminal-inventory term.
- Loss attribution is paired per decision window: Δprofit for the single lever changed.
- Uncertainty: block bootstrap over windows (autocorrelation-aware), reported as intervals.

### 3.3 Information discipline

- Only **as-issued** forecasts and decision-time tariff information; no future price beyond the
  published horizon.
- Chronological split: tune on an earlier window, evaluate on a later held-out window.
- Forecast vintages are captured prospectively from now; where archives are missing, results are
  labelled **empirical policy improvement**, not VSS.
- Every policy is replayed non-anticipatively: same information, same triggers, same corrective
  controller (§5).

### 3.4 Gate

No scenario, terminal or risk layer ships unless its measured out-of-sample gain exceeds its
complexity cost. The harness itself must first reproduce a known live day within tolerance.

## 4. Terminal value (stage 3)

The continuation value is a **vector over the stored-energy grid**, computed by a constrained tail
solve over the fetched price window.

**Structural sufficiency condition** (not a sampled test): the terminal vector may replace
`salvage` only when the omitted state cannot affect the tail's optimal value for any reachable
path, which holds when, over the whole tail:

- the sell floor is disabled, or it provably never binds for any reachable path (checked with the
  reachable energy and basis bounds), so the cost basis cannot change the tail's constraints; and
- the marginal credit rate is exogenous over the tail (§2.3 reachability), so the billing state
  cannot change the tail's rewards.

If either condition fails, the terminal mode is disabled for that plan, `salvage` is used, and the
reason is reported. No numerical sampling of the observed basis is used as evidence of
sufficiency.

**Tail boundary:** beyond the fetched window the tail extends with a periodic price profile and a
terminal `salvage`; the tail length is doubled in a convergence test and the horizon-end value must
be stable within tolerance.

**Tests:** small exhaustive enumeration over the complete reachable state domain versus the
DP+vector, and the `salvage` baseline as the reference arm.

## 5. Candidate layer and risk (stage 4 — MOTHBALLED 2026-10-18, see docs/STAGE4_FINDINGS.md)

**Status: not built, and not to be built without new evidence.** Stage 4's premise was that
candidate policies produce materially different plans, so a selector has something to choose
between. The first dimension of that premise — whether PV/load inputs change dispatch at all — was
tested on the live recording before any code was written, with deterministic input perturbations,
and no feasible plan power value changed: PV- and load-scaled inputs move **no** plan power value
(exactly 0.0 W, unrounded, in 41/41 faithfully reconstructed windows, and in the severity sweep down
to PV = 0 and up to load = 5x). That is a statement about deterministic input perturbations only —
not about scenario objectives, recourse or any uncertainty-aware policy, none of which were
tested.

The price-side and floor variants *do* produce different plans, so the premise is not false in
general; what is established is that the tested PV/load dimension supplies no demonstrated
selection benefit, and that no candidate layer is worth building on this evidence. The observations
are consistent with grid cost being separable in flows and with
non-decisive PV/load-dependent constraints; they do not establish that the feasible set is
PV/load independent (it demonstrably is not under `no_export`, where the same perturbation does move
power — though those plans are themselves infeasible on this instance). The measurement, its limits,
and the re-run recipe are in **docs/STAGE4_FINDINGS.md**; the probe is
**tools/candidate_probe.py**. Reconsider only on representative new data or a materially different
price/risk objective — see that note's section (i) for the exact conditions. The design below is
retained as the record of what was intended, with the amendments the review forced, so a future
revival does not restart from the rejected version.

The precondition "only if stage 2 justifies it" was circular — stage 2 cannot price a candidate
layer that does not exist — so it is resolved by measuring first, in a replay-only probe, before
any production surface is added.

### 5.1 Candidates are policies, not paths

A candidate is an executable non-anticipative **policy**: at every replan it re-solves the DP with
its own objective/inputs and returns the current slot's action, which reaches the actuator through
the ordinary dispatcher path. The candidate identity is the objective/scenario set used at each
re-solve, never a stored action path:

- deterministic policy — point forecast, today's objective;
- **`expected_inputs`** policy — the DP solved on scenario-**mean inputs**;
- per-scenario policy `s` — the costs of scenario `s`.

The middle candidate was called "expected-cost SAA" until review. It is not: the DP's constraints
are not affine in the inputs, so scenario-dependent feasibility and transitions mean mean inputs do
**not** produce expected cost — the name `expected_inputs` is the honest description of a heuristic,
and true stochastic optimisation is deferred. Note also that a per-(state, action) scenario-*cost*
sweep in the accelerator would not by itself deliver it: scenarios that change feasibility or the
successor state need recourse and transition handling, which is a different and much larger change
than a cost sweep.

Each policy is scored by replaying that exact policy through the same MPC loop: same replan
cadence and triggers, same clamps, and the resulting post-clamp SoC and ledger transitions. No
candidate is scored as an open-loop path, and no scenario-specific future action is ever used at
execution time.

### 5.2 Selection

At each replan, generate the candidate policies' current actions, evaluate them under the frozen
risk objective, and execute the winner's action for the slot; repeat. Selection is
`min over candidates of (1−λ)·E[total cost] + λ·CVaR_α(total cost)`, labelled **best among
candidates**, with λ ∈ [0, 1]; λ = 1 is the CVaR-only endpoint (of the empirical tail, not a
general worst-case objective) and λ = 0 is pure min-E. (An earlier draft called the
traditional `E + λ·CVaR` form wrong. It is not: for λ ≥ 0 that form equals
`(1+λ)·[(1−β)·E + β·CVaR]` with β = λ/(1+λ), i.e. the same ranking reparameterised. The mixture is
used here because λ is directly interpretable as a tail weight, not because the other form is
invalid.) A stability margin δ on the incumbent is applied when the winner changes, so an
indistinguishable improvement does not flip the plan; δ is a measured margin, **not** a switching
cost, since the physical wear of switched actions already enters each rollout's own cost. At most
one candidate draw is added, and only if measured held-out value justifies widening the set.
Each candidate's cost distribution is obtained by **closed-loop policy rollouts per scenario** —
the candidate policy is re-solved at every rollout replan and driven through the same clamps — and
each rollout holds **one fixed candidate**: a rollout that re-runs the selector would be a
recursive, undefined process, and the composite selector's own switching effect is measured
separately, in selector replay, rather than assumed here. The selector itself is a policy: it is
the thing replayed in §3 and gated in §8, and because CVaR is not time-consistent, the design
states plainly that switching candidates every
slot yields a composite policy whose realised risk is the *replayed* selector's risk, not the risk
of any single candidate. Probabilities, the CVaR convention, `α` and admissible `λ` are frozen in
the SPEC amendment before any comparison. Cut: distorted-probability reweighting and per-slot
weighted-median consensus.

`optimizer.risk_aversion` selects among candidates; it does not promise a sale above break-even. A
guaranteed realisation is a **separately named trigger policy** with its measured profit tradeoff,
explicitly opt-in.

## 6. C acceleration (stage 1a)

- **Budget:** the 120-slot solve must run **< 20 ms** on the live amd64 host with the default grid
  (<= 400 states, `power_steps` 24) — the Python reference is ~0.4 s, so the target is ~20x; the C
  sweep is expected to be far below it. The benchmark is a gate, measured in the add-on image, and
  the per-architecture expectation is documented.
- **Surface:** one deterministic Bellman sweep + path reconstruction over validated flat arrays.
  Tariffs, scenario construction, risk selection and orchestration stay in Python. GIL released
  for parallel scenario solves; the Python DP stays the differential oracle. The core also returns
  its state grid and its full value function (native-double bytes) so the loader can verify the
  decision and the backward sweep, and it rejects unsupported dimensions or numerically
  pathological bounds/efficiencies with an explicit error instead of narrowing or overflowing.
- **Correctness:** differential tests with numerical tolerances, malformed-input and
  boundary-dimension checks, build/import/fallback coverage on all five musl architectures.
  **Every solve is verified before a plan can use it:** lengths, finiteness (including every entry
  of the value-function buffer, with non-finite operands and accumulators failing closed before
  any comparison) and the returned grid are checked against an independently rebuilt reference
  grid; every returned power must lie in the SoC/grid bounds derived with the reference helpers and
  the returned windows must match; every *unclamped* transition (terminal included) must stay
  inside `[e_min, e_max]`; the objective and terminal error are re-derived from the reconstructed
  path and the reported cycle cost must equal the wear accumulated over that path; and for every
  slot the returned power must **equal** the reference's own choice — the same action set, the same
  strict argmin (ties resolve to the lowest power), the same grid-snapped stored decision and the
  same reconstruction clamp, so an off-action or tied-but-higher power is rejected even when its
  cost ties. The terminal value row and a sample of `V_i[k]` entries are recomputed from `V_next`
  (backward Bellman), with non-finite values failing closed. **Full equivalence** with the unchanged
  Python reference is cross-checked on the first solve after load and every 20 solves
  (`_fast._CHECK_INTERVAL`, configurable, 0 = off). Between cross-checks the guarantee is therefore
  feasibility + strict decision consistency + self-consistency + sampled backward consistency, not
  a full re-derivation of the value function; the periodic full check bounds the residual risk. On
  any failure at any of these layers the extension is disabled for the process and the solve is
  redone in Python; an extension that returns incorrect results is **never** treated as equivalent
  to an absent extension. The fallback is used only when the extension is absent, fails to import,
  or fails this verification.
- Build deps are added and removed in the Dockerfile; a failed compile leaves the add-on fully
  functional on the Python fallback.
- **Gate:** the budget benchmark, the differential suite, malformed-input / boundary-dimension /
  runtime-verification / cross-check tests, and the exercised fallback. No dispatch-behaviour
  change: the C core is verified against the Python reference and ships as an accelerator only.

## 7. Compatibility, safety and rollback

Compatibility is a property of a complete configuration, not of `λ = 0`: enabling scenarios,
changing the feed-in model or the terminal mode can change the schedule at zero risk aversion.
Every new setting defaults to today's behaviour; the UI shows expected monetary profit separately
from the risk-adjusted objective.

Because every stage can change live battery commands, **each gate requires**:

- a **shadow run** (compute-only) with a plan diff against the current v1 schedule before
  actuation is enabled;
- a documented **kill switch** that reverts the whole configuration to v1 behaviour in one step;
- **fail-safe actuator behaviour** when the solver raises, times out, refuses or sees stale
  inputs: hold, or self-consumption-only when a hold is unsafe, with tests for each path.

## 8. Stage gates (acceptance criteria)

1. **C core (stage 1a)** — the < 20 ms benchmark, differential correctness and tolerance tests,
   malformed-input checks, runtime output validation with automatic fallback, architecture
   build/import coverage, exercised fallback. No dispatch-behaviour change; the Python reference
   stays authoritative.
2. **Tariffs (stage 1b)** — SPEC amendment with the settlement functions, the reachability rule
   and the billing-state transitions; tests for threshold crossings, tax treatment, negative
   prices, equality, `sell > buy`, missing/stale state, the PV-surplus-plus-recharge reachability
   regression, forced-PV-export pricing (negative feed-in, the `buy = −0.20` saldering case, and a
   tiered contract whose rates fall below the single-formula value), the confirmed-vs-unconfirmed
   tax component (refund off with a default/unknown component refuses; with a confirmed zero and a
   confirmed non-zero it prices `sell = buy − tax`), and refusal paths; whole-schedule costs
   verified against independent contract calculations; recorded evidence of the live contract's
   netting convention; a minimal replay fixture resolves the sequencing dependency; shadow run,
   kill switch and fail-safe actuator tests.
3. **Replay** — reproduces live decisions and execution within declared tolerances; forecast and
   tariff availability timestamps verified; counterfactual actuator transitions, common terminal
   accounting and missing-data handling specified; held-out chronological evaluation with
   dependence-aware intervals; no historical VSS claim without valid vintages.
4. **Candidates/risk** — policy semantics frozen and replay-scored with closed-loop rollouts; the
   selector itself is the gated policy; scenario probabilities, CVaR convention, `α` and admissible
   `λ` defined; deterministic candidate retained; identical accounting and information constraints;
   held-out improvement on the declared objective with the monetary tradeoff reported separately;
   default off with a deterministic fallback; the many-variant search uses the stage-1a C core.
5. **Terminal** — the structural sufficiency condition is implemented and tested, including the
   disabled path; small exhaustive tests plus the salvage baseline; credible out-of-sample net
   benefit, not a positive point estimate.

## 9. Open questions for the reviewer

1. For `saldering`, is import-first ordering with the eligible/excess split the right single
   supported convention, and is rejecting the others acceptable?
2. Is exact-or-refuse (§2.3) the right call versus shipping an exact extended-state DP later?
3. Is the structural sufficiency condition (§4) the right bar for the terminal vector, or should
   the tail carry the basis explicitly in all cases?
