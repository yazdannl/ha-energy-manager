"""Per-arm accounting: the arm's own flows, the recording's prices, and the withheld windows.

The rules under test are the accepted corrections: price provenance (1), the real-timestamp
integral (2), plant ceilings that withhold a window (3), the terminal term at one declared
valuation, and the window cut that keeps recorded rows outside [start, end) out of an arm's energy.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from dataclasses import replace
from types import SimpleNamespace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "energy_manager"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.models import quarter_floor, to_iso  # noqa: E402
from app.replay import accounting as accounting_module  # noqa: E402
from app.store import MIGRATIONS, Store  # noqa: E402
from app.replay.accounting import (  # noqa: E402
    BUCKET_NOT_VISITED,
    COVERAGE_GAP,
    ENERGY_GAP,
    PLAN_SLOT_FALLBACK,
    PLANT_CEILING,
    PLANT_TALLY_MISSING,
    NO_PRICE,
    PRICE_NULL,
    RECORDED_ACTUALS,
    ROW_ABSENT,
    SOURCE_SCHEMA_UNKNOWN,
    TARIFF_FALLBACK_ONLY,
    DERIVED_WINDOW_START_BASIS,
    EXPORT_REVENUE_UNVERIFIED,
    RECORDING_SELF_CONSISTENCY,
    TARIFF_MODEL_DECLARED,
    TARIFF_MODEL_DERIVED,
    TARIFF_MODEL_UNAVAILABLE,
    FROZEN_PROFILE_BASIS,
    NO_LEDGER_BASIS,
    NO_WINDOW_START_BASIS,
    AccountingError,
    Segment,
    WindowStartValuation,
    account_leg,
    paired_delta,
    recorded_tariff_check,
    window_start_valuation,
)
from app.replay.episode import BASE_MEASURED, open_episode  # noqa: E402
from app.replay.manifest import PlantCeiling, PlantLimits, default_plant_limits  # noqa: E402
from app.replay.plant import RECORDED, SIMULATED  # noqa: E402
from app.replay.runner import LegResult, Runner  # noqa: E402
from app.sources.feed_in import FeedInResult  # noqa: E402
from test_replay_manifest import BATTERY, START, recording  # noqa: E402

BATTERY_ID = getattr(BATTERY, "id", BATTERY)

PLANTS = default_plant_limits()


def a_leg(tmp_path: Path, *, plant: str = RECORDED, minutes: int = 20, source: Path | None = None,
          **runner_kwargs) -> LegResult:
    """One driven leg on the synthetic recording; 20 minutes gives two decision windows."""
    path = source or recording(tmp_path)
    episode = open_episode(path, START, START + timedelta(minutes=minutes), 1.0)
    return Runner(episode, name="leg", plant=plant, **runner_kwargs).run()


def make_the_recording_older(source: Path, *, version: int = 1) -> None:
    """Roll a recording back to an older schema, the way an old recording really looks.

    A recording written before a migration has *both* properties: the columns that migration adds
    are absent and ``schema_version`` predates it.  Dropping a column alone would leave the source
    claiming to be current, which no real recording does, and the scratch copy would then never be
    migrated back to today's columns either -- so the engine's own writes would fail instead of the
    case under test (the copy has today's columns, the recording never did).
    """
    connection = sqlite3.connect(source)
    try:
        for number, statements in MIGRATIONS:
            if number <= version:
                continue
            for statement in statements:
                if not isinstance(statement, str):
                    continue  # a Python migration step adds no column to roll back
                match = re.match(r"\s*ALTER TABLE (\w+) ADD COLUMN (\w+)", statement)
                if match is None:
                    continue
                table, column = match.groups()
                present = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                if column in present:
                    connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        connection.execute("UPDATE schema_version SET version = ?", (int(version),))
        connection.commit()
    finally:
        connection.close()


def add_replan(source: Path, at: datetime) -> None:
    """A second recorded plan vintage inside the window: the leg then has two decision windows."""
    store = Store(source)
    try:
        plan = store.latest_plan()
        store.save_plan(
            replace(
                plan,
                generated_at=at,
                horizon_start=at,
                horizon_end=at + timedelta(hours=1),
                slots=[
                    replace(slot, t=at + timedelta(minutes=15 * index))
                    for index, slot in enumerate(plan.slots)
                ],
            )
        )
    finally:
        store.close()


def book_the_derived_sell_price(source: Path, *, until: datetime) -> None:
    """Book sell = buy on the rows before ``until``, so only the later rows breach the model."""
    connection = sqlite3.connect(source)
    connection.execute("UPDATE actuals SET price_sell = price_buy WHERE ts < ?", (to_iso(until),))
    connection.commit()
    connection.close()


def add_cost_basis(source: Path, basis: float, energy: float) -> None:
    """A frozen ledger row: written two hours before the window start, so it is as-of vintage."""
    connection = sqlite3.connect(source)
    connection.execute(
        "INSERT INTO learning_profiles(kind, key, updated_at, payload_json) VALUES(?, ?, ?, ?)"
        " ON CONFLICT(kind, key) DO UPDATE SET updated_at = excluded.updated_at,"
        " payload_json = excluded.payload_json",
        (
            "battery_cost_basis",
            BATTERY_ID,
            to_iso(START - timedelta(hours=2)),
            json.dumps({"basis_eur_per_kwh": basis, "energy_kwh": energy}),
        ),
    )
    connection.commit()
    connection.close()


# ------------------------------------------------------------------- the integral and its prices


def test_the_headline_integral_is_the_arms_own_flow_at_the_recordings_prices(tmp_path: Path) -> None:
    leg = a_leg(tmp_path)
    account = account_leg(leg, limits=PLANTS)

    # 0.2 kW of import for the whole 20 minutes, at the recording's 0.25 EUR/kWh buy price.
    assert account.import_kwh == pytest.approx(0.2 * 20 / 60)
    assert account.export_kwh == pytest.approx(0.0)
    assert account.cost_eur == pytest.approx(0.2 * 20 / 60 * 0.25)
    assert account.revenue_eur == pytest.approx(0.0)
    assert account.profit_eur == pytest.approx(-account.cost_eur)
    assert account.profit_eur == pytest.approx(account.revenue_eur - account.cost_eur)
    assert account.windows_unknown == 0
    assert account.withheld_windows == ()
    # The window is the recording's own decision cadence: its single plan vintage opens one window
    # that runs to the end of the episode.
    assert [window.index for window in account.windows] == [0]
    window = account.windows[0]
    assert (window.start, window.end) == (leg.start, leg.end)
    assert window.import_kwh == pytest.approx(account.import_kwh)
    assert window.price_fallbacks == 0
    assert window.tariff_divergence_segments == 0
    assert window.gaps == ()
    assert all(piece.buy_source == RECORDED_ACTUALS for piece in window.segments)
    assert all(piece.sell_source == RECORDED_ACTUALS for piece in window.segments)
    assert len(window.segments) == 4, "one segment per settlement bucket"
    # The recorded arm's own flow is the recording's: gate 1's divergence is exactly zero.
    assert account.reference_divergence_kwh == pytest.approx(0.0, abs=1e-9)


def test_the_recording_is_a_reference_and_never_the_simulated_arms_flow(tmp_path: Path) -> None:
    source = recording(tmp_path)
    recorded = account_leg(a_leg(tmp_path, plant=RECORDED, source=source), limits=PLANTS)
    simulated = account_leg(a_leg(tmp_path, plant=SIMULATED, source=source), limits=PLANTS)
    assert simulated.import_kwh < recorded.import_kwh - 0.01, "the plant discharged into the house"
    assert simulated.profit_eur > recorded.profit_eur
    assert simulated.reference_divergence_kwh > 1e-3, (
        "the recorded rows are the reference the arm's own flows are measured against"
    )
    assert recorded.reference_divergence_kwh == pytest.approx(0.0, abs=1e-9)
    # The fixture's recorded columns are measured, so every window says so; a derived base would be
    # labelled instead of silently passing as measured.
    assert {window.pv_load_source for window in simulated.windows} == {BASE_MEASURED}


def test_price_resolution_follows_correction_one(tmp_path: Path) -> None:
    from app.replay.accounting import _arm_price

    leg = a_leg(tmp_path, minutes=2)
    columns = frozenset({"ts", "grid_kw", "price_buy", "price_sell"})
    resolve = _arm_price(leg, columns)
    prices = {"ts": to_iso(START), "price_buy": 0.31, "price_sell": 0.07}
    assert resolve(START, START, prices) == (0.31, 0.07, RECORDED_ACTUALS, RECORDED_ACTUALS)
    # A present-but-NULL column is a refusal, and never falls back.
    buy, sell, buy_source, sell_source = resolve(
        START, START, {"ts": to_iso(START), "price_buy": None, "price_sell": 0.07}
    )
    assert (buy, sell) == (None, 0.07)
    assert (buy_source, sell_source) == (PRICE_NULL, RECORDED_ACTUALS)
    # A column the recording's schema does not have falls back to the recorded plan slot.
    no_columns = _arm_price(leg, frozenset({"ts", "grid_kw"}))
    buy, sell, buy_source, sell_source = no_columns(START, START, {"ts": to_iso(START)})
    assert (buy, sell) == (0.25, 0.05)
    assert (buy_source, sell_source) == (PLAN_SLOT_FALLBACK, PLAN_SLOT_FALLBACK)
    # No slot covers the instant either: still a refusal, now as a missing vintage.
    far = START + timedelta(days=3)
    buy, sell, buy_source, sell_source = no_columns(far, far, {"ts": to_iso(far)})
    assert buy is None and sell is None
    assert (buy_source, sell_source) == (NO_PRICE, NO_PRICE)


def test_a_present_but_null_price_withholds_the_window(tmp_path: Path) -> None:
    source = recording(tmp_path)
    connection = sqlite3.connect(source)
    connection.execute("UPDATE actuals SET price_buy = NULL WHERE ts = ?", (to_iso(START),))
    connection.commit()
    connection.close()
    account = account_leg(a_leg(tmp_path, source=source), limits=PLANTS)
    first = account.windows[0]
    assert first.reasons == (PRICE_NULL,)
    assert first.withheld is True
    assert first.cost_eur is None and first.revenue_eur is None and first.profit_eur is None
    assert first.import_kwh is not None, "the kWh is known, the money is not"
    assert first.segments[0].buy_source == PRICE_NULL
    assert account.profit_eur is None, "no window could be settled, so the leg carries no profit"
    assert account.withheld_windows == (0,)


def test_the_settlement_never_uses_the_arms_own_booked_tariff(tmp_path: Path) -> None:
    leg = a_leg(tmp_path)
    connection = leg.store.connection
    connection.execute(
        "UPDATE actuals SET price_buy = 0.99, price_sell = 0.99 WHERE ts >= ? AND ts < ?",
        (to_iso(START), to_iso(leg.end)),
    )
    connection.commit()
    account = account_leg(leg, limits=PLANTS)
    assert account.cost_eur == pytest.approx(0.2 * 20 / 60 * 0.25), (
        "settlement is the recording's price series, never the arm's booked one"
    )
    assert account.windows[0].tariff_divergence_segments == len(account.windows[0].segments)
    assert account.withheld_windows == (), "a tariff divergence is a finding, not a refusal"


# ------------------------------------------------------------------------------- correction two


def test_the_integral_uses_real_timestamp_gaps(tmp_path: Path) -> None:
    leg = a_leg(tmp_path)
    connection = leg.store.connection
    connection.execute("DELETE FROM actuals WHERE ts = ?", (to_iso(START + timedelta(minutes=5)),))
    connection.commit()
    account = account_leg(leg, limits=PLANTS)
    first = account.windows[0]
    assert ENERGY_GAP in first.reasons
    assert first.withheld is True
    assert first.import_kwh is None, "a missing bucket must not silently undercount energy"
    assert first.cost_eur is None and first.profit_eur is None
    assert len(first.gaps) == 1
    assert first.gaps[0].gap_s == pytest.approx(600.0)
    assert first.gaps[0].covered_until == START + timedelta(minutes=5), (
        "the row covers its own declared interval; everything past it is the gap"
    )
    assert first.segments[0].end == START + timedelta(minutes=5), (
        "a row's value describes its own declared interval, never the gap behind it"
    )
    assert account.withheld_windows == (0,)


def test_a_bucket_the_arm_never_rewrote_withholds_its_window(tmp_path: Path) -> None:
    leg = a_leg(tmp_path)
    leg.buckets_written = leg.buckets_written[1:]  # as if a coarser cadence skipped the first bucket
    account = account_leg(leg, limits=PLANTS)
    first = account.windows[0]
    assert first.reasons == (BUCKET_NOT_VISITED,)
    assert first.import_kwh is None and first.profit_eur is None
    assert account.windows_unknown == 1
    assert account.withheld_windows == (0,)


# ----------------------------------------------------------------------------- correction three


def test_a_plant_ceiling_withholds_the_window_and_its_delta(tmp_path: Path) -> None:
    source = recording(tmp_path)
    clean = account_leg(a_leg(tmp_path, plant=SIMULATED, source=source), limits=PLANTS)
    # The recorded plant never evaluates the site export bound, so its manifest ceiling is null and
    # nothing is withheld; a declared numeric ceiling trips instead.
    plain = account_leg(a_leg(tmp_path, source=source), limits=PLANTS)
    assert plain.withheld_windows == ()
    assert plain.windows[0].plant.unverified_bounds > 0
    tripped = account_leg(
        a_leg(tmp_path, source=source),
        limits=PlantLimits(recorded=PlantCeiling(0, 0, 0), simulated=PLANTS.simulated),
    )
    for window in tripped.windows:
        assert window.ceiling_tripped == ("unverified_bounds",)
        assert window.reasons == (PLANT_CEILING,)
        assert window.withheld is True
        assert window.profit_eur is None, "a window the plant could not verify carries no money"
        assert window.import_kwh is not None, "its kWh are still reported"
    paired = paired_delta(plain, tripped, change="test lever")
    assert paired.withheld_windows == (0,)
    assert paired.included_windows == 0
    assert paired.delta_eur is None, "a sum over zero included windows is not a zero delta"
    assert paired.delta_with_terminal_eur is None
    assert paired.bootstrap_samples == (), "a withheld window is no bootstrap sample"
    # Two clean legs of the same window pair normally.
    twin = paired_delta(plain, account_leg(a_leg(tmp_path, source=source), limits=PLANTS))
    assert twin.included_windows == 1
    assert twin.delta_eur == pytest.approx(0.0)
    assert twin.bootstrap_samples == (0.0,)


def test_pairing_refuses_mismatched_windows(tmp_path: Path) -> None:
    source = recording(tmp_path)
    long_leg = account_leg(a_leg(tmp_path, plant=SIMULATED, source=source), limits=PLANTS)
    short_leg = account_leg(
        a_leg(tmp_path, plant=SIMULATED, minutes=2, source=source), limits=PLANTS
    )
    with pytest.raises(AccountingError) as caught:
        paired_delta(short_leg, long_leg)
    assert caught.value.reason == "window_mismatch"


# ------------------------------------------------------------------------------- the terminal term


def test_the_terminal_term_is_the_energy_delta_at_the_frozen_basis(tmp_path: Path) -> None:
    source = recording(tmp_path)
    add_cost_basis(source, 0.20, 2.75)
    leg = a_leg(tmp_path, plant=SIMULATED, source=source)
    account = account_leg(leg, limits=PLANTS)
    assert account.valuation_eur_per_kwh == pytest.approx(0.20)
    assert account.initial_energy_kwh == pytest.approx(2.75)
    assert account.energy_delta_kwh == pytest.approx(
        account.terminal_energy_kwh - account.initial_energy_kwh
    )
    assert account.energy_delta_kwh < 0, "the simulated leg discharged"
    assert account.terminal_adjustment_eur == pytest.approx(
        account.energy_delta_kwh * 0.20
    )
    assert account.profit_with_terminal_eur == pytest.approx(
        account.profit_eur + account.terminal_adjustment_eur
    )
    assert account.sensitivity_terminal_adjustment_eur == pytest.approx(
        account.terminal_adjustment_eur, abs=1e-6
    )
    assert account.sensitivity_valuation_eur_per_kwh == pytest.approx(0.20, abs=1e-3)
    assert account.notes == ()
    assert account.profit_eur == pytest.approx(account.revenue_eur - account.cost_eur)
    # The engine's own savings row is a different measurement (its own cadence, its own baseline) and
    # is never blended into the arm's flows.
    savings = leg.store.savings_daily_range(
        leg.start.date().isoformat(), leg.end.date().isoformat()
    )
    assert savings, "the engine books its own savings row"
    assert savings[0]["baseline_cost_eur"] != pytest.approx(account.cost_eur)


def test_without_a_frozen_basis_the_terminal_term_is_unavailable(tmp_path: Path) -> None:
    account = account_leg(a_leg(tmp_path, plant=SIMULATED), limits=PLANTS)
    assert account.valuation_eur_per_kwh is None
    assert account.terminal_adjustment_eur is None
    assert account.profit_with_terminal_eur is None
    assert account.terminal_energy_kwh is not None, "the energies are still reported"
    assert [slug for slug, _detail in account.notes] == ["no_valuation", "no_ledger_basis"], (
        "both the headline and the ledger-booked sensitivity are named as unvalued"
    )
    assert account.energy_delta_kwh is not None


# ---------------------------------------------------------- the declared window-start valuation


def a_ledger_row(basis: float = 0.31, energy: float = 2.0) -> dict:
    return {"basis_eur_per_kwh": basis, "energy_kwh": energy}


def test_a_frozen_row_is_declared_without_rebuilding_the_ledger(tmp_path: Path, monkeypatch) -> None:
    """The recording already knew this pack's basis at the window start, so nothing is rebuilt."""
    source = recording(tmp_path)
    add_cost_basis(source, 0.4, 3.0)
    calls: list[dict] = []

    def rebuild(*_args, **kwargs):
        calls.append(kwargs)
        return a_ledger_row()

    monkeypatch.setattr(accounting_module, "update_cost_basis", rebuild)
    with open_episode(source, START, START + timedelta(minutes=20), 1.0) as episode:
        declaration = window_start_valuation(episode)
    assert calls == [], "a frozen row means no ledger rebuild at all"
    assert declaration.source == FROZEN_PROFILE_BASIS
    assert declaration.basis == {BATTERY_ID: 0.4}
    assert declaration.frozen == (BATTERY_ID,) and declaration.derived == ()
    assert declaration.as_dict()["packs"] == 1


def test_a_missing_row_is_rebuilt_at_the_window_start_and_declared_as_derived(
    tmp_path: Path, monkeypatch
) -> None:
    """The rebuild is the engine's own first-tick call: same moment, same fleet and reserve inputs."""
    captured: dict = {}

    def rebuild(_store, _battery, moment, *, fleet_enabled, reserve_soc_pct):
        captured.update(moment=moment, fleet_enabled=fleet_enabled, reserve=reserve_soc_pct)
        return a_ledger_row()

    monkeypatch.setattr(accounting_module, "update_cost_basis", rebuild)
    with open_episode(recording(tmp_path), START, START + timedelta(minutes=20), 1.0) as episode:
        declaration = window_start_valuation(episode)
        start = episode.start
    assert captured["moment"] == start, "the derivation reads nothing past the window start"
    assert captured["fleet_enabled"] == 1 and captured["reserve"] == 0.0
    assert declaration.source == DERIVED_WINDOW_START_BASIS
    assert declaration.basis == {BATTERY_ID: 0.31}
    assert declaration.derived == (BATTERY_ID,) and declaration.missing == ()


def test_a_declared_valuation_values_the_terminal_term_and_reports_where_it_came_from(
    tmp_path: Path,
) -> None:
    """A declared basis is what the terminal term uses, provenance included -- never the leg's run."""
    leg = a_leg(tmp_path, plant=SIMULATED)
    declaration = WindowStartValuation(
        basis={BATTERY_ID: 0.4}, source=DERIVED_WINDOW_START_BASIS, derived=(BATTERY_ID,)
    )
    account = account_leg(leg, limits=PLANTS, window_start=declaration)
    assert account.valuation_source == DERIVED_WINDOW_START_BASIS
    assert account.valuation_derived == (BATTERY_ID,) and account.valuation_frozen == ()
    assert account.valuation_eur_per_kwh == pytest.approx(0.4)
    assert account.terminal_adjustment_eur == pytest.approx(account.energy_delta_kwh * 0.4)
    assert account.profit_with_terminal_eur == pytest.approx(
        account.profit_eur + account.terminal_adjustment_eur
    )


def test_the_declaration_never_mixes_the_two_sources_and_names_what_it_could_not_value(
    tmp_path: Path, monkeypatch
) -> None:
    """A pack the rebuild cannot value is named, and only the terminal term is refused for it."""
    monkeypatch.setattr(
        accounting_module, "update_cost_basis", lambda *_args, **_kwargs: None
    )
    with open_episode(recording(tmp_path), START, START + timedelta(minutes=20), 1.0) as episode:
        declaration = window_start_valuation(episode)
        leg = Runner(episode, name="leg", plant=SIMULATED).run()
        account = account_leg(leg, limits=PLANTS, window_start=declaration)
    assert declaration.source == NO_WINDOW_START_BASIS
    assert declaration.basis == {} and declaration.missing == (BATTERY_ID,)
    assert account.valuation_source == NO_WINDOW_START_BASIS
    assert account.valuation_missing == (BATTERY_ID,)
    assert account.terminal_adjustment_eur is None and account.profit_with_terminal_eur is None
    assert account.energy_delta_kwh is not None, "the energy delta itself is still reported"
    assert [slug for slug, _detail in account.notes] == ["no_valuation", "no_ledger_basis"]


# ------------------------------------------- the recording's booked feed-in price vs the model


def test_the_recorded_feed_in_price_is_checked_against_the_assumed_contract(tmp_path: Path) -> None:
    """Revenue may not rest on an assumed tariff: the recording's booked price must follow it."""
    source = recording(tmp_path)
    connection = sqlite3.connect(source)
    connection.execute(
        "UPDATE actuals SET price_buy = 0.25, price_sell = 0.25 WHERE ts >= ? AND ts < ?",
        (to_iso(START), to_iso(START + timedelta(minutes=20))),
    )
    connection.commit()
    connection.close()
    account = account_leg(
        a_leg(tmp_path, source=source), limits=PLANTS, tariff_tolerance_eur_per_kwh=0.0005
    )
    check = account.tariff_check
    assert check.model == TARIFF_MODEL_DERIVED, "the recording declares no mode, so the derived rule"
    assert check.comparand == RECORDING_SELF_CONSISTENCY
    assert check.compared_segments >= 1
    assert check.worst_eur_per_kwh == pytest.approx(0.0)
    assert check.diverged_windows == () and account.withheld_windows == ()
    assert check.worst_recorded_eur_per_kwh == pytest.approx(0.25)
    assert "self-consistent with the assumed" in check.note
    assert "not evidence the tariff was billed" in check.note, (
        "a match is recording self-consistency, never proof the tariff was billed"
    )


def test_a_recorded_feed_in_price_the_model_cannot_reproduce_withholds_the_window(
    tmp_path: Path,
) -> None:
    """The fixture books sell = 0.05 against buy = 0.25, which the derived rule cannot explain."""
    account = account_leg(a_leg(tmp_path), limits=PLANTS, tariff_tolerance_eur_per_kwh=0.0005)
    check = account.tariff_check
    assert check.worst_eur_per_kwh == pytest.approx(0.20, abs=1e-6)
    assert check.diverged_windows == tuple(window.index for window in account.windows)
    first = account.windows[0]
    assert EXPORT_REVENUE_UNVERIFIED in first.reasons and first.withheld is True
    assert first.profit_eur is None and first.revenue_eur is None and first.cost_eur is None
    assert first.import_kwh is not None, "the kWh is known, the money is not"
    assert account.withheld_windows == check.diverged_windows and account.profit_eur is None
    assert "withheld" in check.note and check.worst_model_eur_per_kwh == pytest.approx(0.25)


def test_a_leg_that_declares_its_own_rate_is_declared_not_self_verified(tmp_path: Path) -> None:
    """An explicit rate is declared by construction: comparing it with itself proves nothing."""
    account = account_leg(
        a_leg(tmp_path), limits=PLANTS, tariff_tolerance_eur_per_kwh=0.0005, tariff_declared=True
    )
    assert account.tariff_check.model == TARIFF_MODEL_DECLARED
    assert account.tariff_check.compared_segments == 0
    assert account.withheld_windows == (), "nothing was compared, so nothing is withheld"
    assert "not self-verified" in account.tariff_check.note


# ---------------------------------------------------------------- review findings (regressions)


def test_finding_2_a_leg_never_settles_on_its_own_booked_price(tmp_path: Path, monkeypatch) -> None:
    """With no recorded rows at all, the arm's own scratch row must not become the recorded price.

    The old ``reference.get(bucket) if reference else row`` passed the arm's own row whenever the
    reference dict was empty, so a leg whose tariff is deliberately wrong (E2's declared leg) priced
    itself with its own booked rate.  A missing recorded row is a refusal.
    """
    leg = a_leg(tmp_path)
    connection = leg.store.connection
    connection.execute(
        "UPDATE actuals SET price_buy = 0.99, price_sell = 0.99 WHERE ts >= ? AND ts < ?",
        (to_iso(leg.start), to_iso(leg.end)),
    )
    connection.commit()
    monkeypatch.setattr(accounting_module, "_recorded_rows", lambda _leg: {})
    account = account_leg(leg, limits=PLANTS)
    first = account.windows[0]
    assert ROW_ABSENT in first.reasons, "the missing recorded row is the refusal, not a fallback"
    assert all(piece.buy_source == ROW_ABSENT for piece in first.segments)
    assert first.cost_eur is None and first.profit_eur is None
    assert account.profit_eur is None, "the arm's own 0.99 rate never settled anything"


def test_finding_3_a_missing_row_refuses_but_a_missing_column_falls_back(
    tmp_path: Path, monkeypatch
) -> None:
    """Row presence and column presence are different facts: schema decides the column."""
    leg = a_leg(tmp_path)
    columns = accounting_module._recorded_columns(leg)
    assert {"price_buy", "price_sell"} <= columns
    resolve = accounting_module._arm_price(leg, columns)
    assert resolve(START, START, None) == (None, None, ROW_ABSENT, ROW_ABSENT), (
        "a missing recorded row is a refusal, never a silent price"
    )
    # The same missing row against a schema that does not have the price columns is the declared
    # fallback to the recorded plan slot -- a missing column, not a missing row.
    no_columns = accounting_module._arm_price(leg, frozenset({"ts", "grid_kw"}))
    assert no_columns(START, START, None) == (
        0.25,
        0.05,
        PLAN_SLOT_FALLBACK,
        PLAN_SLOT_FALLBACK,
    )
    # And the fallback path settles, when the tariff check is declared by construction (otherwise
    # an unverifiable recording tariff withholds the window, finding 8b).
    monkeypatch.setattr(
        accounting_module, "_recorded_columns", lambda _leg: frozenset({"ts", "grid_kw"})
    )
    account = account_leg(leg, limits=PLANTS, tariff_declared=True)
    first = account.windows[0]
    assert first.price_fallbacks == len(first.segments) and first.profit_eur is not None
    assert account.import_kwh == pytest.approx(0.2 * 20 / 60)


def a_holed_window(account, **changes):
    """One account whose first window carries the given changes (a forcing helper for the tests)."""
    return replace(
        account,
        windows=tuple(
            replace(window, **changes) if window.index == 0 else window
            for window in account.windows
        ),
    )


def test_finding_4_an_unknown_reference_is_not_perfect_fidelity(tmp_path: Path) -> None:
    """A window with no measurable divergence makes the leg's gate-1 number unknown, not zero."""
    account = account_leg(a_leg(tmp_path), limits=PLANTS)
    assert account.reference_divergence_kwh == pytest.approx(0.0, abs=1e-9)
    holed = a_holed_window(account, divergence_kwh=None, reasons=("energy_gap",))
    assert holed.reference_divergence_kwh is None, (
        "summing only the measurable windows would report an unknown reference as perfect fidelity"
    )


def test_finding_5_a_partial_sum_is_not_a_leg_total(tmp_path: Path) -> None:
    """Leg totals are whole-window or None: one withheld window of two nulls the money, not a sum."""
    source = recording(tmp_path)
    # Two decision windows, so "the leg total" is a real sum over more than one window, and only the
    # later window's recorded sell price breaches the assumed contract.
    add_replan(source, START + timedelta(minutes=10))
    book_the_derived_sell_price(source, until=START + timedelta(minutes=10))
    leg = a_leg(tmp_path, source=source)
    assert [window.index for window in leg.windows] == [0, 1], "two windows, not one"
    account = account_leg(leg, limits=PLANTS, tariff_tolerance_eur_per_kwh=0.0005)
    assert account.withheld_windows == (1,)
    assert account.windows_priced == 1 and account.windows_measured == len(account.windows)
    assert account.profit_eur is None, "1 of 2 windows priced must never print as the leg's profit"
    assert account.cost_eur is None and account.revenue_eur is None
    assert account.profit_with_terminal_eur is None, (
        "the full-window terminal term must not be added to a partial sum"
    )
    assert account.import_kwh is not None and account.export_kwh is not None, (
        "both flows were measured, so the kWh totals still cover every window"
    )
    assert next(window for window in account.windows if window.index == 0).profit_eur is not None, (
        "the per-window number stays visible: only the leg total is withheld"
    )

    # A window whose *flow* is unknown (one bucket its cadence never rewrote) makes the kWh totals
    # partial as well.
    unvisited = START + timedelta(minutes=15)
    flowless = account_leg(
        replace(
            leg,
            buckets_written=tuple(
                bucket for bucket in leg.buckets_written if bucket != unvisited
            ),
        ),
        limits=PLANTS,
        tariff_tolerance_eur_per_kwh=0.0005,
    )
    assert flowless.windows_measured == len(flowless.windows) - 1
    assert flowless.windows_unknown == 1
    assert flowless.import_kwh is None and flowless.export_kwh is None
    assert flowless.profit_eur is None

    # A wholly withheld account keeps the same rule (the existing ceiling case).
    holed = account_leg(
        a_leg(tmp_path, source=source),
        limits=PlantLimits(recorded=PlantCeiling(0, 0, 0), simulated=PLANTS.simulated),
    )
    assert holed.windows_priced == 0 and holed.windows_measured == len(holed.windows)
    assert holed.profit_eur is None
    assert holed.profit_with_terminal_eur is None
    paired = paired_delta(account, holed, change="withheld")
    assert paired.delta_eur is None
    assert paired.delta_with_terminal_eur is None
    assert paired.bootstrap_samples == ()
    complete = paired_delta(
        account_leg(a_leg(tmp_path, source=source), limits=PLANTS),
        account_leg(a_leg(tmp_path, source=source), limits=PLANTS),
        change="twin",
    )
    assert complete.delta_eur == pytest.approx(0.0), "a complete zero stays a real 0.0"


def test_finding_8a_plan_slot_fallbacks_are_not_compared_with_the_rule_that_made_them(
    tmp_path: Path, monkeypatch
) -> None:
    """The recorded plan slot was derived by production with the same rule, so it is not evidence."""
    monkeypatch.setattr(
        accounting_module, "_recorded_columns", lambda _leg: frozenset({"ts", "grid_kw"})
    )
    account = account_leg(a_leg(tmp_path), limits=PLANTS)
    check = account.tariff_check
    assert check.compared_segments == 0 and check.fallback_segments > 0, (
        "fallback segments are counted and excluded, never compared with their own rule"
    )
    assert check.model == TARIFF_MODEL_UNAVAILABLE
    assert check.reason == TARIFF_FALLBACK_ONLY, (
        "a fallback-priced recording is unverifiable for its own reason, not \"the model refused\""
    )
    assert TARIFF_FALLBACK_ONLY in check.note and "plan slot" in check.note
    assert "not independent evidence" in check.note


def test_finding_4_a_fallback_only_recording_is_refused_by_its_own_slug(tmp_path: Path) -> None:
    """A recording that predates the sell-price column must not read as \"model unavailable\"."""
    source = recording(tmp_path)
    make_the_recording_older(source)
    # The scratch copy is migrated on open, so it has both columns again: the check has to read the
    # recording's own schema, which no longer does.
    leg = a_leg(tmp_path, source=source)
    try:
        copy_columns = {
            str(row[1])
            for row in leg.episode.store().connection.execute("PRAGMA table_info(actuals)")
        }
        assert {"price_buy", "price_sell"} <= copy_columns, (
            "the scratch copy was migrated back to today's schema, so only the source can answer"
        )
        account = account_leg(leg, limits=PLANTS, tariff_tolerance_eur_per_kwh=0.0005)
    finally:
        leg.episode.close()
    check = account.tariff_check
    assert check.compared_segments == 0 and check.fallback_segments > 0
    assert check.model == TARIFF_MODEL_UNAVAILABLE and check.reason == TARIFF_FALLBACK_ONLY
    assert TARIFF_FALLBACK_ONLY in check.note
    assert account.withheld_windows == tuple(window.index for window in account.windows)
    assert account.profit_eur is None and account.revenue_eur is None


def test_finding_8iii_an_unreadable_source_schema_is_a_refusal_not_a_guess(monkeypatch) -> None:
    """With no row in the window, the column question must not be answered by the migrated copy."""
    fake = SimpleNamespace(
        boundary=SimpleNamespace(source_rows={"actuals": ()}),
        source_columns=lambda _table: None,
    )
    leg = SimpleNamespace(episode=fake)
    with pytest.raises(AccountingError) as caught:
        accounting_module._recorded_columns(leg)
    assert caught.value.reason == SOURCE_SCHEMA_UNKNOWN
    assert "scratch copy" in str(caught.value)
    # A readable source answers with its own columns; a row in the window answers first.
    fake.source_columns = lambda _table: frozenset({"ts", "grid_kw"})
    assert accounting_module._recorded_columns(leg) == frozenset({"ts", "grid_kw"})
    fake.boundary = SimpleNamespace(source_rows={"actuals": ({"ts": 0, "price_buy": 1.0},)})
    assert accounting_module._recorded_columns(leg) == frozenset({"ts", "price_buy"})


def test_finding_8iii_the_source_schema_answers_not_the_migrated_copy(tmp_path: Path) -> None:
    """A column the recording never had must read as absent even though the copy gains it."""
    source = recording(tmp_path)
    make_the_recording_older(source)
    leg = a_leg(tmp_path, source=source)
    try:
        copy_columns = {
            str(row[1])
            for row in leg.episode.store().connection.execute("PRAGMA table_info(actuals)")
        }
        assert "price_sell" in copy_columns, "the copy was migrated, so the rows path would guess"
        assert "price_sell" not in accounting_module._recorded_columns(leg)
        assert accounting_module._recorded_columns(leg) == leg.episode.source_columns("actuals")
    finally:
        leg.episode.close()


def test_finding_8iii_the_source_connection_is_read_only_and_cached(
    tmp_path: Path, monkeypatch
) -> None:
    """Reading the source's schema is read-only and cached, and a failed read returns ``None``."""
    source = recording(tmp_path)
    episode = open_episode(source, START, START + timedelta(minutes=20), 1.0)
    try:
        columns = episode.source_columns("actuals")
        assert columns is not None and {"ts", "grid_kw", "price_buy"} <= set(columns)
        assert episode.source_columns("actuals") is columns, "the schema is cached, not re-read"
        assert episode.source_columns("not_a_table") == frozenset(), (
            "an absent table is a fact about the recording, not an unreadable source"
        )
        def unreadable(*_args, **_kwargs):
            raise sqlite3.OperationalError("unable to open database file")

        monkeypatch.setattr(sqlite3, "connect", unreadable)
        assert episode.source_columns("measurements") is None
    finally:
        episode.close()


def test_finding_8b_an_unavailable_tariff_model_withholds_the_revenue(tmp_path: Path) -> None:
    """A model that cannot price the recording's own intervals leaves the revenue unverified."""
    account = account_leg(
        a_leg(tmp_path, source=recording(tmp_path, settings_extra={"price.feed_in_mode": "tiered"})),
        limits=PLANTS,
        tariff_tolerance_eur_per_kwh=0.0005,
    )
    check = account.tariff_check
    assert check.model == TARIFF_MODEL_UNAVAILABLE
    assert account.withheld_windows == tuple(window.index for window in account.windows)
    assert account.profit_eur is None and account.revenue_eur is None
    assert all(EXPORT_REVENUE_UNVERIFIED in window.reasons for window in account.windows)


def test_finding_8c_no_tolerance_means_the_tariff_was_not_judged(tmp_path: Path) -> None:
    """No declared tolerance is an unchecked check, not a pass with a reassuring note."""
    account = account_leg(a_leg(tmp_path), limits=PLANTS)
    check = account.tariff_check
    assert check.unchecked is True and check.tolerance_eur_per_kwh is None
    assert check.diverged_windows == ()
    assert "unchecked" in check.note and "self-consistent" not in check.note
    assert account.withheld_windows == (), "unchecked is stated, not silently withheld"


def test_finding_8d_buckets_are_unique_and_the_settlement_length_is_checked(
    tmp_path: Path, monkeypatch
) -> None:
    """A bucket clipped across two windows is compared once; a misaligned settlement is refused."""
    def segment(bucket, index):
        return Segment(
            bucket=bucket,
            start=bucket,
            end=bucket + timedelta(minutes=5),
            grid_kw=0.2,
            import_kwh=0.02,
            export_kwh=0.0,
            buy_eur_per_kwh=0.25,
            sell_eur_per_kwh=0.09,
            buy_source=RECORDED_ACTUALS,
            sell_source=RECORDED_ACTUALS,
            arm_booked_buy=0.25,
            arm_booked_sell=0.09,
        )

    first, second = START, START + timedelta(minutes=5)
    windows = [
        SimpleNamespace(index=0, segments=(segment(first, 0), segment(second, 0))),
        SimpleNamespace(index=1, segments=(segment(first, 0),)),
    ]
    settings = {"price.feed_in_mode": "explicit", "price.feed_in_eur_per_kwh": 0.05}
    check, per_window = recorded_tariff_check(
        windows, settings=settings, tolerance_eur_per_kwh=0.0005
    )
    assert check.compared_segments == 2, "the shared bucket is one independent observation"
    assert check.diverged_windows == (0, 1), (
        "the shared bucket's divergence is attributed to every window that contains it"
    )
    assert per_window == {0: pytest.approx(0.04), 1: pytest.approx(0.04)}

    # A settlement whose length does not match the buckets cannot be aligned by position: refused.
    monkeypatch.setattr(
        accounting_module,
        "settle_feed_in",
        lambda *_args, **_kwargs: FeedInResult("explicit", [0.1], True, None, 0.1, [], None),
    )
    refused, _per_window = recorded_tariff_check(
        windows, settings=settings, tolerance_eur_per_kwh=0.0005
    )
    assert refused.model == TARIFF_MODEL_UNAVAILABLE
    assert "cannot be aligned" in refused.note


# --------------------------------------------------- review findings 9, 11 and 14b (accounting)


def test_finding_9_the_sensitivity_valuation_comes_from_the_ledger(tmp_path: Path) -> None:
    """The declared sensitivity is the ledger's own booked basis, not a copy of the headline."""
    source = recording(tmp_path)
    add_cost_basis(source, 0.20, 2.75)
    leg = a_leg(tmp_path, plant=SIMULATED, source=source)
    declaration = WindowStartValuation(
        basis={BATTERY_ID: 0.9},
        source=DERIVED_WINDOW_START_BASIS,
        derived=(BATTERY_ID,),
    )
    account = account_leg(leg, limits=PLANTS, window_start=declaration)
    assert account.valuation_eur_per_kwh == pytest.approx(0.9), "the headline is the declaration"
    assert account.sensitivity_valuation_eur_per_kwh == pytest.approx(0.20), (
        "recomputing the declared basis would report one number twice and measure nothing"
    )
    assert account.sensitivity_terminal_adjustment_eur == pytest.approx(
        account.energy_delta_kwh * 0.20
    )


def test_finding_9_the_sensitivity_is_unknown_without_a_ledger_basis(tmp_path: Path) -> None:
    """A pack with a real energy delta and no booked basis has no measurable sensitivity."""
    account = account_leg(a_leg(tmp_path, plant=SIMULATED), limits=PLANTS)
    assert account.energy_delta_kwh is not None and account.energy_delta_kwh != 0.0
    assert account.sensitivity_valuation_eur_per_kwh is None
    assert account.sensitivity_terminal_adjustment_eur is None
    assert NO_LEDGER_BASIS in [slug for slug, _detail in account.notes]


def test_finding_11_a_missing_window_tally_withholds_the_window(tmp_path: Path) -> None:
    """The tally is the plant's own claim: without it the window's flow was not verified."""
    leg = a_leg(tmp_path)
    account = account_leg(replace(leg, window_tallies=()), limits=PLANTS)
    window = account.windows[0]
    assert PLANT_TALLY_MISSING in window.reasons
    assert window.withheld is True and window.profit_eur is None
    assert account.profit_eur is None, "no tally means no settled money"


def test_finding_14b_uncovered_window_time_is_named_and_withheld(tmp_path: Path) -> None:
    """A lost first-bucket write leaves window time no row covers: that is a coverage gap."""
    leg = a_leg(tmp_path)
    first = quarter_floor(leg.start, 5)
    # A store write the runner still counted as written (buckets_written keeps the bucket): the
    # deleted row leaves leading window time no segment covers.
    assert first in set(leg.buckets_written)
    leg.store.connection.execute("DELETE FROM actuals WHERE ts = ?", (to_iso(first),))
    leg.store.connection.commit()
    account = account_leg(leg, limits=PLANTS)
    window = account.windows[0]
    assert COVERAGE_GAP in window.reasons
    assert window.gaps and any(gap.gap_s > 0 for gap in window.gaps)
    assert window.import_kwh is None, "a flow summed over a hole is not a flow"
    assert account.profit_eur is None
