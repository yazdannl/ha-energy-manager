"""The replay manifest: the contract fixed before a run (docs/REPLAY_HARNESS.md sections 1, 5, 6).

The manifest is what makes gate 3 meaningful: tolerances and the plant ceilings are declared
before the first step, and a manifest that does not match the recording (drifted frozen state,
different arms, different window, edited numbers) is refused instead of quietly reused.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import asdict
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "energy_manager"))

from app.main import EXAMPLE_BATTERY, _selfcheck_settings  # noqa: E402
from app.models import Action, Measurement, Plan, PlanSlot, PlanSummary, quarter_floor, to_iso  # noqa: E402
from app.replay.episode import open_episode  # noqa: E402
from app.replay.manifest import (  # noqa: E402
    ARM_SPEC_CHANGED,
    ARM_UNKNOWN,
    ARM_UNSUPPORTED,
    FINGERPRINT_MISMATCH,
    MANIFEST_INVALID,
    MANIFEST_MISMATCH,
    MANIFEST_VERSION,
    PLANT_LIMIT_INVALID,
    TOLERANCE_INVALID,
    ManifestError,
    PlantCeiling,
    PlantLimits,
    Tolerances,
    build_manifest,
    digest_of,
    differing_components,
    episode_fingerprint,
    load_manifest,
    _content_order,
)
from app.store import Store  # noqa: E402

START = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
END = START + timedelta(hours=1)
BATTERY = EXAMPLE_BATTERY


def recording(
    tmp_path: Path,
    *,
    name: str = "recording.sqlite",
    load_kw: float = 1.2,
    settings_extra: dict | None = None,
) -> Path:
    """A one-hour recording: complete 5-minute actuals buckets, battery samples and one plan."""
    values = _selfcheck_settings().all()
    values.update(
        {
            "mode": "auto",
            "dry_run": False,
            "control.grid_follow": False,
            "batteries": [asdict(BATTERY)],
        }
    )
    values.update(settings_extra or {})
    path = tmp_path / name
    store = Store(path)
    try:
        store.set_settings(values, now=START - timedelta(hours=2))
        measurements = []
        moment = quarter_floor(START, 5)
        while moment < END:
            store.upsert_actuals(
                to_iso(moment),
                pv_kw=1.0,
                load_kw=load_kw,
                grid_kw=load_kw - 1.0,
                battery_kw=0.0,
                soc_kwh=2.75,
                price_buy=0.25,
                price_sell=0.05,
            )
            measurements.append(
                Measurement(ts=moment, role=f"battery_soc:{BATTERY.id}", value=55.0, unit="%")
            )
            measurements.append(
                Measurement(ts=moment, role=f"battery_power:{BATTERY.id}", value=0.0, unit="W")
            )
            moment += timedelta(minutes=5)
        store.add_measurements(measurements)
        store.save_plan(
            Plan(
                generated_at=START - timedelta(minutes=30),
                horizon_start=START,
                horizon_end=END,
                slot_minutes=15,
                summary=PlanSummary(),
                batteries=[{"id": BATTERY.id, "share_w": 0}],
                slots=[
                    PlanSlot(
                        t=START + timedelta(minutes=15 * index),
                        buy=0.25,
                        sell=0.05,
                        pv_kw=1.0,
                        load_kw=load_kw,
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
        )
    finally:
        store.close()
    return path


def an_episode(tmp_path: Path, **kwargs):
    return open_episode(recording(tmp_path, **kwargs), START, END, 1.0)


# ---------------------------------------------------------------------------- the frozen contract


def test_build_freezes_the_window_the_arms_and_the_fingerprint(tmp_path: Path) -> None:
    with an_episode(tmp_path) as episode:
        manifest = build_manifest(episode, arms=["E1"])
    assert manifest.version == MANIFEST_VERSION
    assert (manifest.start, manifest.end) == (START, END)
    assert manifest.step_s == 1.0
    assert manifest.arm_names() == ("E1",)
    assert manifest.arm("E1").changes == "none"
    assert manifest.arm("E1").legs[0].plant == "recorded"
    assert manifest.seed == 0 and manifest.blocks == 4 and manifest.resamples == 2000
    assert manifest.valuation["headline"] == "cost_basis_window_start"
    assert manifest.valuation["sensitivity"] == "ledger_booked"
    assert [item["slug"] for item in manifest.limitations] == [
        "saa_not_shipped",
        "toon_subset",
        "forecast_archive_truncated",
    ]
    assert len(manifest.digest()) == 64
    components = manifest.fingerprint["components"]
    for name in ("settings", "profiles", "batteries", "initial_batteries", "warm_start", "series"):
        assert components.get(name), name


def test_tolerances_default_to_the_recordings_own_planner_grid(tmp_path: Path) -> None:
    with an_episode(tmp_path) as episode:
        manifest = build_manifest(episode, arms=["E1"])
    # ``optimizer.slot_minutes`` defaults to 60 in this recording, so one planner grid step is 1 h.
    assert manifest.tolerances.grid_step_s == 3600.0
    assert manifest.tolerances.energy_kwh == 0.5
    assert manifest.tolerances.basis_eur_per_kwh == 0.005
    assert manifest.plant_limits.recorded.unverified_bounds is None, (
        "the recorded arm never evaluates the site export bound: declared, not counted"
    )
    assert manifest.plant_limits.simulated.unverified_bounds == 0


def test_declared_tolerances_are_kept_and_change_the_contract_digest(tmp_path: Path) -> None:
    declared = Tolerances(grid_step_s=900.0, energy_kwh=0.25, basis_eur_per_kwh=0.001)
    with an_episode(tmp_path) as episode:
        default = build_manifest(episode, arms=["E1"])
        explicit = build_manifest(episode, arms=["E1"], tolerances=declared)
    assert explicit.tolerances == declared
    assert explicit.digest() != default.digest(), "tolerances are part of the pre-run contract"
    assert explicit.fingerprint == default.fingerprint, "tolerances are not recorded state"


def test_invalid_tolerances_are_refused(tmp_path: Path) -> None:
    with an_episode(tmp_path) as episode:
        with pytest.raises(ManifestError) as zero:
            build_manifest(episode, arms=["E1"], tolerances=Tolerances(0.0, 0.5, 0.005))
        assert zero.value.reason == TOLERANCE_INVALID
        with pytest.raises(ManifestError) as negative:
            build_manifest(episode, arms=["E1"], tolerances=Tolerances(900.0, -1.0, 0.005))
        assert negative.value.reason == TOLERANCE_INVALID


def test_plant_ceilings_must_be_declared_sane(tmp_path: Path) -> None:
    with an_episode(tmp_path) as episode:
        with pytest.raises(ManifestError) as negative:
            build_manifest(
                episode,
                arms=["E1"],
                plant_limits=PlantLimits(
                    recorded=PlantCeiling(-1, 0, None), simulated=PlantCeiling(0, 0, 0)
                ),
            )
        assert negative.value.reason == PLANT_LIMIT_INVALID
        with pytest.raises(ManifestError) as null:
            build_manifest(
                episode,
                arms=["E1"],
                plant_limits=PlantLimits(
                    recorded=PlantCeiling(0, 0, None), simulated=PlantCeiling(0, 0, None)
                ),
            )
        assert null.value.reason == PLANT_LIMIT_INVALID, (
            "only the recorded arm may declare the export bound as not applicable"
        )
        allowed = build_manifest(
            episode,
            arms=["E1"],
            plant_limits=PlantLimits(
                recorded=PlantCeiling(0, 1, None), simulated=PlantCeiling(2, 3, 4)
            ),
        )
        assert allowed.plant_limits.simulated.unverified_bounds == 4


# ------------------------------------------------------------------------------------- the arms


def test_e2_needs_a_declared_contract(tmp_path: Path) -> None:
    with an_episode(tmp_path) as episode:
        with pytest.raises(ManifestError) as missing:
            build_manifest(episode, arms=["E2"])
        assert missing.value.reason == ARM_UNSUPPORTED
        with pytest.raises(ManifestError) as derived:
            build_manifest(
                episode,
                arms=["E2"],
                contract={"optimizer.feed_in_wiring": "on", "price.feed_in_mode": "derived"},
            )
        assert derived.value.reason == ARM_UNSUPPORTED
        with pytest.raises(ManifestError) as unknown_key:
            build_manifest(
                episode,
                arms=["E2"],
                contract={
                    "optimizer.feed_in_wiring": "on",
                    "price.feed_in_mode": "explicit",
                    "nonsense.key": 1,
                },
            )
        assert unknown_key.value.reason == MANIFEST_INVALID


def test_e2_pairs_the_recorded_model_with_the_declared_one(tmp_path: Path) -> None:
    contract = {"optimizer.feed_in_wiring": "on", "price.feed_in_mode": "explicit"}
    path = tmp_path / "e2.json"
    with an_episode(tmp_path) as episode:
        manifest = build_manifest(episode, arms=["E2"], contract=contract)
        manifest.write(path)
        loaded = load_manifest(path, episode=episode, arms=["E2"])
    legs = loaded.arm("E2").legs
    assert [leg.name for leg in legs] == ["recorded_model", "declared_model", "fidelity_anchor"]
    assert legs[0].patch == ()
    assert dict(legs[1].patch) == {"optimizer.feed_in_wiring": "on", "price.feed_in_mode": "explicit"}
    # The third leg is the pair's own fidelity anchor: E1's leg, so gates 1 and 3 are measured in
    # this arm's report instead of borrowed from another arm's run.  It carries no patch, or the
    # anchor would test a different contract than the recording it is compared against.
    assert legs[2].patch == ()
    assert (legs[2].plant, legs[2].policy) == ("recorded", "production")
    assert loaded.arm("E2").changes == "tariff_model"
    # Section 4 needs the profit of every arm to come from that arm's OWN flows, and the recorded
    # plant ignores commands, so both *paired* legs must run the simulated plant or the paired delta
    # is zero by construction (the tariff error would have no realized cost at all).  The third leg
    # is the recorded-plant reference the gates judge, never a pair member.
    assert [leg.plant for leg in legs] == ["simulated", "simulated", "recorded"]
    assert [leg.policy for leg in legs[:2]] == ["production", "production"]
    holds = loaded.arm("E2").holds
    for constant in ("plant", "plant_physics", "initial_state", "vintages", "policy"):
        assert constant in holds, f"E2 must declare {constant} as held constant across the pair"


def test_e4_declares_the_recorded_leg_and_its_isolation_cross_check(tmp_path: Path) -> None:
    with an_episode(tmp_path) as episode:
        manifest = build_manifest(episode, arms=["E4"])
    legs = manifest.arm("E4").legs
    assert [leg.name for leg in legs] == [
        "policy",
        "recorded_policy",
        "recorded_policy_recorded_plant",
        "fidelity_anchor",
    ]
    assert [leg.plant for leg in legs] == ["simulated", "simulated", "recorded", "recorded"]
    assert [leg.policy for leg in legs] == ["production", "recorded", "recorded", "production"]
    assert [leg.isolate for leg in legs] == [False, False, True, False], (
        "the plant-model error must be measurable as its own leg, not absorbed into the delta"
    )
    # The last leg is E1's shape: the reference gates 1-3 are defined against, and never a pair
    # member (E4's pair is legs[1] vs legs[0]).
    assert legs[3].patch == () and legs[3].plant == "recorded" and legs[3].policy == "production"


def test_an_unknown_arm_is_refused(tmp_path: Path) -> None:
    with an_episode(tmp_path) as episode:
        with pytest.raises(ManifestError) as caught:
            build_manifest(episode, arms=["E9"])
        assert caught.value.reason == ARM_UNKNOWN
        manifest = build_manifest(episode, arms=["E1"])
        with pytest.raises(ManifestError) as missing:
            manifest.arm("E4")
        assert missing.value.reason == ARM_UNKNOWN


# -------------------------------------------------------------------- freezing, writing, loading


def test_the_fingerprint_ignores_the_scratch_copy(tmp_path: Path) -> None:
    source = recording(tmp_path)
    with open_episode(source, START, END, 1.0) as first, open_episode(
        source, START, END, 1.0
    ) as second:
        left = build_manifest(first, arms=["E1"])
        right = build_manifest(second, arms=["E1"])
    assert left.fingerprint == right.fingerprint
    assert left.digest() == right.digest(), "the same recording must freeze to the same contract"


def test_a_changed_recording_drifts_the_fingerprint(tmp_path: Path) -> None:
    with an_episode(tmp_path, name="a.sqlite", load_kw=1.2) as left, an_episode(
        tmp_path, name="b.sqlite", load_kw=1.3
    ) as right:
        first = build_manifest(left, arms=["E1"])
        second = build_manifest(right, arms=["E1"])
    assert first.fingerprint["digest"] != second.fingerprint["digest"]
    assert "series" in differing_components(first.fingerprint, second.fingerprint)


def test_finding_1_a_same_shape_price_edit_drifts_the_fingerprint(tmp_path: Path) -> None:
    """Row *counts* are not content: a changed recorded price has to drift the frozen boundary."""
    source = recording(tmp_path, name="priced.sqlite")
    connection = sqlite3.connect(source)
    connection.execute("UPDATE actuals SET price_buy = 0.31, price_sell = 0.09")
    connection.commit()
    connection.close()
    with an_episode(tmp_path, name="plain.sqlite") as plain, open_episode(
        source, START, END, 1.0
    ) as edited:
        first = build_manifest(plain, arms=["E1"])
        second = build_manifest(edited, arms=["E1"])
    assert first.fingerprint["sizes"]["boundary"] == second.fingerprint["sizes"]["boundary"]
    assert first.fingerprint["digest"] != second.fingerprint["digest"]
    assert "boundary" in differing_components(first.fingerprint, second.fingerprint)


def test_finding_1_the_window_start_valuation_is_inside_the_fingerprint(
    tmp_path: Path, monkeypatch
) -> None:
    """The derived basis is money the terminal term is valued on: it is part of the contract."""
    from app.replay import accounting as accounting_module

    with an_episode(tmp_path) as episode:
        with monkeypatch.context() as patch:
            patch.setattr(accounting_module, "update_cost_basis", lambda *_a, **_k: None)
            unknown = build_manifest(episode, arms=["E1"])
        with monkeypatch.context() as patch:
            patch.setattr(
                accounting_module,
                "update_cost_basis",
                lambda *_a, **_k: {"basis_eur_per_kwh": 0.31},
            )
            valued = build_manifest(episode, arms=["E1"])
    drifted = differing_components(unknown.fingerprint, valued.fingerprint)
    assert drifted == ("window_start_valuation",), (
        "a change in the derived basis is attributed by name, not as boundary drift"
    )


def backfill_ledger_history(source: Path, *, hours: int = 3) -> None:
    """Pre-window plan prices, dispatch, actuals and samples, so the ledger derivation books.

    The window-start derivation runs the real ``update_basis`` against rows at or before the window
    start; without a plan covering those hours and one applied command, it books nothing and the
    idempotence test would prove nothing.
    """
    store = Store(source)
    try:
        origin = START - timedelta(hours=hours)
        store.save_plan(
            Plan(
                generated_at=origin,
                horizon_start=origin,
                horizon_end=START,
                slot_minutes=15,
                summary=PlanSummary(),
                batteries=[{"id": BATTERY.id, "share_w": 0}],
                slots=[
                    PlanSlot(
                        t=origin + timedelta(minutes=15 * index),
                        buy=0.25,
                        sell=0.25,
                        pv_kw=1.0,
                        load_kw=1.2,
                        soc_kwh=2.0,
                        power_w=1000.0,
                        action=Action.CHARGE,
                        reason="recorded",
                    )
                    for index in range(4 * hours)
                ],
                mode="auto",
                dry_run=False,
            )
        )
        store.add_dispatch(
            ts=origin,
            battery_id=BATTERY.id,
            requested_w=1000.0,
            applied_w=1000.0,
            dry_run=False,
            mode="auto",
            result="ok",
            code="ok",
        )
        measurements = []
        moment = origin
        soc = 2.0
        while moment < START:
            store.upsert_actuals(
                to_iso(moment),
                pv_kw=1.0,
                load_kw=1.2,
                grid_kw=1.2,
                battery_kw=1.0,
                soc_kwh=soc,
                price_buy=0.25,
                price_sell=0.25,
            )
            measurements.append(
                Measurement(ts=moment, role=f"battery_soc:{BATTERY.id}", value=50.0, unit="%")
            )
            measurements.append(
                Measurement(ts=moment, role=f"battery_power:{BATTERY.id}", value=1000.0, unit="W")
            )
            soc += 0.05
            moment += timedelta(minutes=5)
        store.add_measurements(measurements)
    finally:
        store.close()


def test_finding_2_deriving_the_window_start_valuation_is_idempotent(tmp_path: Path) -> None:
    """The derivation writes the copy's ledger; repeated calls at one instant must not drift.

    Fingerprinting derives this declaration (once per manifest build and once per driven leg), so a
    derivation that booked a little more on every call would make the frozen contract depend on how
    many times it was read.  Fixed instant, three calls: same declaration, same ledger bytes.
    """
    from app.replay.accounting import DERIVED_WINDOW_START_BASIS, window_start_valuation

    source = recording(tmp_path)
    backfill_ledger_history(source)
    with open_episode(source, START, END, 1.0) as episode:

        def ledger_row():
            row = episode.store().connection.execute(
                "SELECT kind, key, updated_at, payload_json FROM learning_profiles"
                " WHERE kind = ? AND key = ?",
                ("battery_cost_basis", BATTERY.id),
            ).fetchone()
            return None if row is None else tuple(row)

        declarations = []
        rows = []
        for _ in range(3):
            declarations.append(window_start_valuation(episode))
            rows.append(ledger_row())
    assert declarations[0].source == DERIVED_WINDOW_START_BASIS
    assert declarations[0].basis, "the derivation really booked energy, so this test has teeth"
    assert declarations[0] == declarations[1] == declarations[2]
    assert rows[0] is not None and rows[0][3], "the derivation wrote the ledger on the copy"
    assert rows[0] == rows[1] == rows[2], "the ledger rows it writes must be byte-identical"


def test_finding_2_the_valuation_component_is_not_the_boundary_component(tmp_path: Path) -> None:
    """The window-start valuation is its own fingerprint component, named in every drift report."""
    with an_episode(tmp_path) as episode:
        fingerprint = build_manifest(episode, arms=["E1"]).fingerprint
    assert "window_start_valuation" in fingerprint["components"]
    assert "window_start_valuation" not in fingerprint["components"]["boundary"]


def test_finding_8ii_the_fingerprint_does_not_depend_on_the_reader_row_order(
    tmp_path: Path,
) -> None:
    """The manifest is the contract: a changed query order must not drift a byte-identical one.

    Every row list is hashed in content order, so the same recording read back in a different
    arrival order must produce the same digest -- and, because the digest is per-component, the
    same per-component digests too.
    """
    from dataclasses import replace

    source = recording(tmp_path)
    store = Store(source)
    try:
        for minutes in (5, 10):
            store.add_dispatch(
                ts=START + timedelta(minutes=minutes),
                battery_id=BATTERY.id,
                requested_w=1000.0,
                applied_w=1000.0,
                dry_run=False,
                mode="auto",
                result="ok",
                code="ok",
            )
    finally:
        store.close()
    with open_episode(source, START, END, 1.0) as episode:
        before = episode_fingerprint(episode)
        assert len(episode.dispatch) > 1 and episode.boundary.source_rows["actuals"], (
            "reversing a single row would not test the ordering"
        )
        episode.dispatch = tuple(reversed(episode.dispatch))
        episode.forecasts = tuple(reversed(episode.forecasts))
        episode.plans = tuple(reversed(episode.plans))
        episode.series = {
            name: tuple(reversed(points)) for name, points in episode.series.items()
        }
        episode.battery_samples = {
            battery_id: {name: tuple(reversed(points)) for name, points in by_name.items()}
            for battery_id, by_name in episode.battery_samples.items()
        }
        episode.boundary = replace(
            episode.boundary,
            source_rows={
                table: tuple(reversed(rows))
                for table, rows in episode.boundary.source_rows.items()
            },
        )
        after = episode_fingerprint(episode)
    assert after == before, "rows are hashed in content order, not in the reader's arrival order"
    assert after["components"] == before["components"], (
        "the per-component digests are stable too: only their content is hashed"
    )


def test_write_then_load_round_trips_the_contract(tmp_path: Path) -> None:
    path = tmp_path / "episode.json"
    with an_episode(tmp_path) as episode:
        written = build_manifest(episode, arms=["E1", "E3"])
        digest = written.write(path)
        stored = json.loads(path.read_text(encoding="utf-8"))
        loaded = load_manifest(path, episode=episode, arms=["E1", "E3"])
    assert stored["manifest_digest"] == digest == written.digest()
    assert loaded.digest() == written.digest()
    assert loaded.arm_names() == ("E1", "E3")
    assert loaded.tolerances == written.tolerances
    assert loaded.fingerprint == written.fingerprint
    assert loaded.arm("E3").terminal_pair is True


def test_e3_freezes_the_terminal_pair_and_retires_its_limitation(tmp_path: Path) -> None:
    """E3 is ``salvage`` vs the terminal vector, and the not-shipped disclosure is gone.

    The pair is the two legs that differ only in ``optimizer.terminal_mode``, in baseline-first
    order; E1's own leg travels along as the gate reference, so E3's report can be judged by its own
    gates instead of borrowing another arm's run.
    """
    with an_episode(tmp_path) as episode:
        manifest = build_manifest(episode, arms=["E3"])
    spec = manifest.arm("E3")
    assert spec.changes == "terminal_treatment"
    assert spec.terminal_pair is True
    assert [leg.name for leg in spec.legs] == ["replay", "salvage", "vector"]
    assert [leg.plant for leg in spec.legs] == ["recorded", "simulated", "simulated"]
    assert spec.legs[0].patch_dict() == {}, "the reference leg is E1's, unpatched"
    assert spec.legs[1].patch_dict() == {"optimizer.terminal_mode": "salvage"}
    assert spec.legs[2].patch_dict() == {"optimizer.terminal_mode": "vector"}
    assert "valuation" in spec.holds, "both legs must be valued at the one declared price"
    slugs = [item["slug"] for item in manifest.limitations]
    assert "terminal_vector_not_shipped" not in slugs


def test_load_refuses_drifted_frozen_state(tmp_path: Path) -> None:
    path = tmp_path / "episode.json"
    with an_episode(tmp_path, name="a.sqlite", load_kw=1.2) as first:
        build_manifest(first, arms=["E1"]).write(path)
    with an_episode(tmp_path, name="b.sqlite", load_kw=1.3) as second:
        with pytest.raises(ManifestError) as caught:
            load_manifest(path, episode=second, arms=["E1"])
    assert caught.value.reason == FINGERPRINT_MISMATCH
    assert "series" in str(caught.value)


def test_load_refuses_a_different_arm_set(tmp_path: Path) -> None:
    path = tmp_path / "episode.json"
    with an_episode(tmp_path) as episode:
        build_manifest(episode, arms=["E1"]).write(path)
        with pytest.raises(ManifestError) as caught:
            load_manifest(path, episode=episode, arms=["E2"])
    assert caught.value.reason == MANIFEST_MISMATCH


def test_load_refuses_a_different_window(tmp_path: Path) -> None:
    path = tmp_path / "episode.json"
    source = recording(tmp_path)
    with open_episode(source, START, END, 1.0) as episode:
        build_manifest(episode, arms=["E1"]).write(path)
    with open_episode(source, START, START + timedelta(minutes=30), 1.0) as shorter:
        with pytest.raises(ManifestError) as caught:
            load_manifest(path, episode=shorter, arms=["E1"])
    assert caught.value.reason == MANIFEST_MISMATCH
    assert "window" in str(caught.value)


def test_a_manifest_edited_after_it_was_written_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "episode.json"
    with an_episode(tmp_path) as episode:
        build_manifest(episode, arms=["E1"]).write(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["tolerances"]["energy_kwh"] = 5.0
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ManifestError) as caught:
            load_manifest(path, episode=episode, arms=["E1"])
    assert caught.value.reason == MANIFEST_INVALID
    assert "digest" in str(caught.value)


def test_missing_and_malformed_manifests_are_refused(tmp_path: Path) -> None:
    with an_episode(tmp_path) as episode:
        with pytest.raises(ManifestError) as missing:
            load_manifest(tmp_path / "absent.json", episode=episode, arms=["E1"])
        assert missing.value.reason == MANIFEST_INVALID
        broken = tmp_path / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        with pytest.raises(ManifestError) as malformed:
            load_manifest(broken, episode=episode, arms=["E1"])
        assert malformed.value.reason == MANIFEST_INVALID
        path = tmp_path / "trimmed.json"
        build_manifest(episode, arms=["E1"]).write(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["version"] = "replay-manifest/99"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ManifestError) as version:
            load_manifest(path, episode=episode, arms=["E1"])
        assert version.value.reason == MANIFEST_INVALID


def test_non_finite_recorded_values_cannot_be_fingerprinted() -> None:
    """A non-finite recorded value refuses as bad data, including where the sort key hashes it.

    ``_content_order`` applies ``canonical_json`` as a sort *key* before ``digest_of`` sees the
    value, and a key function runs even for a one-element list -- so a NaN used to escape as a raw
    ``ValueError``, which is outside the refusal handling and reports the run as an unexpected
    error (exit 1) instead of a refusal (exit 2).  SQLite stores a NaN as NULL, so a non-finite
    number cannot arrive from the recording's own columns; it is pinned where a value actually
    enters the fingerprint.
    """
    with pytest.raises(ManifestError) as caught:
        digest_of({"value": float("nan")})
    assert caught.value.reason == MANIFEST_INVALID
    for value in (float("nan"), float("inf"), float("-inf")):
        for rows in ([{"ts": "2026-10-16T11:43:57+00:00", "grid_kw": value}], [{"b": 1}, {"a": value}]):
            with pytest.raises(ManifestError) as caught:
                _content_order(rows)
            assert caught.value.reason == MANIFEST_INVALID
            assert "cannot fingerprint recorded data" in str(caught.value)
    assert _content_order([{"b": 1}, {"a": 2}]) == [{"a": 2}, {"b": 1}], "content order, not query order"


def test_the_declared_contract_can_carry_the_contracts_money(tmp_path: Path) -> None:
    """The declared model is only meaningful with its rates: every key goes through the same coercion."""
    contract = {
        "optimizer.feed_in_wiring": "on",
        "price.feed_in_mode": "explicit",
        "price.feed_in_eur_per_kwh": "0.065",
        "price.export_fee_eur_per_kwh": 0.0,
        "price.energy_tax_eur_per_kwh": 0.02,
        "price.energy_tax_confirmed": True,
    }
    with an_episode(tmp_path) as episode:
        manifest = build_manifest(episode, arms=["E2"], contract=contract)
    patch = dict(manifest.arm("E2").legs[1].patch)
    assert patch["price.feed_in_eur_per_kwh"] == pytest.approx(0.065), "coerced, not the string"
    assert patch["price.energy_tax_eur_per_kwh"] == pytest.approx(0.02)
    assert patch["price.energy_tax_confirmed"] is True
    assert manifest.arm("E2").legs[0].patch == (), "only the declared side carries the money"


def test_a_rerun_has_to_declare_the_same_contract(tmp_path: Path) -> None:
    """A different declared contract is a different experiment, so loading it back refuses."""
    contract = {
        "optimizer.feed_in_wiring": "on",
        "price.feed_in_mode": "explicit",
        "price.feed_in_eur_per_kwh": 0.065,
    }
    path = tmp_path / "e2.json"
    with an_episode(tmp_path) as episode:
        build_manifest(episode, arms=["E2"], contract=contract).write(path)
        same = load_manifest(path, episode=episode, arms=["E2"], contract=contract)
        with pytest.raises(ManifestError) as changed:
            load_manifest(
                path,
                episode=episode,
                arms=["E2"],
                contract={**contract, "price.feed_in_eur_per_kwh": 0.09},
            )
    assert dict(same.arm("E2").legs[1].patch)["price.feed_in_eur_per_kwh"] == pytest.approx(0.065)
    assert changed.value.reason == MANIFEST_MISMATCH
    assert "price.feed_in_eur_per_kwh" in str(changed.value)


def test_the_feed_in_price_tolerance_is_declared_with_the_others(tmp_path: Path) -> None:
    """The recording's booked sell price is checked against the model, so its tolerance is pinned."""
    with an_episode(tmp_path) as episode:
        manifest = build_manifest(episode, arms=["E1"])
        payload = manifest.payload()
    assert manifest.tolerances.price_eur_per_kwh == pytest.approx(0.0005)
    assert payload["tolerances"]["price_eur_per_kwh"] == pytest.approx(0.0005), "inside the digest"
    assert manifest.digest() == digest_of(payload)
    with pytest.raises(ManifestError) as caught:
        build_manifest(
            episode, arms=["E1"], tolerances=Tolerances(3600.0, 0.5, 0.005, True)
        )
    assert caught.value.reason == TOLERANCE_INVALID


def test_a_contract_written_before_a_tolerance_existed_refuses(tmp_path: Path) -> None:
    """Filling the gap with a default would silently weaken the gate the tolerance pins."""
    path = tmp_path / "old.json"
    with an_episode(tmp_path) as episode:
        manifest = build_manifest(episode, arms=["E1"])
        payload = manifest.payload()
        payload["tolerances"].pop("price_eur_per_kwh")
        path.write_text(json.dumps(payload))
        with pytest.raises(ManifestError) as caught:
            load_manifest(path, episode=episode, arms=["E1"])
    assert caught.value.reason == MANIFEST_INVALID
    assert "price_eur_per_kwh" in str(caught.value)


def _resealed(path: Path, mutate) -> None:
    """Edit a written manifest and re-seal its digest, so only content checks can catch it."""
    from app.replay.manifest import digest_of

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("manifest_digest", None)
    mutate(payload)
    payload["manifest_digest"] = digest_of(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_finding_7_an_arm_with_a_hand_edited_leg_set_is_refused(tmp_path: Path) -> None:
    """A loaded arm must be the canonical one: E4 without its reference leg measures something else."""
    path = tmp_path / "e4.json"
    with an_episode(tmp_path) as episode:
        build_manifest(episode, arms=["E4"]).write(path)
        _resealed(
            path,
            lambda payload: payload["arms"][0].__setitem__(
                "legs",
                [
                    leg
                    for leg in payload["arms"][0]["legs"]
                    if leg["name"] != "fidelity_anchor"
                ],
            ),
        )
        with pytest.raises(ManifestError) as caught:
            load_manifest(path, episode=episode, arms=["E4"])
    assert caught.value.reason == ARM_SPEC_CHANGED
    assert "canonical" in str(caught.value)


def test_finding_7_an_arm_with_a_hand_edited_patch_is_refused(tmp_path: Path) -> None:
    """E1 declares no patch: one added by hand would silently change what the arm holds fixed."""
    path = tmp_path / "e1.json"
    with an_episode(tmp_path) as episode:
        build_manifest(episode, arms=["E1"]).write(path)
        _resealed(
            path,
            lambda payload: payload["arms"][0]["legs"][0].__setitem__(
                "patch", [["price.feed_in_mode", "explicit"]]
            ),
        )
        with pytest.raises(ManifestError) as caught:
            load_manifest(path, episode=episode, arms=["E1"])
    assert caught.value.reason == ARM_SPEC_CHANGED


def test_finding_7_an_e4_without_its_isolate_leg_is_refused(tmp_path: Path) -> None:
    """The isolate flag is part of the posture cross-check, not a free-form extra."""
    path = tmp_path / "e4.json"
    with an_episode(tmp_path) as episode:
        build_manifest(episode, arms=["E4"]).write(path)
        _resealed(
            path,
            lambda payload: payload["arms"][0]["legs"][2].__setitem__("isolate", False),
        )
        with pytest.raises(ManifestError) as caught:
            load_manifest(path, episode=episode, arms=["E4"])
    assert caught.value.reason == ARM_SPEC_CHANGED


def test_finding_12_a_rerun_count_that_disagrees_with_the_manifest_is_refused(tmp_path: Path) -> None:
    """The frozen manifest is authoritative for its run counts, exactly like its settings."""
    from app.replay.__main__ import _check_requested_counts

    with an_episode(tmp_path) as episode:
        manifest = build_manifest(episode, arms=["E1"], seed=7, blocks=6, resamples=100)
    _check_requested_counts(
        SimpleNamespace(seed=7, blocks=6, resamples=100), manifest
    )  # the frozen numbers are fine
    for name, value in (("seed", 8), ("blocks", 5), ("resamples", 99)):
        args = SimpleNamespace(seed=None, blocks=None, resamples=None)
        setattr(args, name, value)
        with pytest.raises(ManifestError) as caught:
            _check_requested_counts(args, manifest)
        assert caught.value.reason == MANIFEST_MISMATCH, (
            "the manifest is well formed; the caller's request is what disagrees with it"
        )
        assert f"--{name}" in str(caught.value)
    _check_requested_counts(SimpleNamespace(seed=None, blocks=None, resamples=None), manifest)


def test_finding_12_an_external_contract_digest_can_be_required(tmp_path: Path) -> None:
    """The one check an unkeyed self-hash cannot make: against a digest recorded outside the file."""
    from app.replay.__main__ import _check_expected_digest

    with an_episode(tmp_path) as episode:
        manifest = build_manifest(episode, arms=["E1"])
    digest = manifest.digest()
    _check_expected_digest(SimpleNamespace(expect_digest=digest.upper()), manifest)
    _check_expected_digest(SimpleNamespace(expect_digest=None), manifest)
    with pytest.raises(ManifestError) as caught:
        _check_expected_digest(SimpleNamespace(expect_digest="0" * 64), manifest)
    assert caught.value.reason == MANIFEST_MISMATCH
