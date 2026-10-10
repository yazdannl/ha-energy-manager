"""WP5 robustness / degradation tests (SPEC 1, 4, 7, 9, 10).

Boot and serve in every mode, with no batteries, with HA unreachable, with the price provider
failing (the fallback chain must reach ``static``), with solar rate-limited (429) and with failed
measurement sampling.  Every case must degrade with a clear alert/log entry, must not raise out of
the engine, and the API must keep answering.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "energy_manager"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.models import BatteryConfig, Mode, Reason  # noqa: E402
from fake_ha import EXAMPLE_CONFIG, FakeHAState, FakeHAServer, dead_ha_url  # noqa: E402

OBSERVED: list[str] = []


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def battery_config(**overrides):
    data = dict(
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
    )
    data.update(overrides)
    return BatteryConfig(**data)


def price_forecast(now, *, source="example_static"):
    from app.models import PriceForecast, PriceSlot

    forecast = PriceForecast(source=source, generated_at=now)
    start = now.replace(minute=0, second=0, microsecond=0)
    for index in range(96):
        moment = start + timedelta(minutes=15 * index)
        buy = 0.05 if 10 <= moment.hour < 14 else (0.90 if moment.hour >= 18 else 0.30)
        forecast.slots.append(PriceSlot(t=moment, buy=buy, sell=0.01, source=source))
    return forecast


def load_forecast(now, *, source="example_load"):
    from app.models import LoadForecast, LoadSlot

    forecast = LoadForecast(source=source, generated_at=now, confidence="low")
    start = now.replace(minute=0, second=0, microsecond=0)
    for index in range(96):
        forecast.slots.append(LoadSlot(t=start + timedelta(minutes=15 * index), load_kw=0.6))
    return forecast


class Stack:
    def __init__(self, *, server, store, settings, engine, api, ha):
        self.server = server
        self.store = store
        self.settings = settings
        self.engine = engine
        self.api = api
        self.ha = ha
        self.base = f"http://127.0.0.1:{api.address[1]}"

    def close(self):
        self.api.stop()
        if self.server is not None:
            self.server.stop()
        self.store.close()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=20) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"null")


def build_stack(
    tmp_path,
    *,
    mode="advisory",
    dry_run=True,
    with_battery=True,
    ha=None,
    server=None,
    location=(0.0, 0.0, "UTC"),
    **engine_kwargs,
):
    from app.engine import Engine
    from app.harest import HAClient
    from app.main import build_settings, build_sources, build_store
    from app.notify import Notifier
    from app.api import ApiServer

    if server is None and ha is None:
        server = FakeHAServer(
            FakeHAState(
                {
                    "number.example_battery_power": {"state": "0", "attributes": {}},
                    "sensor.example_battery_soc": {
                        "state": "55",
                        "attributes": {"unit_of_measurement": "%"},
                    },
                },
                config=EXAMPLE_CONFIG,
            )
        ).start()
    store = build_store(str(tmp_path))
    settings = build_settings(store, {}, str(tmp_path / "options.json"))
    assert settings is not None
    if with_battery:
        settings.add_battery(battery_config())
    settings.set("mode", mode)
    settings.set("dry_run", dry_run)
    settings.set("notify.enabled", False)
    settings.set("solar.enabled", False)
    settings.set("control.grid_follow", False)
    settings.set("price.source", "static")
    settings.set(
        "price.static_schedule",
        [
            {"from_hour": 10, "to_hour": 14, "price_eur_per_kwh": 0.05, "days": [0, 1, 2, 3, 4, 5, 6]},
            {"from_hour": 0, "to_hour": 24, "price_eur_per_kwh": 0.30, "days": [0, 1, 2, 3, 4, 5, 6]},
            {"from_hour": 18, "to_hour": 23, "price_eur_per_kwh": 0.90, "days": [0, 1, 2, 3, 4, 5, 6]},
        ],
    )
    if ha is None:
        ha = HAClient(server.url, "example-token", retries=0, backoff=0.0)
    clock = Clock(datetime(2026, 3, 4, 12, 30, tzinfo=timezone.utc))
    sources = build_sources(settings, store, ha, location=location)
    sources.update(engine_kwargs)  # explicit sources/sampler win over the built ones
    engine = Engine(
        settings,
        store=store,
        ha=ha,
        notifier=Notifier(ha, settings, clock=clock),
        location="Example Home",
        clock=clock,
        version="0.1.0",
        commit="example",
        **sources,
    )
    engine._registry_source = lambda: []
    api = ApiServer(engine, host="127.0.0.1", port=0).start()
    return Stack(server=server, store=store, settings=settings, engine=engine, api=api, ha=ha)


@pytest.mark.parametrize(
    "mode,dry_run,expect_writes",
    [
        ("off", True, False),
        ("off", False, False),
        ("advisory", True, False),
        ("advisory", False, False),
        ("auto", True, False),
        ("auto", False, True),
    ],
)
def test_every_mode_boots_serves_and_writes_only_when_live(tmp_path, mode, dry_run, expect_writes):
    stack = build_stack(tmp_path, mode=mode, dry_run=dry_run)
    try:
        plan = stack.engine.plan_now("wp5")
        assert plan is not None and plan.slots, (mode, dry_run)
        stack.engine.tick_now()
        writes = list(stack.server.state.service_calls)
        if expect_writes:
            assert writes, f"{mode}/{dry_run}: expected a setpoint write"
        else:
            assert writes == [], (mode, dry_run, writes)
        for path in (
            "/api/v1/health",
            "/api/v1/status",
            "/api/v1/plan",
            "/api/v1/forecast?kind=price",
            "/api/v1/batteries",
            "/api/v1/config",
            "/api/v1/savings?range=day",
            "/api/v1/log?limit=10",
            "/api/v1/providers",
        ):
            status, _body = stack.get(path)
            assert status == 200, (mode, dry_run, path, status)
        OBSERVED.append(
            f"mode={mode} dry_run={dry_run}: plan={len(plan.slots)} slots, "
            f"service_calls={len(writes)}, routes ok"
        )
    finally:
        stack.close()


def test_mode_off_still_plans_but_never_writes(tmp_path):
    """SPEC 1 says `off` does no planning; observe what the shipped engine actually does."""
    stack = build_stack(tmp_path, mode="off", dry_run=True)
    try:
        plan = stack.engine.plan_now("wp5")
        assert plan is not None
        # no writes, and the dispatch log explains the authority block
        stack.engine.tick_now()
        assert stack.server.state.service_calls == []
        entries = stack.engine.log(limit=20, kind="dispatch")
        assert entries and all(entry["code"] == Reason.MODE_NOT_AUTO for entry in entries)
        OBSERVED.append(
            f"mode=off plan_now -> {len(plan.slots)} slots (SPEC 1 wording says 'no planning'); "
            "writes=0, log reason=mode_not_auto"
        )
    finally:
        stack.close()


def test_no_batteries_configured_degrades_to_hold_schedule(tmp_path):
    stack = build_stack(tmp_path, mode="auto", dry_run=False, with_battery=False)
    try:
        plan = stack.engine.plan_now("wp5")
        assert plan is not None and plan.slots
        assert all(abs(slot.power_w) < 1e-9 for slot in plan.slots)
        assert plan.summary.charged_kwh == 0.0
        stack.engine.tick_now()
        assert stack.server.state.service_calls == []
        status, body = stack.get("/api/v1/batteries")
        assert status == 200 and body["batteries"] == []
        status, body = stack.get("/api/v1/plan")
        assert status == 200 and body["slots"]
        status, body = stack.get("/api/v1/status")
        assert status == 200 and body["batteries"] == []
        OBSERVED.append("no batteries: hold-only plan, /batteries [] , routes ok, writes=0")
    finally:
        stack.close()


def test_the_dead_ha_url_still_holds_its_port():
    """The unreachable-HA helper must keep the port it bound for the rest of the run.

    Closing the refusing socket is invisible: a later ``FakeHAServer`` inside the same run may get
    handed the same port, and the "dead" client then silently talks to a live fake -- a flap that
    depends on the platform's port allocation.  Asserting the bind keeps the guard deterministic
    (audit finding 5).
    """
    url = dead_ha_url()
    port = int(url.rsplit(":", 1)[1].split("/", 1)[0])
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        with pytest.raises(OSError):
            probe.bind(("127.0.0.1", port))
    finally:
        probe.close()
    # and the helper is stable: the same port comes back, still held
    assert dead_ha_url() == url


def test_ha_unreachable_degrades_without_exceptions(tmp_path):
    from app.harest import HAClient

    # the fake server exists (so service calls are observable) but the engine's client points at a
    # closed port: HA is unreachable for every call the app makes.
    server = FakeHAServer(FakeHAState({}, config=EXAMPLE_CONFIG)).start()
    dead = HAClient(dead_ha_url(), "example-token", retries=0, backoff=0.0)
    stack = build_stack(tmp_path, mode="auto", dry_run=False, ha=dead, server=server)
    try:
        plan = stack.engine.plan_now("wp5")
        assert plan is not None and plan.slots, "planning must survive HA being down"
        stack.engine.tick_now()  # must not raise
        assert stack.server.state.service_calls == []
        log = stack.engine.log(limit=50)
        assert any(
            entry["code"] in ("sampling_failed", "battery_unavailable", "ha_disconnected")
            for entry in log
        ), log
        status, body = stack.get("/api/v1/status")
        assert status == 200
        assert body["ha_connected"] is False
        assert body["ha_last_error"]
        for path in ("/api/v1/plan", "/api/v1/forecast?kind=price", "/api/v1/log?limit=5"):
            assert stack.get(path)[0] == 200, path
        OBSERVED.append(
            f"HA unreachable: plan ok, tick ok, ha_connected={body['ha_connected']}, "
            f"last_error={body['ha_last_error'][:60]!r}"
        )
    finally:
        stack.close()


def test_price_fallback_chain_reaches_static(tmp_path):
    """SPEC 4: configured -> energy_charts -> static, with the failed names reported."""
    from app.sources import ProviderError, RateLimited
    from app.sources.price import fetch_static, load_price_forecast

    class Settings(dict):
        def get(self, key, default=None):
            return dict.get(self, key, default)

    schedule = [
        {"from_hour": 0, "to_hour": 24, "price_eur_per_kwh": 0.25, "days": [0, 1, 2, 3, 4, 5, 6]}
    ]
    settings = Settings(
        {
            "price.source": "zonneplan_entity",
            "price.entity": "sensor.example_price_now",
            "price.static_schedule": schedule,
        }
    )

    class FailingClient:
        def get_state(self, entity_id):
            raise ProviderError("zonneplan_entity: sensor has no price list attribute")

    def failing_energy_charts(client, settings, now=None, http=None, tz=None):
        raise ProviderError("energy_charts: provider request failed")

    now = datetime(2026, 3, 4, 12, 30, tzinfo=timezone.utc)
    forecast, statuses = load_price_forecast(
        settings,
        FailingClient(),
        now=now,
        providers={
            "zonneplan_entity": lambda client, settings, now=None, http=None, tz=None: __import__(
                "app.sources.price", fromlist=["fetch_zonneplan_entity"]
            ).fetch_zonneplan_entity(client, settings, now=now, http=http, tz=tz),
            "energy_charts": failing_energy_charts,
            "static": fetch_static,
        },
    )
    assert forecast.source == "static"
    assert forecast.slots
    assert forecast.degraded is True
    assert forecast.notes and "fell back" in forecast.notes[-1]
    by_name = {status.name: status for status in statuses}
    assert by_name["zonneplan_entity"].last_error
    assert by_name["energy_charts"].last_error
    assert by_name["static"].ok is True
    OBSERVED.append(
        f"price fallback: source={forecast.source} degraded={forecast.degraded} "
        f"notes={forecast.notes[-1][:70]!r}"
    )


def test_price_fallback_survives_an_ha_transport_error():
    """Re-verification of WP5 finding 1 (fixed): a Home Assistant transport error is a
    per-provider failure, so the chain still reaches ``static``."""
    from app.harest import HAError
    from app.sources.price import fetch_static, load_price_forecast

    class Settings(dict):
        def get(self, key, default=None):
            return dict.get(self, key, default)

    class UnreachableClient:
        def get_state(self, entity_id):
            raise HAError("HA GET /states/... unavailable: connection refused")

    def failing_energy_charts(client, settings, now=None, http=None, tz=None):
        from app.sources import ProviderError

        raise ProviderError("energy_charts: provider request failed")

    settings = Settings(
        {
            "price.source": "zonneplan_entity",
            "price.entity": "sensor.example_price_now",
            "price.static_schedule": [
                {"from_hour": 0, "to_hour": 24, "price_eur_per_kwh": 0.25, "days": [0, 1, 2, 3, 4, 5, 6]}
            ],
        }
    )
    forecast, _statuses = load_price_forecast(
        settings,
        UnreachableClient(),
        now=datetime(2026, 3, 4, 12, 30, tzinfo=timezone.utc),
        providers={
            "zonneplan_entity": __import__(
                "app.sources.price", fromlist=["fetch_zonneplan_entity"]
            ).fetch_zonneplan_entity,
            "energy_charts": failing_energy_charts,
            "static": fetch_static,
        },
    )
    assert forecast.source == "static"


def test_price_provider_failure_at_engine_level_keeps_serving(tmp_path):
    def broken_price(now):
        raise RuntimeError("example provider outage")

    stack = build_stack(
        tmp_path,
        mode="advisory",
        dry_run=True,
        price_source=broken_price,
    )
    try:
        plan = stack.engine.plan_now("wp5")
        assert plan is None, "no usable forecast must not invent a plan"
        log = stack.engine.log(limit=50)
        assert any(entry["code"] == "forecast_unavailable" for entry in log), log
        status, body = stack.get("/api/v1/status")
        assert status == 200
        assert any("forecast" in alert["code"] for alert in body["alerts"])
        assert stack.get("/api/v1/plan")[0] == 200
        OBSERVED.append("price provider outage: plan withheld, forecast_unavailable alert, API ok")
    finally:
        stack.close()


def test_solar_rate_limit_degrades_and_backs_off(tmp_path):
    from app.sources import RateLimited
    from app.sources.solar import load_solar_forecast

    class Settings(dict):
        def get(self, key, default=None):
            return dict.get(self, key, default)

    class Store:
        def __init__(self):
            self.profiles = {}
            self.cache = {}

        def profile_get(self, kind, key):
            return self.profiles.get((kind, key))

        def profile_put(self, kind, key, payload, updated_at=None):
            self.profiles[(kind, key)] = (updated_at, payload)

        def cache_get(self, kind, key):
            return self.cache.get((kind, key))

        def cache_put(self, kind, key, payload, fetched_at=None):
            self.cache[(kind, key)] = (fetched_at, payload)

    def rate_limited(url, **kwargs):
        raise RateLimited("provider rate limited (HTTP 429)")

    settings = Settings(
        {
            "solar.enabled": True,
            "solar.planes": [{"kwp": 3.0, "tilt_deg": 35.0, "azimuth_deg": 0.0}],
            "solar.api_key": "",
            "optimizer.horizon_hours": 12,
        }
    )
    now = datetime(2026, 3, 4, 12, 30, tzinfo=timezone.utc)
    store = Store()
    forecast, status = load_solar_forecast(
        settings, store, None, location=(0.0, 0.0, "UTC"), now=now, http=rate_limited
    )
    assert forecast is not None and forecast.slots == []
    assert status.ok is False
    assert status.last_error or forecast.notes, (status, forecast.notes)
    assert any("429" in note or "rate" in note.lower() for note in forecast.notes), forecast.notes
    # the back-off state was recorded; a second call does not even attempt the HTTP request
    calls = []

    def counting_http(url, **kwargs):
        calls.append(url)
        raise RateLimited("provider rate limited (HTTP 429)")

    forecast2, _status2 = load_solar_forecast(
        settings, store, None, location=(0.0, 0.0, "UTC"), now=now + timedelta(seconds=30), http=counting_http
    )
    assert calls == []
    assert any("back" in note.lower() for note in forecast2.notes), forecast2.notes
    OBSERVED.append(
        f"solar 429: no raise, status_error={bool(status.last_error)}, notes={forecast.notes}"
    )


def test_failed_sampling_is_journalled_and_the_tick_survives(tmp_path):
    def broken_sampler(now):
        raise RuntimeError("example measurement outage")

    stack = build_stack(
        tmp_path,
        mode="auto",
        dry_run=False,
        sampler=broken_sampler,
    )
    try:
        stack.engine.plan_now("wp5")
        stack.engine.tick_now()  # must not raise
        log = stack.engine.log(limit=50)
        assert any(entry["code"] == "sampling_failed" for entry in log), log
        status, body = stack.get("/api/v1/status")
        assert status == 200 and body["ha_connected"] is False
        assert stack.get("/api/v1/plan")[0] == 200
        OBSERVED.append("sampling outage: tick survived, sampling_failed journal entry, API ok")
    finally:
        stack.close()


def test_stale_battery_state_blocks_writes_and_is_logged(tmp_path):
    """A stale measurement must hold power, with a dispatch row naming the reason."""
    from app.control.dispatcher import Dispatcher
    from app.models import BatteryState, Plan, PlanSlot, SystemState, Action

    class FakeJournal:
        def __init__(self):
            self.dispatches = []
            self.events = []

        def dispatch(self, entry):
            self.dispatches.append(dict(entry))

        def event(self, level, code, message, payload=None):
            self.events.append((level, code, message, payload or {}))

    class FakeDriver:
        def __init__(self):
            self.applied = []

        def id(self):
            return "example_battery_1"

        def read_state(self):
            return BatteryState(
                id="example_battery_1",
                available=True,
                soc_pct=50.0,
                power_w=0.0,
                read_at=datetime(2026, 3, 4, 8, 0, tzinfo=timezone.utc),  # hours old
                stale=False,
            )

        def apply(self, power_w, *, dry_run):
            self.applied.append((power_w, dry_run))
            raise AssertionError("a stale battery must never be written")

        def hold(self, *, dry_run):
            raise AssertionError("hold must not be needed here")

    settings = {
        "control.tick_seconds": 30,
        "control.stale_state_seconds": 180,
        "control.grid_follow": False,
        "optimizer.max_grid_import_w": 0,
        "optimizer.max_grid_export_w": 0,
    }
    journal = FakeJournal()
    driver = FakeDriver()
    dispatcher = Dispatcher(settings, journal, {"example_battery_1": driver})
    now = datetime(2026, 3, 4, 12, 30, tzinfo=timezone.utc)
    slot = PlanSlot(
        t=now,
        buy=0.3,
        sell=0.05,
        pv_kw=0.0,
        load_kw=0.6,
        soc_kwh=2.5,
        power_w=500.0,
        action=Action.CHARGE,
        reason=Reason.ARBITRAGE_CHARGE,
    )
    plan = Plan(
        generated_at=now,
        horizon_start=now,
        horizon_end=now + timedelta(hours=1),
        slot_minutes=15,
        slots=[slot],
    )
    state = SystemState(
        mode=Mode.AUTO.value,
        dry_run=False,
        ha_connected=True,
        now=now,
        batteries=[driver.read_state()],
    )
    outcome = dispatcher.tick(
        now, state=state, plan=plan, batteries=[battery_config()]
    )
    assert outcome.block is True
    assert outcome.allowed_w == 0.0
    assert Reason.STALE_STATE in outcome.reasons
    assert driver.applied == []
    assert journal.dispatches and journal.dispatches[0]["code"] == Reason.STALE_STATE
    OBSERVED.append(
        f"stale battery: block={outcome.block} reasons={outcome.reasons} writes=0"
    )


# --- finding 1 re-verification: every provider failure is a per-provider failure -------------

_PROVIDER_FAILURES = {
    "ha_error": "app.harest:HAError",
    "provider_error": "app.sources.price:ProviderError",
    "rate_limited": "app.sources.price:RateLimited",
    "runtime_error": "builtins:RuntimeError",
    "key_error": "builtins:KeyError",
    "zero_division": "builtins:ZeroDivisionError",
    "timeout": "builtins:TimeoutError",
    "attribute_error": "builtins:AttributeError",
    "type_error": "builtins:TypeError",
}


def _raise_factory(target: str, message: str = "example provider failure"):
    module_name, name = target.split(":")

    def raiser(client, settings, now=None, http=None, tz=None):
        raise __import__(module_name, fromlist=[name]).__dict__[name](message)

    raiser.__name__ = f"raises_{name}"
    return raiser


class _PriceSettings(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


_PRICE_SETTINGS = _PriceSettings(
    {
        "price.source": "zonneplan_entity",
        "price.entity": "sensor.example_price_now",
        "price.static_schedule": [
            {"from_hour": 0, "to_hour": 24, "price_eur_per_kwh": 0.25, "days": [0, 1, 2, 3, 4, 5, 6]}
        ],
    }
)


class _UnreachableHA:
    def get_state(self, entity_id):
        from app.harest import HAError

        raise HAError(f"HA GET /states/{entity_id} unavailable: connection refused")


def _chain_forecast(first, second):
    from app.sources.price import fetch_static, load_price_forecast

    return load_price_forecast(
        _PRICE_SETTINGS,
        _UnreachableHA(),
        now=datetime(2026, 3, 4, 12, 30, tzinfo=timezone.utc),
        providers={
            "zonneplan_entity": first,
            "energy_charts": second,
            "static": fetch_static,
        },
    )


@pytest.mark.parametrize("target", sorted(_PROVIDER_FAILURES))
def test_price_chain_reaches_static_after_any_configured_provider_failure(target):
    """WP5 finding 1 (fixed): any exception from the configured provider must not abort the chain.

    Falsifies both the original bug (HAError escaped) and a narrower fix that only catches the
    documented provider errors: a third-party provider blowing up with an arbitrary Python
    exception must still fall through energy_charts to the static schedule.
    """
    forecast, statuses = _chain_forecast(
        _raise_factory(_PROVIDER_FAILURES[target], f"{target}: broken"),
        _raise_factory("app.sources.price:ProviderError", "energy_charts: broken"),
    )
    by_name = {status.name: status for status in statuses}
    assert forecast.source == "static"
    assert forecast.slots, "the static fallback must carry slots"
    assert forecast.degraded is True
    assert not by_name["zonneplan_entity"].ok and by_name["zonneplan_entity"].last_error
    assert not by_name["energy_charts"].ok and by_name["energy_charts"].last_error
    assert by_name["static"].ok is True
    assert by_name["static"].last_success is not None
    assert any("static" in note for note in forecast.notes)


def test_price_chain_raises_price_unavailable_when_every_provider_fails():
    """The chain must still be loud when there is genuinely nothing to serve."""
    from app.sources.price import PriceUnavailable, load_price_forecast

    with pytest.raises(PriceUnavailable) as err:
        load_price_forecast(
            _PRICE_SETTINGS,
            _UnreachableHA(),
            now=datetime(2026, 3, 4, 12, 30, tzinfo=timezone.utc),
            providers={
                "zonneplan_entity": _raise_factory("app.sources.price:ProviderError", "zonneplan: broken"),
                "energy_charts": _raise_factory("app.sources.price:RateLimited", "energy_charts: 429"),
                "static": _raise_factory("app.sources.price:ProviderError", "static: empty schedule"),
            },
        )
    assert "zonneplan" in str(err.value)
    assert "energy_charts" in str(err.value)
    assert "static" in str(err.value)


def test_price_chain_treats_a_returned_forecast_without_future_slots_as_a_failure():
    """WP5 nit (fixed): an *empty* returned forecast is a provider failure, not an empty success.

    SPEC 4 wants every shape of "nothing usable" to fail through the chain: a zero-slot forecast, a
    forecast that only contains past slots, one that starts beyond the requested horizon, and a
    provider that returns ``None`` instead of a forecast.  The chain must then continue to
    ``static``, the fallback note must name the reason, and a chain whose every source is empty must
    raise ``PriceUnavailable`` naming the sources (which is what the engine turns into the
    user-visible ``forecast_unavailable`` event).
    """
    from app.models import PriceForecast, PriceSlot
    from app.sources.price import (
        SLOT_MINUTES,
        PriceUnavailable,
        horizon_hours,
        load_price_forecast,
        quarter_floor,
    )

    now = datetime(2026, 3, 4, 12, 30, tzinfo=timezone.utc)
    start = quarter_floor(now, SLOT_MINUTES)
    end = start + timedelta(hours=horizon_hours(_PRICE_SETTINGS))
    reason = "no future price slots in the requested horizon"

    def _forecast(moments):
        forecast = PriceForecast(source="example_empty", generated_at=now)
        for moment in moments:
            forecast.slots.append(PriceSlot(t=moment, buy=0.20, sell=0.01, source="example_empty"))
        return forecast

    def empty(client, settings, now=None, http=None, tz=None):
        return _forecast([])

    def past_only(client, settings, now=None, http=None, tz=None):
        return _forecast([now - timedelta(hours=2), now - timedelta(minutes=15)])

    def beyond_horizon(client, settings, now=None, http=None, tz=None):
        return _forecast([end + timedelta(hours=1), end + timedelta(hours=2)])

    def none_forecast(client, settings, now=None, http=None, tz=None):
        return None

    empty_shapes = [
        (empty, reason),
        (past_only, reason),
        (beyond_horizon, reason),
        (none_forecast, "AttributeError"),
    ]
    for provider, expected in empty_shapes:
        forecast, statuses = _chain_forecast(
            provider, _raise_factory("app.sources.price:ProviderError", "energy_charts: broken")
        )
        by_name = {status.name: status for status in statuses}
        assert forecast.source == "static", provider.__name__
        assert forecast.slots and forecast.degraded is True, provider.__name__
        assert by_name["zonneplan_entity"].ok is False, provider.__name__
        assert expected in by_name["zonneplan_entity"].last_error, (
            provider.__name__, by_name["zonneplan_entity"].last_error
        )
        assert "energy_charts" in by_name["energy_charts"].last_error
        assert by_name["static"].ok is True
        assert any("static" in note for note in forecast.notes), provider.__name__

    # every source empty -> loud failure naming them, which is what raises forecast_unavailable
    with pytest.raises(PriceUnavailable) as err:
        load_price_forecast(
            _PRICE_SETTINGS,
            _UnreachableHA(),
            now=now,
            providers={
                "zonneplan_entity": empty,
                "energy_charts": past_only,
                "static": beyond_horizon,
            },
        )
    message = str(err.value)
    for name in ("zonneplan_entity", "energy_charts", "static"):
        assert name in message, message
    assert message.count(reason) == 3, message


def test_engine_ticks_safely_on_a_zero_slot_price_forecast(tmp_path):
    """A price forecast without slots must not produce invented power or crashes (SPEC 4)."""
    from app.models import PriceForecast

    def empty_price(now):
        return PriceForecast(source="example_empty", generated_at=now)

    stack = build_stack(tmp_path, mode="auto", dry_run=True, price_source=empty_price)
    try:
        outcome = stack.engine.tick_now()
        assert outcome.plan_power_w is None
        assert outcome.target_w == 0.0 and outcome.allowed_w == 0.0
        assert outcome.writes == 0
        assert stack.get("/api/v1/status")[0] == 200
        OBSERVED.append("zero-slot price forecast: no plan, no target, no writes, API ok")
    finally:
        stack.close()


def test_mode_off_stops_planning_and_clears_the_stale_plan(tmp_path):
    """WP5 finding 4 (fixed): `off` must not plan, and no stale plan may stay on screen."""
    stack = build_stack(tmp_path, mode="auto", dry_run=True)
    try:
        plan = stack.engine.plan_now("wp5")
        assert plan is not None and plan.slots, "the fixture must produce a plan in auto"
        assert stack.get("/api/v1/plan")[1].get("slots"), "auto keeps a current plan"

        stack.engine.set_mode("off")
        engine = stack.engine
        assert engine.active_plan() is None, "the stale plan must be dropped when planning stops"
        assert not stack.get("/api/v1/plan")[1].get("slots"), "/plan must not show a current plan"
        outcome = engine.tick_now()
        assert outcome.plan_power_w is None and outcome.target_w == 0.0, outcome
        # a queued recompute is dropped, never deferred to a later tick
        assert engine.request_replan("wp5") == {"queued": True, "reason": "wp5"}
        assert engine.active_plan() is None, "a recompute request must not resurrect a plan in off"
        codes = [entry["code"] for entry in engine.log(limit=100)]
        assert "plan_cleared" in codes, codes
        # an explicit plan_now() still plans on demand (documented escape hatch)
        assert engine.plan_now("wp5") is not None
        OBSERVED.append("mode off: plan cleared, no plan in /plan, recompute dropped, no writes")
    finally:
        stack.close()


def test_a_plan_computed_without_a_live_soc_is_withheld_until_the_first_read(tmp_path):
    """WP5 finding 7 (fixed): no plan is published, stored or dispatched before a battery reading.

    ``plan_now()`` may still compute a schedule for its caller (the API and ``--selfcheck`` answer
    with it), but with no live SoC the plan is withheld: ``active_plan()`` is ``None``, nothing is
    persisted, the tick issues no service call, and one ``battery_reading_pending`` event/alert is
    raised (once, not per call).  Once the SoC becomes readable the first tick publishes a plan
    anchored on the *measured* energy and issues a real write.
    """
    from app.harest import HAClient

    server = FakeHAServer(
        FakeHAState(
            {
                "number.example_battery_power": {"state": "0", "attributes": {}},
                # no soc entity on purpose: the SoC is not readable yet
            },
            config=EXAMPLE_CONFIG,
        )
    ).start()
    stack = build_stack(
        tmp_path,
        mode="auto",
        dry_run=False,
        server=server,
        ha=HAClient(server.url, "example-token", retries=0, backoff=0.0),
    )
    engine = stack.engine
    try:
        plan = engine.plan_now("wp5_warmup")
        assert plan is not None and plan.slots, "the caller still sees a computed schedule"
        # ... but it is withheld from the UI, from storage and from dispatch
        assert engine.active_plan() is None
        assert engine.snapshot().active_plan is None
        outcome = engine.tick_now()
        assert outcome.plan_power_w is None, outcome
        assert outcome.target_w == 0.0 and outcome.writes == 0, outcome
        assert server.state.service_calls == [], server.state.service_calls
        pending = [entry for entry in engine.log(limit=100) if entry["code"] == "battery_reading_pending"]
        # one event row plus the matching alert row, latched for the process
        assert {entry["kind"] for entry in pending} == {"event", "alert"}, pending
        assert any(alert.code == "battery_reading_pending" for alert in engine.snapshot().alerts)
        pending_count = len(pending)
        # asking again must not add rows: no event spam while warming up
        engine.plan_now("wp5_warmup_again")
        engine.request_replan("wp5_warmup_again")
        assert len([e for e in engine.log(limit=100) if e["code"] == "battery_reading_pending"]) == pending_count
        # the replan decision function must not ask for a replan while nothing can be planned
        assert engine._replan_reason() is None

        # the SoC becomes readable: 55 % of 5 kWh = 2.75 kWh
        server.state.states["sensor.example_battery_soc"] = {
            "state": "55",
            "attributes": {"unit_of_measurement": "%"},
        }
        outcome = engine.tick_now()
        published = engine.active_plan()
        assert published is not None, "the first tick after a readable SoC must publish a plan"
        assert published.slots[0].soc_kwh == pytest.approx(2.75), published.slots[0]
        assert outcome.plan_power_w == published.slot_at(outcome.ts).power_w
        assert outcome.writes >= 1, outcome
        assert any(
            call[2]["entity_id"] == "number.example_battery_power" and float(call[2]["value"]) != 0.0
            for call in server.state.service_calls
        ), server.state.service_calls
        assert any(entry["code"] == "battery_reading_ok" for entry in engine.log(limit=100))
        # and the UI sees it, too
        status, body = stack.get("/api/v1/plan")
        assert status == 200 and body.get("slots"), body
        OBSERVED.append("warm-up: plan withheld until the first SoC read, then measured-anchored + real write")
    finally:
        stack.close()


def test_zero_enabled_batteries_are_not_stuck_behind_the_warming_up_gate(tmp_path):
    """Step 3a: an install with no enabled battery must keep planning and keep serving.

    The warming-up gate is a "no live SoC" gate; with zero enabled batteries there is nothing to
    read, so the documented do-nothing schedule is authoritative.  The gate must not swallow the
    plan, must not log or alert as if a reading were pending, and must not fill the journal.
    """
    stack = build_stack(tmp_path, mode="auto", dry_run=False, with_battery=False)
    try:
        plan = stack.engine.plan_now("wp5_no_batteries")
        assert plan is not None and plan.slots
        assert stack.engine.active_plan() is not None, "no-battery installs still get a plan"
        for _ in range(10):
            stack.engine.tick_now()
        assert stack.engine.active_plan() is not None
        rows = stack.engine.log(limit=200)
        assert not [row for row in rows if row["code"] == "battery_reading_pending"], rows
        assert not [row for row in rows if row["code"] == "battery_reading_ok"], rows
        assert len(rows) <= 40, ("journal grew without a battery", len(rows))
        # the API and the shipped UI still answer
        for path in ("/api/v1/health", "/api/v1/status", "/api/v1/plan", "/api/v1/batteries", "/api/v1/config"):
            status, body = stack.get(path)
            assert status == 200, (path, status, body)
        assert stack.get("/api/v1/batteries")[1].get("batteries") == []
        assert stack.get("/api/v1/savings?range=day")[0] == 200
        OBSERVED.append("zero batteries: plan kept, no pending events, API+UI still answer")
    finally:
        stack.close()


def test_the_replan_loop_does_not_spin_while_no_live_soc_exists(tmp_path):
    """Step 3b: while no live SoC exists the replan loop must stay quiet and bounded."""
    from app.harest import HAClient

    server = FakeHAServer(
        FakeHAState(
            {"number.example_battery_power": {"state": "0", "attributes": {}}},
            config=EXAMPLE_CONFIG,
        )
    ).start()
    stack = build_stack(
        tmp_path,
        mode="auto",
        dry_run=False,
        server=server,
        ha=HAClient(server.url, "example-token", retries=0, backoff=0.0),
    )
    engine = stack.engine
    try:
        def counts(code):
            return len([row for row in engine.log(limit=500) if row["code"] == code])

        engine.tick_now()
        plans_before = counts("plan_updated")
        replan_marks = [engine._last_replan_at]
        for _ in range(5):
            engine.request_replan("wp5_spin_check")
            assert engine._replan_reason() is None, "the loop must not be asked to replan"
            engine.tick_now()
            replan_marks.append(engine._last_replan_at)
        assert counts("plan_updated") == plans_before, "no plan was recomputed while warming up"
        assert replan_marks == [None] * len(replan_marks), replan_marks
        # a *direct* plan attempt journals the pending event once (event row + alert row), latched
        engine.plan_now("wp5_spin_check")
        engine.plan_now("wp5_spin_check_again")
        assert counts("battery_reading_pending") == 2, counts("battery_reading_pending")

        # once a SoC exists the same engine replans and publishes, so the gate is a gate, not a stop
        server.state.states["sensor.example_battery_soc"] = {
            "state": "55",
            "attributes": {"unit_of_measurement": "%"},
        }
        # the first tick after the reading publishes the measured plan and stops the drain
        engine.tick_now()
        published = engine.active_plan()
        assert published is not None and published.slots[0].soc_kwh == pytest.approx(2.75)
        assert counts("plan_updated") > plans_before
        OBSERVED.append("replan loop: no spin without a SoC, resumes as soon as one exists")
    finally:
        stack.close()
