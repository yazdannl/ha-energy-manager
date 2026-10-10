# Changelog

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
