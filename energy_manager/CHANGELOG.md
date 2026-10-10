# Changelog

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
