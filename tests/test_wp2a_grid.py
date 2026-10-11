"""WP2A: the Energy-dashboard grid fallback (SPEC.md 3, 7.1.2, 7.2).

``energy/get_prefs`` is read-only and not admin-gated, so the add-on can find the meter its owner
already described to Home Assistant: the signed power sensor (positive = import) for live sampling
and control, and the import/export energy counters as a long-term-statistics source for the derived
load and PV tiers. An explicit ``grid.*`` setting always wins as a whole.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "energy_manager") not in sys.path:
    sys.path.insert(0, str(ROOT / "energy_manager"))

from app.config import Settings  # noqa: E402
from app.grid import GridMapping, roles_from_energy_prefs  # noqa: E402

NOW = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)

#: The shape Home Assistant 2026.x returns: one unified grid source with a generated net power
#: sensor plus the user's original power config, and the mandatory energy counters.
POWER_PREFS = {
    "energy_sources": [
        {"type": "gas", "stat_energy_from": "sensor.example_gas"},
        {
            "type": "grid",
            "stat_energy_from": "sensor.zonneplan_electricity_consumption_today",
            "stat_energy_to": "sensor.zonneplan_electricity_returned_today",
            "stat_rate": "sensor.example_net_power",
            "power_config": {
                "stat_rate_from": "sensor.example_grid_import",
                "stat_rate_to": "sensor.example_grid_export",
            },
        },
    ]
}

ENERGY_PREFS = {
    "energy_sources": [
        {
            "type": "grid",
            "stat_energy_from": "sensor.zonneplan_electricity_consumption_today",
            "stat_energy_to": "sensor.zonneplan_electricity_returned_today",
        }
    ]
}


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


class FakeWS:
    """A context-managed Energy-dashboard reader; counts reads so the TTL is testable."""

    def __init__(self, prefs=None, error=None):
        self.prefs = prefs if prefs is not None else {}
        self.error = error
        self.reads = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def energy_prefs(self):
        self.reads += 1
        if self.error:
            raise RuntimeError(self.error)
        return self.prefs


def settings(**values):
    return Settings(None, options=values)


# --------------------------------------------------------------------------- the pure parser


def test_a_generated_net_power_sensor_wins_over_the_two_sensor_config():
    roles = roles_from_energy_prefs(POWER_PREFS)
    # Home Assistant fills stat_rate for every power configuration, including a generated net
    # sensor for a two-sensor config; that sensor's sign already matches grid.power_entity.
    assert roles["grid_power"] == "sensor.example_net_power"
    assert "grid_import" not in roles and "grid_export" not in roles
    assert roles["grid_energy_import"] == "sensor.zonneplan_electricity_consumption_today"
    assert roles["grid_energy_export"] == "sensor.zonneplan_electricity_returned_today"


def test_the_two_sensor_config_is_used_when_there_is_no_generated_sensor():
    prefs = {
        "energy_sources": [
            {
                "type": "grid",
                "power_config": {
                    "stat_rate_from": "sensor.example_grid_import",
                    "stat_rate_to": "sensor.example_grid_export",
                },
            }
        ]
    }
    roles = roles_from_energy_prefs(prefs)
    assert roles["grid_import"] == "sensor.example_grid_import"
    assert roles["grid_export"] == "sensor.example_grid_export"
    assert "grid_power" not in roles


def test_energy_counters_are_read_when_there_is_no_power_sensor():
    roles = roles_from_energy_prefs(ENERGY_PREFS)
    assert roles == {
        "grid_energy_import": "sensor.zonneplan_electricity_consumption_today",
        "grid_energy_export": "sensor.zonneplan_electricity_returned_today",
    }


def test_a_power_grid_source_outranks_an_energy_only_one():
    prefs = {
        "energy_sources": [
            {
                "type": "grid",
                "stat_energy_from": "sensor.example_first_in",
                "stat_energy_to": "sensor.example_first_out",
            },
            {"type": "grid", "stat_rate": "sensor.example_second_power"},
        ]
    }
    roles = roles_from_energy_prefs(prefs)
    assert roles["grid_power"] == "sensor.example_second_power"


def test_the_legacy_flow_shape_is_tolerated():
    prefs = {
        "energy_sources": [
            {
                "type": "grid",
                "flow_from": [{"stat_energy_from": "sensor.example_legacy_in"}],
                "flow_to": [{"stat_energy_to": "sensor.example_legacy_out"}],
                "power": [{"power_config": {"stat_rate_inverted": "sensor.example_inverted"}}],
            }
        ]
    }
    roles = roles_from_energy_prefs(prefs)
    assert roles["grid_energy_import"] == "sensor.example_legacy_in"
    assert roles["grid_energy_export"] == "sensor.example_legacy_out"
    # Inverted without the generated net sensor is skipped: guessing would flip every decision.
    assert "grid_power" not in roles


def test_odd_payloads_are_empty_not_errors():
    assert roles_from_energy_prefs(None) == {}
    assert roles_from_energy_prefs({}) == {}
    assert roles_from_energy_prefs({"energy_sources": "nope"}) == {}
    assert roles_from_energy_prefs({"energy_sources": [{"type": "solar", "stat_energy_from": "sensor.example_pv"}]}) == {}
    assert roles_from_energy_prefs({"energy_sources": [{"type": "grid", "stat_energy_from": None, "stat_energy_to": None}]}) == {}


# --------------------------------------------------------------------------- the cache and roles


def test_explicit_settings_win_and_the_dashboard_is_never_read():
    ws = FakeWS(POWER_PREFS)
    mapping = GridMapping(settings(**{"grid.power_entity": "sensor.example_my_meter"}), ws_factory=lambda: ws)
    assert mapping.power_roles() == {"grid_power": "sensor.example_my_meter"}
    assert mapping.source() == "settings"
    assert ws.reads == 0


def test_dashboard_roles_stay_visible_under_an_explicit_override():
    mapping = GridMapping(
        settings(**{"grid.power_entity": "sensor.example_my_meter"}),
        ws_factory=lambda: FakeWS(POWER_PREFS),
    )
    # The panel needs the dashboard's own roles even when the effective mapping is explicit, so
    # the override can be told apart from the fallback it replaces.
    assert mapping.dashboard_roles()["grid_power"] == "sensor.example_net_power"
    assert mapping.dashboard_roles()["grid_energy_import"] == "sensor.zonneplan_electricity_consumption_today"
    assert mapping.power_roles() == {"grid_power": "sensor.example_my_meter"}
    assert mapping.source() == "settings"


def test_power_roles_come_from_the_dashboard_and_energy_stays_for_history():
    ws = FakeWS(POWER_PREFS)
    mapping = GridMapping(settings(), ws_factory=lambda: ws)
    assert mapping.power_roles() == {"grid_power": "sensor.example_net_power"}
    assert mapping.history_roles() == {"grid_power": "sensor.example_net_power"}
    assert mapping.source() == "energy_dashboard_power"
    assert "Energy dashboard" in mapping.note()


def test_energy_counters_are_history_only():
    mapping = GridMapping(settings(), ws_factory=lambda: FakeWS(ENERGY_PREFS))
    assert mapping.power_roles() == {}
    assert mapping.history_roles() == {
        "grid_energy_import": "sensor.zonneplan_electricity_consumption_today",
        "grid_energy_export": "sensor.zonneplan_electricity_returned_today",
    }
    assert mapping.source() == "energy_dashboard_energy"
    assert "energy" in mapping.note()


def test_the_dashboard_read_is_cached_for_the_ttl():
    clock = Clock()
    ws = FakeWS(POWER_PREFS)
    mapping = GridMapping(settings(), ws_factory=lambda: ws, clock=clock)
    mapping.power_roles()
    mapping.power_roles()
    clock.advance(seconds=299)
    mapping.power_roles()
    assert ws.reads == 1
    clock.advance(seconds=2)
    mapping.power_roles()
    assert ws.reads == 2


def test_an_unreachable_dashboard_is_cached_and_never_raises():
    clock = Clock()
    ws = FakeWS(error="no prefs")
    mapping = GridMapping(settings(), ws_factory=lambda: ws, clock=clock)
    assert mapping.power_roles() == {}
    assert mapping.history_roles() == {}
    assert mapping.source() == ""
    clock.advance(seconds=60)
    mapping.power_roles()
    assert ws.reads == 1, "a failed read must not turn every tick into a socket attempt"


def test_a_factory_that_raises_on_connect_is_cached_too():
    calls = []

    def broken():
        calls.append(1)
        raise RuntimeError("websocket refused")

    mapping = GridMapping(settings(), ws_factory=broken, clock=Clock())
    assert mapping.power_roles() == {}
    mapping.power_roles()
    assert len(calls) == 1


def test_no_factory_means_explicit_settings_only():
    mapping = GridMapping(settings())
    assert mapping.power_roles() == {}
    assert mapping.history_roles() == {}
    assert mapping.source() == ""
    assert "Energy dashboard has none" in mapping.note()
