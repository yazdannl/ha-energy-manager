"""Gate, delta and rendering tests for the replay report (docs/REPLAY_HARNESS.md sections 4-7).

The fixtures are one-hour synthetic recordings from the manifest suite's builder, with two local
variations the report needs: the recording's own dispatch history (so the recorded-policy leg has
setpoints to replay), and a *fresh* plan vintage -- a recording whose last plan was issued five
minutes before the window, which is what a live recording looks like when the replay starts.  A
30-minute-old plan makes the replay replan on its first tick (its own declared cadence), and the
report is expected to say so rather than hide it: that is the difference between "the harness ran"
and "the harness reproduced the recording's decisions".
"""

from __future__ import annotations

import io
import json
import sqlite3
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "energy_manager"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.replay import __main__ as cli  # noqa: E402
from app.replay.accounting import (  # noqa: E402
    RECORDING_SELF_CONSISTENCY,
    TARIFF_MODEL_DERIVED,
    account_leg,
)
from app.replay.episode import open_episode  # noqa: E402
from app.replay.manifest import default_plant_limits, default_tolerances  # noqa: E402
from app.replay.report import (  # noqa: E402
    ACCOUNTING_FIDELITY,
    ANCHOR_LEG_IS_THE_FIDELITY_REFERENCE,
    DECISION_FIDELITY,
    FLOOR_STALL,
    ILLUSTRATIVE_INTERVAL,
    SOURCE_BASIS_COMPARAND,
    TERMINAL_PAIR_IS_EXTENDED_HORIZON,
    TERMINAL_PAIR_SAME_SCHEDULE,
    Finding,
    _bootstrap_dict,
    gate_accounting_fidelity,
    gate_decision_fidelity,
    EMPIRICAL,
    PLANT_FIDELITY,
    REPORT_SCHEMA,
    LegRun,
    contract_verdict,
    deterministic_view,
    gates_for,
    pair_legs,
    render,
    terminal_pair,
    toon,
)
from app.replay.runner import run_leg  # noqa: E402
from app.replay.statistics import (  # noqa: E402
    HORIZON_FROM_PLAN_SPAN,
    HORIZON_FROM_SETTING,
    HORIZON_SOURCE_UNKNOWN,
    HORIZON_UNKNOWN,
    block_bootstrap,
)
from app.models import to_iso  # noqa: E402
from app.store import Store  # noqa: E402
from test_replay_accounting import add_cost_basis  # noqa: E402
from test_replay_manifest import BATTERY, START, recording  # noqa: E402

WINDOW_MINUTES = 5
FRESH_OFFSET_MINUTES = -5


def _with_history(path: Path, *, fresh_vintage: bool, history: bool) -> Path:
    """Add the recording's own dispatch history and (optionally) make its plan vintage fresh."""
    connection = sqlite3.connect(path)
    if fresh_vintage:
        connection.execute(
            "UPDATE plans SET created_at = ?",
            ((START + timedelta(minutes=FRESH_OFFSET_MINUTES)).isoformat(),),
        )
    connection.commit()
    connection.close()
    if not history:
        return path
    store = Store(path)
    try:
        # The recording's own journal: a pre-window row (survives the cut) and an in-window one
        # (removed as post-start information).  Neither is the replay's journal.
        store.add_event(
            ts=START - timedelta(hours=1), level="info", code="engine_running", message="recorded"
        )
        store.add_event(
            ts=START + timedelta(minutes=1),
            level="warning",
            code="battery_floor_reached",
            message="recorded",
        )
        moment = START
        while moment < START + timedelta(hours=1):
            store.add_dispatch(
                ts=moment,
                battery_id=BATTERY.id,
                requested_w=0.0,
                applied_w=0.0,
                dry_run=False,
                mode="auto",
                result="ok",
                code="ok",
            )
            moment += timedelta(minutes=5)
    finally:
        store.close()
    return path


def recording_path(tmp_path: Path, *, fresh_vintage: bool = True, history: bool = True) -> Path:
    path = recording(tmp_path)
    # Book the sell price the way the derived contract does (buy - fee - tax, no export fee or tax
    # recorded here), so gate 3's tariff check reads zero on this fixture and the deliberate breach
    # is made explicitly by the test that wants one.
    connection = sqlite3.connect(path)
    connection.execute("UPDATE actuals SET price_sell = price_buy WHERE price_buy IS NOT NULL")
    connection.commit()
    connection.close()
    if history or fresh_vintage:
        _with_history(path, fresh_vintage=fresh_vintage, history=history)
    return path


def drive(
    tmp_path: Path,
    *,
    arm: str = "E1",
    minutes: int = WINDOW_MINUTES,
    source: Path | None = None,
    fresh_vintage: bool = True,
    fmt: str = "json",
    extra: tuple[str, ...] = (),
) -> tuple[int, dict, str]:
    """Run the CLI in-process and return ``(exit code, report, stdout)``."""
    path = source or recording_path(tmp_path, fresh_vintage=fresh_vintage)
    argv = [
        "--store", str(path),
        "--start", START.isoformat(),
        "--end", (START + timedelta(minutes=minutes)).isoformat(),
        "--arm", arm,
        "--format", fmt,
        *extra,
    ]
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = cli.main(argv)
    text = buffer.getvalue()
    if fmt == "json":
        return code, (json.loads(text) if text.strip() else {}), text
    return code, {}, text


def one_leg(tmp_path: Path, *, minutes: int = WINDOW_MINUTES, fresh_vintage: bool = True) -> LegRun:
    """One driven leg (recorded plant, production policy) with its own account, without the CLI."""
    path = recording_path(tmp_path, fresh_vintage=fresh_vintage)
    with open_episode(str(path), START, START + timedelta(minutes=minutes), 1.0) as episode:
        result = run_leg(episode, name="replay", plant="recorded")
        return LegRun(result=result, account=account_leg(result, limits=default_plant_limits()))


def gate_named(report: dict, gate: str) -> dict:
    return next(verdict for verdict in report["gates"] if verdict["gate"] == gate)


# -------------------------------------------------------------------------------------------- gates


def test_e1_passes_every_measured_gate_and_exits_zero(tmp_path: Path) -> None:
    code, report, text = drive(tmp_path)
    assert code == 0, [verdict for verdict in report["gates"] if verdict["passed"] is False]
    assert report["schema"] == REPORT_SCHEMA
    assert report["limits"]["status"] == "gates_passed"
    for verdict in report["gates"]:
        assert verdict["passed"] is True, verdict
    fidelity = gate_named(report, PLANT_FIDELITY)
    assert fidelity["detail"]["reference_leg"] == "replay"
    assert fidelity["detail"]["reference_divergence_kwh"] == pytest.approx(0.0, abs=1e-9)
    decision = gate_named(report, DECISION_FIDELITY)
    assert decision["detail"]["fatal_findings"] == 0
    assert decision["detail"]["grid_step_s"] == 3600.0, "the tolerance is the planner's own grid"
    assert report["accounting"]["legs"][0]["windows"][0]["profit_eur"] is not None
    # A lost store write would shrink the very decisions gate 2 judges, so it must show up as one.
    assert report["drive"]["legs"][0]["store_failures"] == {}
    assert decision["detail"]["store_failures"] == {}
    # The leg's journal is the replay's own: not the recording's pre-window history, and not the
    # recording's in-window rows either (those are post-start information for this window).
    events = report["drive"]["legs"][0]["events"]
    assert "engine_running" not in events["by_code"]
    assert "battery_floor_reached" not in events["by_code"]
    assert events["by_code"] == {} or set(events["by_code"]) <= {"capacity_learned_applied"}


def test_a_stale_recorded_plan_is_reported_as_a_decision_failure(tmp_path: Path) -> None:
    """The fixture's plan is 30 minutes old, so the replay replans on its first tick: say so."""
    code, report, _text = drive(tmp_path, fresh_vintage=False)
    assert code == 2
    decision = gate_named(report, DECISION_FIDELITY)
    assert decision["passed"] is False
    assert decision["detail"]["fatal_findings"] >= 1
    assert any(finding["kind"].startswith("slot_") for finding in decision["findings"])
    assert report["limits"]["status"] == "refused_at_a_gate"
    assert report["limits"]["withheld"] == DECISION_FIDELITY
    # A failed gate withholds the money: no delta is printed on top of an unexplained divergence.
    assert report["accounting"]["withheld_by_gate"] == DECISION_FIDELITY
    assert report["accounting"]["paired"] is None
    # The replay's own replan is journalled, and the recording's rows are still not its journal.
    events = report["drive"]["legs"][0]["events"]
    assert events["by_code"].get("plan_updated", 0) >= 1
    assert "engine_running" not in events["by_code"]
    assert "battery_floor_reached" not in events["by_code"]


def test_gate_two_and_three_are_not_measured_without_a_recorded_leg(tmp_path: Path) -> None:
    """A negative control: no recorded production leg means the gates say so, never guess.

    Every arm now declares a reference leg, so this pins the *absence* path directly: an arm whose
    legs hold only a pair and a recorded-plant leg under another policy has no E1 comparand.
    """
    from dataclasses import replace

    from app.replay.manifest import arm_spec

    path = recording_path(tmp_path)
    spec = arm_spec(["E4"], None)[0]
    trimmed = replace(
        spec,
        legs=tuple(
            leg for leg in spec.legs if not (leg.plant == "recorded" and leg.policy == "production")
        ),
    )
    assert [leg.name for leg in trimmed.legs] == [
        "policy",
        "recorded_policy",
        "recorded_policy_recorded_plant",
    ]
    legs = []
    for leg in trimmed.legs:
        episode = open_episode(str(path), START, START + timedelta(minutes=WINDOW_MINUTES), 1.0)
        try:
            source = cli.recorded_policy_source(episode) if leg.policy == "recorded" else None
            result = run_leg(
                episode,
                name=leg.name,
                plant=leg.plant,
                policy=leg.policy,
                settings_patch=leg.patch_dict(),
                command_source=source,
            )
            legs.append(
                LegRun(result=result, account=account_leg(result, limits=default_plant_limits()))
            )
        finally:
            episode.close()
    verdicts = gates_for(
        trimmed, legs, tolerances=default_tolerances(60), isolation=None
    )
    verdict = next(item for item in verdicts if item.gate == DECISION_FIDELITY)
    assert verdict.passed is None and verdict.reasons == ("no_recorded_leg",)
    assert "no recorded, production-policy leg" in verdict.detail["note"]
    reference = verdict.detail["reference"]
    assert [leg["plant"] for leg in reference["declared_legs"]] == [
        "simulated",
        "simulated",
        "recorded",
    ]
    assert reference["recorded_plans_in_window"] >= 1
    assert reference["recorded_dispatch_rows_in_window"] >= 1
    assert next(item for item in verdicts if item.gate == ACCOUNTING_FIDELITY).passed is None
    assert next(item for item in verdicts if item.gate == PLANT_FIDELITY).measured is True


def test_gate_one_reports_the_plant_model_term_and_the_frozen_reference(tmp_path: Path) -> None:
    leg = one_leg(tmp_path)
    verdict = gates_for(
        _arm_stub("E1"), [leg], tolerances=_tolerances(), isolation=None
    )[0]
    assert verdict.passed is True
    assert "plant_model_error_kwh" not in verdict.detail, "no substitute leg, so no model term"
    assert verdict.detail["reference_divergence_kwh"] == pytest.approx(0.0, abs=1e-9)


def test_the_report_carries_the_ceiling_and_withheld_flags_per_window(tmp_path: Path) -> None:
    _code, report, _text = drive(tmp_path)
    rows = report["plant"]["legs"][0]["windows"]
    assert rows and all("withheld" in row and "reasons" in row for row in rows)
    assert report["plant"]["legs"][0]["ceiling"]["unverified_bounds"] is None, "recorded plant: n/a"


# ----------------------------------------------------------------------------------------- rendering


def test_the_report_is_deterministic_apart_from_the_clock(tmp_path: Path) -> None:
    _code, first, _text = drive(tmp_path / "a")
    _code, second, _text = drive(tmp_path / "b")
    assert first["run"]["runtime_s"] is not None and second["run"]["runtime_s"] is not None
    assert deterministic_view(first) == deterministic_view(second)


def test_the_information_section_reports_where_each_frozen_value_came_from(tmp_path: Path) -> None:
    """Every frozen decision input names its origin, so a report can say how it was vintaged."""
    code, report, _text = drive(tmp_path)
    assert code == 0
    sources = report["information"]["as_of_sources"]
    assert sources
    for leg_sources in sources.values():
        assert leg_sources
        assert set(leg_sources.values()) <= {"history", "seed", "current"}


def test_both_dialects_are_offered_and_toon_is_a_subset(tmp_path: Path) -> None:
    code, report, _text = drive(tmp_path)
    assert code == 0
    rendered = render(report, "toon")
    for prefix in ("schema:", "run.manifest_digest:", "gates[]:", "accounting.arm:", "information.pv_load_source.", "limits.status:"):
        assert prefix in rendered, prefix
    assert "pv_load_source" in rendered and "vintages" in rendered
    # The CLI's own dialect carries the same sections in the report's own order.
    _code, _report, text = drive(tmp_path / "cli", fmt="toon")
    assert text.startswith("schema: ")
    assert text.index("run.window.start:") < text.index("accounting.")
    assert text.index("accounting.") < text.index("gates[3]{}:") < text.index("limits.status:")
    assert json.loads(render(report, "json"))["schema"] == REPORT_SCHEMA


def test_toon_renders_every_shape_the_report_uses() -> None:
    payload = {
        "a": 1,
        "b": None,
        "c": {"d": "text", "e": {}},
        "f": [{"g": True}, {"g": False}],
        "h": [],
        "i": [[1, 2]],
    }
    lines = toon(payload).splitlines()
    assert lines[0] == "a: 1"
    assert "b: null" in lines
    assert "c.d: \"text\"" in lines
    assert "c.e: {}" in lines
    assert "f[2]{}:" in lines and 'f[]: {"g": true}' in lines
    assert "h: []" in lines
    assert "i: [[1, 2]]" in lines


# ------------------------------------------------------------------------------------------ refusals


def test_e4_reports_the_plant_model_term_and_labels_the_evidence(tmp_path: Path) -> None:
    """E4's recorded legs differ only in their plant, so gate 1 can name the plant-model error."""
    code, report, _text = drive(tmp_path, arm="E4", minutes=10)
    assert code == 0, report.get("refusal", report["gates"])
    assert report["accounting"]["evidence"] == EMPIRICAL, "a substitute for SAA is never declared"
    assert len(report["accounting"]["legs"]) == 4, "the pair plus its reference leg"
    fidelity = gate_named(report, PLANT_FIDELITY)
    assert fidelity["detail"]["plant_model_error_kwh"] is not None
    assert fidelity["detail"]["plant_model_windows"] >= 1
    assert any(finding["kind"] == "plant_model" for finding in fidelity["findings"])
    paired = report["accounting"]["paired"]
    assert paired is not None and paired["change"] == "policy"
    assert report["accounting"]["terminal_pair"] is None


def test_e3_pairs_the_terminal_vector_against_salvage_on_extended_horizon_accounting(
    tmp_path: Path,
) -> None:
    """E3 is a real pair now: ``salvage`` vs ``vector``, read on the extended-horizon delta.

    On this fixture the pair is an identity and says so: the recording's booked sell equals its buy
    (the derived contract, which gate 3 checks), and the declared basis sets a sell floor above
    every tail sale, so the vector is refused and both legs fall back to the same salvage schedule.
    What is asserted is therefore the *shape* of the comparison -- which two legs, which number, and
    that a zero is labelled rather than published as a terminal effect -- not a gain: the window is
    shorter than one plan horizon, so the interval stays ``illustrative_interval``.
    """
    source = recording_path(tmp_path)
    add_cost_basis(source, 0.4, 3.0)
    code, report, _text = drive(tmp_path, arm="E3", minutes=10, source=source)
    assert code == 0, report.get("refusal", report.get("gates"))
    accounting = report["accounting"]
    assert accounting["evidence"] == EMPIRICAL
    assert [leg["leg"] for leg in accounting["legs"]] == ["replay", "salvage", "vector"]
    paired = accounting["paired"]
    assert paired is not None and paired["change"] == "terminal_treatment"
    assert paired["legs"] == ["salvage", "vector"], "baseline first, the vector is the declared side"
    # One declared valuation for both legs, so the two terminal terms are comparable at all.
    terminal = accounting["terminal_pair"]
    assert terminal is not None, "the fixture declares a window-start basis, so the term is valued"
    assert terminal["valuations_agree"] is True
    assert terminal["valuation_eur_per_kwh"] == pytest.approx(0.4)
    assert terminal["valuation_source"] == "frozen_profile_row"
    assert terminal["terminal_delta_eur"] == pytest.approx(
        terminal["vector_terminal_adjustment_eur"] - terminal["salvage_terminal_adjustment_eur"]
    )
    assert paired["delta_with_terminal_eur"] == pytest.approx(
        paired["delta_eur"] + paired["terminal_delta_eur"], abs=2e-4
    )
    codes = [note["code"] for note in accounting["notes"]]
    assert TERMINAL_PAIR_IS_EXTENDED_HORIZON in codes, "the arm's number is the extended-horizon one"
    assert terminal["schedule_moved"] is False
    assert TERMINAL_PAIR_SAME_SCHEDULE in codes, "a pair whose schedules never moved is not a result"
    # The evidence rules are unchanged by any of this.
    assert accounting["bootstrap"]["underpowered"] is True
    assert ILLUSTRATIVE_INTERVAL in accounting["evidence_labels"]
    assert TERMINAL_PAIR_IS_EXTENDED_HORIZON in accounting["evidence_labels"]


def _segment(start: datetime, end: datetime, grid_kw: float, import_kwh: float,
             export_kwh: float) -> SimpleNamespace:
    return SimpleNamespace(
        start=start, end=end, grid_kw=grid_kw, import_kwh=import_kwh, export_kwh=export_kwh
    )


def _window(*segments: SimpleNamespace, withheld: bool = False) -> SimpleNamespace:
    return SimpleNamespace(withheld=withheld, segments=list(segments))


def _terminal_leg(**overrides: object) -> SimpleNamespace:
    """A leg stand-in with only the fields the terminal disclosure reads."""
    account = {
        "terminal_adjustment_eur": 1.0,
        "valuation_eur_per_kwh": 0.4,
        "valuation_source": "frozen_profile_row",
        "valuation_frozen": ("pack",),
        "valuation_derived": (),
        "valuation_missing": (),
        "profit_eur": 2.0,
        "profit_with_terminal_eur": 3.0,
        "sensitivity_valuation_eur_per_kwh": 0.35,
        "sensitivity_terminal_adjustment_eur": 0.9,
        "import_kwh": 4.0,
        "export_kwh": 1.0,
        "energy_delta_kwh": 2.0,
    }
    account.update(overrides)
    return SimpleNamespace(account=SimpleNamespace(**account))


def test_the_terminal_disclosure_names_both_legs_and_refuses_a_valuation_mismatch() -> None:
    """The block is arithmetic, so it is tested as arithmetic: same valuation, two legs, one delta.

    A delta between two *different* valuations is not the terminal treatment's effect, so the block
    reports whether the two legs agreed instead of assuming it; and an unknown flow total leaves the
    schedule question unknown rather than answering "nothing moved".
    """
    hour = timedelta(hours=1)
    first = _segment(START, START + hour, 1.0, 1.0, 0.0)
    second = _segment(START + hour, START + 2 * hour, -1.0, 0.0, 1.0)
    pair = (
        _terminal_leg(windows=[_window(first, second)]),
        _terminal_leg(
            terminal_adjustment_eur=2.5, import_kwh=5.0, windows=[_window(first, second)]
        ),
    )
    block = terminal_pair(pair)
    assert block is not None
    assert block["valuations_agree"] is True
    # An identity in every booked flow: the same trajectory, not merely the same totals.
    assert block["schedule_moved"] is False
    assert block["terminal_delta_eur"] == pytest.approx(1.5)
    assert block["vector_profit_with_terminal_eur"] == pytest.approx(3.0)
    assert terminal_pair((_terminal_leg(), _terminal_leg(valuation_eur_per_kwh=0.9)))[
        "valuations_agree"
    ] is False
    # F6: equal totals are not an identity.  Both legs book the same 1 kWh in and 1 kWh out, but the
    # second books the import one slot later -- a charge moved between priced slots, which a totals
    # comparison would have labelled "the same schedule".
    early = _terminal_leg(
        windows=[_window(_segment(START, START + hour, 0.0, 1.0, 0.0),
                          _segment(START + hour, START + 2 * hour, 0.0, 0.0, 1.0))]
    )
    late = _terminal_leg(
        windows=[_window(_segment(START, START + hour, 0.0, 0.0, 1.0),
                          _segment(START + hour, START + 2 * hour, 0.0, 1.0, 0.0))]
    )
    assert early.account.import_kwh == late.account.import_kwh
    assert terminal_pair((early, late))["schedule_moved"] is True
    # An unknown trajectory is unknown, never "nothing moved": a withheld window, a leg with no
    # measured segment at all, and a segment with an unmeasured flow.
    withheld = _terminal_leg(windows=[_window(withheld=True)])
    assert terminal_pair((_terminal_leg(windows=[_window(first)]), withheld))[
        "schedule_moved"
    ] is None
    assert terminal_pair((_terminal_leg(), _terminal_leg(windows=[_window(first)])))[
        "schedule_moved"
    ] is None
    unknown_flow = _terminal_leg(windows=[_window(_segment(START, START + hour, 1.0, None, 0.0))])
    assert terminal_pair((_terminal_leg(windows=[_window(first)]), unknown_flow))[
        "schedule_moved"
    ] is None
    # R3: the comparison is exact, so flows that differ below the old rounding thresholds are a
    # moved schedule rather than an identity (1e-10 under the old 9-decimal grid rate rounding,
    # 1e-13 under the old 12-decimal energy rounding).
    exact = _terminal_leg(windows=[_window(_segment(START, START + hour, 1.0, 1.0, 0.0))])
    nudged_rate = _terminal_leg(
        windows=[_window(_segment(START, START + hour, 1.0 + 1e-10, 1.0, 0.0))]
    )
    nudged_energy = _terminal_leg(
        windows=[_window(_segment(START, START + hour, 1.0, 1.0 + 1e-13, 0.0))]
    )
    assert terminal_pair((exact, nudged_rate))["schedule_moved"] is True
    assert terminal_pair((exact, nudged_energy))["schedule_moved"] is True
    assert terminal_pair((_terminal_leg(), _terminal_leg(export_kwh=None)))[
        "schedule_moved"
    ] is None
    assert terminal_pair(None) is None


def test_pair_legs_selects_e3_by_name_not_by_position() -> None:
    """E3's pair must survive its extra reference leg and a reordered leg list."""
    arm = SimpleNamespace(name="E3")
    salvage, vector = _terminal_leg(), _terminal_leg()
    salvage.result = SimpleNamespace(name="salvage")
    vector.result = SimpleNamespace(name="vector")
    anchor = _terminal_leg()
    anchor.result = SimpleNamespace(name="replay")
    assert pair_legs(arm, [anchor, salvage, vector]) == (salvage, vector)
    assert pair_legs(arm, [vector, anchor, salvage]) == (salvage, vector)
    assert pair_legs(arm, [anchor, salvage]) is None, "half a pair is not a pair"
    assert pair_legs(SimpleNamespace(name="E1"), [anchor, salvage, vector]) is None


def test_a_recording_without_dispatch_history_refuses_the_recorded_policy(tmp_path: Path) -> None:
    path = recording_path(tmp_path, history=False)
    code, report, _text = drive(tmp_path, arm="E4", source=path)
    assert code == 2
    assert report["refusal"]["reason"] == "no_dispatch"
    assert report["limits"]["status"] == "refused"
    assert "gates" not in report, "a refusal measures nothing, and claims nothing"


def test_a_missing_recording_refuses_with_a_slug(tmp_path: Path) -> None:
    code, report, _text = drive(tmp_path, source=tmp_path / "absent.sqlite")
    assert code == 2
    assert report["refusal"]["reason"], report["refusal"]


def test_help_documents_the_exit_codes() -> None:
    text = cli.parser().format_help()
    assert "exit codes" in text
    assert "2  a gate failed" in text
    assert "1  an unexpected error" in text


# -------------------------------------------------------------------------------------- the manifest


def test_the_manifest_is_written_first_and_validated_on_a_second_run(tmp_path: Path) -> None:
    source = recording_path(tmp_path)
    manifest_path = tmp_path / "episode.json"
    argv = [
        "--store", str(source), "--start", START.isoformat(),
        "--end", (START + timedelta(minutes=WINDOW_MINUTES)).isoformat(),
        "--arm", "E1", "--format", "json", "--episode", str(manifest_path),
    ]
    with redirect_stdout(io.StringIO()) as buffer:
        assert cli.main(argv) == 0
    stored = json.loads(manifest_path.read_text())
    assert stored["version"] and stored["manifest_digest"]
    assert json.loads(buffer.getvalue())["run"]["manifest_digest"] == stored["manifest_digest"]
    # A widened tolerance changes the digest, so the stored contract refuses to be re-used.
    stored["tolerances"]["energy_kwh"] = 99.0
    manifest_path.write_text(json.dumps(stored, indent=2, sort_keys=True) + "\n")
    with redirect_stdout(io.StringIO()) as buffer:
        assert cli.main(argv) == 2
    assert json.loads(buffer.getvalue())["refusal"]["reason"] == "manifest_invalid"


def test_the_arm_is_refused_when_the_stored_manifest_declares_a_different_one(tmp_path: Path) -> None:
    source = recording_path(tmp_path)
    manifest_path = tmp_path / "episode.json"
    base = [
        "--store", str(source), "--start", START.isoformat(),
        "--end", (START + timedelta(minutes=WINDOW_MINUTES)).isoformat(),
        "--format", "json", "--episode", str(manifest_path),
    ]
    with redirect_stdout(io.StringIO()):
        assert cli.main([*base, "--arm", "E1"]) == 0
    with redirect_stdout(io.StringIO()) as buffer:
        assert cli.main([*base, "--arm", "E3"]) == 2
    assert json.loads(buffer.getvalue())["refusal"]["reason"] == "manifest_mismatch"


# ---------------------------------------------------------------------------------------- helpers


def _tolerances():
    from app.replay.manifest import default_tolerances

    return default_tolerances(60)


def _arm_stub(name: str):
    from app.replay.manifest import arm_spec

    return arm_spec([name], None)[0]


def with_cost_basis(path: Path, basis: float, *, when: timedelta | None = None) -> None:
    """The recording's own ledger row: a comparand gate 3 may read whatever its timestamp."""
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO learning_profiles(kind, key, updated_at, payload_json) VALUES(?, ?, ?, ?)"
        " ON CONFLICT(kind, key) DO UPDATE SET updated_at = excluded.updated_at,"
        " payload_json = excluded.payload_json",
        (
            "battery_cost_basis",
            BATTERY.id,
            to_iso(START + (when or timedelta(hours=-2))),
            json.dumps({"basis_eur_per_kwh": basis, "energy_kwh": 3.0}),
        ),
    )
    connection.commit()
    connection.close()


def live_leg(path: Path):
    """A driven recorded leg whose scratch copy is still open, plus the episode that owns it."""
    episode = open_episode(str(path), START, START + timedelta(minutes=WINDOW_MINUTES), 1.0)
    result = run_leg(episode, name="replay", plant="recorded")
    return episode, LegRun(result=result, account=account_leg(result, limits=default_plant_limits()))


# ------------------------------------------- the measurement rows, the ledger and its comparand


def test_gate_two_reports_the_measurement_rows_it_wrote_and_the_ledger_comparand(
    tmp_path: Path,
) -> None:
    """The rows that keep the ledger alive are measured evidence, so gate 2 states them per role."""
    episode, leg = live_leg(recording_path(tmp_path))
    try:
        verdict = gate_decision_fidelity(leg, tolerances=default_tolerances(60))
    finally:
        episode.close()
    rows = verdict.detail["measurements"]["rows_per_role"]
    assert rows and all(count > 0 for count in rows.values())
    assert any(role.startswith("battery_soc:") for role in rows)
    assert verdict.detail["measurements"]["failures_total"] == 0
    stall = verdict.detail["ledger_stall"]
    assert stall["comparand"] == SOURCE_BASIS_COMPARAND
    assert [entry["battery_id"] for entry in stall["packs"]] == [BATTERY.id]
    assert stall["diverged"] == [], "the instrumented run keeps its ledger live on both sides"


def test_a_ledger_that_stalled_while_the_recording_stayed_live_fails_gate_two(
    tmp_path: Path,
) -> None:
    """A stalled ledger deactivates the sell floor, so gate 2 names it instead of blaming the policy."""
    path = recording_path(tmp_path)
    with_cost_basis(path, 0.4)
    episode, leg = live_leg(path)
    try:
        leg.result.store.profile_put(
            "battery_cost_basis",
            BATTERY.id,
            {
                "basis_eur_per_kwh": 0.4,
                "energy_kwh": 3.0,
                "no_samples_since": to_iso(START),
                "skipped_no_samples": 2,
            },
            START + timedelta(minutes=15),
        )
        verdict = gate_decision_fidelity(leg, tolerances=default_tolerances(60))
    finally:
        episode.close()
    stall = verdict.detail["ledger_stall"]
    assert [entry["battery_id"] for entry in stall["packs"]] == [BATTERY.id]
    assert stall["diverged"] == [BATTERY.id]
    assert stall["packs"][0]["recorded_stalled"] is False, "the recording's own ledger stayed live"
    assert [finding.kind for finding in verdict.findings] == [FLOOR_STALL]
    assert verdict.passed is False and FLOOR_STALL in verdict.reasons


def test_gate_three_compares_the_booked_basis_with_the_recordings_own_row(tmp_path: Path) -> None:
    """The comparand is the source recording's row -- read-only, and allowed past the window end."""
    path = recording_path(tmp_path)
    with_cost_basis(path, 0.4)
    episode, leg = live_leg(path)
    try:
        agreed = gate_accounting_fidelity(leg, tolerances=default_tolerances(60))
        # The rebuild drifted from what the recording held: the difference is the finding.
        leg.result.store.profile_put(
            "battery_cost_basis",
            BATTERY.id,
            {"basis_eur_per_kwh": 0.9, "energy_kwh": 3.0},
            leg.result.start + timedelta(minutes=5),
        )
        diverged = gate_accounting_fidelity(leg, tolerances=default_tolerances(60))
    finally:
        episode.close()
    assert agreed.detail["basis_comparand"] == SOURCE_BASIS_COMPARAND
    assert agreed.detail["packs_compared"] == 1
    assert agreed.detail["worst_basis_eur_per_kwh"] == 0.0
    assert agreed.passed is True
    assert [finding.kind for finding in diverged.findings] == ["basis_divergence"]
    assert diverged.detail["worst_basis_eur_per_kwh"] == pytest.approx(0.5)
    assert diverged.passed is False


def test_the_declared_contract_says_whether_export_was_priced(tmp_path: Path) -> None:
    """E2 must not let a refusal to price export read as a tariff error: the report states which it is."""
    extra = (
        "--feed-in-wiring", "on",
        "--feed-in-mode", "explicit",
        "--setting", "price.feed_in_eur_per_kwh=0.13",
    )
    code, report, _text = drive(tmp_path, arm="E2", fresh_vintage=False, extra=extra)
    # The stale vintage makes the replay replan, which this fixture's synthesized 0 W history cannot
    # match: gate 2 names that divergence (exit 2, the pair's money withheld), while the statement
    # about the declared contract is still what this test is about.
    assert code == 2, report.get("refusal", report["gates"])
    assert gate_named(report, DECISION_FIDELITY)["passed"] is False
    assert report["accounting"]["withheld_by_gate"] == DECISION_FIDELITY
    contract = report["drive"]["contract"]
    assert contract["declared"]["price.feed_in_eur_per_kwh"] == pytest.approx(0.13)
    refusals = contract["evidence"]["refusals_by_code"]
    # The verdict is derived from the evidence, so the two can never disagree.
    assert contract["verdict"] == ("resolved" if not refusals else "partial_refusal")
    assert contract["clean_tariff_delta"] == (contract["verdict"] == "resolved")
    assert contract["evidence"]["priced_slots"] >= 1
    assert report["accounting"]["contract"]["verdict"] == contract["verdict"], (
        "the verdict travels with the paired delta it produced"
    )
    if refusals:
        assert "not a clean tariff delta" in contract["note"]
        assert "not a clean tariff delta" in report["accounting"]["contract"]["note"]


def test_the_declared_contract_is_not_accepted_for_an_arm_without_one(tmp_path: Path) -> None:
    """Only E2 compares two contract models, so only E2 may declare one."""
    code, report, _text = drive(tmp_path, arm="E1", extra=("--setting", "price.feed_in_eur_per_kwh=0.13"))
    assert code == 2
    assert report["refusal"]["reason"] == "manifest_invalid"


def test_e2_carries_its_own_fidelity_anchor(tmp_path: Path) -> None:
    """E2's tariff error is evidence only if its machinery is validated: the anchor does that."""
    extra = ("--feed-in-wiring", "on", "--feed-in-mode", "explicit")
    code, report, _text = drive(tmp_path, arm="E2", minutes=10, extra=extra)
    assert code == 0, report.get("refusal", report["gates"])
    legs = report["drive"]["legs"]
    assert [(leg["leg"], leg["plant"], leg["policy"]) for leg in legs] == [
        ("recorded_model", "simulated", "production"),
        ("declared_model", "simulated", "production"),
        ("fidelity_anchor", "recorded", "production"),
    ]
    # Gates 1-3 are measured on the anchor, and each says which leg it judged.
    for gate, slug in ((PLANT_FIDELITY, PLANT_FIDELITY), (DECISION_FIDELITY, DECISION_FIDELITY)):
        verdict = gate_named(report, gate)
        assert verdict["passed"] is True, verdict
    assert gate_named(report, DECISION_FIDELITY)["detail"]["reference_leg"] == "fidelity_anchor"
    accounting = gate_named(report, ACCOUNTING_FIDELITY)
    assert accounting["detail"]["reference_leg"] == "fidelity_anchor"
    assert accounting["detail"]["basis_comparand"] == SOURCE_BASIS_COMPARAND
    assert accounting["detail"]["basis_tolerance_eur_per_kwh"] == pytest.approx(0.005)
    # The pair's own verdict stays about the pair, and says which leg the gates judge.
    note = next(
        note for note in report["accounting"]["notes"] if note["code"] == ANCHOR_LEG_IS_THE_FIDELITY_REFERENCE
    )
    assert note["leg"] == "fidelity_anchor"
    assert "is the gate reference" in note["text"]
    assert "neither paired leg has a recorded counterpart" in note["text"]
    # The pair is the two simulated legs, by name: the reference leg must never drift into it.
    assert report["accounting"]["paired"]["legs"] == ["recorded_model", "declared_model"]
    assert report["run"]["legs"] == 3, "one arm, three legs: stated, not hidden"
    anchor = report["accounting"]["legs"][2]
    assert anchor["leg"] == "fidelity_anchor" and anchor["booked_basis"]


def test_a_leg_cannot_read_another_legs_scratch_copy(tmp_path: Path) -> None:
    """Every leg gets its own scratch copy: an arm's numbers must not depend on its other legs.

    The engine writes plans, actuals, measurements, dispatch rows and the cost-basis ledger while it
    runs, so one shared scratch copy would leave the earlier legs' rows inside the reference leg's
    own episode -- its journal, its ledger and its gates would all be somebody else's too.
    """
    # A stale vintage makes the replay actually replan, so every leg journals its own plan_updated.
    source = recording_path(tmp_path, fresh_vintage=False)
    _, single, _ = drive(tmp_path, arm="E1", source=source)
    _, pair, _ = drive(
        tmp_path,
        arm="E2",
        source=source,
        extra=("--setting", "price.feed_in_eur_per_kwh=0.13"),
    )
    anchor = next(leg for leg in pair["accounting"]["legs"] if leg["leg"] == "fidelity_anchor")
    lone = single["accounting"]["legs"][0]
    assert anchor["booked_basis"] == lone["booked_basis"], (
        "the anchor's ledger must start where the single-leg run's does"
    )
    assert anchor["profit_eur"] == pytest.approx(lone["profit_eur"])
    # The discriminating check: on a shared scratch copy the two legs in front of the anchor would
    # still be journalled in its episode (three plan_updated events instead of its own one).
    assert pair["drive"]["legs"][2]["events"] == single["drive"]["legs"][0]["events"]
    assert pair["drive"]["legs"][2]["steps"] == single["drive"]["legs"][0]["steps"]


def test_the_contract_verdict_comes_from_the_refusal_evidence() -> None:
    """A derived verdict, so a claim can never contradict the counters next to it."""
    assert contract_verdict(5, 0) == "resolved"
    assert contract_verdict(5, 3) == "partial_refusal"
    assert contract_verdict(0, 3) == "refuses_export"
    assert contract_verdict(0, 0) == "unknown"


def test_e4_labels_its_null_pair_as_an_identity(tmp_path: Path) -> None:
    """The recording WAS production, so E4's zero is arithmetic -- never a measured policy gain."""
    code, report, _text = drive(tmp_path, arm="E4", minutes=10)
    assert code == 0, report.get("refusal", report["gates"])
    accounting = report["accounting"]
    paired, bootstrap = accounting["paired"], accounting["bootstrap"]
    assert paired["delta_eur"] == 0.0 and paired["by_construction"] is True
    assert paired["null_reason"] == "recorded_policy_equals_production"
    assert bootstrap["by_construction"] is True
    assert bootstrap["null_reason"] == "recorded_policy_equals_production"
    assert "recorded_policy_equals_production" in accounting["evidence_labels"]
    assert "saa_not_shipped" in accounting["evidence_labels"], "the SAA substitute is labelled"
    note = next(
        note
        for note in accounting["notes"]
        if note["code"] == "recorded_policy_equals_production"
    )
    assert "by construction" in note["text"] and "not a measured policy gain" in note["text"]
    assert "saa_not_shipped" in [item["slug"] for item in report["limits"]["declared"]]
    # E4 pairs the production policy against the recorded one, by name, never its reference leg.
    assert accounting["paired"]["legs"] == ["recorded_policy", "policy"]
    assert report["run"]["legs"] == 4
    # The reference leg makes gates 2 and 3 measured in this arm's own report.
    assert gate_named(report, DECISION_FIDELITY)["detail"]["reference_leg"] == "fidelity_anchor"
    accounting_gate = gate_named(report, ACCOUNTING_FIDELITY)
    assert accounting_gate["detail"]["reference_leg"] == "fidelity_anchor"
    assert accounting_gate["detail"]["basis_comparand"] == SOURCE_BASIS_COMPARAND
    # The interval is judged against the plan horizon: a 10-minute window cannot speak for it.
    assert bootstrap["block_length_horizon_windows"] > bootstrap["samples"]
    assert bootstrap["underpowered"] is True and bootstrap["publishable"] is False
    assert bootstrap["minimum_episode_hours"] is not None
    assert "illustrative_interval" in accounting["evidence_labels"]
    assert bootstrap["evidence_requirement"]


def test_the_plan_horizon_is_measured_from_the_plan_not_a_setting() -> None:
    """The publishability check's horizon: the recording's own plan span, never a guess.

    A settings row is written only when a value changes, so ``optimizer.horizon_hours`` is usually
    absent from a recording even though every plan ran with one -- reading the frozen settings dict
    alone left the block length unjudgeable (``horizon_unknown``) and the check could never pass.
    The plan's own span is the horizon the engine actually planned over, and every answer carries
    its provenance: a reader must be able to tell a declared horizon from a missing one.
    """
    settings = {"optimizer.horizon_hours": 12}
    plan = SimpleNamespace(
        horizon_start=datetime(2026, 10, 16, 11, 43, 57, tzinfo=timezone.utc),
        horizon_end=datetime(2026, 10, 17, 17, 43, 57, tzinfo=timezone.utc),
    )
    episode = SimpleNamespace(warm_start=SimpleNamespace(plan=plan), settings_values=settings)
    assert cli._plan_horizon_hours(episode) == (pytest.approx(30.0), HORIZON_FROM_PLAN_SPAN)
    # Without a plan the recording's own setting answers, and the provenance says so.
    no_plan = SimpleNamespace(warm_start=SimpleNamespace(plan=None), settings_values=settings)
    assert cli._plan_horizon_hours(no_plan) == (pytest.approx(12.0), HORIZON_FROM_SETTING)
    assert cli._plan_horizon_hours(
        SimpleNamespace(warm_start=SimpleNamespace(plan=None), settings_values={})
    ) == (None, HORIZON_SOURCE_UNKNOWN), "the engine's 30 h default is not the recording's own"
    # A zero-length plan span is not a horizon, so the setting answers instead.
    flat = SimpleNamespace(horizon_start=plan.horizon_start, horizon_end=plan.horizon_start)
    assert cli._plan_horizon_hours(
        SimpleNamespace(warm_start=SimpleNamespace(plan=flat), settings_values=settings)
    ) == (pytest.approx(12.0), HORIZON_FROM_SETTING)


def test_finding_6_an_unknown_horizon_publishes_no_implied_length_claim() -> None:
    """A recording that states no horizon may not have one implied for it, provenance and all."""
    unknown = block_bootstrap(
        [1.0, 2.0, 3.0],
        blocks=2,
        resamples=50,
        seed=1,
        horizon_hours=None,
        horizon_source=HORIZON_SOURCE_UNKNOWN,
        window_seconds=300.0,
    )
    assert unknown.publishable is False and unknown.block_length_horizon_windows is None
    assert unknown.reason == HORIZON_UNKNOWN
    assert unknown.horizon_source == HORIZON_SOURCE_UNKNOWN
    assert "neither a plan span nor an optimizer.horizon_hours setting" in unknown.note
    payload = _bootstrap_dict(unknown)
    assert payload["horizon_hours"] is None and payload["horizon_source"] == HORIZON_SOURCE_UNKNOWN
    assert payload["block_length_horizon_windows"] is None
    assert payload["gap_windows"] == [] and payload["windows_contiguous"] is True
    # The same sample judged against a horizon the recording does state is publishable, and its
    # provenance is carried into the report rather than left for the reader to infer.
    stated = block_bootstrap(
        [1.0, 2.0, 3.0],
        blocks=2,
        resamples=50,
        seed=1,
        horizon_hours=5.0,
        horizon_source=HORIZON_FROM_PLAN_SPAN,
        window_seconds=3600.0,
    )
    assert stated.horizon_source == HORIZON_FROM_PLAN_SPAN
    assert _bootstrap_dict(stated)["horizon_source"] == HORIZON_FROM_PLAN_SPAN
    assert _bootstrap_dict(stated)["block_length_horizon_windows"] == 5


def test_gate_three_checks_the_recordings_booked_feed_in_price(tmp_path: Path) -> None:
    """The recording's own tariff columns must follow the model, or the money is withheld."""
    path = recording_path(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute("UPDATE actuals SET price_sell = 0.05 WHERE price_buy IS NOT NULL")
    connection.commit()
    connection.close()
    code, report, _text = drive(tmp_path, arm="E1", source=path)
    assert code == 2, "the fixture books sell = 0.05 against buy = 0.25"
    gate = gate_named(report, ACCOUNTING_FIDELITY)
    assert gate["passed"] is False
    assert gate["detail"]["tariff_comparand"] == RECORDING_SELF_CONSISTENCY
    assert gate["detail"]["tariff_model"] == TARIFF_MODEL_DERIVED
    assert gate["detail"]["worst_tariff_eur_per_kwh"] == pytest.approx(0.20, abs=1e-6)
    assert gate["detail"]["tariff_tolerance_eur_per_kwh"] == pytest.approx(0.0005)
    withheld = gate["detail"]["tariff_windows_withheld"]
    assert withheld == [0], "the fixture window is one decision window"
    assert [finding["kind"] for finding in gate["findings"]] == ["export_revenue_unverified"]
    leg = report["accounting"]["legs"][0]
    assert leg["tariff"]["comparand"] == RECORDING_SELF_CONSISTENCY
    assert leg["tariff"]["diverged_windows"] == withheld
    assert leg["revenue_eur"] is None, "no window could be settled on the assumed tariff"


# --------------------------------------------------------------- review findings 4, 5, 10 and 15


def test_finding_4_gate_one_is_not_measurable_without_a_reference_divergence(tmp_path: Path) -> None:
    """One window with no measurable reference makes gate 1 unmeasurable, not a pass."""
    from dataclasses import replace

    from app.replay.report import REFERENCE_DIVERGENCE_UNKNOWN

    leg = one_leg(tmp_path)
    holed = replace(
        leg,
        account=replace(
            leg.account,
            windows=tuple(
                replace(window, divergence_kwh=None) if window.index == 0 else window
                for window in leg.account.windows
            ),
        ),
    )
    verdict = gates_for(_arm_stub("E1"), [holed], tolerances=_tolerances(), isolation=None)[0]
    assert verdict.gate == PLANT_FIDELITY
    assert verdict.passed is None and verdict.measured is False
    assert verdict.reasons == (REFERENCE_DIVERGENCE_UNKNOWN,)
    assert any(finding.kind == REFERENCE_DIVERGENCE_UNKNOWN for finding in verdict.findings)


def test_finding_5_a_withheld_window_pair_renders_its_delta_as_null(tmp_path: Path) -> None:
    """A pair with no included window prints null, never a 0.0 that reads like a measured zero."""
    from app.replay.accounting import paired_delta
    from app.replay.manifest import PlantCeiling, PlantLimits
    from app.replay.report import _paired_dict

    path = recording_path(tmp_path)
    limits = default_plant_limits()
    legs = []
    for ceiling in (None, PlantCeiling(0, 0, 0)):
        with open_episode(str(path), START, START + timedelta(minutes=WINDOW_MINUTES), 1.0) as episode:
            result = run_leg(episode, name="replay", plant="recorded")
            legs.append(
                LegRun(
                    result=result,
                    account=account_leg(
                        result,
                        limits=(
                            limits
                            if ceiling is None
                            else PlantLimits(recorded=ceiling, simulated=limits.simulated)
                        ),
                        tariff_tolerance_eur_per_kwh=0.0005,
                    ),
                )
            )
    pair = paired_delta(legs[0].account, legs[1].account, change="withheld")
    rendered = _paired_dict(pair, (legs[0], legs[1]))
    assert rendered["delta_eur"] is None and rendered["delta_withheld"] is True
    assert rendered["delta_with_terminal_eur"] is None
    assert rendered["included_windows"] == 0 and rendered["withheld_windows"]
    text = toon({"accounting": {"paired": rendered}})
    assert "delta_eur: null" in text


def test_finding_14b_the_pair_section_names_the_windows_that_break_contiguity() -> None:
    """The interval's contiguity verdict must be checkable against the pair's own window list."""
    from app.replay.accounting import PLANT_CEILING, PairedDelta, PairedWindow
    from app.replay.report import _paired_dict

    def windows(*, withheld: tuple[int, ...]) -> tuple:
        return tuple(
            PairedWindow(
                index=index,
                start=START + timedelta(minutes=15 * index),
                end=START + timedelta(minutes=15 * (index + 1)),
                left_profit_eur=0.0,
                right_profit_eur=None if index in withheld else 1.0,
                delta_eur=None if index in withheld else 1.0,
                withheld=index in withheld,
                reasons=(PLANT_CEILING,) if index in withheld else (),
            )
            for index in range(5)
        )

    holed_windows = windows(withheld=(2,))
    holed = PairedDelta(
        change="policies",
        windows=holed_windows,
        delta_eur=4.0,
        delta_with_terminal_eur=None,
        terminal_delta_eur=None,
        withheld_windows=(2,),
        included_windows=4,
    )
    rendered = _paired_dict(holed)
    assert rendered["gap_windows"] == [3], (
        "window 3 starts the run after the withheld window 2, so it is the named gap"
    )
    assert rendered["windows_contiguous"] is False
    assert rendered["withheld_windows"] == [2]
    clean = PairedDelta(
        change="policies",
        windows=windows(withheld=()),
        delta_eur=5.0,
        delta_with_terminal_eur=None,
        terminal_delta_eur=None,
        withheld_windows=(),
        included_windows=5,
    )
    assert _paired_dict(clean)["gap_windows"] == []
    assert _paired_dict(clean)["windows_contiguous"] is True
    # The interval's own total is nulled by the exclusion, while the rate it describes stays put.
    from app.replay.statistics import bootstrap_paired

    payload = _bootstrap_dict(
        bootstrap_paired(holed, blocks=2, resamples=25, seed=1, window_seconds=900.0)
    )
    assert payload["excluded_windows"] == 1 and payload["windows_total"] == 5
    assert payload["mean_eur"] is not None
    assert payload["total_eur"] is None, (
        "the sum of the four included windows is a partial total, not this arm's total"
    )
    assert payload["gap_windows"] == [3] and payload["windows_contiguous"] is False


def test_finding_10_every_legs_copy_is_fingerprinted(tmp_path: Path, monkeypatch) -> None:
    """A drift that only shows up on a later leg's copy is a refusal, not a silent rerun."""
    from app.replay.manifest import FINGERPRINT_MISMATCH

    real = cli.episode_fingerprint
    calls = {"count": 0}

    def drifting(episode):
        calls["count"] += 1
        fingerprint = real(episode)
        if calls["count"] >= 2:
            return {**fingerprint, "digest": "0" * 64}
        return fingerprint

    monkeypatch.setattr(cli, "episode_fingerprint", drifting)
    code, report, _text = drive(tmp_path, arm="E4", minutes=10)
    assert code == 2
    assert report["refusal"]["reason"] == FINGERPRINT_MISMATCH
    assert "recorded_policy" in report["refusal"]["message"]
    assert calls["count"] == 2, "the second leg's copy is where the drift appeared"


def test_finding_15_an_unmeasured_gate_withholds_the_paired_money(tmp_path: Path) -> None:
    """A gate that never ran is not a passed gate: the deltas are withheld and the gate named."""
    from app.replay.accounting import paired_delta
    from app.replay.manifest import build_manifest
    from app.replay.report import GateVerdict, REFERENCE_DIVERGENCE_UNKNOWN, build_report
    from app.replay.statistics import bootstrap_paired

    path = recording_path(tmp_path)
    with open_episode(str(path), START, START + timedelta(minutes=WINDOW_MINUTES), 1.0) as episode:
        manifest = build_manifest(episode, arms=["E1"])
        result = run_leg(episode, name="replay", plant="recorded")
        leg = LegRun(result=result, account=account_leg(result, limits=default_plant_limits()))
        twin = LegRun(
            result=result,
            account=account_leg(result, limits=default_plant_limits()),
        )
        pair = paired_delta(leg.account, twin.account, change="twin")
        bootstrap = bootstrap_paired(
            pair, blocks=2, resamples=10, seed=0, horizon_hours=None
        )
        assert pair.delta_eur == pytest.approx(0.0), "the pair itself is a real zero"
        report = build_report(
            manifest=manifest,
            arm=_arm_stub("E1"),
            legs=[leg, twin],
            paired=pair,
            bootstrap=bootstrap,
            isolation=None,
            gates=[
                GateVerdict(
                    gate=PLANT_FIDELITY,
                    passed=None,
                    reasons=(REFERENCE_DIVERGENCE_UNKNOWN,),
                )
            ],
            runtime_s=1.0,
        )
    assert report["limits"]["status"] == "gates_not_measured"
    assert report["limits"]["withheld"] == PLANT_FIDELITY
    assert report["accounting"]["withheld_by_gate"] == PLANT_FIDELITY
    assert report["accounting"]["paired"] is None
    assert report["accounting"]["bootstrap"] is None
