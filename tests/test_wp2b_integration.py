"""WP2B integration test: the engine against WP2A's real ``config``/``store``/``sources`` modules.

Everything runs offline: the price source is ``static``, solar is disabled and the load forecast
uses its fallback profile.  Skipped when the WP2A modules are not on disk, so the rest of the WP2B
suite still runs standalone.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "energy_manager"))

pytest.importorskip("app.config")
pytest.importorskip("app.store")
pytest.importorskip("app.sources.price")

from app.api import ApiServer  # noqa: E402
from app.engine import Engine  # noqa: E402
from app.main import (  # noqa: E402
    EXAMPLE_BATTERY,
    SELFCHECK_OPTIONS,
    _FakeHA,
    build_settings,
    build_sources,
    build_store,
)
from app.models import Mode, Reason  # noqa: E402


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)
        return self.now


@pytest.fixture()
def wired(tmp_path):
    store = build_store(str(tmp_path))
    assert store is not None
    settings = build_settings(store, {}, str(tmp_path / "options.json"))
    assert settings is not None
    settings.add_battery(EXAMPLE_BATTERY)
    for key, value in SELFCHECK_OPTIONS.items():
        settings.set(key, value)
    clock = Clock()
    ha = _FakeHA()
    sources = build_sources(settings, store, ha, location=(52.0, 5.0, "UTC"))
    engine = Engine(
        settings,
        store=store,
        ha=ha,
        clock=clock,
        version="0.1.0",
        commit="example",
        **sources,
    )
    engine._registry_source = lambda: []  # no websocket in tests
    yield engine, settings, store, ha, clock
    store.close()


def test_sampler_forwards_the_pv_report_timestamp(wired):
    engine, settings, _store, ha, clock = wired
    settings.set("solar.measured_entity", "sensor.example_pv_power")
    ha.states["sensor.example_pv_power"] = {
        "state": "2.1",
        "attributes": {"unit_of_measurement": "kW"},
        "last_updated": clock.now.isoformat(),
        "last_reported": clock.now.isoformat(),
    }

    sampled = engine._sampler(clock.now)

    assert sampled["pv_w"] == pytest.approx(2100.0)
    assert sampled["pv_updated_at"] == clock.now


def test_plan_and_dispatch_through_the_real_store(wired):
    engine, settings, store, ha, clock = wired
    plan = engine.plan_now("integration")
    assert plan is not None and plan.slots and plan.feasible
    assert plan.sources["price"] == "static"
    # the horizon is whatever DEFAULTS says (30 h), never a hardcoded number
    assert plan.horizon_end - plan.horizon_start == timedelta(
        hours=max(12, min(48, int(settings.get("optimizer.horizon_hours"))))
    )
    assert not any(slot.price_stale for slot in plan.slots)
    # the optimiser fills the SPEC 10 slot fields
    assert all(slot.price_source for slot in plan.slots)
    assert {slot.pv_source for slot in plan.slots} <= {"", plan.sources.get("solar", "")}
    assert all(slot.soc_pct is not None for slot in plan.slots)

    # the plan is persisted by WP2A's store
    stored = store.latest_plan()
    assert stored is not None and len(stored.slots) == len(plan.slots)

    engine.tick_now()
    assert not ha.service_calls  # advisory never writes
    # let a minute pass so the day's savings row is accumulated and flushed (SPEC 11)
    clock.advance(seconds=60)
    engine.tick_now()
    assert not ha.service_calls

    engine.set_mode(Mode.AUTO.value, True)
    engine.tick_now()
    assert not ha.service_calls  # dry run never writes
    dry_entries = store.dispatch_log(limit=10)
    assert dry_entries and any(entry["dry_run"] for entry in dry_entries)

    engine.set_mode(Mode.AUTO.value, False)
    # the plan is anchored on the measured SoC now (not on the floor), so a mid-day slot holds;
    # move into the plan's own next-day cheap window, where it really charges
    clock.advance(hours=16)
    engine.plan_now("integration")  # a fresh plan for the new moment (old plans are not executed)
    engine.set_override(-400.0, 5.0)  # a deterministic target for the write half of this test
    engine.tick_now()
    assert ha.service_calls
    writes = store.dispatch_log(limit=10)
    assert writes
    newest = writes[0]
    assert newest["result"] in ("ok", "skipped") and newest["dry_run"] == 0
    assert newest["battery_id"] == "example_battery_1"

    # measurement/actuals/savings rows land in the real schema
    clock.advance(seconds=30)
    engine.tick_now()
    actuals = store.actuals_range(clock.now - timedelta(hours=1), clock.now + timedelta(hours=1))
    assert actuals, "expected an actuals row"
    savings = store.savings_daily_range("2026-01-01", "2026-01-01")
    assert savings and savings[0]["day"] == "2026-01-01"
    buckets, totals = engine.savings("day")
    assert len(buckets) == 1 and totals["buckets"] == 1.0

    events = store.events(limit=50)
    assert any(event["code"] == "plan_updated" for event in events)


def test_real_settings_descriptors_and_patch_through_the_api(wired):
    engine, settings, store, ha, clock = wired
    api = ApiServer(engine, host="127.0.0.1", port=0)

    status, _headers, _content, body = _call(api, "GET", "/api/v1/config")
    assert status == 200
    rows = {row["key"]: row for row in body["settings"]}
    # The advertised set is exactly config.DEFAULTS, in both directions.
    defaults = build_defaults()
    assert set(rows) == set(defaults)
    for row in rows.values():
        # SPEC 10: `advanced` is the additive row field that drives the Settings disclosure (SPEC 13.2).
        assert set(row) == {"key", "value", "type", "default", "label", "help", "group", "choices", "advanced"}
        assert isinstance(row["advanced"], bool), row["key"]
        assert "(advanced)" not in row["label"], "folding is the only signal, not the label text"
    assert rows["mode"]["choices"] == ["off", "advisory", "auto"]
    for key in ("grid.power_entity", "grid.import_entity", "grid.export_entity"):
        assert key in rows

    status, _headers, _content, body = _call(
        api, "PATCH", "/api/v1/config", {"settings": {"control.tick_seconds": 45}}
    )
    assert status == 200 and body["applied"] == ["control.tick_seconds"]
    assert int(settings.get("control.tick_seconds")) == 45

    status, _headers, _content, body = _call(
        api, "PATCH", "/api/v1/config", {"settings": {"mode": "example"}}
    )
    # per-key rejects arrive as 200 + errors so the form can render them (SPEC 10)
    assert status == 200 and body["applied"] == []
    assert body["errors"] and body["errors"][0]["key"] == "mode"
    assert settings.get("mode") != "example"

    status, _headers, _content, body = _call(
        api, "PATCH", "/api/v1/config", {"settings": {"latitude_override": 12.0}}
    )
    assert status == 200 and body["applied"] == ["latitude_override"]
    settings.set("latitude_override", None)


def test_engine_battery_crud_with_real_settings(wired):
    engine, settings, store, ha, clock = wired
    created = engine.create_battery(
        {
            "id": "example_battery_2",
            "name": "Example Battery 2",
            "driver": "generic",
            "capacity_kwh": 3.0,
            "mapping": {"power_setpoint_entity": "number.example_battery_power"},
        }
    )
    assert created["id"] == "example_battery_2"
    assert {battery.id for battery in settings.battery_list()} == {
        "example_battery_1",
        "example_battery_2",
    }
    engine.update_battery("example_battery_2", {"max_charge_w": 750.0})
    assert engine.battery_configs()[1].max_charge_w == 750.0
    engine.delete_battery("example_battery_2")
    assert [battery.id for battery in settings.battery_list()] == ["example_battery_1"]
    with pytest.raises(ValueError):
        engine.create_battery({"id": "bad", "driver": "generic", "capacity_kwh": 1.0, "mapping": {}})


def test_solar_module_owns_the_pv_bias_profile(wired):
    engine, settings, store, ha, clock = wired
    engine.plan_now("integration")
    assert engine._pv_bias_external is True  # delegated to sources.solar when store + module exist
    # learning a sample persists through WP2A's profile store
    from app import analytics
    from app.sources.solar import update_solar_bias, solar_bias_factors

    payload = update_solar_bias(
        store, [(clock.now, 2.0, 3.0)], now=clock.now, tz="UTC"
    )
    assert payload["factors"][clock.now.hour] > 1.0
    assert solar_bias_factors(store, now=clock.now)[clock.now.hour] > 1.0
    stored = store.profile_get("solar_bias", "hourly")
    assert stored is not None and stored[1]["factors"][clock.now.hour] > 1.0
    del analytics  # imported for symmetry with the in-engine fallback path


def test_api_status_plan_and_log_against_the_wired_engine(wired):
    engine, settings, store, ha, clock = wired
    engine.plan_now("integration")
    engine.tick_now()
    api = ApiServer(engine, host="127.0.0.1", port=0)
    for target in (
        "/api/v1/health",
        "/api/v1/status",
        "/api/v1/plan",
        "/api/v1/forecast?kind=price",
        "/api/v1/forecast?kind=load",
        "/api/v1/batteries",
        "/api/v1/config",
        "/api/v1/savings?range=day",
        "/api/v1/accuracy",
        "/api/v1/log",
        "/api/v1/providers",
        "/api/v1/export?what=plan&format=json",
    ):
        status, _headers, _content, body = _call(api, "GET", target)
        assert status == 200, (target, body)
    _status, _headers, _content, log_body = _call(api, "GET", "/api/v1/log?limit=5&kind=dispatch")
    assert log_body["entries"]
    assert any(entry["code"] == Reason.MODE_NOT_AUTO for entry in log_body["entries"])


def build_defaults() -> dict:
    from app.api import default_settings

    return default_settings()


def test_grid_confirmations_persist_into_the_settings_keys(wired):
    """SPEC 3 ``grid.*``: a confirmed discovery role must reach the sampler/derived-load keys."""
    engine, settings, store, ha, clock = wired
    result = engine.apply_grid_mapping(
        {"grid_power": "sensor.example_grid_power", "grid_export": "sensor.example_grid_export"}
    )
    assert result["applied"] == ["grid.export_entity", "grid.power_entity"]
    assert result["errors"] == []
    assert settings.get("grid.power_entity") == "sensor.example_grid_power"
    assert engine.grid_mapping() == {
        "grid_power": "sensor.example_grid_power",
        "grid_import": "",
        "grid_export": "sensor.example_grid_export",
    }
    # it is an ordinary settings patch, so the documented PATCH route persists it too
    api = ApiServer(engine, host="127.0.0.1", port=0)
    status, _headers, _content, body = _call(
        api, "PATCH", "/api/v1/config", {"settings": {"grid.import_entity": "sensor.example_import"}}
    )
    assert status == 200 and body["applied"] == ["grid.import_entity"] and body["errors"] == []
    assert engine.grid_mapping()["grid_import"] == "sensor.example_import"
    try:
        engine.apply_grid_mapping({"example_role": "sensor.example_x"})
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    try:
        engine.apply_grid_mapping({"grid_power": "not an entity id"})
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_real_engine_battery_view_and_stop_route(wired):
    """The real engine + API: cycles, flat capabilities and the atomic stop (SPEC 9.4/10)."""
    engine, settings, store, ha, clock = wired
    engine.plan_now("integration")
    engine.set_mode(Mode.AUTO.value, False)
    engine.tick_now()

    api = ApiServer(engine, host="127.0.0.1", port=0)
    status, _headers, _content, body = _call(api, "GET", "/api/v1/batteries")
    assert status == 200
    record = body["batteries"][0]
    assert set(record) == {
        "config",
        "state",
        "capabilities",
        "mapping_resolved",
        "cycles",
        "discovery",
        # SPEC 5 learned capacity (additive): flat fields plus the nested detail.
        "capacity",
        "capacity_source",
        "capacity_kwh",
        "capacity_learned_kwh",
        "capacity_samples",
        "capacity_updated_at",
        "effective_capacity_kwh",
        "capacity_reason",
        # SPEC 5 learned round trip (0.31.0, additive): the same shape as the capacity fields.
        "efficiency",
        "efficiency_round_trip",
        "efficiency_learned_round_trip",
        "efficiency_samples",
        "efficiency_updated_at",
        "efficiency_reason",
        # SPEC 8.1 scheduled full-charge maintenance (additive): the per-battery state.
        "maintenance",
    }
    assert isinstance(record["cycles"], float)
    assert record["state"]["extra"]["cycles"] == record["cycles"]
    assert record["capabilities"]["driver"] == "generic"
    assert all(
        not isinstance(value, dict)
        for key, value in record["capabilities"].items()
        if key != "entities"
    )

    # the store accumulates a savings row with cycles, and /savings reports the split
    clock.advance(seconds=30)
    engine.tick_now()
    status, _headers, _content, body = _call(api, "GET", "/api/v1/savings?range=day")
    assert status == 200
    assert {"arbitrage_kwh", "self_consumption_kwh", "cycles"} <= set(body["totals"])

    # 0.17.1: Stop only neutralises hardware this run really wrote to.  An override forces a real
    # write first (the plan slot for this fixture can legitimately be 0 W), and is cleared before
    # the stop so the response is still the plain "no override" shape.
    engine.set_override(500.0, 5.0)
    engine.tick_now()
    assert engine.pending_holds() == ["example_battery_1"], "the add-on must really command"
    engine.clear_override()
    assert ha.service_calls[-1][2]["value"] == 500.0
    ha.states["number.example_battery_power"] = {"state": "450", "attributes": {}}
    status, _headers, _content, body = _call(api, "POST", "/api/v1/stop")
    assert status == 200
    assert body["mode"] == "advisory"
    assert body["override_cleared"] is False
    assert body["stopped"][0]["dry_run"] is False and body["stopped"][0]["applied_w"] == 0.0
    assert ha.service_calls[-1][2]["value"] == 0.0
    assert engine.snapshot().mode == Mode.ADVISORY.value
    rows = store.dispatch_log(limit=5)
    assert any(row["applied_w"] == 0.0 for row in rows)


def _call(api: ApiServer, method: str, target: str, body=None):
    import json

    payload = json.dumps(body).encode() if body is not None else b""
    status, headers, content = api.dispatch(method, target, payload)
    parsed = None
    if headers.get("Content-Type", "").startswith("application/json") and content:
        parsed = json.loads(content)
    return status, headers, content, parsed


def test_zendure_battery_resolves_the_manager_at_runtime(wired):
    """appendix A end to end: registry resolution, manual dispatch, negated raw setpoint."""
    engine, settings, store, ha, clock = wired
    registry_rows = [
        {
            "entity_id": "number.example_manager_manual_power",
            "platform": "zendure_ha",
            "translation_key": "manual_power",
            "device_id": "example_manager",
            "name": "Manual power",
        },
        {
            "entity_id": "select.example_manager_operation",
            "platform": "zendure_ha",
            "translation_key": "operation",
            "device_id": "example_manager",
            "name": "Operation",
        },
        {
            "entity_id": "sensor.example_device_electric_level",
            "platform": "zendure_ha",
            "translation_key": "electric_level",
            "device_id": "example_device_1",
            "name": "Electric level",
        },
        {
            "entity_id": "sensor.example_device_grid_input",
            "platform": "zendure_ha",
            "translation_key": "grid_input_power",
            "device_id": "example_device_1",
            "name": "Grid input power",
        },
        {
            "entity_id": "sensor.example_device_output_home",
            "platform": "zendure_ha",
            "translation_key": "output_home_power",
            "device_id": "example_device_1",
            "name": "Output home power",
        },
    ]
    engine._registry_source = lambda: list(registry_rows)  # noqa: SLF001
    engine._discover_cache = None  # noqa: SLF001
    ha.states["sensor.example_device_electric_level"] = {
        "state": "50",
        "attributes": {"unit_of_measurement": "%"},
    }
    ha.states["sensor.example_device_grid_input"] = {"state": "0", "attributes": {}}
    ha.states["sensor.example_device_output_home"] = {"state": "0", "attributes": {}}
    ha.states["number.example_manager_manual_power"] = {"state": "0", "attributes": {}}
    ha.states["select.example_manager_operation"] = {"state": "smart", "attributes": {}}

    # the user only confirms the SoC sensor; the manager roles come from the entity registry
    created = engine.create_battery(
        {
            "id": "example_zendure",
            "name": "Example Zendure",
            "driver": "zendure",
            "capacity_kwh": 2.4,
            "mapping": {"soc_entity": "sensor.example_device_electric_level"},
        }
    )
    assert created["mapping"] == {"soc_entity": "sensor.example_device_electric_level"}
    view = next(entry for entry in engine.batteries_view() if entry["config"]["id"] == "example_zendure")
    assert view["capabilities"]["control_path"] == "manager_manual_power"
    assert view["capabilities"]["entities"]["manual_power_entity"] == "number.example_manager_manual_power"

    # emulate the device: the measured pair follows the (sign-inverted) manager setpoint
    base_get = ha.get_state

    def device(entity_id):
        raw = float(ha.states["number.example_manager_manual_power"]["state"])
        if entity_id == "sensor.example_device_grid_input":
            return {"state": str(max(0.0, -raw)), "attributes": {}}  # charging
        if entity_id == "sensor.example_device_output_home":
            return {"state": str(max(0.0, raw)), "attributes": {}}  # discharging
        return base_get(entity_id)

    ha.get_state = device

    # dispatch it directly: +800 W charge must write raw -800 (appendix A)
    driver = engine._drivers["example_zendure"]  # noqa: SLF001
    result = driver.apply(800.0, dry_run=False)
    assert result.ok, result.message
    assert ha.states["number.example_manager_manual_power"]["state"] == "-800.0"
    assert ha.states["select.example_manager_operation"]["state"] == "manual"
    engine.delete_battery("example_zendure")


def test_off_mode_serves_the_api_without_a_current_plan(wired):
    """SPEC 1/10: `off` is "no planning, no writes" - the API keeps answering, plan-free."""
    engine, settings, store, ha, clock = wired
    assert engine.plan_now("integration") is not None
    engine.set_mode(Mode.OFF.value)
    assert engine.active_plan() is None  # nothing stale shown as current

    api = ApiServer(engine, host="127.0.0.1", port=0)
    status, _headers, _content, body = _call(api, "GET", "/api/v1/plan")
    assert status == 200 and body["generated_at"] is None and body["slots"] == []
    assert body["infeasible_reason"] == "no_plan"
    status, _headers, _content, body = _call(api, "GET", "/api/v1/status")
    assert status == 200 and body["active_plan"] is None and body["mode"] == Mode.OFF.value
    status, _headers, _content, _body = _call(api, "GET", "/api/v1/health")
    assert status == 200
    status, _headers, _content, _body = _call(api, "GET", "/api/v1/forecast?kind=price")
    assert status == 200

    # a tick in off mode still samples and serves, it just does not plan
    engine.tick_now()
    assert engine.active_plan() is None
    status, _headers, _content, body = _call(api, "GET", "/api/v1/batteries")
    assert status == 200 and body["batteries"]


def test_build_sources_exposes_the_energy_dashboard_grid_mapping(tmp_path):
    """SPEC 3: the Energy-dashboard fallback is built with the sources and handed to the engine."""
    from app.grid import GridMapping

    store = build_store(str(tmp_path))
    settings = build_settings(store, {}, str(tmp_path / "options.json"))
    sources = build_sources(settings, store, _FakeHA(), location=(52.0, 5.0, "UTC"))
    assert isinstance(sources.get("grid_mapping"), GridMapping)
    engine = Engine(settings, store=store, ha=_FakeHA(), **sources)
    assert engine._grid_mapping is sources["grid_mapping"]  # noqa: SLF001 - wiring assertion
    store.close()
