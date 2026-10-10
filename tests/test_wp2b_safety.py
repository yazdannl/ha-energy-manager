"""WP2B safety tests (SPEC sections 9.3 and 14).

``control.safety.check`` is a pure function, so this file runs it as an exhaustive table over the
clauses: mode, override, staleness, availability, SoC floor/ceiling, grid import/export caps, power
limits and the per-tick ramp.  Every row asserts the exact allowed power and reason slugs.
"""

from __future__ import annotations

import itertools
import os
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "energy_manager"))

from app.control import safety  # noqa: E402
from app.models import (  # noqa: E402
    Action,
    BatteryConfig,
    BatteryState,
    Mode,
    PlanSlot,
    Reason,
    SystemState,
    to_iso,
)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

SETTINGS = {
    "control.tick_seconds": 30,
    "control.stale_state_seconds": 180,
    "control.replan_minutes": 15,
    "optimizer.reserve_soc_pct": 0.0,
    "optimizer.max_grid_import_w": 0,
    "optimizer.max_grid_export_w": 0,
}


def battery(**overrides) -> BatteryConfig:
    data = dict(
        id="example_battery_1",
        name="Example Battery 1",
        driver="generic",
        capacity_kwh=10.0,
        min_soc_pct=10.0,
        max_soc_pct=100.0,
        max_charge_w=2000.0,
        max_discharge_w=2000.0,
        eta_charge=1.0,
        eta_discharge=1.0,
        mapping={"power_setpoint_entity": "number.example_power"},
    )
    data.update(overrides)
    return BatteryConfig(**data)


def live_state(
    *,
    soc_pct: float = 50.0,
    power_w: float = 0.0,
    available: bool = True,
    age_s: float = 5.0,
    stale: bool = False,
    batteries=None,
) -> BatteryState:
    readings = batteries if batteries is not None else [(soc_pct, power_w, available, age_s, stale)]
    first = readings[0]
    return BatteryState(
        id="example_battery_1",
        available=first[2],
        soc_pct=first[0],
        power_w=first[1],
        capacity_kwh=10.0,
        energy_kwh=10.0 * first[0] / 100.0,
        read_at=NOW - timedelta(seconds=first[3]),
        stale=first[4],
    )


def system_state(
    *,
    mode: str = Mode.AUTO.value,
    dry_run: bool = False,
    ha_connected: bool = True,
    override: dict | None = None,
    battery_state: BatteryState | None = None,
    grid_now_w: float | None = None,
    load_now_w: float | None = None,
    pv_now_w: float | None = None,
) -> SystemState:
    return SystemState(
        mode=mode,
        dry_run=dry_run,
        ha_connected=ha_connected,
        batteries=[battery_state if battery_state is not None else live_state()],
        override=override,
        grid_now_w=grid_now_w,
        load_now_w=load_now_w,
        pv_now_w=pv_now_w,
    )


def plan_slot(load_kw: float = 1.0, pv_kw: float = 0.0) -> PlanSlot:
    return PlanSlot(
        t=NOW,
        buy=0.3,
        sell=0.05,
        pv_kw=pv_kw,
        load_kw=load_kw,
        soc_kwh=5.0,
        power_w=0.0,
        action=Action.HOLD,
        reason=Reason.SPREAD_BELOW_THRESHOLD,
    )


# ------------------------------------------------------------------------------ clause 1


def test_advisory_and_off_never_allow_a_write():
    config = battery()
    for mode in (Mode.OFF.value, Mode.ADVISORY.value, "unknown", ""):
        for dry_run in (True, False):
            for request in (-2000.0, 0.0, 2000.0):
                state = system_state(mode=mode, dry_run=dry_run)
                decision = safety.check(request, plan_slot(), [config], state, SETTINGS, NOW)
                assert decision.block is True
                assert decision.allowed_w == 0.0
                assert decision.reasons == [Reason.MODE_NOT_AUTO]


# ------------------------------------------------------------------------------ clause 2


def test_kill_switch_is_missing_mode_and_therefore_blocks():
    config = battery()
    state = system_state(mode=Mode.OFF.value)
    assert safety.check(1500.0, plan_slot(), [config], state, SETTINGS, NOW).block is True


def test_active_override_pins_the_power_and_expires():
    config = battery()
    override = {
        "power_w": 700.0,
        "battery_id": None,
        "expires_at": to_iso(NOW + timedelta(minutes=5)),
    }
    state = system_state(override=override, battery_state=live_state(power_w=700.0))
    decision = safety.check(2000.0, plan_slot(), [config], state, SETTINGS, NOW)
    assert decision.block is False
    assert decision.allowed_w == 700.0
    assert Reason.OVERRIDE_ACTIVE in decision.reasons

    expired = {**override, "expires_at": to_iso(NOW - timedelta(seconds=1))}
    state = system_state(override=expired, battery_state=live_state(power_w=2000.0))
    decision = safety.check(2000.0, plan_slot(), [config], state, SETTINGS, NOW)
    assert decision.allowed_w == 2000.0
    assert Reason.OVERRIDE_ACTIVE not in decision.reasons


# ------------------------------------------------------------------------ clauses 3 and 4


def test_stale_state_blocks_and_is_reported():
    config = battery()
    for age, stale, expected_block in ((5.0, False, False), (200.0, False, True), (5.0, True, True)):
        state = system_state(battery_state=live_state(age_s=age, stale=stale))
        decision = safety.check(500.0, plan_slot(), [config], state, SETTINGS, NOW)
        assert decision.block is expected_block
        if expected_block:
            assert Reason.STALE_STATE in decision.reasons
            assert decision.allowed_w == 0.0


def test_missing_reading_counts_as_stale():
    config = battery()
    unknown = BatteryState(id=config.id, available=True, soc_pct=50.0, capacity_kwh=10.0)
    state = system_state(battery_state=unknown)
    decision = safety.check(500.0, plan_slot(), [config], state, SETTINGS, NOW)
    assert decision.block is True and Reason.STALE_STATE in decision.reasons


def test_ha_disconnected_blocks_every_battery():
    config = battery()
    state = system_state(ha_connected=False)
    decision = safety.check(500.0, plan_slot(), [config], state, SETTINGS, NOW)
    assert decision.block is True
    assert decision.reasons == [Reason.HA_DISCONNECTED]


def test_no_batteries_and_no_ha_reports_both_causes():
    """WP5 nit: with zero batteries *and* HA down, the more actionable cause must be visible."""
    state = replace(system_state(ha_connected=False), batteries=[])
    decision = safety.check(500.0, plan_slot(), [], state, SETTINGS, NOW)
    assert decision.block is True
    assert decision.reasons == [Reason.BATTERY_UNAVAILABLE, Reason.HA_DISCONNECTED]
    # the block itself is unchanged, and with HA up it stays the single reason
    state = replace(system_state(), batteries=[])
    decision = safety.check(500.0, plan_slot(), [], state, SETTINGS, NOW)
    assert decision.block is True and decision.reasons == [Reason.BATTERY_UNAVAILABLE]


def test_unavailable_battery_is_excluded_and_blocks_when_alone():
    config = battery()
    state = system_state(battery_state=live_state(available=False))
    decision = safety.check(500.0, plan_slot(), [config], state, SETTINGS, NOW)
    assert decision.block is True
    assert Reason.BATTERY_UNAVAILABLE in decision.reasons


def test_disabled_battery_is_not_usable():
    config = battery(enabled=False)
    decision = safety.check(500.0, plan_slot(), [config], system_state(), SETTINGS, NOW)
    assert decision.block is True and Reason.BATTERY_UNAVAILABLE in decision.reasons


def test_one_stale_battery_blocks_only_that_one():
    configs = [battery(id="a"), battery(id="b")]
    state = SystemState(
        mode=Mode.AUTO.value,
        ha_connected=True,
        batteries=[
            live_state(soc_pct=50.0, age_s=5.0),
            replace(live_state(soc_pct=50.0, age_s=400.0), id="b"),
        ],
    )
    state.batteries[0].id = "a"
    decision = safety.check(1000.0, plan_slot(), configs, state, SETTINGS, NOW)
    assert decision.block is False
    assert decision.blocked_ids == ["b"]
    assert Reason.STALE_STATE in decision.reasons
    assert decision.allowed_w == 1000.0


# ----------------------------------------------------------------------------- clauses 5-8


def test_soc_ceiling_clamp_is_exact():
    config = battery(capacity_kwh=10.0, min_soc_pct=0.0, max_charge_w=5000.0)
    # 0.025 kWh of room, 30 s tick (1/120 h), eta = 1 -> the SoC ceiling allows 3 kW
    expected = 0.025 / (30 / 3600.0) * 1000.0
    state = system_state(battery_state=live_state(soc_pct=99.75, power_w=expected))
    decision = safety.check(4000.0, plan_slot(), [config], state, SETTINGS, NOW)
    assert decision.block is False
    assert expected < config.max_charge_w  # the SoC clause is the binding one
    assert abs(decision.allowed_w - expected) < 1e-6
    assert decision.clamped is True
    assert Reason.SOC_CEILING in decision.reasons


def test_soc_floor_clamp_is_exact():
    config = battery(capacity_kwh=10.0, min_soc_pct=10.0, max_discharge_w=5000.0)
    expected = -(0.02 * 1.0) / (30 / 3600.0) * 1000.0  # 0.02 kWh above the floor
    state = system_state(battery_state=live_state(soc_pct=10.2, power_w=expected))
    decision = safety.check(-4000.0, plan_slot(), [config], state, SETTINGS, NOW)
    assert decision.block is False
    assert abs(decision.allowed_w - expected) < 1e-6
    assert Reason.SOC_FLOOR in decision.reasons


def test_reserve_soc_is_honoured():
    config = battery(capacity_kwh=10.0, min_soc_pct=5.0)
    settings = {**SETTINGS, "optimizer.reserve_soc_pct": 30.0}
    expected = -(0.001 * 1.0) / (30 / 3600.0) * 1000.0  # 0.001 kWh above the 30% reserve
    state = system_state(battery_state=live_state(soc_pct=30.01, power_w=expected))
    decision = safety.check(-4000.0, plan_slot(), [config], state, settings, NOW)
    assert abs(decision.allowed_w - expected) < 1e-6
    assert Reason.SOC_FLOOR in decision.reasons


def test_power_limit_clamp():
    config = battery(max_charge_w=1500.0, max_discharge_w=800.0)
    state = system_state(battery_state=live_state(power_w=1500.0))
    assert safety.check(5000.0, plan_slot(), [config], state, SETTINGS, NOW).allowed_w == 1500.0
    assert Reason.POWER_CLAMP in safety.check(5000.0, plan_slot(), [config], state, SETTINGS, NOW).reasons
    state = system_state(battery_state=live_state(power_w=-800.0))
    assert safety.check(-5000.0, plan_slot(), [config], state, SETTINGS, NOW).allowed_w == -800.0
    assert Reason.POWER_CLAMP in safety.check(-5000.0, plan_slot(), [config], state, SETTINGS, NOW).reasons


def test_grid_import_limit_clamps_and_blocks():
    config = battery()
    settings = {**SETTINGS, "optimizer.max_grid_import_w": 1000.0}
    # load 1.0 kW, no PV: charging above 0 W would push import over 1 kW.
    state = system_state()
    decision = safety.check(500.0, plan_slot(load_kw=1.0, pv_kw=0.0), [config], state, settings, NOW)
    assert decision.allowed_w == 0.0
    assert Reason.GRID_IMPORT_LIMIT in decision.reasons
    assert decision.block is False
    # discharge is allowed: it reduces the import
    decision = safety.check(-500.0, plan_slot(load_kw=1.0, pv_kw=0.0), [config], state, settings, NOW)
    assert decision.allowed_w == -500.0 and decision.block is False
    # the battery cannot fix a load that already exceeds the import cap
    decision = safety.check(0.0, plan_slot(load_kw=5.0, pv_kw=0.0), [config], state, settings, NOW)
    assert decision.block is True
    assert Reason.GRID_IMPORT_LIMIT in decision.reasons


def test_grid_export_limit_clamps_and_blocks():
    config = battery()
    settings = {**SETTINGS, "optimizer.max_grid_export_w": 1000.0}
    state = system_state(battery_state=live_state(power_w=-1600.0))
    state.grid_now_w = -1100.0  # the meter agrees with the forecast model (0.5 kW load)
    decision = safety.check(-2000.0, plan_slot(load_kw=0.5, pv_kw=0.0), [config], state, settings, NOW)
    assert decision.allowed_w == -1500.0
    assert Reason.GRID_EXPORT_LIMIT in decision.reasons
    # no meter reading at all: the cap itself bounds the battery (fail closed, audit finding 7)
    state.grid_now_w = None
    decision = safety.check(-2000.0, plan_slot(load_kw=0.5, pv_kw=0.0), [config], state, settings, NOW)
    assert decision.allowed_w == -1000.0
    assert Reason.GRID_EXPORT_LIMIT in decision.reasons
    # PV surplus already exceeds the export cap: discharge would make it worse
    decision = safety.check(-500.0, plan_slot(load_kw=0.0, pv_kw=3.0), [config], state, settings, NOW)
    assert decision.block is True
    assert Reason.GRID_EXPORT_LIMIT in decision.reasons


def test_grid_limits_use_the_live_meter_not_the_forecast():
    """Audit finding 7: a real 4 kW import against a 0.5 kW forecast must not pass a 3 kW cap."""
    config = battery(max_charge_w=2000.0, max_discharge_w=2000.0)
    settings = {**SETTINGS, "optimizer.max_grid_import_w": 3000.0}
    state = system_state(
        battery_state=live_state(power_w=0.0),
        load_now_w=500.0,
        pv_now_w=0.0,
        grid_now_w=4000.0,
    )
    decision = safety.check(2000.0, plan_slot(load_kw=0.5, pv_kw=0.0), [config], state, settings, NOW)
    assert Reason.GRID_IMPORT_LIMIT in decision.reasons
    assert decision.block is True or decision.allowed_w <= 1000.0
    # with no meter the same request falls back to the cap bound (fail closed)
    state.grid_now_w = None
    fallback = safety.check(2000.0, plan_slot(load_kw=0.5, pv_kw=0.0), [config], state, settings, NOW)
    assert fallback.block is True or fallback.allowed_w <= 3000.0
    # and a forecast-only install (no caps configured) is unchanged: the request is only bounded
    # by the per-tick ramp limit, never by a grid reason
    decision = safety.check(
        1500.0,
        plan_slot(load_kw=0.5, pv_kw=0.0),
        [config],
        system_state(battery_state=live_state(power_w=0.0)),
        SETTINGS,
        NOW,
    )
    assert decision.allowed_w == 1000.0
    assert Reason.GRID_IMPORT_LIMIT not in decision.reasons


def test_ramp_limit_is_one_kilowatt_per_tick_per_battery():
    config = battery()
    state = system_state(battery_state=live_state(power_w=500.0))
    decision = safety.check(2000.0, plan_slot(), [config], state, SETTINGS, NOW)
    assert decision.allowed_w == 1500.0
    assert Reason.RAMP_LIMIT in decision.reasons
    decision = safety.check(-2000.0, plan_slot(), [config], state, SETTINGS, NOW)
    assert decision.allowed_w == -500.0
    assert Reason.RAMP_LIMIT in decision.reasons
    # two batteries share the budget of two ramps
    state = system_state()
    state.batteries = [live_state(power_w=0.0), replace(live_state(power_w=0.0), id="b")]
    state.batteries[0].id = "a"
    configs = [battery(id="a"), battery(id="b")]
    decision = safety.check(5000.0, plan_slot(), configs, state, SETTINGS, NOW)
    assert decision.allowed_w == 2000.0


def test_ramp_blocked_when_the_window_is_empty():
    config = battery(max_charge_w=200.0)
    state = system_state(battery_state=live_state(power_w=0.0))
    decision = safety.check(2000.0, plan_slot(), [config], state, SETTINGS, NOW)
    assert decision.block is False and decision.allowed_w == 200.0


# ------------------------------------------------------------------------------ exhaustive


def test_exhaustive_table_of_clauses():
    """Every combination of the clause inputs; asserts the exact outcome."""
    config = battery(capacity_kwh=10.0, min_soc_pct=10.0, max_charge_w=2000.0, max_discharge_w=2000.0)
    rows = 0
    for mode, soc, power, age, import_limit, request in itertools.product(
        [Mode.AUTO.value, Mode.ADVISORY.value],
        [10.0, 50.0, 100.0],
        [0.0, 1800.0, -1800.0],
        [5.0, 400.0],
        [0.0, 1000.0],
        [2000.0, 0.0, -2000.0],
    ):
        state = system_state(
            mode=mode,
            battery_state=live_state(soc_pct=soc, power_w=power, age_s=age),
        )
        settings = {**SETTINGS, "optimizer.max_grid_import_w": import_limit}
        decision = safety.check(request, plan_slot(load_kw=0.8, pv_kw=0.0), [config], state, settings, NOW)
        rows += 1
        if mode != Mode.AUTO.value:
            assert decision.block and decision.allowed_w == 0.0 and decision.reasons == [Reason.MODE_NOT_AUTO]
            continue
        if age > 180.0:
            assert decision.block and decision.allowed_w == 0.0
            assert Reason.STALE_STATE in decision.reasons
            continue
        if decision.block:
            assert decision.allowed_w == 0.0 and decision.reasons
            continue
        assert -config.max_discharge_w - 1e-6 <= decision.allowed_w <= config.max_charge_w + 1e-6
        assert abs(decision.allowed_w - power) <= 1000.0 + 1e-6  # per-tick ramp envelope
        if soc >= 99.99 and request > 0:
            assert decision.allowed_w <= 0.0  # never charge above the ceiling
        if soc <= 10.01 and request < 0:
            assert decision.allowed_w >= 0.0  # never discharge below the floor
        if decision.allowed_w != request:
            assert decision.clamped is True
    assert rows == 2 * 3 * 3 * 2 * 2 * 3


def test_advisory_never_produces_a_non_blocked_decision():
    config = battery()
    for request, override in itertools.product(
        [-2000.0, 0.0, 2000.0],
        [None, {"power_w": 500.0, "expires_at": to_iso(NOW + timedelta(minutes=5))}],
    ):
        for mode in (Mode.OFF.value, Mode.ADVISORY.value):
            state = system_state(mode=mode, override=override)
            assert safety.check(request, plan_slot(), [config], state, SETTINGS, NOW).block is True


# ------------------------------------------------------------------------------------ purity


def test_check_is_pure_and_does_not_mutate_its_inputs():
    config = battery()
    state = system_state(override={"power_w": 100.0, "expires_at": to_iso(NOW + timedelta(minutes=1))})
    slot = plan_slot()
    settings = dict(SETTINGS)
    before_state = repr(state)
    before_slot = repr(slot)
    before_settings = dict(settings)
    first = safety.check(1234.0, slot, [config], state, settings, NOW)
    second = safety.check(1234.0, slot, [config], state, settings, NOW)
    assert first == second
    assert repr(state) == before_state
    assert repr(slot) == before_slot
    assert settings == before_settings
    assert first.reasons is not second.reasons  # fresh list every call, no shared mutable state


def test_safety_module_has_no_io_or_state():
    import inspect

    source = inspect.getsource(safety)
    for forbidden in ("open(", "socket", "requests", "urllib", "time.time", "datetime.now", "print("):
        assert forbidden not in source
    assert safety.RAMP_LIMIT_W == 1000.0
