"""WP2A: measurement sampling, role mapping and the 5-minute bucket (SPEC.md section 7.3)."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "energy_manager") not in sys.path:
    sys.path.insert(0, str(ROOT / "energy_manager"))

from app.config import Settings  # noqa: E402
from app.models import BatteryConfig  # noqa: E402
from app.sources.measure import MEASUREMENT_MINUTES, collect, power_to_w, role_entities  # noqa: E402
from app.store import Store  # noqa: E402

NOW = datetime(2026, 1, 5, 12, 2, tzinfo=timezone.utc)


class FakeClient:
    """Serves single-entity reads and refuses the unfiltered snapshot the sampler must avoid."""

    def __init__(self, states):
        self.states = {str(item["entity_id"]): item for item in states}
        self.reads = []

    def get_state(self, entity_id):
        self.reads.append(entity_id)
        if entity_id not in self.states:
            raise KeyError(entity_id)  # an unreadable/unknown entity is skipped, not fatal
        return self.states[entity_id]

    def get_states(self):  # pragma: no cover - must never be called
        raise AssertionError("the sampler must not fetch the full /api/states snapshot")


def state(entity_id, value, unit):
    return {"entity_id": entity_id, "state": str(value), "attributes": {"unit_of_measurement": unit}}


def battery() -> BatteryConfig:
    return BatteryConfig(
        id="battery_1",
        name="Example battery",
        driver="zendure",
        capacity_kwh=5.0,
        mapping={
            "charge_power_entity": "sensor.example_battery_charge",
            "discharge_power_entity": "sensor.example_battery_discharge",
            "soc_entity": "sensor.example_battery_soc",
        },
    )


@pytest.fixture()
def store(tmp_path):
    instance = Store(tmp_path / "measure.db")
    yield instance
    instance.close()


def test_power_to_w_units():
    assert power_to_w(1500, "W") == pytest.approx(1500)
    assert power_to_w(1.5, "kW") == pytest.approx(1500)
    assert power_to_w(1, "furlongs") is None


def test_role_entities_from_settings_batteries_and_overrides():
    settings = Settings(
        None,
        options={
            "solar.measured_entity": "sensor.example_pv_power",
            "load.entity": "sensor.example_load_power",
            "price.entity": "sensor.example_price_now",
            "grid.power_entity": "sensor.example_grid",
            "grid.import_entity": "sensor.example_grid_import",
            "grid.export_entity": "sensor.example_grid_export",
        },
    )
    mapping = role_entities(settings, [battery()])
    assert mapping["pv_power"] == "sensor.example_pv_power"
    assert mapping["load_power"] == "sensor.example_load_power"
    assert mapping["price"] == "sensor.example_price_now"
    assert mapping["grid_power"] == "sensor.example_grid"
    assert mapping["grid_import"] == "sensor.example_grid_import"
    assert mapping["grid_export"] == "sensor.example_grid_export"
    assert mapping["battery_charge:battery_1"] == "sensor.example_battery_charge"
    assert mapping["battery_discharge:battery_1"] == "sensor.example_battery_discharge"
    assert mapping["battery_soc:battery_1"] == "sensor.example_battery_soc"
    # explicit roles (engine's confirmed mapping) win over the settings keys
    overridden = role_entities(settings, [], {"pv_power": "sensor.example_override", "grid_power": "sensor.example_grid_override"})
    assert overridden["pv_power"] == "sensor.example_override"
    assert overridden["grid_power"] == "sensor.example_grid_override"


def test_collect_derives_grid_kw_from_import_and_export_roles():
    settings = Settings(
        None,
        options={"grid.import_entity": "sensor.example_grid_import", "grid.export_entity": "sensor.example_grid_export"},
    )
    client = FakeClient([state("sensor.example_grid_import", 900, "W"), state("sensor.example_grid_export", 400, "W")])
    measurements, actuals = collect(client, settings, now=NOW)
    assert {item.role for item in measurements} == {"grid_import", "grid_export"}
    assert actuals["grid_kw"] == pytest.approx((900 - 400) / 1000.0)


def test_collect_converts_units_and_builds_actuals():
    settings = Settings(None, options={"solar.measured_entity": "sensor.example_pv_power", "load.entity": "sensor.example_load_power", "price.entity": "sensor.example_price_now"})
    states = [
        state("sensor.example_grid", 1500, "W"),
        state("sensor.example_pv_power", 2.5, "kW"),
        state("sensor.example_load_power", 400, "W"),
        state("sensor.example_battery_charge", 600, "W"),
        state("sensor.example_battery_discharge", 100, "W"),
        state("sensor.example_battery_soc", 50, "%"),
        state("sensor.example_price_now", 21, "ct/kWh"),
        state("sensor.example_unrelated", 1, "W"),
    ]
    client = FakeClient(states)
    measurements, actuals = collect(client, settings, [battery()], entities={"grid_power": "sensor.example_grid"}, now=NOW)
    roles = {item.role: item.value for item in measurements}
    assert roles["grid_power"] == pytest.approx(1500)
    assert roles["pv_power"] == pytest.approx(2500)
    assert roles["load_power"] == pytest.approx(400)
    assert roles["battery_charge:battery_1"] == pytest.approx(600)
    assert roles["battery_discharge:battery_1"] == pytest.approx(100)
    assert roles["battery_soc:battery_1"] == pytest.approx(50)
    assert roles["price"] == pytest.approx(0.21)
    assert actuals["grid_kw"] == pytest.approx(1.5)
    assert actuals["pv_kw"] == pytest.approx(2.5)
    assert actuals["load_kw"] == pytest.approx(0.4)
    assert actuals["battery_kw"] == pytest.approx(0.5)  # 600 W in - 100 W out, positive = charging
    assert actuals["soc_kwh"] == pytest.approx(2.5)
    assert actuals["price_buy"] == pytest.approx(0.21)
    assert actuals["pv_updated_at"] is None  # legacy/fake states carry no HA report timestamp
    assert {"pv_kw", "load_kw", "grid_kw"} <= set(actuals)


def test_collect_prefers_home_assistants_last_reported_for_live_pv():
    settings = Settings(None, options={"solar.measured_entity": "sensor.example_pv_power"})
    pv = state("sensor.example_pv_power", 1500, "W")
    pv["last_updated"] = (NOW - timedelta(minutes=9)).isoformat()
    pv["last_reported"] = NOW.isoformat()
    _, actuals = collect(FakeClient([pv]), settings, now=NOW)
    assert actuals["pv_kw"] == pytest.approx(1.5)
    assert actuals["pv_updated_at"] == NOW


def test_collect_never_mixes_dc_pv_input_into_battery_power():
    """DC ``battery_pv`` is not AC-side power: it must not reach ``actuals['battery_kw']``."""
    settings = Settings(None, options={})
    dc_only = BatteryConfig(
        id="battery_1",
        name="Example battery",
        driver="zendure",
        capacity_kwh=5.0,
        mapping={"pv_power_entity": "sensor.example_battery_pv", "output_power_entity": "sensor.example_battery_output"},
    )
    states = [state("sensor.example_battery_pv", 600, "W"), state("sensor.example_battery_output", 100, "W")]
    _, actuals = collect(FakeClient(states), settings, [dc_only], now=NOW)
    assert actuals["battery_kw"] is None


def test_collect_uses_signed_setpoint_when_present():
    settings = Settings(None, options={})
    battery_signed = BatteryConfig(
        id="battery_2", name="Example", driver="generic", capacity_kwh=10.0,
        mapping={"power_setpoint_entity": "number.example_battery_setpoint", "soc_entity": "sensor.example_soc_2"},
    )
    states = [state("number.example_battery_setpoint", -700, "W"), state("sensor.example_soc_2", 25, "%")]
    _, actuals = collect(FakeClient(states), settings, [battery_signed], now=NOW)
    assert actuals["battery_kw"] == pytest.approx(-0.7)
    assert actuals["soc_kwh"] == pytest.approx(2.5)


def test_disabled_batteries_are_skipped():
    settings = Settings(None, options={})
    disabled = BatteryConfig(
        id="battery_1", name="Example", driver="zendure", capacity_kwh=5.0, enabled=False,
        mapping={"soc_entity": "sensor.example_battery_soc"},
    )
    states = [state("sensor.example_battery_soc", 90, "%")]
    measurements, actuals = collect(FakeClient(states), settings, [disabled], now=NOW)
    assert measurements == []
    assert actuals["soc_kwh"] is None


def test_measured_pv_entity_and_report_time_are_persisted_for_history(store):
    settings = Settings(None, options={"solar.measured_entity": "sensor.example_pv_power"})
    pv = state("sensor.example_pv_power", 1250, "W")
    pv["last_reported"] = NOW.isoformat()
    measurements, actuals = collect(FakeClient([pv]), settings, now=NOW)

    store.add_measurements(measurements, pv_observed_at=actuals["pv_updated_at"])
    rows = store.measurements_range(NOW - timedelta(minutes=5), NOW + timedelta(minutes=5), roles=["pv_power"])
    assert len(rows) == 1
    assert rows[0]["entity_id"] == "sensor.example_pv_power"
    assert rows[0]["observed_at"] == NOW.isoformat()
    assert rows[0]["value"] == pytest.approx(1250.0)


def test_collect_reads_only_mapped_entities_and_upserts_five_minute_buckets(store):
    settings = Settings(None, options={"load.entity": "sensor.example_load_power"})
    client = FakeClient([state("sensor.example_load_power", 400, "W")])
    measurements, actuals = collect(client, settings, now=NOW)  # 12:02
    assert client.reads == ["sensor.example_load_power"]  # no full snapshot, one targeted read
    assert MEASUREMENT_MINUTES == 5
    bucket = NOW.replace(minute=0, second=0, microsecond=0)
    assert measurements[0].ts == bucket and actuals["ts"] == bucket

    # two ticks inside the same documented bucket upsert a single row (last value wins)
    store.add_measurements(measurements)
    client.states = {"sensor.example_load_power": state("sensor.example_load_power", 600, "W")}
    later, later_actuals = collect(client, settings, now=NOW + timedelta(minutes=2))
    assert later[0].ts == bucket and later_actuals["ts"] == bucket
    store.add_measurements(later)
    rows = store.measurements_range(NOW - timedelta(minutes=10), NOW + timedelta(minutes=10))
    assert len(rows) == 1 and rows[0]["value"] == pytest.approx(600.0)
    assert rows[0]["ts"] == "2026-01-05T12:00:00+00:00"


# ------------------------------------------- driver-resolved measured roles (0.20.3)


def test_measured_roles_resolve_through_the_driver():
    """The sampler reads the driver's own vocabulary, never a private key list (0.20.3).

    The live incident: a Zendure mapping with only the native pair persisted no battery power at
    all, so the cost-basis ledger and the capacity learner silently had no samples.
    """
    settings = Settings(None, options={})
    native = BatteryConfig(
        id="battery_1", name="Example", driver="zendure", capacity_kwh=5.0,
        mapping={
            "power_setpoint_entity": "number.example_battery_set",
            "grid_input_power_entity": "sensor.example_battery_grid_input",
            "output_power_entity": "sensor.example_battery_output_home",
            "pv_power_entity": "sensor.example_battery_pv",
            "soc_entity": "sensor.example_battery_soc",
        },
    )
    mapping = role_entities(settings, [native])
    assert mapping["battery_charge:battery_1"] == "sensor.example_battery_grid_input"
    assert mapping["battery_discharge:battery_1"] == "sensor.example_battery_output_home"
    assert mapping["battery_power:battery_1"] == "number.example_battery_set"
    assert mapping["battery_pv:battery_1"] == "sensor.example_battery_pv"
    assert mapping["battery_output:battery_1"] == "sensor.example_battery_output_home"
    assert mapping["battery_soc:battery_1"] == "sensor.example_battery_soc"

    generic = BatteryConfig(
        id="battery_2", name="Example", driver="generic", capacity_kwh=5.0,
        mapping={
            "charge_power_entity": "sensor.example_generic_charge",
            "discharge_power_entity": "sensor.example_generic_discharge",
            "soc_entity": "sensor.example_generic_soc",
        },
    )
    generic_roles = role_entities(settings, [generic])
    assert generic_roles["battery_charge:battery_2"] == "sensor.example_generic_charge"
    assert generic_roles["battery_discharge:battery_2"] == "sensor.example_generic_discharge"
    assert generic_roles["battery_soc:battery_2"] == "sensor.example_generic_soc"
    assert "battery_output:battery_2" not in generic_roles
    assert "battery_pv:battery_2" not in generic_roles


def test_generic_charge_keys_win_over_the_native_pair():
    """Both vocabularies present: the generic keys win, matching the save-time validation."""
    settings = Settings(None, options={})
    both = BatteryConfig(
        id="battery_1", name="Example", driver="zendure", capacity_kwh=5.0,
        mapping={
            "charge_power_entity": "sensor.example_generic_charge",
            "discharge_power_entity": "sensor.example_generic_discharge",
            "grid_input_power_entity": "sensor.example_battery_grid_input",
            "output_power_entity": "sensor.example_battery_output_home",
            "soc_entity": "sensor.example_battery_soc",
        },
    )
    mapping = role_entities(settings, [both])
    assert mapping["battery_charge:battery_1"] == "sensor.example_generic_charge"
    assert mapping["battery_discharge:battery_1"] == "sensor.example_generic_discharge"
    # Roles only the native mapping has still resolve.
    assert mapping["battery_output:battery_1"] == "sensor.example_battery_output_home"


def test_measured_roles_need_the_complete_native_pair_and_never_guess():
    """One native side is not a measurement; an unmapped battery resolves nothing at all."""
    settings = Settings(None, options={})
    incomplete = BatteryConfig(
        id="battery_1", name="Example", driver="zendure", capacity_kwh=5.0,
        mapping={
            "grid_input_power_entity": "sensor.example_battery_grid_input",
            "soc_entity": "sensor.example_battery_soc",
        },
    )
    mapping = role_entities(settings, [incomplete])
    assert "battery_charge:battery_1" not in mapping
    assert "battery_discharge:battery_1" not in mapping
    assert mapping["battery_soc:battery_1"] == "sensor.example_battery_soc"

    blank = BatteryConfig(
        id="battery_9", name="Example", driver="zendure", capacity_kwh=5.0,
        mapping={"manual_power_entity": "number.example_manual_power"},
    )
    assert role_entities(settings, [blank]) == {}


def test_collect_reads_registry_resolved_roles_through_the_mapping_override():
    """A registry-filled role counts for the sampler exactly like a user-confirmed one."""
    settings = Settings(None, options={})
    stored = BatteryConfig(
        id="battery_1", name="Example", driver="zendure", capacity_kwh=5.0,
        mapping={"manual_power_entity": "number.example_manual_power"},
    )
    resolved = {
        "battery_1": {
            "grid_input_power_entity": "sensor.example_battery_grid_input",
            "output_power_entity": "sensor.example_battery_output_home",
            "soc_entity": "sensor.example_battery_soc",
        }
    }
    states = [
        state("sensor.example_battery_grid_input", 500, "W"),
        state("sensor.example_battery_output_home", 200, "W"),
        state("sensor.example_battery_soc", 40, "%"),
    ]
    measurements, actuals = collect(
        FakeClient(states), settings, [stored], mappings=resolved, now=NOW
    )
    roles = {item.role for item in measurements}
    assert {"battery_charge:battery_1", "battery_discharge:battery_1", "battery_soc:battery_1"} <= roles
    assert "battery_power:battery_1" not in roles  # the stored mapping has no signed setpoint
    assert actuals["battery_kw"] == pytest.approx(0.3)  # 500 W in - 200 W out
    assert actuals["soc_kwh"] == pytest.approx(2.0)
