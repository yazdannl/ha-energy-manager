"""Episode extraction: the read-only copy, the as-of rule and every refusal (REPLAY_HARNESS 1-3)."""

from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "energy_manager"))

from app import capacity as capacity_learning
from app.main import EXAMPLE_BATTERY, _selfcheck_settings
from app.models import (
    Action,
    Measurement,
    Plan,
    PlanSlot,
    PlanSummary,
    quarter_floor,
    to_iso,
)
from app.replay.episode import (
    INCOMPLETE_ACTUALS,
    LIVE_STORE,
    MALFORMED_JSON,
    NO_VINTAGE,
    NON_ANTICIPATION,
    NOT_SQLITE,
    UNFLUSHED_WAL,
    WINDOW,
    EpisodeError,
    open_episode,
)
from app.store import Store

START = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
END = START + timedelta(hours=1)
BATTERY = EXAMPLE_BATTERY


def settings_values(extra: dict | None = None) -> dict:
    values = _selfcheck_settings().all()
    values.update(
        {
            "mode": "auto",
            "dry_run": False,
            "control.grid_follow": False,
            "batteries": [asdict(BATTERY)],
        }
    )
    values.update(extra or {})
    return values


def default_actuals(start: datetime = START, end: datetime = END) -> list[dict]:
    rows = []
    moment = quarter_floor(start, 5)
    while moment < end:
        rows.append(
            {
                "ts": to_iso(moment),
                "pv_kw": 1.0,
                "load_kw": 1.2,
                "grid_kw": 0.2,
                "battery_kw": 0.0,
                "soc_kwh": 2.75,
                "price_buy": 0.25,
                "price_sell": 0.05,
            }
        )
        moment += timedelta(minutes=5)
    return rows


def default_measurements(start: datetime = START, end: datetime = END) -> list[Measurement]:
    rows = []
    moment = quarter_floor(start, 5)
    index = 0
    while moment < end:
        rows.append(
            Measurement(ts=moment, role=f"battery_soc:{BATTERY.id}", value=55.0 + index, unit="%")
        )
        rows.append(
            Measurement(ts=moment, role=f"battery_power:{BATTERY.id}", value=100.0 * index, unit="W")
        )
        moment += timedelta(minutes=5)
        index += 1
    return rows


def default_plan(start: datetime = START, end: datetime = END) -> Plan:
    return Plan(
        generated_at=start - timedelta(minutes=30),
        horizon_start=start,
        horizon_end=end,
        slot_minutes=15,
        feasible=True,
        summary=PlanSummary(),
        batteries=[{"id": BATTERY.id, "share_w": 0}],
        slots=[
            PlanSlot(
                t=start + timedelta(minutes=15 * index),
                buy=0.25,
                sell=0.05,
                pv_kw=1.0,
                load_kw=1.2,
                soc_kwh=2.75,
                power_w=0.0,
                action=Action.HOLD,
                reason="recorded",
            )
            for index in range(4)
        ],
        mode="auto",
        dry_run=False,
    )


def recording(
    tmp_path: Path,
    name: str = "recording.sqlite",
    *,
    settings_extra: dict | None = None,
    profiles: tuple = (),
    actual_rows: list[dict] | None = None,
    plans: list[Plan] | None = None,
    with_plan: bool = True,
    with_forecast: bool = True,
    dispatch_rows: int = 1,
    measurements: list[Measurement] | bool = True,
    start: datetime = START,
    end: datetime = END,
) -> Path:
    """One synthetic recording written with the real ``Store`` (schema and all)."""
    path = tmp_path / name
    store = Store(path)
    try:
        store.set_settings(settings_values(settings_extra), now=start - timedelta(hours=2))
        for kind, key, payload, updated_at in profiles:
            store.profile_put(kind, key, payload, updated_at=updated_at)
        for row in actual_rows if actual_rows is not None else default_actuals(start, end):
            store.upsert_actuals(**row)
        if measurements is True:
            store.add_measurements(default_measurements(start, end))
        elif measurements:
            store.add_measurements(measurements)
        if plans is not None:
            for plan in plans:
                store.save_plan(plan)
        elif with_plan:
            store.save_plan(default_plan(start, end))
        if with_forecast:
            store.archive_forecast(
                [("solar", "selfcheck", start, start - timedelta(hours=2), 1.0, 10.0)]
            )
        for index in range(dispatch_rows):
            store.add_dispatch(
                ts=start - timedelta(minutes=1 + index),
                battery_id=BATTERY.id,
                requested_w=0.0,
                applied_w=0.0,
                soc_pct=55.0,
                mode="auto",
                dry_run=False,
                result="ok",
                code="ok",
                message="",
            )
        store.upsert_savings_daily(
            start.date(),
            import_kwh=1.0,
            export_kwh=0.0,
            cost_eur=0.3,
            revenue_eur=0.0,
            baseline_cost_eur=0.4,
        )
        store.add_event(ts=start - timedelta(minutes=1), code="probe", message="recording")
    finally:
        store.close()
    return path


def test_open_episode_copies_the_recording_and_leaves_the_source_alone(tmp_path: Path) -> None:
    path = recording(tmp_path)
    before = path.read_bytes()
    episode = open_episode(path, START, END, 60.0)
    try:
        # A fresh writable copy, not the source: the engine may write, the recording may not.
        assert Path(episode.scratch_path) != path
        store = episode.store()
        store.set_setting("replay_probe", 1)
        assert store.get_setting("replay_probe") == 1
        assert path.read_bytes() == before
        assert sorted(item.name for item in tmp_path.iterdir()) == ["recording.sqlite"]
        source = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        try:
            count = source.execute(
                "SELECT COUNT(*) FROM settings WHERE key = 'replay_probe'"
            ).fetchone()[0]
        finally:
            source.close()
        assert count == 0
        # The recorded plan is read from the source before the copy; a plan written into the
        # scratch copy afterwards must not become part of the recording.
        store.save_plan(default_plan(START + timedelta(minutes=20), END))
        assert episode.at(END).plan().generated_at == START - timedelta(minutes=30)
    finally:
        episode.close()
    assert not Path(episode.scratch_path).exists()


def test_a_live_store_path_is_refused_even_through_a_symlink(tmp_path: Path) -> None:
    path = recording(tmp_path)
    link = tmp_path / "live-link.sqlite"
    os.symlink(path, link)
    with pytest.raises(EpisodeError) as error:
        open_episode(path, START, END, live_store_paths=[link])
    assert error.value.reason == LIVE_STORE


def test_a_hard_link_to_a_live_store_is_refused(tmp_path: Path) -> None:
    path = recording(tmp_path)
    hard = tmp_path / "live-hard.sqlite"
    os.link(path, hard)
    # A string comparison would miss this: the two names share one inode.
    with pytest.raises(EpisodeError) as error:
        open_episode(path, START, END, live_store_paths=[hard])
    assert error.value.reason == LIVE_STORE


def test_a_symlinked_source_still_sees_its_targets_wal(tmp_path: Path) -> None:
    path = recording(tmp_path)
    link = tmp_path / "alias.sqlite"
    os.symlink(path, link)
    Path(str(path) + "-wal").write_bytes(b"frames a read-only connection cannot checkpoint" * 2)
    # The WAL check must look next to the target, not next to the alias.
    with pytest.raises(EpisodeError) as error:
        open_episode(link, START, END)
    assert error.value.reason == UNFLUSHED_WAL


def test_a_non_store_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "notes.sqlite"
    path.write_text("this is not a database")
    with pytest.raises(EpisodeError) as error:
        open_episode(path, START, END)
    assert error.value.reason == NOT_SQLITE


def test_a_foreign_sqlite_store_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "foreign.sqlite"
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value_json TEXT, updated_at TEXT)")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(EpisodeError) as error:
        open_episode(path, START, END)
    assert error.value.reason == NOT_SQLITE


def test_a_source_with_an_unflushed_wal_is_refused(tmp_path: Path) -> None:
    path = recording(tmp_path)
    Path(str(path) + "-wal").write_bytes(b"frames a read-only connection cannot checkpoint" * 2)
    with pytest.raises(EpisodeError) as error:
        open_episode(path, START, END)
    assert error.value.reason == UNFLUSHED_WAL


@pytest.mark.parametrize(
    ("name", "decoy"),
    [
        ("weird?.sqlite", "weird"),
        ("weird#.sqlite", "weird"),
        ("weird%20.sqlite", "weird .sqlite"),
    ],
)
def test_a_source_name_with_uri_metacharacters_opens_the_intended_file(
    tmp_path: Path, name: str, decoy: str
) -> None:
    """A ``?``, ``#`` or percent escape in a name must stay data in the sqlite URI.

    Interpolating the path into ``file:{path}?mode=ro`` would cut the name at the metacharacter
    (``?``) or decode it as a different name (``%20``) and open the decoy file instead -- a file
    that never passed the live-store and unflushed-WAL checks.
    """
    path = recording(tmp_path, name=name, settings_extra={"mode": "advisory"})
    recording(tmp_path, name=decoy, settings_extra={"mode": "auto"})
    episode = open_episode(path, START, END)
    try:
        assert episode.settings_values["mode"] == "advisory"
    finally:
        episode.close()


def test_a_settings_row_newer_than_the_window_is_refused(tmp_path: Path) -> None:
    path = recording(tmp_path)
    store = Store(path)
    try:
        store.set_setting("mode", "advisory", now=START + timedelta(minutes=5))
    finally:
        store.close()
    with pytest.raises(EpisodeError) as error:
        open_episode(path, START, END)
    assert error.value.reason == NO_VINTAGE
    assert "mode" in str(error.value)


def test_a_decision_feeding_profile_newer_than_the_window_is_refused(tmp_path: Path) -> None:
    path = recording(
        tmp_path,
        profiles=[("control", "live_writes", {"enabled": True}, START + timedelta(minutes=5))],
    )
    with pytest.raises(EpisodeError) as error:
        open_episode(path, START, END)
    assert error.value.reason == NO_VINTAGE
    assert "control/live_writes" in str(error.value)


def test_a_profile_that_does_not_feed_decisions_may_be_newer(tmp_path: Path) -> None:
    path = recording(
        tmp_path,
        profiles=[("savings_note", "anything", {"value": 1}, START + timedelta(days=1))],
    )
    episode = open_episode(path, START, END)
    try:
        assert episode.profiles == {}
    finally:
        episode.close()


def test_the_as_of_rule_takes_the_value_recorded_before_the_window(tmp_path: Path) -> None:
    path = recording(
        tmp_path,
        settings_extra={"mode": "advisory"},
        profiles=[
            (
                capacity_learning.PROFILE_KIND,
                BATTERY.id,
                {"capacity_kwh": 4.0, "samples": 6, "updated_at": to_iso(START - timedelta(days=1))},
                START - timedelta(days=1),
            )
        ],
    )
    episode = open_episode(path, START, END)
    try:
        assert episode.settings_values["mode"] == "advisory"
        assert episode.profiles[capacity_learning.PROFILE_KIND, BATTERY.id]["capacity_kwh"] == 4.0
    finally:
        episode.close()


@pytest.mark.parametrize("damage", ["missing", "null"])
def test_incomplete_actuals_are_refused(tmp_path: Path, damage: str) -> None:
    rows = default_actuals()
    if damage == "missing":
        rows = [row for index, row in enumerate(rows) if index != 3]
    else:
        rows[3] = {**rows[3], "load_kw": None}
    path = recording(tmp_path, actual_rows=rows)
    with pytest.raises(EpisodeError) as error:
        open_episode(path, START, END)
    assert error.value.reason == INCOMPLETE_ACTUALS


def test_a_window_without_a_plan_or_forecast_is_refused(tmp_path: Path) -> None:
    path = recording(tmp_path, with_plan=False, with_forecast=False)
    with pytest.raises(EpisodeError) as error:
        open_episode(path, START, END)
    assert error.value.reason == NO_VINTAGE


def test_an_archived_forecast_alone_seeds_the_window(tmp_path: Path) -> None:
    path = recording(tmp_path, with_plan=False, with_forecast=True)
    episode = open_episode(path, START, END)
    try:
        assert episode.warm_start.plan is None
        assert episode.warm_start.last_replan_at is None
    finally:
        episode.close()


def test_a_backwards_window_is_refused(tmp_path: Path) -> None:
    path = recording(tmp_path)
    with pytest.raises(EpisodeError) as error:
        open_episode(path, END, START)
    assert error.value.reason == WINDOW


def test_the_snapshot_refuses_to_see_the_future(tmp_path: Path) -> None:
    path = recording(tmp_path)
    episode = open_episode(path, START, END, 60.0)
    try:
        snapshot = episode.at(START)
        assert snapshot.value_at("load_kw", START) == 1.2
        with pytest.raises(EpisodeError) as error:
            snapshot.value_at("load_kw", START + timedelta(minutes=5))
        assert error.value.reason == NON_ANTICIPATION
        with pytest.raises(EpisodeError):
            snapshot.range("load_kw", START, START + timedelta(minutes=10))
        # The visible series stops at the replay instant, never at the window end.
        episode.advance_to(START + timedelta(minutes=6))
        visible = episode.at(episode.now).series("load_kw")
        assert visible[-1][0] == START + timedelta(minutes=5)
        assert episode.at(episode.now).latest("load_kw")[0] == START + timedelta(minutes=5)
        with pytest.raises(EpisodeError) as backwards:
            episode.advance_to(START)
        assert backwards.value.reason == NON_ANTICIPATION
        with pytest.raises(EpisodeError) as beyond:
            episode.advance_to(END + timedelta(seconds=1))
        assert beyond.value.reason == WINDOW
    finally:
        episode.close()


def test_the_initial_battery_view_uses_the_recordings_own_samples(tmp_path: Path) -> None:
    path = recording(tmp_path)
    episode = open_episode(path, START, END)
    try:
        state = episode.initial_battery_states()[0]
        assert state.id == BATTERY.id
        assert state.available is True
        assert state.soc_pct == pytest.approx(55.0)
        assert state.power_w == pytest.approx(0.0)
        assert state.energy_kwh == pytest.approx(2.75)
        later = episode.at(START + timedelta(minutes=10)).battery_state(BATTERY)
        assert later.soc_pct == pytest.approx(57.0)
        assert later.power_w == pytest.approx(200.0)
    finally:
        episode.close()


def test_a_sample_gap_keeps_its_own_timestamp(tmp_path: Path) -> None:
    samples = [
        Measurement(ts=START, role=f"battery_soc:{BATTERY.id}", value=55.0, unit="%"),
        Measurement(ts=START, role=f"battery_power:{BATTERY.id}", value=0.0, unit="W"),
    ]
    path = recording(tmp_path, measurements=samples)
    episode = open_episode(path, START, END)
    try:
        episode.advance_to(START + timedelta(minutes=30))
        state = episode.at(episode.now).battery_state(BATTERY)
        assert state.soc_pct == pytest.approx(55.0)
        assert state.read_at == START  # never refreshed to the replay instant
        assert state.age_seconds(episode.now) == pytest.approx(1800.0)
        assert state.stale is True  # 30 minutes is older than the recording's own 5 minute cadence
    finally:
        episode.close()


def test_a_sample_before_the_window_seeds_the_first_instant(tmp_path: Path) -> None:
    samples = [
        Measurement(
            ts=START - timedelta(minutes=5), role=f"battery_soc:{BATTERY.id}", value=50.0, unit="%"
        ),
        Measurement(
            ts=START - timedelta(minutes=1), role=f"battery_soc:{BATTERY.id}", value=52.0, unit="%"
        ),
        Measurement(
            ts=START - timedelta(minutes=1),
            role=f"battery_power:{BATTERY.id}",
            value=100.0,
            unit="W",
        ),
    ]
    path = recording(tmp_path, measurements=samples)
    episode = open_episode(path, START, END)
    try:
        state = episode.initial_battery_states()[0]
        assert state.soc_pct == pytest.approx(52.0)
        assert state.power_w == pytest.approx(100.0)
        assert state.read_at == START - timedelta(minutes=1)
        assert state.stale is False
        assert state.extra == {"soc_age_s": 60.0, "power_age_s": 60.0}
    finally:
        episode.close()


def test_the_fleet_share_fallback_is_marked_not_silent(tmp_path: Path) -> None:
    path = recording(tmp_path, measurements=False)
    episode = open_episode(path, START, END)
    try:
        state = episode.initial_battery_states()[0]
        assert state.soc_pct == pytest.approx(55.0)  # 2.75 kWh fleet / 5 kWh pack
        assert state.read_at == START
        assert state.extra.get("sample_source") == "fleet_share"
        assert state.power_measured is False
    finally:
        episode.close()


def test_a_stale_power_sample_is_not_presented_as_fresh(tmp_path: Path) -> None:
    """A fresh SoC next to an old power sample is a power-telemetry outage, not a flow."""
    samples = [
        Measurement(
            ts=START - timedelta(minutes=30),
            role=f"battery_power:{BATTERY.id}",
            value=500.0,
            unit="W",
        ),
        Measurement(ts=START, role=f"battery_soc:{BATTERY.id}", value=55.0, unit="%"),
    ]
    path = recording(tmp_path, measurements=samples)
    episode = open_episode(path, START, END)
    try:
        state = episode.at(START).battery_state(BATTERY)
        assert state.soc_pct == pytest.approx(55.0)
        assert state.power_w == pytest.approx(500.0)
        # The state is only as fresh as its oldest contributor...
        assert state.read_at == START - timedelta(minutes=30)
        assert state.age_seconds(START) == pytest.approx(1800.0)
        assert state.stale is True
        # ...the old power sample is not a measurement of the current flow...
        assert state.power_measured is False
        # ...and both vintages are visible instead of hidden behind one timestamp.
        assert state.extra["power_age_s"] == pytest.approx(1800.0)
        assert state.extra["soc_age_s"] == pytest.approx(0.0)
    finally:
        episode.close()


def test_effective_configs_freeze_the_learned_capacity_and_manual_limits(tmp_path: Path) -> None:
    path = recording(
        tmp_path,
        settings_extra={
            "batteries": [{**asdict(BATTERY), "capacity_source": "learned"}],
            "manual_limits": {BATTERY.id: {"charge_w": 600.0}},
        },
        profiles=[
            (
                capacity_learning.PROFILE_KIND,
                BATTERY.id,
                {"capacity_kwh": 4.2, "samples": 6, "updated_at": to_iso(START - timedelta(days=1))},
                START - timedelta(days=1),
            )
        ],
    )
    episode = open_episode(path, START, END)
    try:
        config = episode.batteries[0]
        assert config.capacity_kwh == pytest.approx(4.2)
        assert config.max_charge_w == pytest.approx(600.0)
        assert config.max_discharge_w == pytest.approx(float(BATTERY.max_discharge_w))
    finally:
        episode.close()


def test_plans_are_ordered_by_created_at_not_by_insertion_order(tmp_path: Path) -> None:
    newest = default_plan(START, END)  # id 1, created_at START - 30 min
    older = replace(newest, generated_at=START - timedelta(minutes=50))  # id 2, older
    path = recording(tmp_path, plans=[newest, older])
    episode = open_episode(path, START, END)
    try:
        assert episode.boundary.plan_id == 2  # insertion order really does end with the older row
        assert [plan.generated_at for plan in episode.plans] == [
            START - timedelta(minutes=50),
            START - timedelta(minutes=30),
        ]
        assert episode.warm_start.plan.generated_at == START - timedelta(minutes=30)
        assert episode.at(START).plan().generated_at == START - timedelta(minutes=30)
    finally:
        episode.close()


def test_a_plan_tie_breaks_on_the_row_id(tmp_path: Path) -> None:
    stamp = START - timedelta(minutes=20)
    first = replace(default_plan(START, END), generated_at=stamp, sources={"pick": "first"})
    second = replace(default_plan(START, END), generated_at=stamp, sources={"pick": "second"})
    path = recording(tmp_path, plans=[first, second])
    episode = open_episode(path, START, END)
    try:
        assert episode.warm_start.plan.sources == {"pick": "second"}
        assert episode.at(START).plan().sources == {"pick": "second"}
    finally:
        episode.close()


def test_a_valid_json_null_is_a_recorded_value(tmp_path: Path) -> None:
    path = recording(tmp_path)
    store = Store(path)
    try:
        store.set_setting("replay_null", None, now=START - timedelta(hours=1))
    finally:
        store.close()
    episode = open_episode(path, START, END)
    try:
        assert episode.settings_values["replay_null"] is None
    finally:
        episode.close()


@pytest.mark.parametrize("table", ["settings", "learning_profiles"])
def test_malformed_decision_json_is_refused(tmp_path: Path, table: str) -> None:
    path = recording(tmp_path)
    conn = sqlite3.connect(path)
    try:
        if table == "settings":
            conn.execute("UPDATE settings SET value_json = '{oops' WHERE key = 'mode'")
        else:
            conn.execute(
                "INSERT INTO learning_profiles(kind, key, updated_at, payload_json)"
                " VALUES('control', 'live_writes', ?, '{oops')",
                (to_iso(START - timedelta(hours=1)),),
            )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(EpisodeError) as error:
        open_episode(path, START, END)
    assert error.value.reason == MALFORMED_JSON


def test_the_boundary_captures_max_ids_and_the_source_rows(tmp_path: Path) -> None:
    path = recording(tmp_path, dispatch_rows=2)
    episode = open_episode(path, START, END)
    try:
        boundary = episode.boundary
        assert (boundary.plan_id, boundary.dispatch_id, boundary.event_id) == (1, 2, 1)
        assert boundary.key("measurements", {"ts": "t", "entity_role": "r"}) == ("t", "r")
        settings_index = boundary.source_index("settings")
        assert settings_index[("mode",)]["value_json"] == '"auto"'
        assert len(boundary.source_rows["actuals"]) == 12
        assert len(boundary.source_rows["learning_profiles"]) == 0
    finally:
        episode.close()


def test_an_arrival_after_the_snapshot_cannot_change_it(tmp_path: Path) -> None:
    """The cadence that judges a sample is the one visible then, never the whole episode's."""
    samples: list[Measurement] = []
    moment = START - timedelta(minutes=30)
    while moment <= START + timedelta(minutes=10):  # a 5-minute recording cadence
        for role, value, unit in (
            (f"battery_soc:{BATTERY.id}", 55.0, "%"),
            (f"battery_power:{BATTERY.id}", 400.0, "W"),
        ):
            samples.append(Measurement(ts=moment, role=role, value=value, unit=unit))
        moment += timedelta(minutes=5)
    while moment < START + timedelta(minutes=40):  # a faster cadence AFTER the snapshot instant
        for role, value, unit in (
            (f"battery_soc:{BATTERY.id}", 55.0, "%"),
            (f"battery_power:{BATTERY.id}", 400.0, "W"),
        ):
            samples.append(Measurement(ts=moment, role=role, value=value, unit=unit))
        moment += timedelta(minutes=1)
    short_end = START + timedelta(minutes=14)
    long_end = START + timedelta(minutes=45)
    short_path = recording(tmp_path, name="short.sqlite", measurements=samples, end=short_end)
    long_path = recording(tmp_path, name="long.sqlite", measurements=samples, end=long_end)
    short = open_episode(short_path, START, short_end)
    long = open_episode(long_path, START, long_end)
    try:
        instant = short_end
        # At this instant the newest sample is 4 minutes old, which is inside the 5-minute cadence
        # the recording actually had: sweeping in the later one-minute burst must not age it.
        assert short.at(instant).battery_state(BATTERY) == long.at(instant).battery_state(BATTERY)
        state = long.at(instant).battery_state(BATTERY)
        assert state.read_at == START + timedelta(minutes=10)
        assert state.extra["soc_age_s"] == pytest.approx(240.0)
        assert state.stale is False
        assert state.power_measured is True
    finally:
        short.close()
        long.close()


def test_each_quantity_is_judged_against_its_own_cadence(tmp_path: Path) -> None:
    """A SoC inside its own cadence next to a power sample that outlived its own is an outage."""
    samples: list[Measurement] = []
    for minutes in (0, 5):  # SoC every 5 minutes, newest one at +5 min
        samples.append(
            Measurement(
                ts=START + timedelta(minutes=minutes),
                role=f"battery_soc:{BATTERY.id}",
                value=55.0,
                unit="%",
            )
        )
    for minutes in range(8):  # power every minute, newest one at +7 min
        samples.append(
            Measurement(
                ts=START + timedelta(minutes=minutes),
                role=f"battery_power:{BATTERY.id}",
                value=400.0,
                unit="W",
            )
        )
    episode = open_episode(recording(tmp_path, measurements=samples), START, END)
    try:
        state = episode.at(START + timedelta(minutes=9)).battery_state(BATTERY)
        assert state.extra["soc_age_s"] == pytest.approx(240.0)
        assert state.extra["power_age_s"] == pytest.approx(120.0)
        # The SoC is fresh by its 5-minute cadence; the power sample is not, by its 1-minute one.
        assert state.extra["stale_quantities"] == ["power"]
        assert state.stale is True
        assert state.power_measured is False
        assert state.read_at == START + timedelta(minutes=5)
    finally:
        episode.close()


def test_a_power_row_without_a_value_is_not_a_measurement(tmp_path: Path) -> None:
    """A present-but-NULL power row is an absent flow, not a fresh one."""
    path = recording(
        tmp_path,
        measurements=[
            Measurement(ts=START, role=f"battery_soc:{BATTERY.id}", value=55.0, unit="%"),
            Measurement(ts=START, role=f"battery_power:{BATTERY.id}", value=0.0, unit="W"),
        ],
    )
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO measurements (ts, entity_role, value, unit) VALUES (?, ?, NULL, ?)",
            (to_iso(START), f"battery_power:{BATTERY.id}", "W"),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # no frames for episode extraction to refuse
    finally:
        conn.close()
    episode = open_episode(path, START, END)
    try:
        state = episode.at(START).battery_state(BATTERY)
        assert state.power_w is None
        assert state.power_measured is False
    finally:
        episode.close()
