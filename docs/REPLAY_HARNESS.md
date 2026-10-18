# Replay harness (stage 2) — implementation design

Semantics are fixed by `docs/OPTIMIZER_V2.md` §3 (rev 7); this note fixes the *seam*: which
production code the replay drives, what the plant is, how accounting is computed, and what the
harness refuses to do. Anything here that contradicts §3 is a bug in this note.

## 1. Drive the production engine, not a copy of it

The harness constructs a real `Engine` and steps it through a virtual clock, so what is measured is
the production planning path: replan triggers and cadence, dispatcher, safety layers, cost-basis
ledger, journal. A harness that re-implemented the loop would measure the harness.

Two small, behaviour-preserving production seams are required, because the engine cannot be driven
headlessly without them:

1. **Step methods.** The three loop bodies started by `start()` (`sem-control` → `_control_tick`,
   `sem-replan` → the body of `_replan_loop`, `sem-guard` → the body of `_guard_loop`, which owns the
   watchdog hold, throttled alerts, `_maybe_maintenance` and `publish_states`) are split into
   `_control_step(now)`, `_replan_step(now)`, `_guard_step(now)`; each thread keeps its `while` loop
   and calls its step. `start()` is **never** called: threads plus a virtual clock would be a race,
   and `_replan` is unreachable from `_control_tick_unlocked` except on the first-battery-read path.
2. **`driver_factory`.** `device_client_factory` is the WebSocket device-registry client, not the
   battery path: drivers come from `driver_registry.create(prepared, ha, clock=…)` inside
   `_sync_drivers`. `Engine.__init__` gains
   `driver_factory: Callable[[BatteryConfig], BatteryDriver] | None = None`, used by `_sync_drivers`
   in place of `driver_registry.create`; `None` keeps today's behaviour. Serving the plant through a
   fake `HAFacade` instead is not enough: `create()` is called without `sleep=`, so `BaseDriver`'s
   read-back would spend real `time.sleep` while the virtual clock is frozen.

### Cadence (it is part of what is measured)

`Runner.step(now)` is called once per virtual second and dispatches the production schedule, not one
call of each step:

| step | production cadence | source |
|---|---|---|
| `_control_step` | `control.tick_seconds` (default 30 s, frozen per episode) | `Dispatcher.tick_interval_s()` |
| `_guard_step` | every 2.0 s | `_guard_loop`'s `self._stop.wait(2.0)` |
| `_replan_step` | every 1.0 s, but the engine itself decides via `_replan_reason` (`replan_interval_s`, ≥60 s) and the `_replan_gap_s` backoff | `_replan_loop` |

The applied power is ramp-clamped per control tick (`RAMP_LIMIT_W`), so a replay at the wrong
cadence cannot match live decisions for reasons the tally would misattribute; E1 is only meaningful
with the frozen tick interval, and the report prints it.

### Frozen state, and how a vintage is chosen

Declared per episode, because each one changes decisions: `mode`/`dry_run`, manual limits, the
battery config snapshot, the settings values, and every `learning_profiles` row that feeds a
decision (`control`/`live_writes`, `control`/`cell_voltage_latch`, `full_charge`/…, `capacity`/…,
`cost_basis`/…, `load`/`profile`, `pv_bias`/`hourly`).

Each of those has an **as-of rule**, and for `settings` and decision-feeding `learning_profiles`
rows the rule rests on an append-only history (`settings_history` / `learning_profiles_history`,
SPEC.md 11): every write or delete through the store's own writers appends a row (a write only when
the serialised value changed), and the migration seeds the rows that existed when the schema was
upgraded. The value in force at a window start is the newest history row with `at ≤ start` -- exact,
never interpolated, compared as a parsed instant rather than as text (a stored `+02:00` offset can
sort on the wrong side of the boundary), and stored canonically in UTC at full microsecond
precision. A stored stamp is accepted only as a whole: extended calendar date, `T`, `HH:MM[:SS]`
with an optional `.`/`,` fraction of any length, and a `Z` or signed `HH:MM[:SS]` offset; the
offset's digits are read from that match, not from the parser, which can drop an offset fraction
whole. Anything else -- a week or ordinal date, a basic-format date, another separator, a missing
offset -- is unplaceable: refused for a value, pruned from the scratch. That refusal is the safe
half of the trade, since a stamp this build writes always matches and a guessed instant does not.
Within the form, any number of fractional digits is fine, time and offset alike, while each digit
past microsecond precision is zero; only one that would lose real precision refuses instead of
being rounded. The scratch cut removes such rows in bounded batches, so a long history prunes
without a bind-variable error. A key
whose only row (a write or a tombstone) is newer than the start is still
**refused** (`no_vintage`), never back-filled, and the refusal names the key. A recording made
before the history existed reads its current row exactly as before. The scratch copy the engine runs
on is then given those as-of rows, because the engine reads `learning_profiles` through
`profile_get` itself; a rebuildable key may be pruned only as an explicitly labelled variant,
because pruning changes what the engine plans on. The episode records, per frozen value, whether it
came from an appended history row, the upgrade's seed, or a pre-history recording's current row,
and the report's information section carries that list.

A seeded row's `at` is its pre-upgrade last-write time, so the history cannot recover any value from
before its seed: a window starting before a decision key's newest write still refuses, because only
that write is known. A long window will therefore generally need further uptime after the upgrade,
but it need not begin strictly after it -- a start later than every relevant seed can qualify, even
if that start is still before the deployment.

**Warm start.** The recorded run did not begin from nothing, and neither may the replay: the active
plan (`_plan`, the newest recorded `plans` row with `created_at ≤ start`, which is the §3 as-of rule
applied at `now = start`), the moment of the last replan (`_last_replan_at`, the same row's
`created_at`), `_replan_backoff_s`, and the battery view the planner anchors on
(`_state.batteries`, seeded from the plant's as-of read at `start` together with
`_battery_read_ok`/`_last_sample_at`) are seeded from the recording before the first step. Seeding
`_battery_read_ok` without `_state.batteries` would be worse than not seeding it: `_planning_ready`
needs an `energy_kwh` there and the dispatcher's `soc_deviation` reads the same list, so
`_replan_reason` would answer nothing until the first control step — the opposite of the recorded
cadence. `_replan_backoff_s` is memory-only, so a window that starts mid-backoff (after failed
replans) is a declared assumption: the runner starts at `MIN_REPLAN_GAP_S` and says so in the
report's divergence attribution instead of pretending it recovered the value.

`full_charge` evaluation (`_evaluate_full_charge`, reachable from `_control_tick_unlocked` and
`_replan`) and the non-forced `update_cost_bases(now)` inside `_replan` are **production
behaviour and therefore replayed**, starting from the declared initial state and advancing as the
recording did; only `_maybe_maintenance`'s forced paths are skipped. The report lists every advance,
so a difference caused by a learned value is attributed rather than silently absorbed.

### The store, and the two façades

`Store` has no read-only mode (its connection enables WAL and runs migrations) and the engine writes
on every tick and replan (`save_plan`, `upsert_actuals`, `add_measurements`, `savings_daily`,
`add_dispatch`, `add_event`, `update_cost_bases`, `archive_forecast`, the pv-bias sink). Therefore:

- the **source** is opened read-only and copied with SQLite's `.backup` (never a bare file copy: an
  unflushed WAL truncates the episode). A path that looks like the live add-on database is refused;
- the engine runs against a **writable scratch copy**, and the source handle is the *only* read path
  for recording data — a comparand never reads the recording out of the scratch copy;
- every comparand states its side, and the two kinds of table need two different cuts, because the
  engine upserts most of them under the recording's own keys:
  - **append-only** (`plans`, `dispatch_log`, `events`): cut by `max(id)`/`max(ts)` captured
    immediately after the copy; rows past the cut are the replay's;
  - **upserted by key** (`actuals`, `measurements`, `savings_daily`, `learning_profiles`,
    `forecast_archive`, `settings`): "written after the boundary" does not exist — the replay
    overwrites the recording's own key, and `measurements` is `INSERT … ON CONFLICT(ts,
    entity_role) DO UPDATE` with samples bucketed onto the recording's own five-minute grid, so a
    `max(ts)` cut cannot see the rewrite at all. Recording value = the row under that key in the
    *source*; replay value = the row under that key in the scratch copy; a key rewritten with a
    different value is a finding, not a boundary artefact.

`HAFacade` is a **capture** façade: it answers reads from the episode and records every call. A
raiser would be wrong as the primary control, because `publish_states` catches exceptions and only
marks the API unhealthy — so the assertion is on the capture. Commands never reach it (they go
through the plant driver), and the notifier is injected as a no-op, because `Notifier.send` calls
`ha.call_service("persistent_notification", …)` from the replan and guard paths. Publishing has no
freeze setting (`publish_states` no-ops only when `ha is None`, which the engine cannot be), so the
façade's `set_state` publish calls are the expected capture: they are counted and reported per
window, and the checked property is that the façade saw **no `call_service`** and no state write
outside the publish path.

## 2. The plant

- **`recorded`** — `read_state()` answers from the recording (SoC samples, pack power), `apply()`
  records the setpoint. This arm holds the recorded trajectory fixed and is the only valid basis for
  E1 fidelity.
- **`simulated`** — `apply()` moves stored DC energy by the commanded power over the elapsed step,
  using the episode's **effective config snapshot** (not today's settings), with the pack's own
  `capacity_kwh`, `eta_charge`/`eta_discharge`, power limits, `min_soc_pct` and the per-tick ramp;
  grid flow is `load − pv + charge_ac − discharge_ac` with the AC/DC convention the planner assumes.
  The export limit is the planner's own (`optimizer.max_grid_export_w`, the same helper the feed-in
  request uses), not a second constant. Constraints the model does not reproduce (inverter AC cap,
  derating, temperature, driver latency) are listed in the report.

Neither arm may read beyond the current replay instant: the plant exposes `at(now)` snapshots only,
and a read past `now` raises.

## 3. Information discipline (§3.3)

- A tick sees only data whose as-of time is `≤ now`: the recorded plan it is compared against is the
  newest `plans` row with `created_at ≤ now` (plus its `plan_slots`), and fuller forecasts come from
  `forecast_archive` rows issued at or before the window (`forecast_archive` upserts the newest issue
  per `(kind, source, target_ts)`, so coverage is sparse but well defined). A window with neither
  source is refused (`no_vintage`).
- A window whose `actuals` coverage is incomplete is refused (`incomplete_actuals`); gaps are never
  treated as zero import.
- Chronological split (`tune` / `holdout`) is declared in the episode manifest and the report never
  mixes the two. Results over windows without valid vintages are labelled **empirical policy
  improvement**, never VSS.
- An arm's price vintage is **deliberately unprovenanced**: the runner stamps no contract model on it,
  exactly like any other caller-built forecast, and provenance is never inferred from price *shapes*.
  Provenance is a fact only whoever resolved the tariff can record, and a recorded window's own sells
  cannot establish it — 0.16/0.22 buys with 0.14/0.20 sells satisfy `sell = buy - fee` exactly and are
  equally an entity publishing two explicit rates. Configuration cannot declare it either: a configured
  entity may have been unavailable when the window was recorded, in which case production priced the
  sells from the derived rule and stamped `unavailable`.

  So such a vintage takes the engine's own two-step path instead (SPEC optimizer-v2 stage 3, the tail's
  pricing models): one supported model must fit the window, and the tail is priced under **both**
  readings — each published entry derived from its own buy, versus the clock-time reference rate — with
  the vector refused (`tail_pricing_ambiguous`) if they disagree anywhere. E3 is unaffected because its
  tail is entirely window-filled, so the two readings coincide by construction; a recording whose tail
  gained published entries falls under the comparison automatically instead of being trusted. Recording
  the contract model *as data* is a separate, deferred question.

## 4. Accounting (§3.2)

One number per arm, from **one declared source of truth per quantity** — never both:

```
profit = revenue − cost + terminal_adjustment
```

The energy terms are each **arm's own** flows, not the recording's (using the recording's flows on
every arm would make every `Δprofit` zero by construction): only the price series is the recording's,
and only its own settlement applies to the arm's own import/export.

- `cost` = the arm's import intervals × the rate recorded with them (`actuals.price_buy`);
- `revenue` = the arm's export intervals × `actuals.price_sell`, checked for **recording
  self-consistency** against `sources/feed_in.py` for that slot (one implementation of the contract,
  not two). Production very likely *writes* that column from the same rule, so a match says the
  recording's own columns and the assumed model are one rule — never that the household was paid
  that price: an external invoice is the only source for that and is out of scope. A recording that
  books no price column of its own (every slot a plan-slot fallback) is refused as unverifiable
  (`tariff_unverifiable_fallback_only`) rather than counted as a match;
- a row whose price column is NULL is **refused**, not priced from a later slot; the recorded plan
  slot's `buy`/`sell` is the fallback only where the actuals column is absent, and the report says
  which source was used;
- self-consumption creates no cash flow: its value appears as a lower `cost`, never as revenue;
- `terminal_adjustment` = `(E_end − E_start) × valuation`, reported **with and without** it, where
  `E` is DC pack energy under the same definition and the same valuation in every arm while
  `E_end` is each arm's own realised pack energy (forcing the two equal would turn the term into a
  constant offset and hide exactly what it is meant to show).

Valuation is reported two ways: the declared window-start fleet **cost basis** (headline, identical
across arms, so paired differences are well defined) and the ledger's own per-kWh booked basis
(sensitivity), because the ledger books PV-charged energy at the slot `sell` and grid-charged energy
at `buy` — a single basis cannot be fair to both, and the report states which one it used.

`savings_daily` is **not** combined with a terminal term: `charge_cost_eur`/`discharge_value_eur`/
`savings_eur` already embed value, so using them next to `import_kwh`/`export_kwh` would
double-count. Where a replay needs an independent check it recomputes from `actuals` and reports the
divergence as a finding.

Loss attribution is per decision window: the `Δprofit` of the single lever that differs between
paired arms — same window, same start state, same actuator limits.

## 5. The gate (§3.4, §8 stage 3)

The harness ships only when, on a recorded day:

1. **plant fidelity** — simulated DC energy vs the recording under the same commands;
2. **decision fidelity (E1)** — per decision window, the replayed setpoints
   (`dispatch_log.requested_w`/`applied_w`, plus `code`, which carries the decision slug when
   `code ∈ {OK, DRY_RUN, IDEMPOTENT_SKIP}` and `reason` is non-empty, while `result` stays
   `ok|error|skipped|dry_run`) and the plan's `plan_slots.power_w`/`action`/`reason` equal the
   recording within one planner grid step, with every difference attributed;
3. **accounting fidelity** — the replay's booked energy and basis vs the recording's, within
   tolerances that are **fixed in the episode manifest before the run** (an E1 grid-step
   tolerance, an energy tolerance in kWh and a basis tolerance in EUR/kWh), so they cannot be
   widened to fit the observed divergence.

E2 (tariff error), E3 (terminal error) and E4 (policy gain) are only reported as evidence for a
later stage once those three pass. A gate that could not be measured (no reference leg, a window
whose reference divergence is unknown) is reported as `null` and withholds the arm's paired money
exactly like a failed gate: an unmeasured gate is not a passed gate. Uncertainty is a **block
bootstrap over decision windows** (never IID resampling of autocorrelated prices), with block length
and resample count stated. An interval is called publishable only when the drawn block reaches the
plan-horizon-implied window count (taken from the recording's own plan span,
`horizon_end - horizon_start`, so it needs no setting to be present; the report prints which source
answered — `horizon_source` — and a recording that declares neither a plan nor an
`optimizer.horizon_hours` value gets no implied-length claim at all rather than the engine's
default), the sample spans the declared number of horizons (the manifest's `evidence_horizons`,
5 by default, so the floor is that many times the implied count), and the included windows are
contiguous (only interior holes count: the windows that start a new run are named as `gap_windows`,
while the circular draw's wrap from the last window back to the first is intrinsic to the method);
anything short of that is still reported with its reason and never called evidence. The declared
horizon count is a floor, not a sufficiency claim: with the drawn block at one horizon the
percentile interval rests on about that many independent blocks, so five horizons is the least a
sample can carry and still be evidence at all — enough to stop a one-horizon window being quoted as
if it were precise, not enough to make the interval itself precise.

## 6. Surfaces and refusal

- `python -m energy_manager.app.replay` — `--store <sqlite> [--episode <json>] --arm E1..E4 --start
  --end --step --blocks --resamples --seed --expect-digest --format toon|json`. The episode manifest
  is authoritative: an explicit `--seed`/`--blocks`/`--resamples` that disagrees with a loaded
  manifest is refused as `manifest_mismatch` (the manifest is well formed; the *request* is out of
  step with the frozen contract), and `--expect-digest` refuses a manifest that does not carry a
  contract digest recorded outside the file.
- Refusals: a live-looking source path, a source with a WAL whose data cannot be checkpointed, a
  window with `no_vintage` or `incomplete_actuals`, a missing as-of rows for the frozen state, an
  arm spec or fingerprint that is not the canonical frozen contract, and any façade command call.
- No API/UI surface in this stage: an experiment runner reachable from the add-on's HTTP API must be
  safe to call on the live instance, which is a separate decision.

## 7. Non-goals

- Re-implementing the optimizer, dispatcher or feed-in contract (import them).
- Optimizing anything: the harness measures; it never changes a live decision.
- Reporting a VSS number where as-issued vintages are missing.
