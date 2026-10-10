"""Entrypoint: bootstrap, serve the API, start the engine (SPEC 15).

This is the only module that touches the WP2A modules (``config``, ``store``, ``harest``, ``haws``,
``sources.*``); everything is imported and wired defensively so a missing piece is logged clearly
instead of taking the add-on down.  ``--selfcheck`` boots the whole stack (real settings and store
when they are available, in-process fakes for Home Assistant) and exits 0 without a live HA.
"""

from __future__ import annotations

import argparse
import atexit
import importlib
import logging
import os
import shutil
import signal
import sys
import tempfile
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Sequence

from .api import ApiServer
from .engine import Engine
from .models import (
    BatteryConfig,
    LoadForecast,
    LoadSlot,
    Mode,
    PriceForecast,
    PriceSlot,
    SolarForecast,
    SolarSlot,
    utcnow,
)
from .notify import Notifier

log = logging.getLogger("app.main")

APP_VERSION = os.environ.get("SEM_VERSION") or os.environ.get("BUILD_VERSION") or "0.1.2"
COMMIT = os.environ.get("SEM_COMMIT") or os.environ.get("GITHUB_SHA") or ""
DEFAULT_PORT = 8099
DEFAULT_OPTIONS = "/data/options.json"
DEFAULT_DATA_DIR = "/data"


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _import(module: str) -> Any:
    """Import a WP2A module, or None when it is not installed (selfcheck / partial tree)."""
    try:
        return importlib.import_module(f"app.{module}")
    except ImportError as exc:
        log.warning("app.%s is unavailable: %s", module, exc)
        return None


def _callable(module: Any, name: str) -> Callable[..., Any] | None:
    function = getattr(module, name, None) if module is not None else None
    return function if callable(function) else None


def _watts(value: Any) -> float | None:
    """kW (as the measurement layer reports) -> W, or None."""
    try:
        return None if value is None else float(value) * 1000.0
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------------------------
# Wiring the WP2A modules
# ---------------------------------------------------------------------------------------------


def build_store(data_dir: str | None) -> Any:
    module = _import("store")
    store_class = _callable(module, "Store")
    if store_class is None:
        log.warning("app.store.Store is unavailable: running without persistence")
        return None
    path = os.path.join(data_dir or DEFAULT_DATA_DIR, "energy_manager.db")
    try:
        return store_class(path)
    except Exception as exc:
        log.error("cannot open the store at %s: %s", path, exc)
        return None


def build_settings(store: Any, options: Mapping[str, Any], options_path: str) -> Any:
    module = _import("config")
    settings_class = _callable(module, "Settings")
    if settings_class is None:
        log.error("app.config.Settings is unavailable: cannot start")
        return None
    try:
        return settings_class(store, dict(options) or None, options_path=options_path)
    except TypeError:
        try:
            return settings_class(store, dict(options) or None)
        except Exception as exc:
            log.error("cannot construct app.config.Settings: %s", exc)
            return None
    except Exception as exc:
        log.error("cannot construct app.config.Settings: %s", exc)
        return None


def build_ha() -> Any:
    module = _import("harest")
    client_class = _callable(module, "HAClient")
    if client_class is None:
        log.warning("app.harest.HAClient is unavailable: Home Assistant calls are disabled")
        return None
    try:
        client = client_class(
            os.environ.get("HA_URL") or None,
            os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN") or None,
        )
    except Exception as exc:
        log.error("cannot construct the Home Assistant client: %s", exc)
        return None
    if not getattr(client, "token", ""):
        log.warning("no SUPERVISOR_TOKEN/HA_TOKEN: Home Assistant calls will fail until it is set")
    return client


def resolve_location(settings: Any, ha: Any) -> tuple[Any, str]:
    """(typed location tuple or None, human label) from the add-on options + HA ``/api/config``."""
    ha_config: dict[str, Any] = {}
    if ha is not None:
        try:
            ha_config = ha.get_config() or {}
        except Exception as exc:
            log.warning("cannot read the Home Assistant config: %s", exc)
    resolver = _callable(_import("config"), "resolve_location")
    location: tuple[float, float, str] | None = None
    if resolver is not None:
        try:
            latitude, longitude, zone = resolver(ha_config, dict(getattr(settings, "options", {}) or {}))
            location = (latitude, longitude, zone)
        except Exception as exc:
            log.warning("no usable location, solar forecasting stays disabled: %s", exc)
            # never invent coordinates (SPEC 3)
    label = str(ha_config.get("location_name") or "")
    return location, label


def build_sources(
    settings: Any, store: Any, ha: Any, *, location: tuple[float, float, str] | None = None
) -> dict[str, Any]:
    """Forecast providers (SPEC 4/7), the measurement sampler and the discovery sources."""
    price_module = _import("sources.price")
    solar_module = _import("sources.solar")
    load_module = _import("sources.load")
    measure_module = _import("sources.measure")
    haws_module = _import("haws")

    price_load = _callable(price_module, "load_price_forecast")
    solar_load = _callable(solar_module, "load_solar_forecast")
    load_load = _callable(load_module, "load_forecast")
    measure_collect = _callable(measure_module, "collect")

    price_source = None
    if price_load is not None:
        price_source = lambda now: price_load(settings, ha, now=now)  # noqa: E731
    solar_source = None
    if solar_load is not None:
        solar_source = lambda now: solar_load(  # noqa: E731
            settings, store, ha, location=location, now=now
        )
    load_source = None
    if load_load is not None:
        load_source = lambda now: load_load(settings, store, now=now)  # noqa: E731

    # Confirmed grid roles (SPEC 3 ``grid.*``): the sampler and the derived load tier resolve them.
    grid_roles = {
        role: str(settings.get(key, "") or "")
        for role, key in (
            ("grid_power", "grid.power_entity"),
            ("grid_import", "grid.import_entity"),
            ("grid_export", "grid.export_entity"),
        )
    }
    grid_roles = {role: entity for role, entity in grid_roles.items() if entity}

    sampler = None
    if measure_collect is not None and ha is not None:
        def sampler(now: datetime) -> dict[str, Any]:
            measurements, actuals = measure_collect(
                ha, settings, settings.battery_list(), entities=grid_roles, now=now
            )
            if store is not None and measurements:
                # 5-minute measurement rows; the actuals row is written by the engine, which is
                # the only side that knows the planned/dispatched power.
                try:
                    store.add_measurements(measurements)
                except Exception as exc:
                    log.warning("cannot store measurements: %s", exc)
            return {
                "grid_w": _watts(actuals.get("grid_kw")),
                "pv_w": _watts(actuals.get("pv_kw")),
                "load_w": _watts(actuals.get("load_kw")),
                "price_buy": actuals.get("price_buy"),
            }

    registry_source = None
    ws_class = _callable(haws_module, "HAWebSocket")
    if ws_class is not None:
        def registry_source() -> list[dict[str, Any]]:
            with ws_class() as websocket:
                return list(websocket.list_entity_registry())

    states_source = None
    if ha is not None and _callable(ha, "get_states") is not None:
        def states_source() -> dict[str, Any]:
            return {
                str(row.get("entity_id")): row
                for row in (ha.get_states() or [])
                if row.get("entity_id")
            }

    return {
        "price_source": price_source,
        "solar_source": solar_source,
        "load_source": load_source,
        "sampler": sampler,
        "registry_source": registry_source,
        "states_source": states_source,
    }


def build_maintenance(settings: Any, store: Any, ha: Any, *, timezone_name: str | None = None):
    """Daily hook: learn the load profile from history, then apply store retention (SPEC 7.2/11)."""
    load_module = _import("sources.load")
    haws_module = _import("haws")
    collect_history = _callable(load_module, "collect_history")
    update_profile = _callable(load_module, "update_load_profile")
    ws_class = _callable(haws_module, "HAWebSocket")

    # The derived load tier needs the confirmed grid roles (SPEC 3 ``grid.*``).
    derived = {
        role: str(settings.get(key, "") or "")
        for role, key in (
            ("grid_power", "grid.power_entity"),
            ("grid_import", "grid.import_entity"),
            ("grid_export", "grid.export_entity"),
        )
    }
    derived = {role: entity for role, entity in derived.items() if entity}

    def maintenance(now: datetime) -> None:
        # Each part is guarded: a history failure must not stop retention (SPEC 7.2/11).
        if collect_history is not None and update_profile is not None and ha is not None:
            try:
                history = None
                if ws_class is not None:
                    try:
                        with ws_class() as websocket:
                            history = collect_history(
                                settings, ha, ws=websocket, now=now, days=60,
                                derived_entities=derived,
                            )
                    except Exception as exc:
                        log.warning("load history over websocket failed: %s", exc)
                if history is None:
                    history = collect_history(
                        settings, ha, now=now, days=60, derived_entities=derived
                    )
                if history is not None and getattr(history, "ok", False) and history.samples:
                    update_profile(store, history.samples, now=now, tz=timezone_name)
                    log.info("load profile refreshed from %s samples", len(history.samples))
            except Exception as exc:
                log.warning("load profile learning failed: %s", exc)
        trim = _callable(store, "trim")
        if trim is not None:
            removed = trim(now=now)
            log.info("retention applied: %s", removed)

    return maintenance


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.main", description="Smart Energy Manager")
    parser.add_argument("--options", default=os.environ.get("SEM_OPTIONS", DEFAULT_OPTIONS))
    parser.add_argument("--data-dir", default=os.environ.get("SEM_DATA_DIR", DEFAULT_DATA_DIR))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SEM_PORT", DEFAULT_PORT)))
    parser.add_argument("--host", default=os.environ.get("SEM_HOST", "0.0.0.0"))
    parser.add_argument("--log-level", default=os.environ.get("SEM_LOG_LEVEL", "info"))
    parser.add_argument(
        "--selfcheck",
        action="store_true",
        help="boot against in-process fakes, exercise the stack and exit 0",
    )
    args = parser.parse_args(argv)
    _configure_logging(args.log_level)

    if args.selfcheck:
        return selfcheck(port=args.port)

    options: dict[str, Any] = {}
    config_module = _import("config")
    load_options = _callable(config_module, "load_options")
    if load_options is not None:
        try:
            options = dict(load_options(args.options) or {})
        except Exception as exc:
            log.error("cannot read options from %s: %s", args.options, exc)
    log.info("Smart Energy Manager %s starting", APP_VERSION)

    store = build_store(args.data_dir)
    settings = build_settings(store, options, args.options)
    if settings is None:
        log.error("configuration is unavailable; cannot start")
        return 2
    ha = build_ha()
    location, label = resolve_location(settings, ha)
    sources = build_sources(settings, store, ha, location=location)
    maintenance = build_maintenance(
        settings, store, ha, timezone_name=location[2] if location else None
    )
    engine = Engine(
        settings,
        store=store,
        ha=ha,
        notifier=Notifier(ha, settings),
        maintenance=maintenance,
        location=label,
        version=APP_VERSION,
        commit=COMMIT,
        **sources,
    )
    engine.start()
    _install_exit_holds(engine)
    api = ApiServer(engine, host=args.host, port=args.port, version=APP_VERSION)
    try:
        api.serve_forever()
    except BaseException as exc:  # fatal error: never leave the battery at its last setpoint
        log.exception("fatal error, holding the batteries before exit")
        engine.hold_on_exit("fatal_error")
        _ = exc
        raise
    finally:
        api.stop()
        engine.hold_on_exit("shutdown")
        engine.stop()
        close = _callable(store, "close")
        if close is not None:
            close()
    return 0


def _install_exit_holds(engine: Any) -> None:
    """Hold the batteries on SIGTERM/SIGINT and at interpreter exit (appendix A: no watchdog)."""

    def hold(reason: str) -> None:
        try:
            engine.hold_on_exit(reason)
        except Exception as exc:  # pragma: no cover - best effort on the way out
            log.warning("hold on exit failed: %s", exc)

    def handler(signum: int, _frame: Any) -> None:
        log.warning("signal %s received: neutralising the batteries", signum)
        hold("signal")
        engine.stop()
        raise SystemExit(0)

    for signal_name in ("SIGTERM", "SIGINT", "SIGHUP"):
        number = getattr(signal, signal_name, None)
        if number is None:
            continue
        try:
            signal.signal(number, handler)
        except (ValueError, OSError):  # pragma: no cover - non-main thread / unsupported
            pass
    atexit.register(lambda: hold("atexit"))


# ---------------------------------------------------------------------------------------------
# --selfcheck harness: real settings/store when present, in-process fakes for Home Assistant.
# ---------------------------------------------------------------------------------------------


class _FakeStore:
    """In-memory stand-in with the same method surface as ``app.store.Store``."""

    def __init__(self) -> None:
        self.dispatches: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.plans: list[Any] = []
        self.actuals: list[dict[str, Any]] = []
        self.savings: dict[str, dict[str, Any]] = {}
        self.learning: dict[tuple[str, str], Any] = {}
        self.measurements: list[Any] = []

    def save_plan(self, plan: Any) -> int:
        self.plans.append(plan)
        return len(self.plans)

    def add_dispatch(self, **entry: Any) -> int:
        self.dispatches.append(dict(entry))
        return len(self.dispatches)

    def add_event(self, **entry: Any) -> int:
        self.events.append(dict(entry))
        return len(self.events)

    def upsert_actuals(self, ts: Any, **fields: Any) -> None:
        self.actuals.append({"ts": ts, **fields})

    def actuals_range(self, start: Any, end: Any) -> list[dict[str, Any]]:
        return list(self.actuals)

    def upsert_savings_daily(self, day: Any, **fields: Any) -> None:
        self.savings[str(day)] = dict(fields)

    def savings_daily_range(self, start_day: str, end_day: str) -> list[dict[str, Any]]:
        return [{"day": day, **row} for day, row in self.savings.items()]

    def profile_get(self, kind: str, key: str) -> Any:
        return self.learning.get((kind, key))

    def profile_put(self, kind: str, key: str, payload: Any, updated_at: Any = None) -> None:
        self.learning[(kind, key)] = (updated_at, payload)

    def add_measurements(self, measurements: Any) -> int:
        rows = list(measurements)
        self.measurements.extend(rows)
        return len(rows)

    def trim(self, **kwargs: Any) -> dict[str, int]:
        return {}


class _SelfcheckSettings(dict):
    """Dict-backed settings with the ``Settings`` surface the engine uses."""

    def __init__(self, **values: Any) -> None:
        super().__init__(**values)
        self._batteries: list[BatteryConfig] = []

    def get(self, key: str, default: Any = None) -> Any:
        return super().get(key, default)

    def set(self, key: str, value: Any) -> None:
        self[key] = value

    def all(self) -> dict[str, Any]:
        return dict(self)

    def battery_list(self) -> list[BatteryConfig]:
        return list(self._batteries)

    def add_battery(self, battery: BatteryConfig) -> BatteryConfig:
        self._batteries = [b for b in self._batteries if b.id != battery.id]
        self._batteries.append(battery)
        return battery

    def update_battery(self, battery_id: str, patch: Mapping[str, Any]) -> BatteryConfig:
        for existing in self._batteries:
            if existing.id == battery_id:
                data = {**asdict(existing), **patch}
                return self.add_battery(
                    BatteryConfig(
                        **{
                            key: value
                            for key, value in data.items()
                            if key in BatteryConfig.__dataclass_fields__
                        }
                    )
                )
        data = {"id": battery_id, "name": battery_id, "driver": "generic", "capacity_kwh": 1.0}
        data.update(patch)
        return self.add_battery(
            BatteryConfig(
                **{
                    key: value
                    for key, value in data.items()
                    if key in BatteryConfig.__dataclass_fields__
                }
            )
        )

    def remove_battery(self, battery_id: str) -> bool:
        before = len(self._batteries)
        self._batteries = [b for b in self._batteries if b.id != battery_id]
        return len(self._batteries) != before


class _FakeHA:
    """Minimal HA facade: the entities referenced by the fake battery mapping only."""

    def __init__(self) -> None:
        self.states: dict[str, dict[str, Any]] = {
            "number.example_battery_power": {"state": "0", "attributes": {}},
            "sensor.example_battery_soc": {
                "state": "55",
                "attributes": {"unit_of_measurement": "%"},
            },
        }
        self.service_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.published: dict[str, Any] = {}

    def get_state(self, entity_id: str) -> dict[str, Any] | None:
        return self.states.get(entity_id)

    def get_states(self) -> list[dict[str, Any]]:
        return [{"entity_id": key, **value} for key, value in sorted(self.states.items())]

    def get_config(self) -> dict[str, Any]:
        return {}

    def call_service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        self.service_calls.append((domain, service, dict(data)))
        if domain in ("number", "input_number"):
            self.states[str(data.get("entity_id"))] = {
                "state": str(data.get("value")),
                "attributes": {},
            }
        elif domain in ("select", "input_select"):
            self.states[str(data.get("entity_id"))] = {
                "state": str(data.get("option")),
                "attributes": {},
            }

    def set_state(self, entity_id: str, state: Any, attributes: dict[str, Any] | None = None) -> None:
        self.published[entity_id] = {"state": state, "attributes": dict(attributes or {})}


EXAMPLE_BATTERY = BatteryConfig(
    id="example_battery_1",
    name="Example Battery 1",
    driver="generic",
    capacity_kwh=5.0,
    min_soc_pct=10.0,
    max_soc_pct=100.0,
    max_charge_w=1000.0,
    max_discharge_w=1000.0,
    eta_charge=0.95,
    eta_discharge=0.95,
    mapping={
        "power_setpoint_entity": "number.example_battery_power",
        "soc_entity": "sensor.example_battery_soc",
    },
    enabled=True,
)

#: Overrides on top of ``config.DEFAULTS`` for the selfcheck only -- never a second set of defaults
#: (the horizon, slot size and every other default stay exactly what the shipped settings say).
SELFCHECK_OPTIONS: dict[str, Any] = {
    "mode": Mode.ADVISORY.value,
    "dry_run": True,
    "log_level": "warning",
    "solar.enabled": False,
    "notify.enabled": False,
    "price.source": "static",
    "price.static_schedule": [
        {"from_hour": 0, "to_hour": 6, "price_eur_per_kwh": 0.15, "days": [0, 1, 2, 3, 4, 5, 6]},
        {"from_hour": 6, "to_hour": 18, "price_eur_per_kwh": 0.30, "days": [0, 1, 2, 3, 4, 5, 6]},
        {"from_hour": 18, "to_hour": 24, "price_eur_per_kwh": 0.45, "days": [0, 1, 2, 3, 4, 5, 6]},
    ],
    "optimizer.soc_step_kwh": 0.1,
    "optimizer.power_steps": 12,
    "control.grid_follow": True,
}


def default_settings() -> dict[str, Any]:
    """``config.DEFAULTS`` when WP2A is importable, else ``{}`` (the fake keeps its own defaults)."""
    config_module = _import("config")
    defaults = getattr(config_module, "DEFAULTS", None) if config_module is not None else None
    return dict(defaults) if isinstance(defaults, Mapping) else {}


def _selfcheck_settings() -> _SelfcheckSettings:
    values = {**default_settings(), **SELFCHECK_OPTIONS}
    values.pop("batteries", None)  # batteries live in the add_battery helper, not in the dict
    settings = _SelfcheckSettings(**values)
    settings.add_battery(EXAMPLE_BATTERY)
    return settings


def _selfcheck_sources() -> dict[str, Any]:
    """Deliberately fake, obviously synthetic forecasts (SPEC R1: no real figures)."""

    def price_source(now: datetime) -> PriceForecast:
        forecast = PriceForecast(source="selfcheck", generated_at=now)
        start = now.replace(minute=0, second=0, microsecond=0)
        for index in range(96):
            moment = start + timedelta(minutes=15 * index)
            cheap = 12 <= moment.hour < 16
            expensive = 18 <= moment.hour < 21
            buy = 0.10 if cheap else (0.40 if expensive else 0.25)
            forecast.slots.append(PriceSlot(t=moment, buy=buy, sell=0.05))
        return forecast

    def solar_source(now: datetime) -> SolarForecast:
        forecast = SolarForecast(source="selfcheck", generated_at=now)
        start = now.replace(minute=0, second=0, microsecond=0)
        for index in range(96):
            moment = start + timedelta(minutes=15 * index)
            pv = 2.0 if 10 <= moment.hour < 16 else 0.0
            forecast.slots.append(SolarSlot(t=moment, pv_kw=pv))
        return forecast

    def load_source(now: datetime) -> LoadForecast:
        forecast = LoadForecast(source="selfcheck", generated_at=now, confidence="low")
        start = now.replace(minute=0, second=0, microsecond=0)
        for index in range(96):
            moment = start + timedelta(minutes=15 * index)
            base = 0.4 if 0 <= moment.hour < 7 else 0.8
            forecast.slots.append(
                LoadSlot(t=moment, load_kw=base, p10_kw=base * 0.6, p90_kw=base * 1.5)
            )
        return forecast

    def sampler(now: datetime) -> dict[str, Any]:
        return {"grid_w": 250.0, "pv_w": 0.0, "load_w": 400.0}

    return {
        "price_source": price_source,
        "solar_source": solar_source,
        "load_source": load_source,
        "sampler": sampler,
        "registry_source": lambda: [
            {
                "entity_id": "number.example_battery_power",
                "platform": "generic_example",
                "name": "Example battery power",
                "unit_of_measurement": "W",
            },
            {
                "entity_id": "sensor.example_price_now",
                "platform": "example_price",
                "name": "Example price now",
                "device_class": "monetary",
                "unit_of_measurement": "EUR/kWh",
            },
        ],
        "states_source": lambda: {},
    }


def _selfcheck_backends(data_dir: str) -> tuple[Any, Any]:
    """Real ``Settings`` + ``Store`` when WP2A is importable, else the in-memory fakes."""
    store = build_store(data_dir)
    settings = build_settings(store, {}, os.path.join(data_dir, "options.json"))
    if store is not None and settings is not None:
        log.warning("selfcheck: using the real settings/store in %s", data_dir)
        return settings, store
    log.warning("selfcheck: app.config/app.store unavailable, using in-memory fakes")
    return _selfcheck_settings(), _FakeStore()


def selfcheck(port: int = 0) -> int:
    """Boot the real engine + API against fakes; return 0 when everything answers."""
    _configure_logging("warning")
    checks: list[tuple[str, bool, str]] = []
    data_dir = tempfile.mkdtemp(prefix="sem-selfcheck-")

    def check(name: str, condition: bool, detail: str = "") -> None:
        checks.append((name, bool(condition), detail))

    settings, store = _selfcheck_backends(data_dir)
    adder = getattr(settings, "add_battery", None)
    if store is not None and not isinstance(store, _FakeStore) and callable(adder):
        adder(EXAMPLE_BATTERY)
    settings.set("control.grid_follow", False)
    settings.set("notify.enabled", False)
    if not isinstance(settings, _SelfcheckSettings):
        for key, value in SELFCHECK_OPTIONS.items():
            try:
                settings.set(key, value)
            except Exception as exc:
                log.warning("selfcheck: cannot set %s: %s", key, exc)
    ha = _FakeHA()
    engine = Engine(
        settings,
        store=store,
        ha=ha,
        notifier=Notifier(ha, settings),
        version=APP_VERSION,
        commit=COMMIT,
        **_selfcheck_sources(),
    )
    api = ApiServer(engine, host="127.0.0.1", port=port or 0, version=APP_VERSION)
    try:
        # SPEC 9.1: the engine reads a battery *before* it plans, so the first plan is anchored on
        # the measured SoC (55 % -> 2.75 kWh) and never on the assumed aggregate floor (10 %).
        plan = engine.plan_now("selfcheck")
        floor_kwh = EXAMPLE_BATTERY.capacity_kwh * EXAMPLE_BATTERY.min_soc_pct / 100.0
        check(
            "plan_created",
            plan is not None and bool(plan.slots) and plan.slots[0].soc_kwh > floor_kwh + 1e-6,
            "no plan, or a plan anchored on the assumed floor SoC",
        )
        if plan is not None:
            second = engine.plan_now("selfcheck")
            check(
                "plan_deterministic",
                second is not None
                and [s.power_w for s in plan.slots] == [s.power_w for s in second.slots],
                "two runs differ",
            )
        tick = engine.tick_now(plan.generated_at if plan is not None else utcnow())
        check("control_tick", bool(tick.battery_states), "no battery state read")
        check("advisory_no_writes", not ha.service_calls, f"{len(ha.service_calls)} service calls")
        published = engine.publish_states()
        check("states_published", published >= 8, f"published {published} entities")
        check("journal_written", bool(engine.log(limit=5)), "empty journal")

        engine.set_mode(Mode.AUTO.value, True)
        engine.tick_now()
        check("auto_dry_run_no_writes", not ha.service_calls, f"{len(ha.service_calls)} service calls")

        engine.set_mode(Mode.AUTO.value, False)
        engine.tick_now()
        writes = [call for call in ha.service_calls if call[0] in ("number", "input_number")]
        check(
            "auto_writes_setpoint",
            bool(writes) and abs(float(writes[-1][2].get("value") or 0.0)) > 0.0,
            "no non-zero setpoint write",
        )
        engine.stop_now()
        check("stop_now_advisory", engine.snapshot().mode == Mode.ADVISORY.value, "mode not advisory")

        api.start()
        checks.extend(_selfcheck_http(api, engine))
    finally:
        api.stop()
        engine.stop()
        close = _callable(store, "close")
        if close is not None:
            close()
        shutil.rmtree(data_dir, ignore_errors=True)

    failed = [name for name, ok, _ in checks if not ok]
    for name, ok, detail in checks:
        print(f"  [{'ok' if ok else 'FAIL'}] {name}" + (f" ({detail})" if detail and not ok else ""))
    print(f"selfcheck: {len(checks) - len(failed)}/{len(checks)} checks passed")
    return 1 if failed else 0


def _selfcheck_http(api: ApiServer, engine: Any) -> list[tuple[str, bool, str]]:
    import json
    import urllib.error
    import urllib.request

    results: list[tuple[str, bool, str]] = []
    base = f"http://127.0.0.1:{api.address[1]}"

    def request(method: str, path: str, payload: Any = None) -> tuple[int, Any]:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                body = response.read()
                try:
                    parsed = json.loads(body)
                except ValueError:
                    parsed = {"bytes": len(body)}  # CSV/HTML payloads: only prove it arrived
                return response.status, parsed
        except urllib.error.HTTPError as exc:
            return exc.code, None

    for path in (
        "/api/v1/health",
        "/api/v1/status",
        "/api/v1/plan",
        "/api/v1/forecast?kind=price",
        "/api/v1/batteries",
        "/api/v1/config",
        "/api/v1/discover?kind=battery",
        "/api/v1/discover?kind=grid",
        "/api/v1/savings?range=day",
        "/api/v1/accuracy?days=14",
        "/api/v1/log?limit=10&kind=dispatch",
        "/api/v1/providers",
        "/api/v1/export?what=plan&format=csv",
    ):
        try:
            status, body = request("GET", path)
            results.append((f"GET {path}", status == 200 and body is not None, f"status {status}"))
        except Exception as exc:  # pragma: no cover - harness only
            results.append((f"GET {path}", False, str(exc)[:120]))

    # SPEC 10 details the UI depends on, asserted against the running server.
    try:
        status, config = request("GET", "/api/v1/config")
        keys = {row["key"] for row in (config or {}).get("settings", [])}
        defaults = default_settings()
        results.append(
            (
                "config catalogue == DEFAULTS",
                bool(defaults) and keys == set(defaults),
                f"{len(keys)} keys advertised",
            )
        )
        groups = {row["group"] for row in (config or {}).get("settings", []) if row["key"].startswith("control.")}
        results.append(("safety group present", groups == {"Safety"}, str(sorted(groups))))

        status, plan = request("GET", "/api/v1/plan")
        slots = (plan or {}).get("slots") or []
        results.append(
            (
                "plan slots carry soc_pct/pv_source/price_source",
                bool(slots)
                and all(slot.get("price_source") for slot in slots)
                and all(slot.get("soc_pct") is not None for slot in slots),
                f"{len(slots)} slots",
            )
        )

        status, savings = request("GET", "/api/v1/savings?range=day")
        totals = (savings or {}).get("totals") or {}
        results.append(
            (
                "savings totals split arbitrage/self-consumption",
                "arbitrage_kwh" in totals and "self_consumption_kwh" in totals,
                str(sorted(totals)),
            )
        )

        status, batteries = request("GET", "/api/v1/batteries")
        record = ((batteries or {}).get("batteries") or [{}])[0]
        results.append(
            (
                "batteries carry capabilities + cycles",
                isinstance(record.get("capabilities"), dict)
                and "cycles" in record
                and "cycles" in (record.get("state", {}).get("extra") or {}),
                str(sorted(record)),
            )
        )

        status, _ = request("POST", "/api/v1/mode", {"dry_run": True})
        results.append(("POST /mode partial update", status == 200, f"status {status}"))
        # Leave a *real* setpoint running so Stop now has something to neutralise: the engine only
        # holds for real where it actually commanded power (audit findings 2/3).
        engine.set_mode(Mode.AUTO.value, False)
        engine.set_override(-400.0, 3.0)
        engine.tick_now()
        status, stopped = request("POST", "/api/v1/stop")
        entry = ((stopped or {}).get("stopped") or [{}])[0]
        results.append(
            (
                "POST /stop holds for real",
                status == 200
                and set(stopped or {}) == {"mode", "override_cleared", "stopped"}
                and entry.get("dry_run") is False,
                f"status {status}",
            )
        )
        status, export = request("GET", "/api/v1/export?what=actuals&format=json")
        results.append(("GET /export json", status == 200 and export is not None, f"status {status}"))
    except Exception as exc:  # pragma: no cover - harness only
        results.append(("SPEC 10 detail checks", False, str(exc)[:120]))

    try:
        urllib.request.urlopen(base + "/api/v1/nonexistent", timeout=10)
        results.append(("GET unknown route 404", False, "expected 404"))
    except urllib.error.HTTPError as exc:
        results.append(("GET unknown route 404", exc.code == 404, f"status {exc.code}"))
    except Exception as exc:  # pragma: no cover
        results.append(("GET unknown route 404", False, str(exc)[:120]))
    return results


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
