# Changelog

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
