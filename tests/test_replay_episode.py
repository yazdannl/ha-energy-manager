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
from app import cost_basis as cost_basis_ledger
from app import full_charge
from app.main import EXAMPLE_BATTERY, _selfcheck_settings
from app.models import (
    Action,
    Measurement,
    Plan,
    PlanSlot,
    PlanSummary,
    from_iso,
    quarter_floor,
    to_iso,
)
from app.replay import episode as episode_mod  # noqa: E402
from app.replay.episode import (
    BASE_DERIVED,
    BASE_MEASURED,
    BASE_MIXED,
    BASE_REFUSED,
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
from app.replay.plant import SIMULATED, plant_factory
from app.sources import load as load_source
from app.sources import solar as solar_source
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


def derived_actuals(
    start: datetime = START, end: datetime = END, *, measured: bool = False
) -> list[dict]:
    """Rows whose pv/load columns are absent and whose derived pair is the only base.

    ``pv_derived_kw == max(0, load_derived_kw - grid_kw + battery_kw)`` is the identity the column is
    defined by, so this is what a real instance without pv/load sensors records; ``measured`` fills
    the measured columns instead, which is what the other fixtures do.
    """
    rows = []
    moment = quarter_floor(start, 5)
    while moment < end:
        load, grid, battery = 1.2, 0.2, 0.0
        rows.append(
            {
                "ts": to_iso(moment),
                "pv_kw": 1.0 if measured else None,
                "load_kw": load if measured else None,
                "pv_derived_kw": max(0.0, load - grid + battery),
                "load_derived_kw": load,
                "grid_kw": grid,
                "battery_kw": battery,
                "soc_kwh": 2.75,
                "price_buy": 0.25,
                "price_sell": 0.05,
            }
        )
        moment += timedelta(minutes=5)
    return rows


def test_the_derived_pair_is_the_base_and_the_window_is_labelled(tmp_path: Path) -> None:
    path = recording(tmp_path, actual_rows=derived_actuals())
    with open_episode(path, START, END, 60.0) as episode:
        assert episode.base.window(START, END) == BASE_DERIVED
        split = episode.base.split(START, END)
        assert split[BASE_DERIVED] == 12 and split[BASE_REFUSED] == 0
        reading = episode.base_at(START)
        assert reading is not None and reading.source == BASE_DERIVED
        assert reading.load_kw == pytest.approx(1.2) and reading.pv_kw == pytest.approx(1.0)
        assert reading.base_kw == pytest.approx(0.2), "the base the plant reconstructs from"
        # The effective series carries the same numbers the planner and the sampler consumed.
        assert episode.series["pv_kw"][0][1] == pytest.approx(1.0)
        assert episode.series["load_kw"][0][1] == pytest.approx(1.2)
        driver = plant_factory(episode, SIMULATED)(BATTERY)
        assert driver.grid_kw() == pytest.approx(0.2)


def test_measured_columns_are_labelled_measured(tmp_path: Path) -> None:
    path = recording(tmp_path, actual_rows=derived_actuals(measured=True))
    with open_episode(path, START, END, 60.0) as episode:
        assert episode.base.window(START, END) == BASE_MEASURED
        assert episode.base.split(START, END) == {
            BASE_MEASURED: 12,
            BASE_DERIVED: 0,
            BASE_MIXED: 0,
            BASE_REFUSED: 0,
        }
        reading = episode.base_at(START)
        assert reading is not None and reading.source == BASE_MEASURED


def test_a_window_that_mixes_the_two_sources_is_labelled_mixed(tmp_path: Path) -> None:
    rows = derived_actuals()
    rows[0]["pv_kw"] = 1.0  # only the pv column was measured in that bucket
    path = recording(tmp_path, actual_rows=rows)
    with open_episode(path, START, END, 60.0) as episode:
        assert episode.base.window(START, END) == BASE_MIXED
        split = episode.base.split(START, END)
        assert split[BASE_MIXED] == 1 and split[BASE_DERIVED] == 11


def test_a_derived_base_that_fails_the_recording_identity_is_refused(tmp_path: Path) -> None:
    rows = derived_actuals()
    rows[0]["pv_derived_kw"] = 9.9  # no longer max(0, load_derived - grid + battery)
    path = recording(tmp_path, actual_rows=rows)
    with open_episode(path, START, END, 60.0) as episode:
        # The window still opens: the identity check guards the base, not the episode.
        assert episode.base.refused == (START,)
        assert episode.base.split(START, END)[BASE_REFUSED] == 1
        assert episode.base_at(START) is None, "a half-written bucket has no base"
        assert episode.base_at(START + timedelta(minutes=5)) is not None, "and no older one is used"
        driver = plant_factory(episode, SIMULATED)(BATTERY)
        assert driver.grid_kw() is None, "a half-written row never becomes a plant's base"


def test_a_bucket_with_neither_measured_nor_derived_still_refuses(tmp_path: Path) -> None:
    rows = derived_actuals()
    rows[2]["pv_derived_kw"] = None
    rows[2]["load_derived_kw"] = None
    path = recording(tmp_path, actual_rows=rows)
    with pytest.raises(EpisodeError) as error:
        open_episode(path, START, END, 60.0)
    assert error.value.reason == INCOMPLETE_ACTUALS
    assert to_iso(START + timedelta(minutes=10)) in str(error.value)


def test_the_cut_takes_a_removed_plans_slots_with_it(tmp_path: Path) -> None:
    """A plan issued inside the window carries slots from before it; those go with the plan.

    Cutting slots by their own timestamp alone would leave rows of a plan the cut just removed, and
    because row ids are reused after a delete, the next plan saved would collide with them.
    """
    path = recording(tmp_path)
    store = Store(path)
    try:
        store.save_plan(
            Plan(
                generated_at=START + timedelta(minutes=7),
                horizon_start=START - timedelta(minutes=30),
                horizon_end=END,
                slot_minutes=15,
                summary=PlanSummary(),
                batteries=[{"id": BATTERY.id, "share_w": 0}],
                slots=[
                    PlanSlot(
                        t=START - timedelta(minutes=30) + timedelta(minutes=15 * index),
                        buy=0.25,
                        sell=0.05,
                        pv_kw=1.0,
                        load_kw=1.2,
                        soc_kwh=2.75,
                        power_w=0.0,
                        action=Action.HOLD,
                        reason="later",
                    )
                    for index in range(6)
                ],
                mode="auto",
                dry_run=False,
            )
        )
    finally:
        store.close()
    with open_episode(path, START, END, 60.0) as episode:
        connection = episode.store().connection
        orphaned = connection.execute(
            "SELECT COUNT(*) FROM plan_slots WHERE plan_id NOT IN (SELECT id FROM plans)"
        ).fetchone()[0]
        after = connection.execute(
            "SELECT COUNT(*) FROM plan_slots WHERE t >= ?", (to_iso(START),)
        ).fetchone()[0]
        plans = connection.execute("SELECT COUNT(*) FROM plans WHERE created_at >= ?", (to_iso(START),)).fetchone()[0]
    assert orphaned == 0, "every surviving slot belongs to a surviving plan"
    assert after == 0, "no slot of the removed plan reaches past the boundary either"
    assert plans == 0


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


def test_the_scratch_copy_is_cut_at_the_window_start(tmp_path: Path) -> None:
    path = recording(tmp_path)
    store = Store(path)
    try:
        store.add_dispatch(
            ts=START + timedelta(minutes=5),
            battery_id=BATTERY.id,
            requested_w=250.0,
            applied_w=250.0,
            dry_run=False,
            result="ok",
            code="ok",
        )
        store.add_event(ts=START + timedelta(minutes=5), code="later", message="after")
        store.upsert_savings_daily((START + timedelta(days=1)).date(), import_kwh=2.0)
        store.archive_forecast(
            [
                (
                    "solar",
                    "selfcheck",
                    START + timedelta(hours=2),
                    START + timedelta(minutes=5),
                    2.0,
                    10.0,
                )
            ]
        )
        store.save_plan(default_plan(START + timedelta(minutes=30), END))
    finally:
        store.close()

    with open_episode(path, START, END, 60.0) as episode:
        store = episode.store()
        # The recording's own in-window rows are still visible to the episode ...
        assert episode.series["grid_kw"] and episode.boundary.source_rows["actuals"]
        assert episode.plans and episode.dispatch
        # ... but the store the engine writes into holds nothing from the window: production's
        # store never holds a row newer than "now", so no reader needs an upper bound.
        assert store.actuals_range(START - timedelta(hours=1), END) == []
        assert [row["ts"] for row in store.dispatch_log(limit=50)] == [
            to_iso(START - timedelta(minutes=1))
        ], "the setpoint in force is kept; every later recorded decision is gone"
        assert [row["code"] for row in store.events(limit=50)] == ["probe"]
        assert store.savings_daily_range(
            START.date().isoformat(), (START + timedelta(days=1)).date().isoformat()
        ) == []
        assert episode.truncation.cut_at == START
        for table in (
            "actuals",
            "measurements",
            "dispatch_log",
            "events",
            "savings_daily",
            "plans",
            "plan_slots",
        ):
            assert episode.truncation.rows.get(table), f"{table} had rows after the boundary"
        # A vintage issued before the boundary is as-of data and stays; a later issue does not.
        assert episode.truncation.rows["forecast_archive"] == 1
        assert store.forecast_archive_range(START, END)

    # The cut is a property of the scratch copy: the recording keeps its own future.
    source = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    try:
        assert (
            source.execute(
                "SELECT COUNT(*) FROM dispatch_log WHERE ts >= ?", (to_iso(START),)
            ).fetchone()[0]
            == 1
        )
        assert source.execute(
            "SELECT COUNT(*) FROM plan_slots WHERE t >= ?", (to_iso(START),)
        ).fetchone()[0] == 8
    finally:
        source.close()


def test_a_newer_cost_basis_row_is_dropped_so_the_ledger_can_rebuild(tmp_path: Path) -> None:
    path = recording(
        tmp_path,
        profiles=[
            (
                cost_basis_ledger.PROFILE_KIND,
                BATTERY.id,
                {"basis_eur_per_kwh": 0.21, "energy_kwh": 5.0},
                START + timedelta(minutes=39),
            )
        ],
    )
    with open_episode(path, START, END, 60.0) as episode:
        assert episode.profiles == {}, "a newer ledger row is never an as-of value"
        assert episode.truncation.dropped_profiles == ((cost_basis_ledger.PROFILE_KIND, BATTERY.id),)
        assert episode.truncation.rows.get("learning_profiles") == 1
        assert episode.store().profile_get(cost_basis_ledger.PROFILE_KIND, BATTERY.id) is None


def test_a_newer_capacity_row_is_dropped_and_the_configured_value_stands(tmp_path: Path) -> None:
    path = recording(
        tmp_path,
        profiles=[
            (
                capacity_learning.PROFILE_KIND,
                BATTERY.id,
                {"capacity_kwh": 9.5},
                START + timedelta(minutes=39),
            )
        ],
    )
    with open_episode(path, START, END, 60.0) as episode:
        assert episode.truncation.dropped_profiles == ((capacity_learning.PROFILE_KIND, BATTERY.id),)
        assert episode.batteries[0].capacity_kwh == pytest.approx(BATTERY.capacity_kwh)
        assert episode.store().profile_get(capacity_learning.PROFILE_KIND, BATTERY.id) is None


def test_a_newer_control_row_still_refuses_the_window(tmp_path: Path) -> None:
    # No rebuild path exists for a control row inside a replay, so dropping it would silently change
    # what the engine does: the window is refused instead.
    path = recording(
        tmp_path,
        profiles=[("control", "live_writes", {"enabled": True}, START + timedelta(minutes=5))],
    )
    with pytest.raises(EpisodeError) as error:
        open_episode(path, START, END, 60.0)
    assert error.value.reason == NO_VINTAGE
    assert "no rebuild path" in str(error.value)


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


def test_a_settings_key_written_only_after_the_window_is_refused(tmp_path: Path) -> None:
    """A key the recording knows only from after ``start`` still refuses, and names itself."""
    path = recording(tmp_path)
    store = Store(path)
    try:
        store.set_setting("late.setting", "advisory", now=START + timedelta(minutes=5))
    finally:
        store.close()
    with pytest.raises(EpisodeError) as error:
        open_episode(path, START, END)
    assert error.value.reason == NO_VINTAGE
    assert "late.setting" in str(error.value)


def test_a_setting_rewritten_after_the_window_start_uses_the_as_of_value(tmp_path: Path) -> None:
    """The running add-on re-stamps mode/dry_run; the window still opens on the old value."""
    path = recording(tmp_path, settings_extra={"mode": "auto"})
    store = Store(path)
    try:
        store.set_setting("mode", "advisory", now=START + timedelta(minutes=5))
    finally:
        store.close()
    with open_episode(path, START, END, 60.0) as episode:
        assert episode.settings_values["mode"] == "auto"
        assert episode.asof_sources["settings:mode"] == "history"
        # The engine reads settings through the frozen view, but the scratch copy must agree: the
        # as-of row, not the recording's end state, is what every reader of the copy sees.
        assert episode.store().get_setting("mode") == "auto"
        row = episode.store().connection.execute(
            "SELECT updated_at FROM settings WHERE key = 'mode'"
        ).fetchone()
        assert from_iso(row["updated_at"]) <= START


def test_a_backdated_write_cannot_displace_a_later_one_in_the_frozen_settings(tmp_path):
    """R1: selection is by instant, not by insertion id -- a write may be backdated."""
    path = recording(tmp_path, settings_extra={"mode": "auto"})
    store = Store(path)
    try:
        store.set_setting("mode", "advisory", now=START - timedelta(hours=1))
        store.set_setting("mode", "sneaky", now=START - timedelta(hours=3))  # backdated, newest id
    finally:
        store.close()
    with open_episode(path, START, END, 60.0) as episode:
        assert episode.settings_values["mode"] == "advisory"
        assert episode.store().get_setting("mode") == "advisory"


def test_a_history_offset_cannot_smuggle_a_later_value_into_the_window(tmp_path):
    """R2: ``START + 30 min`` spelled ``08:30-02:00`` is textually before ``10:00+00:00``."""
    path = recording(tmp_path, settings_extra={"mode": "auto"})
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO settings_history(key, at, value_json, origin)"
            " VALUES('mode', '2026-01-01T08:30:00-02:00', '\"advisory\"', 'write')"
        )
        conn.commit()
    finally:
        conn.close()
    with open_episode(path, START, END, 60.0) as episode:
        assert episode.settings_values["mode"] == "auto"
        assert episode.store().get_setting("mode") == "auto"


def test_an_unreadable_history_timestamp_refuses_the_window(tmp_path):
    """R2(b): an instant that cannot be read refuses rather than being trusted."""
    path = recording(tmp_path, settings_extra={"mode": "auto"})
    conn = sqlite3.connect(path)
    try:
        conn.execute("UPDATE settings_history SET at = 'sometime' WHERE key = 'mode'")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(EpisodeError) as error:
        open_episode(path, START, END, 60.0)
    assert error.value.reason == MALFORMED_JSON
    assert "mode" in str(error.value)


def test_a_legacy_recording_keeps_a_frozen_value_at_exactly_the_window_start(tmp_path):
    """R3: the pre-v5 path must not delete a row the freeze accepted at exactly ``start``."""
    path = recording(tmp_path)
    store = Store(path)
    try:
        store.set_setting("mode", "start", now=START)
        store.profile_put("control", "live_writes", {"batteries": []}, updated_at=START)
    finally:
        store.close()
    conn = sqlite3.connect(path)
    try:
        conn.execute("DROP TABLE settings_history")
        conn.execute("DROP TABLE learning_profiles_history")
        conn.execute("UPDATE schema_version SET version = 4")
        conn.commit()
    finally:
        conn.close()
    with open_episode(path, START, END, 60.0) as episode:
        assert episode.settings_values["mode"] == "start"
        assert episode.profiles[("control", "live_writes")] == {"batteries": []}
        # The scratch keeps it: the engine reads through the store, not through the freeze.
        assert episode.store().get_setting("mode") == "start"
        assert episode.store().profile_get("control", "live_writes") == (START, {"batteries": []})


def test_a_legacy_recording_does_not_show_the_engine_a_post_start_profile(tmp_path):
    """R3: without a history table the strict cut must still run, or the scratch leaks the future.

    The fetch backoff is not a decision-feeding kind, so the freeze neither refuses it nor declares
    it dropped: only the boundary cut can keep a post-start backoff out of the engine's own reads.
    """
    path = recording(tmp_path)
    store = Store(path)
    try:
        store.profile_put(
            solar_source.BACKOFF_KIND,
            solar_source.BACKOFF_KEY,
            {"failures": 9},
            updated_at=START + timedelta(minutes=5),
        )
    finally:
        store.close()
    conn = sqlite3.connect(path)
    try:
        conn.execute("DROP TABLE settings_history")
        conn.execute("DROP TABLE learning_profiles_history")
        conn.execute("UPDATE schema_version SET version = 4")
        conn.commit()
    finally:
        conn.close()
    with open_episode(path, START, END, 60.0) as episode:
        assert episode.store().profile_get(
            solar_source.BACKOFF_KIND, solar_source.BACKOFF_KEY
        ) is None


def test_the_cut_deletes_more_rows_than_a_connection_can_bind(tmp_path, monkeypatch):
    """M3: the cut must not spell one placeholder per doomed row.

    A long history can hold far more future rows than a connection has bind variables, and the
    window this cut exists for is exactly the long one.  The limit is lowered here, and the batch
    with it, so the failure the batching prevents is reachable in a small test.
    """
    conn = sqlite3.connect(tmp_path / "many.db")
    try:
        if not hasattr(conn, "setlimit"):
            pytest.skip("this sqlite3 binding cannot lower the variable limit")
        conn.execute("CREATE TABLE doomed (at TEXT)")
        conn.executemany("INSERT INTO doomed(at) VALUES(?)", [("x",)] * 12)
        conn.commit()
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 3)
        monkeypatch.setattr(episode_mod, "_DELETE_BATCH", 2)
        rowids = [int(row[0]) for row in conn.execute("SELECT rowid FROM doomed")]
        assert len(rowids) > 3
        episode_mod._delete_rowids(conn, "doomed", rowids)
        assert conn.execute("SELECT COUNT(*) FROM doomed").fetchone()[0] == 0
        # The batch in production stays under the smallest variable limit any build may have.
        monkeypatch.undo()
        assert episode_mod._DELETE_BATCH < 999
    finally:
        conn.close()


def test_a_history_with_thousands_of_future_rows_is_cut(tmp_path):
    """M3: a long append-only history prunes without error, and only its future rows go."""
    path = recording(tmp_path)
    conn = sqlite3.connect(path)
    try:
        before = conn.execute("SELECT COUNT(*) FROM settings_history").fetchone()[0]
        conn.executemany(
            "INSERT INTO settings_history(key, at, value_json, origin) VALUES('mode', ?, '1', 'write')",
            [(to_iso(START + timedelta(seconds=30 + index)),) for index in range(4000)],
        )
        conn.commit()
    finally:
        conn.close()
    with open_episode(path, START, END, 60.0) as episode:
        assert episode.truncation.rows.get("settings_history") == 4000
        store = episode.store()
        assert store.settings_as_of(START)["mode"].value == "auto"
        assert store.connection.execute(
            "SELECT COUNT(*) FROM settings_history WHERE at > ?", (to_iso(START),)
        ).fetchone()[0] == 0
        assert store.connection.execute(
            "SELECT COUNT(*) FROM settings_history"
        ).fetchone()[0] == before


def test_a_legacy_row_with_extra_zero_digits_survives_the_cut(tmp_path):
    """R2: a fraction written to ten digits with zeros past the microsecond is still a real row.

    Counting digits refuses it, the cut then deletes it as unplaceable, and the engine loses a row
    the recording really had -- data loss rather than a safe refusal.
    """
    path = recording(tmp_path)
    store = Store(path)
    try:
        store.profile_put(
            solar_source.BACKOFF_KIND,
            solar_source.BACKOFF_KEY,
            {"failures": 4},
            updated_at=datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc),
        )
    finally:
        store.close()
    conn = sqlite3.connect(path)
    try:
        spelled = "2026-01-01T09:00:00.5" + "0" * 6 + "+00:00"
        conn.execute(
            "UPDATE learning_profiles SET updated_at = ? WHERE kind = ? AND key = ?",
            (spelled, solar_source.BACKOFF_KIND, solar_source.BACKOFF_KEY),
        )
        conn.execute("DROP TABLE settings_history")
        conn.execute("DROP TABLE learning_profiles_history")
        conn.execute("UPDATE schema_version SET version = 4")
        conn.commit()
    finally:
        conn.close()
    with open_episode(path, START, END, 60.0) as episode:
        got = episode.store().profile_get(solar_source.BACKOFF_KIND, solar_source.BACKOFF_KEY)
        assert got is not None and got[1] == {"failures": 4}


def test_a_legacy_offset_profile_after_the_window_is_cut_not_shown(tmp_path):
    """R1: ``08:30-02:00`` is 10:30Z -- after a 10:00Z start -- however it sorts as text.

    The text comparison kept the row, so the engine saw a post-start backoff the freeze had not
    frozen (and, being a non-decision kind, never replaced).  The cut must compare instants.
    """
    path = recording(tmp_path)
    store = Store(path)
    try:
        store.profile_put(
            solar_source.BACKOFF_KIND,
            solar_source.BACKOFF_KEY,
            {"failures": 9},
            updated_at=datetime(2026, 1, 1, 8, 30, tzinfo=timezone(timedelta(hours=-2))),
        )
    finally:
        store.close()
    conn = sqlite3.connect(path)
    try:
        bumped = conn.execute(
            "SELECT updated_at FROM learning_profiles"
        ).fetchone()[0]
        assert bumped == "2026-01-01T08:30:00-02:00", "the row really is spelled with an offset"
        conn.execute("DROP TABLE settings_history")
        conn.execute("DROP TABLE learning_profiles_history")
        conn.execute("UPDATE schema_version SET version = 4")
        conn.commit()
    finally:
        conn.close()
    with open_episode(path, START, END, 60.0) as episode:
        assert episode.store().profile_get(
            solar_source.BACKOFF_KIND, solar_source.BACKOFF_KEY
        ) is None


def test_a_control_profile_rewritten_after_the_window_start_is_materialised_as_of(
    tmp_path: Path,
) -> None:
    """The engine reads ``control/live_writes`` from the scratch store, so the as-of row must live
    there: merely returning it from a reader would leave the engine reading the future."""
    path = recording(tmp_path)
    store = Store(path)
    try:
        store.profile_put(
            "control",
            "live_writes",
            {"batteries": [{"battery_id": BATTERY.id, "power_w": 100.0}]},
            updated_at=START - timedelta(hours=1),
        )
        store.profile_put(
            "control",
            "live_writes",
            {"batteries": [{"battery_id": BATTERY.id, "power_w": 900.0}]},
            updated_at=START + timedelta(minutes=5),
        )
    finally:
        store.close()
    with open_episode(path, START, END, 60.0) as episode:
        assert episode.profiles[("control", "live_writes")]["batteries"][0]["power_w"] == 100.0
        assert episode.asof_sources["profiles:control/live_writes"] == "history"
        stored = episode.store().profile_get("control", "live_writes")
        assert stored is not None
        at, payload = stored
        assert at <= START
        assert payload["batteries"][0]["power_w"] == 100.0, (
            "the engine's profile_get must return the frozen value, not the newer write"
        )
        newer = episode.store().connection.execute(
            "SELECT COUNT(*) FROM learning_profiles WHERE updated_at > ?", (to_iso(START),)
        ).fetchone()[0]
        assert newer == 0


def test_the_scratch_carries_every_frozen_value_and_nothing_newer(tmp_path: Path) -> None:
    """The invariant behind the materialisation, for every non-rebuildable decision kind."""
    path = recording(tmp_path)
    store = Store(path)
    try:
        store.set_setting("mode", "advisory", now=START + timedelta(minutes=5))
        for index, kind in enumerate(
            ("control", full_charge.STATE_KIND, load_source.PROFILE_KIND, solar_source.BIAS_KIND)
        ):
            store.profile_put(kind, f"key{index}", {"value": index}, updated_at=START - timedelta(hours=1))
            store.profile_put(
                kind, f"key{index}", {"value": 100 + index}, updated_at=START + timedelta(minutes=5)
            )
    finally:
        store.close()
    with open_episode(path, START, END, 60.0) as episode:
        connection = episode.store().connection
        for table, column in (("settings", "updated_at"), ("learning_profiles", "updated_at")):
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} > ?", (to_iso(START),)
            ).fetchone()[0] == 0
        assert episode.store().get_setting("mode") == "auto"
        for (kind, key), payload in episode.profiles.items():
            stored = episode.store().profile_get(kind, key)
            assert stored is not None, f"{kind}/{key} is frozen but missing from the scratch"
            at, stored_payload = stored
            assert at <= START
            assert stored_payload == payload


def test_a_profile_row_at_exactly_the_window_start_survives_the_strict_cut(tmp_path: Path) -> None:
    path = recording(tmp_path)
    store = Store(path)
    try:
        store.profile_put("control", "live_writes", {"batteries": []}, updated_at=START)
    finally:
        store.close()
    with open_episode(path, START, END, 60.0) as episode:
        stored = episode.store().profile_get("control", "live_writes")
        assert stored is not None and stored[0] == START
        assert episode.profiles[("control", "live_writes")] == {"batteries": []}


def test_a_tombstone_before_the_window_leaves_the_key_absent_without_refusing(
    tmp_path: Path,
) -> None:
    path = recording(tmp_path)
    store = Store(path)
    try:
        store.profile_put(
            "control", "live_writes", {"batteries": []}, updated_at=START - timedelta(hours=1)
        )
        store.profile_delete("control", "live_writes", now=START - timedelta(minutes=30))
    finally:
        store.close()
    with open_episode(path, START, END, 60.0) as episode:
        assert ("control", "live_writes") not in episode.profiles
        assert episode.store().profile_get("control", "live_writes") is None


def test_a_setting_written_and_deleted_after_the_window_still_refuses(tmp_path: Path) -> None:
    """A tombstone is a change too: a window starting before both must refuse that key."""
    path = recording(tmp_path)
    store = Store(path)
    try:
        store.set_setting("late.setting", "v", now=START + timedelta(minutes=3))
        store.delete_setting("late.setting", now=START + timedelta(minutes=5))
    finally:
        store.close()
    with pytest.raises(EpisodeError) as error:
        open_episode(path, START, END, 60.0)
    assert error.value.reason == NO_VINTAGE
    assert "late.setting" in str(error.value)


def test_an_upgraded_recording_freezes_seeded_rows_and_says_so(tmp_path: Path) -> None:
    """A pre-v5 recording upgraded by opening it: migration 5 seeds the history."""
    path = recording(tmp_path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("DROP TABLE settings_history")
        conn.execute("DROP TABLE learning_profiles_history")
        conn.execute("UPDATE schema_version SET version = 4")
        conn.commit()
    finally:
        conn.close()
    upgraded = Store(path)
    upgraded.connection  # open once: migration 5 seeds the history
    upgraded.close()
    with open_episode(path, START, END, 60.0) as episode:
        assert episode.settings_values["mode"] == "auto"
        assert episode.asof_sources["settings:mode"] == "seed"
        assert episode.store().get_setting("mode") == "auto"


def test_a_recording_without_history_falls_back_to_the_current_row(tmp_path: Path) -> None:
    """A source that never saw v5 keeps today's behaviour: the current row is the as-of row."""
    path = recording(tmp_path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("DROP TABLE settings_history")
        conn.execute("DROP TABLE learning_profiles_history")
        conn.execute("UPDATE schema_version SET version = 4")
        conn.commit()
    finally:
        conn.close()
    with open_episode(path, START, END, 60.0) as episode:
        assert episode.settings_values["mode"] == "auto"
        assert episode.asof_sources["settings:mode"] == "current"


def test_a_recording_without_history_still_refuses_a_newer_settings_row(tmp_path: Path) -> None:
    path = recording(tmp_path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("DROP TABLE settings_history")
        conn.execute("DROP TABLE learning_profiles_history")
        conn.execute("UPDATE schema_version SET version = 4")
        conn.execute(
            "UPDATE settings SET updated_at = ? WHERE key = 'mode'",
            (to_iso(START + timedelta(minutes=5)),),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(EpisodeError) as error:
        open_episode(path, START, END, 60.0)
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
                {
                    "capacity_kwh": 4.0,
                    "samples": 6,
                    "schema": capacity_learning.SCHEMA,
                    "updated_at": to_iso(START - timedelta(days=1)),
                },
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
                {
                    "capacity_kwh": 4.2,
                    "samples": 6,
                    "schema": capacity_learning.SCHEMA,
                    "updated_at": to_iso(START - timedelta(days=1)),
                },
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
            conn.execute("UPDATE settings_history SET value_json = '{oops' WHERE key = 'mode'")
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


def test_a_windows_label_is_the_bucket_in_force_at_its_start(tmp_path: Path) -> None:
    """The window label names the base its ticks used, not an average over the range.

    A bucket's base is held until the next bucket begins, so a window starting inside a bucket would
    otherwise read as ``absent`` while every one of its ticks was running on that bucket's value;
    ``split`` is where the per-bucket counts live (documented at ``Episode.base.window``).
    """
    path = recording(tmp_path, actual_rows=derived_actuals())
    with open_episode(path, START, END, 60.0) as episode:
        inside = START + timedelta(minutes=6)
        assert episode.base.split(inside, inside + timedelta(minutes=3))[BASE_DERIVED] == 0
        assert episode.base.window(inside, inside + timedelta(minutes=3)) == BASE_DERIVED
        assert episode.base.window(inside, inside + timedelta(minutes=3)) == episode.base.at(inside)
