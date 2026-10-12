"""WP2A: configuration defaults, merge order, location resolution and battery CRUD (SPEC.md section 3)."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "energy_manager") not in sys.path:
    sys.path.insert(0, str(ROOT / "energy_manager"))

from app.config import (  # noqa: E402
    DEFAULTS,
    SETTINGS_META,
    Settings,
    battery_payload,
    coerce_setting,
    load_options,
    resolve_location,
    setting_descriptors,
)
from app.models import BatteryConfig, ConfigError  # noqa: E402
from app.store import Store  # noqa: E402

SPEC_KEYS = {
    "price.source",
    "price.entity",
    "price.static_schedule",
    "price.import_fee_eur_per_kwh",
    "price.export_fee_eur_per_kwh",
    "price.vat_pct",
    "price.energy_tax_eur_per_kwh",
    "price.unit",
    "price.scale_divisor",
    "price.bzn",
    "price.feed_in_eur_per_kwh",
    "price.feed_in_entity",
    "price.feed_in_tax_inclusive",
    "solar.enabled",
    "solar.source",
    "solar.planes",
    "solar.api_key",
    "solar.bias_correction",
    "solar.measured_entity",
    "load.mode",
    "load.entity",
    "load.temperature_entity",
    "load.base_kwh_per_day",
    "grid.power_entity",
    "grid.import_entity",
    "grid.export_entity",
    "optimizer.slot_minutes",
    "optimizer.horizon_hours",
    "optimizer.soc_step_kwh",
    "optimizer.power_steps",
    "optimizer.salvage_multiplier",
    "optimizer.cycle_cost_eur_per_kwh",
    "optimizer.allow_grid_charge",
    "optimizer.allow_export",
    "optimizer.reserve_soc_pct",
    "optimizer.max_grid_import_w",
    "optimizer.max_grid_export_w",
    "optimizer.min_spread_eur_per_kwh",
    "optimizer.target_soc_pct_end",
    "control.tick_seconds",
    "control.replan_minutes",
    "control.grid_follow",
    "control.grid_follow_deadband_w",
    "control.grid_follow_max_ratio",
    "control.grid_follow_max_w",
    "control.stale_plan_seconds",
    "control.stale_state_seconds",
    "control.failsafe_on_error",
    "maintenance.full_charge_enabled",
    "maintenance.full_charge_interval_days",
    "maintenance.full_charge_target_soc_pct",
    "notify.enabled",
    "notify.persistent",
    "notify.mobile_service",
    "notify.events",
    "batteries",
}


@pytest.fixture()
def store(tmp_path):
    instance = Store(tmp_path / "config.db")
    yield instance
    instance.close()


def test_defaults_cover_spec_and_ship_safe():
    assert SPEC_KEYS <= set(DEFAULTS)
    assert DEFAULTS["mode"] == "advisory" and DEFAULTS["dry_run"] is True
    assert DEFAULTS["latitude_override"] is None and DEFAULTS["longitude_override"] is None
    assert DEFAULTS["price.entity"] == "" and DEFAULTS["batteries"] == []
    assert DEFAULTS["grid.power_entity"] == "" and DEFAULTS["grid.import_entity"] == ""
    assert DEFAULTS["grid.export_entity"] == ""
    assert "accuracy_degraded" not in DEFAULTS["notify.events"]
    assert "solcast_api_key" not in DEFAULTS and "solar.declination_correction" not in DEFAULTS
    descriptors = setting_descriptors()
    assert [item["key"] for item in descriptors] == list(DEFAULTS)
    assert all("group" in item and "type" in item and "default" in item for item in descriptors)
    # no code emits accuracy_degraded, so the UI must not offer it as a choice either
    choices = {item["key"]: item["choices"] for item in descriptors}
    assert "accuracy_degraded" not in choices["notify.events"]
    assert DEFAULTS["notify.events"] and set(DEFAULTS["notify.events"]) <= set(choices["notify.events"])
    # mutable defaults are handed out by value, so callers cannot corrupt DEFAULTS
    Settings(None, options={}).get("price.static_schedule").append({"from_hour": 0})
    assert DEFAULTS["price.static_schedule"] == []


def test_grid_follow_max_w_round_trips_and_validates(store):
    """SPEC 3/9.1: the dispatcher's absolute budget must be storable and non-negative."""
    descriptors = {item["key"]: item for item in setting_descriptors()}
    entry = descriptors["control.grid_follow_max_w"]
    assert entry["group"] == "control" and entry["type"] == "float" and entry["default"] == 0.0
    assert "capability" in entry["help"] and entry["min"] == 0.0
    settings = Settings(store, options={})
    settings.set("control.grid_follow_max_w", 1200)
    assert Settings(store, options={}).get("control.grid_follow_max_w") == 1200.0
    with pytest.raises(ConfigError):
        coerce_setting("control.grid_follow_max_w", -1.0)


def test_stored_settings_override_addon_options_with_a_warning(store, caplog):
    """Stop now persists mode=advisory; editing the add-on option back must warn, not flip (audit item 3)."""
    store.set_setting("mode", "advisory")
    store.set_setting("dry_run", False)
    with caplog.at_level("WARNING"):
        settings = Settings(store, options={"mode": "auto", "dry_run": True, "log_level": "info"})
    # the stored value still wins and nothing is silently flipped either way
    assert settings.get("mode") == "advisory" and settings.get("dry_run") is False
    conflicts = {item["key"]: item for item in settings.option_conflicts()}
    assert set(conflicts) == {"mode", "dry_run"}
    assert conflicts["mode"]["option"] == "auto" and conflicts["mode"]["stored"] == "advisory"
    assert "stored setting" in conflicts["mode"]["message"] and "wins" in conflicts["mode"]["message"]
    warnings = [record.getMessage() for record in caplog.records if record.levelno >= 30]
    assert sum("stored setting" in message for message in warnings) == 2  # one per conflicting key

    # once the app writes what the option asks for, the conflict clears
    settings.set("mode", "auto")
    assert [item["key"] for item in settings.option_conflicts()] == ["dry_run"]
    # no false positives: options only, or matching stored values
    assert Settings(None, options={"mode": "auto", "dry_run": True}).option_conflicts() == []
    store.set_setting("dry_run", True)
    assert settings.option_conflicts() == []


def test_merge_order_options_then_db(store):
    settings = Settings(store, options={"mode": "advisory", "log_level": "warning", "latitude_override": 52.0})
    assert settings.get("mode") == "advisory"
    assert settings.get("log_level") == "warning"
    assert settings.get("latitude_override") == 52.0
    assert settings.get("price.source") == "zonneplan_entity"
    settings.set("mode", "auto")
    assert settings.get("mode") == "auto"  # DB beats options
    assert settings.get("log_level") == "warning"
    settings.set("price.import_fee_eur_per_kwh", "0.02")
    assert settings.get("price.import_fee_eur_per_kwh") == 0.02
    assert store.get_setting("mode") == "auto"
    fresh = Settings(store, options={"mode": "advisory"})
    assert fresh.get("mode") == "auto"


def test_validation_rejects_bad_values():
    with pytest.raises(ConfigError):
        coerce_setting("nope.nope", 1)
    with pytest.raises(ConfigError):
        coerce_setting("mode", "turbo")
    with pytest.raises(ConfigError):
        coerce_setting("optimizer.horizon_hours", "soon")
    with pytest.raises(ConfigError):
        coerce_setting("control.tick_seconds", 2)
    with pytest.raises(ConfigError):
        coerce_setting("price.static_schedule", [{"from_hour": 0}])
    with pytest.raises(ConfigError):
        coerce_setting("solar.planes", [{"kwp": 0}])
    assert coerce_setting("dry_run", "false") is False
    assert coerce_setting("optimizer.slot_minutes", 15.0) == 15
    assert coerce_setting("price.bzn", "DE-LU") == "DE-LU"
    assert coerce_setting("currency", None) is None
    with pytest.raises(ConfigError):
        coerce_setting("solar.planes", [{"kwp": 1.0, "azimuth_deg": 400}])
    assert coerce_setting("solar.planes", [{"kwp": 1.0, "azimuth_deg": 270}])[0]["azimuth_deg"] == 270.0
    schedule = coerce_setting(
        "price.static_schedule",
        [{"from_hour": 23, "to_hour": 7, "price_eur_per_kwh": 0.2, "days": ["Mon", "tue"]}],
    )
    assert schedule == [{"from_hour": 23, "to_hour": 7, "price_eur_per_kwh": 0.2, "days": [0, 1]}]


def test_update_reports_per_key_errors(store):
    settings = Settings(store, options={})
    applied, errors = settings.update({"mode": "auto", "optimizer.horizon_hours": 99999, "nope": 1})
    assert applied == ["mode"]
    assert {error["key"] for error in errors} == {"optimizer.horizon_hours", "nope"}


def test_resolve_location_order():
    # HA's field is time_zone; the legacy key still works, options override both
    ha_config = {"latitude": 52.0, "longitude": 5.0, "time_zone": "Europe/Amsterdam"}
    assert resolve_location(ha_config, {}) == (52.0, 5.0, "Europe/Amsterdam")
    assert resolve_location({"latitude": 52.0, "longitude": 5.0, "timezone": "UTC"}, {}) == (52.0, 5.0, "UTC")
    assert resolve_location(ha_config, {"latitude_override": 51.0, "longitude_override": 4.0, "timezone": "UTC"}) == (51.0, 4.0, "UTC")
    assert resolve_location({"latitude": 52.0, "longitude": 5.0}, {}) == (52.0, 5.0, "UTC")
    # an explicit 0, 0 override is a user choice and is accepted
    assert resolve_location(ha_config, {"latitude_override": 0.0, "longitude_override": 0.0, "timezone": "UTC"}) == (0.0, 0.0, "UTC")
    with pytest.raises(ConfigError):
        resolve_location({}, {})
    # HA reports 0, 0 when the location is unset: never treat that as a real position
    with pytest.raises(ConfigError) as err:
        resolve_location({"latitude": 0.0, "longitude": 0.0, "time_zone": "Europe/Amsterdam"}, {})
    assert "0, 0" in str(err.value)
    with pytest.raises(ConfigError):
        resolve_location({"latitude": "north", "longitude": 5.0}, {})
    with pytest.raises(ConfigError) as err:
        resolve_location(ha_config, {"timezone": "Mars/Olympus"})
    assert "unknown timezone" in str(err.value)


def test_options_loading(tmp_path):
    assert load_options(str(tmp_path / "missing.json")) == {}
    broken = tmp_path / "options.json"
    broken.write_text("{not json", encoding="utf-8")
    assert load_options(str(broken)) == {}
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"mode": "advisory"}), encoding="utf-8")
    assert load_options(str(good)) == {"mode": "advisory"}


def _battery(**overrides) -> BatteryConfig:
    payload = dict(
        id="battery_1",
        name="Example battery",
        driver="generic",
        capacity_kwh=5.0,
        min_soc_pct=10.0,
        max_soc_pct=95.0,
        max_charge_w=800.0,
        max_discharge_w=800.0,
        mapping={"power_setpoint_entity": "number.example_battery_setpoint"},
    )
    payload.update(overrides)
    return BatteryConfig(**payload)


def _battery_payload(**overrides) -> dict:
    payload = dict(
        id="battery_1",
        name="Example battery",
        driver="generic",
        capacity_kwh=5.0,
        mapping={"power_setpoint_entity": "number.example_battery_setpoint"},
    )
    payload.update(overrides)
    return payload


def test_stale_plan_seconds_round_trips_and_validates(store):
    """SPEC.md section 3: the plan-staleness gate's bound (WP2C reads it) must be settable."""
    descriptors = {item["key"]: item for item in setting_descriptors()}
    entry = descriptors["control.stale_plan_seconds"]
    assert entry["group"] == "control" and entry["type"] == "int" and entry["default"] == 1800
    assert entry["min"] == 60 and entry["max"] == 86400
    assert "older than" in entry["help"]
    settings = Settings(store, options={})
    settings.set("control.stale_plan_seconds", 3600)
    assert Settings(store, options={}).get("control.stale_plan_seconds") == 3600
    with pytest.raises(ConfigError):
        coerce_setting("control.stale_plan_seconds", 59)
    with pytest.raises(ConfigError):
        coerce_setting("control.stale_plan_seconds", 100000)


def test_round_trip_efficiency_pct_splits_evenly(store):
    settings = Settings(store, options={})
    created = settings.add_battery(_battery_payload(round_trip_efficiency_pct=75))
    # symmetric to within one ULP, and the product is exactly the number the user asked for
    # NB: write long-precision expectations as expressions (math.sqrt(0.75), 0.2 / 0.9);
    # tools/audit.sh flags 5+ fractional-digit literals as plausible coordinates (SPEC R1).
    assert created.eta_charge == pytest.approx(math.sqrt(0.75), abs=1e-15)
    assert created.eta_discharge == pytest.approx(math.sqrt(0.75), abs=1e-15)
    assert created.round_trip_efficiency == 0.75
    # the read-back the UI/API sees is derived, not stored
    assert battery_payload(created)["round_trip_efficiency_pct"] == 75.0
    assert "round_trip_efficiency_pct" not in DEFAULTS
    assert "round_trip_efficiency_pct" not in store.get_setting("batteries")[0]


@pytest.mark.parametrize("pct", [90, 92.5, 80, 99, 75, 100])
def test_round_trip_pct_is_exact_for_common_values(store, pct):
    settings = Settings(store, options={})
    created = settings.add_battery(_battery_payload(round_trip_efficiency_pct=pct))
    assert created.round_trip_efficiency == pct / 100  # exact, not approx
    assert battery_payload(created)["round_trip_efficiency_pct"] == float(pct)
    assert abs(created.eta_charge - created.eta_discharge) <= 2 * math.ulp(created.eta_charge)


def test_explicit_etas_win_over_a_conflicting_pct(store):
    settings = Settings(store, options={})
    created = settings.add_battery(
        _battery_payload(round_trip_efficiency_pct=90, eta_charge=0.8, eta_discharge=0.9)
    )
    assert (created.eta_charge, created.eta_discharge) == (0.8, 0.9)
    assert battery_payload(created)["round_trip_efficiency_pct"] == pytest.approx(72.0)

    # a pct-only patch re-splits, replacing the inherited per-direction values
    updated = settings.update_battery("battery_1", {"round_trip_efficiency_pct": 81})
    assert updated.eta_charge == pytest.approx(0.9) and updated.eta_discharge == pytest.approx(0.9)
    assert battery_payload(updated)["round_trip_efficiency_pct"] == pytest.approx(81.0)
    assert battery_payload(Settings(store, options={}).battery("battery_1"))[
        "round_trip_efficiency_pct"
    ] == pytest.approx(81.0)

    # explicit etas in a patch still win over the pct sent alongside them
    patched = settings.update_battery(
        "battery_1", {"round_trip_efficiency_pct": 90, "eta_charge": 0.7, "eta_discharge": 0.8}
    )
    assert (patched.eta_charge, patched.eta_discharge) == (0.7, 0.8)
    assert battery_payload(patched)["round_trip_efficiency_pct"] == pytest.approx(56.0)


@pytest.mark.parametrize("bad", [0, -5, 150, "ninety", True, float("nan")])
def test_round_trip_efficiency_pct_rejects_nonsense(store, bad):
    settings = Settings(store, options={})
    with pytest.raises(ConfigError) as err:
        settings.add_battery(_battery_payload(round_trip_efficiency_pct=bad))
    assert "round_trip_efficiency_pct" in str(err.value)
    assert settings.battery_list() == []  # rejected, not half-saved


def test_a_rejected_battery_change_keeps_the_existing_config(store):
    settings = Settings(store, options={})
    settings.add_battery(_battery_payload(round_trip_efficiency_pct=90))
    with pytest.raises(ConfigError):
        settings.update_battery("battery_1", {"round_trip_efficiency_pct": 0})
    with pytest.raises(ConfigError):
        settings.update_battery("battery_1", {"round_trip_efficiency_pct": "ninety"})
    batteries = settings.battery_list()
    assert [battery.id for battery in batteries] == ["battery_1"]
    assert batteries[0].round_trip_efficiency == pytest.approx(0.9)


def test_legacy_eta_only_batteries_keep_working(store):
    settings = Settings(store, options={})
    created = settings.add_battery(_battery_payload(eta_charge=0.9, eta_discharge=0.85))
    assert (created.eta_charge, created.eta_discharge) == (0.9, 0.85)
    assert battery_payload(created)["round_trip_efficiency_pct"] == pytest.approx(76.5)
    reloaded = Settings(store, options={}).battery("battery_1")
    assert (reloaded.eta_charge, reloaded.eta_discharge) == (0.9, 0.85)


def test_break_even_uses_the_requested_round_trip(store):
    settings = Settings(store, options={})
    ninety = settings.add_battery(_battery_payload(round_trip_efficiency_pct=90))
    assert ninety.break_even_sell_eur_per_kwh(0.20) == pytest.approx(0.2 / 0.9)
    seventy_five = settings.add_battery(
        _battery_payload(id="battery_2", round_trip_efficiency_pct=75)
    )
    assert seventy_five.break_even_sell_eur_per_kwh(0.20) == pytest.approx(0.2 / 0.75)


def test_wear_is_derived_from_price_and_cycle_life(store):
    settings = Settings(store, options={})
    plain = settings.add_battery(_battery_payload(round_trip_efficiency_pct=90))
    assert plain.wear_eur_per_kwh == 0.0, "no price/life means no wear, never a guess"
    assert plain.break_even_sell_eur_per_kwh(0.20) == pytest.approx(0.2 / 0.9)

    # 5 kWh x (100 - 10) % = 4.5 kWh usable; 2700 / (6000 x 4.5 x sqrt(0.9) discharge) per delivered kWh
    priced = settings.add_battery(
        _battery_payload(
            id="battery_2",
            round_trip_efficiency_pct=90,
            price_eur=2700.0,
            cycle_life=6000,
        )
    )
    wear = 2700.0 / (6000.0 * 4.5 * math.sqrt(0.9))
    assert priced.wear_eur_per_kwh == pytest.approx(wear)
    # One full discharge delivers usable x eta_discharge and must cost exactly price / cycle_life.
    assert priced.wear_eur_per_kwh * priced.usable_kwh() * priced.eta_discharge == pytest.approx(2700.0 / 6000.0)
    assert priced.break_even_sell_eur_per_kwh(0.20) == pytest.approx(0.2 / 0.9 + wear)
    assert battery_payload(priced)["wear_eur_per_kwh"] == pytest.approx(wear)

    # an explicit number is the advanced override; the derived value is the default
    assert priced.break_even_sell_eur_per_kwh(0.20, 0.0) == pytest.approx(0.2 / 0.9)
    assert priced.break_even_sell_eur_per_kwh(0.20, 0.25) == pytest.approx(0.2 / 0.9 + 0.25)

    # one input alone is still no wear; the UI says so rather than guessing the other side
    half = settings.add_battery(_battery_payload(id="battery_3", price_eur=2700.0))
    assert half.wear_eur_per_kwh == 0.0


def test_wear_fields_round_trip_and_validate(store):
    settings = Settings(store, options={})
    created = settings.add_battery(_battery_payload(price_eur=1200.0, cycle_life=3000))
    assert (created.price_eur, created.cycle_life) == (1200.0, 3000.0)
    stored = store.get_setting("batteries")[0]
    assert stored["price_eur"] == 1200.0 and stored["cycle_life"] == 3000.0

    # the derived wear is a read-only convenience: a PATCH accepting it must not persist it
    updated = settings.update_battery("battery_1", {"wear_eur_per_kwh": 0.5})
    assert battery_payload(updated)["wear_eur_per_kwh"] == pytest.approx(1200.0 / (3000 * 4.5 * 0.95))
    assert "wear_eur_per_kwh" not in store.get_setting("batteries")[0]

    with pytest.raises(ConfigError):
        settings.update_battery("battery_1", {"price_eur": -1})
    with pytest.raises(ConfigError):
        settings.update_battery("battery_1", {"cycle_life": -1})
    for field in ("price_eur", "cycle_life"):
        for value in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(ConfigError):
                settings.update_battery("battery_1", {field: value})
    reloaded = Settings(store, options={}).battery("battery_1")
    assert (reloaded.price_eur, reloaded.cycle_life) == (1200.0, 3000.0)


def test_battery_crud(store):
    settings = Settings(store, options={})
    created = settings.add_battery(_battery())
    assert created.id == "battery_1"
    assert settings.battery_list()[0].capacity_kwh == 5.0
    assert settings.battery("battery_1").name == "Example battery"

    with pytest.raises(ConfigError):
        settings.add_battery(_battery())

    updated = settings.update_battery("battery_1", {"max_charge_w": 1200.0, "enabled": False})
    assert updated.max_charge_w == 1200.0 and updated.enabled is False
    with pytest.raises(ConfigError):
        settings.update_battery("battery_1", {"id": "battery_2"})
    with pytest.raises(ConfigError):
        settings.update_battery("nope", {"name": "x"})

    assert settings.remove_battery("battery_1") is True
    assert settings.remove_battery("battery_1") is False
    assert settings.battery_list() == []


def test_generic_battery_requires_setpoint():
    settings = Settings(None, options={})
    with pytest.raises(ConfigError) as err:
        settings.add_battery(_battery(mapping={}))
    assert "power_setpoint_entity" in str(err.value)
    with pytest.raises(ConfigError):
        settings.add_battery(_battery(driver="mystery"))
    # Zendure mappings are discovered at runtime; a non-empty confirmed mapping is still required.
    with pytest.raises(ConfigError):
        settings.add_battery(_battery(driver="zendure", mapping={}))
    zendure = settings.add_battery(
        _battery(driver="zendure", mapping={"power_setpoint_entity": "number.example_battery_setpoint"})
    )
    assert zendure.driver == "zendure"


def test_battery_helpers_without_store_keep_memory_only():
    settings = Settings(None, options={})
    settings.add_battery(_battery())
    settings.update_battery("battery_1", {"max_discharge_w": 400.0})
    assert settings.battery("battery_1").max_discharge_w == 400.0
    assert settings.get("batteries")[0]["max_discharge_w"] == 400.0


# ----------------------------------------------------------------------------------------------
# SPEC 4.1: price provider/contract warnings (GET /api/v1/config price_warnings).
# ----------------------------------------------------------------------------------------------


def _price_settings(**overrides):
    return Settings(None, options=overrides)


def test_default_price_settings_have_no_warnings():
    # entity (default) accepts any contract and needs nothing extra.
    assert _price_settings().price_warnings() == []
    assert _price_settings(**{"price.provider": "zonneplan", "price.contract": "dynamic_quarter_hourly"}).price_warnings() == []


def test_provider_contract_mismatch_is_a_warning_not_a_failure():
    dynamic_providers = ("zonneplan", "day_ahead")
    for provider in dynamic_providers:
        for contract in ("fixed_single", "fixed_day_night"):
            warnings = {item["key"] for item in _price_settings(**{"price.provider": provider, "price.contract": contract}).price_warnings()}
            assert "price.contract" in warnings, (provider, contract)
    # the fixed provider needs a fixed contract
    for contract in ("dynamic_quarter_hourly", "dynamic_hourly"):
        warnings = _price_settings(**{"price.provider": "fixed", "price.contract": contract}).price_warnings()
        assert any(w["key"] == "price.contract" and "fixed" in w["message"] for w in warnings)
    # entity accepts a fixed contract without a mismatch warning (advanced explicit path)
    assert all(w["key"] != "price.contract" for w in _price_settings(**{"price.provider": "entity", "price.contract": "fixed_single"}).price_warnings())


def test_fixed_single_requires_a_price_or_entity():
    missing = _price_settings(**{"price.provider": "fixed", "price.contract": "fixed_single"}).price_warnings()
    assert any(w["key"] == "price.fixed_price_eur_per_kwh" for w in missing)
    # a typed rate satisfies it
    assert not any(w["key"] == "price.fixed_price_eur_per_kwh" for w in _price_settings(**{"price.provider": "fixed", "price.contract": "fixed_single", "price.fixed_price_eur_per_kwh": 0.25}).price_warnings())
    # an entity alone satisfies it too
    assert not any(w["key"] == "price.fixed_price_eur_per_kwh" for w in _price_settings(**{"price.provider": "fixed", "price.contract": "fixed_single", "price.fixed_price_entity": "sensor.example_tariff"}).price_warnings())


def test_fixed_day_night_requires_both_rates_and_distinct_windows():
    base = {"price.provider": "fixed", "price.contract": "fixed_day_night"}
    neither = _price_settings(**base).price_warnings()
    keys = {w["key"] for w in neither}
    assert "price.fixed_day_price_eur_per_kwh" in keys and "price.fixed_night_price_eur_per_kwh" in keys
    # only the day rate is set: night still missing
    only_day = _price_settings(**{**base, "price.fixed_day_price_eur_per_kwh": 0.3}).price_warnings()
    assert {w["key"] for w in only_day} == {"price.fixed_night_price_eur_per_kwh"}
    # both rates, but the two start hours collapse the windows
    collapsed = _price_settings(**{**base, "price.fixed_day_price_eur_per_kwh": 0.3, "price.fixed_night_price_eur_per_kwh": 0.15, "price.fixed_day_start_hour": 7, "price.fixed_night_start_hour": 7}).price_warnings()
    assert any(w["key"] == "price.fixed_day_start_hour" for w in collapsed)
    # a valid day/night contract is clean
    ok = _price_settings(**{**base, "price.fixed_day_price_eur_per_kwh": 0.3, "price.fixed_night_price_eur_per_kwh": 0.15}).price_warnings()
    assert ok == []


def test_day_ahead_wholesale_with_no_adders_warns_it_is_not_a_consumer_price():
    zero = {"price.provider": "day_ahead", "price.contract": "dynamic_hourly", "price.vat_pct": 0.0, "price.energy_tax_eur_per_kwh": 0.0, "price.import_fee_eur_per_kwh": 0.0, "price.export_fee_eur_per_kwh": 0.0}
    warnings = _price_settings(**zero).price_warnings()
    assert any("not a consumer price" in w["message"] for w in warnings)
    # any single adder configured makes it a consumer price and clears the informational warning
    for key in ("price.vat_pct", "price.energy_tax_eur_per_kwh", "price.import_fee_eur_per_kwh"):
        configured = dict(zero)
        configured[key] = 0.05
        assert not any("not a consumer price" in w["message"] for w in _price_settings(**configured).price_warnings()), key
    # not day_ahead: never warned, even with every adder at zero
    assert not any("not a consumer price" in w["message"] for w in _price_settings(**{**zero, "price.provider": "zonneplan"}).price_warnings())


# ----------------------------------------------------------------------------------------------
# SPEC 13.3: the frozen `advanced` classification travels from _Meta to the UI (folded, not hidden).
# ----------------------------------------------------------------------------------------------

#: Exactly the keys SPEC 13.3 freezes as advanced. Everything else is essential.
ADVANCED_KEYS = {
    "currency",
    "timezone",
    "latitude_override",
    "longitude_override",
    "batteries",
    "log_level",
    "price.source",
    "price.entity",
    "price.contract",
    "price.bzn",
    "price.unit",
    "price.scale_divisor",
    "price.vat_pct",
    "price.energy_tax_eur_per_kwh",
    "price.import_fee_eur_per_kwh",
    "price.fixed_price_entity",
    "price.fixed_day_price_entity",
    "price.fixed_night_price_entity",
    "price.fixed_day_start_hour",
    "price.fixed_night_start_hour",
    "price.static_schedule",
    "price.feed_in_eur_per_kwh",
    "price.feed_in_entity",
    "price.feed_in_tax_inclusive",
    "solar.bias_correction",
    "solar.capacity_wp",
    "solar.tilt_deg",
    "solar.azimuth_deg",
    "solar.performance_ratio",
    "solar.weather_entity",
    "solar.nasa_irradiance",
    "solar.planes",
    "solar.api_key",
    "solar.sources",
    "solar.merge_strategy",
    "load.mode",
    "load.entity",
    "load.temperature_entity",
    "load.base_kwh_per_day",
    "grid.import_entity",
    "grid.export_entity",
    "control.tick_seconds",
    "control.replan_minutes",
    "control.grid_follow",
    "control.grid_follow_deadband_w",
    "control.grid_follow_max_ratio",
    "control.grid_follow_max_w",
    "control.stale_state_seconds",
    "control.stale_plan_seconds",
    "notify.events",
    "notify.persistent",
    "notify.mobile_service",
    "optimizer.slot_minutes",
    "optimizer.power_steps",
    "optimizer.soc_step_kwh",
    "optimizer.salvage_multiplier",
    "optimizer.cycle_cost_eur_per_kwh",
    "optimizer.target_soc_pct_end",
    # SPEC 5 cost basis (0.18.0): the margin folds, the on/off stays essential.
    "optimizer.sell_floor_margin_eur_per_kwh",
    # SPEC 8.1 scheduled full-charge maintenance: the two tuning knobs fold, the on/off stays essential.
    "maintenance.full_charge_interval_days",
    "maintenance.full_charge_target_soc_pct",
}


def test_every_meta_payload_carries_a_boolean_advanced():
    """No key may be left unclassified: the Settings form folds on this flag alone (SPEC 13.2)."""
    for key, meta in SETTINGS_META.items():
        payload = meta.as_payload()
        assert isinstance(payload.get("advanced"), bool), key
    descriptors = setting_descriptors()
    keys = {row["key"] for row in descriptors}
    assert keys >= set(SETTINGS_META) == set(DEFAULTS)
    for row in descriptors:
        assert isinstance(row["advanced"], bool), row["key"]


def test_advanced_classification_is_exactly_the_frozen_list():
    """Both directions: a new key must be classified deliberately, and nothing new may be advanced."""
    from_meta = {key for key, meta in SETTINGS_META.items() if meta.as_payload()["advanced"]}
    from_payload = {row["key"] for row in setting_descriptors() if row["advanced"]}
    assert from_meta == ADVANCED_KEYS
    assert from_payload == ADVANCED_KEYS
    # a handful of keys that must stay essential, on both sides of the folding line
    for essential in ("mode", "dry_run", "price.provider", "solar.source", "grid.power_entity"):
        assert essential not in ADVANCED_KEYS, essential
        assert next(row for row in setting_descriptors() if row["key"] == essential)["advanced"] is False
    for advanced in ("price.source", "batteries", "optimizer.salvage_multiplier", "price.contract"):
        assert next(row for row in setting_descriptors() if row["key"] == advanced)["advanced"] is True


def test_no_label_carries_a_manual_advanced_suffix():
    """Folding is the only signal, and labels are user-facing text (SPEC 13.2/13.3)."""
    for row in setting_descriptors():
        assert "(advanced)" not in row["label"], row["key"]
    assert {row["label"] for row in setting_descriptors() if row["key"] in ("price.source", "price.entity")} == {
        "Price source",
        "Price entity",
    }
