"""WP2A: Forecast.Solar hourly/single-plane contract, cache, 429 back-off, bias (SPEC §7.1, App. A)."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    _interpolate,
    _parse_watts,
    fetch_plane,
    load_solar_forecast,
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
    options = {"solar.enabled": True, "solar.planes": [PLANE], "solar.bias_correction": False}
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
    "optimizer.horizon_hours": 24,
}
TEST_LOCATION = (52, 5, "UTC")  # integers: no coordinate shape anywhere in this file


def capacity_settings(store=None, **overrides):
    options = dict(CAPACITY_DEFAULTS)
    options.update(overrides)
    return Settings(store, options=options)


def load_capacity(settings, store=None, client=None, *, location=TEST_LOCATION, now=NOW):
    # Default the store to the one the settings were built with, so a one-argument call still sees
    # whatever bias learning that store holds.
    if store is None:
        store = getattr(settings, "store", None)
    return load_solar_forecast(settings, store, client, location=location, now=now)


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
