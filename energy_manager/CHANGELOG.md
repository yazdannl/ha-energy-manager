# Changelog

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
