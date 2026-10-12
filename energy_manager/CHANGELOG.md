# Changelog

## 0.19.0

- **The sell floor restricts discharge; it never forces a charge.** The 0.18.0 floor branch used
  `grid_lo = max(grid_lo, -base)`, so in a PV-surplus slot (`base = load - pv < 0`) the lower bound
  became *positive* and the pack had to swallow the whole surplus at any price: a live 30 h plan
  charged 08:00-10:00 (sell 0.34 / 0.30 / 0.28) instead of the cheaper 11:00-13:00 hours
  (0.27 / 0.26 / 0.27), about 6 ct worse over that window. The bound is now
  `max(grid_lo, -max(0.0, base))`: a deficit caps the discharge at the house's own demand, a surplus
  leaves `p >= 0` while the PV exports freely. The floor is about *stored* energy; PV that would go
  straight to the meter has no cost basis. `_sell_floor_blocks` and the dispatcher clamp already
  judged discharges only and are unchanged.
- **You choose how PV is priced: `optimizer.pv_charge_preference`.** `auto` (default) prices PV
  energy charged into the pack at the slot's sell price (the export forgone — PV then wins exactly
  when `sell < buy`), `prefer_pv` at 0 (absorb the surplus before buying anything) and `avoid_pv` at
  the slot's buy price (export the PV and charge from the grid in the cheapest hours). The chosen
  price is an internal shadow price added to the objective for the PV part of a charge; the plan
  summary keeps reporting the true grid cost of the returned schedule, so no mode can invent a
  saving. `allow_grid_charge=false` and `min_spread` keep their existing scopes.

## 0.18.1

- **The measured energy is booked on the sample grid.** On a live install whose applied setpoint
  changes every ~2 minutes while the sampler reports every 5 minutes, the 0.18.0 ledger booked each
  update's own tail flat at the last visible sample, so the ramp the next sample revealed was never
  recovered: the reported pack content ran ~12 % behind the meter (250 command intervals, 142 with
  no power booked). Energy now follows the samples and price follows the commands: the covered
  sample spans are integrated between consecutive samples and split at the command changes and
  price-slot boundaries they cross, each part is attributed to the command in force, and a command
  whose whole span falls between two samples still books its measured share. `last_ts` is the end of
  the last processed sample span (never a command timestamp) and nothing beyond the newest settled
  sample is booked, so an unanswered tail waits for its next sample instead of being extrapolated
  flat and a replay cannot double-book.
- **The quantity is reconciled with the pack.** After booking, the accounted energy is compared
  with the pack's own measured SoC content (`capacity_kwh * (soc - min_soc_pct) / 100 / eta_charge`,
  clamped to the pack) and the difference is applied to the quantity **without touching the basis**,
  kept as the signed cumulative `reconciled_kwh` plus `reconciled_last_kwh`. A stale or missing SoC
  sample skips the step (`skipped_no_soc`), and a single correction larger than half the pack
  capacity resets the ledger (`reconciled_resets`) rather than silently inventing an adjustment.
  The new counters ride along in every `/status` `cost_basis` battery row.

## 0.18.0

- **The energy in the pack is valued at what it really cost.** A per-battery cost-basis ledger
  (`app/cost_basis.py`, runtime data in the store profile, never settings) derives the weighted
  average price of the AC energy currently held from **applied** dispatch changes and the *measured*
  battery power between them: a slice opens on an applied `auto` change and closes at the next one
  (or at the update), one dispatch row books at most one slice, and the slice end is stored so a
  replay books nothing twice. Each slice is priced at the plan slot it was charged in — grid energy
  at the slot's buy price, PV energy at the export price it forgoes — splitting only at price-slot
  boundaries, falling back to buy when sell is missing and skipping (never guessing) when neither
  resolves. A discharge reduces the accounted energy by the round trip, the SoC floor resets the
  ledger, and a bounded journal keeps the provenance.
- **A sale below that value is refused by default.** `optimizer.sell_floor_enabled` (default true)
  and `optimizer.sell_floor_margin_eur_per_kwh` (0..1, default 0) turn the ledger into a per-battery
  floor through the existing break-even helper, with the fleet's energy-weighted mean enforced by the
  plan (`sell_floor` reason) and by a dispatch clamp on a live planned discharge. Charging, SoC
  holds, overrides and everything outside `auto` are untouched, and with no ledger yet the behaviour
  is exactly as before. `/status` gains the per-battery basis and stored value plus a fleet block,
  the plan summary carries the floor, and two new entities (`sensor.sem_battery_cost_basis`,
  `sensor.sem_battery_value`) mirror it into Home Assistant.

## 0.17.1

- **A running plan keeps its setpoints.** The recovery guard treated this run's own live command as
  "a previous run died with a live setpoint": in `auto` it wrote 0 W over the plan every
  `RECOVERY_RETRY_S` (about 60 s), alerted `live_setpoint_recovered` on every retry and cut a
  sustained command to roughly two thirds duty (live test 2026-10-12). The latch is now split into a
  per-battery applied-command record for what *this* run wrote and a boot latch restored from the
  store for what a *previous* process left behind. Only the boot latch is guard-recoverable, and a
  verified hold clears it, so a real recovery holds once, alerts once and then stays silent.
- **Only a real, verified non-zero write latches.** A blocked `advisory`/`off` tick used to journal
  `requested_w` and latch from it, which let the guard write `input_limit=0` while the add-on was in
  advisory. A result now latches only when it is not `dry_run`, is `ok`, really wrote, and applied a
  non-zero setpoint — so `advisory`/`off`/`dry_run` runs issue zero write calls of any kind, guard
  holds included. The only writes outside `auto` remain the documented de-escalations (mode or
  `dry_run` change, Stop now, exit) and neutralising a genuine previous-run latch at boot.
- **The latch follows what the add-on commanded, not an optional setpoint entity.** On
  `control_path: device` there is no setpoint entity, so any momentary 0 W flow used to clear the
  record while `input_limit` still executed — an `auto → advisory` change then logged
  `hold_skipped — nothing to neutralise` and left the pack charging. The record now clears only on
  verified neutral: a hold the driver confirmed in its own read-back window, or two consecutive
  readings where the driver's own setpoint and the measured flow both read ~0. The Zendure driver
  reports the `input_limit`/`output_limit` read-back as its `commanded_power()` on the device path.
- **De-escalation touches only what we wrote.** `auto → advisory/off`, `dry_run` on, Stop now and
  exit neutralise every battery this run really commanded and has not verified neutral, and nothing
  else: a fresh advisory install, or a run that never wrote, is not touched at all. The `/stop`
  response shape and per-battery results are unchanged, and `pending_holds` now means "commanded and
  not yet verified neutral".

## 0.17.0

- **A scheduled full charge keeps the pack balanced.** New `maintenance.full_charge_enabled`
  (off by default) asks the plan to reach `maintenance.full_charge_target_soc_pct` (default 100)
  every `maintenance.full_charge_interval_days` (default 30) days so the BMS can balance its cells.
  While a tracked battery is due, the optimiser reuses the existing terminal-target mechanism —
  keeping the higher of the maintenance target and `optimizer.target_soc_pct_end` — so it charges
  in the cheapest slots before the deadline instead of force-charging at peak. Charging slots keep
  the stable `terminal_target` reason, and the plan carries an additive `maintenance` block
  (`enabled`, `active`, `due`, `target_soc_pct`, the affected `batteries`) that the Plan view marks.
- **Completion is measured, never faked.** The per-battery runtime state (`last_completed`,
  `next_due`, `first_seen`, `overdue_alerted_at`) lives in the store profile, not in user settings.
  A measured SoC at or above the target minus one point records the completion and moves the
  deadline a whole interval out; the due-date arithmetic keeps the local wall-clock time across
  DST and month boundaries. A deadline past `due + 24 h` raises the rate-limited
  `full_charge_overdue` alert and the cycle keeps trying; a missing or stale reading changes
  nothing. A battery with no history is due immediately, and one already at target completes on the
  first check.
- **Disabled or off changes nothing.** With the feature off no target and no profile record are
  written; `off` plans nothing at all, and advisory still plans the charge without writing to
  hardware (the normal dispatch safety pipeline is untouched). `GET /api/v1/batteries` gains a
  per-battery `maintenance` state, `GET /api/v1/status` and `GET /api/v1/plan` gain the plan-level
  block, and the Batteries card shows `due in N days` / `due now` / `overdue` / `last completed` /
  `off`.

## 0.16.0

- **A critically low cell now charges the pack out of danger instead of letting it discharge.**
  With `protection.cell_voltage_enabled` on (off by default) the add-on watches the **lowest cell
  voltage** — a mapped `cell_voltage_entity` (read like every other measurement, with the same
  freshness rules), or the `zendure_native` driver's own `read_cell_voltage()` hook when no entity
  is mapped. Below `protection.cell_voltage_cutoff_v` the battery is **latched**: discharge is
  blocked and it is charged at `min(max_charge_w, protection.emergency_charge_power_w or
  max_charge_w)` regardless of price or plan, still respecting max SoC and the per-tick ramp.
  Between the cutoff and `protection.cell_voltage_resume_v` it is blocked from discharging only; at
  or above the resume voltage the latch clears and normal planning returns. The two stable reasons
  `cell_voltage_guard` and `cell_voltage_emergency_charge` appear in the dispatch log and as
  rate-limited alerts. `protection.cell_voltage_warning_v` is the advisory threshold inside the
  band and must sit above the cutoff (as must the resume voltage), validated at save time.
- **The guard fails safe and survives a restart.** A stale or unreadable voltage keeps the previous
  latch (and its emergency charge) and raises a `cell_voltage_stale` note; a battery whose voltage
  was never seen stays inactive, and a zero or missing reading is never treated as a voltage. The
  per-battery latch is stored in the runtime profile and restored on boot, so a restart cannot
  silently resume discharging a pack that was charging out of a low cell.
- **Advisory and dry-run still see it, without a write.** The guard is evaluated before the
  authority gate, so advisory/off and dry-run log the reason and raise the alert but issue no
  service call; `auto` with dry run off writes the emergency charge for real. The status API gains
  a `cell_voltage` block (thresholds, latch, per-battery readings) and each battery a
  `cell_voltage_v`, and the Batteries card states the voltage and marks a latched battery.
- **The guard is a pure, ordered clause of `control.safety.check`** (SPEC 9.3 clause 4b) and is
  unit-tested without hardware, alongside the existing SoC/power/grid/ramp clauses whose order and
  semantics are unchanged.

## 0.15.0

- **Zendure hardware can now be driven directly, with no `zendure_ha` integration.** The new
  `zendure_native` battery driver speaks the device's own local zenSDK HTTP API with the standard
  library alone (`urllib`/`ssl`; `requirements.txt` stays empty): no integration, no MQTT broker and
  no cloud account. Add the battery by hand in the wizard — `host` and `serial` are required, with
  optional `scheme`/`port`/`api_path`/`token`/`timeout_s`/`insecure_tls` — and enable the device's
  local API first (in the Zendure app, add HEMS and exit to apply).
- **Reads and writes follow the documented device contract** (`docs/EXTERNAL_APIS.md` section 6,
  from the vendor zenSDK docs and the reference implementation): `GET /properties/report` supplies
  SoC, the measured AC power pair, PV, the device's own limits and per-pack voltages; the write is
  `POST /properties/write` with `{sn, properties: {acMode, inputLimit, outputLimit, smartMode}}`
  using the property sign convention (magnitudes plus direction). Writes are clamped to the
  configured limit **and** the device's reported maximum, and every write is read back against the
  device's own limit echo within 5 W — a mismatch or timeout is a driver error, so the existing
  failsafe hold path decides what happens next. `read_cell_voltage()` exposes the lowest pack cell
  voltage (0.01 V) for a low-voltage guard, and the device's pack count/temperature/limits are
  reported as diagnostics. The device reports no capacity in kWh, so `capacity_kwh` stays the
  figure you entered — never a number derived from voltages.
- **Saving a `zendure_native` battery proves the connection.** The add-on reads the device once
  (bounded to 5 s) before storing: an unreachable host, a report with no properties (local API
  disabled) or a serial that does not match the device is refused at save time with the reason
  named. `allow_unreachable` in the mapping is the explicit opt-out; the battery card then shows
  the read error instead. A configured `token` is sent as `Authorization: Bearer`, stored, masked in
  every API/UI read-back (`***`, and a PATCH that omits or echoes the mask keeps the stored value)
  and never logged.
- **The local MQTT path is documented as planned work, not stubbed.** The device is an MQTT
  *client* that must be pointed at a broker (`POST /rpc`, `HA.Mqtt.SetConfig`) and publishes on
  `iot/<productKey>/<deviceId>/properties/{report,read,write}`; a stdlib-only broker/client within
  this task was not achievable, so the exact remaining work is recorded in the docs and no
  untested code ships.
- **Verified against an in-process fake device, not the hardware.** `tests/test_wp5_zendure_native.py`
  covers read/write round trips, charge-vs-discharge sign mapping, device clamping, read-back
  mismatch, HTTP errors, malformed JSON, timeouts, holds, cell-voltage reads, connection validation
  and the save-time probe; live firmware behaviour (per-model maxima, HTTPS certificates, the
  undocumented 401/token path) still needs validation on a real device and is stated as assumed in
  `docs/EXTERNAL_APIS.md` section 6.

## 0.14.0

- **Each pack's real usable capacity can now be learned from its own cycles.** A battery gains a
  `capacity_source` of `manual` (the shipped default) or `learned`. In `learned` mode the capacity
  you typed becomes the seed/fallback, and the planner uses an estimate measured from the battery's
  own 5-minute power and SoC rows. Each charge window computes
  `AC_charge_kWh * eta_charge / dSoC` and each discharge window
  `AC_discharge_kWh / eta_discharge / |dSoC|`; a window counts only with a SoC move of at least 10
  points, a mean power of at least 20 % of the battery's own limit, finite values and no gap longer
  than one sample. Accepted windows are combined with a median after a MAD outlier filter, at least
  three are required, the result is clamped to a plausible 1..200 kWh, and an estimate older than 30
  days is ignored. The effective capacity is resolved once and used everywhere capacity is consumed
  — drivers, the DP optimiser, allocation, the savings baseline and the published summaries — with a
  stable reason slug logged as the learned value is applied or rejected. The estimate is stored as
  runtime data (`learning_profiles`), never over your typed capacity. **Manual always wins**: with
  the default source, or when the estimate is stale or not yet confident, the configured value is
  used unchanged.
- **`GET /api/v1/batteries` and the Batteries card show where the capacity comes from.** Each record
  exposes `capacity_source`, the configured `capacity_kwh`, `capacity_learned_kwh`,
  `capacity_samples`, `capacity_updated_at` and the `effective_capacity_kwh`, plus a nested
  `capacity` detail. The card states the effective value, the estimate, its window count and last
  update, and the Edit dialog switches between manual and learned through the normal save flow. All
  additions are additive; `capacity_source` defaults to `manual`, so an existing configuration plans
  exactly as before.

## 0.13.0

- **Savings are now earned only by energy the battery actually moved.** The savings accounting used
  to derive battery cycles from whatever power the driver reported, and some drivers read back the
  setpoint they had written — so a dry-run, blocked or stale-refused dispatch, or a setpoint the
  battery never confirmed, could still show up as delivered energy. Battery flow is now counted only
  when the driver read a real measurement back: a setpoint-only reading (the generic driver, or a
  Zendure whose measured pair went missing) reports the day's battery flow and cycle count as
  **unknown**, never as the planned value. `BatteryState` gains `power_measured`, the Zendure driver
  sets it for the complete measured pair, and `actuals.battery_kw` is left empty rather than filled
  from a command.
- **Measured AC battery flows are accounted per day and per range.** `savings_daily` gains
  `charged_kwh`, `discharged_kwh`, `charge_cost_eur` (priced with the buy series at flow time) and
  `discharge_value_eur` (priced with the sell series), plus per-day coverage flags for the grid,
  load, PV and battery series. `/api/v1/savings` exposes them on every bucket and in the totals
  (with `measured_buckets` counts), and the Savings view shows them under a clearly labelled
  *Measured battery flows* heading together with the coverage. A measurement gap makes the flow
  unknown (`--`), never a smaller partial total that would look measured.
- **Nothing else changed shape.** New columns are added by a forward-only schema migration; rows
  written before this version keep working and report the new fields as unknown. All API additions
  are additive.

## 0.12.0

- **Several PV forecasts can be merged into one hourly input.** The planner used to take its solar
  forecast from one provider. A new `solar.sources` list combines any mix of them instead: the
  Forecast.Solar API (`forecast_solar`), **any solar-forecast provider already configured in your
  Home Assistant Energy dashboard** (`ha_energy` — read locally over the WebSocket
  `energy/solar_forecast` command, so Solcast and friends work without re-entering the array), the
  watt-peak estimate (`capacity`), and the meter-derived one (`derived`). Each entry carries a
  finite `weight > 0` and may override the matching `solar.*` setting (planes, watt peak, geometry,
  weather entity, ...) for that source only. `solar.merge_strategy` picks `weighted_mean` (default),
  `median` or `max`. Every source is aligned onto the same 15-minute grid with the existing
  interpolation, so hourly and 15-minute sources mix cleanly; a failed or empty source is skipped
  with a note and the rest still produce a forecast. **An empty `solar.sources` (the shipped
  default) leaves today's single-source path byte-for-byte unchanged**, and if no source produces a
  usable series the single-source chain runs as the fallback. Bias correction and NASA clearness
  keep working inside each source before the merge. The Solar card shows the configured sources
  with their weight and status and can add, remove and reorder them through the normal dirty/Save
  flow; the JSON field in All settings remains the fallback editor. Each configured source is
  reported with `name`, `weight`, `state` (`ok | degraded | failed | disabled`) and a short note on
  the solar provider list and in `/api/v1/forecast?kind=solar`. New settings are validated: a
  malformed entry is reported per key and never crashes the engine.

## 0.11.0

- **A battery can be added and driven through the inverter's own MQTT bridge.** A SolarFlow whose
local MQTT feature is on publishes its zenSDK properties itself, and Home Assistant's `mqtt`
integration turns each into an entity (`number.<serial>_inputlimit`, `select.<serial>_acmode`,
`sensor.<serial>_electriclevel`, `switch.<serial>_smartmode`, ...). The new `zendure_mqtt` driver
writes those directly: the AC mode select for the direction, the two magnitude limits for the power,
and the device's own `smartmode` switch (on for a live command, off for a hold) — the Zendure-HA
integration sets exactly that flag with every AC command, and the inverter only acts on the limits
while it is set. No manager is involved and nothing waits for a P1 event. If the Zendure-HA
integration still manages the same inverter, its `operation` select can be mapped so the driver keeps
it `off` — otherwise it re-writes both limits on every P1 event. Add it with **Driver → zendure_mqtt**
in the battery dialog, or pick the device in **Find a battery**.
- **The battery picker now recognises a Zendure MQTT device and prefills the whole mapping.** The
device is identified by its platform plus manufacturer/model (Zendure / solarFlow…), and each row's
role is read from the zenSDK property name it carries — no confidence score, so the device-level
`electricLevel` always wins over a pack's `socLevel`, and the pick fills `ac_mode_entity`,
`charge_limit_entity`, `discharge_limit_entity`, `soc_entity`, `soc_target_entity`, `min_soc_entity`,
`output_power_entity`, `grid_input_power_entity`, `pv_power_entity` and `smart_mode_entity` plus the
`max_charge_w`/`max_discharge_w` the limit entities state. Discovery caps at 50 rows per read
(SPEC 10) and sorts by confidence, so the bridge device — whose property names score lower than the
integration's translation-key matches — is found by searching its device name or serial; the picker
says so when a read comes back without a battery.
- The **Driver** dropdown says where the entities come from, `Control path` is hidden for drivers that
have only one path, and the MQTT choice states plainly that no other controller may manage the same
inverter at the same time. `Dispatch` on the battery card reads *Inverter over MQTT: AC mode +
limits (the device's own entities)*.
- Save-time validation follows the control layer: the driver list is no longer duplicated in
`config.py`, so a battery is checked against exactly the drivers the dispatcher can build.

## 0.10.1

- **A battery can now be told which write path to use, and a Zendure inverter can be driven
directly.** `BatteryConfig.control_path` (`auto` | `manager` | `device`, default `auto`) is picked in
the battery dialog and validated at save time: `manager` needs the Zendure manager's `manual_power`
and `operation` entities, `device` needs that inverter's own `charge_limit_entity`
(`input_limit`) and `discharge_limit_entity` (`output_limit`). The **device** path writes the
direction select (`ac_mode` → *input* while charging, *output* while discharging) plus both limits,
and puts the manager into `off` so its next P1 event cannot overwrite them. That matters because the
manager applies a setpoint **only** when its P1 meter changes: on a house whose grid feed reaches
Home Assistant once every five minutes, a 1 kW charge planned for hours was applied as a five-second
blip about once every twenty minutes (the inverter path needs no P1 event at all). The battery card
now states which path is really in use (`Dispatch`), and the dialog explains the trade-off.
- **A command the device has not confirmed yet is no longer revoked.** The driver used to require the
*measured* AC flow to match within the read-back window and otherwise report `driver_error`, which
with `control.failsafe_on_error` wrote 0 W back — cancelling the very command the device was about to
apply (live 2026-10-12: −1000 W written every 30 s and zeroed 4.6 s later, forever). Such a command is
now reported as the new dispatch code `commanded`: `applied_w` is the setpoint that is in force,
`readback_w` what the device reports, and the message names both. `driver_error` (and the fail-safe
hold) is kept for a write that really did not land, or a manager that cannot be held in `manual`. The
**Plan** view states it in words ("commanded 1.00 kW of 1.00 kW requested; the device has not
confirmed it yet") and the measured-vs-planned line remains the honest check.
- The device path confirms itself honestly: `input_limit`/`output_limit` are device properties, so a
matching read-back **is** device evidence (the inverter echoes them within a few seconds over zenSDK
or MQTT), and the AC flow is the second proof.

## 0.10.0

- **Every planned window now states the data behind it, not just its action.** The **Next actions**
  list shows, per window: the buy and sell price with their spread, the break-even
  (`buy / round_trip + wear`) and the margin above it, the planned kWh in and out, the SoC path and
  the floor a reserve hold defends, the planned grid flow and what it costs or earns, the price
  provider's age, and the window's rank by buy price in the horizon. One line above the list adds the
  windows up. The figures come from the plan payload the UI already had - they were simply never
  shown, which made a 15-minute grid and an hourly tariff look identical on screen.
- **The plan now reports its own resolution, and the interface uses it.** `GET /api/v1/plan` carries
  `slot_minutes` (SPEC 10, additive). Before this the UI assumed 60 minutes for every plan, so a
  15-minute plan was captioned as hourly, window end times were up to an hour wrong, and the "now"
  slot was looked up on the wrong grid. The offline render check now asserts the payload's own
  resolution drives the window range.
- **New additive route `GET /api/v1/plan/execution`.** It joins the plan's slot grid with the
  persisted 5-minute actuals and the dispatch log: per slot, the last dispatch attempt per battery
  (requested vs applied, clamped, dry run, mode, reason) and the measured aggregate (kWh in/out,
  import/export, average power, SoC start/end, how much of the slot was actually observed). A window
  therefore says "applied 400 W of 800 W requested (clamped)" or "measured -0.70 kW vs -0.80 kW
  planned" instead of only what was intended, and a slot nobody reached reports nothing rather than a
  fabricated zero.
- **A stale window says what it means: it is never dispatched.** The dispatcher refuses a slot whose
  price forecast is not fresh, so the window now states `not dispatchable: this slot has no fresh
  price (stale_input)` instead of presenting its plan power as if it had been applied. With an hourly
  tariff on a 15-minute plan grid this is the difference between three quarters of every hour looking
  scheduled and being visibly blocked.
- The Plan view refreshes in place when you return to the tab (`state.refresh`), so the "now" marker,
  the measured figures and the price age no longer sit on a screen that was opened hours ago. Panels
  reload without flashing "Loading…".

## 0.9.1

- **The Meter card now shows where the grid meter comes from.** With every `grid.*` key empty it
  names the roles the add-on resolved from the Home Assistant Energy dashboard ("From the Home
  Assistant Energy dashboard: signed power …, import counter …"); a saved value is labelled as an
  override of that fallback, with the dashboard's own roles beside it, and a **Use the Energy
  dashboard** button clears `grid.power_entity` / `grid.import_entity` / `grid.export_entity` back to
  the automatic path. The settings themselves stay empty - this only makes the 0.9.0 runtime default
  visible (and escapable), since a saved value previously disabled it with no way back from the UI.

## 0.9.0

- **The meter now comes from your Energy dashboard by default.** The add-on reads `energy/get_prefs`
  (read-only, no admin needed) and uses the grid source Home Assistant already has: its signed power
  sensor (positive = import) for live sampling and control, or - when no power sensor is configured -
  the import/export energy counters as a long-term-statistics history source. The **Meter** card can
  stay empty; an explicit `grid.*` value still wins, and the read is cached (5 minutes) so an
  unreachable Home Assistant costs one attempt, not one per tick.
- **The derived load and solar estimates now read history from long-term statistics**, which are
  never purged: hourly means for power sensors and hourly `change` for energy counters, converted to
  average kW. A recorder with the default 10-day purge (or an Energy dashboard with only kWh
  counters) still gives the meter-derived solar fit weeks of data, so the learned array size is not
  limited to whatever the recorder still holds. Recorder history stays the fallback.
- **A solar source with no planes now estimates from the meter instead of failing.** With
  `solar.source=forecast_solar` and an empty `solar.planes`, the meter-derived estimate runs when a
  meter resolves (an explicit mapping or the Energy dashboard) and the forecast says so; only when
  that is impossible too does the panel report `no_pv_source`, now naming the derived failure in its
  hint.

## 0.8.2

- **The plan grid now defaults to 60 minutes**, matching the hourly dynamic tariffs most suppliers
  publish (`optimizer.slot_minutes` = 60; was 15).
- **The price grid now starts where the plan grid starts.** This is the fix that makes the 60-minute
  default safe: the plan grid is floored to its own step, so a price forecast starting at the
  *current quarter* (16:45 at 16:50) left the plan's first slot - the one the dispatcher acts on -
  filled from a later slot and flagged stale, and the dispatcher never executes a stale current
  slot. With an hourly tariff on a 15-minute plan grid that meant the battery was blocked for three
  quarters of every hour, and the optimiser discarded 90 of 120 slots as flat. Now the current plan
  slot always matches a real provider entry: an hourly feed plans 30 fresh slots with no gaps, and a
  quarter-hourly feed is unchanged (its grid still starts at the current quarter). It also stops
  `sensor.sem_price_now` reporting the *next* hour's price during the last quarter of the hour.
- **A fully covered forecast is no longer reported as a short tail.** The grid ends one step before
  the horizon (`slot_range` excludes its end), and comparing that slot's *start* with the horizon
  marked every complete forecast `degraded` and added a "coverage ends …; uncovered slots are stale"
  note that was simply false. Coverage is now measured with the fresh slots' own cadence.
- Dispatch resolution follows the same setting: setpoints change once per slot, and the action
  windows, captions and slot table all report the plan's own resolution (it is never assumed to be
  15 minutes). Setting it in the add-on's Settings page overrides the new default.
- The offline demo fixtures (`?mock=1`) use the same 60-minute plan grid, so the demo's captions and
  slot table show what a default installation shows.
- Stale-price warning reworded to name plan slots rather than "price slots".

## 0.8.1

- **The chart legend no longer lands on top of the text under it.** On a phone the legend was drawn
  inside the chart's fixed height and overflowed onto the explanation below it, which made both
  unreadable. The legend and the one-line chart caption are now their own rows under the plot and
  wrap like normal text. Series names also say what they are without a colour word ("Stale price
  slot", "Charge level", "Upper uncertainty (long dashes)").
- **Axis numbers are consistent and carry their unit.** A percent axis used to mix "100" with
  "75.00"; every tick now uses one precision, percent ticks read "25 %"–"100 %" and the rotated
  percent label that collided with them is gone (the other axes keep a smaller gutter on a phone).
- **Less prose, in the right place.** The long paragraph under the plan timeline (which repeated the
  legend) is replaced by one short caption per chart plus a single line about the break-even line;
  the worked break-even maths stays in "Why this plan?". The worked example now names what it
  computes ("a break-even of 0.29 EUR/kWh, which includes 0.05 EUR of wear per kWh delivered").
- **Plainer wording in the plan views.** "Discharge · 700 W" (not "-700 W discharge"), single-slot
  action windows show one time and multi-slot windows end at the end of their last slot, the plan
  summary rows name what they are ("Estimated net cost", "Saving vs baseline", "Energy charged"),
  the stale-price warning says what staleness does to the plan, timestamps read "just now" instead
  of "0 s ago" and whole minutes read "7 min" instead of "7 min 0 s".

## 0.8.0

- **Every screen now answers first, then explains.** Today opens with the control authority, what the
  battery is actually doing and what the plan intends, instead of nine equal cards. Plan says whether
  the horizon saves or loses money before showing the timeline. Forecasts start with the measured
  error and how fresh the source is. Savings starts with the comparable saving or loss and how many
  periods actually had a baseline. Log starts with the message. Worked examples, raw capabilities,
  mappings and long provider notes moved behind named disclosures ("Why this plan?", "Efficiency and
  wear", "Source details", "Entity mapping", "All slots").
- **A short navigation instead of seven equal tabs.** Today, Plan, Savings and Manage fit one row on a
  phone; the second row is contextual (Plan → Forecasts, Manage → Batteries, Settings, Log). All seven
  pages keep their own link, and each view has one heading that gets focus when you navigate.
- **Setup cards that can actually finish their job.** The fixed day/night window hours, the day-ahead
  VAT/energy-tax/import-fee adders, the array's watt peak and the Forecast.Solar plane list are on the
  card that selects the option, instead of hidden in the advanced drawer. Settings also shows the
  current authority (mode and dry run) permanently, keeps search and "show all advanced" always
  visible, and reports saving success or a rejected value in the always-visible save bar — with the
  server's message attached to the field it rejected. The Battery card reads the battery list Home
  Assistant actually stores, so an empty or stale descriptor can no longer claim you have no batteries.
- **Nothing you are reading is thrown away by a refresh.** Polling updates values in place and keeps
  your scroll position, focus and expanded detail. It pauses while a dialog is open, a change is being
  saved, you have unsaved settings or a Stop result is on screen. The Log tells you a new entry
  arrived instead of rebuilding the list you are reading, and Retry now always makes a new request.
- **One authority, one explanation, one confirmation.** Turning on Auto or turning dry run off asks the
  same question and gives the same answer wherever you do it; the mode buttons respond on the first
  click; and a cancelled dry-run change puts the confirmed switch state back. A test discharge now
  says in plain text what it is waiting for ("Requires Auto and dry run off — currently mode is …").
- **Numbers you can trust at a glance.** Forecast solar and load are labelled kW (they used to be
  labelled as energy), forecast error is shown with the unit it is measured in, a day with no forecast
  data is left blank instead of plotted as zero, partial baselines state their coverage, and "Loss",
  "Unknown" and "Stale" are separate visible labels. Readability and touch: darker/lighter metadata
  text that meets contrast in both themes, ~44 px controls, and one type scale instead of many close
  sizes.
- **Charts are identifiable without colour.** Each series has a fixed line signature (solid, long
  dashed or dotted) named in words in the legend, the break-even threshold is dotted and the sell price
  long-dashed, axis gutters narrow on a phone without dropping their unit, and switching the theme by
  hand redraws the charts immediately.

## 0.7.0

- **Wear is derived from your battery, not typed in.** Each battery now takes an optional purchase
  price and cycle life; the add-on computes wear per kWh delivered as
  `price / (cycle life × usable kWh × discharge efficiency)`, so one full cycle costs exactly
  `price / cycle_life` (3000 EUR over 6000 cycles on 9.6 kWh usable at 95 % discharge ≈ 5.5 ct/kWh).
  Charging alone costs no wear because wear is charged on the discharge leg only. The plan uses the
  delivered-energy-weighted mean over the fleet; the advanced `optimizer.cycle_cost_eur_per_kwh`
  setting stays as an override and, when non-zero, replaces the derived value. The battery card shows
  the derivation and the Plan summary shows the wear it used.
- **The break-even is simpler: `buy / round trip + wear`.** Because wear is now stated per kWh
  delivered, the formula no longer needs the discharge efficiency; the chart line, the battery card,
  the idle-slot `efficiency_loss` reason and the docs all use the same expression.
- **Solar accuracy: NASA POWER observed irradiance (on by default).** The capacity and meter-derived
  solar sources now learn a per-hour-of-day clearness index from NASA POWER's observed all-sky and
  clear-sky hourly irradiance over the last three weeks (keyless, ~2-3 day latency, one request per
  location per day, cached). It fills in slots the weather entity has no cloud data for and refines
  the meter-derived watt-peak fit. It is an addition, not a replacement: the weather-entity cloud
  attenuation and the measured-PV bias keep priority, `forecast_solar` is unchanged, and a NASA
  failure is a note, never a failed forecast. Turn it off with `solar.nasa_irradiance`.

## 0.6.0

- **Four setup cards instead of a wall of options.** Settings now opens on **Price**, **Meter**,
  **Solar** and **Battery** cards, each with only the primary choice of its topic, and one collapsed
  **All settings** drawer holding the full grouped form. The always-visible set dropped from 55 rows
  to 21, and the `control.*` tuning knobs moved out of the never-folding *Safety* group into a
  foldable *Control* group.
- **Searchable entity pickers.** The Meter card and the Solar card search your Home Assistant
  entities (`/discover`) and save the entity you pick; a typed entity id can always be used directly.
  An export-only grid sensor is saved to `grid.export_entity` rather than to the card's key, because
  the candidate row says which setting it belongs to.
- **Solar from the meter, with nothing to enter.** `solar.source = derived` learns your array from
  the moments your meter exports: `pv = export + battery charge`. It takes the cloudless envelope of
  those samples to size the array and then runs the same sun model as the watt-peak source. The house
  load at those moments is not subtracted, so the learned size is deliberately conservative; an
  explicit `solar.capacity_wp` overrides it.
- **Feed-in price is derived now.** The sell price is `buy − feed-in cost` (the new name for
  `price.export_fee_eur_per_kwh`, default 0), minus the energy-tax component when
  `price.feed_in_energy_tax_refund` is off — the two cases a saldering contract can have. A flat
  tariff or a feed-in sensor still wins if you set one, and `optimizer.allow_export` still defaults
  to off.
- `PriceSlot` gained an additive `energy_tax` field (per slot from the Zonneplan included/excluded
  pair, otherwise the configured wholesale adder), and discovery rows for price/solar/load now carry
  the `setting_key` the pickers write.


## 0.5.1

- **Fixed: an update could take an hour to appear.** The add-on served its own assets - `style.css`,
  `app.js`, `charts.js`, `mock.js`, the vendored framework - with `Cache-Control: public,
  max-age=3600`, and the asset URLs carry no version, so a browser could keep rendering the previous
  release's interface for up to an hour after an update, with nothing on screen to say so. Every asset
  now revalidates on every load. The top bar shows the running version, so the panel itself tells you
  which release you are looking at.

## 0.5.0

- **The interface was rebuilt: same features, far less wall-of-fields.** The settings form is now a
  group rail - General, Price, Solar, Load, Grid, Optimizer, Safety, Notify, Batteries - showing one
  group at a time, with a sticky save bar that counts your unsaved edits and asks before you navigate
  away from them.
- **Every setting is reachable now.** The old form silently dropped any field that did not apply to
  the selected source *and* still held its default value, so options simply did not exist as far as
  the UI was concerned. Nothing is dropped because of its value any more: fields that do not apply to
  the current price/solar source stay listed (muted, "not used by the current selection") when they
  hold something, and everything else is behind a per-group `Advanced (n)` disclosure that remembers
  whether you opened it. `Show all advanced` opens every group at once. Safety settings are never
  folded.
- **The server decides what is advanced.** A new additive `advanced` row field carries that
  classification from the configuration metadata (SPEC 13.3), so the UI cannot drift from the backend
  and no label has to say "(advanced)" any more.
- **Bootstrap 5.3.3, vendored as CSS.** The look is a design layer over Bootstrap's tokens rather than
  a pile of bespoke rules: one accent colour, 1px borders, an 8px spacing scale, tabular figures, no
  gradients or webfonts, light *and* dark palettes, and a three-state theme control (auto / dark /
  light) that follows your operating system by default. Still no build step and still no CDN: the
  stylesheet is a pinned file in `vendor/` (checksum and refresh steps recorded) and the audit gate
  skips it only while its bytes match that record.
- Fixes found while building it: every free-text setting rendered as an empty dropdown (the API sends
  `choices: []`, which the form mistook for a select); `control.stale_plan_seconds` was missing from
  the offline mock entirely; and `price.feed_in_entity` escaped folding because a front-end safety
  regex was doing double duty as the fold rule.
- New gates: `tools/ui_render_check.js` renders the real front-end headless through a small DOM shim
  and asserts that every view renders, every applicable setting is reachable, folding and unfolding
  work, no number prints as `undefined`, every emitted class exists in CSS and no asset is remote.

## 0.4.0

- **AC-coupled solar, estimated from the array's watt peak.** `solar.source` now has two modes:
  `forecast_solar` (the API, per-plane geometry) and `capacity` (new) - state the array's total Wp and
  the add-on estimates production itself. No API key, no plane geometry, and it fits an AC-coupled
  array whose production reaches the house through the meter rather than through the battery.
- **A documented model, not a black box.** Solar elevation and azimuth from the standard declination
  and equation-of-time equations, Haurwitz clear-sky irradiance, isotropic transposition onto the
  plane, optional Kasten-Czeplak cloud attenuation from a Home Assistant weather entity, and a
  performance ratio (default 0.85). Tilt and azimuth are optional: left unset, the forecast *states*
  that it assumed south-facing at 35 degrees instead of implying geometry you never entered.
  Uncertainty is 35% without cloud data and 25% with it.
- **Your real production makes it more accurate.** Point `solar.measured_entity` at the inverter's AC
  output and the per-hour learned correction (EWMA, clamped, decaying after 14 days) narrows the
  estimate from what actually happened. Use the inverter's AC output, not a meter's export register:
  self-consumed energy never reaches the meter, which would bias the correction low.
- Cloud attenuation uses `weather.get_forecasts`. An entity that supports no forecast type at all
  (some integrations publish only a current `cloud_coverage`) is not a failure - the current cover is
  held flat and the note says so; with no cloud data the estimate is clear-sky and says that too.
  Weather never fails the forecast.
- A missing `solar.capacity_wp` fails with a hint naming the setting; a capacity is never guessed from
  history, and an empty solar panel now names whichever setting its configured source is missing.
- The settings form shows only the fields the chosen source uses (planes and API key for
  `forecast_solar`, watt peak, tilt, azimuth, performance ratio and weather entity for `capacity`),
  and names any hidden field that still holds a value.

## 0.3.0

- **You choose where prices come from, and what your contract looks like.** Two settings replace the
  need to know which sensor to paste: `price.provider` (Zonneplan / day-ahead market / fixed tariff /
  your own entity) and `price.contract` (dynamic per quarter hour, dynamic hourly, one fixed rate, or a
  day/night split). The add-on resolves the concrete source, says which one it used, and fetches from
  there.
- **Zonneplan is found in Home Assistant, structurally.** The tariff sensor is located by its payload
  (a kWh unit, at least two entries on a regular 15-minute or hourly grid, and its own state agreeing
  with the slot it is in) and never by its name. That matters: the gas tariff has the *same*
  fixed-point shape and its state agrees with its own single slot, so only the unit and the grid
  separate them. A quarter-hourly contract prefers the 15-minute sensor, an hourly contract the hourly
  one, and having to use the other grid is reported as a note rather than passing silently.
- **Day-ahead prices** come from energy-charts, keyless, for the configured bidding zone. They are
  wholesale: the forecast carries a note naming the VAT, energy tax and fees actually applied, because
  a wholesale price with no adders is not a consumer price. Zone coverage is real - a valid zone can
  simply publish nothing for the window, and the hint then names that zone and points at the
  alternatives instead of claiming a generic failure.
- **A fixed tariff** is built from one or two consumer prices (typed, or read from a sensor you point
  at) and, for the day/night split, the two window start hours. Windows are evaluated per slot in your
  timezone, so a window that crosses midnight and DST both work. A fixed contract has **no market
  fallback**: a missing price fails loudly with a hint naming the field, because silently planning a
  flat tariff against market prices would be worse than not planning.
- **An empty forecast panel now explains itself.** `GET /api/v1/forecast` carries `provider`,
  `contract`, `notes`, `reason` (a stable slug such as `provider_failed`, `fixed_price_missing`,
  `no_pv_source`, `no_history`) and `hint` - one sentence naming the setting or condition to fix. The
  forecast view shows the hint verbatim with the reason beside it, plus the source, provider and
  contract, instead of the old "the source failed or the horizon is empty".
- `GET /api/v1/config` carries `price_warnings`: provider/contract mismatches, a missing fixed price, a
  collapsed day/night window, and wholesale prices with no adders configured. Warnings are reported,
  never fatal - refusing to plan would be worse than planning with a stated mismatch. The settings
  form shows only the price fields the chosen provider and contract use, and names any hidden field
  that still holds a value, so nothing takes effect invisibly.
- The advanced `entity` provider keeps its old behaviour and remains the default, so an existing
  configuration is unaffected. `ct/kWh` sensors are now scaled correctly on the state comparison.

## 0.2.0

- **Batteries are selected, not typed.** The add-battery dialog lists the batteries discovery can see
  and filters them as you type. Each row names the device (the name you gave it), its model,
  manufacturer and platform, how many battery roles it can fill, and whether it can actually be
  driven. One *Select* fills the id (slug derived from the device name, made unique against the
  batteries you already have, still editable), the name, the driver, the whole role mapping and the
  device's own numeric limits: charge and discharge limits from the entities that carry them, and
  capacity from the pack figure.
- Read-only look-alikes are never offered as drivable. A device whose only control-role matches are
  sensors is labelled `control entities are read-only` and ranked below a real battery, and a sensor
  is never auto-mapped to a setpoint role. Discovery legitimately returns lone `soc`, `connection` or
  `manual_mode` suggestions for thermostats, bridges, printers and phones; they no longer appear as
  batteries.
- **Fixed: usable energy could be mapped as the installed capacity.** `available_kwh` (what is left
  above `min_soc`) and `total_kwh` (how big the pack is) both matched the single `capacity` role, so
  the wizard could write a nearly empty battery's reading - negative, on a live install - into
  `capacity_entity`. Usable energy now has its own role and its own driver key.
- `GET /api/v1/discover` rows gained the additive `device_id`, `device_name`, `manufacturer`, `model`,
  `min` and `max` fields (SPEC section 10). Without a reachable device registry they stay null and
  nothing else changes.

## 0.1.2

- **Fixed: the add-on still could not start — the Supervisor token never reached the entrypoint.**
  0.1.1 got past the option validation and then exited immediately: the base image's s6-overlay
  entrypoint runs the container command as a service, and s6 services do not inherit the container
  environment, so `run.sh` saw neither `SUPERVISOR_TOKEN` nor `HASSIO_TOKEN` and refused to start.
  The command now goes through `/command/with-contenv`, which imports the environment the Supervisor
  passes to the container. The fail-fast check is unchanged and still fires when no token is set.
- The CI image job now runs the built image with a Supervisor-style environment and fails if the
  token does not reach the entrypoint or the app never reaches its start banner — the two defects
  that reached a live install were both invisible to a build-only check.

## 0.1.1

- **Fixed: the add-on could not start on a fresh install.** `options:` carried explicit `null`
  values for the four keys that are read from Home Assistant at runtime (`currency`, `timezone`,
  `latitude_override`, `longitude_override`) while the schema declared them optional. Supervisor
  rejects a null option value and refused the start with `Missing required option 'currency'`.
  Those keys are now absent from `options:` (which is what "optional" means), and
  `tests/test_wp4_manifests.py` fails the suite if a null or a stray schema key returns — the
  previous check required `options` and `schema` to hold identical key sets, which is what forced
  the nulls in.
- The image now takes the manifest version from the Supervisor build (`ARG BUILD_VERSION`) instead
  of relying on a hardcoded string, so the store, the log banner and the API report one number.

## 0.1.0

First release.

- Dynamic-price aware battery planning: 15-minute slots over a 12-48 h horizon, SoC dynamic
  programming with per-battery allocation.
- Solar forecast from Forecast.Solar (keyless), price forecast from a Zonneplan price sensor,
  energy-charts day-ahead or any sensor that publishes a forecast attribute.
- Load learning from long-term statistics (median weekday/weekend hourly profile, level scaling,
  EWMA bias correction, optional temperature term) with a flat fallback on a cold start.
- Modes `off`, `advisory` and `auto`, with `dry_run` in front of every write.
- Safety guard rails: SoC floor/ceiling, power and grid limits, ramp limit, stale-state hold,
  override with a bounded timer, `Stop now` kill switch, failsafe 0 W on error.
- Own web UI served through Home Assistant ingress (no build step, no CDN assets).
- Publishes `sensor.sem_*` state back to Home Assistant over the state API.
- Savings accounting against a do-nothing baseline; accuracy reporting for PV, load and price.
- Zero third-party Python dependencies: the Python standard library only.
