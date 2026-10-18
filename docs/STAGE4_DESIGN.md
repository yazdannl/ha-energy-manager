# Candidate and risk layer (stage 4) — implementation design

> **Status 2026-10-18: MOTHBALLED, not built.** The first dimension of the gating premise — whether
> PV/load inputs change dispatch at all, so a scenario- or risk-aware candidate has something to
> select on — was tested before implementation with deterministic input perturbations, and no
> feasible plan power value changed in this recording: `pv`- and `load`-scaled inputs move no plan
> power value (exactly 0.0 W, unrounded), in the live configuration and under a binding sell floor
> alike. That is a statement about deterministic perturbations only, not about scenario objectives,
> recourse or any uncertainty-aware policy, none of which were tested. The premise is therefore
> **not** false in general: the price-side and floor variants *do* produce different plans (all
> feasible), so what is established is that the tested PV/load dimension supplied no demonstrated
> selection benefit on this evidence, and the layer is mothballed for lack of demonstrated benefit
> rather than because candidates cannot differ. See **docs/STAGE4_FINDINGS.md** §(i) for the
> reconsideration conditions (probe: `tools/candidate_probe.py`). This note is retained as the
> design record, including the review-driven amendments, so a revival does not restart from an
> unpicked-apart draft. Two claims it originally made about the spec were themselves wrong and are
> corrected in §9.

Semantics are declared by `docs/OPTIMIZER_V2.md` §5 (rev 7). This note fixes the *seam*: what a
candidate is, where its scenarios come from, how selection is scored and gated, what it costs, and
what it refuses to do. It is written to stand alone; a reader who has not read §5, the replay
harness note or the spec should still be able to implement and audit it from this file. Anything
here that contradicts §5 is a bug in this note — and §9 lists the places where this note does
contradict §5 on purpose, with the reason.

The one-line shape: **candidate policies each produce their current action at every replan; the
candidates are scored by closed-loop rollouts and never as open-loop paths; the winner is the one
with the best risk-adjusted cost; the whole layer defaults off and may act only when a replay on
this instance's own recording says it pays.**

## 1. What the layer is, and what it is not

- **It is a selector over executable policies.** At each scoring epoch the layer builds a small set
  of candidate policies, evaluates each one by simulating the *whole remaining horizon* under each
  scenario with that candidate's own re-solves driving the plant, and publishes one winner. Only the
  winner's plan reaches the dispatcher.
- **It is not a path optimiser.** No candidate ever stores or replays an action path. A candidate is
  an input transform fed to `optimize/dp.py`'s existing solve; the action executed is always the
  first slot of a plan that candidate just computed from the current state.
- **It is not a new simulator.** The rollout's actuator, state of charge and cost-basis ledger use
  the same production code as the replay harness: the dispatcher's plan-to-power mapping
  (`Dispatcher._slot_power`), the plant's integration and clamps (`app/replay/plant.py`), and the
  ledger transition (`cost_basis`). §6.3 names the two small seams that requires; a roller that
  re-implemented any of them would measure itself.
- **It is off by default, and off means absent.** See §8.

## 2. Cadence: where this layer is allowed to run

Cadence is not an incidental detail here; it decides whether the layer is affordable at all.

| loop | cadence | source | solves? |
|---|---|---|---|
| control tick | `control.tick_seconds`, default **30 s**, floor 5 s | `Dispatcher.tick_interval_s()` | **never** |
| replan | `control.replan_minutes`, default **15 min**, floor 60 s | `Dispatcher.replan_interval_s()`, `Engine._replan_reason` | one per candidate action |
| guard | 2.0 s | `_guard_loop` | never |
| scoring battery | derived, **at most once per hour** and only when the plan-basis fingerprint changed | this note | many |

- The **control tick never solves**: it re-dispatches the stored plan's current slot. Scoring is
  therefore not on the tick path at all, and the "5-second tick" the design was briefed against does
  not constrain the battery's arithmetic.
- Scoring must still be **off the control thread**, because a long stall is not harmless:
  `Dispatcher.stalled()` fires when the control thread has not ticked within 3 tick intervals, and
  the guard then runs `watchdog()`, which **holds every battery** at 0 W with reason `STALE_STATE`
  and journals `watchdog_hold`. A 60 s score on the control thread at the default 30 s tick would
  trip that and turn a decision aid into an outage.
- The battery therefore runs on its own lazily started daemon thread, **chunked**: at most
  `CHUNK_SOLVES = 200` solves (≈3.4 s at 17 ms) between yields, then a 50 ms sleep. Any single
  burst stays far below the 90 s stale threshold, and a burst cannot delay a tick past one interval
  on the reference host.
- The scoring cadence is derived, not configured: score when the plan-basis fingerprint changed
  since the last battery, or when the last score is older than `max(3600 s, 4 × replan_interval)`,
  or when no winner has ever been produced. The winner's **action** is still re-solved by the winner
  at every replan; only the *ranking* is refreshed hourly.
- The battery's hard wall-clock budget is **80 s**. On overrun it is abandoned, the previous winner
  is held (first-ever battery: `det`), and `selection_budget_exceeded` is journalled. At the start
  of each battery the layer calibrates on 3 solves and refuses up front when the estimate exceeds
  the budget, so a slower host degrades to "never selects", never to "selects on stale arithmetic".

## 3. Candidates are policies

A candidate is a **named input transform** with the signature
`inputs(context) -> (slots, options)` consumed by `dp.solve`. Its identity is that transform; its
action at instant `t` is the first slot of the plan it computes from information available at `t`.
Every candidate is executable, non-anticipative and has its own re-selection rule (it re-solves with
its own inputs at every replan and takes its own current slot's action), and every candidate's
action reaches the actuator through the ordinary dispatcher, SoC and ledger paths.

The shipped set is six ids, in a fixed evaluation order (ties break to the lowest index, then to the
incumbent):

| id | objective / inputs | needs a scenario set? |
|---|---|---|
| `det` | **the deterministic baseline**: point forecast, current objective, today's settings | no |
| `expected_inputs` | scenario-mean buy/sell and scenario-mean PV/load | yes |
| `robust_p90` | per-slot 0.9-quantile buy and load, mean sell/PV | yes |
| `scenario_peak` | the draw with the highest total buy cost, verbatim | yes |
| `scenario_lowpv` | the draw with the lowest total PV, verbatim | yes |
| `no_export` | `det` with `allow_export=False` | no |

- `det` is the incumbent and the fallback. When the layer is off, when evidence is missing, or when
  fewer than two candidates are available, `det` is what runs — not a new code path, the existing
  one.
- `no_export` is deliberately scenario-free: on a fresh install, with no residual history, the layer
  can still compare `det` against a materially different executable policy.
- **No candidate re-simulates the selector.** A rollout holds its candidate fixed for its whole
  length: at each rollout step the candidate re-solves with its own inputs and takes its own action;
  it does not re-rank the candidate set. The `det` candidate is therefore not "the same policy with
  the same objective re-solved" — the §5 objection to a single-policy candidate set is about a
  candidate *set* that collapses to one policy, and this set does not: `expected_inputs`,
  `robust_p90`, `scenario_peak`, `scenario_lowpv` and `no_export` each produce different actions
  from the same state. The recursion in "re-selected exactly as it would run" is rejected in §9.3.
- Only one candidate's actions ever touch the hardware: the winner's. The candidate *set* is
  evaluated in the battery, which is pure computation against a simulated plant.

## 4. Scenarios

- **Sources, in order.** (a) Realized forecast-error pairs: solar and load from `forecast_archive`
  (`kind`, `source`, `target_ts`, `issued_at`) joined against the realised values in `actuals`;
  price from the recorded plan rows (`plan_slots.buy` as issued) joined against `actuals.price`.
  (b) A documented residual bootstrap when there are too few admissible pairs — bucket residuals by
  lead time (hours-to-target), draw with replacement, add to the current point forecast.
- **The set is `S = 5`**: one `central` member (the point forecast, weight 1/5) plus four draws.
  Draws use a seeded RNG whose seed is recorded; the generator is a pure function of
  `(store rows, seed, t)`, so a replay reproduces the set bit-for-bit.
- **Non-anticipation rule.** A residual or vintage pair is admissible only when its `target_ts <= t`.
  A pair whose target is in the future relative to the decision instant is excluded even when its
  `issued_at <= t`. No scenario may be built from a *future* action or a future realisation, and no
  scenario's action path is ever executed: scenarios only re-price and re-shape the horizon the
  candidate solves.
- **Staleness is copied, never cleared.** A scenario may add a price residual to a stale slot (that
  is where in-horizon price uncertainty actually lives), but the `price_stale` flag is copied from
  the epoch vintage unchanged, so the dispatcher's stale-price stand-down and the all-stale refusal
  behave exactly as they would without the layer. If every remaining slot is stale, the epoch
  refuses (`scenario_vintage_stale`), because scoring a plan the dispatcher would refuse is
  meaningless.
- **Price uncertainty is mostly absent inside the horizon, and the design says so.** Day-ahead
  prices are published for the whole 30 h horizon, so `robust_p90` on buy collapses toward `det`
  inside the published window and price residuals only meaningfully apply where the vintage is
  stale. The real in-horizon uncertainty is PV and load; the layer is honest about that by naming
  `scenario_lowpv` and by treating price draws as tail information.
- **Fresh install.** With no admissible pairs the bootstrap is unavailable and the scenario
  generator refuses (`scenarios_unavailable`). `expected_inputs`, `robust_p90`, `scenario_peak` and
  `scenario_lowpv` are dropped; the layer then compares `det` against `no_export` only, and if that
  comparison is the whole set it still counts as two candidates.

## 5. Selection and risk

- **Objective.** The selector minimises `(1-λ)·E[cost] + λ·CVaR_α(cost)` over the candidates, with
  `cost = -profit` accumulated over the rollout at the scenario's own prices, wear included, and
  terminal inventory valued exactly as the arm's accounting values it. `optimizer.risk_aversion`
  is λ, a new setting, **default 0.0**, admissible range `[0, 1]`. At λ=0 CVaR is inert and the
  selector is pure min-E.
- **Why the convex mixture, not `E + λ·CVaR`.** An earlier draft called the spec's form wrong
  because it "double-counts the mean". It does not: for λ ≥ 0,
  `E + λ·CVaR = (1+λ)·[(1−β)·E + β·CVaR]` with β = λ/(1+λ), so the two are the same *ranking*
  reparameterised, and λ is not price-scale dependent. The mixture form is adopted here for one
  reason only: λ is then directly readable as the weight on the tail, with λ = 0 pure min-E and
  λ = 1 the CVaR-only endpoint (the empirical tail above α — the worst draw at S=5 — not a general
  worst-case objective).
- **α is derived, not configured.** `α = 1 - 1/S`, so with S=5, α=0.8 and CVaR is the worst draw's
  cost; λ=1 is then CVaR-only at that empirical tail edge, which is not a general worst-case
   objective. There is **no α setting** in this stage: an
  empirical CVaR at α=0.95 needs at least 20 samples to be anything but a single point, and 20
  samples would cost 14 400 solves (≈4 min) per epoch. `robust_p90` inherits the same
  sample-size caveat: with S=5, a 0.9-quantile is effectively the maximum.
- **Time-inconsistency is accepted, explicitly.** CVaR is not time-consistent, so the per-epoch
  ranking need not agree with the risk of the composite policy that actually runs. The design
  therefore claims nothing about coherence: the selector is a Markov policy with a fixed
  remaining-horizon risk window, no risk number is shown as a promise, and the gated object is the
  **replayed selector's realised cost**, never a candidate's or the selector's CVaR.
- **Hysteresis is mandatory, and was missing from the spec.** A challenger displaces the incumbent
  only when its risk-adjusted cost is lower by

  ```
  δ = max(0.0025 · |E_incumbent|, 2 · wear_eur_per_kwh · (e_max_kwh - e_min_kwh))
  ```

  i.e. a quarter of a percent of the incumbent's horizon cost, or two full cycles of wear at the
  fleet's own wear constant, whichever is larger. The DP charges wear on discharge but nothing for
  *switching* the policy, and a selector that flips between near-equal candidates pays real
  round-trip losses the objective never sees.
- **Failure modes.** λ too high (or a small S with α at its floor) makes the worst single draw
  dominate: the selector becomes timid and forgoes spread, which shows up as an opportunity cost on
  the `det`-paired delta, not as a crash. λ = 0 makes CVaR dead weight but is the safe default. δ too
  high freezes the incumbent; too low churns. All three are visible in the replay report as paired
  per-window deltas and switch counts; none is silent.

## 6. Cost, caching and the terminal row

### 6.1 Measured cost

Reference host (amd64, C core built): one solve 17 ms; the explicit full-differential mode 50 ms;
Python reference 2 s. A battery over the remaining horizon uses `N = remaining / slot_minutes`
decision epochs (120 for a fresh 30 h horizon at 15-minute slots), `C = 6` candidates and `S = 5`
scenarios, i.e. `C · S · N` solves:

| battery | solves | C core | full differential | Python fallback |
|---|---|---|---|---|
| fresh horizon, N=120 | 3 600 | 61 s | 180 s | 2 h 0 min |
| last hour, N=4 | 120 | 2 s | 6 s | 4 min |
| one horizon of hourly scoring | 55 800 | 949 s (31.6 s/h, **0.9 % duty**) | 46 min | 31 h |

- Rollout solves use the production entry point with its normal per-solve runtime validation
  (≈17 ms). The explicit full-differential mode is **not** run inside the battery; the periodic
  `_fast._CHECK_INTERVAL` cross-check still applies process-wide, and if it ever fails the
  accelerator is disabled, the battery aborts (`accelerator_absent`) and the layer falls back to the
  incumbent. The layer **never** falls back to the 2 s Python path inside a battery.
- Without the C core the layer refuses to score at all. A 2 s Python solve makes a battery two hours
  long — not a slow feature, an unavailable one. The same refusal applies to the live epoch: when the
  accelerator is absent or has been disabled, the layer withholds to `det` and never runs its own
  candidate solves, so it cannot inflate the live planning path.
- The 60 s first battery is the worst case; on a host where the calibration estimate exceeds the
  80 s budget the layer never selects. On the musl/ARM targets, where a solve can be several times
  the amd64 number, that means the layer stays effectively off — which is the honest outcome, and
  the reason the stage-1a benchmark is its gate.

### 6.2 Caching: what can be shared, and what cannot

A stage-3 continuation row is only usable by an accelerated solve when it is built on **the same
state grid the solve will use**, because that grid is anchored on the current SoC
(`_anchors(start_kwh, agg, e_target)`) and the C core rebuilds it internally and checks only the
row's *length* (a same-length row on a different grid would pass the length check and be silently
misaligned). Consequences:

- **Within one scoring epoch, one anchored row is shared by all six candidates and all five
  scenarios**, because they all start from the same SoC and the same tail: the tail is always built
  from the point-forecast window, never from a candidate's or a scenario's inputs, so the row does
  not vary across the candidate set. That saves `C-1` rows per epoch: ≈1.1 s shared versus ≈6.0 s
  unshared, an **82 % reduction** at that epoch.
- **It cannot be shared into the rollouts.** Every rollout step moves the simulated SoC, hence the
  grid anchor, hence the row. Reusing one row across rollout steps would be silently wrong (the
  length check does not catch it), and rebuilding it per step costs 0.98 s per step — 3 600 rows,
  ≈59 min per battery.
- **Invalidation key** for the shared row: canonical JSON digest of `(grid points, tail slots,
  tail minutes, tail params, aggregate, options, salvage, sell-floor/reachability bound,
  feed-in settlement, point-window vintage)` plus the settings fingerprint. Any change to prices or
  the vintage, the PV/load profile, the aggregate or its efficiency/wear, the reserve or grid
  bounds, `pv_charge_preference`, `min_spread`, the terminal parameters, the floor or the basis
  bound, or the feed-in wiring invalidates it.

### 6.3 Terminal-mode policy, and the acceleration verdict

The anchor problem forces a decision rather than a cache: **stage 4.0 requires
`optimizer.terminal_mode = salvage`.** A candidate rollout uses salvage on every solve, and the
winner's executed plan is solved with the same salvage treatment, so scoring and execution see the
same objective. `selection_mode ∈ {shadow, on}` with `terminal_mode = vector` refuses to run
(`terminal_mode_conflict`) instead of silently re-solving one side differently. This costs nothing
under the contract the add-on actually ships: with `sell = buy` and the sell floor enabled, the
floor bound `max(basis, buy_hi)/rt + wear + margin` exceeds `buy_hi` for any `rt < 1`, so the
terminal vector is refused by its own sufficiency check on every peak tail slot already.

**Tail acceleration is not required for stage 4 under this design, and would not fix it.** The
expensive object in a terminal-mode rollout is the row, not the tail sweep's speed: what would make
terminal rows affordable inside a battery is a **caller-supplied grid** on the C core (so one
canonical row could serve every state), and that is a stage-1a surface change with its own
verification burden. It would become required only if the layer were asked to score with the vector
row at every rollout step while keeping the 80 s budget; it should not be, in this stage.

## 7. The gate: default off, evidence-gated

- **Setting.** `optimizer.selection_mode`, a three-way choice `off | shadow | on`, **default
  `off`**, with an unknown stored value behaving as `off` (the `optimizer.feed_in_wiring` pattern).
  Plus `optimizer.risk_aversion` (λ, default `0.0`, range `[0,1]`). No other new settings: the
  scoring cadence, α, the scenario count and δ are derived.
- **Behaviour of each mode.**
  - `off` — nothing runs: no scenarios, no battery, no thread, no new journal events, and the module
    is not imported by the planning path at all. See §8.
  - `shadow` — the battery runs and publishes its result to the **journal only**. It never mutates
    `engine._plan`, never writes a plan, never touches `dispatch_log`, and never influences an
    action. It is a preview, not an evidence path.
  - `on` — the winner's action may drive the plan, and only after the evidence record below is
    valid.
- **Evidence record.** `on` additionally requires `meta["stage4_evidence"]`, a JSON record that
  **only the replay CLI may write**, via an explicit `--record-evidence` flag, and only when that
  run's own gates pass: arm `E4`, an episode fingerprint digest, the manifest digest, the candidate
  set, the scenario seed, the sample's first and last instant, the contiguous-window count, the
  plan-horizon count (`>= 5`), `publishable = true`, and a paired per-window interval whose lower
  bound is strictly positive. The CLI refuses to write anything else
  (`evidence_not_publishable`). The record carries the instance fingerprint it was measured under
  (settings + battery snapshot digest).
- **Missing, stale or invalid evidence.** If the record is absent, newer settings or batteries
  changed the instance fingerprint, the record is malformed, or the bounds are not positive, `on`
  runs the deterministic baseline: `det`'s plan is dispatched unchanged, `selection_withheld` is
  journalled with the reason, and an alert fires once. **An unmeasured gate is not a passed gate**,
  exactly as in the replay harness's own rule.
- **The layer cannot bootstrap itself.** The evidence must come from the harness's own gates, run
  against the code and settings under test on a contiguous recording, so the recording clock starts
  when the release that carries this code ships: a publishable record cannot exist on the day it
  lands, and `on` is structurally unreachable until roughly a week of contiguous uptime has been
  recorded and replayed. That is a property of the design, not a limitation to be worked around.
- **How a wrong selection can cost money.**
  1. A non-anticipative leak (a scenario built from a future target) would score a profit that
     cannot materialise — prevented by the admissibility rule and a test that injects future rows.
  2. A wrong scenario distribution (a regime shift, or residuals from a stale season) makes the
     winner under-price peak imports — mitigations: PV/load scenarios dominate in-horizon, the
     evidence gate is dynamic, and the replay reports the paired delta.
  3. Policy churn pays round-trip and wear the DP does not charge to switching — mitigated by δ.
  4. Small-sample CVaR at λ>0 makes the selector timid and forgoes spread — visible as an
     opportunity cost against `det` in the paired report.
  5. Score/execution mismatch, if the terminal treatment differed between the battery and the live
     solve — mitigated by the salvage requirement in §6.3.
  6. Rollout ledger divergence from the live ledger (a basis that the live path would not have) —
     mitigated by sharing `cost_basis`'s transition code and by an equivalence test.
  7. A silently wrong accelerator poisons every candidate score — mitigated by the existing
     per-solve validation and periodic differentials; a failure disables the accelerator and aborts
     the battery rather than degrading it.
  8. A candidate that the physical plant cannot execute (ramp limits, cell-voltage emergency
     charge) looks best on paper — mitigated by driving the plant's own clamps and reading its
     `last_infeasible` verdict in scoring.
- **Recording, for attribution.** Per scoring epoch the layer journals one `selection` event
  (candidates considered, per-candidate `E` and CVaR, winner, scenario seed, battery fingerprint,
  the shared-row digest or the reason it was not used), a `selection_switched` event on a winner
  change, and the refusal slugs of §11 as they occur. When `on` executes a plan, the winner id and
  the scenario seed are also written into `plan.sources["selection"]` /
  `plan.sources["selection_seed"]`, so a window's plans carry the policy that produced them.
  Because the scenario generator is a pure function of `(store, seed, t)` and the seed is recorded,
  a replay with `selection_mode = on` reproduces the winner sequence, and the selector's delta is
  the paired, per-window difference against the `off` leg on the same window, same initial state,
  same plant.

## 8. What happens when the layer is off

`off` is a hard, auditable no-op:

- `Engine._replan` takes its existing branch; no candidate module is imported, no scenario is
  generated, no extra solve is performed, no setting is read that changes a decision.
- No selection thread is created, no `selection` journal row is written, no `plan.sources` key is
  added, and no store row is written.
- The deterministic baseline is not merely equivalent: **it is the same code path**, so the plan,
  `plan_slots`, `dispatch_log` and journal of an `off` run are byte-identical to a run of the
  pre-stage-4 build.
- **How that is proven**, without the full suite: a golden-trace test drives a fixed episode through
  `Engine._replan` with `selection_mode = off` and compares the serialized `Plan` (including
  `sources`), the persisted `plan_slots`, the `dispatch_log` rows and the journal event list against
  a capture taken from `main`; a second test asserts the candidate module is absent from
  `sys.modules` after a full off-mode replan and that no `selection` thread exists. `shadow` is
  covered by the same plan-level assertions (plans unchanged) plus a test that its only write is the
  journal.

## 9. Corrections to the spec (§5), each with its reason

1. **The risk objective: the spec's form is not wrong, but the mixture is preferred.** An earlier
   draft of this note claimed `min E + λ·CVaR_α` double-counts the mean and is scale-dependent. Both
   claims are false: for λ ≥ 0 the form equals `(1+λ)·[(1−β)·E + β·CVaR]` with β = λ/(1+λ), i.e.
   the same ranking under reparameterisation. The mixture `(1-λ)·E + λ·CVaR_α` with λ ∈ [0,1] is
   proposed because λ is then interpretable as a tail weight and the endpoints are clean, not
   because the spec is invalid. What the spec does need is a stated admissible λ domain.
2. **"Expected-cost SAA" is the wrong name for what a mean-input solve computes.** The DP's cost
   is affine in per-slot prices, but its *constraints* (PV charge preference, grid bounds, the
   cost-basis floor, `min_spread`) are scenario-dependent, so a solve on scenario-mean inputs is
   not the expected-cost optimum. The candidate is therefore named `expected_inputs` and true
   stochastic optimisation is deferred. Note that adding a per-(state, action) scenario-*cost*
   sweep to the accelerator would **not** be a complete remedy either: scenarios that change
   feasibility or the successor state need recourse and transition handling, so the information
   timing and the recourse structure have to be defined before such a surface is designed.
3. **"re-solved and re-selected exactly as it would run" is an undefined recursion.** Simulating
   the selector requires the selector's own future scores, which requires simulating the selector;
   the rollout bottoms out by holding a fixed candidate, and the composite selector is *measured* in
   replay, not modelled (which is exactly what §5.2's own closing sentence already concedes).
4. **The switching cost is missing.** Add the hysteresis threshold δ of §5; without it the selector
   can alternate between near-equal candidates every epoch and pay real round-trip and wear that no
   candidate's objective charges.
5. **The §5 precondition is circular.** "Only if stage 2 justifies it" cannot be satisfied, because
   stage 2's E4 is itself the candidate comparison that needs this layer. Resolution: the layer's
   candidate, scenario and risk code ships **replay-usable and `shadow`-only** first, so E4 can
   produce the justifying evidence before any enablement exists.
6. **Ship recommendation.** Do not enable `on` under the current contract. With `sell = buy`, the
   sell floor on, and day-ahead prices known for the whole horizon, the achievable gain over the
   deterministic baseline is expected to be unmeasurable, and the terminal lever is inactive by its
   own sufficiency check. The layer is worth shipping as a **measurement instrument and a documented
   refusal** — it turns "does risk-aware selection pay here?" into a gated experiment that can
   answer *no* with evidence, and it amortises the safety machinery needed if the answer is ever
   *yes*. It is not worth enabling, and the tail-acceleration work should not be spent on it.
   Concretely: `off` by default, `shadow` available, `on` unreachable until a publishable
   ≥5-horizon recording shows a positive lower bound.
7. **This whole layer is now moot, and that is a measurement, not a change of mind.** Item 6 was the
   recommendation before the probe ran. The gating premise it rests on was then measured directly
   (see `docs/STAGE4_FINDINGS.md`): the tested PV/load dimension changes no dispatched watt (0.0 W
   exactly, 56/56 windows, all severities, under a binding floor), while price-side and floor variants
   *do* differ, so the premise is not false in general and the layer is mothballed for lack of
   demonstrated benefit rather than because candidates cannot differ. §5's "only if stage 2 justifies
   it" precondition is answered *no* on this evidence, and §(i) of the findings note lists what would
   reopen the question. Nothing here should be implemented pending that.

## 10. Test and evidence plan

**Provable now** (unit/property/differential, each command well under the 45 s budget, no full
suite):

- **Off equivalence**: golden-trace byte comparison of plan/`plan_slots`/`dispatch_log`/journal
  versus `main`; no candidate import; no selection thread. (`tests/test_stage4_off.py`)
- **Degenerate collapse**: a scenario set equal to the point forecast (all draws identical to
  `central`) must make the selector return `det` bit-for-bit, at every epoch of a synthetic
  horizon. This is the strongest correctness statement available without a week of data: the layer
  may not invent a gain where the future is known.
- **Candidate identity**: each candidate's plan differs from `det` only through its declared inputs;
  `no_export` never exports to the grid; `det` is byte-identical to the pre-layer solve.
- **Determinism and ties**: same fingerprint and seed ⇒ identical winner sequence; ties resolve to
  the incumbent, then to the lowest id; `λ=0` ⇒ min E.
- **Non-anticipation**: inserting a store row with `target_ts > t` (and `issued_at <= t`) cannot
  change the scenario-set hash or any candidate action; a stale-everywhere vintage refuses.
- **CVaR**: Rockafellar–Uryasev versus brute-force enumeration on small samples; λ=1 ⇒ the CVaR-only
  endpoint above (the empirical tail, not a general worst-case objective);
  monotonicity in λ; the α derivation rule.
- **Hysteresis**: a challenger below δ never switches; the incumbent changes only on a δ-clearing
  gain; a churn-prone synthetic pair does not oscillate.
- **Caching**: the shared row is regenerated when, and only when, any invalidation-key component
  changes; a same-length row on a different grid is refused by the battery (the alignment guard).
- **Budget**: the N=120 battery with the C core completes under 80 s on the reference host; the
  calibration refusal fires without `_fast`; chunking never exceeds 200 solves per burst.
- **Ledger/plant equivalence**: N identical steps in the roller and in a replay leg with the same
  commands produce identical SoC and basis rows.
- **Gate**: absent, stale-fingerprint, malformed, short, gapped or non-positive records all yield
  `selection_withheld` and the baseline plan; only the replay CLI can write the record; `shadow`
  writes only journal rows.
- **Replay integration**: an E4 arm with a pinned candidate patch, gates 1–3 in the same report.

**A reviewer should refuse:**

- any benefit claim from fewer than 5 contiguous plan horizons (≈6.2 days), or any interval that
  spans `gap_windows`;
- any point estimate without a chronological holdout;
- any "VSS" label, and any gain measured against anything but a paired same-window `det` arm with
  the same initial state and plant;
- any gain measured with the C core absent, or with a pinned candidate rather than the replayed
  selector;
- any claim that the composite selector's realised risk equals any candidate's CVaR
  (time-inconsistency);
- any enablement argument from a single week: one week is one weather regime, and it is the *floor*
  for evidence, not a sufficiency claim.

## 11. Refusal slugs

`selection_disabled` (informational), `selection_withheld` (evidence absent/stale/invalid),
`evidence_not_publishable` (CLI write refused), `terminal_mode_conflict`, `accelerator_absent`,
`scenarios_unavailable`, `scenario_vintage_stale`, `selection_budget_exceeded`,
`last_candidate_standing`, `core_differential_failed`. Each is journaled with the epoch's
fingerprint; each leaves the previously validated winner in force.

## 12. Non-goals and open items for the critic

**Non-goals.** No live behaviour change in `off` or `shadow`; no third-party dependency; no UI or
API surface; no true SAA; no terminal row inside rollouts; no per-install α tuning; no VSS number
without valid vintages.

**Open items.**

1. Is refusing `terminal_mode = vector` while the layer is on the right call, or should the layer
   simply force salvage for the *battery* and let the live solve keep the vector row (accepting a
   bounded score/execution mismatch)? The note chooses refusal.
2. Is the caller-supplied-grid change to the C core ever worth it, or should terminal rows and the
   candidate layer stay permanently exclusive?
3. Is the 80 s battery budget with 200-solve chunks the right ceiling, given the guard's 90 s
   `stalled()` threshold and a possible 2–4× slower ARM host?
4. Should the evidence record live in `meta` (written by the replay CLI on the live store) or in a
   digest-pinned side file, and should its writes be audited through `settings_history`?
5. Per-scenario candidates are `scenario_peak` and `scenario_lowpv` only (K=2, keeping C=6). Is a
   third per-scenario policy worth its `S·N` solves, or does it just duplicate `det` under a
   different draw?
6. Candidate pinning for replay arms needs either a hidden `optimizer.candidate_pin` defaulting to
   `None` or an arm-only allowlist in `replay/manifest.py`'s `validated_patch`; the note recommends
   the allowlist. Confirm that does not violate "no new setting without a default-behaviour
   statement".
