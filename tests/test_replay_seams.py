"""Stage 2a replay seams: the engine's thread step methods and the battery driver factory.

The replay harness (``docs/REPLAY_HARNESS.md`` section 1) drives the *production* engine through
these two seams instead of reimplementing it, so they must stay behaviour-preserving: one step call
is exactly one thread iteration, and an injected driver factory replaces the registry driver
wholesale.  The boundary tests use an advancing clock, because a fixed clock cannot tell *when* a
step sampled it -- only *what* it saw.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "energy_manager"))

from app.control.dispatcher import TickOutcome  # noqa: E402
from app.control.drivers.base import BatteryDriver  # noqa: E402
from app.engine import Engine  # noqa: E402
from app.main import _FakeHA, _FakeStore, _selfcheck_settings, _selfcheck_sources  # noqa: E402
from app.models import BatteryConfig, BatteryState, DispatchResult, Mode  # noqa: E402

START = datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now: datetime | None = None) -> None:
        self.now = now or START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: Any) -> datetime:
        self.now = self.now + timedelta(**kwargs)
        return self.now


class AdvancingClock:
    """Returns a new instant on every call, so a clock sample during a step is observable."""

    def __init__(self, step_s: float = 1.0) -> None:
        self.now = START
        self.step = timedelta(seconds=step_s)
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        moment = self.now
        self.now = self.now + self.step
        return moment


class RaisingClock:
    """Any clock read is a bug in the step boundary under test."""

    def __call__(self) -> datetime:
        raise RuntimeError("example clock failure")


class FakeDriver:
    """An in-process ``BatteryDriver``: a replay plant plus a record of the engine's calls."""

    def __init__(self, config: BatteryConfig, clock: Clock) -> None:
        self.config = config
        self._clock = clock
        self.soc_pct = 50.0
        self.reads = 0
        self.calls: list[tuple[str, float, bool]] = []

    def id(self) -> str:
        return self.config.id

    def read_state(self) -> BatteryState:
        self.reads += 1
        return BatteryState(
            id=self.config.id,
            available=True,
            soc_pct=self.soc_pct,
            power_w=0.0,
            capacity_kwh=self.config.capacity_kwh,
            energy_kwh=self.config.capacity_kwh * self.soc_pct / 100.0,
            read_at=self._clock(),
            power_measured=True,
        )

    def apply(self, power_w: float, *, dry_run: bool) -> DispatchResult:
        self.calls.append(("apply", power_w, dry_run))
        return DispatchResult(
            battery_id=self.config.id,
            ts=self._clock(),
            requested_w=power_w,
            applied_w=power_w,
            ok=True,
            dry_run=dry_run,
        )

    def hold(self, *, dry_run: bool) -> DispatchResult:
        self.calls.append(("hold", 0.0, dry_run))
        return DispatchResult(
            battery_id=self.config.id,
            ts=self._clock(),
            requested_w=0.0,
            applied_w=0.0,
            ok=True,
            dry_run=dry_run,
        )

    def capabilities(self) -> dict[str, Any]:
        return {"driver": "fake", "signed": True, "entities": dict(self.config.mapping)}


def make_factory(clock: Clock) -> tuple[Any, list[FakeDriver]]:
    made: list[FakeDriver] = []

    def factory(config: BatteryConfig) -> FakeDriver:
        driver = FakeDriver(config, clock)
        made.append(driver)
        return driver

    return factory, made


def build_engine(clock: Any = None, **overrides: Any) -> tuple[Engine, Any]:
    settings = _selfcheck_settings()
    settings["control.grid_follow"] = False
    engine = Engine(
        settings,
        store=_FakeStore(),
        ha=_FakeHA(),
        clock=clock or Clock(),
        version="0.1.0",
        commit="replay-seams",
        **_selfcheck_sources(),
        **overrides,
    )
    return engine, settings


# ------------------------------------------------------------------------- step methods


def test_control_step_performs_one_tick_with_the_tick_outcome():
    """``_control_step`` is one ``_control_tick`` with and without an explicit moment."""
    clock = Clock()
    stepped, _settings = build_engine(clock)
    ticked, _settings = build_engine(clock)
    assert stepped._control_step(clock.now) == ticked._control_tick(clock.now)
    production, _settings = build_engine(clock)
    identical, _settings = build_engine(clock)
    # The loop passes no argument, so the tick must resolve the same moment it always did.
    assert production._control_step() == identical._control_tick()


def test_replan_step_replans_only_when_the_engine_says_so():
    """The step keeps the engine's decision: no reason, no replan; otherwise the given moment."""
    engine, _settings = build_engine()
    clock = Clock()
    calls: list[tuple[str, datetime]] = []
    engine._replan_reason = lambda: None
    engine._replan = lambda reason, now: calls.append((reason, now))
    engine._replan_step(clock.now)
    assert calls == []
    engine._replan_reason = lambda: "forced"
    clock.advance(seconds=5)
    engine._replan_step(clock.now)
    assert calls == [("forced", clock.now)]


def test_replan_step_reads_no_clock_when_there_is_no_reason():
    """The idle iteration must not sample the clock at all (the old loop did not)."""
    clock = AdvancingClock()
    engine, _settings = build_engine(clock)
    engine._replan_reason = lambda: None
    before = clock.calls
    engine._replan_step()
    assert clock.calls == before


def test_replan_step_samples_the_clock_after_the_reason():
    """With no argument the replan moment is read once, after ``_replan_reason`` has run."""
    clock = AdvancingClock()
    engine, _settings = build_engine(clock)
    seen: list[datetime] = []

    def reason() -> str:
        seen.append(engine._clock())  # the real ``_replan_reason`` reads the clock first
        return "forced"

    engine._replan_reason = reason
    calls: list[tuple[str, datetime]] = []
    engine._replan = lambda reason, now: calls.append((reason, now))
    before = clock.calls
    engine._replan_step()
    assert clock.calls - before == 2, "exactly one sample for the reason, one for the replan"
    assert calls == [("forced", seen[0] + timedelta(seconds=1))], calls


def test_an_explicit_now_is_used_verbatim_by_every_step():
    """An explicit moment must never be replaced by a clock sample."""
    clock = AdvancingClock()
    engine, _settings = build_engine(clock)
    moment = datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc)

    outcome = engine._control_step(moment)
    assert outcome is not None and outcome.ts == moment

    calls: list[tuple[str, datetime]] = []
    engine._replan_reason = lambda: "forced"
    engine._replan = lambda reason, now: calls.append((reason, now))
    before = clock.calls
    engine._replan_step(moment)
    assert calls == [("forced", moment)]
    assert clock.calls == before, "the replan moment must not be sampled from the clock"

    seen: list[datetime] = []
    for name in (
        "_retry_live_write_recovery",
        "_check_battery_floor",
        "_check_negative_price",
        "_maybe_maintenance",
        "publish_states",
    ):
        real = getattr(engine, name)
        setattr(engine, name, lambda now, _real=real: (seen.append(now), _real(now))[1])
    engine._guard_step(moment)
    assert seen and all(now == moment for now in seen), seen


def test_a_raising_clock_is_swallowed_by_the_steps(caplog):
    """With no argument the steps resolve the moment themselves; a broken clock must not escape."""
    engine, _settings = build_engine()
    engine._clock = RaisingClock()

    assert engine._control_step() is None
    assert any(row["code"] == "control_tick_failed" for row in engine.log(limit=50))

    with caplog.at_level(logging.ERROR):
        assert engine._guard_step() is None
    assert "guard loop failed" in caplog.text


def test_guard_step_runs_every_duty_and_publishes():
    """One guard iteration: recovery, floor, negative price, maintenance, then the publisher."""
    engine, _settings = build_engine()
    clock = Clock()
    seen: list[str] = []
    for name in (
        "_retry_live_write_recovery",
        "_check_battery_floor",
        "_check_negative_price",
        "_maybe_maintenance",
        "publish_states",
    ):
        real = getattr(engine, name)
        setattr(engine, name, lambda now, _name=name, _real=real: (seen.append(_name), _real(now))[1])
    engine._guard_step(clock.now)
    assert seen == [
        "_retry_live_write_recovery",
        "_check_battery_floor",
        "_check_negative_price",
        "_maybe_maintenance",
        "publish_states",
    ]
    assert len(engine._ha.published) >= 8, "the publisher must have reached the HA facade"


def test_guard_step_reaches_the_battery_floor_alert():
    """The floor duty is live, not just present: a battery at its floor alerts in the guard."""
    clock = Clock()
    factory, made = make_factory(clock)
    engine, _settings = build_engine(clock, driver_factory=factory)
    engine.tick_now()
    made[0].soc_pct = 5.0  # below the example battery's 10% floor
    engine.tick_now()
    codes = [alert.code for alert in engine.snapshot().alerts]
    assert "battery_floor_reached" not in codes, "the ticks must not raise the floor alert"
    engine._guard_step(clock.now)
    codes = [alert.code for alert in engine.snapshot().alerts]
    assert "battery_floor_reached" in codes, "the guard step is what raises it"


def test_guard_step_keeps_the_defensive_error_handling():
    """A failing duty must not escape the step: the loop's defensive except lives in the step."""
    engine, _settings = build_engine()
    clock = Clock()

    def broken(now=None):
        raise RuntimeError("example guard failure")

    engine.publish_states = broken
    assert engine._guard_step(clock.now) is None  # must not raise


# ---------------------------------------------------------------------- driver factory


def test_driver_factory_replaces_the_registry_driver():
    clock = Clock()
    factory, made = make_factory(clock)
    engine, _settings = build_engine(clock, driver_factory=factory)
    engine._sync_drivers()
    assert [driver.config.id for driver in made] == ["example_battery_1"]
    injected = engine._drivers["example_battery_1"]
    assert injected is made[0]
    assert isinstance(injected, BatteryDriver), "the fake must satisfy the driver protocol"


def test_no_driver_factory_keeps_the_registry_driver():
    engine, _settings = build_engine()
    engine._sync_drivers()
    assert type(engine._drivers["example_battery_1"]).__name__ == "GenericDriver"


def test_the_injected_driver_drives_read_state_and_apply():
    clock = Clock()
    factory, made = make_factory(clock)
    engine, _settings = build_engine(clock, driver_factory=factory)
    engine.set_mode(Mode.AUTO.value, False)
    outcome = engine.tick_now()
    driver = made[0]
    assert driver.reads >= 1, "the tick must sample through the injected driver"
    assert outcome.battery_states and outcome.battery_states[0].soc_pct == 50.0
    applies = [call for call in driver.calls if call[0] == "apply"]
    assert applies, "the dispatcher must apply through the injected driver"
    _method, power_w, dry_run = applies[-1]
    assert dry_run is False
    assert power_w == outcome.results[0].applied_w
