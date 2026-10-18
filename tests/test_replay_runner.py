"""The replay runner: the virtual clock, the episode-backed sources and the cadence dispatch.

The runner is the only place that advances the replay instant, so these tests pin *when* each
production step runs, that nothing else samples the clock, that the warm start is the recording's
own state, and that a leg's record is reproducible.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "energy_manager"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.engine import MIN_REPLAN_GAP_S, Engine  # noqa: E402
from app.models import PriceForecast, PriceSlot, to_iso  # noqa: E402
from app.optimize import terminal as tv  # noqa: E402
from app.replay.episode import open_episode  # noqa: E402
from app.replay.plant import RECORDED, SIMULATED  # noqa: E402
from app.sources import price as price_source  # noqa: E402
from app.replay.runner import (  # noqa: E402
    FACADE_COMMAND,
    MEASUREMENT_WRITE_FAILED,
    NO_DISPATCH,
    NO_VINTAGE,
    RECORDED_POLICY_UNKNOWN,
    RecordedPolicy,
    Runner,
    RunnerError,
)
from test_replay_manifest import BATTERY, START, recording  # noqa: E402
from test_terminal_value import (  # noqa: E402
    agg_for,
    make_slots,
    options,
    plan_for,
    request,
)

WINDOW_MINUTES = 20
WINDOW_S = WINDOW_MINUTES * 60


def a_window(tmp_path: Path, *, step_s: float = 1.0, minutes: int = WINDOW_MINUTES, **kwargs):
    source = recording(tmp_path, **kwargs)
    return open_episode(source, START, START + timedelta(minutes=minutes), step_s)


def trace(runner: Runner) -> dict[str, list[datetime]]:
    """Record the instant every step was given, without changing what it does."""
    seen: dict[str, list[datetime]] = {"control": [], "replan": [], "guard": []}
    for attribute, key in (
        ("_control_step", "control"),
        ("_replan_step", "replan"),
        ("_guard_step", "guard"),
    ):
        real = getattr(runner.engine, attribute)

        def wrapper(now=None, _real=real, _key=key):
            seen[_key].append(now)
            return _real(now)

        setattr(runner.engine, attribute, wrapper)
    return seen


# --------------------------------------------------------------------------- cadence and bounds


def test_the_runner_drives_the_production_cadence(tmp_path: Path) -> None:
    with a_window(tmp_path) as episode:
        end = episode.end
        runner = Runner(episode, name="E1.replay", plant=RECORDED)
        seen = trace(runner)
        result = runner.run()

    assert result.refused is False
    assert result.steps == WINDOW_S + 1, "one step per virtual second, including both ends"
    assert len(seen["control"]) == WINDOW_S // 30 + 1, "control.tick_seconds = 30"
    assert len(seen["guard"]) == WINDOW_S // 2 + 1, "the guard loop waits 2.0 s"
    assert len(seen["replan"]) == WINDOW_S + 1, "the replan loop waits 1.0 s"
    assert (result.control_steps, result.guard_steps, result.replan_steps) == (
        len(seen["control"]),
        len(seen["guard"]),
        len(seen["replan"]),
    )
    for key, moments in seen.items():
        assert moments[0] == START, key
        assert all(moment is not None for moment in moments), key
        assert all(START <= moment <= end for moment in moments), key
    assert seen["control"][-1] == end
    assert seen["guard"][1] == START + timedelta(seconds=2)
    assert {call.kind for call in result.vintage_calls} == {"price", "solar", "load"}, (
        "a replan asks for all three vintages"
    )


def test_the_run_ends_exactly_at_the_window_and_never_past_it(tmp_path: Path) -> None:
    with a_window(tmp_path) as episode:
        end = episode.end
        result = Runner(episode, name="E1.replay", plant=RECORDED).run()
        assert episode.now == end, "the replay instant stops at the boundary"
    assert result.steps == WINDOW_S + 1
    assert result.windows[0].start == START
    assert result.windows[-1].end == end
    # 55 % of a 5 kWh pack, held by the recording: the terminal energy is read at the boundary.
    assert result.initial_energy_kwh() == pytest.approx(2.75)
    assert result.terminal_energy_kwh() == pytest.approx(2.75)


def test_a_coarser_step_still_covers_the_whole_window(tmp_path: Path) -> None:
    with a_window(tmp_path, step_s=7.0) as episode:
        end = episode.end
        runner = Runner(episode, name="E1.replay", plant=RECORDED)
        seen = trace(runner)
        result = runner.run()
    assert result.steps == 1200 // 7 + 2, "the final instant is clamped to the boundary"
    assert seen["guard"][-1] == end
    # A step coarser than the production cadence can only coarsen the cadence, never skip the
    # boundary: the last control tick due inside the window is the one at 1190 s.
    assert seen["control"][-1] == START + timedelta(seconds=1190)
    assert episode.now == end


def test_no_threads_are_started_and_start_is_never_called(tmp_path: Path, monkeypatch) -> None:
    def forbidden(self):  # pragma: no cover - it must never run
        raise AssertionError("the replay must never call Engine.start()")

    monkeypatch.setattr(Engine, "start", forbidden)
    with a_window(tmp_path, minutes=2) as episode:
        runner = Runner(episode, name="E1.replay", plant=RECORDED)
        assert runner.engine._threads == []
        assert runner.engine._clock is runner.clock
        assert runner.clock() == episode.start
        result = runner.run()
    assert result.steps == 120 + 1


def test_the_warm_start_is_the_recordings_own_state(tmp_path: Path) -> None:
    with a_window(tmp_path) as episode:
        runner = Runner(episode, name="E1.replay", plant=RECORDED)
        engine = runner.engine
        warm = episode.warm_start
    assert engine._plan is warm.plan
    assert engine._state.active_plan is warm.plan
    assert engine._last_replan_at == warm.last_replan_at == warm.plan.generated_at
    assert engine._replan_backoff_s == warm.backoff_s == MIN_REPLAN_GAP_S
    assert engine._last_sample_at == episode.start
    assert engine._battery_read_ok is True
    assert [state.energy_kwh for state in engine._state.batteries] == [pytest.approx(2.75)]
    assert engine._state.last_plan_at == warm.plan.generated_at


# --------------------------------------------------------------------------- the source closures


def test_the_sampler_serves_the_recordings_own_flow(tmp_path: Path) -> None:
    with a_window(tmp_path) as episode:
        runner = Runner(episode, name="E1.replay", plant=RECORDED)
        values = runner.sampler(episode.start)
    assert values["pv_w"] == pytest.approx(1000.0)
    assert values["load_w"] == pytest.approx(1200.0)
    assert values["grid_w"] == pytest.approx(200.0), "the recording's own grid measurement"


def test_the_simulated_sampler_rebuilds_the_arms_own_site_flow(tmp_path: Path) -> None:
    with a_window(tmp_path) as episode:
        runner = Runner(episode, name="E4.policy", plant=SIMULATED)
        assert "grid_w" not in runner.sampler(episode.start), (
            "before the first control tick there is no pack flow to add, so the site flow is "
            "unknown rather than the bare base"
        )
        runner.step(episode.start)
        values = runner.sampler(episode.start)
        expected = (1.2 - 1.0) * 1000.0 + sum(
            float(driver.realized_power() or 0.0) for driver in runner.drivers
        )
        assert values["grid_w"] == pytest.approx(expected)


def test_the_run_writes_the_arms_own_flows_into_the_scratch_store(tmp_path: Path) -> None:
    with a_window(tmp_path) as episode:
        result = Runner(episode, name="E1.replay", plant=RECORDED).run()
        assert result.store is episode.store()
        rows = result.store.actuals_range(START, episode.end)
    assert rows, "the control ticks must book the arm's flows"
    assert all(row["grid_kw"] == pytest.approx(0.2) for row in rows)
    assert all(row["pv_kw"] == pytest.approx(1.0) for row in rows)
    assert all(row["load_kw"] == pytest.approx(1.2) for row in rows)
    assert all(row["price_buy"] == pytest.approx(0.25) for row in rows)


def test_a_replay_vintage_carries_no_inferred_provenance(tmp_path: Path) -> None:
    """R6: provenance is a fact whoever resolved the tariff records, never a model-fit inference.

    Matching recorded prices cannot establish a contract (0.16/0.22 buys with 0.14/0.20 sells *are*
    ``buy - fee`` and are equally two explicit rates), so the runner stamps **nothing** -- the vintage
    stays unprovenanced, exactly like any other caller-built forecast, and takes the engine's two-step
    path: one supported model must fit the window, and the tail is priced under both readings.  No arm
    name, settings patch or recorded price shape can promote that fit into a declaration.
    """
    for extra in (
        {},
        {"price.export_fee_eur_per_kwh": 0.20},  # would make the recorded sells derived-shaped
        {"price.export_fee_eur_per_kwh": 0.10},  # would leave them one flat rate
        {"price.feed_in_eur_per_kwh": 0.09},     # an explicit contract in the settings
        {"price.feed_in_entity": "sensor.example_feed_in"},
    ):
        with a_window(tmp_path, settings_extra=extra) as episode:
            vintage = Runner(episode, name="E3.vector", plant=RECORDED).price_source(episode.start)
        assert vintage is not None, extra
        assert getattr(vintage, "feed_in_source", None) is None, extra
        assert [slot.sell for slot in vintage.slots] == [0.05] * len(vintage.slots), extra
        assert all(slot.buy == pytest.approx(0.25) for slot in vintage.slots), extra


def test_a_replay_vintage_with_published_tail_entries_refuses_on_a_disagreement() -> None:
    """R6: the case the deleted stamp would have skipped -- both readings, then refuse.

    The reviewer's coincidence: buys 0.16/0.22 with sells 0.14/0.20 satisfy ``buy - 0.02`` exactly and
    are equally an entity publishing those two rates.  A published 0.30 tail buy must therefore not be
    turned into 0.28 by a derived-only fit; it is compared against the clock-time reference (0.14) and
    the vector refuses.  E3 itself is unaffected: its tail is entirely window-filled, so the two
    readings coincide and the arm still proceeds.
    """
    settings = {"price.export_fee_eur_per_kwh": 0.02}
    window = make_slots(2, 60, buy=[0.16, 0.22], sell=[0.14, 0.20])
    start = window[-1].t + timedelta(minutes=60)
    vintage = PriceForecast(
        source="replay",
        slots=[PriceSlot(t=slot.t, buy=slot.buy, sell=slot.sell, source="replay") for slot in window],
        # Published tail entries gained after the recording; hour 0 of the tail is its 23rd slot.
        tail_slots=[PriceSlot(t=start + timedelta(hours=22), buy=0.30, sell=0.0, source="replay")],
    )
    assert getattr(vintage, "feed_in_source", None) is None
    pricing = price_source.tail_sell_pricing(vintage, settings)
    assert pricing.ambiguous is True and pricing.unsupported is False
    assert pricing.evidence["derived_fits"] is True and pricing.evidence["flat_fits"] is False
    verdict = plan_for(
        window,
        agg=agg_for(),
        opts=options(terminal_mode="vector", terminal_tail_hours=24.0),
        tail_price=vintage.tail_slots,
        req=request(),
        start=start,
        sell_pricing=pricing,
    )
    assert verdict.reason == tv.REASON_TAIL_PRICING_AMBIGUOUS
    conflict = verdict.detail["tail_pricing_ambiguous"]
    assert conflict["derived_eur_per_kwh"] == pytest.approx(0.28)
    assert conflict["window_rate_eur_per_kwh"] == pytest.approx(0.14)
    assert "no feed-in provenance" in verdict.detail["sell_provenance"]


def test_a_missing_vintage_refuses_the_leg(tmp_path: Path) -> None:
    with a_window(tmp_path) as episode:
        runner = Runner(episode, name="E1.replay", plant=RECORDED)
        episode.plans = ()  # no recorded plan is in force at any instant
        assert runner.price_source(episode.start) is None
        assert runner.load_source(episode.start) is None
        assert runner.refusals[0].reason == NO_VINTAGE
        with pytest.raises(RunnerError) as caught:
            runner.step(episode.start)
        assert caught.value.reason == NO_VINTAGE
        assert runner.result().refused is True


def test_the_vintage_is_the_as_of_plan_and_names_its_tail_extension(tmp_path: Path) -> None:
    with a_window(tmp_path) as episode:
        runner = Runner(episode, name="E1.replay", plant=RECORDED)
        price = runner.price_source(episode.start)
        solar = runner.solar_source(episode.start)
        load = runner.load_source(episode.start)
        calls = runner.vintage_calls
    assert price is not None and price.slots
    plan = episode.warm_start.plan
    assert price.slots[0].t == START  # the recorded plan's own grid
    assert len(price.slots) == len(plan.slots), "the vintage passes through verbatim"
    assert price.slots[-1].t == plan.slots[-1].t, "no slot is invented beyond the vintage"
    assert all(slot.buy == pytest.approx(0.25) and slot.sell == pytest.approx(0.05) for slot in price.slots)
    assert solar is not None and solar.slots and load is not None and load.slots
    # The forecast is labelled with the vintage's own issue time, not with the replay instant.
    assert price.generated_at == episode.warm_start.plan.generated_at
    assert [call.kind for call in calls] == ["price", "solar", "load"]
    assert calls[0].sources == ("plan_vintage",)
    assert calls[0].tail_uncovered_slots > 0, (
        "the solver horizon reaches past the vintage, and the tail is left for align_slots to fill "
        "and mark stale"
    )
    assert calls[0].horizon_end - episode.start == timedelta(hours=30)


# ------------------------------------------------------------------------------- refusals / seams


def test_the_facade_publishes_but_never_commands(tmp_path: Path) -> None:
    with a_window(tmp_path, minutes=2) as episode:
        runner = Runner(episode, name="E1.replay", plant=RECORDED)
        runner.step(episode.start)
        published = runner.ha.summary()
        assert published["publishes"] > 0, "publish_states is production behaviour and is captured"
        assert published["commands"] == 0, "commands belong to the plant, not to the facade"
        runner.ha.call_service("persistent_notification", "create", {"message": "x"})
        with pytest.raises(RunnerError) as caught:
            runner.step(episode.start + timedelta(seconds=1))
    assert caught.value.reason == FACADE_COMMAND
    assert runner.result().refused is True


def with_dispatch(tmp_path: Path, *, other_battery: bool = False) -> Path:
    """A fixture recording that also carries the recording's own dispatch history."""
    source = recording(tmp_path)
    rows = [
        (START - timedelta(minutes=5), BATTERY.id, 300.0, 300.0, 0),  # in force at the window start
        (START + timedelta(minutes=3), BATTERY.id, -500.0, -500.0, 0),
        (START + timedelta(minutes=4), BATTERY.id, 999.0, 999.0, 1),  # a dry run moved nothing
        (START + timedelta(minutes=5), BATTERY.id, 250.0, None, 0),  # requested, never confirmed
    ]
    if other_battery:
        rows = [(START, f"{BATTERY.id}_other", 1.0, 1.0, 0)]
    connection = sqlite3.connect(source)
    connection.executemany(
        "INSERT INTO dispatch_log(ts, battery_id, requested_w, applied_w, dry_run, result)"
        " VALUES(?, ?, ?, ?, ?, 'ok')",
        [
            (to_iso(moment), battery_id, requested, applied, dry)
            for moment, battery_id, requested, applied, dry in rows
        ],
    )
    connection.commit()
    connection.close()
    return source


def test_the_episode_carries_the_setpoint_in_force_at_the_window_start(tmp_path: Path) -> None:
    source = with_dispatch(tmp_path)
    with open_episode(source, START, START + timedelta(minutes=20), 1.0) as episode:
        rows = list(episode.dispatch)
    # Ordered by (ts, id) and seeded with the newest row before the window, because the setpoint in
    # force when the window opens was written before it.
    assert [row["ts"] for row in rows] == sorted(row["ts"] for row in rows)
    assert rows[0]["ts"] < to_iso(START) and rows[0]["applied_w"] == pytest.approx(300.0)
    assert [row["applied_w"] for row in rows[1:3]] == [pytest.approx(-500.0), pytest.approx(999.0)]
    assert rows[3]["applied_w"] is None and rows[3]["requested_w"] == pytest.approx(250.0), (
        "the unconfirmed row is the recording's request, with no applied value"
    )


def test_the_recorded_policy_reads_the_setpoint_in_force_and_holds_when_missing(
    tmp_path: Path,
) -> None:
    source = with_dispatch(tmp_path)
    with open_episode(source, START, START + timedelta(minutes=20), 1.0) as episode:
        policy = RecordedPolicy(episode)
        assert policy(BATTERY, START) == pytest.approx(300.0), "the pre-window setpoint is in force"
        assert policy(BATTERY, START + timedelta(minutes=2)) == pytest.approx(300.0)
        assert policy(BATTERY, START + timedelta(minutes=3)) == pytest.approx(-500.0)
        assert policy(BATTERY, START + timedelta(minutes=4, seconds=30)) == pytest.approx(-500.0), (
            "a dry run sits in the history but moved nothing, so the older setpoint stays in force"
        )
        assert policy(BATTERY, START + timedelta(minutes=5)) == pytest.approx(250.0), (
            "an unconfirmed row falls back to what the recording requested"
        )
        assert policy(BATTERY, START - timedelta(minutes=6)) is None, "no row yet: hold, never invent"
        summary = policy.summary()
    assert summary["missing_holds"] == 1
    assert summary["first_missing"] == to_iso(START - timedelta(minutes=6))
    assert summary["provided"] == 5
    assert summary["requested_fallback"] == 1
    assert summary["rows"] == 3 and summary["packs"] == 1, "id-free counts: the dry run is not a row"
    assert summary["last_source"] == "requested"


def test_a_recording_without_dispatch_history_refuses_the_recorded_leg(tmp_path: Path) -> None:
    source = recording(tmp_path)
    with open_episode(source, START, START + timedelta(minutes=2), 1.0) as episode:
        with pytest.raises(RunnerError) as caught:
            Runner(episode, name="E4.recorded", plant=SIMULATED, policy="recorded")
    assert caught.value.reason == NO_DISPATCH


def test_a_recorded_leg_that_never_found_a_setpoint_is_refused(tmp_path: Path) -> None:
    source = with_dispatch(tmp_path, other_battery=True)
    with open_episode(source, START, START + timedelta(minutes=2), 1.0) as episode:
        result = Runner(episode, name="E4.recorded", plant=SIMULATED, policy="recorded").run()
    assert result.refused is True
    assert [refusal.reason for refusal in result.refusals] == [RECORDED_POLICY_UNKNOWN]
    assert result.policy_notes["provided"] == 0
    assert result.policy_notes["missing_holds"] > 0


def test_the_recorded_leg_applies_the_recording_and_journals_the_engines_request(
    tmp_path: Path,
) -> None:
    source = with_dispatch(tmp_path)
    with open_episode(source, START, START + timedelta(minutes=2), 1.0) as episode:
        runner = Runner(episode, name="E4.recorded", plant=SIMULATED, policy="recorded")
        result = runner.run()
        # Read the scratch journal while the episode (and its scratch copy) is still open, and keep
        # only the replay's own rows: the copy still holds the recording's history up to the cut.
        cut = int(episode.boundary.dispatch_id or 0)
        rows = [row for row in result.store.dispatch_log(limit=200) if int(row["id"]) > cut]
    assert result.policy_notes["provided"] > 0
    assert result.policy_notes["missing_holds"] == 0
    assert runner.drivers[0].commands[-1][1] == pytest.approx(300.0), (
        "the plant applied the recorded setpoint, not the engine's request"
    )
    assert rows and all(row["applied_w"] == pytest.approx(300.0) for row in rows)
    assert any(abs(float(row["requested_w"]) - 300.0) > 1e-9 for row in rows), (
        "the engine's own request stays in the journal, so every row is the pair"
    )


def test_a_caller_supplied_command_source_still_wins(tmp_path: Path) -> None:
    seen: list[datetime] = []

    def source(config, now):
        seen.append(now)
        return 700.0

    with a_window(tmp_path, minutes=2) as episode:
        runner = Runner(
            episode,
            name="E4.recorded",
            plant=SIMULATED,
            policy="recorded",
            command_source=source,
        )
        runner.step(episode.start)
    assert seen, "the substitute driver asks the injected policy at the replay instant"
    assert seen[0] == episode.start
    assert runner.drivers[0].commands[-1][1] == pytest.approx(700.0)


def test_a_settings_patch_reaches_the_engine_and_not_the_recording(tmp_path: Path) -> None:
    with a_window(tmp_path, minutes=2) as episode:
        runner = Runner(
            episode,
            name="E2.declared_model",
            plant=SIMULATED,
            settings_patch={"optimizer.feed_in_wiring": "shadow", "price.feed_in_mode": "explicit"},
        )
        assert runner.engine.settings.get("optimizer.feed_in_wiring") == "shadow"
        assert runner.engine.settings.get("price.feed_in_mode") == "explicit"
        assert episode.settings_values["optimizer.feed_in_wiring"] == "off"
        with pytest.raises(RunnerError) as caught:
            Runner(episode, name="x", plant=SIMULATED, settings_patch={"nonsense.key": 1})
    assert caught.value.reason == "runner_invalid"


def test_an_unknown_plant_or_policy_is_refused(tmp_path: Path) -> None:
    with a_window(tmp_path, minutes=2) as episode:
        with pytest.raises(RunnerError):
            Runner(episode, name="x", plant="imaginary")
        with pytest.raises(RunnerError):
            Runner(episode, name="x", plant=RECORDED, policy="wishful")


# ----------------------------------------------------------------------------------- the record


def test_the_leg_record_carries_the_counts_and_the_plant_tally(tmp_path: Path) -> None:
    with a_window(tmp_path) as episode:
        result = Runner(episode, name="E1.replay", plant=RECORDED).run()
    assert result.name == "E1.replay"
    assert (result.plant, result.policy, result.patch) == (RECORDED, "production", ())
    assert result.steps == WINDOW_S + 1
    assert result.control_steps == result.tally.ticks, "the plant tally is counted per control tick"
    assert result.tally.commands > 0
    assert result.tally.infeasible_commands == 0
    # The recorded arm never evaluates the site export bound (plant.py says so): every command is
    # counted as unverified, and the manifest declares that ceiling as not applicable.
    assert result.tally.unverified_bounds == result.tally.commands
    assert result.tally.unavailable_ticks == 0
    assert result.facade["commands"] == 0
    assert result.events and all(str(row["code"]) != "engine_start" for row in result.events)
    assert all(row["ts"] for row in result.events)
    assert result.vintage_calls
    assert [tally.index for tally in result.window_tallies] == [
        boundary.index for boundary in result.windows
    ]
    assert sum(tally.unverified_bounds for tally in result.window_tallies) == (
        result.tally.unverified_bounds
    )
    assert result.buckets_written[0] == START
    # One 5-minute bucket per control tick cadence, plus the boundary bucket the final tick lands in
    # (10:20:00, outside [start, end), so the accounting domain never includes it).
    assert len(result.buckets_written) == 5
    assert result.buckets_written[-1] == episode.end


def test_two_runs_of_the_same_window_are_identical(tmp_path: Path) -> None:
    source = recording(tmp_path)
    limits = (START, START + timedelta(minutes=WINDOW_MINUTES), 1.0)
    with open_episode(source, *limits) as first, open_episode(source, *limits) as second:
        left = Runner(first, name="E1.replay", plant=RECORDED).run()
        right = Runner(second, name="E1.replay", plant=RECORDED).run()
    assert left.events == right.events, "the journal is on the virtual clock, so even its ts match"
    assert left.vintage_calls == right.vintage_calls
    assert left.windows == right.windows
    assert left.steps == right.steps and left.tally.ticks == right.tally.ticks
    assert [
        (state.energy_kwh, state.soc_pct) for state in left.final_battery_states
    ] == [(state.energy_kwh, state.soc_pct) for state in right.final_battery_states]
    assert left.store.actuals_range(START, first.end) == right.store.actuals_range(START, second.end)


# ------------------------------------------------------- the measurement rows the ledger reads


def test_the_recorded_arm_re_persists_the_recordings_own_measurement_rows(tmp_path: Path) -> None:
    """The live sampler -- ``app.main``'s closure, never the engine -- writes the measurement rows.

    A replay injects its own sampler, so nothing else writes them, and the cost-basis ledger reads
    exactly these rows: without them it declares itself stalled after the stall tolerance and the
    sell floor goes inactive, which is a behavioural difference from the recording.  The recorded arm
    re-persists the recording's own rows verbatim, so its ledger sees the history it saw live.
    """
    with a_window(tmp_path, minutes=10) as episode:
        recorded = [
            row
            for row in (episode.boundary.source_rows.get("measurements") or ())
            if row["ts"] < to_iso(episode.end)
        ]
        result = Runner(episode, name="E1.replay", plant=RECORDED).run()
        written = dict(result.measurement_rows)
        stored = result.store.measurements_range(START, episode.end)
    assert recorded and stored, "the fixture carries measurement rows and the replay writes them"
    assert set(written) == {str(row["entity_role"]) for row in recorded}
    expected = {(str(row["entity_role"]), str(row["ts"])): float(row["value"]) for row in recorded}
    for row in stored:
        key = (str(row["entity_role"]), str(row["ts"]))
        if key in expected:
            assert float(row["value"]) == pytest.approx(expected[key]), "verbatim, never rescaled"
    assert result.measurement_buckets and result.measurement_failures == ()
    assert all(bucket.minute % 5 == 0 for bucket in result.measurement_buckets)


def test_the_simulated_arms_measurement_rows_are_the_plants_own_flows(tmp_path: Path) -> None:
    """The simulated arm writes its own flows under the ledger's roles, and omits what it cannot know.

    The roles come from :func:`app.cost_basis.measurement_roles` (the list the ledger reads), the
    sign follows the persisted convention (the charge/discharge pair, both sides written), and an
    unknown flow or site value contributes no row at all rather than a zero.
    """
    from types import SimpleNamespace

    class StubDriver:
        driver_name = "stub"

        def __init__(self, power):
            self.config = BATTERY
            self._power = power

        def id(self) -> str:
            return self.config.id

        def realized_power(self):
            return self._power

        def read_state(self):
            return SimpleNamespace(id=self.config.id, soc_pct=55.0, energy_kwh=2.0)

    with a_window(tmp_path) as episode:
        runner = Runner(episode, name="E4.policy", plant=SIMULATED)
        runner.drivers = [StubDriver(500.0)]
        rows = {(row.role, row.unit): row.value for row in runner._measurement_rows(START, 900.0)}
        assert rows[(f"battery_charge:{BATTERY.id}", "W")] == pytest.approx(500.0)
        assert rows[(f"battery_discharge:{BATTERY.id}", "W")] == 0.0, "both sides are written"
        assert rows[(f"battery_soc:{BATTERY.id}", "%")] == pytest.approx(55.0)
        assert rows[("grid_power", "W")] == pytest.approx(900.0)
        runner.drivers = [StubDriver(-300.0)]
        discharging = {(row.role, row.unit): row.value for row in runner._measurement_rows(START, None)}
        assert discharging[(f"battery_charge:{BATTERY.id}", "W")] == 0.0
        assert discharging[(f"battery_discharge:{BATTERY.id}", "W")] == pytest.approx(300.0)
        assert ("grid_power", "W") not in discharging, "an unknown site flow writes no site row"
        runner.drivers = [StubDriver(None)]
        unknown_flow = {row.role for row in runner._measurement_rows(START, 900.0)}
        assert unknown_flow == {f"battery_soc:{BATTERY.id}", "grid_power"}, (
            "an unknown pack flow writes no flow row, but its measured SoC is still known"
        )


def test_a_measurement_write_failure_is_recorded_and_the_leg_keeps_running(
    tmp_path: Path, monkeypatch
) -> None:
    """A failed write is the live closure's warning path, and gate 2 attributes the stall it causes."""
    with a_window(tmp_path) as episode:
        runner = Runner(episode, name="E1.replay", plant=RECORDED)

        def broken(_rows):
            raise sqlite3.OperationalError("disk full")

        monkeypatch.setattr(episode.store(), "add_measurements", broken)
        runner.sampler(episode.start)
        assert [refusal.reason for refusal in runner.measurement_failures] == [
            MEASUREMENT_WRITE_FAILED
        ]
        assert runner.refusals == [], "the run is not refused: the ledger's stall is the finding"
        result = runner.result()
        assert result.measurement_failures and result.measurement_rows == ()
