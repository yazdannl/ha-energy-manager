"""Named load devices (SPEC.md section 7.2): subtraction, exclusion, per-device temperature terms.

The feature exists because one house-wide slope cannot be right for both a heat pump (which answers
to the weather) and an EV charger (which answers to its schedule). The load-bearing half is the
*subtraction*: a measured total already contains the device, so a base profile learned from the total
and then added to the device's own profile counts it twice — and the doubling hides inside the
temperature term as a steeper slope rather than as an error. These tests are written so that swapping
the subtraction for an addition (or forgetting to add the device back) fails loudly.

Entity ids are deliberately in the audit's obviously-fake ``example_`` namespace.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "energy_manager") not in sys.path:
    sys.path.insert(0, str(ROOT / "energy_manager"))

from app.config import (  # noqa: E402
    LOAD_DEVICE_KINDS,
    ConfigError,
    Settings,
    setting_descriptors,
)
from app.sources.load import (  # noqa: E402
    DEFAULT_SHAPE,
    DEVICE_KIND_LABELS,
    DEVICE_KEY_PREFIX,
    DEVICE_TEMPERATURE_KINDS,
    collect_history,
    device_config_problems,
    load_device_profile,
    load_devices,
    load_forecast,
    load_profile,
    split_device_load,
    update_load_profile,
    _parse_devices,
)
from app.store import Store  # noqa: E402

NOW = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)  # Monday midnight: whole-day learning windows
LOAD = "sensor.example_load_power"
HP = "sensor.example_heat_pump_power"
EV = "sensor.example_ev_charger_power"
HP_KEY = DEVICE_KEY_PREFIX + HP
EV_KEY = DEVICE_KEY_PREFIX + EV
#: kW per °C the synthetic heat pump gains as it gets colder: the signal a per-device term must find.
COLD_KWH_PER_DEGREE = 0.08
#: The median outdoor temperature of the synthetic week, in °C.
T_MEAN = 8.5


@pytest.fixture()
def store(tmp_path):
    instance = Store(tmp_path / "devices.db")
    yield instance
    instance.close()


# ------------------------------------------------------------------------------------------------
# Synthetic house: a base shape with no temperature response, plus devices that do (or do not).
# ------------------------------------------------------------------------------------------------

def _days(count: int = 28) -> list[datetime]:
    start = (NOW - timedelta(days=count)).replace(hour=0, minute=0, second=0, microsecond=0)
    return [start + timedelta(days=index) for index in range(count)]


def _temperature(day_index: int) -> float:
    """A week-long temperature cycle centred on :data:`T_MEAN`, symmetric for weekdays and weekends.

    The offsets sum to zero and the *weekday* and *weekend* medians are both zero, so every bucket
    median in a full week answers exactly :data:`T_MEAN` — the same value the base profile is
    checked against, whichever side of the weekend|weekday split a bucket lands on.
    """
    offsets = (-3.0, 3.0, -1.0, 1.0, 0.0, 2.0, -2.0)
    return T_MEAN + offsets[day_index % 7]


def _base_kw(hour: int) -> float:
    """A plain domestic shape. Constant per hour, so it correlates with nothing."""
    return 0.4 + (1.0 if 17 <= hour <= 20 else 0.0)


def _house(days: int = 28, *, with_heat_pump: bool = True, with_ev: bool = True):
    """``(total, {name: series}, temperatures)`` for the synthetic house.

    The heat pump follows the day's temperature; the EV charges at 2 kW between 01:00 and 03:00.
    The base shape is constant per hour while temperature varies by day, so a base profile that
    fits a slope is the model inventing one — which is what makes the device's own slope visible.
    """
    total: list[tuple[datetime, float]] = []
    heat_pump: dict[datetime, float] = {}
    ev: dict[datetime, float] = {}
    temperatures: dict[datetime, float] = {}
    for day_index, day in enumerate(_days(days)):
        temperature = _temperature(day_index)
        for hour in range(24):
            stamp = day.replace(hour=hour)
            value = _base_kw(hour)
            if with_heat_pump:
                pump = 0.5 + COLD_KWH_PER_DEGREE * (12.0 - temperature)
                heat_pump[stamp] = pump
                value += pump
            if with_ev:
                car = 2.0 if 1 <= hour <= 3 else 0.0
                ev[stamp] = car
                value += car
            total.append((stamp, value))
            temperatures[stamp] = temperature
    return total, {"heat_pump": heat_pump, "ev": ev}, temperatures


def _settings(*devices: dict, house: str = "") -> Settings:
    options: dict = {"load.devices": list(devices)}
    if house:
        options["load.entity"] = house
    return Settings(None, options=options)


def _hp(kind: str = "heat_pump") -> dict:
    return {"entity": HP, "kind": kind, "name": "Heat pump"}


def _ev(kind: str = "ev_charger") -> dict:
    return {"entity": EV, "kind": kind}


def _future(temperature: float, hours: int = 30) -> dict[datetime, float]:
    start = NOW.replace(minute=0, second=0, microsecond=0)
    return {start + timedelta(hours=index): temperature for index in range(hours)}


def _slot_at_hour(forecast, hour: int):
    """The slot for ``hour`` on the current day (the horizon starts at :data:`NOW`)."""
    return next(
        slot for slot in forecast.slots if slot.t.hour == hour and slot.t.date() == NOW.date()
    )


def _pump_kw() -> float:
    return 0.5 + COLD_KWH_PER_DEGREE * (12.0 - T_MEAN)


# ------------------------------------------------------------------------------------------------
# The subtraction is the load-bearing part
# ------------------------------------------------------------------------------------------------

def test_the_base_profile_is_learned_from_the_total_minus_the_device(store):
    """A base learned from the total and then added to a device counts the device twice.

    If the subtraction is ever swapped for an addition (or dropped), the noon bucket comes out at
    house + 2x pump or house + pump instead of house, and the assertions below fail.
    """
    total, series, temperatures = _house()
    settings = _settings(_hp())
    devices = load_devices(settings)
    profile = update_load_profile(
        store,
        total,
        now=NOW,
        tz="UTC",
        temperatures=temperatures,
        devices=devices,
        device_samples={HP_KEY: series["heat_pump"]},
    )

    assert profile["devices_subtracted"] == [HP_KEY]
    assert profile["devices_excluded"] == {}
    assert profile["devices_dropped_hours"] == 0
    # The house alone: 0.4 kW at noon, 1.4 kW in the evening peak.
    assert profile["buckets"][12]["median"] == pytest.approx(_base_kw(12), abs=0.03)
    assert profile["buckets"][18]["median"] == pytest.approx(_base_kw(18), abs=0.03)
    total_noon = next(value for stamp, value in total if stamp.hour == 12)
    assert total_noon > profile["buckets"][12]["median"] + 0.5, (
        "the base bucket still holds the heat pump: the total was learned as the base"
    )

    # And the device's own profile describes the device, not the house.
    device = load_device_profile(store, HP_KEY)
    assert device["kind"] == "heat_pump" and device["entity"] == HP and device["name"] == "Heat pump"
    assert device["buckets"][12]["median"] == pytest.approx(_pump_kw(), abs=0.02)
    assert device["buckets"][2]["median"] == pytest.approx(_pump_kw(), abs=0.02)
    assert device["samples"] > 0 and device["days"] >= 28


def test_the_forecast_adds_the_device_back_above_the_base(store):
    """base + device = total. Neither the total alone nor the total plus the device is right."""
    total, series, temperatures = _house()
    settings = _settings(_hp())
    devices = load_devices(settings)
    update_load_profile(
        store,
        total,
        now=NOW,
        tz="UTC",
        temperatures=temperatures,
        devices=devices,
        device_samples={HP_KEY: series["heat_pump"]},
    )
    forecast, status = load_forecast(
        settings, store, now=NOW, tz="UTC", temperature_c=_future(T_MEAN)
    )

    assert forecast.slots[0].t == NOW
    noon = _slot_at_hour(forecast, 12)
    assert noon.load_kw == pytest.approx(_base_kw(12) + _pump_kw(), abs=0.05)
    # Explicitly not the two ways this can go wrong: the device missing, or counted twice.
    assert noon.load_kw > _base_kw(12) + 0.4
    assert noon.load_kw < _base_kw(12) + 2.0 * _pump_kw()
    assert status.detail["devices_modelled"] == 1 and status.detail["devices_excluded"] == 0
    assert any("modelled" in note and "Heat pump" in note for note in forecast.notes)


def test_a_device_that_cannot_be_measured_is_not_subtracted_and_is_reported(store):
    """A missing meter means unknown, not zero: it stays inside the base and the note says so."""
    total, series, temperatures = _house()
    settings = _settings(_hp(), _ev())
    devices = load_devices(settings)
    profile = update_load_profile(
        store,
        total,
        now=NOW,
        tz="UTC",
        temperatures=temperatures,
        devices=devices,
        # The EV meter returned nothing this run.
        device_samples={HP_KEY: series["heat_pump"]},
    )

    assert profile["devices_subtracted"] == [HP_KEY]
    assert "no usable hours" in profile["devices_excluded"][EV_KEY]
    # The EV stayed in the base: the 02:00 bucket is house + EV, and the heat pump is not in it.
    assert profile["buckets"][2]["median"] == pytest.approx(_base_kw(2) + 2.0, abs=0.05)
    assert load_device_profile(store, EV_KEY) is None

    forecast, _ = load_forecast(settings, store, now=NOW, tz="UTC", temperature_c=_future(T_MEAN))
    assert any("not modelled" in note and "EV charger" in note for note in forecast.notes)
    assert any("no usable hours" in note for note in forecast.notes)
    # 02:00 tomorrow: house + pump + EV, because the EV was never taken out of the base.
    night = next(
        slot for slot in forecast.slots if slot.t.hour == 2 and slot.t.date() > NOW.date()
    )
    assert night.load_kw == pytest.approx(_base_kw(2) + _pump_kw() + 2.0, abs=0.06)
    assert night.load_kw < _base_kw(2) + _pump_kw() + 4.0, "the unmeasured EV was added on top of itself"


def test_a_device_whose_meter_covers_too_little_of_the_window_is_refused(store):
    """A meter readable at a handful of hours would cost most of the base profile to model."""
    total, series, temperatures = _house()
    settings = _settings(_hp(), _ev())
    devices = load_devices(settings)
    # Five readings in the whole window: below DEVICE_MIN_SAMPLES.
    sparse = {stamp: value for stamp, value in list(series["ev"].items())[:5]}
    profile = update_load_profile(
        store,
        total,
        now=NOW,
        tz="UTC",
        temperatures=temperatures,
        devices=devices,
        device_samples={HP_KEY: series["heat_pump"], EV_KEY: sparse},
    )
    assert profile["devices_subtracted"] == [HP_KEY]
    assert "only 5 usable hours" in profile["devices_excluded"][EV_KEY]
    assert profile["devices_dropped_hours"] == 0  # the refused meter cost the base no hours


def test_split_skips_hours_where_a_subtracted_device_has_no_reading():
    """A missing device reading is unknown: that house hour is dropped, never read as zero.

    Zero would leave the device's draw in the base and then add the device's profile on top of it.
    """
    house = [(datetime(2026, 1, 1, hour, tzinfo=timezone.utc), 3.0) for hour in range(10)]
    device = {stamp: 1.0 for stamp, _ in house}
    gap = house[4][0]
    device.pop(gap)
    devices, problems = _parse_devices(_settings(_hp()))
    assert problems == []
    # The direct-call floor is lowered: these series are ten hours long on purpose, and what is
    # being pinned here is the gap rule, not the learning-window size check.
    split = split_device_load(house, devices, {HP_KEY: device}, min_samples=1)
    assert [stamp for stamp, _ in split.base] == [stamp for stamp, _ in house if stamp != gap]
    assert all(value == pytest.approx(2.0) for _, value in split.base)
    assert split.subtracted == [HP_KEY] and split.dropped_hours == 1


def test_split_drops_the_hour_when_any_device_is_missing_and_floors_at_zero():
    """With two devices, both readings are required; a device sum above the total floors at zero."""
    house = [(datetime(2026, 1, 1, hour, tzinfo=timezone.utc), 3.0) for hour in range(6)]
    pump = {stamp: 1.0 for stamp, _ in house}
    car = {stamp: 0.5 for stamp, _ in house}
    car.pop(house[2][0])
    devices, _ = _parse_devices(_settings(_hp(), _ev()))
    split = split_device_load(house, devices, {HP_KEY: pump, EV_KEY: car}, min_samples=1)
    assert split.dropped_hours == 1
    assert all(value == pytest.approx(1.5) for _, value in split.base)
    assert split.clamped_hours == 0

    over = {stamp: 5.0 for stamp, _ in house}
    clamped = split_device_load(house, devices, {HP_KEY: over, EV_KEY: car}, min_samples=1)
    assert clamped.clamped_hours == 5  # every hour whose car reading exists exceeds the total
    assert [value for _, value in clamped.base] == [0.0] * 5


def test_device_meters_that_together_leave_too_few_hours_are_all_refused():
    """One meter down in the morning and another in the evening can empty the base together.

    Each device may clear the coverage floor on its own; the floor is checked again on the
    intersection, because a base learned from the few hours both happened to share is worse than no
    device modelled at all.
    """
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    house = [(start + timedelta(hours=index), 3.0) for index in range(100)]
    pump = {stamp: 1.0 for index, (stamp, _) in enumerate(house) if index < 60}
    car = {stamp: 0.5 for index, (stamp, _) in enumerate(house) if index >= 40}
    devices, _ = _parse_devices(_settings(_hp(), _ev()))
    split = split_device_load(house, devices, {HP_KEY: pump, EV_KEY: car}, min_samples=1)
    assert split.subtracted == []
    assert split.base == house
    assert split.dropped_hours == 0
    assert set(split.skipped) == {HP_KEY, EV_KEY}
    assert all("together with the other device meters" in reason for reason in split.skipped.values())


def test_a_stale_device_profile_is_not_added_to_a_base_that_never_subtracted_it(store):
    """The second guard against double counting: both the subtraction record and the profile.

    A device profile left on disk (device removed, then re-added) must not be added to a base that
    still contains the device. The base's ``devices_subtracted`` is what makes that decidable.
    """
    total, series, temperatures = _house(with_ev=False)
    settings = _settings(_hp())
    devices = load_devices(settings)
    update_load_profile(
        store,
        total,
        now=NOW,
        tz="UTC",
        temperatures=temperatures,
        devices=devices,
        device_samples={HP_KEY: series["heat_pump"]},
    )
    assert load_device_profile(store, HP_KEY) is not None

    # Re-learn the base without the device, exactly as a run without load.devices would.
    update_load_profile(store, total, now=NOW, tz="UTC", temperatures=temperatures)
    assert "devices_subtracted" not in load_profile(store)

    forecast, _ = load_forecast(settings, store, now=NOW, tz="UTC", temperature_c=_future(T_MEAN))
    assert any("not modelled" in note and "Heat pump" in note for note in forecast.notes)
    assert _slot_at_hour(forecast, 12).load_kw == pytest.approx(_base_kw(12) + _pump_kw(), abs=0.05)
    assert _slot_at_hour(forecast, 12).load_kw < _base_kw(12) + 2.0 * _pump_kw()


# ------------------------------------------------------------------------------------------------
# Per-device temperature terms
# ------------------------------------------------------------------------------------------------

def test_each_device_gets_its_own_temperature_term(store):
    """The base, the heat pump and the EV are three fits, not one compromise slope."""
    total, series, temperatures = _house()
    settings = _settings(_hp(), _ev())
    devices = load_devices(settings)
    update_load_profile(
        store,
        total,
        now=NOW,
        tz="UTC",
        temperatures=temperatures,
        devices=devices,
        device_samples={HP_KEY: series["heat_pump"], EV_KEY: series["ev"]},
    )

    base = load_profile(store)
    pump = load_device_profile(store, HP_KEY)
    car = load_device_profile(store, EV_KEY)
    # The house shape here does not answer to the weather, so its slope is noise near zero.
    assert base["b"] == pytest.approx(0.0, abs=0.01)
    # The heat pump does: it gains COLD_KWH_PER_DEGREE per degree below the mean.
    assert pump["b"] == pytest.approx(-COLD_KWH_PER_DEGREE, abs=0.005)
    assert pump["r2"] > 0.9 and pump["temperature_hours"] > 0
    # The EV charger is a schedule: no term is fitted even though the same temperatures are known.
    assert car["b"] == 0.0 and car["r2"] == 0.0 and car["temperature_hours"] == 0

    cold, _ = load_forecast(settings, store, now=NOW, tz="UTC", temperature_c=_future(0.0))
    warm, _ = load_forecast(settings, store, now=NOW, tz="UTC", temperature_c=_future(20.0))
    delta = cold.slots[0].load_kw - warm.slots[0].load_kw
    assert delta == pytest.approx(COLD_KWH_PER_DEGREE * 20.0, rel=0.15), (
        "only the heat pump answers the temperature; the EV's contribution must not move"
    )


def test_the_kind_decides_whether_a_slope_is_fitted(tmp_path):
    """The same weather-shaped meter gets a slope as a heat pump and none as an EV charger."""
    stamps = [
        (NOW - timedelta(days=28)).replace(hour=0, minute=0) + timedelta(hours=index)
        for index in range(28 * 24)
    ]
    # A device that draws weather-dependently at every hour of every day (the shape the regression
    # must find); the house around it is flat.
    device = {
        stamp: 0.5 + 0.05 * (12.0 - _temperature(index // 24))
        for index, stamp in enumerate(stamps)
    }
    temperatures = {stamp: _temperature(index // 24) for index, stamp in enumerate(stamps)}
    total = [(stamp, 1.0 + device[stamp]) for stamp in stamps]

    for kind, expected in (("heat_pump", -0.05), ("ev_charger", 0.0)):
        settings = _settings({"entity": HP, "kind": kind})
        devices = load_devices(settings)
        instance = Store(tmp_path / f"{kind}.db")
        try:
            update_load_profile(
                instance,
                total,
                now=NOW,
                tz="UTC",
                temperatures=temperatures,
                devices=devices,
                device_samples={HP_KEY: device},
            )
            profile = load_device_profile(instance, HP_KEY)
            assert profile["kind"] == kind
            assert profile["b"] == pytest.approx(expected, abs=0.005), kind
            if kind == "ev_charger":
                assert profile["r2"] == 0.0
        finally:
            instance.close()


def test_an_unobserved_device_hour_is_not_filled_from_the_household_shape(store):
    """The default residential shape describes an occupancy pattern, not one appliance.

    A device hour the meter never reported contributes nothing at forecast time; inventing an
    evening curve for a device would be a forecast the meter never showed. The house hours the
    missing readings cost are counted and reported, which is the honest price of the rule.
    """
    total, _series, temperatures = _house(with_heat_pump=False, with_ev=False)
    settings = _settings(_ev())
    devices = load_devices(settings)
    # The EV meter only ever reports from 00:00 to 13:59: 58% of the house's hours.
    readings = {stamp: 2.0 for stamp, _ in total if stamp.hour <= 13}
    total = [(stamp, value + readings.get(stamp, 0.0)) for stamp, value in total]
    profile = update_load_profile(
        store,
        total,
        now=NOW,
        tz="UTC",
        temperatures=temperatures,
        devices=devices,
        device_samples={EV_KEY: readings},
    )
    assert profile["devices_subtracted"] == [EV_KEY]
    assert profile["devices_dropped_hours"] == 10 * 28
    device = load_device_profile(store, EV_KEY)
    # The evening bucket was never observed and is not filled from the household shape.
    assert device["buckets"][18]["median"] == 0.0
    assert DEFAULT_SHAPE[18] > 1.0
    # The house's own evening bucket is empty for the same reason, and the forecast says so.
    forecast, _ = load_forecast(settings, store, now=NOW, tz="UTC")
    assert any("left out of the base profile" in note for note in forecast.notes)


# ------------------------------------------------------------------------------------------------
# Collection and wiring
# ------------------------------------------------------------------------------------------------

class FakeClient:
    """Minimal stand-in for the HA REST client: only ``get_state`` and ``get_history`` are read."""

    def __init__(self, states=None, history=None):
        self.states = states or {}
        self.history = history or {}
        self.calls = []

    def get_state(self, entity_id):
        return self.states[entity_id]

    def get_history(self, entity_id, start, end):
        self.calls.append(entity_id)
        if isinstance(self.history.get(entity_id), Exception):
            raise self.history[entity_id]
        return self.history.get(entity_id, [])


def _rows(entity_id: str, values) -> list[list[dict]]:
    """Recorder rows (watts) for an ``(stamp, kW)`` series, as ``GET /api/history/period`` returns."""
    return [
        [
            {
                "entity_id": entity_id,
                "state": str(value * 1000.0),
                "last_changed": stamp.isoformat(),
                "attributes": {"unit_of_measurement": "W"},
            }
        ]
        for stamp, value in values
    ]


def test_collect_history_reads_every_device_meter_and_reports_a_silent_one():
    start = NOW - timedelta(hours=6)
    ticks = [start + timedelta(hours=index) for index in range(6)]
    client = FakeClient(
        states={LOAD: {"attributes": {"unit_of_measurement": "W"}}},
        history={
            LOAD: _rows(LOAD, [(stamp, 1.5) for stamp in ticks]),
            HP: _rows(HP, [(stamp, 0.4) for stamp in ticks]),
            EV: RuntimeError("MeterBoom"),
        },
    )
    settings = Settings(None, options={"load.entity": LOAD, "load.devices": [_hp(), _ev()]})
    result = collect_history(settings, client, now=NOW, days=1)
    assert result.ok and result.source == "history"
    assert [device.key for device in result.devices] == [HP_KEY, EV_KEY]
    assert set(result.device_samples) == {HP_KEY}
    assert all(value == pytest.approx(0.4) for value in result.device_samples[HP_KEY].values())
    assert any("EV charger" in note and "MeterBoom" in note for note in result.notes)


def test_the_collection_result_feeds_learning_and_forecasting(store):
    """The whole path with fake recorder data: collect -> subtract -> learn -> forecast."""
    days = 28
    total, series, _temperatures = _house(days, with_ev=False)
    client = FakeClient(
        states={LOAD: {"attributes": {"unit_of_measurement": "W"}}},
        history={
            LOAD: _rows(LOAD, total),
            HP: _rows(HP, sorted(series["heat_pump"].items())),
        },
    )
    settings = Settings(None, options={"load.entity": LOAD, "load.devices": [_hp()]})
    result = collect_history(settings, client, now=NOW, days=days)
    assert result.ok and set(result.device_samples) == {HP_KEY}

    profile = update_load_profile(
        store,
        result.samples,
        now=NOW,
        tz="UTC",
        devices=result.devices,
        device_samples=result.device_samples,
    )
    assert profile["devices_subtracted"] == [HP_KEY]
    forecast, status = load_forecast(settings, store, now=NOW, tz="UTC")
    assert status.ok
    assert _slot_at_hour(forecast, 12).load_kw == pytest.approx(_base_kw(12) + _pump_kw(), abs=0.05)
    assert any("1 modelled" in note for note in forecast.notes)


# ------------------------------------------------------------------------------------------------
# Configuration: shape, validation, cross-key guard and the UI contract
# ------------------------------------------------------------------------------------------------

def test_the_kind_table_is_shared_between_config_and_the_model():
    """One closed set, two views of it: the config's choices and the model's labels/physics."""
    assert set(LOAD_DEVICE_KINDS) == set(DEVICE_KIND_LABELS)
    assert set(DEVICE_TEMPERATURE_KINDS) == {"heat_pump", "water_heater", "pool_pump"}
    assert "ev_charger" not in DEVICE_TEMPERATURE_KINDS


def test_config_round_trips_the_device_list(store):
    settings = Settings(store)
    entries = [_hp(), {"entity": EV, "kind": "ev_charger"}]
    applied, errors = settings.update({"load.devices": entries})
    assert applied == ["load.devices"] and errors == []
    assert settings.get("load.devices") == entries

    row = {row["key"]: row for row in setting_descriptors(settings)}["load.devices"]
    assert row["type"] == "json" and row["group"] == "load" and row["advanced"] is True
    # The UI's kind dropdown is fed from these choices, so the two lists cannot drift apart.
    assert row["choices"] == list(LOAD_DEVICE_KINDS)

    devices = load_devices(settings)
    assert [(d.key, d.entity, d.kind, d.name, d.temperature) for d in devices] == [
        (HP_KEY, HP, "heat_pump", "Heat pump", True),
        (EV_KEY, EV, "ev_charger", "EV charger", False),
    ]


@pytest.mark.parametrize(
    "value, message",
    [
        ("nope", "must be a list"),
        ([["not-an-object"]], "must be an object"),
        ([{"entity": HP, "kind": "heat_pump", "weight": 1}], "unknown keys"),
        ([{"kind": "heat_pump"}], "entity is required"),
        ([{"entity": "Heat pump", "kind": "heat_pump"}], "whitespace"),
        ([{"entity": HP, "kind": "boiler"}], "kind must be one of"),
        ([_hp(), {"entity": HP, "kind": "ev_charger"}], "listed twice"),
    ],
)
def test_config_rejects_a_malformed_device_list(store, value, message):
    settings = Settings(store)
    applied, errors = settings.update({"load.devices": value})
    assert applied == []
    assert errors and message in errors[0]["message"], errors


def test_the_house_meter_cannot_be_one_of_its_own_devices():
    """Subtracting the total from itself would leave a base of zeros and a plausible-looking device."""
    house = _settings(house=LOAD)
    with pytest.raises(ConfigError):
        house.set("load.devices", [{"entity": LOAD, "kind": "heat_pump"}])

    # The same rule through a patch that moves both keys at once, judged on the final state.
    settings = Settings(None, options={})
    applied, errors = settings.update(
        {"load.entity": LOAD, "load.devices": [{"entity": LOAD, "kind": "heat_pump"}]}
    )
    assert "load.devices" not in applied
    assert any("cannot be the house total" in error["message"] for error in errors)

    # And the model refuses the same shape when it arrives through unvalidated add-on options.
    devices, problems = _parse_devices(
        _settings({"entity": LOAD, "kind": "heat_pump"}, house=LOAD)
    )
    assert devices == []
    assert problems and "house meter" in problems[0]


def test_an_options_file_device_list_is_parsed_defensively():
    """Add-on options bypass the validator, so duplicates and unknown kinds must degrade safely."""
    settings = _settings(_hp(), _hp(), {"entity": EV, "kind": "boiler"})
    devices, problems = _parse_devices(settings)
    assert [device.key for device in devices] == [HP_KEY, EV_KEY]
    assert any("repeats a meter" in problem for problem in problems)
    assert any("unknown kind" in problem for problem in problems)
    # An unknown kind is still subtracted (it is a meter the total contains) but gets no term.
    assert devices[1].temperature is False
    assert device_config_problems(settings) == problems


def test_the_daily_maintenance_run_subtracts_the_devices(store, monkeypatch):
    """``main.build_maintenance`` is the only caller wiring collection to learning (SPEC 7.2).

    The device meters are read inside ``collect_history`` and ride on the result into
    ``update_load_profile``; if that wiring is ever dropped, the base profile silently becomes the
    whole total again and every device is double-counted — the failure this feature exists to stop.
    """
    import app.haws as haws
    import app.main as main

    class NoWebSocket:
        """The WebSocket tier is unavailable here; the recorder tier must carry the collection."""

        def __enter__(self):
            raise RuntimeError("no websocket in this test")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(haws, "HAWebSocket", NoWebSocket)
    total, series, _temperatures = _house(with_ev=False)
    client = FakeClient(
        states={LOAD: {"attributes": {"unit_of_measurement": "W"}}},
        history={LOAD: _rows(LOAD, total), HP: _rows(HP, sorted(series["heat_pump"].items()))},
    )
    settings = Settings(store, options={"load.entity": LOAD, "load.devices": [_hp()]})
    maintenance = main.build_maintenance(settings, store, client, timezone_name="UTC")
    maintenance(NOW)

    profile = load_profile(store)
    assert profile["devices_subtracted"] == [HP_KEY]
    assert profile["buckets"][12]["median"] == pytest.approx(_base_kw(12), abs=0.03)
    forecast, status = load_forecast(settings, store, now=NOW, tz="UTC")
    assert status.detail["devices_modelled"] == 1
    assert _slot_at_hour(forecast, 12).load_kw == pytest.approx(_base_kw(12) + _pump_kw(), abs=0.05)


def test_a_kind_change_drops_the_temperature_term_until_the_next_learning_run(store):
    """The kind decides the term, at serving time as well as at learning time.

    A device re-configured from heat pump to EV charger keeps its stored profile (same meter, same
    key) until the next nightly run. Applying the stored slope in the meantime would contradict the
    configured kind, so the term is dropped immediately and a note says the profile is waiting for
    the next learning run. The level — the part the subtraction made safe — keeps being used.
    """
    total, series, temperatures = _house(with_ev=False)
    as_pump = _settings(_hp())
    devices = load_devices(as_pump)
    update_load_profile(
        store,
        total,
        now=NOW,
        tz="UTC",
        temperatures=temperatures,
        devices=devices,
        device_samples={HP_KEY: series["heat_pump"]},
    )
    pump = load_device_profile(store, HP_KEY)
    assert pump["b"] == pytest.approx(-COLD_KWH_PER_DEGREE, abs=0.005)

    as_car = _settings({"entity": HP, "kind": "ev_charger"})  # the same meter, reconfigured
    cold, _ = load_forecast(as_car, store, now=NOW, tz="UTC", temperature_c=_future(0.0))
    warm, _ = load_forecast(as_car, store, now=NOW, tz="UTC", temperature_c=_future(20.0))
    assert cold.slots[0].load_kw == pytest.approx(warm.slots[0].load_kw, abs=1e-9), (
        "the stored heat-pump slope was applied to a device now configured as an EV charger"
    )
    assert any("was last learned as 'heat_pump'" in note for note in cold.notes)
    # The device's level is still added back above the base (it was subtracted from it).
    assert cold.slots[0].load_kw == pytest.approx(_base_kw(0) + _pump_kw(), abs=0.05)
