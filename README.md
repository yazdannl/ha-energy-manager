# Smart Energy Manager

A Home Assistant add-on that plans — and, if you let it, executes — battery dispatch to minimise
electricity cost, from four inputs: a solar production forecast, a dynamic price forecast, a learned
household load profile and live battery/grid measurements.

**It ships inert.** The default mode is `advisory` with `dry_run: true`, so a fresh install computes
and displays plans but cannot write to any device. The add-on is pure standard-library Python: there
are **zero third-party Python dependencies** (`energy_manager/requirements.txt` is empty on purpose),
no MQTT, no cloud account, and no YAML to install by hand.

## What it does

- **Arbitrage and self-consumption.** A dynamic program over the state of charge picks the cheapest
  hours to charge and the most expensive to discharge, respecting your battery's SoC floor and
  ceiling, power limits, grid import/export limits, a ramp limit and its **round-trip efficiency**, so
  a cycle is only planned when the sale price really covers the energy the battery loses and the wear
  it causes (see [Battery round trip](#battery-round-trip-efficiency)).
- **PV surplus first.** When the solar forecast says power is coming, charging is scheduled against
  that surplus instead of buying it.
- **Learns your house.** The load forecast is a median weekday/weekend hourly profile over the last
  8 weeks from Home Assistant long-term statistics, level-scaled to the last 7 days, with an EWMA
  bias correction and an optional outdoor-temperature term.
- **Learns your PV.** With bias correction on, realised PV is compared with the forecast per
  hour-of-day bucket and the forecast is scaled to match what the array actually does.
- **Keeps the pack balanced.** Optionally schedule a full charge every N days (off by default) so
  the BMS can balance its cells: while a battery is due, the plan aims for the target SoC in the
  cheapest slots before the deadline instead of force-charging at peak. Reaching the target is
  measured, an overdue cycle alerts and keeps trying, and advisory mode still plans it without
  writing.
- **Knows what the energy in the pack cost.** A per-battery cost-basis ledger values the energy
  actually held from the measured charge intervals and the price each was charged at (grid at the
  buy price, PV at the export price it forgoes), books that energy on the measurement grid so a
  command shorter than the sample spacing still gets its share, and pulls the quantity back onto
  the pack's own measured SoC content when the samples lag (the basis itself is never touched). By
  default it refuses a discharge or export below that break-even (`optimizer.sell_floor_enabled`,
  with an optional `optimizer.sell_floor_margin_eur_per_kwh`) so the plan cannot sell your pack's
  energy at a loss. The floor only ever restricts a discharge: in a PV-surplus hour the pack holds
  while the PV exports, so a cheaper later hour still wins. The ledger resets when the pack reaches
  its floor, survives restarts, and is exposed per battery in `/status` plus
  `sensor.sem_battery_cost_basis` and `sensor.sem_battery_value`.
- **Explains itself.** Every planning decision and every blocked dispatch carries a stable reason
  slug, visible in the log, so "why didn't it charge?" always has an answer.
- **Shows the money.** Savings are accounted against a do-nothing baseline (defined below) and
  reported per day, week, month, year and all time, with forecast accuracy for PV, load and price.
- **Mirrors its state into Home Assistant** as `sensor.sem_*` entities you can chart and automate.

## Requirements

- Home Assistant **OS or Supervised** (add-ons are not available in HA Container or Core).
- A **price source**, any one of: a Zonneplan price sensor entity, the keyless energy-charts
  day-ahead API, any sensor that publishes a forecast attribute (Nordpool, Tibber, ENTSO-E, ...), or
  a static schedule you enter yourself.
- A **battery** that Home Assistant can control, any one of: a Zendure SolarFlow through the
  community `zendure_ha` integration (entities are discovered and you confirm the mapping), a Zendure
  SolarFlow through its **own local zenSDK HTTP API** (`zendure_native`: no integration, no MQTT
  broker, no cloud — enter the device's address and serial), or any battery with a writable power
  setpoint entity (generic driver).
- Zendure hardware needs no third-party Python package: the native driver uses the standard library
  alone (`urllib`/`ssl`) and `energy_manager/requirements.txt` stays empty.
- Optional: outside temperature and a measured PV power sensor, for better forecasts.

## Install

1. Settings → Add-ons → Add-on store → ⋮ (top right) → **Repositories**.
2. Add `https://github.com/yazdannl/ha-energy-manager` and confirm.
3. Refresh the store page and install **Smart Energy Manager**, then **Start** it.
4. Open the **Energy** panel (it appears in the sidebar; the UI is served through Home Assistant
   ingress, so there is no port to open and no extra login).
5. First run: check the location the add-on read from Home Assistant, add your battery and confirm
   the discovered entity mapping, and choose your price source.
6. Leave the mode at `advisory` for a few days and watch the plan and the log. When you trust it,
   switch to `auto` — *with* `dry_run` still on — and look at the log of would-be writes. Only then
   turn `dry_run` off.

### What you will see (screenshots in words)

- **Dashboard** — now-cards for battery SoC, PV, house load, grid flow, current price and its rank
  against the next 24 h, the current action with its reason, the autopilot state, and the next cheap
  and next expensive window. Below them, a 24 h SoC/price mini-chart, an alert strip, mode switches
  and a **Stop now** button.
- **Plan** — one aligned timeline with price bars, PV and load areas, the SoC line and
  charge/discharge bars, plus a per-slot table with the reason for every slot and the plan summary.
  The price chart marks the break-even sale price for the round trip the plan assumed, so a sell price
  that looks like a profit but does not cover the conversion loss is visibly below the line; such a
  slot stays idle and says `efficiency_loss`. **Next actions** lists the coming action windows with
  the data behind each one: the buy and sell price, the break-even and the margin above it, the kWh
  in and out, the SoC path and the floor a reserve hold defends, the planned grid flow and what it
  costs or earns — and what actually happened, from the measured 5-minute rows and the dispatch log
  (applied vs requested, clamped, dry run, or why a slot was never dispatched). A window whose price
  is stale says so and is never presented as dispatchable, because the dispatcher refuses a stale
  slot rather than acting on a flat price.
- **Forecasts** — three panels (price, solar, load) with source, freshness badge, greyed stale
  slots, a P10/P90 band for load, per-day totals, and the 14-day accuracy trend.
- **Batteries** — one card per battery with live power, SoC, limits, mapping state and a bounded test
  discharge that is disabled unless the mode is `auto` and `dry_run` is off. The card shows the round
  trip you entered, what a stored kWh effectively costs at the current buy price, and the break-even
  sale price that follows from it.
- **Savings** — today, week, month, year and all time against the baseline, with import/export kWh
  and a plain-language explanation of what the baseline means.
- **Settings** — four setup cards (Price, Meter, Solar, Battery) with searchable entity pickers for
  the primary choices, plus an **All settings** drawer holding the grouped, searchable form generated
  from the server, including the safety limits.
- **Log** — every dispatch attempt with requested versus applied power and the blocking reason,
  plus alerts and events.

## Configuration

Add-on options are the small set needed at first boot:

| Option | Default | Meaning |
| --- | --- | --- |
| `mode` | `advisory` | `off`, `advisory` (plans only, no setpoint writes) or `auto` — see the safety model. |
| `dry_run` | `true` | In `auto`, log the exact writes without performing them. |
| `log_level` | `info` | `debug`, `info`, `warning` or `error`. |
| `currency`, `timezone` | `null` | `null` means "read from Home Assistant". |
| `latitude_override`, `longitude_override` | `null` | Only needed if Home Assistant has no location; `null` never invents coordinates. |

Everything else — price source, solar planes, load model, optimizer limits, notifications and the
battery list — lives in the UI settings and is persisted in the add-on's own database, so it
survives restarts and never appears in this repository.

### Notable settings (UI → Settings)

The Settings page opens on four cards — Price, Meter, Solar and Battery — and everything else lives in
one collapsed **All settings** drawer (same fields, grouped by topic, with an `Advanced (n)`
disclosure per group).

| Setting | Default | Meaning |
| --- | --- | --- |
| `price.provider` | `entity` | Where prices come from: `entity` (pick a sensor), `zonneplan` (found automatically), `day_ahead` (energy-charts wholesale) or `fixed`. |
| `price.export_fee_eur_per_kwh` | `0.0` | Feed-in cost, deducted per kWh from the sell price. |
| `price.feed_in_energy_tax_refund` | `true` | On: the sell price is the buy price minus the feed-in cost. Off: the energy-tax component is not paid back and is subtracted too. |
| `price.feed_in_eur_per_kwh` / `price.feed_in_entity` | unset | Optional explicit feed-in tariff or live sensor; either one overrides the derived price. |
| `grid.power_entity` | unset | Signed grid power sensor (positive = import). Enables the load forecast and the meter-derived solar estimate. Left empty, it is read from your Home Assistant Energy dashboard. |
| `solar.source` | `forecast_solar` | The single source used when `solar.sources` is empty: `forecast_solar` (per-plane API), `capacity` (enter the array's Wp) or `derived` (learn the array from the meter's export moments; also used automatically when `forecast_solar` has no planes and a meter resolves). |
| `solar.sources` / `solar.merge_strategy` | unset / `weighted_mean` | Optional merge list `[{source, weight, ...}]` that combines several forecasts into one series: `forecast_solar`, `ha_energy` (every solar-forecast provider already configured in your Home Assistant **Energy dashboard**, e.g. Solcast), `capacity` and `derived`, each with a finite weight `> 0`. The strategy is `weighted_mean`, `median` or `max`; a failed source is skipped, and an empty list keeps `solar.source` alone. The Solar card edits the list with add/remove/reorder. |
| `optimizer.allow_export` | `false` | Export scheduling stays off until you enable it. |
| `optimizer.sell_floor_enabled` | `true` | Refuse a discharge/export below the break-even of the energy actually in the pack (the cost-basis ledger, learned from measured charge intervals and the prices they were charged at). With no ledger yet, or turned off, the plan behaves exactly as before. `optimizer.sell_floor_margin_eur_per_kwh` (0..1, default 0) demands that much extra. The floor never forces a charge: in a PV-surplus slot the pack holds while the PV exports. |
| `optimizer.pv_charge_preference` | `auto` | How the plan prices PV energy it charges into the pack: `auto` (the export price it forgoes — the economically exact default, so PV wins exactly when its export price is below the buy price), `prefer_pv` (free: absorb the surplus before buying anything) or `avoid_pv` (the slot's buy price: export the PV and charge from the grid in the cheapest hours). A shadow price for planning only — the reported plan money stays the true grid cost. |
| `protection.cell_voltage_enabled` | `false` | Watch the lowest cell voltage (a mapped `cell_voltage_entity`, or the `zendure_native` driver's own read) and latch an emergency charge below `protection.cell_voltage_cutoff_v`; blocked from discharging until it recovers to `protection.cell_voltage_resume_v`. `protection.emergency_charge_power_w` 0 = the battery's own `max_charge_w`. See the safety model. |
| `maintenance.full_charge_enabled` | `false` | Reach `maintenance.full_charge_target_soc_pct` (default 100) every `maintenance.full_charge_interval_days` (default 30) days so the BMS can balance its cells. While a battery is due the plan uses the terminal-target mechanism in the cheapest slots before the deadline; the due date and last completion are per-battery runtime data, an overdue cycle alerts and keeps trying, and advisory mode never writes. |

If the **Meter** card is left empty, the add-on resolves the meter from your Home Assistant **Energy
dashboard** (the same grid source the Energy panel already uses): the dashboard's signed power sensor
for live sampling and control, or its import/export energy counters as a long-term-statistics history
source when no power sensor is configured. An explicit `grid.*` value always wins. The Meter card
shows which roles were resolved; a saved value is labelled as an override of the dashboard, and
**Use the Energy dashboard** clears it back to the automatic path.

**The sell price is derived from the buy price by default**: `sell = buy − feed-in cost`, minus the
energy-tax component when `price.feed_in_energy_tax_refund` is off. That is what a saldering contract
pays back; if your contract works differently, enter an explicit feed-in tariff or sensor, or set the
feed-in cost. `optimizer.allow_export` still defaults to off, so nothing is exported until you ask
for it.

### Battery round trip (efficiency) and wear

Each battery takes **one** efficiency number: its **round trip**, in percent — how much of the energy
put in comes back out. The add-on splits it evenly between the two directions
(`eta_charge = eta_discharge = √(round trip)`); if you know the charger and inverter figures
separately, enter those instead (the *charge/discharge efficiency* override) and the round trip is
their product. The shipped defaults are 95 % per direction, just over 90 % round trip.

Wear comes from the pack's own economics, not a number you have to look up: enter the **purchase
price** and the **cycle life** on the battery card and the add-on derives

```
wear = price / (cycle_life × usable kWh × η_dis)   # EUR per kWh delivered
```

Dividing by the AC energy a full discharge delivers (`usable kWh × η_dis`) is what makes a full
cycle cost exactly `price / cycle_life`. For example 3000 EUR over 6000 cycles on 9.6 kWh usable at
a 95 % discharge efficiency is about 5.5 ct per delivered kWh. Charging alone costs no wear, because
wear is charged per kWh actually delivered to the house. Leave the fields empty and the battery
plans as wear-free. The advanced `optimizer.cycle_cost_eur_per_kwh` setting overrides the derived
value when you want to model something the price/life pair cannot express.

A cycle loses energy, so the buy price is not the break-even price:

```
break_even_sell = buy / round_trip + wear
```

`round_trip` is `eta_charge × eta_discharge`. The wear term needs no per-direction efficiency because
it is already stated per delivered kWh.

| Round trip | Break-even at 20 ct/kWh buy, no wear | With 5.2 ct/kWh derived wear |
| --- | --- | --- |
| 75 % | **26.7 ct** | 31.9 ct |
| 80 % | 25.0 ct | 30.2 ct |
| 90 % | 22.2 ct | 27.4 ct |

Buying at 20 ct with a 75 % round trip breaks even at **26.7 ct, not 25 ct** — 25 ct is the 80 % case
(`20 / 0.8`). With no wear it reduces to `buy / round_trip`: 20 ct at 90 % gives 22.2 ct.
The common mistake is to compare the sale price with the buy price and book a profit on a cycle that
actually loses energy; the add-on prices the loss instead.

Where you see it: the battery card shows the round trip, the derived wear (price ÷ (cycle life ×
usable kWh)), the effective cost of a stored kWh at the current buy price and the break-even sale
price that follows from it, and the Plan view marks the assumed round trip, wear and break-even line
against the sell prices. A slot that stays idle because
the spread does not cover the loss reports `efficiency_loss` — that is the conversion loss, distinct
from your own `optimizer.min_spread_eur_per_kwh` floor, which reports `spread_below_threshold`.

With several batteries the plan works on capacity-weighted mean efficiencies and delivered-energy-
weighted mean wear (a documented approximation when packs differ materially), spends the most
efficient battery first, and reports the aggregate round trip and wear it assumed. Break-even is still computed per
battery from its own values, so a cheap old pack and an efficient new one are not averaged into one
misleading number.

### Learn each pack's real usable capacity

The capacity you type is the label, and a label is a promise rather than a measurement: a new pack
may hold less, and an aged one certainly does. Set a battery's **capacity source** to **learned** and
the add-on measures the pack's real usable capacity from its own cycles, using the capacity you typed
only as the seed and fallback.

Each measured charge or discharge window pairs the AC energy it moved with the SoC it moved, with
that direction's efficiency on the right side of the division
(`capacity = AC × η_charge / dSoC` while charging, `capacity = AC / η_discharge / dSoC` while
discharging). A window only counts if the SoC moved at least 10 points, the average power was at
least 20 % of the battery's own limit, every value is finite and no measurement is missing beyond a
single 5-minute sample. Accepted windows are combined with a median after an outlier filter, and at
least three are needed before the estimate is used. Estimates older than 30 days are ignored until
they are refreshed. **Manual always wins**: leave the source on manual, or let a learned estimate go
stale, and the capacity you typed is used unchanged. Nothing is ever guessed — when there is no
usable estimate the seed is used and the battery card says why.

The battery card shows the effective capacity, the estimate, how many windows it was built from and
when it was last learned; the Edit dialog switches between manual and learned through the normal
save flow. The estimate is stored as runtime data, never written back over your typed capacity.

### Data footprint

The add-on reads **only the entities you mapped** (targeted state reads, never a full Home Assistant
state snapshot) and upserts measurements into 5-minute buckets: roughly 2 000 measurement rows and
20 MB of JSON per day regardless of the tick rate, with plan history bounded to the 200 most recent
plans. Everything stays in the add-on's own SQLite database in `/data`.

### Access and authentication

Ingress is the supported path: Home Assistant proxies the panel and supplies the login, and the
add-on **does not publish a port**. The HTTP API has no authentication of its own — anyone who can
reach it can switch the mode to `auto` or turn `dry_run` off, which moves real hardware. Do not add
a `ports:` mapping for 8099 or expose it through a reverse proxy unless you accept handing battery
control to everyone who can reach that address.

## Sensors published to Home Assistant

The add-on writes these entities with the Home Assistant state API (no MQTT, no YAML). The
`sensor.sem_*` namespace is a stable contract.

| Entity | Unit | Attributes |
| --- | --- | --- |
| `sensor.sem_autopilot_state` | `off` / `advisory` / `auto` / `dry_run` | — |
| `sensor.sem_plan_action` | action slug | `reason`, `target_power_w`, `soc_target_pct` |
| `sensor.sem_price_now` | EUR/kWh | `rank_pct`, `cheap_window_start`, `expensive_hour_start` |
| `sensor.sem_pv_forecast_today` | kWh | `tomorrow_kwh`, `peak_w`, `peak_at` |
| `sensor.sem_load_forecast_now` | kW | `today_kwh`, `confidence` |
| `sensor.sem_savings_today` | EUR | `month_eur`, `year_eur`, `baseline_kwh` |
| `sensor.sem_battery_soc_avg` | % | — |
| `sensor.sem_battery_cost_basis` | EUR/kWh | `stored_value_eur`, `energy_kwh`, per-battery basis (state `unknown` without a ledger) |
| `sensor.sem_battery_value` | EUR | `cost_basis_eur_per_kwh`, `sell_floor_eur_per_kwh` |
| `sensor.sem_forecast_accuracy` | % | `pv_mape`, `load_mape`, `price_mae` |
| `binary_sensor.sem_autopilot_active` | on/off | — |
| `binary_sensor.sem_battery_at_floor` | on/off | — |

Publishing is idempotent and throttled (at least 20 s between writes per entity), and is skipped
entirely when Home Assistant is unreachable — the control loop never waits for a state publish.
They are written with the Home Assistant state API (`POST /api/states/<entity_id>`) and therefore
carry no `unique_id`: they are not part of the entity registry and **disappear after a Home
Assistant restart until the add-on republishes them** on its next tick. That is expected, and
automations that use them should tolerate a short gap after a restart.

## Safety model

The shipped defaults cannot move hardware, and the guarantees are enforced in code and tested:

1. **`advisory` and `dry_run` installs issue zero writes.** That holds during ticks, on `Stop now`,
   in `off`, on SIGTERM/SIGINT and on a fatal error. The one exception is neutralising a real
   setpoint that this add-on itself left behind: every verified non-zero setpoint is recorded in a
   per-battery **live-write latch**, and only a latched battery may be neutralised. An
   `advisory`/`dry_run` install that never commanded anything is never written to, not even with a
   hold; in `auto` with `dry_run` off the exit hold covers every enabled battery, latched or not,
   because that is the one case where a stale setpoint must not survive.
2. **The latch survives restarts.** It is persisted in the add-on's database, and on the next start
   an outstanding setpoint is neutralised *before* the control loop resumes. That is the mitigation
   for a battery having no watchdog (see the Zendure section below) when the process is killed
   (SIGKILL/OOM/container kill) or the machine reboots.
3. `dry_run: true` (default) means that even in `auto` the exact write that *would* happen is
   computed, validated and logged as a dry-run dispatch instead of being sent.
4. Every requested power passes an ordered check before it reaches a device: mode, override/kill
   switch, measurement staleness (hold if older than the configured limit), Home Assistant and
   battery availability, the cell-voltage guard, SoC floor and ceiling, per-battery and aggregate
   power limits, grid import and export limits, and finally a 1000 W per tick ramp limit. The checks
   are pure functions, so the whole table is unit-tested without hardware.
5. **Minimum cell-voltage protection (off by default).** With `protection.cell_voltage_enabled` on,
   the add-on watches the lowest **cell** voltage — a mapped `cell_voltage_entity`, or the
   `zendure_native` driver's own read — and never guesses one. Below the cutoff the battery is
   latched into an emergency charge (discharge blocked, charged at
   `min(max_charge_w, emergency_charge_power_w or max_charge_w)` regardless of price/plan, still
   inside max SoC and the ramp); between cutoff and resume it is only blocked from discharging; at
   or above resume the latch clears. A stale reading keeps the previous latch (fail-safe) and is
   noted, a never-seen voltage leaves it inactive, and the latch survives a restart. `advisory` and
   `dry_run` still alert and log it, without a write.
6. **No interleaved writers.** Control ticks, `Stop now` and mode changes are serialised behind one
   engine control lock, and every battery driver serialises its own device writes, so a stop cannot
   race a tick that is already writing and the exit hold cannot interleave with one. If the process
   is killed before it can hold and a setpoint was live, the persisted latch makes the next start
   neutralise it.
7. **`Stop now` is atomic and honest.** Authority is revoked first (mode `advisory`, persisted and
   published), any in-flight tick is drained, and only then is hardware neutralised — for real, and
   only where a setpoint was actually left running. The response reports the mode that is really in
   force, and `pending_holds` in the status shows a neutralising hold that is still outstanding.
8. **Leaving live control is a real write.** Switching to `advisory`/`off`, turning `dry_run` back
   on, or PATCHing the settings while the add-on is live neutralises the batteries for real instead
   of merely stopping new setpoints.
9. A blocked or clamped action is always visible: an alert, a dispatch-log row and a banner in the
   UI, each with a stable reason slug. On an unrecoverable error the failsafe writes 0 W (hold)
   rather than guessing a setpoint.

### Zendure SolarFlow: read this before you uninstall

- **There is no device watchdog.** The Zendure integration does not reset the last setpoint on its
  own, so whatever was written last stays in force if this add-on stops — including after you
  uninstall it. The add-on writes a hold (0 W) setpoint on a clean shutdown and on a fatal error,
  and if the process was killed without that chance, the persisted live-write latch makes the next
  start neutralise the outstanding setpoint before control resumes. Nothing can do that if the
  add-on never starts again, so if you stop using it (or uninstall it), put the Zendure manager back
  into one of its own modes (its smart mode) or switch the device off; do not leave it in manual
  with a stale setpoint.
- **The manager's smart modes overwrite external writes.** That is why the add-on switches the
  Zendure manager to `manual` before it takes control and keeps re-asserting manual mode while it is
  in control. If you switch the manager back to a smart mode yourself, your mode wins and the
  add-on's setpoints are ignored — the log will show the driver reporting a mismatch.
- **A write only takes effect when the device processes its next P1 event.** The setpoint can
  therefore take a short while to show up in the device state. The dashboard and the log show
  *requested* versus *applied* power for exactly this reason: a brief difference is normal, a
  persistent one is a driver error worth investigating.
- **The native driver (`zendure_native`) talks to the device directly and needs neither the
  integration nor a broker.** Choose it in the battery wizard and enter the device's IP address (or
  mDNS name) and serial. Enable the device's own local API first — in the Zendure app, add HEMS and
  exit to apply — otherwise the device refuses the connection and the add-on says so. The add-on
  reads the device (SoC, AC power, PV, the device's own limits and pack voltages) and writes the
  documented property command, clamped to both your limit and the device's own maximum; every write
  is read back from the device and a mismatch is a driver error, so the failsafe hold still governs
  failures. The device reports no capacity in kWh, so the capacity you enter is what the planner
  uses. Saving probes the device once: an unreachable address or a mismatched serial is refused
  unless the stored mapping sets `allow_unreachable`. This transport is verified against the vendor's
  documented API and an in-process fake device; **live-hardware validation is still pending** — if
  you run it on real hardware, the log's per-write messages are the evidence to check (see
  `docs/EXTERNAL_APIS.md` section 6). The device's local MQTT path is documented as planned work, not
  shipped.
- **On the device path there is still no watchdog.** A `zendure_native` hold writes 0 W to the
  device's AC limits and verifies it, but the device itself may keep inverting for an off-grid port;
  the add-on's hold is an AC-limit command, not a power switch.

## Savings baseline

`savings = baseline − actual`, where

> **the baseline** is the *net* cost the same realised consumption would have had with the battery
> doing nothing: the import cost at the same prices **minus the export revenue** that same PV
> surplus would have earned, with no battery flows.

For every metered interval the add-on recomputes the import cost and the export revenue from the
metered load and PV with the battery power set to zero, and compares that with what actually
happened. Including the export credit is deliberate: leaving it out made a household with no battery
look like it was saving money.

If the load for an interval is not knowable (no load entity mapped and no grid sensor to derive it
from), the baseline cannot be computed: the baseline and the savings for that bucket are reported as
**unknown** — the UI shows `--` rather than a fabricated zero.

Baselines are computed **before** old raw rows are trimmed and stored in `savings_daily`, so the
history stays comparable across retention limits.

Battery credit is **measured, not planned**. `charged_kwh` and `discharged_kwh` integrate only
confirmed AC power readings, and `charge_cost_eur`/`discharge_value_eur` price those flows with the
buy/sell price in force at flow time. A dry-run, blocked or stale-refused dispatch, a setpoint the
battery never confirmed, or a driver that only reads back the value it wrote (the generic driver, or
a Zendure with an incomplete measured pair) earns **no** flow credit: the day's battery flows and
cycle count are reported as **unknown** (`--`) instead of the planned value. Each day also records
which series (grid, load, PV, battery) were measured for every interval, and the Savings view shows
that coverage and the measured flows under the per-bucket detail.

Two honest caveats: the baseline is a counterfactual re-pricing of the *same* kWh, not a measured
change in your bill, and it only includes the price components present in the price series you
selected (fixed charges, taxes outside that series and any behaviour change the add-on itself
caused are not part of the number).

## Privacy: what leaves your system

There are exactly three outbound dependencies. All are called only while the matching provider is
enabled, and all three are keyless by default. (Reading a solar forecast that Home Assistant itself
holds — the `ha_energy` merge source — is a local WebSocket call to your own instance, so it is not
an outbound dependency.)

- **`api.forecast.solar`** (solar forecast, enabled by default): the request carries your site
  coordinates and the plane geometry you configured (kWp, tilt, azimuth, damping) — that is what the
  service needs to estimate production. No entity data, no consumption data, no identifiers, no
  account.
- **`api.energy-charts.info`** (day-ahead prices; `api.energy-charts.info/price?bzn=...`): the
  request carries only the bidding zone code (for example `NL`). No personal data is sent to it.
- **`power.larc.nasa.gov`** (NASA POWER observed irradiance, on by default for the capacity and
  meter-derived solar sources): the request carries your site coordinates and a recent date window —
  that is what a point irradiance lookup needs. No entity data, no consumption data, no identifiers,
  no account. It is a public NASA service.

The day-ahead price data served by energy-charts is licensed **CC BY 4.0** and is provided by
**Fraunhofer ISE (Energy-Charts.info)**, with the underlying data from
**Bundesnetzagentur | SMARD.de**. That attribution is required by its licence and applies while you
use that price source; `api.energy-charts.info` is called **only when the `energy_charts` price
source is actually selected**.

If your configured price source is the **Zonneplan Home Assistant sensor**, no price request leaves
your system at all — the prices are read from your own Home Assistant entity. With the
energy-charts source, only the zone code leaves.

Nothing else is sent anywhere. Your Home Assistant entity data stays local: no entity ids, states,
history, measurements, plans, savings or settings are uploaded to any service, and there is no
telemetry, analytics, crash reporting, account or cloud storage. Plans, measurements, learned
profiles and settings live in the add-on's SQLite database in its own `/data` volume on your
machine. The repository contains no instance data either — that is enforced by `tools/audit.sh`,
which fails on coordinates, private IP addresses, real-looking entity ids, credentials and email
addresses.

## Troubleshooting

| Symptom | Likely cause and what to check |
| --- | --- |
| No plan, or "forecast unavailable" alerts | The configured price source returned no future slots. Check the Log for the failing source; the add-on falls back to energy-charts and then to a static schedule, but it never plans against an empty or silently stale forecast. |
| Prices look greyed out / "stale" | Your price sensor's forecast attribute is older than the plan window. The add-on still shows them, but treats stale future prices as flat instead of guessing. |
| Nothing moves the battery | Expected in `advisory` and in `auto` with `dry_run` on. Otherwise read the Log: every rejection has a reason (SoC floor, power clamp, grid limit, ramp limit, stale measurement, override). |
| A Zendure battery is not discovered | The `zendure_ha` integration must be set up and the device reachable (cloud/BLE). Discovery matches translation keys, so renamed entity ids still resolve; the mapping is shown for confirmation before it is stored. A device with **no** integration installed is not discovered at all: add it manually with the `zendure_native` driver and its address + serial. |
| A `zendure_native` battery is refused at save time | The add-on read the device's local API and did not get a usable report: the local API is disabled (in the Zendure app, add HEMS and exit to apply), the address/port is wrong, the serial does not match, or a configured token is rejected (401). The error names the cause; `allow_unreachable` in the stored mapping is the deliberate opt-out, after which the battery card shows the read error instead. |
| A generic battery is rejected | A generic battery needs either a signed power setpoint entity or a charge/discharge entity pair. It is refused when you save, not silently at dispatch time. |
| Times look shifted | Timezone and currency are read from Home Assistant; override them in the options only if Home Assistant itself is misconfigured. |
| Costs look wrong by a factor | Check `price.unit` and `price.scale_divisor` against your provider. The add-on uses the provider's declared unit and never guesses magnitude, because a wrong guess corrupts every cost number. |
| The add-on will not start | It needs Home Assistant OS or Supervised; the log says so if the Supervisor token is missing. |
| Charging at the wrong time | Check `min_spread_eur_per_kwh` (thin arbitrage is ignored on purpose), the cycle cost, the discharge target and whether PV surplus is expected later in the horizon. |
| The battery stays idle at a price that looks profitable | The sell price does not clear the round-trip break-even, so the slot says `efficiency_loss`: at 20 ct buy and a 75 % round trip the battery needs 26.7 ct, plus the cycle cost per kWh delivered. Check the round trip and break-even on the battery card, and `optimizer.cycle_cost_eur_per_kwh`. See [Battery round trip](#battery-round-trip-efficiency). |

## Development

```sh
python3 -m compileall -q energy_manager/app tools/make_icons.py tests   # syntax check
python3 -m pytest tests -q                                             # test suite (pytest + PyYAML)
sh tools/audit.sh                                                      # PII / secret tripwire
python3 tools/make_icons.py                                            # regenerate icon and logo
docker build -t sem-test --build-arg BUILD_ARCH=amd64 energy_manager/  # build the add-on image
```

The Python code targets the standard library only, so `requirements.txt` is empty and the add-on
image installs nothing. Running the tests additionally needs `pytest` and `PyYAML` (used by
`tests/test_wp4_manifests.py`, which checks the add-on manifests against the rules Supervisor
applies when it starts an add-on).

## Licence

MIT — see [LICENSE](LICENSE).

Data and asset credits:

- **energy-charts.info day-ahead prices** (used only when the `energy_charts` price source is
  selected) are licensed **CC BY 4.0**, provided by **Fraunhofer ISE (Energy-Charts.info)** with the
  underlying data from **Bundesnetzagentur | SMARD.de**.
- The bundled **uPlot** chart library is vendored (no CDN) under the **MIT** licence; its licence
  text is at `energy_manager/app/web/vendor/LICENSE.uPlot.txt`, and the version, checksums and
  refresh procedure are recorded in `energy_manager/app/web/vendor/README.md`.
