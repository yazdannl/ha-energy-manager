"""WP2B per-battery round-trip efficiency tests (SPEC sections 5, 6 and 8.1).

Three things the amendment changed and these tests pin:

* ``SALVAGE`` values terminal stored energy at ``multiplier * median(buy) * eta_dis`` -- a stored
  kWh only ever delivers ``eta_dis`` AC kWh, so the undiscounted version over-charged the pack;
* an idle slot caused by the round-trip loss is labelled ``Reason.EFFICIENCY_LOSS``, which is a
  different reason from the user's own ``optimizer.min_spread_eur_per_kwh`` floor;
* the plan reports the efficiency it assumed (aggregate in the summary, per battery in
  ``batteries[]``) so the UI can explain every break-even it shows.

The maths itself lives in the frozen ``models.BatteryConfig`` helpers; these tests only ever call
them, never re-derive them.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "energy_manager"))

from app.api import ApiServer  # noqa: E402
from app.engine import Engine  # noqa: E402
from app.main import _FakeHA, _FakeStore, _selfcheck_settings, _selfcheck_sources  # noqa: E402
from app.models import Action, BatteryConfig, Mode, Plan, PlanSlot, Reason, to_iso  # noqa: E402
from app.optimize import dp  # noqa: E402
from app import efficiency as efficiency_learning  # noqa: E402
from app.sources.measure import MEASUREMENT_MINUTES  # noqa: E402

T0 = datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)

#: A 75 % round trip, split evenly (SPEC section 5).
ETA_75 = 0.75**0.5


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def battery(**overrides) -> BatteryConfig:
    data = dict(
        id="example_battery_1",
        name="Example Battery 1",
        driver="generic",
        capacity_kwh=2.0,
        min_soc_pct=0.0,
        max_soc_pct=100.0,
        max_charge_w=1000.0,
        max_discharge_w=1000.0,
        eta_charge=0.95,
        eta_discharge=0.95,
        mapping={"power_setpoint_entity": "number.example_power"},
    )
    data.update(overrides)
    return BatteryConfig(**data)


def make_slots(count, minutes=15, *, pv=0.0, load=0.0, buy=0.2, sell=0.05):
    """A flat horizon; ``buy``/``sell`` accept a per-slot list to make one slot special."""
    out = []
    for index in range(count):
        out.append(
            dp.SlotInput(
                t=T0 + timedelta(minutes=minutes * index),
                pv_kw=pv[index] if isinstance(pv, list) else pv,
                load_kw=load[index] if isinstance(load, list) else load,
                buy=buy[index] if isinstance(buy, list) else buy,
                sell=sell[index] if isinstance(sell, list) else sell,
            )
        )
    return out


def terminal_energy(plan, agg) -> float:
    """Energy left after the last slot (the plan reports the state at each slot's start)."""
    last = plan.slots[-1]
    return dp.next_energy_kwh(last.soc_kwh, last.power_w / 1000.0, plan.slot_minutes / 60.0, agg)


class Clock:
    def __init__(self, now=None):
        self.now = now or T0

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now = self.now + timedelta(**kwargs)
        return self.now


class FakeNotifier:
    def __init__(self, enabled=True):
        self.sent = []
        self.enabled = enabled

    def send(self, event, title, message, **kwargs):
        self.sent.append((event, title, message))
        return True

    def test(self):
        return True


class _NoJournal:
    """The dispatcher only writes to the journal while dispatching, never while configuring."""

    def dispatch(self, entry):
        raise AssertionError("not expected")

    def event(self, level, code, message, payload=None):
        raise AssertionError("not expected")


def build_engine(clock=None, **overrides):
    settings = _selfcheck_settings()
    settings["control.grid_follow"] = False
    for key, value in overrides.items():
        settings[key] = value
    store = _FakeStore()
    ha = _FakeHA()
    clock = clock or Clock()
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
    return engine, store, ha, clock, settings


def test_the_stale_plan_setting_is_documented_and_reaches_the_dispatcher():
    """WP2A ships ``control.stale_plan_seconds``; the dispatcher's bound must use it.

    The bound is ``max(2 x control.replan_minutes, control.stale_plan_seconds)`` (SPEC 8.1/9.1),
    so the documented default has to survive into the settings the dispatcher reads.
    """
    from app.config import DEFAULTS, setting_descriptors

    assert DEFAULTS.get("control.stale_plan_seconds") == 1800
    rows = {row["key"]: row for row in setting_descriptors()}
    assert rows["control.stale_plan_seconds"]["type"] == "int"
    assert rows["control.stale_plan_seconds"]["group"] == "control"

    from app.control.dispatcher import Dispatcher
    from app.models import Plan, PlanSlot

    settings = {
        "control.tick_seconds": 30,
        "control.replan_minutes": 15,
        "control.stale_plan_seconds": DEFAULTS["control.stale_plan_seconds"],
    }
    dispatcher = Dispatcher(settings, _NoJournal(), {})
    assert dispatcher.replan_interval_s() == 900.0

    moment = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    def plan_aged(minutes: int) -> Plan:
        start = moment - timedelta(minutes=minutes)
        return Plan(
            generated_at=start,
            horizon_start=start,
            horizon_end=start + timedelta(minutes=30),
            slot_minutes=15,
            slots=[
                PlanSlot(
                    t=start + timedelta(minutes=5),
                    buy=0.2,
                    sell=0.05,
                    pv_kw=0.0,
                    load_kw=0.5,
                    soc_kwh=1.0,
                    power_w=0.0,
                    action=Action.HOLD,
                    reason=Reason.SPREAD_BELOW_THRESHOLD,
                )
            ],
        )

    assert dispatcher._stale_input_reason(plan_aged(29), plan_aged(29).slots[0], moment) is None
    assert dispatcher._stale_input_reason(plan_aged(31), plan_aged(31).slots[0], moment) is not None


# --------------------------------------------------------------------------------------
# 1. salvage values stored energy at what it can actually deliver (SPEC 8.1)
# --------------------------------------------------------------------------------------


def test_salvage_is_discounted_by_the_discharge_efficiency():
    """The terminal decision flips once stored energy is valued at ``eta_dis`` EUR per stored kWh.

    A full 1 kWh pack, every slot buying at 0.20, and a final slot selling at 0.21 -- just above
    buy, but below the 0.2309 needed to pay back a 75 % round trip.  Discharging the pack earns
    ``eta_dis * 0.21 = 0.1819`` per stored kWh, which beats the discounted terminal value
    (``0.20 * 0.866 = 0.1732``) and loses to the undiscounted one (0.20).  Valuing stored energy at
    face value therefore kept the pack full for no reason; the plan must discharge instead.
    """
    config = battery(
        capacity_kwh=1.0,
        max_charge_w=4000.0,
        max_discharge_w=4000.0,
        eta_charge=ETA_75,
        eta_discharge=ETA_75,
    )
    agg = dp.BatteryAggregate.from_configs([config], 0.0)
    opts = dp.OptimizerOptions(
        slot_minutes=15, soc_step_kwh=0.05, power_steps=8, salvage_multiplier=1.0, allow_export=True
    )
    slots = make_slots(8, 15, pv=0.0, load=0.0, buy=0.20, sell=0.05)
    slots[-1] = replace(slots[-1], sell=0.21)
    plan = dp.solve(slots, aggregate=agg, start_kwh=1.0, options=opts, generated_at=T0)

    assert plan.slots[-1].power_w < 0.0, "the last slot must sell the pack, not hoard it"
    assert terminal_energy(plan, agg) == pytest.approx(0.0, abs=0.02), terminal_energy(plan, agg)

    # the objective is exactly the discounted one (grid + cycle - multiplier*median(buy)*eta_dis)
    expected_salvage = opts.salvage_multiplier * dp._median([s.buy for s in slots]) * agg.eta_discharge
    internal = dp._solve_once(slots, agg, 1.0, opts, dp.TARGET_HARD_PENALTY, None, expected_salvage)
    assert internal.objective_eur == pytest.approx(plan.summary.objective_eur, abs=1e-9)


def test_salvage_multiplier_zero_still_means_no_terminal_value():
    config = battery(
        capacity_kwh=1.0,
        max_charge_w=4000.0,
        max_discharge_w=4000.0,
        eta_charge=ETA_75,
        eta_discharge=ETA_75,
    )
    agg = dp.BatteryAggregate.from_configs([config], 0.0)
    opts = dp.OptimizerOptions(
        slot_minutes=15, soc_step_kwh=0.05, power_steps=8, salvage_multiplier=0.0, allow_export=True
    )
    slots = make_slots(8, 15, pv=0.0, load=0.0, buy=0.20, sell=0.05)
    slots[-1] = replace(slots[-1], sell=0.21)
    plan = dp.solve(slots, aggregate=agg, start_kwh=1.0, options=opts, generated_at=T0)
    assert plan.slots[-1].power_w < 0.0, "with no terminal value the pack is sold, not kept"


# --------------------------------------------------------------------------------------
# 2. break-even gate and the efficiency_loss reason
# --------------------------------------------------------------------------------------


def _round_trip_case(eta_charge, eta_discharge, **overrides):
    """A half-full pack, one cheap early slot, and one later slot that sells above that cheap buy.

    At a 75 % round trip the sale cannot pay the energy back (0.40/0.75 = 0.53 break-even for a
    0.42 sale), so the slot stays idle *and* the cheap slot is never charged; at 100 % the same
    prices make that cheap slot a real charge, which is the flip these tests pin.  The pack starts
    half full so the slot is genuinely undecided -- an empty pack is ``reserve_hold`` and a full
    one ``full``, which are different reasons.
    """
    config = battery(
        eta_charge=eta_charge,
        eta_discharge=eta_discharge,
        max_charge_w=4000.0,
        max_discharge_w=4000.0,
    )
    opts = dp.OptimizerOptions(
        slot_minutes=15,
        soc_step_kwh=0.05,
        power_steps=8,
        allow_export=True,
        **overrides,
    )
    # slot 0 is cheap enough to charge from; every other slot buys dearer than the pack is worth
    # at the end of the horizon, so only slot 0 is ever a candidate charge.
    buy = [0.45] * 12
    buy[0] = 0.40
    sell = [0.05] * 12
    sell[5] = 0.42  # above the cheap buy (looks profitable) but below 0.40 / 0.75 = 0.533
    slots = make_slots(12, 15, pv=0.0, load=0.0, buy=buy, sell=sell)
    return dp.solve(
        slots,
        aggregate=dp.BatteryAggregate.from_configs([config], 0.0),
        start_kwh=1.0,
        options=opts,
        generated_at=T0,
    )


def test_a_round_trip_loss_labels_the_idle_slot_efficiency_loss():
    plan = _round_trip_case(ETA_75, ETA_75)
    slot = plan.slots[5]
    assert slot.power_w == 0.0
    assert slot.action.value == "hold"
    assert slot.reason == Reason.EFFICIENCY_LOSS
    # the sale really is below the frozen helper's break-even for the cheapest charge
    config = battery(eta_charge=ETA_75, eta_discharge=ETA_75)
    assert slot.sell < config.break_even_sell_eur_per_kwh(0.40)
    # ...and a slot that is not even profitable before the losses keeps the plain label
    assert plan.slots[6].reason == Reason.SPREAD_BELOW_THRESHOLD


def test_the_same_slot_with_a_full_round_trip_is_not_an_efficiency_loss():
    plan = _round_trip_case(1.0, 1.0)
    slot = plan.slots[5]
    assert slot.reason != Reason.EFFICIENCY_LOSS, slot
    assert plan.summary.charged_kwh > 0.0, "a 100% round trip makes that cheap slot a real charge"


def test_the_user_min_spread_floor_keeps_its_own_reason():
    """The user's floor is a separate constraint, so it keeps SPREAD_BELOW_THRESHOLD."""
    plan = _round_trip_case(ETA_75, ETA_75, min_spread_eur_per_kwh=0.05)
    slot = plan.slots[5]
    assert slot.power_w == 0.0
    assert slot.reason == Reason.SPREAD_BELOW_THRESHOLD, slot
    assert slot.reason != Reason.EFFICIENCY_LOSS


def test_efficiency_loss_is_a_documented_plan_reason():
    reasons = {slot.reason for slot in _round_trip_case(ETA_75, ETA_75).slots}
    assert Reason.EFFICIENCY_LOSS in reasons


# --------------------------------------------------------------------------------------
# 3. the plan reports the efficiency it assumed
# --------------------------------------------------------------------------------------


def test_the_plan_summary_reports_the_assumed_efficiency():
    config = battery(eta_charge=0.90, eta_discharge=0.94)
    agg = dp.BatteryAggregate.from_configs([config], 0.0)
    opts = dp.OptimizerOptions(
        slot_minutes=15, soc_step_kwh=0.05, power_steps=8, wear_eur_per_kwh=0.02, allow_export=True
    )
    slots = make_slots(8, 15, pv=0.0, load=0.0, buy=[0.10, 0.30, 0.20, 0.40, 0.20, 0.30, 0.10, 0.40])
    plan = dp.solve(slots, aggregate=agg, start_kwh=1.0, options=opts, generated_at=T0)
    summary = plan.summary
    median_buy = dp._median([s.buy for s in slots])
    assert summary.eta_charge == pytest.approx(0.90)
    assert summary.eta_discharge == pytest.approx(0.94)
    assert summary.round_trip_efficiency == pytest.approx(0.90 * 0.94)
    assert summary.wear_eur_per_kwh == pytest.approx(0.02)
    assert summary.break_even_sell_eur_per_kwh == pytest.approx(
        config.break_even_sell_eur_per_kwh(median_buy, opts.wear_eur_per_kwh)
    )


def test_every_plan_battery_entry_carries_its_own_efficiency():
    engine, _store, _ha, clock, settings = build_engine()
    clock.advance(minutes=7)  # the battery is read before planning, so the plan is published
    plan = engine.plan_now("test")
    assert plan is not None and plan.batteries
    median_buy = dp._median([slot.buy for slot in plan.slots])
    override = float(settings.get("optimizer.cycle_cost_eur_per_kwh", 0.0) or 0.0)
    configs = {config.id: config for config in engine.battery_configs()}
    for entry in plan.batteries:
        config = configs[entry["id"]]
        wear = override if override > 0.0 else config.wear_eur_per_kwh
        assert entry["round_trip_efficiency"] == pytest.approx(config.round_trip_efficiency)
        assert entry["wear_eur_per_kwh"] == pytest.approx(wear)
        assert entry["break_even_sell_eur_per_kwh"] == pytest.approx(
            config.break_even_sell_eur_per_kwh(median_buy, wear)
        )


def test_the_wear_override_replaces_the_derived_wear_end_to_end():
    engine, _store, _ha, clock, _settings = build_engine(
        **{"optimizer.cycle_cost_eur_per_kwh": 0.03}
    )
    clock.advance(minutes=7)
    plan = engine.plan_now("test")
    assert plan is not None and plan.batteries
    assert plan.summary.wear_eur_per_kwh == pytest.approx(0.03)
    for entry in plan.batteries:
        assert entry["wear_eur_per_kwh"] == pytest.approx(0.03), "the override is what the card shows"


def test_the_api_plan_payload_carries_the_efficiency_keys():
    from app.api import plan_dict

    engine, _store, _ha, clock, _settings = build_engine()
    clock.advance(minutes=7)
    plan = engine.plan_now("test")
    payload = plan_dict(plan, clock.now)
    summary = payload["summary"]
    for field in ("eta_charge", "eta_discharge", "round_trip_efficiency", "wear_eur_per_kwh", "break_even_sell_eur_per_kwh"):
        assert field in summary, field
    assert 0 < summary["round_trip_efficiency"] <= 1
    assert summary["break_even_sell_eur_per_kwh"] is not None
    for entry in payload["batteries"]:
        assert "round_trip_efficiency" in entry
        assert "wear_eur_per_kwh" in entry
        assert "break_even_sell_eur_per_kwh" in entry
    # the existing shape is untouched
    assert {"id", "name", "share_w"} <= set(payload["batteries"][0])


# --------------------------------------------------------------------------------------
# 4. battery read-back goes through config.battery_payload()
# --------------------------------------------------------------------------------------


def test_batteries_read_back_exposes_the_derived_round_trip_pct():
    engine, _store, _ha, _clock, _settings = build_engine()
    views = engine.batteries_view()
    assert views
    for view in views:
        config = view["config"]
        assert "round_trip_efficiency_pct" in config, config.keys()
        assert config["round_trip_efficiency_pct"] == pytest.approx(
            config["eta_charge"] * config["eta_discharge"] * 100.0
        )
        # the stored fields are still there (additive only)
        assert config["eta_charge"] > 0 and config["eta_discharge"] > 0


def test_battery_crud_returns_the_derived_pct():
    engine, _store, _ha, _clock, _settings = build_engine()
    created = engine.create_battery(
        {
            "id": "example_battery_2",
            "name": "Example Battery 2",
            "driver": "generic",
            "capacity_kwh": 3.0,
            "min_soc_pct": 10.0,
            "max_soc_pct": 100.0,
            "max_charge_w": 1000.0,
            "max_discharge_w": 1000.0,
            "eta_charge": 0.9,
            "eta_discharge": 0.95,
            "mapping": {"power_setpoint_entity": "number.example_power"},
        }
    )
    assert created["round_trip_efficiency_pct"] == pytest.approx(0.9 * 0.95 * 100.0)
    updated = engine.update_battery("example_battery_2", {"eta_discharge": 0.9})
    assert updated["round_trip_efficiency_pct"] == pytest.approx(0.9 * 0.9 * 100.0)


# --------------------------------------------------------------------------------------
# 5. /config surfaces the add-on option conflicts
# --------------------------------------------------------------------------------------


class ConflictSettings(dict):
    """A settings object that reports one option conflict (like WP2A's Settings)."""

    def get(self, key, default=None):
        return super().get(key, default)

    def set(self, key, value):
        self[key] = value

    def all(self):
        return dict(self)

    def options(self):
        return {"mode": self.get("mode", "advisory"), "dry_run": self.get("dry_run", True)}

    def option_conflicts(self):
        return [
            {
                "key": "mode",
                "option": "auto",
                "stored": "advisory",
                "message": "add-on option mode='auto' is ignored: the stored setting mode='advisory' wins",
            }
        ]


class PlainSettings(dict):
    """A settings object without the helper at all (older fakes)."""

    def get(self, key, default=None):
        return super().get(key, default)

    def set(self, key, value):
        self[key] = value


def _config_engine(settings):
    class _Engine:
        def __init__(self):
            self.settings = settings

        def snapshot(self):
            raise AssertionError("not needed")

    return _Engine()


def test_config_reports_option_conflicts():
    app = ApiServer(_config_engine(ConflictSettings()), host="127.0.0.1", port=0)
    status, _headers, body = app.dispatch("GET", "/api/v1/config", b"")
    payload = json.loads(body)
    assert status == 200
    assert payload["conflicts"] == [
        {
            "key": "mode",
            "option": "auto",
            "stored": "advisory",
            "message": "add-on option mode='auto' is ignored: the stored setting mode='advisory' wins",
        }
    ]


def test_config_reports_an_empty_conflict_list_by_default():
    app = ApiServer(_config_engine(PlainSettings()), host="127.0.0.1", port=0)
    status, _headers, body = app.dispatch("GET", "/api/v1/config", b"")
    payload = json.loads(body)
    assert status == 200
    assert payload["conflicts"] == []
    # the rest of the payload is unchanged
    assert {"options", "settings"} <= set(payload)


# --------------------------------------------------------------------------------------
# 6. persisted actuals keep the sampler's 5-minute resolution (SPEC 7.3)
# --------------------------------------------------------------------------------------


def test_actuals_are_bucketed_at_the_measurement_resolution():
    engine, store, _ha, clock, _settings = build_engine(
        clock=Clock(datetime(2026, 1, 1, 11, 7, 33, tzinfo=timezone.utc))
    )
    engine._apply_measurements(
        {"grid_w": 250.0, "pv_w": 0.0, "load_w": 400.0, "price_buy": 0.2}, clock.now
    )
    row = engine._actuals_ring[-1]
    assert row["ts"] == datetime(2026, 1, 1, 11, 5, tzinfo=timezone.utc).isoformat(), row["ts"]
    assert row["ts"] != datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc).isoformat()
    # the bucket is the sampler's own resolution, not a second copy of the number
    from app.models import quarter_floor

    assert row["ts"] == quarter_floor(clock.now, MEASUREMENT_MINUTES).isoformat()
    assert store.actuals and store.actuals[-1]["ts"] == row["ts"]


# --------------------------------------------------------------------------------------
# 5b. derived wear (SPEC 5): per delivered kWh, fleet average, discharge-only charging
# --------------------------------------------------------------------------------------


def test_wear_cost_is_charged_per_delivered_kwh_on_discharge_only():
    # 1 stored kWh discharged at eta_dis 0.5 delivers 0.5 AC kWh, so it pays half the constant
    assert dp.wear_cost_eur(0.1, 0.5, 2.0, 1.0) == pytest.approx(0.05)
    assert dp.wear_cost_eur(0.1, 0.5, 1.0, 2.0) == 0.0, "charging stores energy; it delivers none"
    assert dp.wear_cost_eur(0.1, 0.5, 1.0, 1.0) == 0.0
    assert dp.wear_cost_eur(0.0, 0.5, 2.0, 1.0) == 0.0


def test_fleet_wear_is_the_usable_weighted_mean_and_the_override_wins():
    cheap = battery(
        id="battery_cheap",
        capacity_kwh=5.0,
        price_eur=1000.0,
        cycle_life=2000,
    )
    costly = battery(
        id="battery_costly",
        capacity_kwh=15.0,
        price_eur=9000.0,
        cycle_life=6000,
    )
    assert cheap.wear_eur_per_kwh == pytest.approx(1000.0 / (2000 * 5.0 * 0.95))
    assert costly.wear_eur_per_kwh == pytest.approx(9000.0 / (6000 * 15.0 * 0.95))
    agg = dp.BatteryAggregate.from_configs([cheap, costly], 0.0)
    # The fleet mean is weighted by delivered energy (usable x eta_discharge), not capacity alone.
    delivered_cheap = 5.0 * cheap.eta_discharge
    delivered_costly = 15.0 * costly.eta_discharge
    expected = (
        delivered_cheap * cheap.wear_eur_per_kwh + delivered_costly * costly.wear_eur_per_kwh
    ) / (delivered_cheap + delivered_costly)
    assert agg.wear_eur_per_kwh == pytest.approx(expected)
    for config in (cheap, costly):
        assert config.wear_eur_per_kwh * config.usable_kwh() * config.eta_discharge == pytest.approx(
            config.price_eur / config.cycle_life
        ), "one full cycle costs price / cycle_life"

    slots = make_slots(8, 15, pv=0.0, load=0.0, buy=[0.10, 0.30, 0.20, 0.40, 0.20, 0.30, 0.10, 0.40])
    opts = dp.OptimizerOptions(slot_minutes=15, soc_step_kwh=0.5, power_steps=6, allow_export=True)
    plan = dp.solve(slots, aggregate=agg, start_kwh=10.0, options=opts, generated_at=T0)
    assert plan.summary.wear_eur_per_kwh == pytest.approx(expected), "the DP falls back to derived wear"

    override = dp.OptimizerOptions(
        slot_minutes=15, soc_step_kwh=0.5, power_steps=6, allow_export=True, wear_eur_per_kwh=0.07
    )
    plan_override = dp.solve(slots, aggregate=agg, start_kwh=10.0, options=override, generated_at=T0)
    assert plan_override.summary.wear_eur_per_kwh == pytest.approx(0.07)
    derived_break_even = dp.median_buy_eur_per_kwh(slots) / agg.eta_charge / agg.eta_discharge + expected
    override_break_even = dp.median_buy_eur_per_kwh(slots) / agg.eta_charge / agg.eta_discharge + 0.07
    assert plan.summary.break_even_sell_eur_per_kwh == pytest.approx(derived_break_even)
    assert plan_override.summary.break_even_sell_eur_per_kwh == pytest.approx(override_break_even)


def test_a_battery_without_price_or_life_plans_with_no_wear():
    free = battery(capacity_kwh=2.0, max_charge_w=2000.0, max_discharge_w=2000.0)
    assert free.wear_eur_per_kwh == 0.0
    agg = dp.BatteryAggregate.from_configs([free], 0.0)
    assert agg.wear_eur_per_kwh == 0.0
    slots = make_slots(8, 15, pv=0.0, load=0.0, buy=[0.10, 0.30, 0.20, 0.40, 0.20, 0.30, 0.10, 0.40])
    opts = dp.OptimizerOptions(slot_minutes=15, soc_step_kwh=0.5, power_steps=6, allow_export=True)
    plan = dp.solve(slots, aggregate=agg, start_kwh=1.0, options=opts, generated_at=T0)
    assert plan.summary.cycle_cost_eur == 0.0
    assert plan.summary.wear_eur_per_kwh == 0.0


def test_an_efficiency_payload_from_an_older_formula_is_reported_but_not_applied():
    """0.31.0 owns the pair formula: a payload without its schema stamp is never trusted."""
    now = T0 + timedelta(days=1)
    legacy = {
        "round_trip": 0.80,
        "eta_charge": 0.80**0.5,
        "eta_discharge": 0.80**0.5,
        "samples": 5,
        "updated_at": to_iso(now),
    }
    status = efficiency_learning.round_trip_status(legacy, now)
    assert status["applied"] is False
    assert status["round_trip"] is None and status["eta_charge"] is None
    assert status["learned_round_trip"] == pytest.approx(0.80), "the evidence is still reported"
    assert status["reason"] == efficiency_learning.REASON_LOW_CONFIDENCE
    current = {**legacy, "schema": efficiency_learning.SCHEMA}
    assert efficiency_learning.round_trip_status(current, now)["applied"] is True
