# Changelog

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
