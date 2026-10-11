"""WP2A: load history tiers, profile learning, cold start and prediction (SPEC.md section 7.2)."""

from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "energy_manager") not in sys.path:
    sys.path.insert(0, str(ROOT / "energy_manager"))

from app.config import Settings  # noqa: E402
from app.models import BatteryConfig  # noqa: E402
from app.sources.load import (  # noqa: E402
    build_profile,
    collect_history,
    collect_pv_export_samples,
    collect_temperatures,
    load_forecast,
    power_to_kw,
    pv_export_roles,
    temperature_to_c,
    update_load_profile,
)
from app.store import Store  # noqa: E402

NOW = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)  # Monday


class FakeClient:
    def __init__(self, states=None, history=None, fail=()):
        self.states = states or {}
        self.history = history or {}
        self.fail = set(fail)
        self.calls = []

    def get_state(self, entity_id):
        return self.states[entity_id]

    def get_history(self, entity_id, start, end):
        self.calls.append((entity_id, start, end))
        if entity_id in self.fail:
            raise RuntimeError("TemperatureBoom")
        return self.history.get(entity_id, [])


class FakeWS:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def statistics_during_period(self, ids, start, end=None, period="hour", types=None):
        self.calls.append((ids, period, types))
        return self.data


def state_row(entity_id, value, unit="W"):
    return {"entity_id": entity_id, "state": str(value), "attributes": {"unit_of_measurement": unit}}


def hourly_rows(entity_id, start, hours, value, unit="W"):
    return [
        [
            {
                "entity_id": entity_id,
                "state": str(value),
                "last_changed": (start + timedelta(hours=index)).isoformat(),
                "attributes": {"unit_of_measurement": unit},
            }
        ]
        for index in range(hours)
    ]


@pytest.fixture()
def store(tmp_path):
    instance = Store(tmp_path / "load.db")
    yield instance
    instance.close()


def test_unit_helpers():
    assert power_to_kw(1500, "W") == pytest.approx(1.5)
    assert power_to_kw(2.0, "kW") == pytest.approx(2.0)
    assert power_to_kw(1, "MW") == pytest.approx(1000.0)
    assert power_to_kw(1, "furlongs") is None and power_to_kw(1, None) is None
    assert temperature_to_c(20, "°C") == pytest.approx(20)
    assert temperature_to_c(68, "°F") == pytest.approx(20)
    assert temperature_to_c(293.15, "K") == pytest.approx(20)
    assert temperature_to_c(20, "bananas") is None


def test_statistics_tier_is_preferred():
    settings = Settings(None, options={"load.entity": "sensor.example_load_power"})
    client = FakeClient({"sensor.example_load_power": state_row("sensor.example_load_power", 1000, "W")})
    ws = FakeWS(
        {
            "sensor.example_load_power": [
                {"start": int((NOW - timedelta(hours=2)).timestamp()), "mean": 500.0},
                {"start": int((NOW - timedelta(hours=1)).timestamp()), "mean": 700.0},
            ]
        }
    )
    result = collect_history(settings, client, ws=ws, now=NOW)
    assert result.source == "statistics" and result.ok
    assert result.samples[0][1] == pytest.approx(0.5)
    assert ws.calls and ws.calls[0][1] == "hour" and ws.calls[0][2] == ["mean"]


def test_history_tier_when_statistics_unavailable():
    settings = Settings(None, options={"load.entity": "sensor.example_load_power"})
    client = FakeClient(
        {"sensor.example_load_power": state_row("sensor.example_load_power", 1000, "W")},
        history={"sensor.example_load_power": hourly_rows("sensor.example_load_power", NOW - timedelta(hours=3), 3, 800.0)},
    )
    result = collect_history(settings, client, ws=None, now=NOW)
    assert result.source == "history"
    assert result.samples[-1][1] == pytest.approx(0.8)


def test_derived_tier_and_mode():
    settings = Settings(None, options={"load.mode": "derived"})
    client = FakeClient(
        history={
            "sensor.example_grid": hourly_rows("sensor.example_grid", NOW - timedelta(hours=2), 2, 1000.0, "W"),
            "sensor.example_pv": hourly_rows("sensor.example_pv", NOW - timedelta(hours=2), 2, 400.0, "W"),
            "sensor.example_battery": hourly_rows("sensor.example_battery", NOW - timedelta(hours=2), 2, -200.0, "W"),
        }
    )
    result = collect_history(
        settings,
        client,
        derived_entities={"grid_power": "sensor.example_grid", "pv": "sensor.example_pv", "battery": "sensor.example_battery"},
        now=NOW,
    )
    assert result.source == "derived"
    assert result.samples[-1][1] == pytest.approx(1.0 + 0.4 + 0.2)  # grid + pv - (-0.2 charge)


def test_profile_and_forecast_bucket_by_local_hour():
    """A resolved zone must move both the learned hour buckets and the predicted hours."""
    base = (NOW - timedelta(days=20)).replace(minute=0, second=0, microsecond=0)
    samples = [
        (base + timedelta(days=day, hours=hour), 2.0 if (base + timedelta(days=day, hours=hour)).hour == 17 else 0.5)
        for day in range(20)
        for hour in range(24)
    ]
    local = build_profile(samples, now=NOW, tz="Europe/Amsterdam")
    utc = build_profile(samples, now=NOW)
    # 17:00 UTC == 18:00 CET: the same sample lands in a different bucket
    assert local["buckets"][18]["median"] == pytest.approx(2.0)
    assert local["buckets"][17]["median"] == pytest.approx(0.5)
    assert utc["buckets"][17]["median"] == pytest.approx(2.0)

    evening = datetime(2026, 1, 5, 17, 0, tzinfo=timezone.utc)  # 18:00 local, 17:00 UTC
    settings = Settings(None, options={})
    local_forecast, _ = load_forecast(settings, None, now=evening, profile=local, tz="Europe/Amsterdam")
    utc_forecast, _ = load_forecast(settings, None, now=evening, profile=local, tz="UTC")
    assert local_forecast.slots[0].load_kw == pytest.approx(2.0, abs=0.2)
    assert utc_forecast.slots[0].load_kw == pytest.approx(0.5, abs=0.2)


def test_derived_tier_subtracts_a_mapped_battery_flow():
    """Charging/discharging intervals must not be learned as household load (audit item 1)."""
    settings = Settings(
        None,
        options={
            "load.mode": "derived",
            "grid.import_entity": "sensor.example_grid_import",
            "solar.measured_entity": "sensor.example_pv",
        },
    )
    settings.add_battery(
        BatteryConfig(
            id="example_battery_1",
            name="Example battery",
            driver="zendure",
            capacity_kwh=5.0,
            mapping={
                "charge_power_entity": "sensor.example_battery_charge",
                "discharge_power_entity": "sensor.example_battery_discharge",
            },
        )
    )
    client = FakeClient(
        history={
            "sensor.example_grid_import": hourly_rows("sensor.example_grid_import", NOW - timedelta(hours=2), 2, 2000.0),
            "sensor.example_pv": hourly_rows("sensor.example_pv", NOW - timedelta(hours=2), 2, 500.0),
            "sensor.example_battery_charge": hourly_rows("sensor.example_battery_charge", NOW - timedelta(hours=2), 2, 1500.0),
            "sensor.example_battery_discharge": hourly_rows("sensor.example_battery_discharge", NOW - timedelta(hours=2), 2, 0.0),
        }
    )
    result = collect_history(settings, client, now=NOW)
    assert result.source == "derived"
    # 2.0 (grid) + 0.5 (pv) - 1.5 (charge) = 1.0 kW gross load; cycling would otherwise read 2.5
    assert result.samples[-1][1] == pytest.approx(1.0)


def test_derived_tier_refused_when_a_configured_battery_has_no_flow_mapping():
    settings = Settings(
        None,
        options={
            "load.mode": "derived",
            "grid.import_entity": "sensor.example_grid_import",
            "solar.measured_entity": "sensor.example_pv",
        },
    )
    settings.add_battery(
        BatteryConfig(
            id="example_battery_1",
            name="Example battery",
            driver="zendure",
            capacity_kwh=5.0,
            mapping={"soc_entity": "sensor.example_battery_soc"},
        )
    )
    client = FakeClient(
        history={
            "sensor.example_grid_import": hourly_rows("sensor.example_grid_import", NOW - timedelta(hours=2), 2, 2000.0),
            "sensor.example_pv": hourly_rows("sensor.example_pv", NOW - timedelta(hours=2), 2, 500.0),
        }
    )
    result = collect_history(settings, client, now=NOW)
    assert result.source == "fallback" and result.samples == []
    assert any("no AC flow mapping" in note for note in result.notes)


def test_derived_tier_skips_intervals_without_battery_flow_samples():
    settings = Settings(
        None,
        options={
            "load.mode": "derived",
            "grid.import_entity": "sensor.example_grid_import",
            "solar.measured_entity": "sensor.example_pv",
        },
    )
    settings.add_battery(
        BatteryConfig(
            id="example_battery_1",
            name="Example battery",
            driver="zendure",
            capacity_kwh=5.0,
            mapping={
                "charge_power_entity": "sensor.example_battery_charge",
                "discharge_power_entity": "sensor.example_battery_discharge",
            },
        )
    )
    client = FakeClient(
        history={
            "sensor.example_grid_import": hourly_rows("sensor.example_grid_import", NOW - timedelta(hours=2), 2, 2000.0),
            "sensor.example_pv": hourly_rows("sensor.example_pv", NOW - timedelta(hours=2), 2, 500.0),
            # battery data only for the older hour: the recent hour is skipped, never zero-filled
            "sensor.example_battery_charge": hourly_rows("sensor.example_battery_charge", NOW - timedelta(hours=2), 1, 1500.0),
            "sensor.example_battery_discharge": hourly_rows("sensor.example_battery_discharge", NOW - timedelta(hours=2), 1, 0.0),
        }
    )
    result = collect_history(settings, client, now=NOW)
    assert result.source == "derived" and len(result.samples) == 1
    assert result.samples[0][1] == pytest.approx(1.0)


def test_derived_tier_refused_when_battery_flow_history_is_missing():
    settings = Settings(
        None,
        options={
            "load.mode": "derived",
            "grid.import_entity": "sensor.example_grid_import",
            "solar.measured_entity": "sensor.example_pv",
        },
    )
    settings.add_battery(
        BatteryConfig(
            id="example_battery_1",
            name="Example battery",
            driver="zendure",
            capacity_kwh=5.0,
            mapping={"power_setpoint_entity": "number.example_battery_power"},
        )
    )
    client = FakeClient(
        history={
            "sensor.example_grid_import": hourly_rows("sensor.example_grid_import", NOW - timedelta(hours=2), 2, 2000.0),
            "sensor.example_pv": hourly_rows("sensor.example_pv", NOW - timedelta(hours=2), 2, 500.0),
            # no history at all for number.example_battery_power
        }
    )
    result = collect_history(settings, client, now=NOW)
    assert result.source == "fallback" and result.samples == []
    assert any("battery flow data" in note for note in result.notes)


def test_derived_tier_takes_grid_roles_from_settings():
    settings = Settings(
        None,
        options={
            "load.mode": "derived",
            "grid.import_entity": "sensor.example_grid_import",
            "grid.export_entity": "sensor.example_grid_export",
            "solar.measured_entity": "sensor.example_pv",  # the PV term that makes the load gross
        },
    )
    client = FakeClient(
        history={
            "sensor.example_grid_import": hourly_rows("sensor.example_grid_import", NOW - timedelta(hours=2), 2, 1000.0, "W"),
            "sensor.example_grid_export": hourly_rows("sensor.example_grid_export", NOW - timedelta(hours=2), 2, 400.0, "W"),
            "sensor.example_pv": hourly_rows("sensor.example_pv", NOW - timedelta(hours=2), 2, 900.0, "W"),
        }
    )
    result = collect_history(settings, client, now=NOW)
    assert result.source == "derived"
    # grid import - export + pv = gross household consumption, not net grid import
    assert result.samples[-1][1] == pytest.approx(0.6 + 0.9)

    # an explicit role from the engine's confirmed mapping still wins over grid.import_entity
    client.history["sensor.example_grid"] = hourly_rows("sensor.example_grid", NOW - timedelta(hours=2), 2, 500.0, "W")
    explicit = collect_history(settings, client, derived_entities={"grid_power": "sensor.example_grid"}, now=NOW)
    assert explicit.source == "derived"
    assert explicit.samples[-1][1] == pytest.approx(0.5 + 0.9)


def test_derived_tier_is_refused_without_a_pv_role():
    """Net grid import must never be learned as household load (WP5/audit finding)."""
    settings = Settings(
        None, options={"load.mode": "derived", "grid.power_entity": "sensor.example_grid"}
    )
    client = FakeClient(
        history={"sensor.example_grid": hourly_rows("sensor.example_grid", NOW - timedelta(hours=2), 2, 1000.0, "W")}
    )
    result = collect_history(settings, client, now=NOW)
    assert result.source == "fallback" and result.samples == []
    assert any("needs a pv series" in note for note in result.notes)


def test_collect_history_without_anything_falls_back():
    result = collect_history(Settings(None, options={}), None, now=NOW)
    assert result.source == "fallback" and result.samples == []
    assert any("flat profile" in note for note in result.notes)


def test_collect_temperatures_converts_units():
    client = FakeClient(history={"sensor.example_temp": hourly_rows("sensor.example_temp", NOW - timedelta(hours=3), 3, 68.0, "°F")})
    rows = collect_temperatures(client, "sensor.example_temp", now=NOW, days=1)
    assert rows[-1][1] == pytest.approx(20.0)


def _temperature_driven_client(days=21, fail=()) -> FakeClient:
    """Recorder history where household load tracks the outdoor temperature (SPEC.md section 7.2)."""
    begin = (NOW - timedelta(days=days)).replace(minute=0, second=0, microsecond=0)
    load_rows, temp_rows = [], []
    for index in range(days * 24):
        stamp = begin + timedelta(hours=index)
        temperature = (
            6.0
            + 8.0 * math.sin(2 * math.pi * (index % 24) / 24.0)
            + 3.0 * math.sin(2 * math.pi * index / (24 * 7))
        )
        for entity, value, unit, rows in (
            ("sensor.example_load_power", 500.0 + 50.0 * temperature, "W", load_rows),
            ("sensor.example_outdoor_temp", temperature, "°C", temp_rows),
        ):
            rows.append(
                [
                    {
                        "entity_id": entity,
                        "state": str(value),
                        "last_changed": stamp.isoformat(),
                        "attributes": {"unit_of_measurement": unit},
                    }
                ]
            )
    return FakeClient(
        history={"sensor.example_load_power": load_rows, "sensor.example_outdoor_temp": temp_rows},
        fail=fail,
    )


def test_temperature_entity_feeds_the_regression_term(store):
    settings = Settings(
        None,
        options={
            "load.entity": "sensor.example_load_power",
            "load.temperature_entity": "sensor.example_outdoor_temp",
        },
    )
    client = _temperature_driven_client()
    result = collect_history(settings, client, now=NOW, days=21)
    assert result.source == "history" and result.ok
    assert result.samples.temperatures  # the series rides along to the fit
    assert not any("temperature:" in note for note in result.notes)
    # fetched for the same window as the load series
    assert ("sensor.example_outdoor_temp", NOW - timedelta(days=21), NOW) in client.calls

    profile = update_load_profile(store, result.samples, now=NOW, tz="UTC")
    assert profile["b"] == pytest.approx(0.05, abs=0.005)  # 50 W per °C as kW/°C
    assert profile["r2"] > 0.9
    assert profile["temperature_hours"] > 0

    # with the same load history but no temperature entity the term is off, as before
    plain = collect_history(
        Settings(None, options={"load.entity": "sensor.example_load_power"}),
        _temperature_driven_client(),
        now=NOW,
        days=21,
    )
    assert plain.samples.temperatures == {}
    assert not any("temperature" in note for note in plain.notes)
    cold = build_profile(plain.samples, now=NOW, tz="UTC")
    assert cold["b"] == 0.0 and cold["r2"] == 0.0 and cold["temperature_hours"] == 0

    # and the learned slope reaches the forecast when the caller supplies forecast temperatures
    future = {
        NOW + timedelta(hours=index): 18.0 for index in range(int(settings.get("optimizer.horizon_hours")))
    }
    warm, _ = load_forecast(settings, store, now=NOW, tz="UTC", temperature_c=future)
    neutral, _ = load_forecast(settings, store, now=NOW, tz="UTC")
    assert sum(slot.load_kw for slot in warm.slots) > sum(slot.load_kw for slot in neutral.slots)


def test_without_a_temperature_entity_nothing_is_fetched_and_the_fit_is_unchanged():
    settings = Settings(None, options={"load.entity": "sensor.example_load_power"})
    client = _temperature_driven_client()
    result = collect_history(settings, client, now=NOW, days=21)
    assert {entity for entity, _, _ in client.calls} == {"sensor.example_load_power"}
    assert result.samples.temperatures == {}
    assert not any("temperature" in note for note in result.notes)
    # identical to the plain-list path: no temperature term, same numbers
    assert build_profile(result.samples, now=NOW, tz="UTC") == build_profile(
        list(result.samples), now=NOW, tz="UTC"
    )


def test_broken_temperature_entity_degrades_with_a_note(store):
    settings = Settings(
        None,
        options={
            "load.entity": "sensor.example_load_power",
            "load.temperature_entity": "sensor.example_outdoor_temp",
        },
    )
    empty = _temperature_driven_client()
    empty.history["sensor.example_outdoor_temp"] = []
    result = collect_history(settings, empty, now=NOW, days=21)
    assert result.source == "history" and result.ok
    assert result.samples.temperatures == {}
    assert any("no usable readings" in note for note in result.notes)
    profile = update_load_profile(store, result.samples, now=NOW, tz="UTC")
    assert profile["b"] == 0.0 and profile["temperature_hours"] == 0
    forecast, status = load_forecast(settings, store, now=NOW, tz="UTC")
    assert status.ok and forecast.slots  # the load forecast still works

    broken = _temperature_driven_client(fail={"sensor.example_outdoor_temp"})
    result = collect_history(settings, broken, now=NOW, days=21)
    assert result.ok and result.samples.temperatures == {}
    assert any("TemperatureBoom" in note for note in result.notes)


def _eight_weeks() -> list[tuple[datetime, float]]:
    base = (NOW - timedelta(days=55)).replace(minute=0, second=0, microsecond=0)
    samples: list[tuple[datetime, float]] = []
    for day_index in range(56):
        day = base + timedelta(days=day_index)
        recent = day_index >= 49
        for hour in range(24):
            if day.weekday() >= 5:
                value = 2.0
            else:
                value = 1.5 if recent else 1.0
            samples.append((day.replace(hour=hour), value))
    return samples


def test_build_profile_separates_weekends_scales_and_corrects_bias():
    samples = _eight_weeks()
    profile = build_profile(samples, now=NOW, tz="UTC")
    weekday_median = profile["buckets"][12]["median"]
    weekend_median = profile["buckets"][24 + 12]["median"]
    assert weekday_median == pytest.approx(1.0)
    assert weekend_median == pytest.approx(2.0)
    assert 0.5 <= profile["scale"] <= 2.0
    assert profile["scale"] > 1.0  # recent week is higher
    assert profile["bias"][12] > 1.2  # weekday EWMA picks up the recent level
    assert profile["bias"][24 + 12] == pytest.approx(1.0)
    assert profile["buckets"][12]["p10"] <= weekday_median <= profile["buckets"][12]["p90"]
    assert profile["days"] == 56 and profile["samples"] == 56 * 24


def test_temperature_regression_only_when_explanatory():
    base = (NOW - timedelta(days=14)).replace(minute=0, second=0, microsecond=0)
    samples, temperatures = [], {}
    for day_index in range(14):
        for hour in range(24):
            stamp = (base + timedelta(days=day_index)).replace(hour=hour)
            temp = 5.0 + hour * 0.5
            samples.append((stamp, 1.0 + 0.1 * temp))
            temperatures[stamp] = temp
    profile = build_profile(samples, now=NOW, temperatures=temperatures)
    assert profile["r2"] > 0.9
    assert profile["b"] == pytest.approx(0.1, abs=0.01)

    flat = build_profile([(stamp, 1.0) for stamp, _ in samples], now=NOW, temperatures=temperatures)
    assert flat["r2"] == pytest.approx(0.0)


def test_load_forecast_cold_start_and_learned(store):
    settings = Settings(store, options={"load.base_kwh_per_day": 24.0})
    forecast, status = load_forecast(settings, store, now=NOW)
    assert status.ok and forecast.confidence == "low"
    assert len(forecast.slots) == settings.get("optimizer.horizon_hours") * 4
    assert forecast.slots[0].t == NOW
    assert forecast.slots[0].load_kw > 0.0
    assert forecast.slots[0].p10_kw == pytest.approx(forecast.slots[0].load_kw * 0.7)

    settings.set("load.base_kwh_per_day", 0.0)
    empty, status = load_forecast(settings, store, now=NOW)
    assert empty.source == "fallback-empty" and all(slot.load_kw == 0.0 for slot in empty.slots)

    update_load_profile(store, _eight_weeks(), now=NOW, tz="UTC")
    learned, status = load_forecast(settings, store, now=NOW)
    assert learned.source == "profile" and learned.confidence == "high"
    slot = learned.slots[0]
    assert slot.load_kw > 1.0 and slot.p10_kw <= slot.load_kw <= slot.p90_kw


def test_prediction_interpolates_hourly_values():
    profile = build_profile(_eight_weeks(), now=NOW, tz="UTC")
    profile["buckets"][12]["median"] = 1.0
    profile["buckets"][13]["median"] = 2.0
    profile["bias"] = [1.0] * 48
    profile["scale"] = 1.0
    forecast, _ = load_forecast(Settings(None, options={}), None, now=NOW, profile=profile, tz="UTC")
    first, second = forecast.slots[0], forecast.slots[1]
    assert first.t.minute == 0 and second.t.minute == 15
    assert first.load_kw == pytest.approx(1.0)
    assert forecast.slots[4].load_kw == pytest.approx(2.0)  # 13:00
    assert second.load_kw == pytest.approx(1.25)


# --------------------------------------------------------------- Energy-dashboard history (SPEC 7.1.2)
# Long-term statistics are never purged, so the derived tiers read the meter through them whenever
# a WebSocket client is available; energy counters (kWh) are converted to hourly average power, so
# an Energy dashboard with no power sensor still gives the meter-derived PV tier a history window.

GRID_STAT = "sensor.example_grid_power"
IMPORT_STAT = "sensor.example_grid_energy_import"
EXPORT_STAT = "sensor.example_grid_energy_export"
PV_STAT = "sensor.example_pv_power"
POWER_META = {"unit_class": "power", "statistics_unit_of_measurement": "W"}
ENERGY_META = {"unit_class": "energy", "statistics_unit_of_measurement": "kWh"}


class FakeStatisticsWS:
    """``recorder/get_statistics_metadata`` + ``statistics_during_period`` without a socket."""

    def __init__(self, metadata=None, rows=None):
        self.metadata = metadata or {}
        self.rows = rows or {}
        self.metadata_calls = []
        self.period_calls = []

    def statistics_metadata(self, ids):
        self.metadata_calls.append(list(ids))
        return {stat_id: self.metadata[stat_id] for stat_id in ids if stat_id in self.metadata}

    def statistics_during_period(self, ids, start, end=None, period="hour", types=None):
        self.period_calls.append((list(ids), period, tuple(types or ())))
        return {stat_id: self.rows.get(stat_id, []) for stat_id in ids}


def hourly_stat_rows(series):
    return [
        {
            "start": stamp.isoformat(),
            "end": (stamp + timedelta(hours=1)).isoformat(),
            "mean": value,
        }
        for stamp, value in series
    ]


def hourly_change_rows(series):
    return [
        {
            "start": stamp.isoformat(),
            "end": (stamp + timedelta(hours=1)).isoformat(),
            "change": value,
        }
        for stamp, value in series
    ]


def test_pv_export_samples_prefer_long_term_statistics_over_the_recorder():
    start = NOW - timedelta(hours=3)
    ws = FakeStatisticsWS(
        metadata={GRID_STAT: POWER_META},
        rows={
            GRID_STAT: hourly_stat_rows(
                [(start, 1200.0), (start + timedelta(hours=1), -800.0), (start + timedelta(hours=2), -400.0)]
            )
        },
    )
    client = FakeClient()  # no recorder history at all
    settings = Settings(None, options={"grid.power_entity": GRID_STAT})
    samples, reason = collect_pv_export_samples(settings, client, ws=ws, now=NOW, days=2)
    assert reason == ""
    assert [round(value, 3) for _stamp, value in samples] == [0.8, 0.4]
    assert client.calls == [], "statistics answered; the recorder must not be touched"
    assert ws.period_calls[0][1] == "hour" and ws.period_calls[0][2] == ("mean",)


def test_statistics_without_rows_fall_back_to_recorder_history():
    ws = FakeStatisticsWS(metadata={GRID_STAT: POWER_META})
    client = FakeClient(history={GRID_STAT: hourly_rows(GRID_STAT, NOW - timedelta(hours=2), 2, -600.0, "W")})
    settings = Settings(None, options={"grid.power_entity": GRID_STAT})
    samples, reason = collect_pv_export_samples(settings, client, ws=ws, now=NOW, days=2)
    assert reason == ""
    assert [round(value, 3) for _stamp, value in samples] == [0.6, 0.6]


def test_energy_counter_statistics_become_a_power_series():
    start = NOW - timedelta(hours=2)
    ws = FakeStatisticsWS(
        metadata={IMPORT_STAT: ENERGY_META, EXPORT_STAT: ENERGY_META},
        rows={
            IMPORT_STAT: hourly_change_rows([(start, 0.7)]),
            EXPORT_STAT: hourly_change_rows([(start, 1.5), (start + timedelta(hours=1), 0.25)]),
        },
    )
    # An import hour at the same stamp reduces the net export, and 1.5 kWh over one hour is 1.5 kW.
    samples, reason = collect_pv_export_samples(
        Settings(None, options={}),
        FakeClient(),
        ws=ws,
        now=NOW,
        days=2,
        roles={"grid_energy_import": IMPORT_STAT, "grid_energy_export": EXPORT_STAT},
    )
    assert reason == ""
    assert [round(value, 3) for _stamp, value in samples] == [0.8, 0.25]
    assert ws.period_calls[0][2] == ("change",)


def test_a_counter_reset_never_becomes_negative_pv():
    start = NOW - timedelta(hours=1)
    ws = FakeStatisticsWS(
        metadata={
            EXPORT_STAT: ENERGY_META,
            IMPORT_STAT: {"unit_class": "energy", "statistics_unit_of_measurement": "Wh"},
        },
        rows={
            EXPORT_STAT: hourly_change_rows([(start, -0.4)]),  # a reset delta, not a sale
            IMPORT_STAT: hourly_change_rows([(start, 1000.0)]),  # 1 kWh imported over the hour
        },
    )
    samples, reason = collect_pv_export_samples(
        Settings(None, options={}),
        FakeClient(),
        ws=ws,
        now=NOW,
        days=2,
        roles={"grid_energy_import": IMPORT_STAT, "grid_energy_export": EXPORT_STAT},
    )
    assert samples == [], "a floored reset leaves the hour importing"
    assert "never went negative" in reason


def test_derived_load_reads_long_term_statistics():
    settings = Settings(
        None,
        options={
            "load.mode": "derived",
            "grid.import_entity": IMPORT_STAT,
            "solar.measured_entity": PV_STAT,
        },
    )
    start = NOW - timedelta(hours=2)
    ws = FakeStatisticsWS(
        metadata={IMPORT_STAT: POWER_META, PV_STAT: POWER_META},
        rows={
            IMPORT_STAT: hourly_stat_rows([(start, 1000.0), (start + timedelta(hours=1), 1000.0)]),
            PV_STAT: hourly_stat_rows([(start, 900.0), (start + timedelta(hours=1), 900.0)]),
        },
    )
    client = FakeClient()  # the recorder has nothing; only statistics answer
    result = collect_history(settings, client, ws=ws, now=NOW)
    assert result.source == "derived"
    assert result.samples[-1][1] == pytest.approx(1.0 + 0.9)


def test_pv_export_roles_accept_dashboard_defaults_and_explicit_settings_win():
    roles, refusal = pv_export_roles(
        Settings(None, options={}),
        defaults={"grid_energy_import": "sensor.example_in", "grid_energy_export": "sensor.example_out"},
    )
    assert refusal == ""
    assert roles == {"grid_energy_import": "sensor.example_in", "grid_energy_export": "sensor.example_out"}

    roles, refusal = pv_export_roles(
        Settings(None, options={"grid.power_entity": GRID_STAT}),
        defaults={"grid_energy_export": "sensor.example_out"},
    )
    assert refusal == ""
    assert roles == {"grid_power": GRID_STAT}, "an explicit grid.* setting replaces the fallback wholesale"


def test_pv_export_roles_without_any_grid_still_refuses_with_a_hint():
    roles, refusal = pv_export_roles(Settings(None, options={}), defaults={"pv": PV_STAT})
    assert roles == {}
    assert "Energy dashboard" in refusal
