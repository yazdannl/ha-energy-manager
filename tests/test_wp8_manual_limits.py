"""Manual override limits (0.20.0, SPEC 5/9.3/10): per-direction caps.

Resolution is pure; the engine tests prove that a cap lowers the *effective* config (plan,
dispatcher, safety and status) without touching the stored battery config, that a manual force is
applied at the limit, that the existing floor/cell-voltage rails still win, and that the limits
persist in the settings store exactly like ``mode``/``dry_run``.  No live HA, no secrets.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "energy_manager"))

from app.api import ApiServer  # noqa: E402
from app.config import Settings  # noqa: E402
from app.engine import Engine  # noqa: E402
from app.main import (  # noqa: E402
    EXAMPLE_BATTERY,
    _FakeHA,
    _FakeStore,
    _selfcheck_settings,
    _selfcheck_sources,
)
from app.models import (  # noqa: E402
    Mode,
    clean_manual_limits,
    resolve_manual_limit,
)
from app.store import Store  # noqa: E402

T0 = datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now=None):
        self.now = now or T0

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now = self.now + timedelta(**kwargs)
        return self.now


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    def send(self, event, title, message, **kwargs):
        self.sent.append((event, title, message))
        return True

    def test(self):
        return True


def build_engine(store=None, engine_settings=None, **overrides):
    """The selfcheck fakes (or injected real ``Settings``/``Store``) with a deterministic clock."""
    settings = engine_settings if engine_settings is not None else _selfcheck_settings()
    if engine_settings is None:
        settings["control.grid_follow"] = False
    for key, value in overrides.items():
        settings.set(key, value)
    store = store if store is not None else _FakeStore()
    ha = _FakeHA()
    clock = Clock()
    engine = Engine(
        settings,
        store=store,
        ha=ha,
        notifier=FakeNotifier(),
        clock=clock,
        version="0.1.0",
        commit="example",
        **_selfcheck_sources(),
    )
    return engine, settings, store, ha, clock


def api_call(api: ApiServer, method: str, target: str, body=None):
    payload = json.dumps(body).encode() if body is not None else b""
    status, headers, content = api.dispatch(method, target, payload)
    parsed = None
    if headers.get("Content-Type", "").startswith("application/json") and content:
        parsed = json.loads(content)
    return status, headers, parsed


def service_writes(ha):
    return [call for call in ha.service_calls if call[0] in ("number", "input_number")]


# ------------------------------------------------------------------ resolution (pure)


def test_resolve_manual_limit_per_battery_then_fleet_and_never_raises_a_maximum():
    limits = {
        "example_battery_1": {"charge_w": 250.0},
        "*": {"charge_w": 400.0, "discharge_w": 0.0},
    }
    # the battery's own entry wins over the fleet entry
    assert resolve_manual_limit(limits, "example_battery_1", "charge", 1000.0) == 250.0
    # a fleet entry applies where the battery has none; 0 blocks that direction
    assert resolve_manual_limit(limits, "example_battery_1", "discharge", 1000.0) == 0.0
    assert resolve_manual_limit(limits, "example_battery_2", "discharge", 1000.0) == 0.0
    # ...and is capped by that battery's own configured maximum
    assert resolve_manual_limit(limits, "example_battery_2", "charge", 300.0) == 300.0
    assert resolve_manual_limit(limits, "example_battery_2", "charge", 1000.0) == 400.0
    # no entry at all = unlimited, never a guessed value
    assert resolve_manual_limit({}, "example_battery_1", "charge", 1000.0) is None
    assert resolve_manual_limit(None, "example_battery_1", "charge", 1000.0) is None
    assert resolve_manual_limit({"*": {"charge_w": 700.0}}, "example_battery_1", "discharge", 1000.0) is None
    # a hand-edited store cannot raise the configured maximum
    assert resolve_manual_limit({"*": {"charge_w": 5000.0}}, "example_battery_1", "charge", 800.0) == 800.0


def test_clean_manual_limits_drops_junk_and_keeps_real_caps():
    cleaned = clean_manual_limits(
        {
            "example_battery_1": {"charge_w": 250, "discharge_w": None},
            "example_junk": {"charge_w": -5.0, "discharge_w": "not a number"},
            "example_nan": {"charge_w": float("nan")},
            "example_empty": {},
            "*": {"discharge_w": 0.0},
        }
    )
    assert cleaned == {"example_battery_1": {"charge_w": 250.0}, "*": {"discharge_w": 0.0}}
    assert clean_manual_limits(None) == {} and clean_manual_limits("junk") == {}


# ------------------------------------------------------------- engine set/clear/effective


def test_set_manual_limits_caps_the_effective_config_not_the_stored_one():
    engine, _settings, _store, _ha, _clock = build_engine()
    assert engine.manual_limits() == {}
    engine.set_manual_limits("example_battery_1", discharge_w=200.0)
    stored = engine.battery_configs()[0]
    # the stored config keeps the requested maxima: the cap lives in the effective copy
    assert stored.max_discharge_w == 1000.0 and stored.max_charge_w == 1000.0
    effective = engine.effective_config(stored)
    assert effective.max_discharge_w == 200.0 and effective.max_charge_w == 1000.0
    # the fleet entry caps a battery that has no own entry, still without raising anything
    engine.set_manual_limits(None, charge_w=400.0)
    effective = engine.effective_config(engine.battery_configs()[0])
    assert effective.max_charge_w == 400.0 and effective.max_discharge_w == 200.0
    # a PATCH-style update of the battery's own maximum is stored untouched while the cap applies
    _settings.update_battery("example_battery_1", {"max_charge_w": 900.0})
    stored = engine.battery_configs()[0]
    assert stored.max_charge_w == 900.0
    assert engine.effective_config(stored).max_charge_w == 400.0
    # clearing restores the configured maxima
    engine.clear_manual_limits("example_battery_1")
    assert engine.manual_limits() == {"*": {"charge_w": 400.0}}
    engine.clear_manual_limits()
    effective = engine.effective_config(engine.battery_configs()[0])
    assert effective.max_charge_w == 900.0 and effective.max_discharge_w == 1000.0


def test_set_manual_limits_rejects_above_the_maximum_unknown_ids_and_negatives():
    engine, _settings, store, _ha, _clock = build_engine()
    with pytest.raises(ValueError) as charge_error:
        engine.set_manual_limits("example_battery_1", charge_w=1001.0)
    assert "1000" in str(charge_error.value)  # the message names the configured maximum
    with pytest.raises(ValueError):
        engine.set_manual_limits("example_battery_1", discharge_w=1001.0)
    with pytest.raises(ValueError):
        engine.set_manual_limits("example_battery_1", discharge_w=-1.0)
    with pytest.raises(KeyError):
        engine.set_manual_limits("example_nope", charge_w=100.0)
    with pytest.raises(KeyError):
        engine.clear_manual_limits("example_nope")
    # a rejected write changes nothing (no half-applied limit)
    assert engine.manual_limits() == {}
    assert not [event for event in store.events if event["code"] == "limits_set"]
    # a partial update sets one direction and leaves the other alone; null clears a direction
    engine.set_manual_limits("example_battery_1", charge_w=250.0)
    engine.set_manual_limits("example_battery_1", discharge_w=0.0)
    engine.set_manual_limits("example_battery_1", charge_w=None)
    assert engine.manual_limits() == {"example_battery_1": {"discharge_w": 0.0}}
    assert any(event["code"] == "limits_set" for event in store.events)


# ------------------------------------------------------------------ persistence


def test_manual_limits_persist_in_the_settings_store_across_a_reload(tmp_path):
    store = Store(tmp_path / "sem.db")
    try:
        settings = Settings(store, options={})
        settings.set("control.grid_follow", False)
        settings.add_battery(EXAMPLE_BATTERY)
        engine = Engine(
            settings,
            store=store,
            ha=_FakeHA(),
            notifier=FakeNotifier(),
            clock=Clock(),
            version="0.1.0",
            commit="example",
            **_selfcheck_sources(),
        )
        engine.set_manual_limits("example_battery_1", discharge_w=200.0)
        engine.set_manual_limits(None, charge_w=500.0)
        expected = {"example_battery_1": {"discharge_w": 200.0}, "*": {"charge_w": 500.0}}
        # exactly like mode/dry_run: the settings store holds the mapping under its own key
        assert store.get_setting("manual_limits") == expected
        assert any(event["code"] == "limits_set" for event in store.events(limit=50))

        # a restart (fresh Settings over the same store) comes back with the limits in force
        reloaded = Engine(
            Settings(store, options={}),
            store=store,
            ha=_FakeHA(),
            notifier=FakeNotifier(),
            clock=Clock(),
            version="0.1.0",
            commit="example",
            **_selfcheck_sources(),
        )
        assert reloaded.manual_limits() == expected
        assert reloaded.effective_config(reloaded.battery_configs()[0]).max_discharge_w == 200.0

        # clearing one entry restores that battery's configured maxima and keeps the fleet entry
        reloaded.clear_manual_limits("example_battery_1")
        assert reloaded.manual_limits() == {"*": {"charge_w": 500.0}}
        assert reloaded.effective_config(reloaded.battery_configs()[0]).max_discharge_w == 1000.0
        assert store.get_setting("manual_limits") == {"*": {"charge_w": 500.0}}

        # clearing everything restores every configured maximum
        reloaded.clear_manual_limits()
        assert reloaded.manual_limits() == {} and store.get_setting("manual_limits") == {}
        effective = reloaded.effective_config(reloaded.battery_configs()[0])
        assert effective.max_charge_w == 1000.0 and effective.max_discharge_w == 1000.0
        assert any(event["code"] == "limits_cleared" for event in store.events(limit=50))
    finally:
        store.close()


def test_a_hand_edited_limit_never_widens_a_direction_on_load():
    engine, settings, _store, _ha, _clock = build_engine()
    settings["manual_limits"] = {"*": {"charge_w": 5000.0, "discharge_w": -3.0}}
    reloaded, *_ = build_engine(engine_settings=settings)
    assert reloaded.manual_limits() == {"*": {"charge_w": 5000.0}}
    effective = reloaded.effective_config(reloaded.battery_configs()[0])
    assert effective.max_charge_w == 1000.0  # clamped to the configured maximum
    assert effective.max_discharge_w == 1000.0  # the negative junk is dropped, not applied


# ------------------------------------------------------------------ planner


def test_the_planner_never_exceeds_a_discharge_cap():
    engine, _settings, _store, _ha, _clock = build_engine()
    uncapped = engine.plan_now("test")
    assert uncapped is not None and uncapped.slots
    assert min(slot.power_w for slot in uncapped.slots) < -200.0  # the cap really bites
    engine.set_manual_limits("example_battery_1", discharge_w=200.0)
    capped = engine.plan_now("test")
    assert capped is not None and capped.slots
    assert all(slot.power_w >= -200.0 - 1e-6 for slot in capped.slots)
    # the schedule still covers the whole horizon
    assert capped.horizon_start == uncapped.horizon_start
    assert capped.horizon_end == uncapped.horizon_end


def test_the_planner_never_exceeds_a_charge_cap():
    engine, _settings, _store, _ha, _clock = build_engine()
    uncapped = engine.plan_now("test")
    assert uncapped is not None and max(slot.power_w for slot in uncapped.slots) > 200.0
    engine.set_manual_limits("example_battery_1", charge_w=200.0)
    capped = engine.plan_now("test")
    assert capped is not None and capped.slots
    assert all(slot.power_w <= 200.0 + 1e-6 for slot in capped.slots)
    assert capped.horizon_end == uncapped.horizon_end


# ------------------------------------------------------------------ dispatch / safety


def test_a_manual_force_above_the_limit_is_applied_at_the_limit():
    engine, _settings, _store, ha, _clock = build_engine()
    engine.set_manual_limits("example_battery_1", charge_w=300.0)
    engine.set_mode(Mode.AUTO.value, False)
    engine.tick_now()  # read the battery, dispatch the plan
    engine.set_override(700.0, 10.0)
    outcome = engine.tick_now()
    writes = service_writes(ha)
    assert writes and writes[-1][2]["value"] == 300.0
    assert outcome.allowed_w <= 300.0 + 0.5


def test_a_force_discharge_pin_at_the_soc_floor_still_yields_zero():
    engine, _settings, _store, ha, _clock = build_engine()
    engine.set_mode(Mode.AUTO.value, False)
    ha.states["sensor.example_battery_soc"] = {"state": "10", "attributes": {"unit_of_measurement": "%"}}
    engine.tick_now()  # read the battery at its floor
    engine.set_override(-500.0, 10.0)
    outcome = engine.tick_now()
    assert outcome.allowed_w == 0.0
    assert all(float(call[2].get("value") or 0.0) >= 0.0 for call in service_writes(ha))


def test_a_blocked_direction_is_never_commanded():
    engine, _settings, _store, ha, _clock = build_engine()
    engine.set_manual_limits("example_battery_1", discharge_w=0.0)
    engine.set_mode(Mode.AUTO.value, False)
    engine.tick_now()
    engine.set_override(-600.0, 10.0)
    outcome = engine.tick_now()
    assert outcome.allowed_w >= 0.0
    assert all(float(call[2].get("value") or 0.0) >= 0.0 for call in service_writes(ha))


# ------------------------------------------------------------------ API


def test_a_battery_patch_is_stored_untouched_while_the_cap_keeps_applying():
    """SPEC 5/10: the PATCH path writes the requested maximum; the manual cap lives on top of it."""
    engine, _settings, _store, _ha, _clock = build_engine()
    engine.tick_now()
    api = ApiServer(engine)
    api_call(api, "POST", "/api/v1/limits", {"battery_id": "example_battery_1", "charge_w": 250})
    status, _headers, body = api_call(
        api, "PATCH", "/api/v1/batteries/example_battery_1", {"patch": {"max_charge_w": 900}}
    )
    assert status == 200 and body["config"]["max_charge_w"] == 900.0
    status, _headers, body = api_call(api, "GET", "/api/v1/batteries")
    assert status == 200 and body["batteries"][0]["config"]["max_charge_w"] == 900.0
    status, _headers, body = api_call(api, "GET", "/api/v1/status")
    row = body["batteries"][0]
    assert row["max_charge_w"] == 900.0 and row["cap_charge_w"] == 250.0
    assert engine.effective_config(engine.battery_configs()[0]).max_charge_w == 250.0


def test_limits_endpoints_round_trip_and_status_carries_the_resolved_caps():
    engine, _settings, _store, _ha, _clock = build_engine()
    engine.tick_now()  # one read so /status has a battery row
    api = ApiServer(engine)

    status, _headers, body = api_call(
        api, "POST", "/api/v1/limits", {"battery_id": "example_battery_1", "charge_w": 250}
    )
    assert status == 200 and body["manual_limits"] == {"example_battery_1": {"charge_w": 250.0}}

    # a missing/null battery_id writes the fleet entry; a partial update leaves the rest alone
    status, _headers, body = api_call(api, "POST", "/api/v1/limits", {"charge_w": 400.0, "discharge_w": 0.0})
    assert status == 200 and body["manual_limits"]["*"] == {"charge_w": 400.0, "discharge_w": 0.0}
    status, _headers, body = api_call(
        api, "POST", "/api/v1/limits", {"battery_id": "example_battery_1", "charge_w": None}
    )
    assert status == 200 and body["manual_limits"] == {"*": {"charge_w": 400.0, "discharge_w": 0.0}}

    # above the configured maximum: 400 invalid_body naming it; unknown battery: 404
    status, _headers, body = api_call(
        api, "POST", "/api/v1/limits", {"battery_id": "example_battery_1", "charge_w": 5000}
    )
    assert status == 400 and body["error"]["code"] == "invalid_body" and "1000" in body["error"]["message"]
    status, _headers, body = api_call(api, "POST", "/api/v1/limits", {"battery_id": "example_nope", "charge_w": 100})
    assert status == 404 and body["error"]["code"] == "not_found"
    status, _headers, body = api_call(api, "POST", "/api/v1/limits", {"charge_w": "lots"})
    assert status == 400 and body["error"]["code"] == "invalid_body"

    status, _headers, body = api_call(api, "GET", "/api/v1/status")
    assert status == 200 and body["manual_limits"] == {"*": {"charge_w": 400.0, "discharge_w": 0.0}}
    row = body["batteries"][0]
    assert row["max_charge_w"] == 1000.0 and row["max_discharge_w"] == 1000.0
    assert row["cap_charge_w"] == 400.0 and row["cap_discharge_w"] == 0.0

    # DELETE one entry, then everything
    api_call(api, "POST", "/api/v1/limits", {"battery_id": "example_battery_1", "charge_w": 250})
    status, _headers, body = api_call(api, "DELETE", "/api/v1/limits?battery_id=example_battery_1")
    assert status == 200 and body["manual_limits"] == {"*": {"charge_w": 400.0, "discharge_w": 0.0}}
    status, _headers, body = api_call(api, "DELETE", "/api/v1/limits")
    assert status == 200 and body["manual_limits"] == {}
    status, _headers, body = api_call(api, "GET", "/api/v1/status")
    assert body["manual_limits"] == {} and body["batteries"][0]["cap_charge_w"] is None
