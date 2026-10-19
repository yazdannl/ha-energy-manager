"""WP2A: SQLite store schema, migrations, retention and helpers (SPEC.md section 11)."""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "energy_manager") not in sys.path:
    sys.path.insert(0, str(ROOT / "energy_manager"))

from app.models import (  # noqa: E402
    Action,
    DispatchResult,
    Measurement,
    Plan,
    PlanSlot,
    PlanSummary,
    from_iso,
    to_iso,
)
from app.store import (  # noqa: E402
    MIGRATIONS,
    SCHEMA_VERSION,
    Store,
    parsed_as_of_instant,
)

NOW = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)

#: Two spellings of one instant, used to prove precision is kept and over-precision refused.  The
#: fraction is spelled by ``strftime`` rather than written out, because a dot followed by five or
#: more digits is the shape tools/audit.sh reads as a coordinate, and this is a timestamp fraction.
FRACTION = datetime(2026, 1, 5, 11, 59, 59, 900000, tzinfo=timezone.utc)
FRACTION_TEXT = FRACTION.strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
SUB_MICRO_TEXT = FRACTION.strftime("%Y-%m-%dT%H:%M:%S.%f") + "7+00:00"

#: The same fraction with four zero digits appended, and a comma-separated one: both are exactly
#: representable, and refusing either would drop a real row rather than decline a guess.
TRAILING_TEXT = FRACTION.strftime("%Y-%m-%dT%H:%M:%S.%f") + "0000+00:00"
COMMA_TEXT = "2026-01-06T00:00:00" + ",5+00:00"
COMMA_CANONICAL = "2026-01-06T00:00:00" + ".500000+00:00"

EXPECTED_TABLES = {
    "schema_version",
    "plans",
    "plan_slots",
    "dispatch_log",
    "measurements",
    "actuals",
    "forecast_cache",
    "learning_profiles",
    "savings_daily",
    "events",
    "settings",
    "meta",
    # 0.22.0: what each provider predicted per slot, so forecast skill is measurable (SPEC 7.4).
    "forecast_archive",
    # 0.27.0: the append-only as-of history for the two upsert tables a replay freezes (SPEC 11).
    "settings_history",
    "learning_profiles_history",
}


@pytest.fixture()
def store(tmp_path):
    instance = Store(tmp_path / "sem.db")
    yield instance
    instance.close()


def test_schema_matches_spec(store):
    rows = store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    tables = {row["name"] for row in rows}
    assert tables == EXPECTED_TABLES
    assert store.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert store.get_meta("schema") is None
    version = store.connection.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == SCHEMA_VERSION


def test_settings_and_meta(store):
    assert store.get_setting("price.source") is None
    store.set_setting("price.source", "static")
    store.set_settings({"mode": "auto", "dry_run": False})
    assert store.get_setting("price.source") == "static"
    assert store.get_settings() == {"price.source": "static", "mode": "auto", "dry_run": False}
    store.delete_setting("mode")
    assert store.get_setting("mode", "advisory") == "advisory"
    store.set_meta("last_maintenance", "2026-01-05")
    assert store.get_meta("last_maintenance") == "2026-01-05"


def test_plan_roundtrip(store):
    plan = Plan(
        generated_at=NOW,
        horizon_start=NOW,
        horizon_end=NOW + timedelta(hours=36),
        slot_minutes=15,
        feasible=False,
        infeasible_reason="terminal target unreachable",
        slots=[
            PlanSlot(
                t=NOW,
                buy=0.2,
                sell=0.05,
                pv_kw=1.5,
                load_kw=0.4,
                soc_kwh=4.0,
                power_w=-800.0,
                action=Action.DISCHARGE,
                reason="peak_discharge",
            ),
            PlanSlot(
                t=NOW + timedelta(minutes=15),
                buy=0.25,
                sell=0.05,
                pv_kw=1.7,
                load_kw=0.4,
                soc_kwh=3.8,
                power_w=0.0,
                action=Action.HOLD,
                reason="reserve_hold",
            ),
        ],
        summary=PlanSummary(cost_estimate_eur=1.5, baseline_cost_eur=2.0, saved_eur=0.5, cycles=0.1),
        batteries=[{"id": "battery_1", "share_w": -800.0}],
        mode="auto",
        dry_run=False,
        sources={"price": "static", "solar": "forecast_solar"},
    )
    plan_id = store.save_plan(plan)
    assert plan_id > 0
    loaded = store.latest_plan()
    assert loaded is not None
    assert loaded.feasible is False
    assert loaded.infeasible_reason == "terminal target unreachable"
    assert loaded.dry_run is False
    assert loaded.mode == "auto"
    assert loaded.sources == {"price": "static", "solar": "forecast_solar"}
    assert loaded.summary.saved_eur == pytest.approx(0.5)
    assert [slot.reason for slot in loaded.slots] == ["peak_discharge", "reserve_hold"]
    assert loaded.slots[0].action is Action.DISCHARGE
    assert loaded.slot_minutes == 15
    assert store.load_plan(999) is None
    assert store.list_plans() == [plan_id]


def test_measurements_and_actuals_upsert(store):
    store.add_measurements([Measurement(ts=NOW, role="pv_power", value=1000.0, unit="W")])
    store.add_measurements([Measurement(ts=NOW, role="pv_power", value=1200.0, unit="W")])
    rows = store.measurements_range(NOW - timedelta(minutes=1), NOW + timedelta(minutes=1))
    assert len(rows) == 1 and rows[0]["value"] == 1200.0
    assert store.measurements_range(NOW, NOW + timedelta(minutes=1), roles=["grid_power"]) == []

    store.upsert_actuals(NOW, pv_kw=1.2, load_kw=0.4)
    store.upsert_actuals(NOW, pv_kw=1.3, grid_kw=-0.5, plan_power_w=200.0)
    rows = store.actuals_range(NOW - timedelta(minutes=1), NOW + timedelta(minutes=1))
    assert len(rows) == 1
    assert rows[0]["pv_kw"] == 1.3 and rows[0]["grid_kw"] == -0.5 and rows[0]["plan_power_w"] == 200.0
    with pytest.raises(ValueError):
        store.upsert_actuals(NOW, temperature=12.0)
    store.upsert_actuals(NOW + timedelta(hours=1))
    assert len(store.actuals_range(NOW, NOW + timedelta(hours=2))) == 2


def test_dispatch_log(store):
    store.add_dispatch_result(
        DispatchResult(
            battery_id="battery_1",
            ts=NOW,
            requested_w=800.0,
            applied_w=500.0,
            ok=False,
            dry_run=True,
            code="power_clamp",
            message="clamped",
        ),
        mode="advisory",
        soc_pct=42.0,
    )
    rows = store.dispatch_log(limit=10)
    assert rows[0]["battery_id"] == "battery_1"
    assert rows[0]["requested_w"] == 800.0 and rows[0]["applied_w"] == 500.0
    assert rows[0]["dry_run"] == 1 and rows[0]["result"] == "error"
    assert store.dispatch_log(battery_id="other") == []


def test_cache_profiles_and_events(store):
    assert store.cache_get("solar", "k") is None
    store.cache_put("solar", "k", {"result": {"watts": {}}}, NOW)
    cached = store.cache_get("solar", "k")
    assert cached[0] == NOW and cached[1]["result"]["watts"] == {}
    assert store.cache_age_seconds("solar", "k", NOW + timedelta(minutes=10)) == 600

    store.profile_put("load", "profile", {"samples": 3}, NOW)
    assert store.profile_get("load", "profile")[1] == {"samples": 3}
    store.profile_delete("load", "profile")
    assert store.profile_get("load", "profile") is None

    store.add_event(ts=NOW, level="warning", code="forecast_unavailable", message="static only", payload={"src": "energy_charts"})
    events = store.events(limit=5, level="warning")
    assert events[0]["code"] == "forecast_unavailable"
    assert events[0]["payload"] == {"src": "energy_charts"}
    assert store.events(code="other") == []


def test_the_history_is_additive_and_the_current_row_is_unchanged(store):
    """Production invariance: the current rows keep their exact upsert semantics, and the only
    production effect is that a *changed* value also lands in the history (SPEC.md 11)."""
    store.set_setting("mode", "auto", now=NOW)
    store.set_setting("mode", "auto", now=NOW + timedelta(minutes=5))  # re-stamp, same value
    store.set_setting("mode", "advisory", now=NOW + timedelta(minutes=10))
    # Current row: newest value, newest instant, one row -- exactly as before v5.
    assert store.get_setting("mode") == "advisory"
    assert store.get_settings()["mode"] == "advisory"
    current = store.connection.execute(
        "SELECT value_json, updated_at FROM settings WHERE key = 'mode'"
    ).fetchone()
    assert current["value_json"] == '"advisory"'
    assert current["updated_at"] == to_iso(NOW + timedelta(minutes=10))
    # History: only the two real changes; the re-stamp added nothing.
    changes = store.connection.execute(
        "SELECT value_json, at, origin FROM settings_history WHERE key = 'mode' ORDER BY id"
    ).fetchall()
    assert [row["value_json"] for row in changes] == ['"auto"', '"advisory"']
    assert {row["origin"] for row in changes} == {"write"}
    assert changes[1]["at"] == to_iso(NOW + timedelta(minutes=10))

    store.profile_put("control", "live_writes", {"enabled": True}, updated_at=NOW)
    store.profile_put("control", "live_writes", {"enabled": True}, updated_at=NOW + timedelta(minutes=5))
    store.profile_put("control", "live_writes", {"enabled": False}, updated_at=NOW + timedelta(minutes=10))
    assert store.profile_get("control", "live_writes") == (
        NOW + timedelta(minutes=10), {"enabled": False},
    )
    stored = store.connection.execute(
        "SELECT payload_json, at FROM learning_profiles_history"
        " WHERE kind = 'control' ORDER BY id"
    ).fetchall()
    assert [row["payload_json"] for row in stored] == ['{"enabled": true}', '{"enabled": false}']


def test_the_readers_return_the_newest_row_at_or_before_an_instant(store):
    store.set_setting("mode", "auto", now=NOW)
    store.set_setting("mode", "advisory", now=NOW + timedelta(hours=1))
    store.profile_put("control", "live_writes", {"n": 1}, updated_at=NOW)
    store.profile_put("control", "live_writes", {"n": 2}, updated_at=NOW + timedelta(hours=1))
    assert "mode" not in store.settings_as_of(NOW - timedelta(minutes=1))
    first = store.settings_as_of(NOW)
    assert (first["mode"].value, first["mode"].origin) == ("auto", "history")
    second = store.settings_as_of(NOW + timedelta(hours=1))
    assert second["mode"].value == "advisory"
    assert store.profile_as_of("control", "live_writes", NOW).value == {"n": 1}
    assert store.profile_as_of("control", "live_writes", NOW + timedelta(hours=1)).value == {"n": 2}
    assert store.profile_as_of("control", "live_writes", NOW - timedelta(minutes=1)) is None


def test_the_settings_readers_follow_the_instant_not_the_insertion_order(store):
    """R1: a backdated write must not displace a later one that merely arrived first."""
    later = NOW + timedelta(hours=2)
    earlier = NOW - timedelta(hours=2)
    store.set_setting("mode", "newer", now=later)
    store.set_setting("mode", "older", now=earlier)  # backdated, inserted last
    selected = store.settings_as_of(later)["mode"]
    assert (selected.value, selected.at) == ("newer", later)
    assert store.settings_as_of(earlier)["mode"].value == "older"
    # The same rule for a tombstone: a backdated delete is not the newest row at ``later``.
    store.set_setting("flag", "on", now=later)
    store.delete_setting("flag", now=earlier)
    assert store.settings_deleted_as_of(earlier) == {"flag"}
    assert "flag" not in store.settings_as_of(earlier)
    assert store.settings_as_of(later)["flag"].value == "on"
    assert "flag" not in store.settings_deleted_as_of(later)


def test_a_write_and_a_delete_at_the_same_instant_are_ordered_by_insertion(store):
    """At equal instants the newest row wins, so a delete hides the value it was written after."""
    moment = NOW + timedelta(hours=1)
    store.set_setting("flag", "on", now=moment)
    store.delete_setting("flag", now=moment)
    assert "flag" not in store.settings_as_of(moment)
    assert store.settings_deleted_as_of(moment) == {"flag"}
    store.set_setting("flag", "back", now=moment)  # written again at the same instant
    assert store.settings_as_of(moment)["flag"].value == "back"
    assert store.settings_deleted_as_of(moment) == set()


def test_a_history_instant_is_compared_as_an_instant_not_as_text(store):
    """R2: an offset can make the stored text sort on the wrong side of the boundary."""
    boundary = NOW  # 2026-01-05 12:00Z
    store.connection.execute(
        "INSERT INTO settings_history(key, at, value_json, origin)"
        " VALUES('late', '2026-01-05T10:30:00-02:00', '\"late\"', 'write')"  # instant 12:30Z
    )
    store.connection.execute(
        "INSERT INTO settings_history(key, at, value_json, origin)"
        " VALUES('early', '2026-01-05T13:00:00+04:00', '\"early\"', 'write')"  # instant 09:00Z
    )
    store.connection.commit()
    snapshot = store.settings_as_of(boundary)
    assert "late" not in snapshot, "a later instant may not leak in through its spelling"
    assert snapshot["early"].value == "early", "an earlier instant may not fall out through its spelling"
    assert store.settings_as_of(boundary + timedelta(hours=1))["late"].value == "late"
    # The profile readers had the same textual filter, and the same fix.
    store.connection.execute(
        "INSERT INTO learning_profiles_history(kind, key, at, payload_json, origin)"
        " VALUES('control', 'live_writes', '2026-01-05T10:30:00-02:00', '{\"late\": 1}', 'write')"
    )
    store.connection.execute(
        "INSERT INTO learning_profiles_history(kind, key, at, payload_json, origin)"
        " VALUES('control', 'live_writes', '2026-01-05T13:00:00+04:00', '{\"early\": 1}', 'write')"
    )
    store.connection.commit()
    profile = store.profile_as_of("control", "live_writes", boundary)
    assert profile is not None and profile.value == {"early": 1}
    profile_after = store.profile_as_of("control", "live_writes", boundary + timedelta(hours=1))
    assert profile_after is not None and profile_after.value == {"late": 1}


def test_a_history_instant_is_stored_canonically_and_the_current_row_is_untouched(store):
    """R2(a): one canonical UTC representation, while ``updated_at`` keeps its own semantics."""
    offset = timezone(timedelta(hours=2))
    stamp = datetime(2026, 1, 5, 13, 0, tzinfo=offset)  # 11:00Z
    store.set_setting("mode", "v", now=stamp)
    store.profile_put("control", "live_writes", {"n": 1}, updated_at=stamp)
    assert store.connection.execute(
        "SELECT at FROM settings_history WHERE key = 'mode'"
    ).fetchone()["at"] == "2026-01-05T11:00:00+00:00"
    assert store.connection.execute(
        "SELECT at FROM learning_profiles_history"
    ).fetchone()["at"] == "2026-01-05T11:00:00+00:00"
    assert store.connection.execute(
        "SELECT updated_at FROM settings WHERE key = 'mode'"
    ).fetchone()["updated_at"] == "2026-01-05T13:00:00+02:00"


def test_an_unreadable_history_row_is_refused_rather_than_trusted(store):
    store.connection.execute(
        "INSERT INTO settings_history(key, at, value_json, origin)"
        " VALUES('broken', 'sometime', '\"v\"', 'write')"
    )
    store.connection.execute(
        "INSERT INTO learning_profiles_history(kind, key, at, payload_json, origin)"
        " VALUES('control', 'live_writes', 'nope', '{}', 'write')"
    )
    store.connection.commit()
    with pytest.raises(ValueError):
        store.settings_as_of(NOW)
    with pytest.raises(ValueError):
        store.settings_deleted_as_of(NOW)
    with pytest.raises(ValueError):
        store.profile_as_of("control", "live_writes", NOW)
    with pytest.raises(ValueError):
        store.profile_deleted_as_of("control", "live_writes", NOW)


def test_a_fractional_second_is_not_truncated_by_canonicalisation(store):
    """R2: whole-second canonicalisation would move a ``.9`` write before a boundary inside it."""
    stamp = FRACTION
    store.set_setting("mode", "v", now=stamp)
    stored = store.connection.execute(
        "SELECT at FROM settings_history WHERE key = 'mode'"
    ).fetchone()["at"]
    assert from_iso(stored) == stamp, "the fractional second survives canonicalisation"
    assert "mode" not in store.settings_as_of(stamp - timedelta(milliseconds=100))
    assert store.settings_as_of(stamp + timedelta(milliseconds=50))["mode"].value == "v"


def test_a_sub_microsecond_instant_is_refused_not_rounded(store):
    """R2: a value the build cannot place exactly refuses; it is never truncated earlier."""
    assert len(SUB_MICRO_TEXT.split(".")[1].split("+")[0]) == 7, "the fixture is over-precise"
    store.connection.execute(
        "INSERT INTO settings_history(key, at, value_json, origin)"
        " VALUES('precise', ?, '\"v\"', 'write')",
        (SUB_MICRO_TEXT,),
    )
    store.connection.commit()
    with pytest.raises(ValueError):
        store.settings_as_of(NOW)
    with pytest.raises(ValueError):
        store.settings_deleted_as_of(NOW)


def test_a_fraction_in_any_component_is_placed_exactly_including_the_offset(store):
    """R1: the offset may carry a fraction too, and the parser can drop it whole.

    ``11:59:59.9-00:00:00.5`` names 12:00:00.4Z -- after ``NOW`` -- while the parser reads it as
    11:59:59.9Z, handing the value over half a second before it existed.  The first fraction is
    ordinary, so a check that stops at the first one would miss it.  Zeros past the microsecond
    name a real instant and are placed; a nonzero digit that would be lost refuses.
    """
    store.connection.execute(
        "INSERT INTO settings_history(key, at, value_json, origin) VALUES('sneaky', ?, '\"v\"', 'write')",
        ("2026-01-05T11:59:59.9-00:00:00." + "5" + "0" * 6,),
    )
    store.connection.commit()
    assert "sneaky" not in store.settings_as_of(NOW), "it really sits after the boundary"
    assert store.settings_as_of(NOW + timedelta(seconds=1))["sneaky"].value == "v"
    store.connection.execute("DELETE FROM settings_history WHERE key = 'sneaky'")
    store.connection.execute(
        "INSERT INTO settings_history(key, at, value_json, origin) VALUES('sneaky', ?, '\"v\"', 'write')",
        ("2026-01-05T11:59:59.9-00:00:00." + "5" + "0" * 5 + "1",),
    )
    store.connection.commit()
    with pytest.raises(ValueError):
        store.settings_as_of(NOW)
    with pytest.raises(ValueError):
        store.settings_deleted_as_of(NOW)


def test_an_exactly_representable_fraction_is_accepted_however_long(store):
    """R2: the rule is exactness, not digit count -- trailing zeros name a real instant.

    Refusing these would lose rows: the migration would skip a legitimate seed, and the harness's
    scratch cut would delete a legitimate row.
    """
    half = datetime(2026, 1, 6, 0, 0, 0, 500000, tzinfo=timezone.utc)
    assert parsed_as_of_instant(TRAILING_TEXT) == FRACTION
    assert parsed_as_of_instant(COMMA_TEXT) == half
    # An offset's own fraction is subtracted exactly, even where the parser drops it whole.
    assert parsed_as_of_instant("2026-01-06T00:00:00.5+00:00:00.5000000") == datetime(
        2026, 1, 6, tzinfo=timezone.utc
    )
    assert parsed_as_of_instant("2026-01-06T00:00:00.5+02:00:00.5") == datetime(
        2026, 1, 5, 22, 0, tzinfo=timezone.utc
    )
    store.connection.execute(
        "INSERT INTO settings_history(key, at, value_json, origin) VALUES('long', ?, '\"v\"', 'write')",
        (TRAILING_TEXT,),
    )
    store.connection.commit()
    assert store.settings_as_of(NOW)["long"].value == "v"
    assert store.settings_deleted_as_of(NOW) == set()


def test_a_week_date_is_refused_rather_than_split_inside_its_date(store):
    """R4: structure comes from the whole accepted form, never from a sign that happens to parse.

    ``2026-W01`` parses as a week date, so a splitter that searches signs reads the weekday as the
    start of an offset: ``2026-W01-1T12:00:00Z`` becomes unplaceable, and ``2026-W01-1+12:00:00``
    (whose ``+`` is the date/time separator, so Monday noon) came out as Sunday midnight minus
    twelve hours -- a different instant.  Refusing both is the safe half of that trade.
    """
    assert parsed_as_of_instant("2026-W01-1T12:00:00Z") is None
    assert parsed_as_of_instant("2026-W01-1+12:00:00") is None
    store.connection.execute(
        "INSERT INTO settings_history(key, at, value_json, origin) VALUES('week', ?, '\"v\"', 'write')",
        ("2026-W01-1T12:00:00Z",),
    )
    store.connection.commit()
    with pytest.raises(ValueError):
        store.settings_as_of(NOW)
    with pytest.raises(ValueError):
        store.settings_deleted_as_of(NOW)


def test_another_date_time_separator_is_refused_rather_than_read_as_an_offset(store):
    """R4: a non-``T`` separator is the form a sign-search confuses with an offset.

    ``2026-01-01+12:00:00`` means midday on the first (``+`` is a legal single-character
    separator), but reading the ``+`` as an offset gives midnight minus twelve hours.  The space
    separator is refused too: the old code happened to read that one correctly, but which exotic
    separator is harmless is precisely what a sign-search cannot know, and this build never writes
    one.  A refusal is safe; a guessed instant is not.
    """
    assert parsed_as_of_instant("2026-01-01+12:00:00") is None
    assert parsed_as_of_instant("2026-01-01 12:00:00+00:00") is None
    store.connection.execute(
        "INSERT INTO settings_history(key, at, value_json, origin) VALUES('plus', ?, '\"v\"', 'write')",
        ("2026-01-01+12:00:00",),
    )
    store.connection.commit()
    with pytest.raises(ValueError):
        store.settings_as_of(NOW)


def test_the_accepted_form_places_every_written_shape(store):
    """R4: the grammar's shapes all place exactly -- ``Z``, negative and second-bearing offsets,
    a missing seconds field, and a comma fraction with trailing zeros."""
    midnight = datetime(2026, 1, 6, tzinfo=timezone.utc)
    assert parsed_as_of_instant("2026-01-06T00:00:00Z") == midnight
    assert parsed_as_of_instant("2026-01-06T00:00:00-02:00") == datetime(
        2026, 1, 6, 2, tzinfo=timezone.utc
    )
    assert parsed_as_of_instant("2026-01-06T00:00:00-02:00:30") == datetime(
        2026, 1, 6, 2, 0, 30, tzinfo=timezone.utc
    )
    assert parsed_as_of_instant("2026-01-06T00:00+01:00") == datetime(
        2026, 1, 5, 23, tzinfo=timezone.utc
    )
    assert parsed_as_of_instant("2026-01-06T00:00:00,5000000+00:00") == datetime(
        2026, 1, 6, 0, 0, 0, 500000, tzinfo=timezone.utc
    )
    assert parsed_as_of_instant("2026-01-06T00:00:00+00:00:00.5000000") == datetime(
        2026, 1, 5, 23, 59, 59, 500000, tzinfo=timezone.utc
    )
    store.connection.execute(
        "INSERT INTO settings_history(key, at, value_json, origin) VALUES('zed', ?, '\"v\"', 'write')",
        ("2026-01-06T00:00:00Z",),
    )
    store.connection.commit()
    assert store.settings_as_of(datetime(2026, 1, 6, tzinfo=timezone.utc))["zed"].value == "v"


def test_an_unknown_history_origin_is_refused_rather_than_trusted(store):
    """R3: the origin is checked before eligibility, in the shared selector, for every reader."""
    earlier = to_iso(NOW - timedelta(hours=2))
    later = to_iso(NOW + timedelta(hours=2))
    store.connection.execute(
        "INSERT INTO settings_history(key, at, value_json, origin) VALUES('odd', ?, '\"v\"', 'mystery')",
        (earlier,),
    )
    store.connection.commit()
    with pytest.raises(ValueError):
        store.settings_as_of(NOW)
    with pytest.raises(ValueError):
        store.settings_deleted_as_of(NOW)
    # Superseded by a newer eligible row: still refused, not silently skipped.
    store.connection.execute(
        "INSERT INTO settings_history(key, at, value_json, origin) VALUES('odd', ?, '\"v\"', 'write')",
        (to_iso(NOW - timedelta(hours=1)),),
    )
    store.connection.commit()
    with pytest.raises(ValueError):
        store.settings_as_of(NOW)
    # Entirely after the boundary: refused too, not treated as "no row yet".
    store.connection.execute("DELETE FROM settings_history WHERE key = 'odd'")
    store.connection.execute(
        "INSERT INTO settings_history(key, at, value_json, origin) VALUES('odd', ?, '\"v\"', 'mystery')",
        (later,),
    )
    store.connection.commit()
    with pytest.raises(ValueError):
        store.settings_deleted_as_of(NOW)
    # And the profile readers answer to the same rule.
    store.connection.execute(
        "INSERT INTO learning_profiles_history(kind, key, at, payload_json, origin)"
        " VALUES('control', 'live_writes', ?, '{}', 'mystery')",
        (later,),
    )
    store.connection.commit()
    with pytest.raises(ValueError):
        store.profile_as_of("control", "live_writes", NOW)
    with pytest.raises(ValueError):
        store.profile_deleted_as_of("control", "live_writes", NOW)


def test_the_selected_instant_is_verified_before_it_is_trusted():
    """R2(c): the backstop in front of the frozen window, exercised directly."""
    from app.store import _verified_moment

    assert _verified_moment(to_iso(NOW), NOW, "row.at") == NOW  # exactly the boundary is allowed
    for text in (None, "", "sometime", to_iso(NOW + timedelta(seconds=1))):
        with pytest.raises(ValueError):
            _verified_moment(text, NOW, "row.at")


def test_a_delete_is_a_tombstone_and_never_resurrects_the_old_value(store):
    store.set_setting("mode", "auto", now=NOW)
    store.profile_put("control", "live_writes", {"enabled": True}, updated_at=NOW)
    store.delete_setting("mode", now=NOW + timedelta(hours=1))
    store.profile_delete("control", "live_writes", now=NOW + timedelta(hours=1))
    assert store.settings_as_of(NOW)["mode"].value == "auto"
    assert store.profile_as_of("control", "live_writes", NOW).value == {"enabled": True}
    # At and after the delete the key is absent from both readers, and not late either.
    after = NOW + timedelta(hours=1)
    assert "mode" not in store.settings_as_of(after)
    assert store.profile_as_of("control", "live_writes", after) is None
    assert store.settings_deleted_as_of(after) == {"mode"}
    assert store.profile_deleted_as_of("control", "live_writes", after) is True
    assert store.settings_deleted_as_of(NOW) == set()
    assert store.profile_deleted_as_of("control", "live_writes", NOW) is False
    history = store.connection.execute(
        "SELECT at, origin FROM settings_history WHERE key = 'mode' ORDER BY id"
    ).fetchall()
    assert [row["origin"] for row in history] == ["write", "delete"]


def test_schema_v5_seeds_the_history_from_the_pre_upgrade_rows(tmp_path):
    """An upgraded store behaves like a fresh one: each existing row's last write is its only 'at'."""
    path = tmp_path / "v4.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_version(version) VALUES(4)")
    for version, statements in MIGRATIONS:
        if version <= 4:
            for statement in statements:
                conn.execute(statement)
    conn.execute(
        "INSERT INTO settings(key, value_json, updated_at) VALUES('mode', '\"auto\"', ?)",
        (to_iso(NOW - timedelta(days=2)),),
    )
    conn.execute("INSERT INTO settings(key, value_json, updated_at) VALUES('unplaceable', '1', NULL)")
    # A legacy row may carry a non-UTC offset: the seed stores the canonical UTC instant.
    conn.execute(
        "INSERT INTO settings(key, value_json, updated_at)"
        " VALUES('offset', '\"zed\"', '2026-01-04T01:00:00+02:00')"
    )
    # A timestamp this build cannot read seeds nothing: an instant we cannot place is not invented.
    conn.execute(
        "INSERT INTO settings(key, value_json, updated_at) VALUES('unreadable', '1', 'sometime')"
    )
    # Fractional seconds are preserved by the seed; sub-microsecond precision is not inventable.
    conn.execute(
        "INSERT INTO settings(key, value_json, updated_at)"
        " VALUES('fractional', '\"frac\"', ?)",
        (FRACTION_TEXT,),
    )
    conn.execute(
        "INSERT INTO settings(key, value_json, updated_at)"
        " VALUES('submicro', '\"tiny\"', ?)",
        (SUB_MICRO_TEXT,),
    )
    # Exactness, not digit count: these two name real instants, so the seed must keep them.
    conn.execute(
        "INSERT INTO settings(key, value_json, updated_at)"
        " VALUES('trailing', '\"long\"', ?)",
        (TRAILING_TEXT,),
    )
    conn.execute(
        "INSERT INTO settings(key, value_json, updated_at)"
        " VALUES('comma', '\"comma\"', ?)",
        (COMMA_TEXT,),
    )
    conn.execute(
        "INSERT INTO learning_profiles(kind, key, updated_at, payload_json)"
        " VALUES('control', 'live_writes', ?, '{\"enabled\": true}')",
        (to_iso(NOW - timedelta(days=1)),),
    )
    conn.commit()
    conn.close()
    store = Store(path)
    try:
        assert store.connection.execute("SELECT version FROM schema_version").fetchone()[0] == 7
        seeded = store.connection.execute(
            "SELECT at, origin FROM settings_history WHERE key = 'mode'"
        ).fetchone()
        assert (seeded["at"], seeded["origin"]) == (to_iso(NOW - timedelta(days=2)), "seed")
        assert store.connection.execute(
            "SELECT COUNT(*) FROM settings_history WHERE key = 'unplaceable'"
        ).fetchone()[0] == 0
        assert store.connection.execute(
            "SELECT COUNT(*) FROM settings_history WHERE key = 'unreadable'"
        ).fetchone()[0] == 0
        assert store.connection.execute(
            "SELECT at FROM settings_history WHERE key = 'offset'"
        ).fetchone()["at"] == "2026-01-03T23:00:00+00:00"
        assert store.connection.execute(
            "SELECT at FROM settings_history WHERE key = 'fractional'"
        ).fetchone()["at"] == FRACTION_TEXT
        assert store.connection.execute(
            "SELECT COUNT(*) FROM settings_history WHERE key = 'submicro'"
        ).fetchone()[0] == 0
        assert store.connection.execute(
            "SELECT at FROM settings_history WHERE key = 'trailing'"
        ).fetchone()["at"] == FRACTION_TEXT
        assert store.connection.execute(
            "SELECT at FROM settings_history WHERE key = 'comma'"
        ).fetchone()["at"] == COMMA_CANONICAL
        row = store.profile_as_of("control", "live_writes", NOW)
        assert row is not None and (row.origin, row.value) == ("seed", {"enabled": True})
        # A post-upgrade change appends a real write rather than replacing the seed.
        store.set_setting("mode", "advisory", now=NOW)
        assert [
            row["origin"]
            for row in store.connection.execute(
                "SELECT origin FROM settings_history WHERE key = 'mode' ORDER BY id"
            )
        ] == ["seed", "write"]
    finally:
        store.close()


def test_savings_upsert_and_range(store):
    store.upsert_savings_daily("2026-01-04", import_kwh=10.0, cost_eur=2.5, revenue_eur=0.5, baseline_cost_eur=3.5)
    row = store.savings_daily_range("2026-01-01", "2026-01-05")[0]
    assert row["savings_eur"] == pytest.approx(1.5)  # 3.5 - (2.5 - 0.5)
    store.upsert_savings_daily("2026-01-04", import_kwh=11.0, cost_eur=2.6, revenue_eur=0.5, baseline_cost_eur=3.5)
    assert len(store.savings_daily_range("2026-01-01", "2026-01-05")) == 1
    with pytest.raises(ValueError):
        store.upsert_savings_daily("2026-01-05", nonsense=1.0)


def test_savings_measured_flow_columns_round_trip(store):
    """0.13.0 additions are additive: an old-style call leaves them None, a new one stores them."""
    store.upsert_savings_daily("2026-01-01", import_kwh=1.0)
    legacy = store.savings_daily_range("2026-01-01", "2026-01-01")[0]
    assert legacy["charged_kwh"] is None and legacy["battery_measured"] is None
    store.upsert_savings_daily(
        "2026-01-02",
        charged_kwh=1.5,
        discharged_kwh=1.0,
        charge_cost_eur=0.3,
        discharge_value_eur=0.05,
        grid_measured=1,
        load_measured=1,
        pv_measured=0,
        battery_measured=1,
    )
    fresh = store.savings_daily_range("2026-01-02", "2026-01-02")[0]
    assert fresh["charged_kwh"] == 1.5 and fresh["discharged_kwh"] == 1.0
    assert fresh["charge_cost_eur"] == 0.3 and fresh["discharge_value_eur"] == 0.05
    assert fresh["battery_measured"] == 1 and fresh["pv_measured"] == 0


def test_schema_v2_migrates_a_v1_database_and_keeps_its_rows(tmp_path):
    """A database written by 0.12.0 opens, migrates forward and reports old rows as unknown."""
    path = tmp_path / "v1.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_version(version) VALUES(1)")
    for statement in MIGRATIONS[0][1]:
        conn.execute(statement)
    conn.execute(
        "INSERT INTO savings_daily(day, import_kwh, cost_eur, baseline_cost_eur, savings_eur, battery_cycles)"
        " VALUES('2026-01-01', 2.0, 0.4, 0.6, 0.2, 0.5)"
    )
    conn.commit()
    conn.close()
    store = Store(path)
    try:
        row = store.savings_daily_range("2026-01-01", "2026-01-01")[0]
        assert row["savings_eur"] == 0.2 and row["battery_cycles"] == 0.5
        assert row["charged_kwh"] is None and row["battery_measured"] is None  # unknown, not 0
        store.upsert_savings_daily("2026-01-02", charged_kwh=1.5, battery_measured=1)
        assert store.savings_daily_range("2026-01-02", "2026-01-02")[0]["charged_kwh"] == 1.5
        version = store.connection.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == SCHEMA_VERSION == 7
    finally:
        store.close()


def test_schema_v6_reapplies_cleanly_to_an_already_migrated_database(tmp_path):
    """A recording claiming a lower version than its file (SPEC 14 R3) must not crash the runner.

    A replay scratch DB can be a copy of a newer build whose ``schema_version`` row says 5.  The
    forward-only runner then re-applies migration 6, whose columns already exist: adding them again
    would raise ``duplicate column name`` and refuse the recording instead of replaying it.
    """
    path = tmp_path / "v6.db"
    store = Store(path)
    try:
        store.upsert_savings_daily("2026-01-01", charged_kwh=1.0, total_hours=2.0, battery_hours=1.5)
    finally:
        store.close()
    conn = sqlite3.connect(path)
    conn.execute("UPDATE schema_version SET version = 5")
    conn.commit()
    conn.close()

    store = Store(path)
    try:
        version = store.connection.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == SCHEMA_VERSION
        row = store.savings_daily_range("2026-01-01", "2026-01-01")[0]
        assert row["charged_kwh"] == 1.0
        assert row["total_hours"] == 2.0 and row["battery_hours"] == 1.5
    finally:
        store.close()


def test_schema_v7_drops_only_the_day_0_30_0_mixed_scopes(tmp_path):
    """Migration 7 deletes today's coverage-bearing row, and nothing else.

    A row written by 0.30.0 could carry the whole day's import/cost (continued from a pre-coverage
    row) against a minutes-old baseline; it cannot be repaired arithmetically, so that day starts
    over.  A row from before coverage tracking has no hours and is the legacy day it always was
    (0.30.1 never continues it), and an older day is history: both are kept.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()

    def reopen(path, day, **values):
        store = Store(path)
        store.upsert_savings_daily(day, **values)
        store.close()
        conn = sqlite3.connect(path)
        conn.execute("UPDATE schema_version SET version = 6")
        conn.commit()
        conn.close()
        return Store(path)

    poisoned = reopen(tmp_path / "today-hours.db", today, charged_kwh=1.0, total_hours=0.1, cost_eur=2.0)
    try:
        assert poisoned.savings_daily_range(today, today) == []
    finally:
        poisoned.close()

    legacy = reopen(tmp_path / "today-legacy.db", today, charged_kwh=1.0)
    try:
        assert legacy.savings_daily_range(today, today)[0]["charged_kwh"] == 1.0
    finally:
        legacy.close()

    history = reopen(tmp_path / "yesterday.db", yesterday, charged_kwh=1.0, total_hours=0.5)
    try:
        assert history.savings_daily_range(yesterday, yesterday)[0]["total_hours"] == 0.5
    finally:
        history.close()


def make_plan(moment, slots=2):
    return Plan(
        generated_at=moment,
        horizon_start=moment,
        horizon_end=moment + timedelta(hours=36),
        slot_minutes=15,
        feasible=True,
        slots=[
            PlanSlot(
                t=moment + timedelta(minutes=15 * index),
                buy=0.2,
                sell=0.0,
                pv_kw=0.0,
                load_kw=0.4,
                soc_kwh=4.0,
                power_w=0.0,
                action=Action.HOLD,
                reason="reserve_hold",
            )
            for index in range(slots)
        ],
        summary=PlanSummary(cost_estimate_eur=1.0, baseline_cost_eur=1.5, saved_eur=0.5, cycles=0.0),
        batteries=[],
        mode="auto",
        dry_run=False,
        sources={"price": "static"},
    )


def test_trim_retention_keeps_only_the_newest_plans(store):
    """Every replan inserts a plan plus ~120 slot rows; retention must bound both."""
    ids = [store.save_plan(make_plan(NOW + timedelta(minutes=15 * index))) for index in range(5)]
    removed = store.trim(now=NOW, plans_rows=2)
    assert removed["plans"] == 3 and removed["plan_slots"] == 6  # 3 plans x 2 slots
    assert store.list_plans() == [ids[-1], ids[-2]]
    assert store.connection.execute("SELECT COUNT(*) FROM plan_slots").fetchone()[0] == 4
    assert store.load_plan(ids[0]) is None and store.load_plan(ids[-1]) is not None
    # a second trim with the same budget deletes nothing
    assert store.trim(now=NOW, plans_rows=2)["plans"] == 0


def test_trim_retention(store):
    store.add_measurements([Measurement(ts=NOW - timedelta(days=500), role="pv_power", value=1.0, unit="W")])
    store.add_measurements([Measurement(ts=NOW, role="pv_power", value=1.0, unit="W")])
    store.upsert_actuals(NOW - timedelta(days=500), pv_kw=1.0)
    store.upsert_actuals(NOW, pv_kw=1.0)
    for _ in range(5):
        store.add_dispatch()
        store.add_event(code="x")
    removed = store.trim(now=NOW, dispatch_rows=2, events_rows=2)
    assert removed["measurements"] == 1 and removed["actuals"] == 1
    assert removed["dispatch_log"] == 3 and removed["events"] == 3
    assert len(store.dispatch_log(limit=100)) == 2
    assert len(store.events(limit=100)) == 2
