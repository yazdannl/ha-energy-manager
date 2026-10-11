"""WP2A: Forecast.Solar hourly/single-plane contract, cache, 429 back-off, bias (SPEC §7.1, App. A)."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "energy_manager") not in sys.path:
    sys.path.insert(0, str(ROOT / "energy_manager"))

from app.config import Settings  # noqa: E402
from app.harest import HAError  # noqa: E402
from app.models import ConfigError, ProviderStatus  # noqa: E402
from app.sources import ProviderError, RateLimited  # noqa: E402
from app.sources.solar import (  # noqa: E402
    BACKOFF_KEY,
    BACKOFF_KIND,
    DERIVED_MIN_SAMPLES,
    NASA_FAILURE_BACKOFF_HOURS,
    NASA_KIND,
    NASA_PROFILE_KEY,
    _capacity_geometry,
    _implied_capacity_wp,
    _interpolate,
    _nasa_cache,
    _parse_watts,
    _plane_irradiance,
    fetch_nasa_hourly,
    fetch_plane,
    load_solar_forecast,
    nasa_clearness_profile,
    solar_bias_factors,
    update_solar_bias,
)
from app.store import Store  # noqa: E402

NOW = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)
PLANE = {"kwp": 5.0, "tilt_deg": 35.0, "azimuth_deg": 180.0, "damping": 0.4}


def watts_payload(watts: float, *, hours: int = 6, start: datetime | None = None, ramp: bool = True) -> dict:
    """Hourly rows in the ``?time=utc`` shape, with the private ``message.info`` block included."""
    origin = (start or NOW).replace(minute=0, second=0, microsecond=0)
    table = {
        (origin + timedelta(hours=index)).isoformat(): watts + (index if ramp else 0)
        for index in range(hours + 1)
    }
    return {
        "result": {
            "watts": table,
            "watt_hours_period": {key: value for key, value in table.items()},
            "watt_hours_day": {origin.date().isoformat(): watts * hours},
        },
        "message": {
            "code": 0,
            "text": "",
            "info": {
                "latitude": 52.0,
                "longitude": 5.0,
                "place": "Example Street 1",
                "timezone": "Europe/Amsterdam",
            },
            "ratelimit": {"period": 3600, "limit": 12, "remaining": 10},
        },
    }


class HttpStub:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.urls = []

    def __call__(self, url, **kwargs):
        self.urls.append(url)
        item = self.responses.pop(0) if self.responses else watts_payload(0.0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture()
def store(tmp_path):
    instance = Store(tmp_path / "solar.db")
    yield instance
    instance.close()


def solar_settings(store=None, **overrides):
    options = {
        "solar.enabled": True,
        "solar.planes": [PLANE],
        "solar.bias_correction": False,
        "solar.nasa_irradiance": False,
    }
    options.update(overrides)
    return Settings(store, options=options)


def test_interpolate_hourly_curve_onto_quarter_grid():
    series = [(NOW, 0.0), (NOW + timedelta(hours=1), 1000.0), (NOW + timedelta(hours=2), 400.0)]
    assert _interpolate(series, NOW - timedelta(hours=1)) == 0.0  # before the series
    assert _interpolate(series, NOW) == pytest.approx(0.0)
    assert _interpolate(series, NOW + timedelta(minutes=15)) == pytest.approx(250.0)
    assert _interpolate(series, NOW + timedelta(minutes=30)) == pytest.approx(500.0)
    assert _interpolate(series, NOW + timedelta(hours=1, minutes=30)) == pytest.approx(700.0)
    assert _interpolate(series, NOW + timedelta(hours=5)) == pytest.approx(400.0)  # after the series


def test_interpolate_keeps_first_partial_sample_anchored():
    # sunrise fill rows are irregular: 07:29 then 08:00; the ramp must start at 07:29, not at 08:00.
    first = NOW.replace(hour=7, minute=29, second=0, microsecond=0)
    series = [(first, 0.0), (first + timedelta(minutes=31), 1550.0)]
    assert _interpolate(series, first) == pytest.approx(0.0)
    assert _interpolate(series, first + timedelta(minutes=15, seconds=30)) == pytest.approx(775.0)
    assert _interpolate(series, first + timedelta(minutes=31)) == pytest.approx(1550.0)


def test_parse_watts_tolerates_offsetless_keys_as_utc():
    payload = {"result": {"watts": {"2026-01-05 12:00:00": 100, "2026-01-05T13:00:00+01:00": 200}}}
    series = _parse_watts(payload)
    assert series == [
        (datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc), 100.0),
        (datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc), 200.0),
    ]


def test_fetch_plane_url_time_utc_azimuth_and_privacy():
    http = HttpStub(watts_payload(1234.0))
    payload = fetch_plane(52.0, 5.0, PLANE, api_key="personal-key", http=http)
    url = http.urls[0]
    assert url.startswith("https://api.forecast.solar/personal-key/estimate/52.0000/5.0000/35/0/5?")
    assert "time=utc" in url
    assert "damping_morning=0.4" in url and "damping_evening=0.4" in url
    assert payload["result"]["watts"]
    assert "info" not in payload["message"]  # coordinates/place are never returned or cached
    assert "Example Street 1" not in repr(payload)

    # compass azimuth: 0 = north -> -180; 90 = east -> -90; 270 = west -> +90
    for compass, expected in ((0.0, "-180"), (90.0, "-90"), (270.0, "90"), (180.0, "0")):
        probe = HttpStub(watts_payload(1.0))
        fetch_plane(52.0, 5.0, {**PLANE, "azimuth_deg": compass}, http=probe)
        assert f"/{expected}/5?" in probe.urls[0]


def test_fetch_plane_validates_and_removes_bad_damping():
    bad = HttpStub({"result": {}, "message": {"code": 1, "text": "invalid key"}})
    with pytest.raises(ProviderError) as err:
        fetch_plane(52.0, 5.0, PLANE, http=bad)
    assert "invalid key" in str(err.value)
    with pytest.raises(ProviderError):
        fetch_plane(52.0, 5.0, {**PLANE, "kwp": 0}, http=HttpStub({}))
    # a rejected damping value is dropped and the request retried once (appendix A item 4)
    retry = HttpStub(ProviderError("bad damping", status=400), watts_payload(10.0))
    fetch_plane(52.0, 5.0, {**PLANE, "damping": 5.0}, http=retry)
    assert len(retry.urls) == 2
    assert "damping" not in retry.urls[1] and "time=utc" in retry.urls[1]


def test_load_solar_keyless_single_plane_warns(store):
    http = HttpStub(watts_payload(1000.0), watts_payload(500.0))
    settings = solar_settings(store, **{"solar.planes": [PLANE, {**PLANE, "kwp": 3.0}]})
    forecast, status = load_solar_forecast(settings, store, None, location=(52.0, 5.0, "UTC"), now=NOW, http=http)
    assert status.ok is True
    assert len(http.urls) == 1  # keyless: never one request per plane
    assert status.detail["planes_used"] == 1 and status.detail["planes_ignored"] == 1
    assert any("single-plane" in note for note in forecast.notes)
    assert len(forecast.planes) == 1
    assert len(forecast.slots) == settings.get("optimizer.horizon_hours") * 4
    assert forecast.slots[0].t == NOW
    assert forecast.slots[0].pv_kw == pytest.approx(1.0)
    assert forecast.slots[0].uncertainty_pct == pytest.approx(25.0)


def test_load_solar_keyed_sums_planes(store):
    http = HttpStub(watts_payload(1000.0), watts_payload(500.0))
    settings = solar_settings(
        store, **{"solar.planes": [PLANE, {**PLANE, "kwp": 3.0}], "solar.api_key": "personal-key"}
    )
    forecast, status = load_solar_forecast(settings, store, None, location=(52.0, 5.0, "UTC"), now=NOW, http=http)
    assert len(http.urls) == 2 and all(url.startswith("https://api.forecast.solar/personal-key/") for url in http.urls)
    assert forecast.slots[0].pv_kw == pytest.approx(1.5)
    assert status.detail["planes_used"] == 2


def test_load_solar_caches_on_the_15_minute_grid(store):
    http = HttpStub(watts_payload(1000.0))
    settings = solar_settings(store)
    first, _ = load_solar_forecast(settings, store, None, location=(52.0, 5.0, "UTC"), now=NOW, http=http)
    cached, _ = load_solar_forecast(
        settings, store, None, location=(52.0, 5.0, "UTC"), now=NOW + timedelta(minutes=29), http=http
    )
    assert len(http.urls) == 1
    assert first.slots[0].pv_kw == pytest.approx(1.0)
    # hourly 1000 -> 1001 W is a linear ramp: +0.25 W at 12:15
    assert first.slots[1].pv_kw == pytest.approx(1000.25 / 1000.0)
    assert cached.slots[0].pv_kw == pytest.approx(1000.25 / 1000.0)


def test_rate_limit_backoff_serves_cache(store):
    settings = solar_settings(store)
    cache_http = HttpStub(watts_payload(800.0, ramp=False))
    first, status = load_solar_forecast(settings, store, None, location=(52.0, 5.0, "UTC"), now=NOW, http=cache_http)
    assert status.ok and first.slots[0].pv_kw == pytest.approx(0.8)

    limited = HttpStub(RateLimited("too many requests"))
    later = NOW + timedelta(minutes=31)
    served, status = load_solar_forecast(settings, store, None, location=(52.0, 5.0, "UTC"), now=later, http=limited)
    assert len(limited.urls) == 1
    assert served.slots[0].pv_kw == pytest.approx(0.8)
    assert any("429" in note for note in served.notes)
    backoff = store.profile_get(BACKOFF_KIND, BACKOFF_KEY)[1]
    assert backoff["failures"] == 1 and backoff["until"] is not None

    def explode(url, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("HTTP call during back-off")

    served, _ = load_solar_forecast(
        settings, store, None, location=(52.0, 5.0, "UTC"), now=later + timedelta(seconds=30), http=explode
    )
    assert served.slots[0].pv_kw == pytest.approx(0.8)
    assert any("back-off" in note for note in served.notes)


def test_rate_limit_without_cache_reports_failure(store):
    settings = solar_settings(store)
    forecast, status = load_solar_forecast(
        settings, store, None, location=(52.0, 5.0, "UTC"), now=NOW, http=HttpStub(RateLimited("429"))
    )
    assert status.ok is False and status.stale is True
    assert forecast.slots == []


def test_bias_correction_is_applied_and_clamped(store):
    http = HttpStub(watts_payload(1000.0))
    settings = solar_settings(store, **{"solar.bias_correction": True})
    update_solar_bias(store, [(NOW, 1.0, 0.5)], now=NOW, tz="UTC")
    factors = solar_bias_factors(store, now=NOW)
    assert factors[12] == pytest.approx(0.9)
    assert factors[3] == pytest.approx(1.0)
    forecast, status = load_solar_forecast(settings, store, None, location=(52.0, 5.0, "UTC"), now=NOW, http=http)
    assert status.ok
    assert forecast.slots[0].pv_kw == pytest.approx(1.0 * 0.9)
    total = settings.get("optimizer.horizon_hours") * 4
    assert forecast.correction_applied == pytest.approx((8 * 0.9 + (total - 8) * 1.0) / total)
    assert status.detail["bias_correction"] is True

    update_solar_bias(store, [(NOW, 1.0, 1000.0)], now=NOW, tz="UTC")
    assert all(0.3 <= factor <= 1.8 for factor in solar_bias_factors(store, now=NOW))


def test_disabled_missing_location_and_no_planes(store):
    disabled = solar_settings(store, **{"solar.enabled": False})
    forecast, status = load_solar_forecast(disabled, store, None, location=(52.0, 5.0, "UTC"), now=NOW)
    assert status.active is False and status.ok is True and forecast.slots == []
    assert "disabled" in " ".join(forecast.notes)

    no_planes = solar_settings(store, **{"solar.planes": []})
    _, status = load_solar_forecast(no_planes, store, None, location=(52.0, 5.0, "UTC"), now=NOW)
    assert status.ok is True and status.detail["reason"] == "no planes configured"

    forecast, status = load_solar_forecast(solar_settings(store), store, None, location=None, now=NOW)
    assert status.ok is False and forecast.slots == []
    assert "location unavailable" in status.last_error


# ----------------------------------------------------------------------------------- capacity source
# SPEC.md section 7.1.1: an AC-coupled array's production estimated from ``solar.capacity_wp`` by
# astronomy (declination / equation of time / hour angle), Haurwitz clear sky, isotropic
# transposition and optional Kasten-Czeplak cloud attenuation. No network: the weather entity is
# answered by a stub and the location comes from the caller.


#: A weather entity id in the audit-clean ``example_`` namespace (SPEC.md R1).
WEATHER_ENTITY = "weather.example_forecast"
CAPACITY_DEFAULTS = {
    "solar.source": "capacity",
    "solar.planes": [],
    "solar.capacity_wp": 4000.0,
    "solar.bias_correction": False,
    # NASA POWER is network I/O; only the dedicated NASA tests turn it on, with a stub.
    "solar.nasa_irradiance": False,
    "optimizer.horizon_hours": 24,
}
TEST_LOCATION = (52, 5, "UTC")  # integers: no coordinate shape anywhere in this file


def capacity_settings(store=None, **overrides):
    options = dict(CAPACITY_DEFAULTS)
    options.update(overrides)
    return Settings(store, options=options)


def load_capacity(settings, store=None, client=None, *, location=TEST_LOCATION, now=NOW, http=None):
    # Default the store to the one the settings were built with, so a one-argument call still sees
    # whatever bias learning that store holds.
    if store is None:
        store = getattr(settings, "store", None)
    return load_solar_forecast(settings, store, client, location=location, now=now, http=http)


def slot_at(forecast, moment):
    return next(slot for slot in forecast.slots if slot.t == moment)


class WeatherStub:
    """Answers the two calls the capacity source makes, with no network involved.

    ``forecast`` mimics ``weather.get_forecasts`` hourly rows; ``error`` mimics an integration that
    rejects the action (HTTP 400 "does not support"); ``current_cloud`` is the entity's
    ``cloud_coverage`` attribute.
    """

    def __init__(
        self,
        *,
        entity=WEATHER_ENTITY,
        forecast=None,
        current_cloud=None,
        error=None,
        wrap=True,
        zone="UTC",
    ):
        self.entity = entity
        self.forecast = list(forecast or [])
        self.current_cloud = current_cloud
        self.error = error
        self.wrap = wrap
        self.zone = zone
        self.calls = []

    def get_config(self):
        return {"latitude": 52, "longitude": 5, "time_zone": self.zone}

    def get_state(self, entity_id):
        if entity_id != self.entity:
            return None
        attributes = {} if self.current_cloud is None else {"cloud_coverage": self.current_cloud}
        return {"entity_id": entity_id, "state": "cloudy", "attributes": attributes}

    def call_service(self, domain, service, data=None, return_response=False):
        self.calls.append((domain, service, dict(data or {}), return_response))
        if self.error is not None:
            raise self.error
        inner = {self.entity: {"forecast": [dict(entry) for entry in self.forecast]}}
        if self.wrap:  # the REST ``?return_response`` wrapper (docs/EXTERNAL_APIS.md section 4)
            return {"changed_states": [], "service_response": inner}
        return inner


def hourly_forecast(coverage, *, start=NOW):
    """Hourly rows shaped like a weather integration's forecast, one value per hour."""
    return [
        {
            "datetime": (start + timedelta(hours=index)).isoformat(),
            "condition": "cloudy",
            "cloud_coverage": value,
        }
        for index, value in enumerate(coverage)
    ]


def kasten_czeplak(cloud_pct):
    """The documented attenuation, computed here independently of the implementation."""
    return 1.0 - 0.75 * (cloud_pct / 100.0) ** 3.4


UNSUPPORTED_FORECAST = HAError(
    "weather.get_forecasts failed: Entity weather.example_forecast does not support action weather.get_forecasts",
    status=400,
)


def test_capacity_night_slots_are_exactly_zero(store):
    # NOW is a January day at 52 N: the sun is below the horizon before 08:00 and after 15:30.
    forecast, status = load_capacity(capacity_settings(store))
    assert status.ok and forecast.slots
    dark = [slot for slot in forecast.slots if slot.t.hour <= 7 or slot.t.hour >= 16]
    assert dark
    assert all(slot.pv_kw == 0.0 for slot in dark)
    assert any(slot.pv_kw > 0.0 for slot in forecast.slots)
    # no weather entity: clear-sky band (SPEC 7.1.1)
    assert all(slot.uncertainty_pct == 35.0 for slot in forecast.slots)


def test_capacity_curve_rises_to_a_peak_near_local_solar_noon_and_falls(store):
    forecast, _ = load_capacity(capacity_settings(store))
    per_hour = {}
    for slot in forecast.slots:
        per_hour[slot.t.hour] = per_hour.get(slot.t.hour, 0.0) + slot.pv_kw
    peak = max(per_hour, key=lambda hour: per_hour[hour])
    assert 10 <= peak <= 13, peak  # solar noon is ~11:40 for longitude 5
    assert per_hour[8] < per_hour[peak] and per_hour[14] < per_hour[peak]
    assert slot_at(forecast, NOW).pv_kw > 0.0


def test_capacity_south_facing_array_beats_north_facing(store):
    latitude, longitude, zone_name = TEST_LOCATION
    south, _ = load_capacity(capacity_settings(store, **{"solar.azimuth_deg": 180.0}))
    north, _ = load_capacity(capacity_settings(store, **{"solar.azimuth_deg": 0.0}))
    assert south.total_kwh() > north.total_kwh()
    assert slot_at(south, NOW).pv_kw > slot_at(north, NOW).pv_kw


def test_capacity_slot_energy_is_monotonic_in_capacity_and_performance_ratio(store):
    noon = [
        slot_at(load_capacity(capacity_settings(store, **{"solar.capacity_wp": wp}))[0], NOW).pv_kw
        for wp in (500, 1000, 2000, 4000)
    ]
    assert noon == sorted(noon) and noon[0] < noon[-1]
    noon = [
        slot_at(load_capacity(capacity_settings(store, **{"solar.performance_ratio": pr}))[0], NOW).pv_kw
        for pr in (0.6, 0.75, 0.9, 1.0)
    ]
    assert noon == sorted(noon) and noon[0] < noon[-1]


def test_capacity_cloud_attenuation_at_zero_fifty_and_hundred(store):
    clear, _ = load_capacity(capacity_settings(store))
    noon_clear = slot_at(clear, NOW).pv_kw
    seen = []
    for cloud_pct in (0, 25, 50, 75, 100):
        client = WeatherStub(forecast=hourly_forecast([cloud_pct] * 24))
        settings = capacity_settings(store, **{"solar.weather_entity": WEATHER_ENTITY})
        forecast, status = load_capacity(settings, store, client)
        assert status.ok
        value = slot_at(forecast, NOW).pv_kw
        assert value == pytest.approx(noon_clear * kasten_czeplak(cloud_pct))
        # cloud data narrows the band to 25 percent (SPEC 7.1.1)
        assert slot_at(forecast, NOW).uncertainty_pct == 25.0
        seen.append(value)
    assert seen == sorted(seen, reverse=True)
    # cc = 0 must be exactly the clear-sky estimate, not an attenuation rounded to it
    client = WeatherStub(forecast=hourly_forecast([0] * 24))
    settings = capacity_settings(store, **{"solar.weather_entity": WEATHER_ENTITY})
    forecast, _ = load_capacity(settings, store, client)
    assert slot_at(forecast, NOW).pv_kw == noon_clear


def test_capacity_hourly_cloud_coverage_maps_onto_the_four_slots_of_each_hour(store):
    # Only the 12:00 hour publishes coverage, so exactly the four slots of that hour may be
    # attenuated: a shifted window would show up as a ratio in the neighbouring hours.
    clear, _ = load_capacity(capacity_settings(store), store)
    rows = hourly_forecast([80] + [None] * 23)
    client = WeatherStub(forecast=rows)
    settings = capacity_settings(store, **{"solar.weather_entity": WEATHER_ENTITY})
    forecast, status = load_capacity(settings, store, client)
    assert status.ok
    ratios = {
        slot.t: slot.pv_kw / plain.pv_kw
        for slot, plain in zip(forecast.slots, clear.slots)
        if plain.pv_kw > 0.0
    }
    covered = [moment for moment in ratios if moment.hour == 12]
    assert len(covered) == 4  # one value per slot of the hour, none outside it
    for moment, ratio in ratios.items():
        assert ratio == pytest.approx(kasten_czeplak(80) if moment.hour == 12 else 1.0)
    # Two hours with different values keep their own factor.
    client = WeatherStub(forecast=hourly_forecast([80, 10] + [None] * 22))
    forecast, _ = load_capacity(settings, store, client)
    by_hour = {}
    for slot, plain in zip(forecast.slots, clear.slots):
        if plain.pv_kw > 0.0 and slot.t.hour in (12, 13):
            by_hour.setdefault(slot.t.hour, set()).add(round(slot.pv_kw / plain.pv_kw, 9))
    assert len(by_hour[12]) == len(by_hour[13]) == 1
    assert by_hour[12] == {round(kasten_czeplak(80), 9)}
    assert by_hour[13] == {round(kasten_czeplak(10), 9)}


def test_capacity_weather_without_cloud_coverage_falls_back_to_clear_sky_with_a_note(store):
    rows = [
        {"datetime": (NOW + timedelta(hours=index)).isoformat(), "condition": "sunny"}
        for index in range(24)
    ]
    client = WeatherStub(forecast=rows)
    settings = capacity_settings(store, **{"solar.weather_entity": WEATHER_ENTITY})
    forecast, status = load_capacity(settings, store, client)
    assert status.ok
    assert any("cloud_coverage" in note for note in forecast.notes)
    assert all(slot.uncertainty_pct == 35.0 for slot in forecast.slots)
    clear, _ = load_capacity(capacity_settings(store))
    assert [slot.pv_kw for slot in forecast.slots] == [slot.pv_kw for slot in clear.slots]


def test_capacity_weather_400_holds_the_current_cloud_cover_flat_with_a_note(store):
    for current_cloud, attenuated in ((80.0, True), (None, False)):
        client = WeatherStub(error=UNSUPPORTED_FORECAST, current_cloud=current_cloud)
        settings = capacity_settings(store, **{"solar.weather_entity": WEATHER_ENTITY})
        forecast, status = load_capacity(settings, store, client)
        assert status.ok  # weather being unavailable never fails the forecast
        assert "get_forecasts" in client.calls[0][1] and client.calls[0][3] is True
        if attenuated:
            assert any("does not support weather.get_forecasts" in note for note in forecast.notes)
            assert any("held flat" in note for note in forecast.notes)
            assert any(WEATHER_ENTITY in note for note in forecast.notes)
            assert slot_at(forecast, NOW).pv_kw == pytest.approx(
                slot_at(load_capacity(capacity_settings(store))[0], NOW).pv_kw
                * kasten_czeplak(current_cloud)
            )
        else:
            assert any("cloud_coverage" in note for note in forecast.notes)
            assert any("clear-sky" in note for note in forecast.notes)


def test_capacity_assumed_geometry_is_stated_only_when_unset(store):
    forecast, _ = load_capacity(capacity_settings(store))
    assert any(
        "assumed south-facing, 35 degree tilt; set solar.tilt_deg / solar.azimuth_deg to refine"
        in note
        for note in forecast.notes
    )
    forecast, _ = load_capacity(capacity_settings(store, **{"solar.tilt_deg": 30.0, "solar.azimuth_deg": 90.0}))
    assert not any("assumed" in note for note in forecast.notes)
    forecast, _ = load_capacity(capacity_settings(store, **{"solar.tilt_deg": 30.0}))
    assert any("assumed south-facing" in note for note in forecast.notes)
    assert not any("35 degree tilt" in note for note in forecast.notes)


def test_capacity_notes_name_capacity_ratio_weather_and_the_measured_entity(store):
    client = WeatherStub(forecast=hourly_forecast([40] * 24))
    settings = capacity_settings(
        store,
        **{
            "solar.weather_entity": WEATHER_ENTITY,
            "solar.capacity_wp": 6000,
            "solar.measured_entity": "sensor.example_pv",
        },
    )
    forecast, status = load_capacity(settings, store, client)
    joined = " ".join(forecast.notes)
    assert "6000 Wp" in joined
    assert "solar.capacity_wp" in joined
    assert "0.85" in joined and "solar.performance_ratio" in joined
    assert WEATHER_ENTITY in joined and "attenuation" in joined
    assert "measured" in joined and "solar.measured_entity" in joined
    assert status.detail["capacity_wp"] == 6000


def test_capacity_measured_entity_empty_warns_in_the_note(store):
    forecast, _ = load_capacity(capacity_settings(store, **{"solar.measured_entity": ""}))
    assert any("solar.measured_entity is not set" in note for note in forecast.notes)


def test_capacity_missing_capacity_wp_is_a_failure_that_names_the_setting(store):
    settings = capacity_settings(store, **{"solar.capacity_wp": None})
    forecast, status = load_capacity(settings, store)
    assert forecast.slots == []
    assert status.ok is False
    assert status.detail["reason"]
    assert "solar.capacity_wp" in (status.last_error or "")
    assert any("solar.capacity_wp" in note for note in forecast.notes)


def test_capacity_missing_capacity_wp_is_explained_by_the_engine_hint(store):
    from app.engine import Engine

    settings = capacity_settings(store, **{"solar.capacity_wp": None})
    engine = Engine(settings, store=store, solar_source=lambda now: None)
    explanation = engine.explain_forecast("solar")
    assert explanation["reason"] == "no_pv_source"
    assert "solar.capacity_wp" in explanation["hint"]


def test_capacity_source_hint_and_location_hint(store):
    from app.engine import Engine

    no_planes = capacity_settings(store, **{"solar.source": "forecast_solar"})
    engine = Engine(no_planes, store=store, solar_source=lambda now: None)
    explanation = engine.explain_forecast("solar")
    assert explanation["reason"] == "no_pv_source"
    assert "solar.planes" in explanation["hint"]

    engine = Engine(capacity_settings(store), store=store, solar_source=lambda now: None)
    engine._solar = None  # noqa: SLF001 - the explanation is driven by the status map
    engine._provider_status[("solar", "capacity")] = ProviderStatus(
        kind="solar",
        name="capacity",
        active=True,
        ok=False,
        last_error="location unavailable: set the overrides or expose /api/config",
    )
    explanation = engine.explain_forecast("solar")
    assert explanation["reason"] == "provider_failed"
    assert "location" in explanation["hint"].lower()


def test_capacity_unknown_source_still_raises(store):
    with pytest.raises(ConfigError):
        load_capacity(capacity_settings(store, **{"solar.source": "magic"}), store)


def test_capacity_bias_learning_refines_the_estimate(store):
    update_solar_bias(store, [(NOW, 1.0, 0.5)], now=NOW, tz="UTC")
    learned, status = load_capacity(capacity_settings(store, **{"solar.bias_correction": True}))
    plain, _ = load_capacity(capacity_settings(store))
    assert learned.correction_applied != 1.0
    assert slot_at(learned, NOW).pv_kw == pytest.approx(slot_at(plain, NOW).pv_kw * 0.9)
    for corrected, uncorrected in zip(learned.slots, plain.slots):
        factor = 0.9 if uncorrected.t.hour == 12 else 1.0
        assert corrected.pv_kw == pytest.approx(uncorrected.pv_kw * factor)
    assert status.detail["correction_applied"] == round(learned.correction_applied, 4)
    assert status.detail["bias_correction"] is True


def test_capacity_slots_follow_the_configured_zone_across_dst(store):
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Europe/Amsterdam")
    spring = datetime(2026, 3, 29, 0, 0, tzinfo=timezone.utc)
    forecast, status = load_capacity(
        capacity_settings(store), store, None, location=(52, 5, "Europe/Amsterdam"), now=spring
    )
    assert status.ok
    assert len(forecast.slots) == 24 * 4
    local = [slot.t.astimezone(zone) for slot in forecast.slots]
    assert local == sorted(local)
    assert len({slot.t for slot in forecast.slots}) == len(local)  # no duplicated instant
    steps = [b - a for a, b in zip(local, local[1:])]
    skipped = [step for step in steps if step != timedelta(minutes=15)]
    assert len(skipped) == 1  # the wall clock jumps once, and the grid never duplicates a slot
    assert timedelta(hours=1) < skipped[0] < timedelta(hours=2)
    assert all(
        datetime(2026, 3, 29, 2, minute, tzinfo=zone) not in local for minute in (0, 15, 30, 45)
    )

    autumn = datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc)
    forecast, status = load_capacity(
        capacity_settings(store), store, None, location=(52, 5, "Europe/Amsterdam"), now=autumn
    )
    assert status.ok and len(forecast.slots) == 24 * 4
    assert len({slot.t for slot in forecast.slots}) == 24 * 4  # repeated wall clock, unique slots
    assert [slot.t.astimezone(zone).hour for slot in forecast.slots].count(2) == 8

    # a summer day peaks in the local afternoon, which only holds if the slots are built in the
    # configured zone (longitude 5 in UTC+2 puts solar noon at ~13:40 local)
    summer = datetime(2026, 6, 21, 0, 0, tzinfo=timezone.utc)
    forecast, _ = load_capacity(
        capacity_settings(store), store, None, location=(52, 5, "Europe/Amsterdam"), now=summer
    )
    per_local_hour = {}
    for slot in forecast.slots:
        hour = slot.t.astimezone(zone).hour
        per_local_hour[hour] = per_local_hour.get(hour, 0.0) + slot.pv_kw
    assert 12 <= max(per_local_hour, key=lambda hour: per_local_hour[hour]) <= 14
    assert {slot.t.astimezone(zone).hour for slot in forecast.slots} == set(range(24))


# ----------------------------------------------------------------------------------- derived source
# SPEC.md section 7.1.2: ``solar.source=derived`` learns the array from the meter's export moments
# (pv = export + battery net charge, house load not subtracted) and then uses the capacity model, so
# an AC-coupled array needs no watt peak, no planes and no geometry.

DERIVED_DEFAULTS = {
    "solar.source": "derived",
    "solar.planes": [],
    "solar.capacity_wp": None,
    "solar.bias_correction": False,
    # NASA POWER is network I/O; only the dedicated NASA tests turn it on, with a stub.
    "solar.nasa_irradiance": False,
    "optimizer.horizon_hours": 24,
    "grid.power_entity": "sensor.example_grid_power",
}
SYNTHETIC_WP = 3000.0
GRID_ENTITY = "sensor.example_grid_power"
CHARGE_ENTITY = "sensor.example_battery_charge"
DISCHARGE_ENTITY = "sensor.example_battery_discharge"


def derived_settings(store=None, **overrides):
    options = dict(DERIVED_DEFAULTS)
    options.update(overrides)
    return Settings(store, options=options)


def battery_mapping_entry(**mapping):
    return [
        {
            "id": "home",
            "name": "Home battery",
            "driver": "generic",
            "capacity_kwh": 10.0,
            "enabled": True,
            "mapping": mapping,
        }
    ]


def model_kw(moment, wp=SYNTHETIC_WP, settings=None):
    """The clear-sky model's output for ``wp`` at ``moment``, with the shipped assumptions."""
    tilt, azimuth, _, _ = _capacity_geometry(settings or derived_settings())
    poa = _plane_irradiance(52, 5, moment, timezone.utc, tilt, azimuth)
    return (wp / 1000.0) * (poa / 1000.0) * 0.85


def daylight_stamps(days=5):
    out = []
    for day_index in range(days):
        day = datetime(2025, 12, 29, tzinfo=timezone.utc) + timedelta(days=day_index)
        for hour in range(8, 17):
            out.append(day.replace(hour=hour))
    return out


class HistoryStub:
    """Answers ``get_history`` for the meter and the battery flow entities, with no network."""

    def __init__(self, series, zone="UTC"):
        self.series = series
        self.zone = zone
        self.calls = []

    def get_config(self):
        return {"latitude": 52, "longitude": 5, "time_zone": self.zone}

    def get_history(self, entity_id, start, end):
        self.calls.append(entity_id)
        rows = [
            {"state": str(value), "attributes": {"unit_of_measurement": "kW"}, "last_changed": stamp.isoformat()}
            for stamp, value in self.series.get(entity_id, [])
            if start <= stamp <= end
        ]
        return [rows]


def test_implied_capacity_recovers_a_synthetic_clear_sky_array():
    samples = [(stamp, model_kw(stamp)) for stamp in daylight_stamps()]
    learned, usable = _implied_capacity_wp(52, 5, timezone.utc, samples, derived_settings())
    assert usable >= DERIVED_MIN_SAMPLES
    assert learned == pytest.approx(SYNTHETIC_WP, rel=0.02)


def test_implied_capacity_uses_the_upper_envelope_when_clouds_lower_some_hours():
    samples = []
    for index, stamp in enumerate(daylight_stamps()):
        clear = model_kw(stamp)
        samples.append((stamp, clear if index % 2 else clear * 0.25))  # half the hours overcast
    learned, _ = _implied_capacity_wp(52, 5, timezone.utc, samples, derived_settings())
    assert learned == pytest.approx(SYNTHETIC_WP, rel=0.05)


def test_implied_capacity_needs_enough_daylight_samples():
    samples = [(stamp, model_kw(stamp)) for stamp in daylight_stamps(days=1)[:5]]
    learned, usable = _implied_capacity_wp(52, 5, timezone.utc, samples, derived_settings())
    assert learned == 0.0 and 0 < usable < DERIVED_MIN_SAMPLES


def test_derived_solar_learns_the_array_from_export_and_battery_flow(store):
    stamps = daylight_stamps()
    # The battery charges 0.5 kW while the meter exports the rest: grid = charge - pv.
    stub = HistoryStub(
        {
            GRID_ENTITY: [(stamp, 0.5 - model_kw(stamp)) for stamp in stamps],
            CHARGE_ENTITY: [(stamp, 0.5) for stamp in stamps],
        }
    )
    settings = derived_settings(
        store,
        batteries=battery_mapping_entry(
            charge_power_entity=CHARGE_ENTITY, discharge_power_entity=DISCHARGE_ENTITY
        ),
    )
    forecast, status = load_solar_forecast(settings, store, stub, location=TEST_LOCATION, now=NOW)
    assert status.ok, status.last_error
    assert status.detail["capacity_source"].startswith("the meter's export moments")
    assert status.detail["derived_samples"] >= DERIVED_MIN_SAMPLES
    assert forecast.planes[0]["capacity_wp"] == pytest.approx(SYNTHETIC_WP, rel=0.02)
    assert any("meter-derived array" in note for note in forecast.notes)
    assert any("capacity estimate:" in note for note in forecast.notes)


def test_derived_solar_without_export_history_is_a_status_not_a_crash(store):
    stamps = daylight_stamps()
    stub = HistoryStub({GRID_ENTITY: [(stamp, 1.5) for stamp in stamps]})  # always importing
    forecast, status = load_solar_forecast(
        derived_settings(store), store, stub, location=TEST_LOCATION, now=NOW
    )
    assert status.ok is False
    assert "never went negative" in status.last_error
    assert forecast.slots == []


def test_derived_solar_needs_a_grid_mapping(store):
    stub = HistoryStub({})
    settings = derived_settings(store, **{"grid.power_entity": ""})
    forecast, status = load_solar_forecast(settings, store, stub, location=TEST_LOCATION, now=NOW)
    assert status.ok is False
    assert "grid.power_entity" in status.last_error
    assert forecast.slots == []


def test_derived_solar_refuses_when_the_battery_flow_is_unmapped(store):
    # A configured battery with no AC flow mapping: its discharge would read as PV, so refuse.
    settings = derived_settings(store, batteries=battery_mapping_entry(soc_entity="sensor.example_soc"))
    stub = HistoryStub({GRID_ENTITY: [(stamp, -1.0) for stamp in daylight_stamps()]})
    forecast, status = load_solar_forecast(settings, store, stub, location=TEST_LOCATION, now=NOW)
    assert status.ok is False
    assert "no AC flow mapping" in status.last_error
    assert forecast.slots == []


def test_derived_solar_needs_the_home_assistant_connection(store):
    forecast, status = load_solar_forecast(
        derived_settings(store), store, None, location=TEST_LOCATION, now=NOW
    )
    assert status.ok is False
    assert "Home Assistant connection" in status.last_error


def test_derived_solar_lets_an_explicit_watt_peak_win(store):
    """An explicit solar.capacity_wp beats the learned one (explicit beats derived)."""
    stub = HistoryStub({})  # no history needed: the explicit value short-circuits
    settings = derived_settings(store, **{"solar.capacity_wp": 2500.0})
    forecast, status = load_solar_forecast(settings, store, stub, location=TEST_LOCATION, now=NOW)
    assert status.ok, status.last_error
    assert forecast.planes[0]["capacity_wp"] == 2500.0
    assert any("solar.capacity_wp is set" in note for note in forecast.notes)
    assert stub.calls == []


def test_derived_solar_caches_the_fit_and_does_not_refetch_history(store):
    """The fit reads weeks of history, so it is cached per mapping and refit at most daily."""
    stamps = daylight_stamps()
    settings = derived_settings(
        store,
        batteries=battery_mapping_entry(
            charge_power_entity=CHARGE_ENTITY, discharge_power_entity=DISCHARGE_ENTITY
        ),
    )
    first = HistoryStub(
        {
            GRID_ENTITY: [(stamp, 0.5 - model_kw(stamp)) for stamp in stamps],
            CHARGE_ENTITY: [(stamp, 0.5) for stamp in stamps],
        }
    )
    forecast, status = load_solar_forecast(settings, store, first, location=TEST_LOCATION, now=NOW)
    assert status.ok and any("fresh fit" in note for note in forecast.notes)
    learned = forecast.planes[0]["capacity_wp"]

    # A second refresh with a client that has no history at all: the cache answers, no fetch happens.
    second = HistoryStub({})
    forecast, status = load_solar_forecast(
        settings, store, second, location=TEST_LOCATION, now=NOW + timedelta(hours=6)
    )
    assert status.ok, status.last_error
    assert second.calls == [], "the cached fit must not refetch 56 days of history"
    assert forecast.planes[0]["capacity_wp"] == pytest.approx(learned)
    assert any("cached daily fit" in note for note in forecast.notes)

    # Changing the mapping invalidates the cache: the new grid entity has no history -> a status.
    changed = derived_settings(
        store,
        batteries=battery_mapping_entry(
            charge_power_entity=CHARGE_ENTITY, discharge_power_entity=DISCHARGE_ENTITY
        ),
        **{"grid.power_entity": "sensor.example_other_meter"},
    )
    forecast, status = load_solar_forecast(
        changed, store, second, location=TEST_LOCATION, now=NOW + timedelta(hours=6)
    )
    assert status.ok is False


# -------------------------------------------------------------------- NASA POWER irradiance
# SPEC.md section 7.1.1a: observed hourly irradiance as an *addition* to the capacity model,
# applied only where the weather entity gives no cloud coverage, and used in the derived fit.
# Keyless, W/m^2, UTC stamps ``YYYYMMDDHH``, fill value -999, ~3 day near-real-time latency.

NASA_RATIO = 0.6


class NasaStub:
    """Answers the NASA POWER hourly request without any network, and records the URLs."""

    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.urls = []

    def __call__(self, url, **kwargs):
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return self.payload


def nasa_payload(ratio_by_hour, *, days=6, origin=NOW):
    """Two synthetic irradiance series around ``origin``; clear-sky only during 08:00-15:00."""
    all_sky, clear_sky = {}, {}
    base = (origin - timedelta(days=20)).replace(hour=0, minute=0, second=0, microsecond=0)
    for day in range(days):
        for hour in range(24):
            stamp = (base + timedelta(days=day, hours=hour)).strftime("%Y%m%d%H")
            clear = 600.0 if 8 <= hour <= 15 else 0.0
            all_sky[stamp] = clear * ratio_by_hour.get(hour, 1.0)
            clear_sky[stamp] = clear
    return {"properties": {"parameter": {"ALLSKY_SFC_SW_DWN": all_sky, "CLRSKY_SFC_SW_DWN": clear_sky}}}


def nasa_series(payload):
    """``fetch_nasa_hourly``'s return shape: floats, fill dropped."""
    return {
        name: {stamp: float(value) for stamp, value in values.items()}
        for name, values in payload["properties"]["parameter"].items()
    }


def test_fetch_nasa_hourly_parses_both_series_and_drops_the_fill_value():
    payload = nasa_payload({12: NASA_RATIO}, days=2)
    payload["properties"]["parameter"]["ALLSKY_SFC_SW_DWN"]["2025121612"] = -999.0
    stub = NasaStub(payload)
    series = fetch_nasa_hourly(52, 5, NOW - timedelta(days=21), NOW, http=stub)
    assert set(series) == {"ALLSKY_SFC_SW_DWN", "CLRSKY_SFC_SW_DWN"}
    assert "2025121612" not in series["ALLSKY_SFC_SW_DWN"], "the fill value is missing data, not 0 W"
    assert "2025121612" in series["CLRSKY_SFC_SW_DWN"]
    url = stub.urls[0]
    assert "ALLSKY_SFC_SW_DWN" in url and "CLRSKY_SFC_SW_DWN" in url
    assert "community=RE" in url and "time-standard=UTC" in url and "format=JSON" in url


def test_fetch_nasa_hourly_rejects_a_response_without_a_series():
    with pytest.raises(ProviderError):
        fetch_nasa_hourly(52, 5, NOW - timedelta(days=21), NOW, http=NasaStub({"properties": {}}))


def test_nasa_clearness_profile_uses_daylight_medians_and_leaves_thin_hours_alone():
    series = nasa_series(nasa_payload({12: NASA_RATIO}, days=6))
    factors, fitted, samples = nasa_clearness_profile(series, timezone.utc)
    assert len(factors) == 24
    assert factors[12] == pytest.approx(NASA_RATIO)
    assert factors[0] == 1.0, "a twilight hour has no clear-sky reference: no invented attenuation"
    assert fitted == 8 and samples == 6 * 8


def test_capacity_forecast_applies_nasa_clearness_only_where_cloud_data_is_missing(store):
    baseline, base_status = load_capacity(capacity_settings(store))
    assert base_status.ok and base_status.detail["nasa_status"] == "disabled"
    nasa = NasaStub(nasa_payload({hour: NASA_RATIO for hour in range(8, 16)}))
    settings = capacity_settings(store, **{"solar.nasa_irradiance": True})
    forecast, status = load_capacity(settings, store, http=nasa)
    assert status.ok, status.last_error
    assert status.detail["nasa_irradiance"] is True and status.detail["nasa_status"] == "fresh fit"
    assert any("NASA POWER clearness applied" in note for note in forecast.notes)
    clear_noon = slot_at(baseline, NOW)
    nasa_noon = slot_at(forecast, NOW)
    assert clear_noon.pv_kw > 0.0
    assert nasa_noon.pv_kw == pytest.approx(clear_noon.pv_kw * NASA_RATIO, rel=1e-9)
    # 30 %: better than astronomy alone, worse than a forecast for the specific slot.
    assert all(slot.uncertainty_pct == 30.0 for slot in forecast.slots)

    # The daily cache answers the very next refresh: no second request, even with a failing stub.
    again, status2 = load_capacity(settings, store, http=NasaStub(error=ProviderError("offline")))
    assert status2.ok and status2.detail["nasa_status"] == "cached daily fit"
    assert again.slots and nasa.urls and len(nasa.urls) == 1


def test_capacity_forecast_stays_clear_sky_when_nasa_fails(store):
    settings = capacity_settings(store, **{"solar.nasa_irradiance": True})
    forecast, status = load_capacity(settings, store, http=NasaStub(error=ProviderError("offline")))
    assert status.ok, "NASA is optional: its failure must never fail the forecast"
    assert status.detail["nasa_irradiance"] is False and status.detail["nasa_status"] == "failed"
    assert any("NASA POWER irradiance unavailable" in note for note in forecast.notes)
    assert all(slot.uncertainty_pct == 35.0 for slot in forecast.slots)


def test_capacity_forecast_does_not_ask_nasa_when_the_weather_entity_covers_the_horizon(store):
    weather = WeatherStub(forecast=hourly_forecast([50] * 25))
    nasa = NasaStub(error=AssertionError("NASA must not be called when cloud data covers every hour"))
    settings = capacity_settings(
        store, **{"solar.nasa_irradiance": True, "solar.weather_entity": WEATHER_ENTITY}
    )
    forecast, status = load_capacity(settings, store, weather, http=nasa)
    assert status.ok, status.last_error
    assert status.detail["nasa_status"] == "not needed"
    assert nasa.urls == []


def test_weather_coverage_in_a_non_utc_zone_also_skips_nasa(store):
    # The cloud map is keyed by slot and looked up by local hour; the coverage test must count
    # local hours too, or a non-UTC install would ask NASA for a horizon the weather already covers.
    weather = WeatherStub(forecast=hourly_forecast([50] * 30))
    nasa = NasaStub(error=AssertionError("NASA must not be called when cloud data covers every hour"))
    settings = capacity_settings(
        store, **{"solar.nasa_irradiance": True, "solar.weather_entity": WEATHER_ENTITY}
    )
    forecast, status = load_capacity(
        settings, store, weather, location=(52, 5, "Europe/Amsterdam"), http=nasa
    )
    assert status.ok, status.last_error
    assert status.detail["nasa_status"] == "not needed"
    assert nasa.urls == []
    assert all(slot.uncertainty_pct == 25.0 for slot in forecast.slots)


def test_partial_cloud_coverage_in_a_non_utc_zone_keeps_cloud_over_nasa(store):
    zone = ZoneInfo("Europe/Amsterdam")
    location = (52, 5, "Europe/Amsterdam")
    weather = WeatherStub(forecast=hourly_forecast([80] * 6))
    plain = capacity_settings(store, **{"solar.weather_entity": WEATHER_ENTITY})
    with_nasa_settings = capacity_settings(
        store, **{"solar.nasa_irradiance": True, "solar.weather_entity": WEATHER_ENTITY}
    )
    nasa = NasaStub(nasa_payload({hour: NASA_RATIO for hour in range(8, 16)}))
    without, _ = load_capacity(plain, store, weather, location=location)
    with_nasa, status = load_capacity(with_nasa_settings, store, weather, location=location, http=nasa)
    assert status.ok, status.last_error
    assert nasa.urls, "partial coverage must consult NASA"
    assert status.detail["nasa_status"] == "fresh fit"
    covered = {(NOW + timedelta(hours=index)).astimezone(zone).hour for index in range(6)}
    checked = False
    for slot, base in zip(with_nasa.slots, without.slots):
        if base.pv_kw <= 0.0:
            continue
        hour = slot.t.astimezone(zone).hour
        expected = 1.0 if hour in covered else (NASA_RATIO if 8 <= hour <= 15 else 1.0)
        assert slot.pv_kw == pytest.approx(base.pv_kw * expected, rel=1e-9), f"hour {hour}"
        checked = True
    assert checked, "the horizon must contain daylight"


class NasaCacheErrorStore:
    """A store whose NASA profile I/O always fails, as a locked database would."""

    def __init__(self, inner):
        self.inner = inner

    def profile_get(self, kind, key):
        if kind == NASA_KIND:
            raise RuntimeError("database is locked")
        return self.inner.profile_get(kind, key)

    def profile_put(self, kind, key, payload, updated_at=None):
        if kind == NASA_KIND:
            raise RuntimeError("database is locked")
        return self.inner.profile_put(kind, key, payload, updated_at)


def test_nasa_cache_io_failure_is_not_a_failed_forecast(store):
    settings = capacity_settings(store, **{"solar.nasa_irradiance": True})
    broken = NasaCacheErrorStore(store)
    forecast, status = load_capacity(settings, broken, http=NasaStub(nasa_payload({12: NASA_RATIO})))
    assert status.ok, status.last_error
    assert status.detail["nasa_status"] == "fresh fit", "a cache write failure must not fail the fit"
    assert not any("NASA POWER irradiance unavailable" in note for note in forecast.notes)
    _, failed = load_capacity(settings, broken, http=NasaStub(error=ProviderError("offline")))
    assert failed.ok and failed.detail["nasa_status"] == "failed"


def test_nasa_failure_is_backed_off_between_refreshes(store):
    settings = capacity_settings(store, **{"solar.nasa_irradiance": True})
    first_fail = NasaStub(error=ProviderError("offline"))
    _, status = load_capacity(settings, store, http=first_fail)
    assert status.detail["nasa_status"] == "failed" and len(first_fail.urls) == 1
    # Inside the backoff window the recorded failure answers: no request, no repeated warning.
    blocked = NasaStub(error=AssertionError("a failed fetch must be backed off"))
    _, cooled = load_capacity(settings, store, http=blocked)
    assert cooled.ok and cooled.detail["nasa_status"] == "failed" and blocked.urls == []
    retry = NasaStub(nasa_payload({12: NASA_RATIO}))
    later = NOW + timedelta(hours=NASA_FAILURE_BACKOFF_HOURS + 0.5)
    _, recovered = load_capacity(settings, store, http=retry, now=later)
    assert recovered.detail["nasa_status"] == "fresh fit" and len(retry.urls) == 1


def test_nasa_profile_cache_is_keyed_by_timezone(store):
    settings = capacity_settings(store, **{"solar.nasa_irradiance": True})
    first = NasaStub(nasa_payload({hour: NASA_RATIO for hour in range(8, 16)}))
    _, status = load_capacity(settings, store, http=first)
    assert status.detail["nasa_status"] == "fresh fit"
    # The buckets are local hours, so another timezone must refit, not reuse the UTC profile.
    other = NasaStub(nasa_payload({hour: NASA_RATIO for hour in range(8, 16)}))
    _, switched = load_capacity(settings, store, http=other, location=(52, 5, "Europe/Amsterdam"))
    assert switched.detail["nasa_status"] == "fresh fit" and len(other.urls) == 1


def test_nasa_cache_ignores_a_timestamp_from_the_future(store):
    fingerprint = "52.0000|5.0000|UTC"
    store.profile_put(
        NASA_KIND,
        NASA_PROFILE_KEY,
        {"fingerprint": fingerprint, "factors": [1.0] * 24, "hours": 8, "samples": 48},
        NOW + timedelta(hours=2),
    )
    assert _nasa_cache(store, fingerprint, NOW) is None, "a clock change is not freshness"


def test_implied_capacity_uses_nasa_clearness_to_undo_cloud_bias():
    clearness = [1.0] * 24
    for hour in range(8, 16):
        clearness[hour] = 0.5
    # Every sample is the clear-sky model attenuated by the measured clearness of its hour.
    samples = [
        (stamp, model_kw(stamp) * clearness[stamp.astimezone(timezone.utc).hour])
        for stamp in daylight_stamps()
        if clearness[stamp.astimezone(timezone.utc).hour] < 1.0
    ]
    without, usable = _implied_capacity_wp(52, 5, timezone.utc, samples, derived_settings())
    assert usable >= DERIVED_MIN_SAMPLES
    assert without == pytest.approx(SYNTHETIC_WP * 0.5, rel=0.02), "without clearness the fit halves"
    with_nasa, _ = _implied_capacity_wp(
        52, 5, timezone.utc, samples, derived_settings(), clearness=clearness
    )
    assert with_nasa == pytest.approx(SYNTHETIC_WP, rel=0.02)


def test_derived_source_uses_nasa_clearness_for_fit_and_forecast(store):
    clearness = [1.0] * 24
    for hour in range(8, 16):
        clearness[hour] = 0.5
    stamps = daylight_stamps()
    history = HistoryStub(
        {
            GRID_ENTITY: [
                (stamp, 0.2 - model_kw(stamp) * clearness[stamp.astimezone(timezone.utc).hour])
                for stamp in stamps
            ],
            CHARGE_ENTITY: [(stamp, 0.2) for stamp in stamps],
        }
    )
    nasa = NasaStub(nasa_payload({hour: 0.5 for hour in range(8, 16)}))
    settings = derived_settings(
        store,
        **{"solar.nasa_irradiance": True},
        batteries=battery_mapping_entry(
            charge_power_entity=CHARGE_ENTITY, discharge_power_entity=DISCHARGE_ENTITY
        ),
    )
    forecast, status = load_solar_forecast(
        settings, store, history, location=TEST_LOCATION, now=NOW, http=nasa
    )
    assert status.ok, status.last_error
    assert forecast.planes[0]["capacity_wp"] == pytest.approx(SYNTHETIC_WP, rel=0.05)
    assert any("NASA POWER clearness refined the meter-derived array fit" in note for note in forecast.notes)
    assert len(nasa.urls) == 1, "the fit and the forecast share one daily profile fetch"


# ----------------------------------------------- Energy dashboard + long-term statistics (SPEC 3/7.1.2)
# The Energy-dashboard fallback gives the derived tier a meter without a manual mapping, and the
# fit reads it through long-term statistics: the recorder's purge window is not the history window.

POWER_STAT_META = {"unit_class": "power", "statistics_unit_of_measurement": "kW"}
ENERGY_STAT_META = {"unit_class": "energy", "statistics_unit_of_measurement": "kWh"}
ENERGY_EXPORT_STAT = "sensor.example_energy_export"


class StatisticsWS:
    """Context-managed ``recorder/statistics_*`` reader for the fit (no socket, no recorder)."""

    def __init__(self, metadata, rows):
        self.metadata = metadata
        self.rows = rows
        self.types = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def statistics_metadata(self, ids):
        return {stat_id: self.metadata[stat_id] for stat_id in ids if stat_id in self.metadata}

    def statistics_during_period(self, ids, start, end=None, period="hour", types=None):
        self.types.append(tuple(types or ()))
        return {stat_id: self.rows.get(stat_id, []) for stat_id in ids}


def stat_rows(series, key="mean"):
    return [
        {
            "start": stamp.isoformat(),
            "end": (stamp + timedelta(hours=1)).isoformat(),
            key: value,
        }
        for stamp, value in series
    ]


def test_derived_solar_reads_long_term_statistics_when_the_recorder_is_empty(store):
    stamps = daylight_stamps()
    settings = derived_settings(
        store,
        batteries=battery_mapping_entry(
            charge_power_entity=CHARGE_ENTITY, discharge_power_entity=DISCHARGE_ENTITY
        ),
    )
    ws = StatisticsWS(
        metadata={GRID_ENTITY: POWER_STAT_META, CHARGE_ENTITY: POWER_STAT_META, DISCHARGE_ENTITY: POWER_STAT_META},
        rows={
            GRID_ENTITY: stat_rows([(stamp, 0.5 - model_kw(stamp)) for stamp in stamps]),
            CHARGE_ENTITY: stat_rows([(stamp, 0.5) for stamp in stamps]),
            DISCHARGE_ENTITY: stat_rows([(stamp, 0.0) for stamp in stamps]),
        },
    )
    stub = HistoryStub({})  # the purge-limited recorder has nothing; statistics must answer
    forecast, status = load_solar_forecast(
        settings, store, stub, location=TEST_LOCATION, now=NOW, ws_factory=lambda: ws
    )
    assert status.ok, status.last_error
    assert forecast.planes[0]["capacity_wp"] == pytest.approx(SYNTHETIC_WP, rel=0.03)
    assert stub.calls == [], "the fit must come from long-term statistics"
    assert ("mean",) in ws.types


def test_forecast_solar_without_planes_falls_back_to_the_meter(store):
    """SPEC 7.1.1: no planes means Forecast.Solar has nothing to size, so the meter answers."""
    stamps = daylight_stamps()
    settings = derived_settings(
        store,
        **{"solar.source": "forecast_solar", "grid.power_entity": ""},
        batteries=battery_mapping_entry(
            charge_power_entity=CHARGE_ENTITY, discharge_power_entity=DISCHARGE_ENTITY
        ),
    )
    stub = HistoryStub(
        {
            GRID_ENTITY: [(stamp, 0.5 - model_kw(stamp)) for stamp in stamps],
            CHARGE_ENTITY: [(stamp, 0.5) for stamp in stamps],
        }
    )
    forecast, status = load_solar_forecast(
        settings,
        store,
        stub,
        location=TEST_LOCATION,
        now=NOW,
        grid_roles={"grid_power": GRID_ENTITY},  # what the Energy dashboard would resolve to
    )
    assert status.ok, status.last_error
    assert forecast.source == "derived" and forecast.slots
    assert any("solar.planes is empty" in note for note in forecast.notes)


def test_forecast_solar_without_planes_reports_why_the_fallback_failed(store):
    settings = derived_settings(
        store, **{"solar.source": "forecast_solar", "grid.power_entity": ""}
    )
    forecast, status = load_solar_forecast(
        settings, store, HistoryStub({}), location=TEST_LOCATION, now=NOW
    )
    assert forecast.slots == []
    assert "grid.power_entity" in str(status.detail.get("derived_fallback"))
    assert any("meter-derived estimate" in note for note in forecast.notes)


def test_derived_solar_accepts_energy_counters_from_the_energy_dashboard(store):
    """An Energy dashboard with no power sensor still sizes the array from its export counter."""
    stamps = daylight_stamps()
    settings = derived_settings(store, **{"grid.power_entity": ""})
    ws = StatisticsWS(
        metadata={ENERGY_EXPORT_STAT: ENERGY_STAT_META},
        rows={
            # Every produced kWh reached the meter: the fit's lower-bound formula adds battery
            # charge back separately, so the counter itself carries the full model output.
            ENERGY_EXPORT_STAT: stat_rows(
                [(stamp, model_kw(stamp)) for stamp in stamps], key="change"
            )
        },
    )
    forecast, status = load_solar_forecast(
        settings,
        store,
        HistoryStub({}),
        location=TEST_LOCATION,
        now=NOW,
        grid_roles={"grid_energy_export": ENERGY_EXPORT_STAT},
        ws_factory=lambda: ws,
    )
    assert status.ok, status.last_error
    assert forecast.planes[0]["capacity_wp"] == pytest.approx(SYNTHETIC_WP, rel=0.03)
    assert ("change",) in ws.types, "an energy counter must be read as an hourly change, not a mean"
