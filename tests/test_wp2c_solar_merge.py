"""WP2C: multi-source PV forecast merge (SPEC.md section 7.1.3).

Covers the pure merge math (weights, median, max, ties, gaps, empty/disabled sources), the
``solar.sources`` / ``solar.merge_strategy`` config validation, the Home Assistant
``energy/solar_forecast`` websocket contract (generic fake payloads only, SPEC.md R1), the merged
forecast end to end, and the forecast explanation / provider status payloads.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "energy_manager") not in sys.path:
    sys.path.insert(0, str(ROOT / "energy_manager"))

from app.api import provider_dict  # noqa: E402
from app.config import Settings  # noqa: E402
from app.models import ConfigError, ProviderStatus  # noqa: E402
from app.sources import ProviderError  # noqa: E402
from app.sources.solar import (  # noqa: E402
    configured_solar_sources,
    ha_energy_power_series,
    load_solar_forecast,
    merge_solar_series,
    parse_ha_energy_wh,
)
from app.store import Store  # noqa: E402

NOW = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)
PLANE = {"kwp": 5.0, "tilt_deg": 35.0, "azimuth_deg": 180.0}
#: The HA Energy dashboard's own provider keys are opaque config entry ids; a fake one is enough.
ENTRY_A = "example_entry_a"
ENTRY_B = "example_entry_b"


def watts_payload(watts: float, *, hours: int = 6) -> dict:
    origin = NOW.replace(minute=0, second=0, microsecond=0)
    table = {(origin + timedelta(hours=index)).isoformat(): watts + index for index in range(hours + 1)}
    return {
        "result": {"watts": table, "watt_hours_period": {}, "watt_hours_day": {}},
        "message": {"code": 0, "text": ""},
    }


class HttpStub:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.urls: list[str] = []

    def __call__(self, url, **kwargs):
        self.urls.append(url)
        item = self.responses.pop(0) if self.responses else watts_payload(0.0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeWS:
    """A context-managed ``HAWebSocket`` stand-in answering ``energy/solar_forecast``."""

    def __init__(self, payload=None, error: BaseException | None = None):
        self.payload = payload if payload is not None else {}
        self.error = error
        self.calls: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def call(self, message_type, **fields):
        self.calls.append(message_type)
        if self.error is not None:
            raise self.error
        return self.payload


@pytest.fixture()
def store(tmp_path):
    instance = Store(tmp_path / "merge.db")
    yield instance
    instance.close()


def settings(store=None, sources=None, **overrides):
    options = {
        "solar.bias_correction": False,
        "solar.nasa_irradiance": False,
        "optimizer.horizon_hours": 12,
    }
    if sources is not None:
        options["solar.sources"] = sources
    options.update(overrides)
    return Settings(store, options=options)


# ------------------------------------------------------------------------------- merge math


def test_merge_weighted_mean_median_and_max():
    grid = [NOW, NOW + timedelta(minutes=15)]
    a = [(NOW, 1.0)]
    b = [(NOW, 3.0)]
    entries = [{"name": "a", "series": a, "weight": 3}, {"name": "b", "series": b, "weight": 1}]

    mean, report = merge_solar_series(grid, entries, "weighted_mean")
    assert mean[0] == pytest.approx((3 * 1.0 + 1 * 3.0) / 4.0)
    assert mean[1] == pytest.approx(mean[0])  # a flat series keeps the same value
    assert {row["name"]: row["state"] for row in report} == {"a": "ok", "b": "ok"}

    median, _ = merge_solar_series(grid, entries, "median")
    assert median[0] == pytest.approx(2.0)  # even count: mean of the two middle values

    best, _ = merge_solar_series(grid, entries, "max")
    assert best[0] == pytest.approx(3.0)


def test_merge_median_odd_count_and_ties_are_deterministic():
    grid = [NOW]
    entries = [
        {"name": "a", "series": [(NOW, 1.0)], "weight": 1},
        {"name": "b", "series": [(NOW, 3.0)], "weight": 1},
        {"name": "c", "series": [(NOW, 3.0)], "weight": 1},
    ]
    values, _ = merge_solar_series(grid, entries, "median")
    assert values[0] == pytest.approx(3.0)


def test_merge_skips_empty_failed_and_disabled_sources():
    grid = [NOW]
    entries = [
        {"name": "empty", "series": [], "weight": 1, "note": "boom"},
        {"name": "disabled", "series": [(NOW, 5.0)], "weight": 1, "state": "disabled"},
        {"name": "ok", "series": [(NOW, 2.0)], "weight": 1},
    ]
    values, report = merge_solar_series(grid, entries, "weighted_mean")
    assert values[0] == pytest.approx(2.0)
    states = {row["name"]: row["state"] for row in report}
    assert states == {"empty": "failed", "disabled": "disabled", "ok": "ok"}
    assert report[0]["note"] == "boom"


def test_merge_gap_before_the_series_contributes_zero():
    grid = [NOW, NOW + timedelta(hours=1)]
    entries = [
        {"name": "late", "series": [(NOW + timedelta(hours=1), 4.0)], "weight": 1},
        {"name": "flat", "series": [(NOW, 1.0)], "weight": 1},
    ]
    values, _ = merge_solar_series(grid, entries, "weighted_mean")
    assert values[0] == pytest.approx((0.0 + 1.0) / 2.0)
    assert values[1] == pytest.approx((4.0 + 1.0) / 2.0)


def test_merge_with_no_contributing_source_is_all_zero():
    values, report = merge_solar_series([NOW, NOW + timedelta(minutes=15)], [], "max")
    assert values == [0.0, 0.0]
    assert report == []


def test_merge_unknown_strategy_falls_back_to_weighted_mean():
    entries = [{"name": "a", "series": [(NOW, 2.0)], "weight": 1}]
    values, _ = merge_solar_series([NOW], entries, "bogus")
    assert values[0] == pytest.approx(2.0)


# ------------------------------------------------------------------------------- config


def test_solar_sources_are_validated_and_normalised():
    instance = Settings(None, options={})
    value = instance.set(
        "solar.sources",
        [
            {"source": "forecast_solar", "weight": 2, "planes": [PLANE]},
            {"source": "ha_energy", "weight": 0.5, "entry_id": ENTRY_A, "name": "Dashboard"},
            {"source": "capacity", "weight": 1, "capacity_wp": 4000},
        ],
    )
    assert [entry["source"] for entry in value] == ["forecast_solar", "ha_energy", "capacity"]
    assert value[0]["weight"] == 2.0 and value[1]["weight"] == 0.5
    assert value[1]["entry_id"] == ENTRY_A and value[2]["capacity_wp"] == 4000.0
    assert instance.set("solar.sources", []) == []


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-list",
        [1],
        [{"weight": 1}],
        [{"source": "magic", "weight": 1}],
        [{"source": "capacity", "weight": 0}],
        [{"source": "capacity", "weight": -1}],
        [{"source": "capacity", "weight": float("inf")}],
        [{"source": "capacity", "weight": "heavy"}],
        [{"source": "capacity", "weight": 1, "unknown_key": 2}],
        [{"source": "forecast_solar", "weight": 1, "planes": [{"kwp": 0}]}],
        [{"source": "capacity", "weight": 1, "capacity_wp": -5}],
        [{"source": "ha_energy", "weight": 1, "planes": [PLANE]}],
    ],
)
def test_malformed_solar_sources_fail_validation_with_a_clear_message(bad):
    with pytest.raises(ConfigError) as err:
        Settings(None, options={}).set("solar.sources", bad)
    assert "solar.sources" in str(err.value)


def test_merge_strategy_is_a_validated_choice():
    instance = Settings(None, options={})
    assert instance.set("solar.merge_strategy", "median") == "median"
    with pytest.raises(ConfigError):
        instance.set("solar.merge_strategy", "bogus")


def test_configured_solar_sources_never_raises_on_a_bad_stored_value():
    class Raw:
        def get(self, key, default=None):
            return [{"source": "magic", "weight": 1}, "nope", {"source": "capacity", "weight": 0}]

    assert configured_solar_sources(Raw()) == []
    assert configured_solar_sources(Settings(None, options={})) == []


def test_engine_reports_a_malformed_solar_sources_patch_as_a_per_key_error(store):
    from app.engine import Engine

    engine = Engine(settings(store), store=store)
    result = engine.apply_settings({"solar.sources": [{"source": "capacity", "weight": 0}]})
    assert result["applied"] == []
    assert result["errors"][0]["key"] == "solar.sources"
    assert "weight" in result["errors"][0]["message"]


# ------------------------------------------------------------------------------- HA websocket


def wh_hours(values: list[float], *, start: datetime | None = None) -> dict[str, float]:
    origin = (start or NOW).replace(minute=0, second=0, microsecond=0)
    return {(origin + timedelta(hours=index)).isoformat(): value for index, value in enumerate(values)}


def test_parse_ha_energy_sums_providers_and_can_select_one():
    payload = {ENTRY_A: {"wh_hours": wh_hours([1000.0, 1000.0])}, ENTRY_B: {"wh_hours": wh_hours([500.0, 500.0])}}
    points, providers = parse_ha_energy_wh(payload)
    assert providers == 2
    assert sorted(points.values()) == [1500.0, 1500.0]

    only_a, providers_a = parse_ha_energy_wh(payload, ENTRY_A)
    assert providers_a == 1
    assert set(only_a.values()) == {1000.0}

    missing, providers_missing = parse_ha_energy_wh(payload, "example_absent")
    assert missing == {} and providers_missing == 0
    assert parse_ha_energy_wh({}) == ({}, 0)


def test_ha_energy_power_series_uses_each_period_length():
    hourly = ha_energy_power_series({NOW: 1000.0, NOW + timedelta(hours=1): 2000.0})
    assert hourly[0][1] == pytest.approx(1.0)  # 1000 Wh over one hour
    assert hourly[1][1] == pytest.approx(2.0)
    half_hour = ha_energy_power_series({NOW: 500.0, NOW + timedelta(minutes=30): 500.0})
    assert half_hour[0][1] == pytest.approx(1.0)  # 500 Wh over 30 minutes


# ------------------------------------------------------------------------------- end to end


def test_merge_combines_forecast_solar_and_capacity_by_weight(store):
    sources = [
        {"source": "forecast_solar", "weight": 2, "planes": [PLANE]},
        {"source": "capacity", "weight": 1, "capacity_wp": 4000.0},
    ]
    merged, status = load_solar_forecast(
        settings(store, sources), store, None, location=(52, 5, "UTC"), now=NOW, http=HttpStub(watts_payload(1000.0))
    )
    solar_only, _ = load_solar_forecast(
        settings(store, [{"source": "forecast_solar", "weight": 1, "planes": [PLANE]}]),
        store,
        None,
        location=(52, 5, "UTC"),
        now=NOW,
        http=HttpStub(watts_payload(1000.0)),
    )
    capacity_only, _ = load_solar_forecast(
        settings(store, [{"source": "capacity", "weight": 1, "capacity_wp": 4000.0}]),
        store,
        None,
        location=(52, 5, "UTC"),
        now=NOW,
    )
    assert status.ok and merged.source == "merge"
    expected = (2 * solar_only.slots[0].pv_kw + 1 * capacity_only.slots[0].pv_kw) / 3.0
    assert merged.slots[0].pv_kw == pytest.approx(expected)
    states = {row.name: row.state for row in merged.sources}
    assert states == {"forecast_solar": "ok", "capacity": "ok"}
    assert status.detail["sources_used"] == ["forecast_solar", "capacity"]
    assert any("merged 2 source(s)" in note for note in merged.notes)


def test_merge_skips_a_failing_source_and_still_forecasts(store):
    sources = [
        {"source": "forecast_solar", "weight": 1, "planes": [PLANE]},
        {"source": "capacity", "weight": 1, "capacity_wp": 4000.0},
    ]
    merged, status = load_solar_forecast(
        settings(store, sources), store, None, location=(52, 5, "UTC"), now=NOW, http=HttpStub(ProviderError("boom"))
    )
    capacity_only, _ = load_solar_forecast(
        settings(store, [{"source": "capacity", "weight": 1, "capacity_wp": 4000.0}]),
        store,
        None,
        location=(52, 5, "UTC"),
        now=NOW,
    )
    states = {row.name: row.state for row in merged.sources}
    assert status.ok and states["forecast_solar"] == "failed" and states["capacity"] == "ok"
    assert merged.slots[0].pv_kw == pytest.approx(capacity_only.slots[0].pv_kw)
    assert any("skipped" in note for note in merged.notes)


def test_merge_falls_back_to_solar_source_when_no_source_works(store):
    options = {
        "solar.sources": [{"source": "ha_energy", "weight": 1}],
        "solar.source": "capacity",
        "solar.capacity_wp": 4000.0,
    }
    merged, status = load_solar_forecast(settings(store, **options), store, None, location=(52, 5, "UTC"), now=NOW)
    assert status.ok and status.name == "capacity"
    assert merged.source == "capacity" and merged.slots
    assert any("using solar.source alone" in note for note in merged.notes)
    assert [(row.name, row.state) for row in merged.sources] == [("ha_energy", "failed")]


def test_disabled_solar_marks_every_configured_source_disabled(store):
    merged, status = load_solar_forecast(
        settings(store, [{"source": "capacity", "weight": 1}], **{"solar.enabled": False}),
        store,
        None,
        location=(52, 5, "UTC"),
        now=NOW,
    )
    assert status.active is False and status.state == "disabled"
    assert merged.slots == []
    assert [row.state for row in merged.sources] == ["disabled"]


def test_ha_energy_source_reads_the_dashboard_providers(store):
    payload = {ENTRY_A: {"wh_hours": wh_hours([1000.0] * 13)}, ENTRY_B: {"wh_hours": wh_hours([500.0] * 13)}}
    ws = FakeWS(payload)
    merged, status = load_solar_forecast(
        settings(store, [{"source": "ha_energy", "weight": 1}]),
        store,
        None,
        location=(52, 5, "UTC"),
        now=NOW,
        ws_factory=lambda: ws,
    )
    assert ws.calls == ["energy/solar_forecast"]
    assert status.ok and merged.slots[0].pv_kw == pytest.approx(1.5)
    assert merged.sources[0].state == "ok"
    assert "2 provider(s)" in merged.sources[0].note


def test_ha_energy_unavailable_is_a_note_and_the_other_source_still_forecasts(store):
    broken = FakeWS(error=RuntimeError("unknown_command"))
    sources = [{"source": "ha_energy", "weight": 1}, {"source": "capacity", "weight": 1, "capacity_wp": 4000.0}]
    merged, status = load_solar_forecast(
        settings(store, sources), store, None, location=(52, 5, "UTC"), now=NOW, ws_factory=lambda: broken
    )
    states = {row.name: row.state for row in merged.sources}
    assert status.ok and states["ha_energy"] == "failed" and states["capacity"] == "ok"
    assert any("unavailable" in (row.note or "") for row in merged.sources if row.name == "ha_energy")


def test_ha_energy_entry_id_label_keeps_sources_apart(store):
    payload = {ENTRY_A: {"wh_hours": wh_hours([1000.0] * 13)}}
    merged, status = load_solar_forecast(
        settings(store, [{"source": "ha_energy", "weight": 1, "entry_id": ENTRY_A}]),
        store,
        None,
        location=(52, 5, "UTC"),
        now=NOW,
        ws_factory=lambda: FakeWS(payload),
    )
    assert status.ok and merged.sources[0].name.startswith("ha_energy:")


# ------------------------------------------------------------------------------- status payloads


def test_provider_dict_reports_state_weight_and_note():
    row = provider_dict(
        ProviderStatus(kind="solar", name="example", active=True, ok=True, weight=2.0, state="ok", note="fine")
    )
    assert row["state"] == "ok" and row["weight"] == 2.0 and row["note"] == "fine"
    # With no explicit state the coarse state is derived, so existing providers keep working.
    assert provider_dict(ProviderStatus(kind="solar", name="a", active=True, ok=False))["state"] == "failed"
    assert provider_dict(ProviderStatus(kind="solar", name="b", active=False, ok=False))["state"] == "disabled"


def test_engine_lists_one_solar_provider_per_configured_source(store):
    from app.engine import Engine

    sources = [
        {"source": "forecast_solar", "weight": 2, "planes": [PLANE]},
        {"source": "capacity", "weight": 1, "capacity_wp": 4000.0},
    ]
    instance = settings(store, sources)
    engine = Engine(instance, store=store, solar_source=None)
    engine._solar_source = lambda now: load_solar_forecast(  # noqa: SLF001 - the status map is the subject
        instance, store, None, location=(52, 5, "UTC"), now=now, http=HttpStub(watts_payload(1000.0))
    )
    engine._fetch("solar", "solar", NOW, engine._solar_source)  # noqa: SLF001
    engine._sync_provider_status()  # noqa: SLF001
    names = {status.name for status in engine.providers()["solar"]}
    assert {"forecast_solar", "capacity"} <= names


def test_merge_explanation_names_the_failing_source(store):
    from app.engine import Engine

    instance = settings(store, [{"source": "ha_energy", "weight": 1}, {"source": "capacity", "weight": 1}])
    engine = Engine(instance, store=store, solar_source=lambda now: None)
    engine._provider_status[("solar", "ha_energy")] = ProviderStatus(  # noqa: SLF001
        kind="solar", name="ha_energy", active=True, ok=False, state="failed", last_error="energy/solar_forecast unavailable"
    )
    engine._provider_status[("solar", "capacity")] = ProviderStatus(  # noqa: SLF001
        kind="solar", name="capacity", active=True, ok=False, state="failed", last_error="no capacity configured"
    )
    explanation = engine.explain_forecast("solar")
    assert explanation["reason"] == "provider_failed"
    assert "ha_energy" in explanation["hint"] and "capacity" in explanation["hint"]


def test_forecast_payload_lists_each_merge_source():
    from app.api import ApiServer
    from app.models import SolarForecast

    class StubEngine:
        def explain_forecast(self, kind):
            return {"reason": None, "hint": None}

    forecast = SolarForecast(source="merge", generated_at=NOW)
    forecast.sources = [
        ProviderStatus(kind="solar", name="forecast_solar", active=True, ok=True, weight=2.0, state="ok", note=""),
        ProviderStatus(kind="solar", name="capacity", active=True, ok=False, weight=1.0, state="failed", note="no capacity configured"),
    ]
    app = ApiServer(StubEngine(), web_root=None, host="127.0.0.1", port=0)
    payload = app._augment_forecast_payload({"kind": "solar", "source": "merge"}, "solar", forecast)  # noqa: SLF001
    rows = payload["sources"]
    assert [(row["name"], row["weight"], row["state"]) for row in rows] == [
        ("forecast_solar", 2.0, "ok"),
        ("capacity", 1.0, "failed"),
    ]
    assert rows[1]["note"] == "no capacity configured"
    # A single-source forecast carries no sources key at all: the payload stays byte-identical.
    single = SolarForecast(source="capacity", generated_at=NOW)
    plain = app._augment_forecast_payload({"kind": "solar", "source": "capacity"}, "solar", single)  # noqa: SLF001
    assert "sources" not in plain
