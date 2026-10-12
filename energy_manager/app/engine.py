"""Application core: threads, shared state, planning, override, kill switch, HA publisher.

Owned threads (SPEC 9.1 and 12):

* ``sem-control``  -- sample measurements, then one dispatcher tick (never skipped because a write
  failed);
* ``sem-replan``   -- refresh forecasts and solve the plan when it is stale or forced;
* ``sem-guard``    -- watchdog (fail-safe hold when the control loop stalls) and the throttled,
  idempotent HA state publisher (SPEC 12); both must never block the control loop, so they run
  here rather than in the tick.

All state lives behind one lock.  Home Assistant and the store are reached through injected
callables/objects so this module has no import-time dependency on the WP2A modules and can boot
against fakes (``--selfcheck``).
"""

from __future__ import annotations

import copy
import json
import logging
import threading
from dataclasses import asdict, fields, replace
from datetime import date, datetime, timedelta
from typing import Any, Callable, Mapping, Sequence

from . import analytics
from .control import drivers as driver_registry
from .control.dispatcher import Dispatcher, TickOutcome
from .control.drivers.base import IDEMPOTENT_TOLERANCE_W, HAFacade
from .models import (
    Action,
    AlertRecord,
    BatteryConfig,
    BatteryState,
    DispatchResult,
    Mode,
    Plan,
    PlanSlot,
    PriceForecast,
    ProviderStatus,
    Reason,
    SavingsBucket,
    SolarForecast,
    SystemState,
    clamp,
    from_iso,
    quarter_floor,
    slot_hours_from,
    to_iso,
    utcnow,
)
from .notify import Notifier
from .optimize import dp
from .optimize.allocate import allocate_power
from .optimize.dp import align_slots

log = logging.getLogger(__name__)

#: Persisted measurement rows keep the sampler's 5-minute resolution (SPEC 7.3, and the same value
#: as ``sources.measure.MEASUREMENT_MINUTES``); the optimiser's own grid stays at
#: ``optimizer.slot_minutes``.  A regression test pins the two together.
ACTUALS_MINUTES = 5

#: Published HA entities (SPEC section 12).  ``sensor.sem_*`` is the documented contract.
SEM_AUTOPILOT_STATE = "sensor.sem_autopilot_state"
SEM_ACTION = "sensor.sem_plan_action"
SEM_PRICE = "sensor.sem_price_now"
SEM_PV_TODAY = "sensor.sem_pv_forecast_today"
SEM_LOAD_NOW = "sensor.sem_load_forecast_now"
SEM_SAVINGS = "sensor.sem_savings_today"
SEM_SOC_AVG = "sensor.sem_battery_soc_avg"
SEM_ACCURACY = "sensor.sem_forecast_accuracy"
SEM_ACTIVE = "binary_sensor.sem_autopilot_active"
SEM_AT_FLOOR = "binary_sensor.sem_battery_at_floor"

PUBLISH_MIN_INTERVAL_S = 20.0
MIN_REPLAN_GAP_S = 60.0
#: Seconds between boot-recovery retries while a live setpoint is still outstanding.
RECOVERY_RETRY_S = 60.0
#: Replan attempts back off exponentially while they keep failing (audit finding 6).
MAX_REPLAN_BACKOFF_S = 900.0
#: Minimum seconds between two identical alerts (audit finding 6).
ALERT_DEDUPE_S = 900.0
SAVINGS_FLUSH_S = 300.0
DISCOVER_CACHE_S = 60.0
#: Requested vs applied within this many watts is an idempotent re-write, not a clamp: a real cap
#: (grid limit, SoC floor, battery ceiling) moves the setpoint by far more (SPEC 10 plan execution).
CLAMP_TOLERANCE_W = 1.0
#: Seconds a fetched device registry stays cached: the wizard's search box must not open a new
#: WebSocket command per keystroke (names change far slower than the user types).
DEVICE_CACHE_S = 5.0
MAX_DISCOVER_ROWS = 50

#: Discovery kinds accepted by ``GET /api/v1/discover`` (SPEC 10).
DISCOVER_KINDS = ("battery", "price", "solar", "load", "grid")

#: ``haws.discover_candidates`` grid role -> the settings key a confirmation is saved to (SPEC 3).
GRID_ROLE_SETTINGS: dict[str, str] = {
    "grid_power": "grid.power_entity",
    "grid_import": "grid.import_entity",
    "grid_export": "grid.export_entity",
}

#: Discovery kind -> role -> the settings key the setup picker writes (SPEC 10, additive). The grid
#: roles keep their own map because the sampler reads it; price/solar/load all feed one key each.
DISCOVER_ROLE_SETTINGS: dict[str, dict[str, str]] = {
    "grid": GRID_ROLE_SETTINGS,
    "price": {"price_now": "price.entity", "price_forecast": "price.entity"},
    "solar": {"pv_power": "solar.measured_entity", "pv_energy": "solar.measured_entity"},
    "load": {
        "load_power": "load.entity",
        "load_energy": "load.entity",
        "temperature": "load.temperature_entity",
    },
}


try:  # WP2A supplies the registry scorer; the engine still loads (with discovery disabled) without it
    from . import haws
except ImportError:  # pragma: no cover - standalone engine tests
    haws = None  # type: ignore[assignment]


class Journal:
    """Dispatch log + event log: in-memory rings, mirrored into the store when it can take them."""

    def __init__(self, store: Any = None, *, max_entries: int = 600, clock=utcnow) -> None:
        self._store = store
        self._max = max_entries
        self._lock = threading.Lock()
        self._dispatches: list[dict[str, Any]] = []
        self._events: list[dict[str, Any]] = []
        self._clock = clock

    def bind_store(self, store: Any) -> None:
        self._store = store

    def dispatch(self, entry: Mapping[str, Any]) -> None:
        ts = entry.get("ts") or self._clock()
        detail = {
            "battery_id": entry.get("battery_id"),
            "requested_w": float(entry.get("requested_w") or 0.0),
            "applied_w": float(entry.get("applied_w") or 0.0),
            "soc_pct": entry.get("soc_pct"),
            "mode": entry.get("mode"),
            "dry_run": bool(entry.get("dry_run")),
            "result": entry.get("result"),
            "reason": entry.get("reason"),
            "writes": list(entry.get("writes") or []),
        }
        record = {
            "ts": ts,
            "kind": "dispatch",
            "level": "error" if str(entry.get("result")) == "error" else "info",
            "code": str(entry.get("code") or ""),
            "message": str(entry.get("message") or ""),
            "detail": detail,
            **detail,
        }
        with self._lock:
            self._dispatches.append(record)
            del self._dispatches[: max(0, len(self._dispatches) - self._max)]
        # The dispatch_log schema has no reason column; store the decision slug as the code when
        # nothing else went wrong, so "why didn't it charge?" is answerable from the database too.
        store_code = record["code"]
        if store_code in (Reason.OK, Reason.DRY_RUN, Reason.IDEMPOTENT_SKIP) and detail["reason"]:
            store_code = str(detail["reason"])
        self._store_call(
            "add_dispatch",
            ts=ts,
            battery_id=detail["battery_id"],
            requested_w=detail["requested_w"],
            applied_w=detail["applied_w"],
            soc_pct=detail["soc_pct"],
            mode=str(detail["mode"] or ""),
            dry_run=detail["dry_run"],
            result=str(detail["result"] or ""),
            code=store_code,
            message=record["message"],
        )

    def event(
        self,
        level: str,
        code: str,
        message: str,
        payload: dict[str, Any] | None = None,
        *,
        kind: str = "event",
    ) -> dict[str, Any]:
        record = {
            "ts": self._clock(),
            "kind": kind if kind in ("event", "alert") else "event",
            "level": str(level),
            "code": str(code),
            "message": str(message),
            "detail": dict(payload or {}),
        }
        with self._lock:
            self._events.append(record)
            del self._events[: max(0, len(self._events) - self._max)]
        self._store_call(
            "add_event",
            ts=record["ts"],
            level=record["level"],
            code=record["code"],
            message=record["message"],
            payload=record["detail"],
        )
        return record

    def entries(
        self, limit: int = 100, level: str | None = None, kind: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            merged = list(self._events) + list(self._dispatches)
        if level:
            merged = [e for e in merged if e["level"] == level]
        if kind:
            merged = [e for e in merged if e["kind"] == kind]
        merged.sort(key=lambda e: e["ts"], reverse=True)
        return [self._serialise(entry) for entry in merged[: max(1, int(limit))]]

    def dispatch_log(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            records = list(self._dispatches)
        records.sort(key=lambda e: e["ts"], reverse=True)
        return [self._serialise(entry) for entry in records[: max(1, int(limit))]]

    def _store_call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if self._store is None:
            return None
        method = getattr(self._store, name, None)
        if method is None:
            return None
        try:
            return method(*args, **kwargs)
        except Exception as exc:  # a broken store must not take the control loop down
            log.warning("store.%s failed: %s", name, exc)
            return None

    @staticmethod
    def _serialise(entry: Mapping[str, Any]) -> dict[str, Any]:
        out = dict(entry)
        out["ts"] = to_iso(entry.get("ts")) if isinstance(entry.get("ts"), datetime) else entry.get("ts")
        return out


class Engine:
    def __init__(
        self,
        settings: Any,
        *,
        store: Any = None,
        ha: HAFacade | None = None,
        price_source: Callable[[datetime], PriceForecast] | None = None,
        solar_source: Callable[[datetime], SolarForecast] | None = None,
        load_source: Callable[[datetime], Any] | None = None,
        sampler: Callable[[datetime], Mapping[str, Any]] | None = None,
        grid_mapping: Any = None,
        registry_source: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
        states_source: Callable[[], Mapping[str, Mapping[str, Any]]] | None = None,
        device_client_factory: Callable[[], Any] | None = None,
        notifier: Notifier | None = None,
        maintenance: Callable[[datetime], Any] | None = None,
        location: str = "",
        clock=utcnow,
        version: str = "0.1.0",
        commit: str = "",
    ) -> None:
        self._settings = settings
        self._store = store
        self._ha = ha
        self._price_source = price_source
        self._solar_source = solar_source
        self._load_source = load_source
        self._sampler = sampler
        self._grid_mapping = grid_mapping
        self._registry_source = registry_source
        self._states_source = states_source
        self._device_client_factory = device_client_factory
        self._maintenance = maintenance
        self._clock = clock
        self._version = version
        self._commit = commit
        self._started_at = clock()

        self._lock = threading.RLock()
        self._journal = Journal(store)
        self._notifier = notifier or Notifier(ha, settings, clock=clock)
        self._dispatcher = Dispatcher(settings, self._journal, {}, clock=clock)
        # One command path at a time: the tick, stop, mode transitions and exit holds must never
        # interleave a setpoint write (audit finding 4).
        self._control_lock = threading.RLock()
        self._drivers: dict[str, Any] = {}
        self._driver_signature: tuple[Any, ...] | None = None

        self._plan: Plan | None = None
        self._plan_inputs: tuple[Any, ...] | None = None
        self._plan_applied_notified = False
        self._price: PriceForecast | None = None
        self._solar: SolarForecast | None = None
        self._load: Any = None
        self._provider_status: dict[tuple[str, str], ProviderStatus] = {}
        self._pv_factors: dict[str, Any] = {}
        self._accuracy: dict[str, list[analytics.AccuracySample]] = {"pv": [], "load": [], "price": []}
        self._slot_actuals: dict[str, dict[datetime, float]] = {"pv": {}, "load": {}, "price": {}}
        self._slot_forecasts: dict[str, dict[datetime, float]] = {"pv": {}, "load": {}, "price": {}}
        self._savings_today: dict[str, Any] = {}
        self._savings_flushed_at: datetime | None = None
        self._savings_cache: tuple[datetime, dict[str, Any], dict[str, Any]] | None = None
        self._actuals_ring: list[dict[str, Any]] = []
        self._last_sample_at: datetime | None = None
        self._last_sample_values: dict[str, Any] = {}
        self._published: dict[str, tuple[datetime, str]] = {}
        self._discover_cache: tuple[datetime, list[dict[str, Any]]] | None = None
        self._device_cache: tuple[datetime, dict[str, dict[str, Any]]] | None = None
        self._force_replan = False
        # Warming-up gate (SPEC 9.1): a plan anchored on an assumed floor SoC must never be
        # published or dispatched, so planning starts only once a battery has really been read.
        self._battery_read_ok = False
        self._pending_plan_alerted = False
        # Per-battery "we commanded a non-zero setpoint for real" latch (audit findings 2/3): a
        # neutralising hold is only allowed to touch hardware when this is set, and it survives a
        # restart so a run that died unclean is neutralised before control resumes.
        self._live_writes: set[str] = set()
        self._live_writes_loaded = False
        self._mode_persist_failed = False
        self._last_recovery_attempt: datetime | None = None
        self._replan_backoff_s = MIN_REPLAN_GAP_S
        self._last_replan_at: datetime | None = None
        self._last_maintenance: date | None = None  # wall-clock day of the last maintenance run
        self._floor_alerted: set[str] = set()
        self._alerted_codes: dict[str, datetime] = {}
        self._negative_price_hour: datetime | None = None

        # PV bias: sources.solar owns the learned profile (it applies it while fetching), so only
        # fall back to the in-engine EWMA when it is unavailable.  Learning is delegated in
        # ``_learn_pv_bias`` to avoid two divergent profiles.
        self._pv_bias_sink: Callable[..., Any] | None = None
        try:  # pragma: no cover - exercised by the integration tests
            from .sources import solar as solar_module

            self._pv_bias_sink = getattr(solar_module, "update_solar_bias", None)
        except ImportError:
            self._pv_bias_sink = None
        self._pv_bias_external = bool(self._pv_bias_sink is not None and store is not None)

        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._held_on_exit = False

        self._state = SystemState(
            version=version,
            commit=commit,
            mode=str(self._settings.get("mode", Mode.ADVISORY.value)),
            dry_run=bool(self._settings.get("dry_run", True)),
            ha_connected=ha is not None,
            location=location,
            now=clock(),
        )
        self._journal.event("info", "engine_start", f"Smart Energy Manager {version} started")

    # ------------------------------------------------------------------ public accessors

    @property
    def settings(self) -> Any:
        return self._settings

    @property
    def store(self) -> Any:
        return self._store

    @property
    def journal(self) -> Journal:
        return self._journal

    def snapshot(self) -> SystemState:
        with self._lock:
            state = copy.copy(self._state)
            state.batteries = [copy.copy(battery) for battery in self._state.batteries]
            state.alerts = list(self._state.alerts)
            state.provider_status = list(self._state.provider_status)
            state.now = self._clock()
            state.uptime_s = (state.now - self._started_at).total_seconds()
            state.scheduler_healthy = not self._dispatcher.stalled(state.now)
            return state

    def active_plan(self) -> Plan | None:
        with self._lock:
            return self._plan

    def forecast(self, kind: str) -> Any:
        with self._lock:
            return {"price": self._price, "solar": self._solar, "load": self._load}.get(kind)

    def providers(self) -> dict[str, list[ProviderStatus]]:
        with self._lock:
            statuses = list(self._state.provider_status)
        out: dict[str, list[ProviderStatus]] = {"solar": [], "price": [], "load": []}
        for status in sorted(statuses, key=lambda s: (s.kind, s.name)):
            out.setdefault(status.kind, []).append(status)
        return out

    # ------------------------------------------------------------------ forecast explanation

    def price_provider_contract(self) -> dict[str, str]:
        """Effective price provider + configured contract for the /forecast payload (SPEC 10).

        ``provider`` is the configured ``price.provider`` (or ``unavailable`` when no forecast was
        produced); ``contract`` is the configured ``price.contract``.
        """
        provider = str(self._settings.get("price.provider", "entity") or "entity")
        contract = str(self._settings.get("price.contract", "dynamic_hourly") or "dynamic_hourly")
        if self.forecast("price") is None:
            provider = "unavailable"
        return {"provider": provider, "contract": contract}

    def explain_forecast(self, kind: str) -> dict[str, Any]:
        """Why a forecast panel is empty or unavailable (SPEC 10, 0.3.0): ``{"reason", "hint"}``.

        ``reason`` is a frozen slug (``not_configured`` | ``provider_failed`` | ``fixed_price_missing``
        | ``no_pv_source`` | ``no_history`` | ``empty_horizon`` | ``unknown``), or ``None`` when the
        panel is healthy. ``hint`` is one sentence naming the setting or condition to fix, so the UI
        never has to say only "the source failed". Uses settings, the stored provider statuses and
        (for load) the store's learned profile. Reads current state; no network.
        """
        if kind == "price":
            return self._explain_price()
        if kind == "solar":
            return self._explain_solar()
        if kind == "load":
            return self._explain_load()
        return {"reason": "unknown", "hint": f"unknown forecast kind {kind!r}"}

    def _forecast_has_future(self, forecast: Any) -> bool:
        if forecast is None:
            return False
        now = self._clock()
        return any(
            getattr(slot, "t", None) is not None and slot.t + timedelta(minutes=15) > now
            for slot in getattr(forecast, "slots", []) or []
        )

    def _effective_price_source_name(self) -> str:
        provider = str(self._settings.get("price.provider", "entity") or "entity")
        if provider == "zonneplan":
            return "zonneplan_entity"
        if provider == "day_ahead":
            return "energy_charts"
        if provider == "fixed":
            return "fixed_tariff"
        return str(self._settings.get("price.source", "zonneplan_entity") or "zonneplan_entity")

    def _price_statuses(self) -> dict[str, ProviderStatus]:
        return {
            status.name: status
            for (status_kind, _), status in self._provider_status.items()
            if status_kind == "price"
        }

    def _missing_fixed_setting(self, contract: str) -> str | None:
        def absent(typed_key: str, entity_key: str) -> bool:
            return self._settings.get(typed_key) is None and not str(
                self._settings.get(entity_key, "") or ""
            ).strip()

        if contract == "fixed_single":
            if absent("price.fixed_price_eur_per_kwh", "price.fixed_price_entity"):
                return "price.fixed_price_eur_per_kwh or price.fixed_price_entity"
        elif contract == "fixed_day_night":
            for label, typed_key, entity_key in (
                ("day", "price.fixed_day_price_eur_per_kwh", "price.fixed_day_price_entity"),
                ("night", "price.fixed_night_price_eur_per_kwh", "price.fixed_night_price_entity"),
            ):
                if absent(typed_key, entity_key):
                    return f"{typed_key} or {entity_key} ({label} rate)"
        return None

    def _explain_price(self) -> dict[str, Any]:
        if self._forecast_has_future(self._price):
            return {"reason": None, "hint": None}
        if self._price_source is None:
            return {"reason": "not_configured", "hint": "price forecasting is disabled on this instance"}
        provider = str(self._settings.get("price.provider", "entity") or "entity")
        contract = str(self._settings.get("price.contract", "dynamic_hourly") or "dynamic_hourly")
        statuses = self._price_statuses()
        resolved = self._effective_price_source_name()

        if provider == "fixed":
            missing = self._missing_fixed_setting(contract)
            if missing:
                return {
                    "reason": "fixed_price_missing",
                    "hint": f"set {missing}; a fixed contract needs a consumer price and never falls back to the market",
                }
            return {"reason": "provider_failed", "hint": self._price_failure_hint(statuses, resolved)}
        if provider == "day_ahead":
            charts = statuses.get("energy_charts")
            if charts is not None and (
                charts.detail.get("http_status") == 404
                or "publishes no prices" in (charts.last_error or "")
            ):
                zone = str(self._settings.get("price.bzn", "NL") or "NL")
                return {
                    "reason": "provider_failed",
                    "hint": (
                        f"energy_charts returned no day-ahead prices for bidding zone {zone!r} (a valid "
                        "zone can have none for this window, including tomorrow before the auction); "
                        "pick a supplier as price.provider (e.g. zonneplan) or set price.entity to your "
                        "own tariff sensor"
                    ),
                }
        if provider == "zonneplan":
            zonne = statuses.get("zonneplan_entity")
            if zonne is not None and "no compatible electricity tariff sensor" in (zonne.last_error or ""):
                return {
                    "reason": "provider_failed",
                    "hint": "enable the Zonneplan integration in Home Assistant, or pick another price provider",
                }
        active = statuses.get(resolved)
        if active is not None and "no future price slots" in (active.last_error or ""):
            return {"reason": "empty_horizon", "hint": active.last_error or "no future price slots"}
        return {"reason": "provider_failed", "hint": self._price_failure_hint(statuses, resolved)}

    def _price_failure_hint(self, statuses: dict[str, ProviderStatus], resolved: str) -> str:
        active = statuses.get(resolved)
        if active is not None and active.last_error:
            return active.last_error
        failing = sorted(name for name, status in statuses.items() if status.last_error)
        if failing:
            return "; ".join(f"{name}: {statuses[name].last_error}" for name in failing)
        return "the price provider chain produced no usable forecast; check the price settings"

    def _explain_solar(self) -> dict[str, Any]:
        if self._forecast_has_future(self._solar):
            return {"reason": None, "hint": None}
        if self._solar_source is None or not bool(self._settings.get("solar.enabled", True)):
            return {
                "reason": "not_configured",
                "hint": "enable solar forecasting (solar.enabled) and add a PV plane (solar.planes)",
            }
        # A non-empty solar.sources list is the multi-source merge (SPEC §7.1.3): the hint names the
        # failing configured sources instead of the single solar.source path below.
        merge_sources = self._settings.get("solar.sources") or []
        if isinstance(merge_sources, (list, tuple)) and merge_sources:
            return self._explain_solar_merge()
        # The hint names the setting that is actually missing for the *configured* source: the
        # capacity estimate states a watt peak, Forecast.Solar states planes (SPEC 7.1.1).
        source = str(self._settings.get("solar.source", "forecast_solar") or "forecast_solar")
        if source == "capacity":
            if self._settings.get("solar.capacity_wp") is None:
                return {
                    "reason": "no_pv_source",
                    "hint": "set solar.capacity_wp (the array's total watt peak) for the capacity solar source",
                }
        elif not (self._settings.get("solar.planes") or []):
            hint = "add a PV plane under solar.planes"
            status = self._provider_status.get(("solar", "forecast_solar"))
            detail = getattr(status, "detail", None) or {}
            fallback = detail.get("derived_fallback") if isinstance(detail, Mapping) else None
            if fallback:
                hint += f" (the meter-derived estimate also failed: {fallback})"
            return {"reason": "no_pv_source", "hint": hint}
        statuses = {
            status.name: status
            for (status_kind, _), status in self._provider_status.items()
            if status_kind == "solar"
        }
        failing = sorted(name for name, status in statuses.items() if status.last_error)
        if any("location unavailable" in (statuses[name].last_error or "") for name in failing):
            return {
                "reason": "provider_failed",
                "hint": (
                    "location unavailable: set latitude_override / longitude_override or expose"
                    " /api/config - the estimate needs the latitude, longitude and timezone to place"
                    " the sun"
                ),
            }
        if failing:
            return {
                "reason": "provider_failed",
                "hint": "; ".join(f"{name}: {statuses[name].last_error}" for name in failing),
            }
        return {
            "reason": "no_pv_source",
            "hint": "solar is enabled but produced no forecast; check solar.source and the location",
        }

    def _explain_solar_merge(self) -> dict[str, Any]:
        """Why a merged solar panel is empty: name the configured sources that failed."""
        statuses = [
            status
            for (kind, _), status in self._provider_status.items()
            if kind == "solar" and status.state in ("ok", "degraded", "failed")
        ]
        failing = sorted(statuses, key=lambda s: s.name)
        failed = [status for status in failing if status.state == "failed"]
        if failed:
            hint = "; ".join(
                f"{status.name}: {status.last_error or status.note or 'no usable forecast'}"
                for status in failed
            )
            return {"reason": "provider_failed", "hint": hint}
        return {
            "reason": "no_pv_source",
            "hint": (
                "no configured solar.sources entry produced a usable series; check the sources and"
                " the location"
            ),
        }

    def _explain_load(self) -> dict[str, Any]:
        if self._forecast_has_future(self._load):
            return {"reason": None, "hint": None}
        if self._load_source is None:
            return {"reason": "not_configured", "hint": "load forecasting is disabled on this instance"}
        entity = str(self._settings.get("load.entity", "") or "")
        has_profile = False
        if self._store is not None and callable(getattr(self._store, "profile_get", None)):
            try:
                has_profile = self._store.profile_get("load", "profile") is not None
            except Exception:  # noqa: BLE001 - a store probe failure never breaks the explanation
                has_profile = False
        if not entity and not has_profile:
            return {
                "reason": "no_history",
                "hint": "map a load entity (load.entity) or wait for learned load history to accumulate",
            }
        statuses = {
            status.name: status
            for (status_kind, _), status in self._provider_status.items()
            if status_kind == "load"
        }
        failing = sorted(name for name, status in statuses.items() if status.last_error)
        if failing:
            return {
                "reason": "provider_failed",
                "hint": "; ".join(f"{name}: {statuses[name].last_error}" for name in failing),
            }
        return {
            "reason": "no_history",
            "hint": "no load history yet; set load.entity or wait for the profile to be learned",
        }

    # ------------------------------------------------------------------ control

    def start(self) -> None:
        if self._threads:
            return
        self._sync_drivers()
        # A previous run may have died with a real setpoint executing: neutralise it *before* the
        # control loop takes over (audit finding 2).
        try:
            self.recover_live_writes()
        except Exception as exc:  # pragma: no cover - defensive, startup must not fail
            log.warning("live setpoint recovery failed: %s", exc)
        self._stop.clear()
        for name, target in (
            ("sem-control", self._control_loop),
            ("sem-replan", self._replan_loop),
            ("sem-guard", self._guard_loop),
        ):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        self._journal.event("info", "engine_running", "control, replan and guard threads running")

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads = []
        self._journal.event("info", "engine_stopped", "threads stopped")

    def request_replan(self, reason: str = "api") -> dict[str, Any]:
        with self._lock:
            self._force_replan = True
        self._journal.event("info", "replan_requested", f"replan requested ({reason})")
        return {"queued": True, "reason": reason}

    def plan_now(self, reason: str = "forced") -> Plan | None:
        """Synchronous replan (selfcheck and tests; the API uses ``request_replan``)."""
        return self._replan(reason, self._clock())

    def tick_now(self, now: datetime | None = None) -> TickOutcome:
        """One control tick on the calling thread (selfcheck and tests)."""
        return self._control_tick(now)

    def set_mode(self, mode: str | None = None, dry_run: bool | None = None) -> dict[str, Any]:
        """Partial update: an omitted field keeps its current value (SPEC 10 POST /mode)."""
        if mode is None and dry_run is None:
            raise ValueError("mode or dry_run is required")
        if mode is not None and mode not in (Mode.OFF.value, Mode.ADVISORY.value, Mode.AUTO.value):
            raise ValueError(f"unknown mode: {mode!r}")
        previous = str(self.snapshot().mode)
        previous_dry_run = bool(self.snapshot().dry_run)
        if mode is not None:
            self._settings.set("mode", mode)
        if dry_run is not None:
            self._settings.set("dry_run", bool(dry_run))
        with self._lock:
            if mode is not None:
                self._state.mode = mode
            if dry_run is not None:
                self._state.dry_run = bool(dry_run)
        self._journal.event(
            "info",
            "mode_changed",
            f"mode {previous} -> {self._state.mode}, dry_run={self._state.dry_run}",
            {"from": previous, "to": self._state.mode, "dry_run": self._state.dry_run},
        )
        if self._state.mode == Mode.OFF.value and previous != Mode.OFF.value:
            # Nothing stale may stay on screen as the "current" plan once planning is switched off.
            self._drop_plan_for_off("mode_changed")
        self._handle_control_transition(previous, previous_dry_run, "mode_change")
        return {"mode": self._state.mode, "dry_run": self._state.dry_run}

    def _handle_control_transition(self, previous_mode: str, previous_dry_run: bool, reason: str) -> None:
        """Leaving live control (auto + real writes) must neutralise what we left running.

        Covers every route: POST /mode, a settings change (PATCH /config or a hand-edited file) and
        a dry_run switch while staying in auto (audit findings 2/3).
        """
        with self._lock:
            mode = str(self._state.mode)
            dry_run = bool(self._state.dry_run)
        was_live = previous_mode == Mode.AUTO.value and not previous_dry_run
        is_live = mode == Mode.AUTO.value and not dry_run
        if was_live and not is_live:
            with self._control_lock:
                self._leave_live_control(reason)

    def stop_now(self) -> dict[str, Any]:
        """The UI "Stop now" button: the atomic, deliberately-unblocked safety stop (SPEC 9.4).

        Authority is revoked *first* (advisory mode, persisted and published), then any in-flight
        tick is drained under the control lock, and only then is the hardware neutralised -- for
        real, but only where we actually left a setpoint running (an advisory/dry-run install that
        never commanded anything must not be written to, let alone have its Zendure smart mode
        switched to manual).  Never raises; a failing battery is reported in the response, and the
        reported ``mode`` is the mode actually in force (audit findings 2/3/4).
        """
        override_cleared = False
        stopped: list[dict[str, Any]] = []
        persisted = True
        try:
            with self._control_lock:
                with self._lock:
                    override_cleared = self._state.override is not None
                    self._state.override = None
                    self._state.mode = Mode.ADVISORY.value
                try:
                    self._settings.set("mode", Mode.ADVISORY.value)
                except Exception as exc:
                    persisted = False
                    log.warning("stop_now: could not persist advisory mode: %s", exc)
                self._journal.event(
                    "warning",
                    "stop_now",
                    "stop now: authority revoked"
                    + ("" if persisted else " (mode could not be persisted)"),
                    {"override_cleared": override_cleared, "mode_persisted": persisted},
                )
                if override_cleared:
                    self._journal.event("info", "override_cleared", "override cleared by stop now")
            with self._control_lock:
                stopped = self._hold_all(
                    reason="stop_now", dry_run=False, only=self._batteries_needing_hold()
                )
                self._clear_verified_holds(stopped)
            actual_mode = str(self._state.mode)
            self._journal.event(
                "warning",
                "stop_now_done",
                f"stop now: {len(stopped)} battery(ies) held, mode={actual_mode}",
                {
                    "override_cleared": override_cleared,
                    "batteries": len(stopped),
                    "mode_persisted": persisted,
                },
            )
        except Exception as exc:  # pragma: no cover - the stop button must always answer
            log.exception("stop_now failed")
            self._journal.event("error", "stop_now_failed", str(exc)[:200])
        with self._lock:
            actual_mode = str(self._state.mode)
        if not persisted:
            # The running process *is* advisory (and `_sync_mode` will not silently flip it back),
            # but after a restart the settings file would come up in the old mode: say so loudly.
            self._alert(
                "error",
                "stop_now_not_persisted",
                "stop now could not persist advisory mode; the setting still holds the old mode",
                {"mode": actual_mode},
            )
            with self._lock:
                self._mode_persist_failed = True
        return {
            # SPEC 10 /stop response: {mode, override_cleared, stopped} (the persistence failure is
            # reported through /status and the alert log, not as an extra key).
            "mode": actual_mode,
            "override_cleared": bool(override_cleared),
            "stopped": stopped,
        }

    def hold_on_exit(self, reason: str = "shutdown") -> list[dict[str, Any]]:
        """Write ``hold()`` for real before the process exits (appendix A: no device watchdog).

        The Zendure manager keeps re-applying its last manual setpoint, so a clean shutdown or a
        fatal error must neutralise the hardware instead of leaving it at the last commanded power.
        Runs at most once.
        """
        if self._held_on_exit:
            return []
        outstanding = self._live_write_ids() or self._load_live_writes()
        with self._lock:
            live_mode = self._state.mode == Mode.AUTO.value and not self._state.dry_run
        if not outstanding and not live_mode:
            # Nothing of *ours* is live and the engine was not writing for real: touching the
            # hardware here would clobber whatever the user set up themselves (audit finding 2).
            return []
        targets = sorted(outstanding) if outstanding else None
        held: list[dict[str, Any]] = []
        for attempt in (1, 2):  # retry once; the one-shot flag is only set on a verified write
            try:
                held = self._hold_all(
                    reason=reason, dry_run=False, only=targets, sync=False
                )
            except Exception as exc:  # pragma: no cover - best effort on the way out
                log.warning("hold_on_exit(%s) attempt %d failed: %s", reason, attempt, exc)
                held = []
            if held and all(entry["ok"] for entry in held):
                self._clear_verified_holds(held)
                self._held_on_exit = True
                log.warning("hold_on_exit(%s): %d battery(ies) neutralised", reason, len(held))
                break
            log.warning("hold_on_exit(%s) attempt %d incomplete: %s", reason, attempt, held)
        return held

    def _leave_live_control(
        self, reason: str, *, only: Sequence[str] | None = None, sync: bool = True
    ) -> list[dict[str, Any]]:
        """The one path that ends live control: hold for real *only* when we left a setpoint on.

        Used by every transition out of live control (mode change, dry_run turned on, stop,
        shutdown, fatal error, boot recovery), so a dry-run or advisory install never writes to the
        user's hardware and a real setpoint is never left executing (audit findings 2/3).
        """
        outstanding = self._live_write_ids()
        targets = sorted(outstanding if only is None else (set(only) & outstanding))
        if not targets:
            self._journal.event(
                "info",
                "hold_skipped",
                f"nothing to neutralise ({reason}): no live setpoint was written",
                {"reason": reason},
            )
            return []
        held = self._hold_all(reason=reason, dry_run=False, only=targets, sync=sync)
        self._clear_verified_holds(held)
        return held

    def _clear_verified_holds(self, held: Sequence[Mapping[str, Any]]) -> None:
        """A verified hold means nothing of ours is live on that battery any more."""
        for entry in held:
            battery_id = str(entry.get("battery_id") or "")
            if not battery_id or not entry.get("ok"):
                continue
            if abs(float(entry.get("applied_w") or 0.0)) <= self._hold_tolerance(battery_id):
                self._mark_live_write(battery_id, False)

    def _hold_all(
        self,
        reason: str,
        dry_run: bool | None = None,
        *,
        only: Sequence[str] | None = None,
        sync: bool = True,
    ) -> list[dict[str, Any]]:
        """Hold batteries; ``dry_run=None`` follows the current mode (SPEC 9.4/9.1).

        ``only`` restricts the hold to specific battery ids; ``sync=False`` never re-creates drivers
        (the exit path must not open a Home Assistant connection, audit finding 15).
        """
        if sync:
            self._sync_drivers()
        elif not self._drivers:
            self._ensure_local_drivers()
        with self._lock:
            dry = bool(self._state.dry_run) if dry_run is None else bool(dry_run)
            mode = str(self._state.mode)
        targets = sorted(self._drivers)
        if only is not None:
            wanted = set(only)
            targets = [battery_id for battery_id in targets if battery_id in wanted]
        held: list[dict[str, Any]] = []
        for battery_id in targets:
            driver = self._drivers[battery_id]
            try:
                result = driver.hold(dry_run=dry)
            except Exception as exc:  # pragma: no cover - defensive
                result = DispatchResult(
                    battery_id=battery_id,
                    ts=self._clock(),
                    requested_w=0.0,
                    applied_w=0.0,
                    ok=False,
                    dry_run=dry,
                    code=Reason.DRIVER_ERROR,
                    message=str(exc)[:160],
                )
            held.append(
                {
                    "battery_id": battery_id,
                    "ok": bool(result.ok),
                    "applied_w": result.applied_w,
                    "code": result.code,
                    "message": result.message or reason,
                    "dry_run": bool(result.dry_run),
                }
            )
            self._journal.dispatch(
                {
                    "ts": result.ts,
                    "battery_id": battery_id,
                    "requested_w": result.requested_w,
                    "applied_w": result.applied_w,
                    "soc_pct": None,
                    "mode": mode,
                    "dry_run": dry,
                    "result": "ok" if result.ok else "error",
                    "code": result.code,
                    "message": result.message or reason,
                    "reason": reason,
                    "writes": result.writes,
                }
            )
        return held

    def set_override(
        self, power_w: float, minutes: float, battery_id: str | None = None
    ) -> dict[str, Any]:
        power = float(power_w)
        bounded = clamp(float(minutes), 1.0, 240.0)
        now = self._clock()
        return self._store_override(
            power, now + timedelta(minutes=bounded), battery_id, minutes=bounded
        )

    def _store_override(
        self,
        power: float,
        until: datetime,
        battery_id: str | None,
        *,
        minutes: float | None = None,
        seconds: float | None = None,
    ) -> dict[str, Any]:
        if battery_id is not None:
            known = {config.id for config in self.battery_configs()}
            if battery_id not in known:
                raise KeyError(battery_id)
        now = self._clock()
        override = {
            "power_w": round(float(power), 1),
            "battery_id": battery_id,
            "set_at": to_iso(now),
            "expires_at": to_iso(until),
            "minutes": round(minutes, 2) if minutes is not None else round(
                max(0.0, (until - now).total_seconds()) / 60.0, 4
            ),
        }
        if seconds is not None:
            override["seconds"] = round(float(seconds), 2)
        with self._lock:
            self._state.override = override
        self._journal.event(
            "warning",
            "override_set",
            f"override {power:.0f} W for {override['minutes']:.2f} min"
            + (f" on {battery_id}" if battery_id else ""),
            {
                "power_w": override["power_w"],
                "minutes": override["minutes"],
                "battery_id": battery_id,
            },
        )
        return override

    def clear_override(self) -> dict[str, Any]:
        with self._lock:
            self._state.override = None
        self._journal.event("info", "override_cleared", "override cleared")
        return {}

    def test_discharge(
        self,
        battery_id: str,
        power_w: float,
        seconds: float,
        intent: str = "discharge",
    ) -> dict[str, Any]:
        """Bounded per-battery test (SPEC 10): refused unless ``auto`` and not ``dry_run``.

        ``intent`` is explicit: the UI's positive watt field means *discharge* here, while the
        internal convention stays signed (positive = charge), so a 10 s test can never turn into a
        60 s charge (audit finding 12).  The expiry is exact seconds -- no 1-minute floor -- and
        the response reports the power that was really applied by a real, safety-clamped dispatch.
        """
        state = self.snapshot()
        if str(state.mode) != Mode.AUTO.value or state.dry_run:
            raise PermissionError(
                "test discharge requires mode: auto and dry_run: false; "
                "the manager is currently non-live"
            )
        config = next((c for c in self.battery_configs() if c.id == battery_id), None)
        if config is None:
            raise KeyError(battery_id)
        want_charge = str(intent or "discharge").strip().lower() == "charge"
        magnitude = abs(float(power_w))
        bounded_power = clamp(
            magnitude if want_charge else -magnitude,
            -config.max_discharge_w,
            config.max_charge_w,
        )
        bounded_seconds = clamp(float(seconds or 5.0), 1.0, 60.0)
        moment = self._clock()
        override = self._store_override(
            bounded_power,
            moment + timedelta(seconds=bounded_seconds),
            battery_id,
            seconds=bounded_seconds,
        )
        self._journal.event(
            "warning",
            "test_discharge",
            f"bounded test: {bounded_power:.0f} W for {bounded_seconds:.0f} s on {battery_id}",
            {"power_w": bounded_power, "seconds": bounded_seconds},
        )
        response: dict[str, Any] = {
            "started": True,
            "battery_id": battery_id,
            "power_w": bounded_power,
            "intent": "charge" if want_charge else "discharge",
            "seconds": bounded_seconds,
            "until": override["expires_at"],
            "applied_w": 0.0,
            "code": Reason.IDEMPOTENT_SKIP,
            "dry_run": False,
        }
        try:
            outcome = self.tick_now(moment)
        except Exception as exc:  # pragma: no cover - defensive: the endpoint must answer
            log.warning("test_discharge tick failed: %s", exc)
            response["code"] = Reason.DRIVER_ERROR
            response["message"] = str(exc)[:160]
            return response
        for result in outcome.results:
            if result.battery_id == battery_id:
                response["applied_w"] = float(result.applied_w or 0.0)
                response["code"] = result.code
                response["ok"] = bool(result.ok)
                if result.message:
                    response["message"] = result.message
                break
        return response

    # ------------------------------------------------------------------ batteries

    def battery_configs(self) -> list[BatteryConfig]:
        getter = getattr(self._settings, "battery_list", None)
        if getter is None:
            return []
        try:
            return list(getter())
        except Exception as exc:
            log.warning("battery_list failed: %s", exc)
            return []

    def resolved_config(self, config: BatteryConfig) -> BatteryConfig:
        """The config a driver is built from: the stored mapping plus registry-resolved roles.

        Only the Zendure drivers resolve at runtime (SPEC 5: their roles are derived from the entity
        registry -- by platform + translation key on ``zendure_ha``, by property name on an MQTT
        bridge device).  The stored mapping always wins, and a config is never mutated --
        ``batteries_view`` still shows what the user confirmed.
        """
        resolver = driver_registry.resolve_mapping_for(config.driver)
        if resolver is None:
            return config
        mapping = dict(config.mapping)
        try:
            resolved = resolver(
                self._registry_entries(self._clock()),
                anchor_entity=mapping.get("soc_entity"),
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("resolve_mapping failed for %s: %s", config.id, exc)
            return config
        if not resolved:
            return config
        merged = {key: value for key, value in resolved.items() if not mapping.get(key)}
        if not merged:
            return config
        return replace(config, mapping={**mapping, **merged})

    def _sync_drivers(self) -> None:
        configs = self.battery_configs()
        effective: dict[str, BatteryConfig] = {}
        for config in sorted(configs, key=lambda c: c.id):
            effective[config.id] = self.resolved_config(config) if config.enabled else config
        signature = tuple(
            (
                config.id,
                config.driver,
                tuple(sorted(effective[config.id].mapping.items())),
                config.enabled,
                config.capacity_kwh,
                config.max_charge_w,
                config.max_discharge_w,
            )
            for config in sorted(configs, key=lambda c: c.id)
        )
        if signature == self._driver_signature:
            return
        drivers: dict[str, Any] = {}
        # A Zendure manager has ONE system-wide manual_power entity: two batteries sharing it would
        # fight over the same setpoint, so only the first one gets a driver (docs/EXTERNAL_APIS 6).
        claimed: dict[tuple[str, str], str] = {}
        if self._ha is not None:
            for config in sorted(configs, key=lambda c: c.id):
                if not config.enabled:
                    continue
                prepared = effective[config.id]
                conflict = ""
                if prepared.driver == driver_registry.ZendureDriver.driver_name:
                    for key in ("manual_power_entity", "operation_entity", "power_setpoint_entity"):
                        entity = str(prepared.mapping.get(key) or "")
                        if not entity:
                            continue
                        owner = claimed.get((key, entity))
                        if owner:
                            conflict = (
                                f"{key} {entity} is already controlled by battery {owner}; the "
                                "Zendure manager setpoint is system-wide, model it as one battery"
                            )
                            break
                        claimed[(key, entity)] = config.id
                if conflict:
                    self._journal.event(
                        "error", "driver_conflict", f"battery {config.id}: {conflict}",
                        {"battery_id": config.id},
                    )
                    continue
                try:
                    drivers[config.id] = driver_registry.create(
                        prepared, self._ha, clock=self._clock
                    )
                except ValueError as exc:
                    self._journal.event(
                        "error",
                        "driver_config_error",
                        f"battery {config.id}: {exc}",
                        {"battery_id": config.id},
                    )
        self._drivers = drivers
        self._driver_signature = signature
        self._dispatcher = Dispatcher(self._settings, self._journal, drivers, clock=self._clock)

    def batteries_view(self) -> list[dict[str, Any]]:
        self._sync_drivers()
        state = self.snapshot()
        live = {battery.id: battery for battery in state.batteries}
        configs = sorted(self.battery_configs(), key=lambda c: c.id)
        # The store keeps cycles per day; the lifetime sum is what the battery card shows.
        cycles = self.battery_cycles()
        discovery = (
            self.discover("battery", limit=10)
            if any(c.driver == driver_registry.ZendureDriver.driver_name for c in configs)
            else []
        )
        views: list[dict[str, Any]] = []
        for config in configs:
            driver = self._drivers.get(config.id)
            capabilities: dict[str, Any] = {}
            if driver is not None:
                try:
                    capabilities = driver.capabilities()
                except Exception as exc:  # pragma: no cover - defensive
                    capabilities = {"error": str(exc)[:160]}
            battery_state = _battery_state_dict(config.id, live.get(config.id))
            extra = dict(battery_state.get("extra") or {})
            extra["cycles"] = cycles
            battery_state["extra"] = extra
            views.append(
                {
                    "config": _battery_view(config),
                    "state": battery_state,
                    "capabilities": capabilities,
                    "mapping_resolved": bool(capabilities.get("mapping_resolved")),
                    "cycles": cycles,
                    "discovery": discovery,
                }
            )
        return views

    def battery_cycles(self) -> float:
        """Lifetime battery cycles from the stored savings rows (SPEC 10 /batteries)."""
        rows = self._store_call("savings_daily_range", "1970-01-01", self._clock().date().isoformat()) or []
        total = 0.0
        for row in rows:
            total += float(row.get("battery_cycles") or row.get("cycles") or 0.0)
        return round(total, 3)

    def create_battery(self, config: Mapping[str, Any]) -> dict[str, Any]:
        known = {field.name for field in fields(BatteryConfig)}
        data = {key: value for key, value in config.items() if key in known}
        if "mapping" in data and not isinstance(data["mapping"], dict):
            raise ValueError("mapping must be an object")
        if not data.get("id"):
            raise ValueError("id is required")
        data.setdefault("name", str(data["id"]))
        data.setdefault("driver", "generic")
        try:
            candidate = BatteryConfig(**data)
        except TypeError as exc:
            raise ValueError(f"invalid battery config: {exc}") from None
        # Validate what the driver will actually use: Zendure roles resolve from the registry at
        # runtime, so a config that only maps the SoC sensor is complete on a manager install.
        problems = driver_registry.validate_config(self.resolved_config(candidate))
        if problems:
            raise ValueError("; ".join(problems))
        adder = getattr(self._settings, "add_battery", None)
        if callable(adder):
            adder(candidate)
        else:  # a settings implementation without add_battery creates on update
            self._settings.update_battery(candidate.id, asdict(candidate))
        self._driver_signature = None
        self._sync_drivers()
        self._journal.event("info", "battery_created", f"battery {candidate.id} saved")
        return _battery_view(candidate)

    def update_battery(self, battery_id: str, patch: Mapping[str, Any]) -> dict[str, Any]:
        current = next((c for c in self.battery_configs() if c.id == battery_id), None)
        if current is None:
            raise KeyError(battery_id)
        known = {field.name for field in fields(BatteryConfig)}
        clean = {key: value for key, value in patch.items() if key in known and key != "id"}
        candidate = replace(current, **clean)
        problems = driver_registry.validate_config(self.resolved_config(candidate))
        if problems:
            raise ValueError("; ".join(problems))
        self._settings.update_battery(battery_id, clean)
        self._driver_signature = None
        self._sync_drivers()
        self._journal.event("info", "battery_updated", f"battery {battery_id} updated")
        return _battery_view(candidate)

    def delete_battery(self, battery_id: str) -> dict[str, Any]:
        current = next((c for c in self.battery_configs() if c.id == battery_id), None)
        if current is None:
            raise KeyError(battery_id)
        remover = getattr(self._settings, "remove_battery", None) or getattr(
            self._settings, "delete_battery", None
        )
        hard = callable(remover)
        if hard:
            remover(battery_id)
        else:
            # Settings has no delete in SPEC 3; disabling is the safe, non-destructive fallback.
            self._settings.update_battery(battery_id, {"enabled": False})
            self._journal.event(
                "warning",
                "battery_delete_unsupported",
                f"settings.delete_battery missing: battery {battery_id} disabled instead",
            )
        self._driver_signature = None
        self._sync_drivers()
        self._journal.event("info", "battery_deleted", f"battery {battery_id} removed")
        return {"deleted": battery_id, "hard": hard}

    def discover(self, kind: str, search: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Capped, filtered entity candidates for the mapping wizard (SPEC 10).

        ``app.haws.discover_candidates`` is the *single* registry-scoring helper (platform plus
        translation key, never a unique-id prefix); this method only translates its role slugs into
        the driver's mapping keys, adds the settings key a grid confirmation must be saved to, and
        names the device a battery row belongs to (``device_name``/``manufacturer``/``model``).
        """
        if kind not in DISCOVER_KINDS:
            raise ValueError(f"unknown discover kind: {kind!r}")
        rows = clamp(int(limit or MAX_DISCOVER_ROWS), 1, MAX_DISCOVER_ROWS)
        now = self._clock()
        entries = self._registry_entries(now)
        if not entries or haws is None:
            return []
        found = haws.discover_candidates(
            kind, entries, self._states_map(), search=search, limit=rows
        )
        # A battery is identified by its *device*: the picker groups the rows into devices and
        # shows what the user called them.  Only fetched when there is something to enrich.
        devices = self._device_map(now) if kind == "battery" and found else {}
        for row in found:
            role = str(row.get("suggested_role") or "")
            if kind == "battery":
                device = devices.get(str(row.get("device_id") or "")) or {}
                row["device_name"] = device.get("device_name")
                row["manufacturer"] = device.get("manufacturer")
                row["model"] = device.get("model")
                row["mapping_key"] = driver_registry.role_to_mapping(
                    "zendure", role
                ) or driver_registry.role_to_mapping("generic", role)
            elif kind in DISCOVER_ROLE_SETTINGS:
                # The setup picker saves a confirmed role into this settings key (/config edits).
                row["setting_key"] = DISCOVER_ROLE_SETTINGS[kind].get(role)
        return found

    def _states_map(self) -> dict[str, Any]:
        if self._states_source is None:
            return {}
        try:
            states = self._states_source() or {}
        except Exception as exc:
            log.debug("states_source failed: %s", exc)
            return {}
        if isinstance(states, Mapping):
            return {str(key): value for key, value in states.items()}
        return {str(item.get("entity_id")): item for item in states if item.get("entity_id")}

    def grid_mapping(self) -> dict[str, str]:
        """Effective grid roles from settings, else the Energy-dashboard fallback (SPEC 3).

        The keys are always present (an unmapped role is ``""``) because the sampler's role map
        and the API read it as a complete picture. The fallback only fills roles the user left
        empty, so an explicit confirmation always wins.
        """
        roles = {
            role: str(self._settings.get(key, "") or "")
            for role, key in GRID_ROLE_SETTINGS.items()
        }
        if self._grid_mapping is not None:
            for role, entity in self._grid_mapping.power_roles().items():
                if not roles.get(role):
                    roles[role] = entity
        return roles

    def grid_resolution(self) -> dict[str, Any]:
        """Grid provenance for the settings panel (SPEC 3).

        ``explicit`` names the ``grid.*`` keys the user has set, ``dashboard`` every role the
        Energy dashboard offers (its power roles plus the energy counters, which have no setting
        key of their own), and ``source`` which of the two the effective mapping comes from.
        """
        explicit = {
            key: entity
            for key, entity in (
                (key, str(self._settings.get(key, "") or ""))
                for key in GRID_ROLE_SETTINGS.values()
            )
            if entity
        }
        dashboard: dict[str, str] = {}
        if self._grid_mapping is not None:
            dashboard = self._grid_mapping.dashboard_roles()
        if explicit:
            source = "settings"
        elif any(dashboard.get(role) for role in GRID_ROLE_SETTINGS):
            source = "energy_dashboard_power"
        elif dashboard:
            source = "energy_dashboard_energy"
        else:
            source = ""
        note = self._grid_mapping.note() if self._grid_mapping is not None else ""
        return {"source": source, "note": note, "explicit": explicit, "dashboard": dashboard}

    def _registry_entries(self, now: datetime) -> list[dict[str, Any]]:
        if self._registry_source is None:
            return []
        cache = self._discover_cache
        if cache is not None and (now - cache[0]).total_seconds() < DISCOVER_CACHE_S:
            return cache[1]
        try:
            entries = [dict(entry) for entry in self._registry_source() or []]
        except Exception as exc:
            self._journal.event("warning", "registry_unavailable", str(exc)[:200])
            entries = []
        self._discover_cache = (now, entries)
        return entries

    def _device_map(self, now: datetime) -> dict[str, dict[str, Any]]:
        """``device_id -> {device_name, manufacturer, model}`` for the mapping wizard.

        Short TTL so typing in the search box cannot hammer Home Assistant; an unreachable or
        malformed registry simply yields an empty map and the device fields stay null.
        """
        cache = self._device_cache
        if cache is not None and (now - cache[0]).total_seconds() < DEVICE_CACHE_S:
            return cache[1]
        devices: dict[str, dict[str, Any]] = {}
        for row in self._device_registry_rows():
            device_id = str(row.get("id") or "")
            if not device_id:
                continue
            # A user rename wins: the picker must show what the user calls the battery.
            devices[device_id] = {
                "device_name": str(row.get("name_by_user") or row.get("name") or "").strip() or None,
                "manufacturer": str(row.get("manufacturer") or "").strip() or None,
                "model": str(row.get("model") or "").strip() or None,
            }
        self._device_cache = (now, devices)
        return devices

    def _device_registry_rows(self) -> list[Mapping[str, Any]]:
        """``config/device_registry/list`` rows, ``[]`` whenever they cannot be fetched.

        The client is the same WebSocket wrapper the entity-registry source uses, built per call by
        the injected factory (``haws.HAWebSocket`` by default) and closed again.  Every failure path
        -- no client, a client without the command, a transport error, malformed rows -- degrades to
        ``[]``: discovery must not break over the device names.
        """
        factory = self._device_client_factory or (None if haws is None else haws.HAWebSocket)
        if factory is None:
            return []
        client = None
        try:
            client = factory()
            fetch = getattr(client, "list_device_registry", None)
            rows = fetch() if callable(fetch) else []
        except Exception as exc:
            log.debug("device registry unavailable: %s", exc)
            return []
        finally:
            if callable(getattr(client, "close", None)):
                try:
                    client.close()
                except Exception:  # pragma: no cover - closing is best effort
                    log.debug("closing the device-registry client failed", exc_info=True)
        return [row for row in rows or [] if isinstance(row, Mapping)]

    # ------------------------------------------------------------------ settings

    def apply_grid_mapping(self, mapping: Mapping[str, Any]) -> dict[str, Any]:
        """Persist confirmed grid-role entities into the ``grid.*`` settings keys (SPEC 3).

        The wizard confirms a discovery row (``grid_power``/``grid_import``/``grid_export``); the
        sampler and the derived load tier read the same keys back through :meth:`grid_mapping`.
        """
        patch: dict[str, Any] = {}
        for role, entity in mapping.items():
            key = GRID_ROLE_SETTINGS.get(str(role)) or (str(role) if str(role) in GRID_ROLE_SETTINGS.values() else None)
            if key is None:
                raise ValueError(f"unknown grid role: {role!r}")
            text = "" if entity is None else str(entity).strip()
            if text and "." not in text:
                raise ValueError(f"{key}: {text!r} is not an entity id")
            patch[key] = text
        return self.apply_settings(patch)

    def apply_settings(self, patch: Mapping[str, Any]) -> dict[str, Any]:
        applied: list[str] = []
        errors: list[dict[str, str]] = []
        replan_keys = ("optimizer.", "price.", "solar.", "load.", "grid.")
        wants_replan = False
        for key in sorted(patch):
            try:
                self._settings.set(key, patch[key])
            except Exception as exc:
                errors.append({"key": key, "message": str(exc)[:200]})
                continue
            applied.append(key)
            wants_replan = wants_replan or str(key).startswith(replan_keys)
            if key in ("mode", "dry_run"):
                with self._lock:
                    previous_mode = str(self._state.mode)
                    previous_dry_run = bool(self._state.dry_run)
                    self._state.mode = str(self._settings.get("mode", self._state.mode))
                    self._state.dry_run = bool(self._settings.get("dry_run", self._state.dry_run))
                self._handle_control_transition(previous_mode, previous_dry_run, "settings")
        if applied:
            self._journal.event("info", "settings_changed", f"settings updated: {', '.join(applied)}")
        if wants_replan:
            self.request_replan("settings")
        return {"applied": applied, "errors": errors}

    # ------------------------------------------------------------------ planning

    def _control_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._control_tick()
            except Exception as exc:  # pragma: no cover - the loop must survive anything
                log.exception("control tick failed")
                self._journal.event("error", "control_tick_failed", str(exc)[:200])
            self._stop.wait(self._dispatcher.tick_interval_s())

    def _control_tick(self, now: datetime | None = None) -> TickOutcome:
        """Serialise every command path: a tick must never interleave with stop/exit holds."""
        with self._control_lock:
            return self._control_tick_unlocked(now)

    def _control_tick_unlocked(self, now: datetime | None = None) -> TickOutcome:
        moment = now or self._clock()
        self._sync_drivers()
        self._sync_mode()
        measurements = self._sample(moment)
        batteries = self.battery_configs()
        live: list[BatteryState] | None = None
        if not self._battery_read_ok:
            # SPEC 9.1: read first, plan second.  A fresh install must act on the measured SoC in
            # its very first tick, never on the floor an unread battery would be planned as.
            live = self._dispatcher.read_batteries(moment, batteries)
            with self._lock:
                self._state.batteries = list(live)
            self._note_ha_read(live)
            if self._accept_battery_read(live):
                # plan immediately: this tick's dispatch already acts on the measured SoC
                self._replan("first_battery_read", moment)
        plan = self.active_plan()
        with self._lock:
            tick_state = copy.copy(self._state)
            tick_state.batteries = list(self._state.batteries)
        outcome = self._dispatcher.tick(
            moment,
            state=tick_state,
            plan=plan,
            batteries=batteries,
            measurements=measurements,
            battery_states=live,
        )
        with self._lock:
            self._state.now = moment
            self._state.last_tick_at = moment
            self._state.batteries = list(outcome.battery_states)
            self._clear_expired_override(moment)
        self._note_ha_read(outcome.battery_states)
        self._apply_measurements(measurements, moment)
        self._record_accuracy(moment, measurements, plan)
        if outcome.stale_input:
            self._alert_once(
                Reason.STALE_INPUT,
                f"Plan not executed: {outcome.stale_input}",
                {"reason": outcome.stale_input},
            )
        self._note_live_writes(outcome)
        if outcome.results and any(result.writes for result in outcome.results):
            self._maybe_notify_applied()
        return outcome

    def _note_live_writes(self, outcome: TickOutcome) -> None:
        """Track whether a real non-zero setpoint is executing (audit findings 2/3).

        Only real, verified commands count: a dry-run tick never touches hardware, and a fresh read
        that shows the device at rest *and* the commanded value at rest clears the latch.
        """
        for result in outcome.results:
            if result.dry_run or not result.ok:
                continue
            tolerance = self._hold_tolerance(result.battery_id)
            if abs(float(result.requested_w or 0.0)) > tolerance:
                self._mark_live_write(result.battery_id, True)
            else:
                self._mark_live_write(result.battery_id, False)
        for battery in outcome.battery_states:
            if battery.power_w is None or not battery.available:
                continue
            tolerance = self._hold_tolerance(battery.id)
            if abs(float(battery.power_w)) > tolerance:
                continue
            commanded = self._commanded_power(battery.id)
            if commanded is None or abs(float(commanded)) <= tolerance:
                self._mark_live_write(battery.id, False)

    def _commanded_power(self, battery_id: str) -> float | None:
        """What the device was last *told* to do, when the driver can tell us (else None)."""
        driver = self._drivers.get(battery_id)
        getter = getattr(driver, "commanded_power", None)
        if not callable(getter):
            return None
        try:
            return getter()
        except Exception:  # pragma: no cover - a read failure must not break the tick
            return None

    def _ensure_local_drivers(self) -> None:
        """Build drivers from the stored mapping only -- never a registry/WebSocket lookup.

        The exit path must neutralise a battery in the Supervisor's SIGTERM grace window, so it may
        not spend it opening connections (audit finding 15).
        """
        for config in self.battery_configs():
            if not config.enabled or config.id in self._drivers:
                continue
            try:
                self._drivers[config.id] = driver_registry.create(config, self._ha)
            except Exception as exc:  # pragma: no cover - defensive, exit path must not raise
                log.warning("exit hold: cannot build a driver for %s: %s", config.id, exc)

    def _batteries_needing_hold(self) -> list[str]:
        """Latch members plus any battery with a non-zero *commanded* setpoint right now."""
        self._sync_drivers()
        needed = self._live_write_ids()
        for battery_id in sorted(self._drivers):
            commanded = self._commanded_power(battery_id)
            if commanded is not None and abs(float(commanded)) > self._hold_tolerance(battery_id):
                needed.add(battery_id)
        return sorted(needed)

    def _sync_mode(self) -> None:
        with self._lock:
            persist_failed = self._mode_persist_failed
            mode = str(self._settings.get("mode", self._state.mode))
            dry_run = bool(self._settings.get("dry_run", self._state.dry_run))
            previous = self._state.mode
            previous_dry_run = bool(self._state.dry_run)
            if mode in (Mode.OFF.value, Mode.ADVISORY.value, Mode.AUTO.value) and not (
                persist_failed and mode == Mode.AUTO.value and previous != Mode.AUTO.value
            ):
                self._state.mode = mode
            self._state.dry_run = dry_run
        if mode == Mode.OFF.value and previous != Mode.OFF.value:
            self._drop_plan_for_off("mode_setting")
        if (mode, dry_run) != (previous, previous_dry_run):
            self._handle_control_transition(previous, previous_dry_run, "settings")

    def mode_persisted(self) -> bool:
        """False when the last mode change could not be written to the settings file (SPEC 10)."""
        with self._lock:
            return not self._mode_persist_failed

    def pending_holds(self) -> list[str]:
        """Battery ids whose last real command is still non-zero (SPEC 10 /status)."""
        with self._lock:
            return sorted(self._live_writes)

    def _hold_tolerance(self, battery_id: str) -> float:
        driver = self._drivers.get(battery_id)
        return float(getattr(driver, "tolerance_w", IDEMPOTENT_TOLERANCE_W) or 0.0)

    def _mark_live_write(self, battery_id: str, live: bool) -> None:
        with self._lock:
            if live:
                self._live_writes.add(battery_id)
            else:
                self._live_writes.discard(battery_id)
        self._persist_live_writes()

    def _persist_live_writes(self) -> None:
        with self._lock:
            snapshot = sorted(self._live_writes)
        self._store_call(
            "profile_put",
            "control",
            "live_writes",
            {"batteries": snapshot, "updated_at": to_iso(self._clock())},
        )

    def _load_live_writes(self) -> set[str]:
        """Restore the latch; a run that died unclean neutralises before control resumes."""
        if self._live_writes_loaded:
            return self._live_write_ids()
        self._live_writes_loaded = True
        stored = self._store_call("profile_get", "control", "live_writes")
        payload = stored[1] if isinstance(stored, tuple) and len(stored) == 2 else stored
        restored: set[str] = set()
        if isinstance(payload, Mapping):
            entries = payload.get("batteries")
            if isinstance(entries, (list, tuple, set)):
                restored = {str(entry) for entry in entries}
        if restored:
            with self._lock:
                self._live_writes |= restored
            self._journal.event(
                "warning",
                "live_setpoint_recovered",
                f"previous run left {len(restored)} live setpoint(s); holding before control resumes",
                {"batteries": sorted(restored)},
            )
        return self._live_write_ids()

    def recover_live_writes(self) -> list[dict[str, Any]]:
        """Neutralise setpoints a previous run left behind, before taking control (audit 2).

        Safe to call repeatedly: the guard loop retries until the latch is empty (audit 3).
        """
        recovered = self._load_live_writes()
        if not recovered:
            return []
        held = self._leave_live_control("boot_recovery", only=sorted(recovered))
        self._alert(
            "warning",
            "live_setpoint_recovered",
            f"held {len(recovered)} battery(ies) left live by a previous run",
            {"batteries": sorted(recovered)},
        )
        return held

    def _live_write_ids(self) -> set[str]:
        with self._lock:
            return set(self._live_writes)

    def _drop_plan_for_stale_inputs(self, reason: str) -> None:
        """Withhold the active plan when its inputs went stale (SPEC 9.1, audit finding 2)."""
        with self._lock:
            had_plan = self._plan is not None
            self._plan = None
            self._state.active_plan = None
        if had_plan:
            self._journal.event(
                "warning", "plan_dropped", f"plan dropped: {reason}", {"reason": reason}
            )
        self._alert_once(Reason.STALE_INPUT, f"Plan withheld: {reason}", {"reason": reason})

    def _drop_plan_for_off(self, reason: str) -> None:
        """Drop the current plan when planning is switched off (SPEC 1: no planning in `off`)."""
        with self._lock:
            had_plan = self._plan is not None
            self._plan = None
            self._state.active_plan = None
            self._force_replan = False
        if had_plan:
            self._journal.event(
                "info", "plan_cleared", f"plan dropped: {reason}", {"reason": reason}
            )

    def _clear_expired_override(self, now: datetime) -> None:
        if self._state.override is None:
            return
        expires = from_iso(self._state.override.get("expires_at"))
        if expires is not None and expires <= now:
            self._state.override = None
            self._journal.event("info", "override_expired", "override expired automatically")

    def _sample(self, now: datetime) -> dict[str, Any]:
        if self._sampler is None:
            return {}
        try:
            values = dict(self._sampler(now) or {})
        except Exception as exc:
            self._journal.event("warning", "sampling_failed", str(exc)[:200])
            self._mark_ha(False, str(exc))
            return {}
        if values:
            self._mark_ha(True)
        return values

    def _apply_measurements(self, values: Mapping[str, Any], now: datetime) -> None:
        with self._lock:
            if values.get("grid_w") is not None:
                self._state.grid_now_w = float(values["grid_w"])
            if values.get("pv_w") is not None:
                self._state.pv_now_w = float(values["pv_w"])
            if values.get("load_w") is not None:
                self._state.load_now_w = float(values["load_w"])
            price = self._price_now(now)
            if price is not None:
                self._state.price_now = price
        self._savings_tick(values, now)
        self._append_actuals_row(values, now)

    def _append_actuals_row(self, values: Mapping[str, Any], now: datetime) -> None:
        plan = self.active_plan()
        slot = plan.slot_at(now) if plan is not None else None
        battery_kw = None
        with self._lock:
            # 0.13.0: only a confirmed measurement may be persisted as battery flow, so a dry-run,
            # blocked or unconfirmed setpoint never becomes a measured actual.
            powers = [
                b.power_w
                for b in self._state.batteries
                if b.power_w is not None and b.power_measured and b.available and not b.stale
            ]
            energies = [b.energy_kwh for b in self._state.batteries if b.energy_kwh is not None]
        if powers:
            battery_kw = sum(float(p) for p in powers) / 1000.0
        row = {
            "ts": to_iso(quarter_floor(now, ACTUALS_MINUTES)),
            "pv_kw": _kw(values.get("pv_w")),
            "load_kw": _kw(values.get("load_w")),
            "grid_kw": _kw(values.get("grid_w")),
            "battery_kw": battery_kw,
            "soc_kwh": sum(float(e) for e in energies) if energies else None,
            "price_buy": slot.buy if slot is not None else self._price_now(now),
            "price_sell": slot.sell if slot is not None else None,
            "plan_power_w": slot.power_w if slot is not None else None,
            "dispatch_power_w": battery_kw * 1000.0 if battery_kw is not None else None,
        }
        self._actuals_ring.append(row)
        del self._actuals_ring[: max(0, len(self._actuals_ring) - 2000)]
        self._store_call(
            "upsert_actuals",
            row["ts"],
            pv_kw=row["pv_kw"],
            load_kw=row["load_kw"],
            grid_kw=row["grid_kw"],
            battery_kw=row["battery_kw"],
            soc_kwh=row["soc_kwh"],
            price_buy=row["price_buy"],
            price_sell=row["price_sell"],
            plan_power_w=row["plan_power_w"],
            dispatch_power_w=row["dispatch_power_w"],
        )

    def _savings_tick(self, values: Mapping[str, Any], now: datetime) -> None:
        previous_at = self._last_sample_at
        self._last_sample_at = now
        if previous_at is None:
            return
        hours = clamp((now - previous_at).total_seconds() / 3600.0, 0.0, 1.0)
        if hours <= 0:
            return
        plan = self.active_plan()
        slot = plan.slot_at(now) if plan is not None else None
        configs = [config for config in self.battery_configs() if config.enabled]
        capacity = sum(config.capacity_kwh for config in configs)
        if not configs:
            # Nothing configured can move: a zero flow is measured-truthful here, not unknown.
            battery_kw: float | None = 0.0
        else:
            with self._lock:
                measured = {
                    b.id: float(b.power_w)
                    for b in self._state.batteries
                    if b.power_w is not None and b.power_measured and b.available and not b.stale
                }
            # Every enabled battery must be confirmed: a partial aggregate would earn credit for
            # energy an unmeasured battery may never have moved (0.13.0).
            battery_kw = (
                sum(measured.values()) / 1000.0
                if set(measured) >= {config.id for config in configs}
                else None
            )
        with self._lock:
            carried_day = str(self._savings_today.get("day") or "")
        if carried_day and carried_day != now.date().isoformat():
            # Crossing midnight with an unflushed row: write it under its *own* day before the new
            # day starts, instead of relabelling yesterday's totals as today's.
            self._flush_savings()
            with self._lock:
                self._savings_today = {}
        self._savings_today = analytics.daily_row_delta(
            self._savings_today,
            day=now.date().isoformat(),
            dt_hours=hours,
            grid_kw=_kw(values.get("grid_w")),
            load_kw=_kw(values.get("load_w")),
            pv_kw=_kw(values.get("pv_w")),
            battery_kw=battery_kw,
            price_buy=slot.buy if slot is not None else self._price_now(now),
            price_sell=slot.sell if slot is not None else None,
            capacity_kwh=capacity,
            now=now,
        )
        if self._savings_flushed_at is None or (
            now - self._savings_flushed_at
        ).total_seconds() >= SAVINGS_FLUSH_S:
            self._flush_savings()

    def _flush_savings(self) -> None:
        """Persist the in-memory day row under the day it was accumulated for."""
        with self._lock:
            row = dict(self._savings_today)
            if not row.get("day"):
                return
            self._savings_flushed_at = self._clock()
        self._store_call(
            "upsert_savings_daily",
            str(row.get("day")),
            import_kwh=float(row.get("import_kwh") or 0.0),
            export_kwh=float(row.get("export_kwh") or 0.0),
            cost_eur=float(row.get("cost_eur") or 0.0),
            revenue_eur=float(row.get("revenue_eur") or 0.0),
            # None means "no load forecast, baseline unknown": never persist a fabricated 0
            # (audit finding 13).
            baseline_cost_eur=None if row.get("baseline_cost_eur") is None else float(row["baseline_cost_eur"]),
            savings_eur=None if row.get("savings_eur") is None else float(row["savings_eur"]),
            pv_kwh=float(row.get("pv_kwh") or 0.0),
            # None means "no confirmed battery measurement today": unknown, never a planned value.
            battery_cycles=None if row.get("battery_cycles") is None else float(row["battery_cycles"]),
            charged_kwh=self._savings_optional("charged_kwh"),
            discharged_kwh=self._savings_optional("discharged_kwh"),
            charge_cost_eur=self._savings_optional("charge_cost_eur"),
            discharge_value_eur=self._savings_optional("discharge_value_eur"),
            grid_measured=self._savings_cover("grid_measured"),
            load_measured=self._savings_cover("load_measured"),
            pv_measured=self._savings_cover("pv_measured"),
            battery_measured=self._savings_cover("battery_measured"),
        )

    def _savings_optional(self, key: str) -> float | None:
        value = self._savings_today.get(key)
        return None if value is None else float(value)

    def _savings_cover(self, key: str) -> int:
        """Per-day coverage flag: 1 only when every interval measured that series."""
        return int(self._savings_today.get(key) or 0)

    def _replan_loop(self) -> None:
        while not self._stop.is_set():
            try:
                reason = self._replan_reason()
                if reason:
                    self._replan(reason, self._clock())
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("replan failed")
                self._journal.event("error", "replan_failed", str(exc)[:200])
            self._stop.wait(1.0)

    def _planning_ready(self, configs: Sequence[BatteryConfig] | None = None) -> bool:
        """True when a plan has a real SoC to anchor on (SPEC 9.1).

        An install with no enabled battery has nothing to read, so the documented do-nothing
        schedule is authoritative there; anything else waits for a live energy reading.
        """
        if not any(config.enabled for config in (configs if configs is not None else self.battery_configs())):
            return True
        with self._lock:
            return any(state.energy_kwh is not None for state in self._state.batteries)

    def _note_ha_read(self, states: Sequence[BatteryState]) -> None:
        """Report Home Assistant connectivity from a battery read (audit finding 10).

        A transport failure recorded by a driver means HA (not the battery) is unreachable; a
        readable state means the link works.  Without this the engine only noticed a dead HA when
        the household sampler happened to raise, and /status kept saying ``ha_connected: true``.
        """
        errors = [
            str(error)
            for battery_id in sorted(self._drivers)
            for error in [getattr(self._drivers[battery_id], "last_read_error", None)]
            if error
        ]
        if errors:
            # Only ever *demote*: a successful battery read is reported by the sampler's own
            # success path, so a failed sample is never masked by a healthy battery read.
            self._mark_ha(False, errors[0])

    def _accept_battery_read(self, states: Sequence[BatteryState]) -> bool:
        """Latch the first successful battery reading (SPEC 9.1 warming-up gate)."""
        if self._battery_read_ok:
            return True
        if not any(state.available and state.energy_kwh is not None for state in states):
            return False
        self._battery_read_ok = True
        self._journal.event(
            "info",
            "battery_reading_ok",
            "first battery reading received; planning from the measured SoC",
            {"batteries": [state.id for state in states if state.available]},
        )
        return True

    def _ensure_battery_reading(self, moment: datetime) -> bool:
        """Read a battery before planning, once per process (SPEC 9.1).

        Returns False while no battery reading is available; the caller must then withhold the
        plan (a schedule anchored on an assumed floor SoC is meaningless).  An install with no
        enabled battery has nothing to read, so its documented do-nothing plan is authoritative.
        """
        configs = self.battery_configs()
        if not any(config.enabled for config in configs):
            return True
        if self._battery_read_ok:
            return True
        self._sync_drivers()
        states = self._dispatcher.read_batteries(moment, configs)
        with self._lock:
            self._state.batteries = list(states)
        self._note_ha_read(states)
        return self._accept_battery_read(states)

    def _journal_pending_plan(self, reason: str) -> None:
        """Log/alert once that planning waits for the first battery reading (UI hint)."""
        if self._pending_plan_alerted:
            return
        self._pending_plan_alerted = True
        detail = {"reason": reason}
        self._journal.event(
            "warning",
            "battery_reading_pending",
            "plan not published: waiting for the first battery reading",
            detail,
        )
        self._alert(
            "warning",
            "battery_reading_pending",
            "Waiting for the first battery reading before planning.",
            detail,
        )

    def _replan_reason(self) -> str | None:
        # The rate limit comes first, for *every* reason: a failing plan (no forecast, no battery)
        # used to refetch every provider and journal a few events once per second (audit finding 6).
        now = self._clock()
        gap = self._replan_gap_s()
        if self._last_replan_at is not None and (now - self._last_replan_at).total_seconds() < gap:
            return None
        if not self._planning_ready():
            # Nothing to plan from yet: no plan is published, so the loop must not spin on it.
            with self._lock:
                forced = self._force_replan
                self._force_replan = False
            if forced:
                self._journal.event(
                    "info", "replan_skipped", "replan skipped: waiting for the first battery reading"
                )
            return None
        if str(self.snapshot().mode) == Mode.OFF.value:
            # SPEC 1: `off` is "no planning, no writes".  Sampling, publishing, alerts and the API
            # keep running; an explicitly queued recompute is dropped here (and journalled once).
            with self._lock:
                forced = self._force_replan
                self._force_replan = False
            if forced:
                self._journal.event("info", "replan_skipped", "replan skipped: mode is off")
            return None
        with self._lock:
            if self._force_replan:
                return "forced"
            plan = self._plan
        if plan is None:
            return "no_plan"
        age = (now - plan.generated_at).total_seconds()
        if age > self._dispatcher.replan_interval_s():
            return "plan_age"
        if plan.slot_at(now) is None:
            return "plan_expired"
        if self._dispatcher.needs_replan(self.snapshot(), plan, self.battery_configs(), now) == "soc_deviation":
            return "soc_deviation"
        return None

    def _replan_gap_s(self) -> float:
        """Minimum seconds between replan attempts: doubles while attempts keep failing."""
        with self._lock:
            return float(self._replan_backoff_s)

    def _note_replan_outcome(self, plan: Plan | None) -> None:
        with self._lock:
            if plan is None:
                self._replan_backoff_s = min(
                    MAX_REPLAN_BACKOFF_S, max(MIN_REPLAN_GAP_S, self._replan_backoff_s * 2.0)
                )
            else:
                self._replan_backoff_s = MIN_REPLAN_GAP_S

    def _replan(self, reason: str, now: datetime) -> Plan | None:
        with self._lock:
            self._force_replan = False
        self._last_replan_at = now
        # ``/providers`` describes the configuration, so it stays meaningful while warming up.
        self._sync_provider_status()
        # SPEC 9.1 / WP5 finding 7: read a battery before planning.  A plan computed without one
        # is anchored on the assumed aggregate floor, so it is never published or dispatched.
        authoritative = self._ensure_battery_reading(now)
        self._refresh_forecasts(now)
        price, solar, load = self._price, self._solar, self._load
        if price is None or load is None:
            self._journal.event(
                "warning",
                "forecast_unavailable",
                "no usable price/load forecast; keeping the previous plan",
                {"reason": reason},
            )
            self._notifier.send(
                "forecast_unavailable",
                "Smart Energy Manager",
                "No usable price or load forecast: planning is paused until the sources recover.",
            )
            self._alert_once("forecast_unavailable", "price/load forecast unavailable")
            # The previous plan was built from inputs that are now unusable: keeping it would let
            # it keep steering the hardware (audit finding 2).
            self._drop_plan_for_stale_inputs("price/load forecast unavailable")
            self._note_replan_outcome(None)
            return None

        configs = self.battery_configs()
        start_kwh = self._live_energy_kwh(configs)
        mode = str(self.snapshot().mode)
        try:
            plan = dp.solve_from_forecasts(
                price,
                solar if solar is not None else SolarForecast(source="unavailable"),
                load,
                battery_configs=configs,
                start_kwh=start_kwh,
                settings=self._settings,
                now=now,
                mode=mode,
                dry_run=bool(self.snapshot().dry_run),
            )
        except ValueError as exc:
            self._journal.event("warning", "plan_infeasible", str(exc)[:200], {"reason": reason})
            self._note_replan_outcome(None)
            return None

        remaining = [
            slot
            for slot in plan.slots
            if slot.t + timedelta(minutes=plan.slot_minutes) > now
        ]
        if remaining and all(slot.price_stale for slot in remaining):
            self._journal.event(
                "warning",
                "plan_withheld",
                "plan dropped: every remaining slot has a stale price forecast",
            )
            self._drop_plan_for_stale_inputs("every remaining slot has a stale price forecast")
            self._note_replan_outcome(None)
            return plan

        if not authoritative:
            # Computed for the caller (the API and --selfcheck still see a plan), but withheld from
            # the UI and from dispatch with a one-off reason the UI can show (SPEC 9.1).
            self._journal_pending_plan(reason)
            return plan

        plan.batteries = self._plan_battery_snapshot(plan, configs, now)
        changed = self._plan_changed(self._plan, plan)
        inputs_changed = self._plan_inputs is not None and self._plan_inputs != self._fingerprint()
        with self._lock:
            self._plan = plan
            self._state.active_plan = plan
            self._state.last_plan_at = plan.generated_at
            self._state.next_cheap_window, self._state.next_expensive_window = analytics.price_windows(
                price.slots, now
            )
            self._plan_inputs = self._fingerprint()
            self._plan_applied_notified = False
        self._store_call("save_plan", plan)
        self._record_plan_forecasts(plan, price, solar, load)
        with self._lock:
            self._state.price_now = self._price_now(now)
        label = "inputs_changed" if (inputs_changed and reason == "plan_age") else reason
        self._journal.event(
            "info",
            "plan_updated",
            f"plan recomputed ({label}): {plan.summary.saved_eur:.2f} EUR vs baseline",
            {
                "reason": label,
                "feasible": plan.feasible,
                "infeasible_reason": plan.infeasible_reason,
                "cost_estimate_eur": plan.summary.cost_estimate_eur,
                "baseline_cost_eur": plan.summary.baseline_cost_eur,
            },
        )
        if not plan.feasible and plan.infeasible_reason:
            self._alert(
                "warning",
                plan.infeasible_reason,
                f"plan not feasible: {plan.infeasible_reason}",
            )
        if changed:
            self._notifier.send(
                "plan_changed",
                "Smart Energy Manager",
                f"New plan: expected saving {plan.summary.saved_eur:.2f} EUR over the horizon.",
            )
        self._note_replan_outcome(plan)
        return plan

    def _fingerprint(self) -> tuple[Any, ...]:
        price = self._price
        solar = self._solar
        load = self._load
        price_vector = (
            tuple((to_iso(slot.t), round(slot.buy, 4), round(slot.sell, 4)) for slot in price.slots[:96])
            if price is not None
            else ()
        )
        pv_total = round(sum(slot.pv_kw for slot in solar.slots), 3) if solar is not None else 0.0
        load_total = round(sum(slot.load_kw for slot in load.slots), 3) if load is not None else 0.0
        return price_vector, pv_total, load_total

    def _plan_changed(self, previous: Plan | None, current: Plan) -> bool:
        if previous is None:
            return True
        if len(previous.slots) != len(current.slots):
            return True
        for old, new in zip(previous.slots, current.slots):
            if old.action != new.action or abs(old.power_w - new.power_w) > 25.0:
                return True
        return False

    def _live_energy_kwh(self, configs: Sequence[BatteryConfig]) -> float:
        with self._lock:
            energies = [b.energy_kwh for b in self._state.batteries if b.energy_kwh is not None]
        if energies:
            return float(sum(energies))
        aggregate = dp.BatteryAggregate.from_configs(
            configs, float(self._settings.get("optimizer.reserve_soc_pct", 0.0) or 0.0)
        )
        return aggregate.e_min_kwh

    def _plan_battery_snapshot(
        self, plan: Plan, configs: Sequence[BatteryConfig], now: datetime
    ) -> list[dict[str, Any]]:
        slot = plan.slot_at(now)
        with self._lock:
            energies = {b.id: b.energy_kwh for b in self._state.batteries}
        shares = {
            config.id: power
            for config, power in allocate_power(
                slot.power_w if slot is not None else 0.0,
                configs,
                energies,
                slot_hours=max(1, plan.slot_minutes) / 60.0,
                reserve_soc_pct=float(self._settings.get("optimizer.reserve_soc_pct", 0.0) or 0.0),
            )
        }
        median_buy = dp.median_buy_eur_per_kwh(plan.slots)
        override = self._wear_override_eur_per_kwh()
        return [
            {
                "id": config.id,
                "name": config.name,
                "capacity_kwh": config.capacity_kwh,
                "max_charge_w": config.max_charge_w,
                "max_discharge_w": config.max_discharge_w,
                "share_w": shares.get(config.id, 0.0),
                # SPEC 5/8.1: each battery's own efficiency and wear (the advanced override when
                # set), with the break-even taken at the same median buy price the plan's salvage
                # and aggregate use.
                "round_trip_efficiency": config.round_trip_efficiency,
                "wear_eur_per_kwh": override if override > 0.0 else config.wear_eur_per_kwh,
                "break_even_sell_eur_per_kwh": config.break_even_sell_eur_per_kwh(
                    median_buy, override if override > 0.0 else config.wear_eur_per_kwh
                ),
            }
            for config in sorted(configs, key=lambda c: c.id)
        ]

    def _wear_override_eur_per_kwh(self) -> float:
        """The advanced ``optimizer.cycle_cost_eur_per_kwh`` override; 0 = derive per battery."""
        return float(self._settings.get("optimizer.cycle_cost_eur_per_kwh", 0.0) or 0.0)

    # ------------------------------------------------------------------ forecasts

    def _refresh_forecasts(self, now: datetime) -> None:
        if self._price_source is not None:
            self._fetch("price", "price", now, self._price_source)
        if self._solar_source is not None:
            solar = self._fetch("solar", "solar", now, self._solar_source)
            if (
                solar is not None
                and not self._pv_bias_external
                and bool(self._settings.get("solar.bias_correction", True))
            ):
                self._solar = analytics.apply_pv_bias(solar, self._pv_factors, now)
        if self._load_source is not None:
            self._fetch("load", "load", now, self._load_source)
        self._sync_provider_status()

    def _fetch(self, kind: str, attribute: str, now: datetime, source: Callable) -> Any:
        try:
            result = source(now)
        except Exception as exc:
            self._mark_provider(kind, str(getattr(source, "__name__", kind)), ok=False, error=str(exc))
            self._journal.event(
                "warning", "forecast_unavailable", f"{kind} forecast failed: {str(exc)[:160]}"
            )
            return None
        statuses = None
        forecast = result
        if isinstance(result, tuple) and len(result) == 2:
            # WP2A providers return ``(forecast, provider statuses)``; keep their detail. (SPEC 4)
            forecast, statuses = result
        if forecast is None:
            self._mark_provider(kind, kind, ok=False, error="provider returned no forecast")
            return None
        setattr(self, f"_{attribute}", forecast)
        self._mark_provider(
            kind,
            str(getattr(forecast, "source", kind)),
            ok=True,
            stale=bool(getattr(forecast, "stale", False)),
        )
        for status in _status_list(statuses):
            self._remember_provider(status)
        # Additive (0.12.0): a merged solar forecast carries one status per configured source on the
        # forecast object, so /api/v1/providers lists each of them (kind "solar").
        for source_status in _status_list(getattr(forecast, "sources", None)):
            self._remember_provider(source_status)
        return forecast

    def _remember_provider(self, status: ProviderStatus) -> None:
        key = (status.kind, status.name or status.kind)
        with self._lock:
            self._provider_status[key] = status

    def _mark_provider(
        self, kind: str, name: str, *, ok: bool, error: str | None = None, stale: bool = False
    ) -> None:
        key = (kind, name or kind)
        status = self._provider_status.get(key) or ProviderStatus(kind=kind, name=name or kind)
        status.ok = ok
        status.stale = stale
        status.last_success = self._clock() if ok else status.last_success
        status.last_error = None if ok else (error or "")[:200]
        status.active = True
        with self._lock:
            self._provider_status[key] = status

    def _sync_provider_status(self) -> None:
        statuses: list[ProviderStatus] = []
        for kind in ("solar", "price", "load"):
            if getattr(self, f"_{kind}_source") is None:
                statuses.append(ProviderStatus(kind=kind, name="not_configured", active=False, ok=False))
                continue
            statuses.extend(
                status for (status_kind, _), status in self._provider_status.items() if status_kind == kind
            )
        with self._lock:
            self._state.provider_status = sorted(statuses, key=lambda s: (s.kind, s.name))

    def _price_now(self, now: datetime) -> float | None:
        price = self._price
        if price is None:
            return None
        for slot in price.slots:
            if slot.t <= now < slot.t + timedelta(minutes=15):
                return float(slot.buy)
        return float(price.slots[0].buy) if price.slots else None

    def _price_rank(self, now: datetime) -> float | None:
        price = self._price
        if price is None or not price.slots:
            return None
        # Include the slot that is running right now, not only the strictly future ones.
        horizon = [
            slot.buy for slot in price.slots if slot.t + timedelta(minutes=15) > now
        ]
        current = self._price_now(now)
        if not horizon or current is None:
            return None
        return round(analytics.percentile_rank(horizon, current), 1)

    # ------------------------------------------------------------------ accuracy

    def _record_plan_forecasts(self, plan: Plan, price: Any, solar: Any, load: Any) -> None:
        try:
            slots = align_slots(
                price,
                solar if solar is not None else SolarForecast(source="unavailable"),
                load,
                plan.horizon_start,
                plan.horizon_end,
                plan.slot_minutes,
            )
        except ValueError as exc:
            log.debug("accuracy: cannot align plan forecasts: %s", exc)
            return
        with self._lock:
            for slot in slots:
                self._slot_forecasts["price"][slot.t] = slot.buy
                self._slot_forecasts["pv"][slot.t] = slot.pv_kw
                self._slot_forecasts["load"][slot.t] = slot.load_kw

    def _record_accuracy(
        self, now: datetime, values: Mapping[str, Any], plan: Plan | None
    ) -> None:
        if plan is None:
            return
        slot = quarter_floor(now, plan.slot_minutes)
        actuals = {
            "price": self._price_now(now),
            "pv": _kw(values.get("pv_w")),
            "load": _kw(values.get("load_w")),
        }
        with self._lock:
            for kind, actual in actuals.items():
                if actual is None:
                    continue
                if slot in self._slot_forecasts[kind]:
                    self._slot_actuals[kind][slot] = float(actual)
            cutoff = quarter_floor(now, plan.slot_minutes)
            for kind in ("pv", "load", "price"):
                for slot_ts in sorted(self._slot_actuals[kind]):
                    if slot_ts >= cutoff:
                        continue
                    actual = self._slot_actuals[kind].pop(slot_ts)
                    forecast = self._slot_forecasts[kind].pop(slot_ts, None)
                    if forecast is None:
                        continue
                    self._accuracy[kind].append(
                        analytics.AccuracySample(ts=slot_ts, forecast=float(forecast), actual=actual)
                    )
                    del self._accuracy[kind][: max(0, len(self._accuracy[kind]) - 2000)]
        self._learn_pv_bias()

    def _learn_pv_bias(self) -> None:
        samples = [s for s in self._accuracy["pv"] if s.forecast > 1e-6]
        if not samples:
            return
        if self._pv_bias_external:
            # sources.solar owns the learned profile and applies it while fetching.
            tuples = [(s.ts, s.forecast, s.actual) for s in samples]
            try:
                self._pv_bias_sink(self._store, tuples, now=self._clock())
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("solar bias update failed: %s", exc)
            return
        factor_key = ("pv_bias", "hourly")
        if factor_key not in self._provider_status and not self._pv_factors:
            stored = self._learning_get("pv_bias", "hourly")
            if stored:
                self._pv_factors = dict(stored)
                self._provider_status[factor_key] = ProviderStatus(
                    kind="solar", name="pv_bias", active=True, ok=True
                )
        updated = analytics.update_bias_factors(self._pv_factors, samples)
        if updated != self._pv_factors:
            self._pv_factors = updated
            self._store_call("profile_put", "pv_bias", "hourly", updated, self._clock())

    def accuracy(self, days: int = 14) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            samples = {kind: list(items) for kind, items in self._accuracy.items()}
        return {
            kind: analytics.accuracy_from_samples(kind, items, days, now)
            for kind, items in samples.items()
        }

    # ------------------------------------------------------------------ savings

    def savings(self, range_key: str) -> tuple[list[SavingsBucket], dict[str, Any]]:
        if range_key not in analytics.RANGES:
            raise ValueError(f"unknown range: {range_key!r}")
        now = self._clock()
        start, _ = analytics.range_bounds(range_key, now)
        rows = (
            self._store_call(
                "savings_daily_range",
                start.date().isoformat(),
                now.date().isoformat(),
            )
            or []
        )
        if not rows and self._savings_today:
            rows = [dict(self._savings_today)]
        capacity = sum(
            float(config.capacity_kwh or 0.0) for config in self.battery_configs() if config.enabled
        )
        return analytics.savings_from_daily(rows, range_key, now, capacity_kwh=capacity)

    def savings_totals(self, range_key: str) -> dict[str, Any]:
        """Cached totals for the HA publisher (cheap enough to call every publish)."""
        now = self._clock()
        cache = self._savings_cache
        if cache is not None and (now - cache[0]).total_seconds() < SAVINGS_FLUSH_S and cache[1] == range_key:
            return cache[2]
        _, totals = self.savings(range_key)
        self._savings_cache = (now, range_key, totals)
        return totals

    # ------------------------------------------------------------------ plan execution

    def plan_execution(self, now: datetime | None = None, hours: int | None = None) -> dict[str, Any]:
        """Per-slot execution evidence for the active plan (SPEC 10 route, additive).

        The plan says what was *intended*; this joins the same slot grid with the persisted
        5-minute actuals and the dispatch log, so a window can state what was measured and what
        was actually applied.  Read-only, and empty (not broken) without a plan or without a store:
        a slot nobody has reached yet carries ``actual: null`` rather than a fabricated zero.
        """
        moment = now or self._clock()
        plan = self.active_plan()
        floors = self._execution_floors()
        batteries = [
            {
                "id": config.id,
                "name": config.name,
                "min_soc_pct": float(config.min_soc_pct),
                "floor_soc_pct": max(float(config.min_soc_pct), floors["reserve_soc_pct"]),
                "capacity_kwh": float(config.capacity_kwh),
            }
            for config in sorted(self.battery_configs(), key=lambda item: item.id)
            if config.enabled
        ]
        if plan is None:
            return {
                "generated_at": None,
                "slot_minutes": None,
                "now": to_iso(moment),
                "floors": floors,
                "batteries": batteries,
                "progress": _execution_progress([], [], moment, 0.0),
                "slots": [],
            }
        minutes = float(plan.slot_minutes or 0)
        limit = moment + timedelta(hours=hours) if hours else None
        slots = [slot for slot in plan.slots if limit is None or slot.t < limit]
        measured = analytics.measured_per_slot(
            [slot.t for slot in slots],
            minutes,
            self._execution_actuals(plan, moment),
            bucket_minutes=float(ACTUALS_MINUTES),
            now=moment,
        )
        dispatches = self._execution_dispatches(plan)
        return {
            "generated_at": to_iso(plan.generated_at),
            "slot_minutes": int(minutes),
            "now": to_iso(moment),
            "floors": floors,
            "batteries": batteries,
            "progress": _execution_progress(slots, measured, moment, minutes),
            "slots": [
                {
                    "t": to_iso(slot.t),
                    "dispatch": _dispatch_in_window(dispatches, slot.t, minutes),
                    "actual": measured[index],
                }
                for index, slot in enumerate(slots)
            ],
        }

    def _execution_floors(self) -> dict[str, float]:
        """The SoC floor a reserve hold defends: the configured reserve above every battery minimum."""
        reserve = float(self._settings.get("optimizer.reserve_soc_pct", 0.0) or 0.0)
        minimums = [float(config.min_soc_pct) for config in self.battery_configs() if config.enabled]
        lowest = min(minimums) if minimums else 0.0
        return {
            "reserve_soc_pct": max(0.0, reserve),
            "min_soc_pct": max(0.0, lowest),
            "floor_soc_pct": max(0.0, reserve, lowest),
        }

    def _execution_actuals(self, plan: Plan, moment: datetime) -> list[dict[str, Any]]:
        """Measured 5-minute rows for the plan horizon, from the store with the ring as fallback."""
        rows = self._store_call(
            "actuals_range", plan.horizon_start, moment + timedelta(minutes=ACTUALS_MINUTES)
        )
        if rows:
            return list(rows)
        with self._lock:
            return [dict(row) for row in self._actuals_ring]

    def _execution_dispatches(self, plan: Plan) -> list[dict[str, Any]]:
        """Dispatch rows since the plan was generated: the persistent log, else the in-memory ring.

        The ring holds ``Journal(max_entries)`` rows -- a few hours of ticks -- while the store keeps
        the whole horizon, so the store is preferred whenever it is bound.
        """
        rows = self._store_call("dispatch_log", limit=2000, since=plan.horizon_start)
        if rows:
            return list(rows)
        return list(self._journal.dispatch_log(limit=600))

    # ------------------------------------------------------------------ log/export

    def log(self, limit: int = 100, level: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        return self._journal.entries(limit, level, kind)

    def export(self, what: str, fmt: str) -> dict[str, Any]:
        if fmt not in ("json", "csv"):
            raise ValueError(f"unknown format: {fmt!r}")
        if what == "plan":
            plan = self.active_plan()
            if plan is None:
                rows: list[dict[str, Any]] = []
            else:
                rows = [
                    {
                        "t": to_iso(slot.t),
                        "buy": slot.buy,
                        "sell": slot.sell,
                        "pv_kw": slot.pv_kw,
                        "load_kw": slot.load_kw,
                        "soc_kwh": slot.soc_kwh,
                        "power_w": slot.power_w,
                        "action": slot.action.value,
                        "reason": slot.reason,
                    }
                    for slot in plan.slots
                ]
        elif what in ("actuals", "savings"):
            if what == "actuals":
                now = self._clock()
                rows = (
                    self._store_call(
                        "actuals_range",
                        now - timedelta(days=400),
                        now,
                    )
                    or []
                )
                if not rows:
                    rows = list(self._actuals_ring)
            else:
                now = self._clock()
                rows = (
                    self._store_call(
                        "savings_daily_range", "1970-01-01", now.date().isoformat()
                    )
                    or []
                )
                if not rows and self._savings_today:
                    rows = [dict(self._savings_today)]
        else:
            raise ValueError(f"unknown export: {what!r}")
        stamp = self._clock().strftime("%Y%m%d-%H%M%S")
        return {
            "what": what,
            "rows": rows,
            "filename": f"energy-manager-{what}-{stamp}.{fmt}",
            "content_type": "application/json" if fmt == "json" else "text/csv",
        }

    def notify_test(self) -> bool:
        return self._notifier.test()

    # ------------------------------------------------------------------ guard thread

    def _guard_loop(self) -> None:
        while not self._stop.is_set():
            try:
                now = self._clock()
                if self._dispatcher.stalled(now):
                    with self._lock:
                        mode = str(self._state.mode)
                        dry_run = bool(self._state.dry_run)
                    if mode == Mode.AUTO.value and not dry_run:
                        self._dispatcher.watchdog(now)
                        self._alert("error", "watchdog_hold", "control loop stalled: holding")
                    else:
                        self._dispatcher.note_tick(now)
                self._retry_live_write_recovery(now)
                self._check_battery_floor(now)
                self._check_negative_price(now)
                self._maybe_maintenance(now)
                self.publish_states(now)
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("guard loop failed")
            self._stop.wait(2.0)

    def _retry_live_write_recovery(self, now: datetime) -> None:
        """Keep trying to neutralise a boot-time latch until it is empty (audit finding 3).

        ``Engine.start()`` attempts the recovery once; if HA was unreachable then, the latch stays
        and the batteries keep executing whatever the previous run commanded.  Retry from the guard
        loop and make a failure visible.
        """
        if not self.pending_holds():
            return
        last = self._last_recovery_attempt
        if last is not None and (now - last).total_seconds() < RECOVERY_RETRY_S:
            return
        self._last_recovery_attempt = now
        try:
            held = self.recover_live_writes()
        except Exception as exc:  # pragma: no cover - defensive
            self._alert(
                "error", "live_setpoint_recovery_failed", f"recovery failed: {str(exc)[:160]}"
            )
            return
        still_live = self.pending_holds()
        if still_live:
            self._alert(
                "error",
                "live_setpoint_recovery_failed",
                "could not neutralise a setpoint left live by a previous run",
                {"batteries": still_live},
            )

    def _check_battery_floor(self, now: datetime) -> None:
        with self._lock:
            state = self.snapshot()
        configs = {config.id: config for config in self.battery_configs()}
        for battery in state.batteries:
            config = configs.get(battery.id)
            if config is None or battery.soc_pct is None:
                continue
            floor = max(config.min_soc_pct, float(self._settings.get("optimizer.reserve_soc_pct", 0.0) or 0.0))
            if battery.soc_pct <= floor + 1.0:
                if battery.id not in self._floor_alerted:
                    self._floor_alerted.add(battery.id)
                    self._alert(
                        "info",
                        "battery_floor_reached",
                        f"{battery.id} reached its configured floor ({floor:.0f}%)",
                    )
                    self._notifier.send(
                        "battery_floor_reached",
                        "Smart Energy Manager",
                        f"Battery {battery.id} reached its configured minimum SoC; it will stop discharging.",
                    )
            else:
                self._floor_alerted.discard(battery.id)

    def _check_negative_price(self, now: datetime) -> None:
        price = self._price_now(now)
        if price is None or price >= 0:
            return
        hour = now.replace(minute=0, second=0, microsecond=0)
        if self._negative_price_hour == hour:
            return
        self._negative_price_hour = hour
        self._alert("info", "negative_price", f"negative price: {price:.4f} EUR/kWh")
        self._notifier.send(
            "negative_price",
            "Smart Energy Manager",
            f"Electricity price is negative ({price:.4f} EUR/kWh); charging may be paid for.",
        )

    def _maybe_maintenance(self, now: datetime) -> None:
        """Daily housekeeping hook: learn the load profile from history, then trim (SPEC 11)."""
        if self._maintenance is None:
            return
        # Wall-clock day boundary, not a per-uptime 86400 s timer: an add-on restarted every
        # morning must still learn and trim (audit finding 14).
        today = now.date()
        if self._last_maintenance is not None and self._last_maintenance >= today:
            return
        self._last_maintenance = today
        try:
            self._maintenance(now)
        except Exception as exc:
            self._journal.event("warning", "maintenance_failed", str(exc)[:200])

    def _mark_ha(self, connected: bool, error: str | None = None) -> None:
        with self._lock:
            self._state.ha_connected = bool(connected)
            if connected:
                self._state.ha_last_error = None
            elif error:
                self._state.ha_last_error = str(error)[:200]

    def _alert_once(
        self, code: str, message: str, detail: dict[str, Any] | None = None
    ) -> None:
        """Alert once per distinct condition: a flapping provider must not spam the log."""
        with self._lock:
            last = self._alerted_codes.get(code)
            now = self._clock()
            if last is not None and (now - last).total_seconds() < ALERT_DEDUPE_S:
                return
            self._alerted_codes[code] = now
        self._alert("warning", code, message, detail)

    def _alert(
        self, level: str, code: str, message: str, detail: dict[str, Any] | None = None
    ) -> None:
        record = AlertRecord(ts=self._clock(), level=level, code=code, message=message, detail=dict(detail or {}))
        with self._lock:
            self._state.alerts.append(record)
            del self._state.alerts[:-20]
        # Alerts are their own log kind so the UI can filter the banner history (SPEC 10 /log).
        self._journal.event(level, code, message, detail, kind="alert")

    def _maybe_notify_applied(self) -> None:
        with self._lock:
            if self._plan_applied_notified:
                return
            self._plan_applied_notified = True
            mode = str(self._state.mode)
            dry_run = bool(self._state.dry_run)
        if mode == Mode.AUTO.value and not dry_run:
            self._notifier.send(
                "plan_applied",
                "Smart Energy Manager",
                "The plan is now being executed on the batteries.",
            )

    # ------------------------------------------------------------------ HA publisher

    def publish_states(self, now: datetime | None = None) -> int:
        """Throttled, idempotent mirror of the engine state into HA (SPEC 12)."""
        moment = now or self._clock()
        if self._ha is None:
            return 0
        payloads = self._state_payloads(moment)
        published = 0
        for entity_id in sorted(payloads):
            state, attributes = payloads[entity_id]
            signature = f"{state}|{sorted(attributes.items()) if isinstance(attributes, dict) else attributes}"
            previous = self._published.get(entity_id)
            if previous is not None:
                previous_at, previous_signature = previous
                if (moment - previous_at).total_seconds() < PUBLISH_MIN_INTERVAL_S:
                    continue
                if previous_signature == signature:
                    continue
            try:
                self._ha.set_state(entity_id, state if isinstance(state, str) else str(state), attributes)
                self._published[entity_id] = (moment, signature)
                published += 1
                self._mark_ha(True)
            except Exception as exc:
                log.debug("publish %s failed: %s", entity_id, exc)
                self._mark_ha(False, f"state publish failed ({type(exc).__name__})")
                break
        return published

    def _state_payloads(self, now: datetime) -> dict[str, tuple[Any, dict[str, Any]]]:
        with self._lock:
            state = copy.copy(self._state)
            state.batteries = list(self._state.batteries)
        plan = self.active_plan()
        slot = plan.slot_at(now) if plan is not None else None
        autopilot = "off" if state.mode == Mode.OFF.value else (
            "auto" if (state.mode == Mode.AUTO.value and not state.dry_run) else
            ("dry_run" if state.mode == Mode.AUTO.value else "advisory")
        )
        soc_values = [b.soc_pct for b in state.batteries if b.soc_pct is not None]
        soc_avg = sum(soc_values) / len(soc_values) if soc_values else 0.0
        at_floor = False
        configs = {config.id: config for config in self.battery_configs()}
        for battery in state.batteries:
            config = configs.get(battery.id)
            if config is not None and battery.soc_pct is not None:
                if battery.soc_pct <= max(config.min_soc_pct, 0.0) + 1.0:
                    at_floor = True
        capacity_kwh = sum(
            float(config.capacity_kwh or 0.0) for config in configs.values() if config.enabled
        )
        soc_target_pct = None
        if plan is not None and plan.slots:
            capacity = sum(c.capacity_kwh for c in configs.values() if c.enabled)
            enabled = [c for c in configs.values() if c.enabled]
            if capacity > 0 and enabled:
                aggregate = dp.BatteryAggregate.from_configs(
                    enabled, float(self._settings.get("optimizer.reserve_soc_pct", 0.0) or 0.0)
                )
                last = plan.slots[-1]
                final_kwh = dp.next_energy_kwh(
                    last.soc_kwh, last.power_w / 1000.0, plan.slot_minutes / 60.0, aggregate
                )
                soc_target_pct = round(final_kwh / capacity * 100.0, 1)
        pv_today, pv_tomorrow, peak_w, peak_at = self._pv_summary(now)
        load_now_kw, load_today_kwh, confidence = self._load_summary(now)
        today = self.savings_totals("day")
        month = self.savings_totals("month")
        year = self.savings_totals("year")
        accuracy = self.accuracy(14)
        pv_mape = accuracy["pv"].mape_pct
        load_mape = accuracy["load"].mape_pct
        price_mae = accuracy["price"].mae
        usable_mapes = [m for m in (pv_mape, load_mape) if m is not None]
        accuracy_pct = max(0.0, 100.0 - (sum(usable_mapes) / len(usable_mapes))) if usable_mapes else 0.0
        cheap, expensive = state.next_cheap_window, state.next_expensive_window
        if cheap is None and expensive is None and self._price is not None:
            cheap, expensive = analytics.price_windows(self._price.slots, now)
        return {
            SEM_AUTOPILOT_STATE: (
                autopilot,
                {"friendly_name": "Smart Energy Manager autopilot"},
            ),
            SEM_ACTION: (
                slot.action.value if slot is not None else Action.HOLD.value,
                {
                    "friendly_name": "Smart Energy Manager action",
                    "reason": slot.reason if slot is not None else Reason.NO_SLOTS,
                    "target_power_w": round(slot.power_w, 0) if slot is not None else 0.0,
                    "soc_target_pct": soc_target_pct,
                },
            ),
            SEM_PRICE: (
                round(state.price_now, 4) if state.price_now is not None else "unknown",
                {
                    "friendly_name": "Smart Energy Manager price now",
                    "unit_of_measurement": "EUR/kWh",
                    "rank_pct": self._price_rank(now),
                    "cheap_window_start": to_iso(cheap[0]) if cheap else None,
                    "expensive_hour_start": to_iso(expensive[0]) if expensive else None,
                },
            ),
            SEM_PV_TODAY: (
                round(pv_today, 2),
                {
                    "friendly_name": "Smart Energy Manager PV forecast today",
                    "unit_of_measurement": "kWh",
                    "tomorrow_kwh": round(pv_tomorrow, 2),
                    "peak_w": round(peak_w, 0),
                    "peak_at": to_iso(peak_at),
                },
            ),
            SEM_LOAD_NOW: (
                round(load_now_kw, 2),
                {
                    "friendly_name": "Smart Energy Manager load forecast now",
                    "unit_of_measurement": "kW",
                    "today_kwh": round(load_today_kwh, 2),
                    "confidence": confidence,
                },
            ),
            SEM_SAVINGS: (
                round(float(today.get("savings_eur") or 0.0), 2),
                {
                    "friendly_name": "Smart Energy Manager savings today",
                    "unit_of_measurement": "EUR",
                    "month_eur": round(float(month.get("savings_eur") or 0.0), 2),
                    "year_eur": round(float(year.get("savings_eur") or 0.0), 2),
                    # Do-nothing grid energy: what the meter would have seen with the battery
                    # idle, estimated from the stored row (audit minor: `baseline_kwh` used to be
                    # a stored key that nothing ever wrote).
                    "baseline_kwh": round(
                        float(today.get("import_kwh") or 0.0)
                        - float(today.get("export_kwh") or 0.0)
                        + float(today.get("battery_cycles") or 0.0) * capacity_kwh,
                        2,
                    ),
                },
            ),
            SEM_SOC_AVG: (
                round(soc_avg, 1),
                {"friendly_name": "Smart Energy Manager average battery SoC", "unit_of_measurement": "%"},
            ),
            SEM_ACCURACY: (
                round(accuracy_pct, 1),
                {
                    "friendly_name": "Smart Energy Manager forecast accuracy",
                    "unit_of_measurement": "%",
                    "pv_mape": round(pv_mape, 1) if pv_mape is not None else None,
                    "load_mape": round(load_mape, 1) if load_mape is not None else None,
                    "price_mae": round(price_mae, 4) if price_mae is not None else None,
                },
            ),
            SEM_ACTIVE: (
                "on" if (state.mode == Mode.AUTO.value and not state.dry_run) else "off",
                {"friendly_name": "Smart Energy Manager autopilot active"},
            ),
            SEM_AT_FLOOR: ("on" if at_floor else "off", {"friendly_name": "Smart Energy Manager battery at floor"}),
        }

    def _pv_summary(self, now: datetime) -> tuple[float, float, float, datetime | None]:
        solar = self._solar
        if solar is None or not solar.slots:
            return 0.0, 0.0, 0.0, None
        today = now.date()
        tomorrow = today + timedelta(days=1)
        hours = slot_hours_from(solar.slots)
        today_kwh = sum(slot.pv_kw * hours for slot in solar.slots if slot.t.date() == today)
        tomorrow_kwh = sum(slot.pv_kw * hours for slot in solar.slots if slot.t.date() == tomorrow)
        peak = max(solar.slots, key=lambda slot: slot.pv_kw)
        return today_kwh, tomorrow_kwh, peak.pv_kw * 1000.0, peak.t

    def _load_summary(self, now: datetime) -> tuple[float, float, str]:
        load = self._load
        if load is None or not load.slots:
            # (load_now_kw, load_today_kwh, confidence) -- the caller always unpacks three values,
            # so a missing forecast must not blow up the state publisher (audit finding 5).
            return 0.0, 0.0, "low"
        slot = load.slots[0]
        for candidate in load.slots:
            if candidate.t <= now < candidate.t + timedelta(minutes=15):
                slot = candidate
                break
        today_kwh = sum(
            s.load_kw * slot_hours_from(load.slots) for s in load.slots if s.t.date() == now.date()
        )
        return float(slot.load_kw), today_kwh, str(getattr(load, "confidence", "low"))

    # ------------------------------------------------------------------ misc

    def _learning_get(self, kind: str, key: str) -> dict[str, Any] | None:
        stored = self._store_call("profile_get", kind, key)
        if isinstance(stored, tuple) and len(stored) == 2:  # (updated_at, payload)
            stored = stored[1]
        if isinstance(stored, Mapping):
            return dict(stored)
        if isinstance(stored, str) and stored:
            try:
                parsed = json.loads(stored)
            except ValueError:
                return None
            return dict(parsed) if isinstance(parsed, Mapping) else None
        return None

    def _store_call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if self._store is None:
            return None
        method = getattr(self._store, name, None)
        if method is None:
            return None
        try:
            return method(*args, **kwargs)
        except Exception as exc:
            log.warning("store.%s failed: %s", name, exc)
            return None


def _status_list(value: Any) -> list[ProviderStatus]:
    """Normalise a provider result into a list of statuses (a single one is allowed)."""
    if isinstance(value, ProviderStatus):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, ProviderStatus)]
    return []


def _battery_view(config: BatteryConfig) -> dict[str, Any]:
    """A battery config as the UI/API read it back, with the derived round-trip pct (SPEC 5).

    ``config.battery_payload`` is WP2A's; the import is lazy because this module must boot without
    WP2A (``--selfcheck`` fakes), and a missing module falls back to the stored fields alone.
    """
    try:
        from .config import battery_payload
    except ImportError:  # standalone engine without WP2A's config module
        return asdict(config)
    return battery_payload(config)


def _kw(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return None


def _battery_state_dict(config_id: str, battery: BatteryState | None) -> dict[str, Any]:
    if battery is None:
        return {"id": config_id, "available": False, "soc_pct": None, "power_w": None}
    return {
        "id": battery.id,
        "available": battery.available,
        "soc_pct": battery.soc_pct,
        "power_w": battery.power_w,
        "capacity_kwh": battery.capacity_kwh,
        "energy_kwh": battery.energy_kwh,
        "pv_power_w": battery.pv_power_w,
        "temperature_c": battery.temperature_c,
        "read_at": to_iso(battery.read_at),
        "stale": battery.stale,
        "last_error": battery.last_error,
        "extra": dict(battery.extra or {}),
    }


def _dispatch_in_window(
    rows: Sequence[Mapping[str, Any]], start: datetime, minutes: float
) -> dict[str, Any] | None:
    """Last dispatch per battery inside one plan slot, or None (SPEC 10 plan execution).

    A window with no dispatch row is *not dispatched* -- an advisory run, a stale slot the
    dispatcher refused, or a slot nobody reached -- and the caller renders exactly that instead of
    reading a missing row as "0 W applied".
    """
    end = start + timedelta(minutes=minutes)
    latest: dict[str, Mapping[str, Any]] = {}
    latest_stamp: datetime | None = None
    last_row: Mapping[str, Any] | None = None
    records = 0
    for row in rows:
        stamp = from_iso(row.get("ts"))
        if stamp is None or not start <= stamp < end:
            continue
        records += 1
        latest[str(row.get("battery_id") or "")] = row
        if latest_stamp is None or stamp >= latest_stamp:
            latest_stamp = stamp
            last_row = row
    if records == 0 or latest_stamp is None or last_row is None:
        return None
    batteries: list[dict[str, Any]] = []
    requested = 0.0
    applied = 0.0
    ok = True
    dry_run = False
    for battery_id in sorted(latest):
        row = latest[battery_id]
        want = float(row.get("requested_w") or 0.0)
        got = float(row.get("applied_w") or 0.0)
        row_ok = bool(row["ok"]) if "ok" in row else str(row.get("result") or "") == "ok"
        requested += want
        applied += got
        ok = ok and row_ok
        dry_run = dry_run or bool(row.get("dry_run"))
        batteries.append({"id": battery_id, "requested_w": want, "applied_w": got, "ok": row_ok})
    last = last_row
    return {
        "ts": to_iso(latest_stamp),
        "requested_w": requested,
        "applied_w": applied,
        "ok": ok,
        "dry_run": dry_run,
        "clamped": any(
            abs(item["applied_w"]) < abs(item["requested_w"]) - CLAMP_TOLERANCE_W
            for item in batteries
        ),
        "code": str(last.get("code") or ""),
        "reason": str(last.get("reason") or last.get("code") or ""),
        "message": str(last.get("message") or ""),
        "records": records,
        "batteries": batteries,
    }


def _execution_progress(
    slots: Sequence[PlanSlot], measured: Sequence[Mapping[str, Any] | None], moment: datetime, minutes: float
) -> dict[str, Any]:
    """Plan-so-far totals: intended vs measured energy and grid cost over the reached slots.

    A fully elapsed slot counts whole; the slot in progress counts by the share actually measured
    (``coverage_pct``), so both sides cover the same span and the comparison stays apples to apples.
    Measured totals stay ``None`` until something was really measured, so a fresh install never
    reports a measured zero it did not observe.
    """
    step = timedelta(minutes=minutes)
    hours = minutes / 60.0
    elapsed = 0
    reached = 0
    samples = 0
    span_minutes = 0.0
    planned_charged = planned_discharged = planned_cost = 0.0
    measured_charged = measured_discharged = imported = exported = measured_cost = 0.0
    for slot, entry in zip(slots, measured):
        if minutes <= 0:
            continue
        complete = slot.t + step <= moment
        coverage = float(entry.get("coverage_pct") or 0.0) if entry else 0.0
        share = 1.0 if complete else coverage / 100.0
        if share <= 0.0:
            continue
        if complete:
            elapsed += 1
        span_minutes += minutes * share
        planned_charged += max(0.0, float(slot.power_w)) / 1000.0 * hours * share
        planned_discharged += max(0.0, -float(slot.power_w)) / 1000.0 * hours * share
        planned_cost += slot.cost_eur(hours) * share
        if entry is None:
            continue
        reached += 1
        samples += int(entry.get("samples") or 0)
        measured_charged += float(entry.get("charged_kwh") or 0.0)
        measured_discharged += float(entry.get("discharged_kwh") or 0.0)
        imported += float(entry.get("import_kwh") or 0.0)
        exported += float(entry.get("export_kwh") or 0.0)
        # Import is billed at the slot's buy price, export earns its sell price (the plan's own
        # tariff for that slot), so measured and planned cost are compared at the same prices.
        measured_cost += (
            float(entry.get("import_kwh") or 0.0) * float(slot.buy)
            - float(entry.get("export_kwh") or 0.0) * float(slot.sell)
        )
    coverage = 0.0
    if span_minutes > 0:
        coverage = min(100.0, 100.0 * samples * float(ACTUALS_MINUTES) / span_minutes)
    return {
        "slots_elapsed": elapsed,
        "slots_measured": reached,
        "slots_total": len(slots),
        "samples": samples,
        "coverage_pct": coverage,
        "planned_charged_kwh": planned_charged,
        "planned_discharged_kwh": planned_discharged,
        "planned_cost_eur": planned_cost,
        "measured_charged_kwh": measured_charged if reached else None,
        "measured_discharged_kwh": measured_discharged if reached else None,
        "measured_import_kwh": imported if reached else None,
        "measured_export_kwh": exported if reached else None,
        "measured_cost_eur": measured_cost if reached else None,
    }
