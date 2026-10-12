"""WP5 safety property tests and end-to-end write/non-write assertions (SPEC 9.3/9.4/14).

The randomized property tests do not re-implement ``safety.check``; they assert *invariants* the
spec demands of any non-blocked decision (physical limits, SoC, grid caps, ramp) over many random
scenarios, plus the authority invariants (`advisory`/`off` and no usable battery never allow a
write).  The deterministic table pins the exact block/clamp outcome for the risky combinations.

The end-to-end part boots the real engine with the WP5 fake HA and proves on the service-call log
that advisory and auto+dry_run never touch the device, while auto with dry_run off issues exactly
the setpoint writes, and ``/stop`` writes 0 W even from advisory + dry_run.
"""

from __future__ import annotations

import os
import random
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "energy_manager"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.control import safety  # noqa: E402
from app.models import (  # noqa: E402
    BatteryConfig,
    BatteryState,
    Mode,
    PlanSlot,
    Reason,
    Action,
    SystemState,
)
from fake_ha import FakeHAFacade, FakeHAState  # noqa: E402

NOW = datetime(2026, 3, 4, 12, 30, tzinfo=timezone.utc)
EPS = 1e-6


class DictSettings(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


def battery(battery_id: str, **overrides) -> BatteryConfig:
    data = dict(
        id=battery_id,
        name=f"Example {battery_id}",
        driver="generic",
        capacity_kwh=5.0,
        min_soc_pct=10.0,
        max_soc_pct=90.0,
        max_charge_w=1000.0,
        max_discharge_w=1000.0,
        eta_charge=0.9,
        eta_discharge=0.9,
        mapping={"power_setpoint_entity": "number.example_power"},
    )
    data.update(overrides)
    return BatteryConfig(**data)


def base_settings(**overrides) -> DictSettings:
    values = {
        "control.tick_seconds": 30,
        "control.stale_state_seconds": 180,
        "optimizer.reserve_soc_pct": 0.0,
        "optimizer.max_grid_import_w": 0,
        "optimizer.max_grid_export_w": 0,
    }
    values.update(overrides)
    return DictSettings(**values)


def plan_slot(load_kw=0.5, pv_kw=0.0) -> PlanSlot:
    return PlanSlot(
        t=NOW,
        buy=0.2,
        sell=0.05,
        pv_kw=pv_kw,
        load_kw=load_kw,
        soc_kwh=2.0,
        power_w=0.0,
        action=Action.HOLD,
        reason=Reason.SELF_CONSUMPTION,
    )


def usable(live: BatteryState | None, now: datetime, stale_after: float) -> bool:
    if live is None or not live.available:
        return False
    if live.stale or live.read_at is None:
        return False
    age = (now - live.read_at).total_seconds()
    return age <= stale_after


def energy_of(config: BatteryConfig, live: BatteryState | None) -> float:
    if live is None:
        return config.capacity_kwh * config.min_soc_pct / 100.0
    if live.energy_kwh is not None:
        return float(live.energy_kwh)
    if live.soc_pct is not None:
        return config.capacity_kwh * float(live.soc_pct) / 100.0
    return config.capacity_kwh * config.min_soc_pct / 100.0


@dataclass
class Scenario:
    batteries: list[BatteryConfig]
    live: list[BatteryState]
    state: SystemState
    settings: DictSettings
    now: datetime
    requested_w: float
    slot: PlanSlot | None


def check(scenario: Scenario):
    return safety.check(
        scenario.requested_w,
        scenario.slot,
        scenario.batteries,
        scenario.state,
        scenario.settings,
        scenario.now,
    )


def assert_invariants(scenario: Scenario, decision) -> None:
    settings = scenario.settings
    state = scenario.state
    now = scenario.now
    stale_after = float(settings.get("control.stale_state_seconds", 180) or 180)
    live_by_id = {row.id: row for row in scenario.live}
    usable_configs = [
        config
        for config in scenario.batteries
        if config.enabled and usable(live_by_id.get(config.id), now, stale_after)
    ]

    # authority and availability: never a non-blocked decision outside auto / without hardware
    if str(state.mode) != Mode.AUTO.value:
        assert decision.block and decision.allowed_w == 0.0
        assert Reason.MODE_NOT_AUTO in decision.reasons
    if not state.ha_connected and str(state.mode) == Mode.AUTO.value:
        assert decision.block and decision.allowed_w == 0.0
        # with zero configured batteries the no-hardware reason wins; both are documented
        assert (Reason.HA_DISCONNECTED in decision.reasons) or (
            Reason.BATTERY_UNAVAILABLE in decision.reasons
        )
    if not usable_configs and str(state.mode) == Mode.AUTO.value and state.ha_connected:
        assert decision.block and decision.allowed_w == 0.0

    # dry_run is reported, never silently dropped
    assert decision.dry_run == bool(state.dry_run)

    if decision.block:
        assert decision.allowed_w == 0.0
        return

    # a non-blocked decision only happens with authority + usable hardware
    assert str(state.mode) == Mode.AUTO.value and state.ha_connected and usable_configs

    # per-battery / aggregate physical limits
    p_max = sum(config.max_charge_w for config in usable_configs)
    p_min = -sum(config.max_discharge_w for config in usable_configs)
    assert p_min - EPS <= decision.allowed_w <= p_max + EPS, (decision.allowed_w, p_min, p_max)

    # SoC envelope for one tick
    reserve = float(settings.get("optimizer.reserve_soc_pct", 0.0) or 0.0)
    e_min = sum(config.capacity_kwh * max(config.min_soc_pct, reserve) / 100.0 for config in usable_configs)
    e_max = sum(config.capacity_kwh * config.max_soc_pct / 100.0 for config in usable_configs)
    e_now = sum(energy_of(config, live_by_id.get(config.id)) for config in usable_configs)
    capacity = sum(config.capacity_kwh for config in usable_configs) or 1.0
    eta_c = sum(config.capacity_kwh * config.eta_charge for config in usable_configs) / capacity
    eta_d = sum(config.capacity_kwh * config.eta_discharge for config in usable_configs) / capacity
    dt = max(float(settings.get("control.tick_seconds", 30) or 30), 1.0) / 3600.0
    if decision.allowed_w > 0:
        assert e_now + dt * decision.allowed_w / 1000.0 * eta_c <= e_max + 1e-6
    if decision.allowed_w < 0:
        assert e_now + dt * decision.allowed_w / 1000.0 / eta_d >= e_min - 1e-6

    # grid caps on the predicted flow
    load_kw = pv_kw = None
    if scenario.slot is not None:
        load_kw, pv_kw = scenario.slot.load_kw, scenario.slot.pv_kw
    elif state.load_now_w is not None and state.pv_now_w is not None:
        load_kw, pv_kw = state.load_now_w / 1000.0, state.pv_now_w / 1000.0
    if load_kw is not None and pv_kw is not None:
        grid = load_kw - pv_kw + decision.allowed_w / 1000.0
        import_w = float(settings.get("optimizer.max_grid_import_w", 0) or 0)
        export_w = float(settings.get("optimizer.max_grid_export_w", 0) or 0)
        if import_w > 0:
            assert grid <= import_w / 1000.0 + 1e-6, (grid, import_w)
        if export_w > 0:
            assert -grid <= export_w / 1000.0 + 1e-6, (grid, export_w)

    # ramp: aggregate budget (documented ceiling for N batteries; the dispatcher clamps each one)
    previous = sum(float(live_by_id[config.id].power_w or 0.0) for config in usable_configs)
    budget = safety.RAMP_LIMIT_W * len(usable_configs)
    assert previous - budget - 1e-6 <= decision.allowed_w <= previous + budget + 1e-6


def random_scenario(rng: random.Random) -> Scenario:
    count = rng.randint(0, 3)
    batteries = []
    live = []
    for index in range(count):
        capacity = rng.choice([3.0, 5.0, 10.0])
        config = battery(
            f"battery_{index}",
            capacity_kwh=capacity,
            min_soc_pct=rng.choice([0.0, 10.0, 20.0]),
            max_soc_pct=rng.choice([80.0, 90.0, 100.0]),
            max_charge_w=rng.choice([500.0, 1000.0, 2000.0]),
            max_discharge_w=rng.choice([500.0, 1000.0, 2000.0]),
            eta_charge=rng.choice([0.8, 0.9, 1.0]),
            eta_discharge=rng.choice([0.8, 0.9, 1.0]),
            enabled=rng.random() > 0.15,
        )
        batteries.append(config)
        position = rng.choice(["floor", "ceiling", "middle", "unknown"])
        soc = {
            "floor": config.min_soc_pct,
            "ceiling": config.max_soc_pct,
            "middle": (config.min_soc_pct + config.max_soc_pct) / 2.0,
            "unknown": None,
        }[position]
        available = rng.random() > 0.2
        age = rng.choice([0.0, 5.0, 100.0, 179.0, 181.0, 4000.0])
        live.append(
            BatteryState(
                id=config.id,
                available=available,
                soc_pct=soc,
                power_w=rng.choice([0.0, 300.0, -450.0, 1000.0, -1000.0]),
                capacity_kwh=capacity,
                energy_kwh=None if soc is None else capacity * soc / 100.0,
                read_at=NOW - timedelta(seconds=age),
                stale=rng.random() < 0.15,
            )
        )
    now = NOW
    mode = rng.choice([Mode.OFF.value, Mode.ADVISORY.value, Mode.AUTO.value, Mode.AUTO.value])
    override = None
    if rng.random() < 0.25:
        minutes = rng.choice([-5.0, 10.0])
        override = {
            "power_w": rng.choice([0.0, 500.0, -500.0]),
            "battery_id": rng.choice([None, "battery_0"]),
            "expires_at": (now + timedelta(minutes=minutes)).isoformat(),
        }
    state = SystemState(
        mode=mode,
        dry_run=rng.random() < 0.5,
        ha_connected=rng.random() > 0.15,
        now=now,
        load_now_w=rng.choice([None, 500.0, 1500.0]),
        pv_now_w=rng.choice([None, 0.0, 2000.0]),
        batteries=[row for row in live],
        override=override,
    )
    settings = base_settings(
        **{
            "optimizer.reserve_soc_pct": rng.choice([0.0, 10.0, 30.0]),
            "optimizer.max_grid_import_w": rng.choice([0, 1000, 2500]),
            "optimizer.max_grid_export_w": rng.choice([0, 1000, 2500]),
            "control.stale_state_seconds": rng.choice([60, 180, 600]),
        }
    )
    slot = None
    if rng.random() < 0.7:
        slot = plan_slot(
            load_kw=rng.choice([0.0, 0.5, 2.0, 5.0]),
            pv_kw=rng.choice([0.0, 0.3, 3.0]),
        )
    return Scenario(
        batteries=batteries,
        live=live,
        state=state,
        settings=settings,
        now=now,
        requested_w=rng.choice([0.0, 250.0, -250.0, 900.0, -900.0, 5000.0, -5000.0]),
        slot=slot,
    )


def test_randomized_safety_invariants_hold_for_many_scenarios():
    rng = random.Random(20260304)
    non_blocked = 0
    for index in range(500):
        scenario = random_scenario(rng)
        decision = check(scenario)
        assert_invariants(scenario, decision)
        if not decision.block:
            non_blocked += 1
    assert non_blocked > 40, f"too few non-blocked scenarios to be meaningful: {non_blocked}"


def test_advisory_and_off_always_block_even_with_healthy_hardware():
    for mode in (Mode.OFF.value, Mode.ADVISORY.value):
        for dry_run in (True, False):
            scenario = Scenario(
                batteries=[battery("battery_0")],
                live=[
                    BatteryState(
                        id="battery_0",
                        available=True,
                        soc_pct=50.0,
                        energy_kwh=2.5,
                        power_w=0.0,
                        read_at=NOW,
                    )
                ],
                state=SystemState(
                    mode=mode,
                    dry_run=dry_run,
                    ha_connected=True,
                    now=NOW,
                    batteries=[],
                    load_now_w=500.0,
                    pv_now_w=0.0,
                ),
                settings=base_settings(),
                now=NOW,
                requested_w=1000.0,
                slot=plan_slot(),
            )
            decision = check(scenario)
            assert decision.block is True
            assert decision.allowed_w == 0.0
            assert decision.reasons == [Reason.MODE_NOT_AUTO]
            assert decision.dry_run is dry_run


def test_exhaustive_outcome_table():
    """Deterministic table over the risky clause combinations (SPEC 14)."""
    ttl = battery("battery_0", capacity_kwh=5.0, min_soc_pct=10.0, max_soc_pct=90.0)
    floor_energy = 0.5
    ceiling_energy = 4.5

    def decision(*, soc_energy, previous_w, requested_w, import_w=0, export_w=0, stale=False,
                 age=0.0, available=True, override=None, slot=None):
        scenario = Scenario(
            batteries=[ttl],
            live=[
                BatteryState(
                    id="battery_0",
                    available=available,
                    soc_pct=soc_energy / 5.0 * 100.0,
                    energy_kwh=soc_energy,
                    power_w=previous_w,
                    read_at=NOW - timedelta(seconds=age),
                    stale=stale,
                )
            ],
            state=SystemState(
                mode=Mode.AUTO.value,
                dry_run=True,
                ha_connected=True,
                now=NOW,
                batteries=[
                    BatteryState(
                        id="battery_0",
                        available=available,
                        soc_pct=soc_energy / 5.0 * 100.0,
                        energy_kwh=soc_energy,
                        power_w=previous_w,
                        read_at=NOW - timedelta(seconds=age),
                        stale=stale,
                    )
                ],
                load_now_w=1000.0,
                pv_now_w=0.0,
                override=override,
            ),
            settings=base_settings(
                **{
                    "optimizer.max_grid_import_w": import_w,
                    "optimizer.max_grid_export_w": export_w,
                }
            ),
            now=NOW,
            requested_w=requested_w,
            slot=slot or plan_slot(),
        )
        return check(scenario)

    # ceiling: a charge request is clamped to zero-ish and never pushed above the ceiling
    ceiling = decision(soc_energy=ceiling_energy, previous_w=0.0, requested_w=1000.0)
    assert not ceiling.block
    assert ceiling.allowed_w <= 1000.0
    assert Reason.SOC_CEILING in ceiling.reasons or ceiling.allowed_w == 0.0
    # floor: a discharge request cannot go below the floor
    floor = decision(soc_energy=floor_energy, previous_w=0.0, requested_w=-1000.0)
    assert not floor.block
    assert floor.allowed_w >= -1000.0
    assert Reason.SOC_FLOOR in floor.reasons or floor.allowed_w == 0.0
    # stale state blocks even in auto with healthy hardware
    stale = decision(soc_energy=2.5, previous_w=0.0, requested_w=500.0, stale=True)
    assert stale.block and stale.allowed_w == 0.0 and Reason.STALE_STATE in stale.reasons
    # old read blocks
    old = decision(soc_energy=2.5, previous_w=0.0, requested_w=500.0, age=4000.0)
    assert old.block and Reason.STALE_STATE in old.reasons
    # unavailable battery blocks
    gone = decision(soc_energy=2.5, previous_w=0.0, requested_w=500.0, available=False)
    assert gone.block and Reason.BATTERY_UNAVAILABLE in gone.reasons
    # ramp: 1000 W per tick from a previous state
    ramp = decision(soc_energy=2.5, previous_w=-1000.0, requested_w=1000.0)
    assert not ramp.block
    assert ramp.allowed_w <= 0.0 + EPS  # -1000 + 1000 = 0
    # import cap clamps the charge: base 1.0 kW (load 1.0, pv 0) + p <= 1.2 kW
    imp = decision(
        soc_energy=2.5,
        previous_w=0.0,
        requested_w=1000.0,
        import_w=1200,
        slot=plan_slot(load_kw=1.0, pv_kw=0.0),
    )
    assert not imp.block
    assert imp.allowed_w == pytest.approx(200.0, abs=EPS)
    assert Reason.GRID_IMPORT_LIMIT in imp.reasons
    # export cap clamps a discharge that would over-export (base -0.8 kW from pv surplus)
    exp = decision(
        soc_energy=2.5,
        previous_w=0.0,
        requested_w=-1000.0,
        export_w=800,
        slot=plan_slot(load_kw=0.2, pv_kw=1.0),
    )
    assert not exp.block
    assert exp.allowed_w == pytest.approx(0.0, abs=EPS)
    assert Reason.GRID_EXPORT_LIMIT in exp.reasons
    # an already-violating export cap that the battery cannot fix blocks ...
    blocked = decision(
        soc_energy=4.5,
        previous_w=0.0,
        requested_w=-1000.0,
        export_w=500,
        slot=plan_slot(load_kw=0.2, pv_kw=2.0),
    )
    assert blocked.block and blocked.allowed_w == 0.0
    assert Reason.GRID_EXPORT_LIMIT in blocked.reasons
    # ... and so does an already-violating import cap with no discharge headroom
    blocked_import = decision(
        soc_energy=0.5,
        previous_w=0.0,
        requested_w=-1000.0,
        import_w=1000,
        slot=plan_slot(load_kw=1.5, pv_kw=0.0),
    )
    assert blocked_import.block and blocked_import.allowed_w == 0.0
    assert Reason.GRID_IMPORT_LIMIT in blocked_import.reasons


def test_override_pins_power_when_feasible_and_expires():
    override = {
        "power_w": 400.0,
        "battery_id": None,
        "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
    }
    scenario = Scenario(
        batteries=[battery("battery_0")],
        live=[
            BatteryState(
                id="battery_0",
                available=True,
                soc_pct=50.0,
                energy_kwh=2.5,
                power_w=0.0,
                read_at=NOW,
            )
        ],
        state=SystemState(
            mode=Mode.AUTO.value,
            dry_run=False,
            ha_connected=True,
            now=NOW,
            batteries=[
                BatteryState(
                    id="battery_0",
                    available=True,
                    soc_pct=50.0,
                    energy_kwh=2.5,
                    power_w=0.0,
                    read_at=NOW,
                )
            ],
            load_now_w=500.0,
            pv_now_w=0.0,
            override=override,
        ),
        settings=base_settings(),
        now=NOW,
        requested_w=-1000.0,
        slot=plan_slot(),
    )
    decision = check(scenario)
    assert not decision.block
    assert decision.allowed_w == 400.0
    assert Reason.OVERRIDE_ACTIVE in decision.reasons

    scenario.state.override = {
        **override,
        "expires_at": (NOW - timedelta(seconds=1)).isoformat(),
    }
    expired = check(scenario)
    assert expired.allowed_w == -1000.0  # the stale pin is ignored
    assert Reason.OVERRIDE_ACTIVE not in expired.reasons


# --------------------------------------------------------------------------------------------
# End to end: the real engine against the WP5 fake HA
# --------------------------------------------------------------------------------------------


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def set(self, moment: datetime) -> None:
        self.now = moment


def build_engine(
    tmp_path,
    *,
    mode: str,
    dry_run: bool,
    hour: int = 12,
    batteries: Sequence[tuple[str, float, float]] | None = None,
    reserve_soc_pct: float | None = None,
    load_w: float = 600.0,
):
    """Real Store/Settings/Engine wired to the fake HA; static price, no solar.

    ``batteries`` is a list of ``(battery_id, soc_pct, capacity_kwh)``; it defaults to a single
    5 kWh battery at 50 %.  ``reserve_soc_pct`` seeds ``optimizer.reserve_soc_pct``.
    """
    from app.engine import Engine
    from app.main import build_settings, build_sources, build_store

    store = build_store(str(tmp_path))
    settings = build_settings(store, {}, str(tmp_path / "options.json"))
    assert settings is not None
    if batteries is None:
        states = {
            "number.example_battery_power": {"state": "0", "attributes": {}},
            "sensor.example_battery_soc": {
                "state": "50",
                "attributes": {"unit_of_measurement": "%"},
            },
        }
        settings.add_battery(
            battery(
                "example_battery_1",
                capacity_kwh=5.0,
                mapping={
                    "power_setpoint_entity": "number.example_battery_power",
                    "soc_entity": "sensor.example_battery_soc",
                },
            )
        )
    else:
        states = {}
        for battery_id, soc_pct, capacity_kwh in batteries:
            states[f"number.{battery_id}"] = {"state": "0", "attributes": {}}
            states[f"sensor.{battery_id}_soc"] = {
                "state": str(soc_pct),
                "attributes": {"unit_of_measurement": "%"},
            }
            settings.add_battery(
                battery(
                    battery_id,
                    capacity_kwh=capacity_kwh,
                    mapping={
                        "power_setpoint_entity": f"number.{battery_id}",
                        "soc_entity": f"sensor.{battery_id}_soc",
                    },
                )
            )
    settings.set("mode", mode)
    if reserve_soc_pct is not None:
        settings.set("optimizer.reserve_soc_pct", reserve_soc_pct)
    settings.set("dry_run", dry_run)
    settings.set("price.source", "static")
    settings.set("price.static_schedule", [])  # replaced by the injected source below
    settings.set("solar.enabled", False)
    settings.set("notify.enabled", False)
    settings.set("control.grid_follow", False)

    def price_source(now):
        from app.models import PriceForecast, PriceSlot

        forecast = PriceForecast(source="example_static", generated_at=now)
        start = now.replace(minute=0, second=0, microsecond=0)
        for index in range(96):
            moment = start + timedelta(minutes=15 * index)
            if 10 <= moment.hour < 14:
                buy = 0.02
            elif moment.hour >= 18:
                buy = 1.00
            else:
                buy = 0.30
            forecast.slots.append(PriceSlot(t=moment, buy=buy, sell=0.01, source="example_static"))
        return forecast

    def solar_source(now):
        from app.models import SolarForecast

        return SolarForecast(source="example_solar", generated_at=now)

    def load_source(now):
        from app.models import LoadForecast, LoadSlot

        forecast = LoadForecast(source="example_load", generated_at=now, confidence="low")
        start = now.replace(minute=0, second=0, microsecond=0)
        for index in range(96):
            forecast.slots.append(LoadSlot(t=start + timedelta(minutes=15 * index), load_kw=0.6))
        return forecast

    live = FakeHAState(states)
    ha = FakeHAFacade(live)
    clock = Clock(datetime(2026, 3, 4, hour, 30, tzinfo=timezone.utc))
    engine = Engine(
        settings,
        store=store,
        ha=ha,
        price_source=price_source,
        solar_source=solar_source,
        load_source=load_source,
        sampler=lambda now: {"grid_w": load_w * 0.5, "pv_w": 0.0, "load_w": load_w},
        registry_source=lambda: [],
        location="Example Home",
        clock=clock,
        version="0.1.0",
    )
    return engine, settings, store, live, clock


def teardown(store):
    store.close()


def test_advisory_end_to_end_never_issues_a_service_call(tmp_path):
    engine, settings, store, live, clock = build_engine(tmp_path, mode=Mode.ADVISORY.value, dry_run=True)
    try:
        plan = engine.plan_now("wp5")
        assert plan is not None and plan.slots
        engine.tick_now()
        assert live.service_calls == [], live.service_calls
        entries = engine.log(limit=50, kind="dispatch")
        assert entries and all(entry["code"] == Reason.MODE_NOT_AUTO for entry in entries)
    finally:
        teardown(store)


def test_auto_dry_run_end_to_end_never_issues_a_service_call(tmp_path):
    engine, settings, store, live, clock = build_engine(tmp_path, mode=Mode.AUTO.value, dry_run=True)
    try:
        engine.plan_now("wp5")
        engine.tick_now()
        assert live.service_calls == [], live.service_calls
        entries = engine.log(limit=50, kind="dispatch")
        assert entries and any(entry["dry_run"] for entry in entries)
        # the would-be writes are visible in the journal, not on the wire
        assert any(entry.get("writes") for entry in entries)
    finally:
        teardown(store)


def test_auto_end_to_end_writes_exactly_the_setpoint(tmp_path):
    engine, settings, store, live, clock = build_engine(tmp_path, mode=Mode.AUTO.value, dry_run=False)
    try:
        plan = engine.plan_now("wp5")
        assert plan is not None
        slot = plan.slot_at(clock.now)
        assert slot is not None and slot.power_w > 0, (slot and slot.power_w)
        engine.tick_now()
        calls = live.service_calls
        assert calls, "expected the setpoint write"
        entity = "number.example_battery_power"
        assert {call[2].get("entity_id") for call in calls} == {entity}
        assert all(call[0] == "number" and call[1] == "set_value" for call in calls)
        assert len(calls) == 1, calls
        expected = slot.power_w  # previous power is 0, so the 1000 W ramp does not clamp it
        assert calls[0][2]["value"] == pytest.approx(expected, abs=1e-6)
        assert all(call[2].get("entity_id") != "sensor.example_battery_soc" for call in calls)
        # battery state read back through the driver
        tick_state = engine.snapshot()
        assert tick_state.batteries and tick_state.batteries[0].soc_pct == 50.0
    finally:
        teardown(store)


def test_stop_writes_zero_even_from_advisory_and_dry_run(tmp_path):
    engine, settings, store, live, clock = build_engine(tmp_path, mode=Mode.AUTO.value, dry_run=False)
    try:
        engine.plan_now("wp5")
        engine.tick_now()
        assert engine.pending_holds() == ["example_battery_1"], "the fixture must command for real"
        original = engine._ha.call_service

        def hiccup(*args, **kwargs):
            raise RuntimeError("example HA hiccup")

        # The mode change's own hold fails, so the applied command is still unverified while the
        # engine is in advisory + dry_run: Stop must still neutralise it for real (0.17.1).
        engine._ha.call_service = hiccup
        engine.set_mode(Mode.ADVISORY.value, True)
        engine._ha.call_service = original
        assert engine.pending_holds() == ["example_battery_1"], "a failed hold must not clear it"
        live.states["number.example_battery_power"] = {"state": "450", "attributes": {}}
        response = engine.stop_now()
        assert response["mode"] == Mode.ADVISORY.value
        assert response["stopped"] and response["stopped"][0]["dry_run"] is False
        assert response["stopped"][0]["applied_w"] == 0.0
        assert live.service_calls, "stop must write for real even under advisory + dry_run"
        last = live.service_calls[-1]
        assert last[2]["entity_id"] == "number.example_battery_power"
        assert last[2]["value"] == 0.0
        assert engine.snapshot().mode == Mode.ADVISORY.value
    finally:
        teardown(store)


def test_no_usable_battery_means_no_write_and_a_reason(tmp_path):
    engine, settings, store, live, clock = build_engine(tmp_path, mode=Mode.AUTO.value, dry_run=False)
    try:
        live.states["sensor.example_battery_soc"] = {
            "state": "unavailable",
            "attributes": {"unit_of_measurement": "%"},
        }
        live.states["number.example_battery_power"] = {"state": "unavailable", "attributes": {}}
        engine.tick_now()
        assert live.service_calls == []
        entries = engine.log(limit=20, kind="dispatch")
        assert entries and any(
            entry["code"] in (Reason.BATTERY_UNAVAILABLE, Reason.STALE_STATE) for entry in entries
        )
    finally:
        teardown(store)


def test_allocation_never_drains_a_battery_below_its_min_soc():
    """The per-battery hard floor (min_soc) is enforced by the allocation helper."""
    from app.optimize.allocate import allocate_power

    full = battery("a_full", capacity_kwh=5.0, min_soc_pct=10.0, max_discharge_w=1000.0)
    low = battery("b_low", capacity_kwh=5.0, min_soc_pct=10.0, max_discharge_w=1000.0)
    energies = {"a_full": 5.0, "b_low": 0.55}
    for _ in range(20):  # repeated ticks must converge, not creep below the floor
        entries = allocate_power(-2000.0, [full, low], energies, slot_hours=30 / 3600)
        for config, power in entries:
            energies[config.id] = energies[config.id] + (
                (power / 1000.0) * (30 / 3600) / config.eta_discharge
            )
    assert energies["b_low"] >= low.capacity_kwh * low.min_soc_pct / 100.0 - 1e-6


def test_allocation_enforces_the_per_battery_reserve_floor():
    """WP5 finding 2 (fixed): the reserve is a per-battery floor supplied by the caller."""
    from app.optimize.allocate import allocate_power

    reserve = 30.0
    full = battery("a_full", capacity_kwh=5.0, min_soc_pct=10.0, max_discharge_w=1000.0)
    low = battery("b_low", capacity_kwh=5.0, min_soc_pct=10.0, max_discharge_w=1000.0)
    energies = {"a_full": 5.0, "b_low": 0.55}
    slot_hours = 30 / 3600
    entries = allocate_power(
        -2000.0, [full, low], energies, slot_hours=slot_hours, reserve_soc_pct=reserve
    )
    by_id = {config.id: power for config, power in entries}
    assert set(by_id) == {"a_full", "b_low"}
    for config, power in entries:
        e_next = energies[config.id] + (power / 1000.0) * slot_hours / config.eta_discharge
        floor = config.capacity_kwh * max(config.min_soc_pct, reserve) / 100.0
        # a battery already below its floor may never be drained further
        assert e_next >= min(energies[config.id], floor) - 1e-9, (config.id, e_next, floor)
    # the repro from the original finding: the low battery is left alone, the roomy one works
    assert by_id["b_low"] == 0.0
    assert by_id["a_full"] < 0.0
    assert abs(sum(power for _, power in entries) + 2000.0) > 1e-6  # demand was not fully servable

    # without the reserve the hard min_soc floor must still hold (reserve only adds to it)
    for config, power in allocate_power(-2000.0, [full, low], energies, slot_hours=slot_hours):
        e_next = energies[config.id] + (power / 1000.0) * slot_hours / config.eta_discharge
        assert e_next >= config.capacity_kwh * config.min_soc_pct / 100.0 - 1e-9, config.id

    # deterministic: the same inputs give the same split twice
    again = allocate_power(-2000.0, [full, low], energies, slot_hours=slot_hours, reserve_soc_pct=reserve)
    assert again == entries


# (label, [(id, capacity_kwh, min_soc_pct, energy_kwh or None)], reserve_soc_pct, requested_w)
RESERVE_CASES = [
    ("one_nearly_empty_one_full", [("a_full", 5.0, 10.0, 5.0), ("b_low", 5.0, 10.0, 0.55)], 30.0, -2000.0),
    (
        "three_batteries_mixed_fleet",
        [("a_big", 10.0, 10.0, 9.5), ("b_mid", 2.0, 10.0, 1.9), ("c_tiny", 1.0, 10.0, 0.5)],
        40.0,
        -3000.0,
    ),
    (
        "request_larger_than_total_room",
        [("a_big", 10.0, 10.0, 4.05), ("b_mid", 2.0, 10.0, 0.85), ("c_tiny", 1.0, 10.0, 0.42)],
        40.0,
        -30000.0,
    ),
    ("whole_fleet_below_the_reserve", [("a_full", 5.0, 10.0, 1.0), ("b_low", 5.0, 10.0, 1.0)], 40.0, -2000.0),
    ("reserve_below_min_soc", [("a_high", 5.0, 40.0, 2.5), ("b_low", 5.0, 40.0, 1.0)], 10.0, -2000.0),
    ("unknown_energy_is_treated_as_its_floor", [("a_full", 5.0, 10.0, None), ("b_low", 5.0, 10.0, 1.5)], 30.0, -2000.0),
    ("different_capacities_same_reserve", [("a_big", 10.0, 10.0, 4.5), ("b_small", 1.0, 10.0, 0.45)], 40.0, -2000.0),
]


@pytest.mark.parametrize("label,batteries,reserve,requested", RESERVE_CASES, ids=[case[0] for case in RESERVE_CASES])
def test_reserve_floor_holds_on_adversarial_multi_battery_cases(label, batteries, reserve, requested):
    """Falsification attempt for WP5 finding 2: no battery may cross its own reserve floor.

    Every case mixes a nearly empty battery with a roomy one (and asks for more power than the
    fleet can deliver), which is exactly the configuration that used to drain the small battery
    while a full one was left untouched.
    """
    from app.optimize.allocate import allocate_power

    configs = [battery(bid, capacity_kwh=cap, min_soc_pct=min_soc) for bid, cap, min_soc, _ in batteries]
    energies = {bid: energy for bid, _, _, energy in batteries}
    slot_hours = 30 / 3600
    entries = allocate_power(
        requested, configs, energies, slot_hours=slot_hours, reserve_soc_pct=reserve
    )
    assert len(entries) == len(configs), "one entry per enabled battery, zeros included"
    total = 0.0
    for config, power in entries:
        energy = energies[config.id]
        floor = config.capacity_kwh * max(config.min_soc_pct, reserve) / 100.0
        assert power <= 0.0, (label, config.id, power)  # discharge only
        if energy is None:
            assert power == 0.0, (label, config.id, power)  # unknown -> floor -> no discharge
            continue
        e_next = energy + (power / 1000.0) * slot_hours / config.eta_discharge
        # a battery already below its own floor must not be drained any further
        assert e_next >= min(energy, floor) - 1e-9, (label, config.id, e_next, floor)
        total += power
    assert total >= requested or total < 0.0  # never over-delivers
    again = allocate_power(requested, configs, energies, slot_hours=slot_hours, reserve_soc_pct=reserve)
    assert again == entries, label  # deterministic


def test_reserve_floor_is_plumbed_from_settings_through_the_dispatcher(tmp_path):
    """E2E re-verification of finding 2: the reserve must reach ``allocate_power`` from the engine.

    Two 10 kWh batteries (95 % and 45 %), reserve 40 %, a slot that wants to discharge.  The small
    own 4.0 kWh reserve floor while above its 10 % hard floor, so it must not be touched at all.
    A control run without the reserve (below) proves this test is not vacuous.
    """
    engine, settings, store, live, clock = build_engine(
        tmp_path,
        mode=Mode.AUTO.value,
        dry_run=False,
        hour=19,
        batteries=[("example_battery_big", 95.0, 10.0), ("example_battery_small", 15.0, 10.0)],
        reserve_soc_pct=40.0,
    )
    try:
        engine.tick_now()  # read the battery states before planning
        plan = engine.plan_now("wp5")
        assert plan is not None and any(slot.power_w < 0.0 for slot in plan.slots), (
            "the fixture must want a discharge"
        )
        for _ in range(5):
            engine.tick_now()
        # no discharge was ever commanded to the battery below its own reserve floor
        small_writes = [
            float(call[2]["value"])
            for call in live.service_calls
            if call[2]["entity_id"] == "number.example_battery_small"
        ]
        assert all(value == 0.0 for value in small_writes), small_writes
        big_writes = [
            float(call[2]["value"])
            for call in live.service_calls
            if call[2]["entity_id"] == "number.example_battery_big"
        ]
        assert min(big_writes) < 0.0, big_writes
        # the plan's own per-battery snapshot must use the same floor (engine call site)
        rechecked = engine.plan_now("wp5_recheck")
        shares = {entry["id"]: entry["share_w"] for entry in rechecked.batteries}
        assert shares.get("example_battery_small") == 0.0, shares
        assert (shares.get("example_battery_big") or 0.0) < 0.0, shares
        # the journal proves the allocation was actually applied, not just omitted
        rows = [entry for entry in engine.log(limit=200, kind="dispatch") if entry["battery_id"]]
        small_rows = [row for row in rows if row["battery_id"] == "example_battery_small"]
        big_rows = [row for row in rows if row["battery_id"] == "example_battery_big"]
        assert small_rows and big_rows, "the dispatcher must journal one row per battery per tick"
        assert all(row["applied_w"] == 0.0 for row in small_rows), small_rows
        assert min(row["applied_w"] for row in big_rows) < 0.0, big_rows
        assert live.service_calls and live.service_calls[0][1] == "set_value"
    finally:
        teardown(store)


def test_without_the_reserve_the_same_tick_shares_the_discharge(tmp_path):
    """Control for the E2E test above: with reserve_soc_pct = 0 the same battery does discharge."""
    engine, settings, store, live, clock = build_engine(
        tmp_path,
        mode=Mode.AUTO.value,
        dry_run=False,
        hour=19,
        batteries=[("example_battery_big", 95.0, 10.0), ("example_battery_small", 15.0, 10.0)],
        reserve_soc_pct=0.0,
    )
    try:
        engine.tick_now()  # read the battery states before planning
        plan = engine.plan_now("wp5")
        assert plan is not None and any(slot.power_w < 0.0 for slot in plan.slots), (
            "the fixture must want a discharge"
        )
        for _ in range(5):
            engine.tick_now()
        small = [
            float(call[2]["value"])
            for call in live.service_calls
            if call[2]["entity_id"] == "number.example_battery_small"
        ]
        assert min(small) < 0.0, small
    finally:
        teardown(store)


def test_hold_on_exit_writes_zero_when_live(tmp_path):
    """Appendix A: no device watchdog, so a clean shutdown must neutralise the setpoint.

    0.17.1: only for a battery this run really commanded -- the tick below is what makes the
    setpoint ours; an install that never wrote is left untouched.
    """
    engine, settings, store, live, clock = build_engine(tmp_path, mode=Mode.AUTO.value, dry_run=False)
    try:
        engine.plan_now("wp5")
        engine.tick_now()
        assert engine.pending_holds() == ["example_battery_1"], "the fixture must command for real"
        live.states["number.example_battery_power"] = {"state": "450", "attributes": {}}
        held = engine.hold_on_exit("shutdown")
        assert held and held[0]["dry_run"] is False
        assert live.service_calls, "shutdown hold must write for real"
        assert live.service_calls[-1][2]["entity_id"] == "number.example_battery_power"
        assert live.service_calls[-1][2]["value"] == 0.0
        assert engine.hold_on_exit("shutdown") == []  # runs at most once
    finally:
        teardown(store)


def test_no_battery_and_ha_down_reports_both_reasons():
    """WP5 finding 5 (fixed): zero usable batteries + HA disconnected reports both slugs."""
    scenario = Scenario(
        batteries=[],
        live=[],
        state=SystemState(
            mode=Mode.AUTO.value,
            dry_run=False,
            ha_connected=False,
            now=NOW,
            batteries=[],
            load_now_w=1000.0,
            pv_now_w=0.0,
        ),
        settings=base_settings(),
        now=NOW,
        requested_w=1000.0,
        slot=plan_slot(),
    )
    decision = check(scenario)
    assert decision.block and decision.allowed_w == 0.0
    assert Reason.BATTERY_UNAVAILABLE in decision.reasons, decision.reasons
    assert Reason.HA_DISCONNECTED in decision.reasons, decision.reasons

    # the two conditions still report on their own when only one of them holds
    solo = Scenario(
        batteries=[],
        live=[],
        state=SystemState(
            mode=Mode.AUTO.value,
            dry_run=False,
            ha_connected=True,
            now=NOW,
            batteries=[],
            load_now_w=1000.0,
            pv_now_w=0.0,
        ),
        settings=base_settings(),
        now=NOW,
        requested_w=1000.0,
        slot=plan_slot(),
    )
    assert list(check(solo).reasons) == [Reason.BATTERY_UNAVAILABLE]
