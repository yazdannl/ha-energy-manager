"""The replay runner: one arm leg, driven through the production step seams on a virtual clock.

``docs/REPLAY_HARNESS.md`` section 1: the harness constructs a real :class:`~app.engine.Engine` and
drives it, because what is measured must be the production planning path -- replan triggers and
cadence, dispatcher, safety layers, cost-basis ledger, journal.  This module owns the four things
that make that possible without threads:

* **the virtual clock** -- :class:`CallableClock` answers ``episode.now``, which only
  :meth:`Runner.step` advances (through ``episode.advance_to``).  ``Engine.start()`` is *never*
  called: it spawns ``sem-control``/``sem-replan``/``sem-guard``, and threads plus a frozen clock
  would be a race.
* **the capture façade** -- :class:`CaptureHA` answers reads from the recording at the current
  instant, records every call, and never lets a ``call_service`` reach anywhere: a command seen
  there is a refusal (``facade_command``), because commands belong to the plant driver.
* **the episode-backed sources** -- price/solar/load vintages and the sampler are closures over
  ``episode.at(now)``, so a tick can only ever see data issued at or before ``now``.
* **the cadence dispatch** -- one step call per due loop cadence, with the production constants
  (control ``Dispatcher.tick_interval_s()``, guard 2.0 s, replan 1.0 s); the engine still decides
  *whether* to replan through ``_replan_reason``.

Vintages.  The as-of recorded plan's own ``plan_slots`` carry the aligned buy/sell/pv/load the
recording's replan actually acted on, so they are the primary source.  Archived ``forecast_archive``
rows (solar and load -- the engine archives no price rows) fill slots the plan vintage does not
cover.  The vintage is passed through **verbatim**: no slot is invented beyond the last one it
knows, so ``dp.align_slots`` forward-fills the tail and, exactly as in production, marks those slots
``price_stale``; the dispatcher's stale-price stand-down and the all-stale guard then act on the
recording's own information instead of on a fresh-looking tail.  The number of solver grid slots the
vintage did not cover is reported per call (``VintageCall.tail_uncovered_slots``), and an instant
with neither source refuses the run with ``no_vintage`` -- never a shortened or invented horizon.

The run is bounded by the episode: instants are ``start + step * i`` and the last one is exactly
``end``, so terminal energy is read at the boundary and ``advance_to`` never has to refuse a step.

The recorded policy (E4).  :class:`RecordedPolicy` answers the recording's own setpoint in force at
the replay instant from ``episode.dispatch``: the newest row at or before ``now``, dry runs skipped
(a dry run moved nothing, so the setpoint in force is the older one), and a row the recording only
requested -- ``applied_w`` NULL -- used as the request, because that is what the recorded policy
decided.  A pack with no such row has no recorded setpoint: the source returns ``None``, the
substitute driver HOLDS it at 0 W, and the tick is counted in ``LegResult.policy_notes`` -- a
missing row never becomes an engine decision.  The engine's own request is kept in the journal (the
substitute driver rewrites only ``DispatchResult.requested_w``), so every row of the scratch
dispatch log is the requested-versus-recorded pair.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping

from .. import __version__ as APP_VERSION
from ..config import DEFAULTS, ConfigError, Settings, coerce_setting
from ..control.drivers.base import BatteryDriver
from ..cost_basis import measurement_roles
from ..engine import ACTUALS_MINUTES, Engine, Journal
from ..models import (
    BatteryConfig,
    BatteryState,
    DispatchResult,
    Measurement,
    LoadForecast,
    LoadSlot,
    PriceForecast,
    PriceSlot,
    SolarForecast,
    SolarSlot,
    clamp,
    from_iso,
    quarter_floor,
    to_iso,
)
from ..sources.measure import MEASUREMENT_MINUTES
from ..store import Store
from .episode import Episode
from .plant import MODES, RECORDED, PlantDriver, plant_factory

#: The cadences ``_guard_loop`` and ``_replan_loop`` wait between iterations; control comes from
#: the dispatcher, which owns ``control.tick_seconds``.
GUARD_INTERVAL_S = 2.0
REPLAN_INTERVAL_S = 1.0

#: Runner refusal slugs (docs/REPLAY_HARNESS.md section 6); stable and machine-readable.
NO_VINTAGE = "no_vintage"
FACADE_COMMAND = "facade_command"
RUNNER_INVALID = "runner_invalid"

#: A measurement row the live sampler would have persisted could not be written.  Like the live
#: closure it does not abort the run -- the ledger reads those rows, so gate 2 sees the floor stall
#: that follows, and the report names the failure instead of hiding it.
MEASUREMENT_WRITE_FAILED = "measurement_write_failed"

#: The recorded-policy leg's own refusals: nothing to replay, or nothing replayed.
NO_DISPATCH = "no_dispatch"
RECORDED_POLICY_UNKNOWN = "recorded_policy_unknown"


class RunnerError(RuntimeError):
    """A refused replay leg, with a stable ``reason`` slug."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason


class CallableClock:
    """The engine's clock: the episode's replay instant, and nothing else."""

    def __init__(self, episode: Episode) -> None:
        self.episode = episode

    def __call__(self) -> datetime:
        return self.episode.now


class NoopNotifier:
    """``Notifier``'s shape with no transport: a replay must never send a notification."""

    def send(
        self,
        event: str,
        title: str,
        message: str,
        *,
        dedupe_s: float = 0.0,
        detail: Mapping[str, Any] | None = None,
    ) -> bool:
        return False


class SettingsOverlay:
    """``Store``-shaped read view over the frozen settings plus one arm's declared patch.

    ``Settings`` only ever calls ``get_setting``/``get_settings`` on its store, so an arm can be
    given a different tariff model (E2) without touching the recording or the plant's actuator
    limits.
    """

    def __init__(self, values: Mapping[str, Any], patch: Mapping[str, Any] | None = None) -> None:
        self._values = dict(values)
        self._patch = dict(patch or {})

    def get_setting(self, key: str, default: Any = None) -> Any:
        if key in self._patch:
            return self._patch[key]
        return self._values.get(key, default)

    def get_settings(self) -> dict[str, Any]:
        return {**self._values, **self._patch}


def validated_patch(patch: Mapping[str, Any] | None) -> tuple[tuple[str, Any], ...]:
    """One arm's declared settings patch, coerced and refused if it names an unknown key."""
    out: dict[str, Any] = {}
    for key, value in (patch or {}).items():
        if key not in DEFAULTS:
            raise RunnerError(RUNNER_INVALID, f"unknown setting in the arm patch: {key}")
        try:
            out[str(key)] = coerce_setting(str(key), value)
        except ConfigError as exc:
            raise RunnerError(RUNNER_INVALID, f"arm patch {key}: {exc}") from exc
    return tuple(sorted(out.items()))


class CaptureHA:
    """The capture façade (docs/REPLAY_HARNESS.md section 1).

    Reads are answered from the recording at the current replay instant, where the battery mapping
    can support them; everything else is unknown rather than fabricated.  Every call is recorded.
    ``set_state`` is the expected publish path (counted, never interpreted); ``call_service`` is
    recorded and the runner refuses the leg, because in a replay commands belong to the plant.
    """

    def __init__(self, episode: Episode) -> None:
        self.episode = episode
        self.reads: list[str] = []
        self.publishes: list[tuple[str, str]] = []
        self.commands: list[tuple[str, str, dict[str, Any]]] = []

    # -- the protocol ``HAFacade`` -------------------------------------------------

    def get_state(self, entity_id: str) -> dict[str, Any] | None:
        self.reads.append(str(entity_id))
        return self._episode_states().get(str(entity_id))

    def get_states(self) -> list[dict[str, Any]]:
        self.reads.append("*")
        return [{"entity_id": key, **value} for key, value in sorted(self._episode_states().items())]

    def get_config(self) -> dict[str, Any]:
        self.reads.append("config")
        return {}

    def call_service(self, domain: str, service: str, data: Mapping[str, Any]) -> None:
        self.commands.append((str(domain), str(service), dict(data)))

    def set_state(
        self, entity_id: str, state: Any, attributes: Mapping[str, Any] | None = None
    ) -> None:
        self.publishes.append((str(entity_id), str(state)))

    # -- the recording's answers ----------------------------------------------------

    def _episode_states(self) -> dict[str, dict[str, Any]]:
        """The battery mapping entities, valued from the recording at the replay instant."""
        snapshot = self.episode.at(self.episode.now)
        states: dict[str, dict[str, Any]] = {}
        for config in self.episode.batteries:
            state = snapshot.battery_state(config)
            for key, entity_id in sorted(config.mapping.items()):
                if not entity_id or not isinstance(entity_id, str):
                    continue
                value, unit = _mapped_value(str(key), state)
                if value is None:
                    continue
                states[entity_id] = {
                    "state": f"{value:g}",
                    "attributes": {"unit_of_measurement": unit} if unit else {},
                }
        return states

    def summary(self) -> dict[str, Any]:
        return {
            "reads": len(self.reads),
            "publishes": len(self.publishes),
            "commands": len(self.commands),
            "published_entities": sorted({entity for entity, _ in self.publishes}),
        }


def _mapped_value(key: str, state: BatteryState) -> tuple[float | None, str]:
    """One mapped entity's recorded value: SoC, power or energy, and never a guess."""
    lowered = key.lower()
    if any(token in lowered for token in ("soc", "level")):
        return state.soc_pct, "%"
    if any(token in lowered for token in ("power", "setpoint", "limit")):
        return state.power_w, "W"
    if any(token in lowered for token in ("energy", "capacity")):
        return state.energy_kwh, "kWh"
    return None, ""


# --------------------------------------------------------------------------------- run records


@dataclass(frozen=True)
class Refusal:
    """One refused instant, with the stable slug the report carries."""

    instant: datetime
    reason: str
    detail: str


@dataclass(frozen=True)
class VintageCall:
    """One planner call's information provenance, so a window can state what it planned on.

    ``tail_uncovered_slots`` counts the solver grid slots beyond the vintage's own last slot: those
    are the ones ``dp.align_slots`` will forward-fill and mark stale, which is the production
    behaviour for that same partial input.  The vintage itself is never extended.
    """

    instant: datetime
    kind: str
    sources: tuple[str, ...]
    tail_uncovered_slots: int
    horizon_end: datetime


@dataclass
class WindowTally:
    """One decision window's plant unavailability counts, against the manifest's ceilings."""

    index: int
    unavailable_ticks: int = 0
    infeasible_commands: int = 0
    unverified_bounds: int = 0


@dataclass
class PlantTally:
    """Plant unavailability counts, in control ticks (the axis the manifest ceilings use).

    ``commands`` counts accepted setpoints; ``infeasible_commands`` and ``unverified_bounds`` count
    the commands the actuator itself reported as impossible or unverifiable.  A driver applies at
    most one command per control tick, so the flags read right after a tick describe that tick's
    command; ``events`` also keeps the first tick of every unavailability spell.
    """

    ticks: int = 0
    commands: int = 0
    unavailable_ticks: int = 0
    infeasible_commands: int = 0
    unverified_bounds: int = 0
    events: list[tuple[datetime, str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class WindowBoundary:
    """One recorded decision window: the plan in force until the next recorded replan."""

    index: int
    start: datetime
    end: datetime


@dataclass
class LegResult:
    """Everything one driven leg produced, in deterministic form (no wall-clock values)."""

    name: str
    plant: str
    policy: str
    patch: tuple[tuple[str, Any], ...]
    start: datetime
    end: datetime
    step_s: float
    steps: int
    control_steps: int
    replan_steps: int
    guard_steps: int
    policy_notes: Mapping[str, Any]
    refusals: tuple[Refusal, ...]
    tally: PlantTally
    initial_battery_states: tuple[BatteryState, ...]
    final_battery_states: tuple[BatteryState, ...]
    windows: tuple[WindowBoundary, ...]
    window_tallies: tuple[WindowTally, ...]
    vintage_calls: tuple[VintageCall, ...]
    buckets_written: tuple[datetime, ...]
    facade: Mapping[str, Any]
    events: tuple[Mapping[str, Any], ...]
    store_failures: Mapping[str, int]
    store: Store
    episode: Episode
    measurement_rows: tuple[tuple[str, int], ...] = ()
    measurement_buckets: tuple[datetime, ...] = ()
    measurement_failures: tuple[Refusal, ...] = ()

    @property
    def refused(self) -> bool:
        return bool(self.refusals)

    def terminal_energy_kwh(self) -> float | None:
        """The arm's own realised pack energy at the window end, or ``None`` when unknown."""
        values = [state.energy_kwh for state in self.final_battery_states]
        if not values or any(value is None for value in values):
            return None
        return float(sum(value for value in values if value is not None))

    def initial_energy_kwh(self) -> float | None:
        values = [state.energy_kwh for state in self.initial_battery_states]
        if not values or any(value is None for value in values):
            return None
        return float(sum(value for value in values if value is not None))


class CommandOverrideDriver:
    """A plant driver whose setpoint comes from a recorded policy instead of the engine.

    E4 needs the recorded policy replayed through the *same* simulated plant as the candidate
    policy; wrapping the plant keeps the physics, the fleet and every read identical, and only the
    applied power is substituted.  Everything the engine reads opportunistically (``realized_power``,
    ``commanded_power``, ``grid_kw``, the flags) is delegated to the wrapped driver.

    The result's ``requested_w`` is rewritten back to the engine's own request, so the journal keeps
    the candidate-versus-recorded pair; ``applied_w`` stays what the plant really did.
    """

    driver_name = "replay_recorded_policy"

    def __init__(
        self,
        inner: PlantDriver,
        source: Callable[[BatteryConfig, datetime], float | None],
    ) -> None:
        self._inner = inner
        self._source = source

    def id(self) -> str:
        return self._inner.id()

    def read_state(self) -> BatteryState:
        return self._inner.read_state()

    def apply(self, power_w: float, *, dry_run: bool) -> Any:
        return self._journal(self._inner.apply(self._substitute(), dry_run=dry_run), float(power_w))

    def hold(self, *, dry_run: bool) -> Any:
        return self._journal(self._inner.apply(self._substitute(), dry_run=dry_run), 0.0)

    def capabilities(self) -> dict[str, Any]:
        return self._inner.capabilities()

    def _substitute(self) -> float:
        """The recorded setpoint, or a HOLD when the recording has none.

        ``None`` must never fall back to the engine's request: the whole point of the recorded leg
        is that the plant follows the recording, not the candidate policy.
        """
        substitute = self._source(self._inner.config, self._inner.episode.now)
        return 0.0 if substitute is None else float(substitute)

    @staticmethod
    def _journal(result: Any, requested_w: float) -> Any:
        if isinstance(result, DispatchResult):
            return replace(result, requested_w=requested_w)
        return result

    def __getattr__(self, name: str) -> Any:
        if name in ("_inner", "_source"):
            raise AttributeError(name)
        return getattr(self._inner, name)


@dataclass(frozen=True)
class RecordedPolicyNote:
    """What the recorded policy answered, in id-free form (the report echoes this)."""

    rows: int
    packs: int
    provided: int
    requested_fallback: int
    missing_holds: int
    first_missing: str | None
    first_provided: str | None
    last_source: str | None


class RecordedPolicy:
    """The recording's own setpoints, as-of the replay instant (the E4 recorded leg).

    ``episode.dispatch`` is ordered by ``(ts, id)`` and carries each pack's newest row at or before
    the window start, so the setpoint *in force* at ``now`` is the last row at or before ``now``:
    never a later one, never an interpolated one.  Dry-run rows are dropped at construction because
    they moved nothing, and a row with a NULL ``applied_w`` falls back to its ``requested_w`` -- what
    the recorded policy decided.  A pack with no row at or before ``now`` returns ``None``: the
    substitute driver holds it and the tick is counted here.
    """

    def __init__(self, episode: Episode) -> None:
        self.episode = episode
        self.provided = 0
        self.requested = 0
        self.missing = 0
        self.first_missing: datetime | None = None
        self.first_provided: datetime | None = None
        self.last_source: str | None = None
        self._rows: dict[str, tuple[list[datetime], list[Mapping[str, Any]]]] = {}
        for row in episode.dispatch:
            moment = from_iso(row.get("ts"))
            if moment is None or bool(row.get("dry_run")):
                continue
            times, rows = self._rows.setdefault(str(row.get("battery_id")), ([], []))
            times.append(moment)
            rows.append(row)
        for times, rows in self._rows.values():
            order = sorted(range(len(times)), key=lambda index: times[index])
            times[:] = [times[index] for index in order]
            rows[:] = [rows[index] for index in order]

    def __call__(self, config: BatteryConfig, now: datetime) -> float | None:
        row = self._row_in_force(config.id, now)
        if row is None:
            self.missing += 1
            if self.first_missing is None:
                self.first_missing = now
            return None
        value = _finite_number(row.get("applied_w"))
        source = "applied"
        if value is None:
            value = _finite_number(row.get("requested_w"))
            source = "requested"
        if value is None:
            self.missing += 1
            if self.first_missing is None:
                self.first_missing = now
            return None
        self.provided += 1
        if source == "requested":
            self.requested += 1
        self.last_source = source
        if self.first_provided is None:
            self.first_provided = now
        return value

    def _row_in_force(self, battery_id: str, now: datetime) -> Mapping[str, Any] | None:
        found = self._rows.get(battery_id)
        if not found:
            return None
        times, rows = found
        index = bisect_right(times, now) - 1
        return rows[index] if index >= 0 else None

    def summary(self) -> dict[str, Any]:
        return dict(
            asdict(
                RecordedPolicyNote(
                    rows=sum(len(rows) for _times, rows in self._rows.values()),
                    packs=len(self._rows),
                    provided=self.provided,
                    requested_fallback=self.requested,
                    missing_holds=self.missing,
                    first_missing=None if self.first_missing is None else to_iso(self.first_missing),
                    first_provided=None if self.first_provided is None else to_iso(self.first_provided),
                    last_source=self.last_source,
                )
            )
        )


def recorded_policy_source(episode: Episode) -> RecordedPolicy:
    """The E4 recorded-policy source for an episode; refuses a recording with no dispatch history."""
    if not episode.dispatch:
        raise RunnerError(
            NO_DISPATCH,
            "the recording carries no dispatch history, so the recorded policy has nothing to "
            "replay: a missing setpoint must not become an engine decision",
        )
    return RecordedPolicy(episode)


def _finite_number(value: Any) -> float | None:
    """A recorded power, or ``None`` when the row does not carry one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and abs(number) != float("inf") else None


# --------------------------------------------------------------------------------------- runner


class StoreAudit:
    """The engine's store, with failed writes counted instead of only logged.

    ``Engine._store_call`` swallows a store error and logs it -- right for a live plant, wrong for
    an evidence run, where a lost plan row would quietly shrink the decisions the report judges.
    This proxy re-raises (so the engine's behaviour is unchanged) after counting the failure by
    method, and the runner hands the counts to the report.
    """

    def __init__(self, store: Any) -> None:
        self.__dict__["store"] = store
        self.__dict__["failures"] = {}

    @property
    def failures(self) -> dict[str, int]:
        return self.__dict__["failures"]

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self.__dict__["store"], name)
        if not callable(attribute):
            return attribute

        def call(*args: Any, **kwargs: Any) -> Any:
            try:
                return attribute(*args, **kwargs)
            except Exception:
                failures = self.__dict__["failures"]
                failures[name] = failures.get(name, 0) + 1
                raise

        return call


class Runner:
    """Drives one leg of one arm over one episode window."""

    def __init__(
        self,
        episode: Episode,
        *,
        name: str,
        plant: str,
        policy: str = "production",
        settings_patch: Mapping[str, Any] | None = None,
        command_source: Callable[[BatteryConfig, datetime], float | None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if plant not in MODES:
            raise RunnerError(RUNNER_INVALID, f"unknown plant {plant!r}; have {', '.join(MODES)}")
        if policy not in ("production", "recorded"):
            raise RunnerError(RUNNER_INVALID, f"unknown policy {policy!r}")
        if policy == "recorded" and command_source is None:
            # The recorded leg's own source: the recording's setpoints, or a refusal when the
            # recording has no dispatch history at all.
            command_source = recorded_policy_source(episode)
        self.episode = episode
        # Each leg starts at the window start: legs share one recording (and its as-of clock) but
        # never one another's scratch copy, so the clock has to come back before this leg drives.
        episode.rewind()
        self.name = name
        self.plant = plant
        self.policy = policy
        self._command_source = command_source
        self.patch = validated_patch(settings_patch)
        if self.patch:
            episode.settings = Settings(
                SettingsOverlay(episode.settings_values, dict(self.patch)), {}, options_path=""
            )
        self.clock = clock or CallableClock(episode)
        self.ha = CaptureHA(episode)
        self.store_audit = StoreAudit(episode.store())
        self.drivers: list[PlantDriver] = []
        self.refusals: list[Refusal] = []
        self.vintage_calls: list[VintageCall] = []
        self.tally = PlantTally()
        self.steps = 0
        self.control_steps = 0
        self.replan_steps = 0
        self.guard_steps = 0
        self._command_cursor: dict[int, int] = {}
        self._unavailable: set[int] = set()
        self._raised = 0
        self.engine = self._build_engine(command_source)
        self._seed_engine()
        self._boundaries = self._windows()
        self._window_tallies: dict[int, WindowTally] = {
            index: WindowTally(index) for index in range(len(self._boundaries))
        }
        self._buckets: set[datetime] = set()
        # The live sampler -- ``app.main``'s closure, never the engine -- persists the 5-minute
        # measurement rows, so a replay has to write them too: without them the cost-basis ledger
        # runs out of its own samples after the stall tolerance and the sell floor goes inactive.
        self._recorded_rows = self._recorded_measurements()
        self._written_measurements: dict[str, int] = {}
        self._measurement_buckets: set[datetime] = set()
        self.measurement_failures: list[Refusal] = []
        self._next_control = episode.start
        self._next_replan = episode.start
        self._next_guard = episode.start

    # -- construction ---------------------------------------------------------------

    def _build_engine(
        self, command_source: Callable[[BatteryConfig, datetime], float | None] | None
    ) -> Engine:
        create = plant_factory(self.episode, self.plant)

        def factory(config: BatteryConfig) -> BatteryDriver:
            driver: Any = create(config)
            if command_source is not None:
                driver = CommandOverrideDriver(driver, command_source)
            self.drivers = [existing for existing in self.drivers if existing.id() != driver.id()]
            self.drivers.append(driver)
            return driver

        engine = Engine(
            self.episode.settings,
            store=self.store_audit,
            ha=self.ha,
            price_source=self.price_source,
            solar_source=self.solar_source,
            load_source=self.load_source,
            sampler=self.sampler,
            driver_factory=factory,
            notifier=NoopNotifier(),
            location="replay",
            clock=self.clock,
            version=APP_VERSION,
            commit="",
        )
        # ``Engine`` builds its journal with the wall clock (there is no constructor seam for it),
        # so every later event and dispatch row is rebound to the replay instant.  The one
        # ``engine_start`` row written before this line is a constructor artefact, not a window
        # event, and ``_events()`` drops it.
        engine._journal = Journal(self.episode.store(), clock=self.clock)
        return engine

    def _seed_engine(self) -> None:
        """The warm start (docs/REPLAY_HARNESS.md section 1): the recorded state at ``start``."""
        warm = self.episode.warm_start
        states = list(self.episode.initial_battery_states())
        engine = self.engine
        engine._plan = warm.plan
        engine._state.active_plan = warm.plan
        engine._state.batteries = states
        engine._state.last_plan_at = None if warm.plan is None else warm.plan.generated_at
        # A seed without a measured energy would let ``_planning_ready`` pass on an assumed floor,
        # which is exactly what SPEC 9.1 forbids; the recorded state decides.
        engine._battery_read_ok = any(state.energy_kwh is not None for state in states)
        engine._last_sample_at = self.episode.start
        engine._last_replan_at = warm.last_replan_at
        engine._replan_backoff_s = warm.backoff_s

    # -- the virtual clock and the cadence ------------------------------------------

    def step(self, now: datetime) -> None:
        """One virtual instant: advance the episode, then dispatch the due production cadences."""
        self.episode.advance_to(now)
        control_due = now >= self._next_control
        if control_due:
            self._next_control = now + timedelta(
                seconds=float(self.engine._dispatcher.tick_interval_s())
            )
            self.engine._control_step(now)
            self.control_steps += 1
            self._buckets.add(quarter_floor(now, ACTUALS_MINUTES))
        if now >= self._next_replan:
            self._next_replan = now + timedelta(seconds=REPLAN_INTERVAL_S)
            self.engine._replan_step(now)
            self.replan_steps += 1
        if now >= self._next_guard:
            self._next_guard = now + timedelta(seconds=GUARD_INTERVAL_S)
            self.engine._guard_step(now)
            self.guard_steps += 1
        self.steps += 1
        if control_due:
            self._count_plant(now)
        self._check_refusals(now)

    def run(self) -> LegResult:
        """Drive the whole window; stops early only on a refusal."""
        start, end, step_s = self.episode.start, self.episode.end, self.episode.step_s
        index = 0
        while True:
            now = start + timedelta(seconds=step_s * index)
            if now > end:
                now = end
            try:
                self.step(now)
            except RunnerError:
                break
            if now >= end:
                break
            index += 1
        self._note_recorded_policy()
        return self.result()

    def _note_recorded_policy(self) -> None:
        """A recorded leg that never found a setpoint is not a policy comparison: refuse it."""
        if self.policy != "recorded":
            return
        summary = self._policy_notes()
        if summary and not summary.get("provided"):
            self._refuse(
                self.episode.end,
                RECORDED_POLICY_UNKNOWN,
                "the recording carries no setpoint for this pack at or before any control tick: "
                "every tick held at 0 W",
            )

    def _policy_notes(self) -> dict[str, Any]:
        summary = getattr(self._command_source, "summary", None)
        return dict(summary()) if callable(summary) else {}

    # -- counting and refusals -------------------------------------------------------

    def _count_plant(self, now: datetime) -> None:
        self.tally.ticks += 1
        window = self._window_tally(now)
        unavailable = False
        for driver in self.drivers:
            key = id(driver)
            if bool(getattr(driver, "energy_unknown", False)):
                unavailable = True
                if key not in self._unavailable:
                    self._unavailable.add(key)
                    self.tally.events.append((now, "unavailable", driver.id()))
            fresh = len(driver.commands) - self._command_cursor.get(key, 0)
            if fresh <= 0:
                continue
            self._command_cursor[key] = len(driver.commands)
            self.tally.commands += fresh
            if bool(getattr(driver, "last_infeasible", False)):
                self.tally.infeasible_commands += 1
                self.tally.events.append((now, "infeasible", driver.id()))
                if window is not None:
                    window.infeasible_commands += 1
            if bool(getattr(driver, "last_bound_unverified", False)):
                self.tally.unverified_bounds += 1
                if window is not None:
                    window.unverified_bounds += 1
        if unavailable:
            self.tally.unavailable_ticks += 1
            if window is not None:
                window.unavailable_ticks += 1

    def _window_tally(self, now: datetime) -> WindowTally | None:
        """The decision window ``now`` falls in (correction 3 attributes plant failures)."""
        if not self._boundaries:
            return None
        starts = [boundary.start for boundary in self._boundaries]
        index = min(max(bisect_right(starts, now) - 1, 0), len(self._boundaries) - 1)
        return self._window_tallies.get(index)

    def _refuse(self, instant: datetime, reason: str, detail: str) -> None:
        if any(refusal.reason == reason for refusal in self.refusals):
            return
        self.refusals.append(Refusal(instant, reason, detail))

    def _check_refusals(self, now: datetime) -> None:
        if self.ha.commands:
            self._refuse(
                now,
                FACADE_COMMAND,
                f"the facade saw {len(self.ha.commands)} call_service call(s): "
                f"{self.ha.commands[0][0]}.{self.ha.commands[0][1]}",
            )
        if len(self.refusals) > self._raised:
            refusal = self.refusals[self._raised]
            self._raised = len(self.refusals)
            raise RunnerError(refusal.reason, f"{refusal.detail} at {to_iso(refusal.instant)}")

    # -- episode-backed sources ------------------------------------------------------

    def _horizon_end(self, now: datetime) -> datetime:
        hours = int(
            clamp(float(self.episode.settings.get("optimizer.horizon_hours", 30) or 30), 12, 48)
        )
        return now + timedelta(hours=hours)

    def _slot_minutes(self) -> int:
        return max(1, int(self.episode.settings.get("optimizer.slot_minutes", 60) or 60))

    def _vintage(self, now: datetime, kind: str) -> tuple[dict[datetime, Any], tuple[str, ...], datetime] | None:
        """One series' as-issued points, which sources contributed them, and when."""
        snapshot = self.episode.at(now)
        plan = snapshot.plan()
        points: dict[datetime, Any] = {}
        sources: list[str] = []
        issued: datetime | None = None
        if plan is not None and plan.slots:
            if kind == "price":
                points = {slot.t: (float(slot.buy), float(slot.sell)) for slot in plan.slots}
            else:
                attribute = "pv_kw" if kind == "solar" else "load_kw"
                points = {slot.t: float(getattr(slot, attribute)) for slot in plan.slots}
            sources.append("plan_vintage")
            issued = plan.generated_at
        if kind != "price":
            # The engine archives solar and load (``_archive_forecast``), never price: a price row
            # carries one value, so its sell side would have to be invented.
            for row in snapshot.forecasts():
                if str(row.get("kind")) != kind:
                    continue
                target = from_iso(row.get("target_ts"))
                value = row.get("value")
                if target is None or value is None or target in points:
                    continue
                points[target] = float(value)
                moment = from_iso(row.get("issued_at"))
                if moment is not None and (issued is None or moment > issued):
                    issued = moment
                if "archive" not in sources:
                    sources.append("archive")
        if not points:
            self._refuse(
                now,
                NO_VINTAGE,
                f"no {kind} vintage issued at or before {to_iso(now)}: neither a recorded plan "
                "nor an archived forecast covers this instant",
            )
            return None
        classic = sorted(points)
        last_t = classic[-1]
        # Never invent a slot: the vintage ends where the recording's own information ends, and
        # ``align_slots`` forward-fills the rest as ``price_stale`` -- the same partial input the
        # production planner would have solved on.  Only the *count* of uncovered solver slots is
        # reported, so a report can say how much of the horizon was carried by a stale tail.
        horizon_end = self._horizon_end(now)
        minutes = self._slot_minutes()
        uncovered = 0
        moment = quarter_floor(now, minutes)
        while moment < horizon_end:
            if moment > last_t:
                uncovered += 1
            moment += timedelta(minutes=minutes)
        self.vintage_calls.append(
            VintageCall(now, kind, tuple(sources), uncovered, horizon_end)
        )
        return points, tuple(sources), (issued or now)

    def price_source(self, now: datetime) -> PriceForecast | None:
        vintage = self._vintage(now, "price")
        if vintage is None:
            return None
        points, sources, issued = vintage
        forecast = PriceForecast(
            slots=[
                PriceSlot(t=moment, buy=float(value[0]), sell=float(value[1]), source="replay")
                for moment, value in sorted(points.items())
            ],
            generated_at=issued,
            source="replay:" + "+".join(sources),
        )
        # Stage-3 R6: the vintage is deliberately left **unprovenanced**, exactly like any other
        # caller-built forecast.  Provenance is a fact only whoever resolved the tariff can record;
        # matching prices cannot establish it (0.16/0.22 buys with 0.14/0.20 sells fit the derived
        # rule *and* are equally two explicit rates), so a model-fit classification stamped here would
        # be an inference wearing the name of a declaration -- and it would skip the both-readings
        # comparison the tail prices under.  Recording the contract model for a replay is a separate,
        # deferred question; until then an unprovenanced vintage takes the engine's own two-step path:
        # one supported model must fit the window, and the tail is priced under both readings.  E3 is
        # unaffected (its tail is entirely window-filled, so the readings coincide), while a vintage
        # that later gains published tail entries falls under the comparison instead of being trusted.
        return forecast

    def solar_source(self, now: datetime) -> SolarForecast | None:
        vintage = self._vintage(now, "solar")
        if vintage is None:
            return None
        points, sources, issued = vintage
        return SolarForecast(
            # ``plan_slots`` carries no uncertainty: 0 is "not recorded", never "certain".
            slots=[SolarSlot(t=moment, pv_kw=value, uncertainty_pct=0.0) for moment, value in sorted(points.items())],
            generated_at=issued,
            source="replay:" + "+".join(sources),
        )

    def load_source(self, now: datetime) -> LoadForecast | None:
        vintage = self._vintage(now, "load")
        if vintage is None:
            return None
        points, sources, issued = vintage
        return LoadForecast(
            slots=[LoadSlot(t=moment, load_kw=value) for moment, value in sorted(points.items())],
            generated_at=issued,
            source="replay:" + "+".join(sources),
            confidence="low",
        )

    def sampler(self, now: datetime) -> dict[str, Any]:
        """The tick's measured values in W: the exogenous sites plus this arm's own battery flow.

        The recorded arm answers the recording's own grid measurement; the simulated arm rebuilds
        ``load - pv + sum(realized pack power)`` from the plant, which is the same convention
        ``plant.PlantFleet.grid_kw`` uses.  The base comes from :meth:`Episode.base_at`, which labels
        its provenance and refuses a bucket whose derived pair failed the recording's identity
        check; an unknown base or pack flow omits ``pv_w``/``load_w``/``grid_w`` rather than writing
        a number the recording never carried.
        """
        snapshot = self.episode.at(now)
        values: dict[str, Any] = {}
        base = self.episode.base_at(now)
        if base is not None:
            values["pv_w"] = base.pv_kw * 1000.0
            values["load_w"] = base.load_kw * 1000.0
        if self.plant == RECORDED:
            grid = snapshot.latest("grid_kw")
            if grid is not None and grid[1] is not None:
                values["grid_w"] = float(grid[1]) * 1000.0
            self._write_measurements(now, values.get("grid_w"))
            return values
        total = self._total_power_w()
        if base is not None and total is not None:
            values["grid_w"] = (base.base_kw + total / 1000.0) * 1000.0
        self._write_measurements(now, values.get("grid_w"))
        return values

    def _recorded_measurements(self) -> dict[tuple[datetime, str], tuple[float, str]]:
        """The recording's own in-window measurement rows, keyed ``(bucket, role)``.

        The recorded arm re-persists these verbatim -- they are what the live sampler wrote -- while
        the simulated arm replaces them with the plant's own flows, because an arm's rows must
        describe the arm.  A row with no value carries no sample and is dropped, never zeroed.
        """
        rows: dict[tuple[datetime, str], tuple[float, str]] = {}
        for row in self.episode.boundary.source_rows.get("measurements", ()) or ():
            when = row.get("ts")
            if isinstance(when, str):
                try:
                    when = from_iso(when)
                except ValueError:
                    continue
            role = str(row.get("entity_role") or row.get("role") or "")
            value = row.get("value")
            if not isinstance(when, datetime) or not role or value is None:
                continue
            try:
                rows[(when, role)] = (float(value), str(row.get("unit") or ""))
            except (TypeError, ValueError):
                continue
        return rows

    def _write_measurements(self, now: datetime, grid_w: Any) -> None:
        """Persist this tick's measurement rows, exactly as the live sampler closure does.

        Bucketed by :data:`app.sources.measure.MEASUREMENT_MINUTES`, through the same
        ``store.add_measurements`` the live path calls.  A failed write is recorded as a refusal and
        the tick continues: the run stays honest about what it could not persist, and the stall the
        ledger reports afterwards is attributed instead of hidden.
        """
        bucket = quarter_floor(now, MEASUREMENT_MINUTES)
        rows = self._measurement_rows(bucket, None if grid_w is None else float(grid_w))
        if not rows:
            return
        try:
            self.episode.store().add_measurements(rows)
        except Exception as exc:  # noqa: BLE001 - the live closure logs and carries on, so does this
            self.measurement_failures.append(
                Refusal(now, MEASUREMENT_WRITE_FAILED, f"measurement write failed: {exc}")
            )
            return
        self._measurement_buckets.add(bucket)
        for row in rows:
            self._written_measurements[row.role] = self._written_measurements.get(row.role, 0) + 1

    def _measurement_rows(self, bucket: datetime, grid_w: float | None) -> list[Measurement]:
        """The rows the live sampler would have written for ``bucket``, from this arm's own values.

        Both arms write the roles the ledger actually reads -- the ``battery_charge``/
        ``battery_discharge`` pair, ``battery_soc`` and the site ``grid_power``, all of which
        :func:`app.cost_basis.measurement_roles` lists -- so capacity learning and the cost-basis
        ledger keep the same in-window evidence they had live.  A quantity this arm cannot know is
        omitted, never guessed.
        """
        if self.plant == RECORDED:
            return [
                Measurement(ts=bucket, role=role, value=value, unit=unit, entity_id="replay")
                for (when, role), (value, unit) in sorted(self._recorded_rows.items())
                if when == bucket
            ]
        rows: list[Measurement] = []
        for driver in self.drivers:
            config = getattr(driver, "config", None)
            if config is None:
                continue
            known = set(measurement_roles(config.id))
            realized = self._driver_value(driver, "realized_power")
            if realized is not None:
                for base, value in (
                    ("battery_charge", max(float(realized), 0.0)),
                    ("battery_discharge", max(-float(realized), 0.0)),
                ):
                    role = f"{base}:{config.id}"
                    if role in known:
                        rows.append(
                            Measurement(ts=bucket, role=role, value=value, unit="W", entity_id="replay")
                        )
            state = self._driver_value(driver, "read_state")
            soc = getattr(state, "soc_pct", None) if state is not None else None
            role = f"battery_soc:{config.id}"
            if soc is not None and role in known:
                rows.append(
                    Measurement(ts=bucket, role=role, value=float(soc), unit="%", entity_id="replay")
                )
        if grid_w is not None:
            rows.append(
                Measurement(ts=bucket, role="grid_power", value=float(grid_w), unit="W", entity_id="replay")
            )
        return rows

    @staticmethod
    def _driver_value(driver: Any, name: str) -> Any:
        """One driver read, never a crash: a plant that cannot answer contributes no row."""
        method = getattr(driver, name, None)
        if not callable(method):
            return None
        try:
            return method()
        except Exception:  # noqa: BLE001
            return None

    def _total_power_w(self) -> float | None:
        """The site's realised pack flow, or ``None`` as soon as one pack's flow is unknown."""
        if not self.drivers:
            return None
        powers = [driver.realized_power() for driver in self.drivers]
        if any(power is None for power in powers):
            return None
        return float(sum(power for power in powers if power is not None))

    # -- the leg's own record --------------------------------------------------------

    def _events(self) -> tuple[Mapping[str, Any], ...]:
        """The window's journal, chronological: the replay's own rows only.

        The scratch copy still holds the recording's *pre-window* history (the cut only removes rows
        at or after the boundary), so a row is the replay's when its stamp is at or after the window
        start -- which is also how the cut's own NULL rule reads.  Two known rows never belong to a
        replay: the engine constructor journals ``engine_start`` on the wall clock before any seam
        exists, and ``engine_running``/``engine_stopped`` only ever come from ``Engine.start()`` and
        ``Engine.stop()``, which a replay never calls (so a run that grew a thread would show up
        here rather than pass unnoticed).
        """
        rows = self.episode.store().events(limit=200_000)
        start = to_iso(self.episode.start)
        kept = [
            {
                "ts": row.get("ts"),
                "level": row.get("level"),
                "code": row.get("code"),
                "message": row.get("message"),
                "payload": row.get("payload_json"),
            }
            for row in rows
            if str(row.get("code") or "") not in ("engine_start", "engine_running", "engine_stopped")
            and isinstance(row.get("ts"), str)
            and str(row.get("ts")) >= start
        ]
        kept.reverse()
        return tuple(kept)

    def _windows(self) -> tuple[WindowBoundary, ...]:
        """The recorded decision windows: the plan in force until the next recorded replan."""
        milestones = {self.episode.start, self.episode.end}
        for plan in self.episode.plans:
            if self.episode.start < plan.generated_at < self.episode.end:
                milestones.add(plan.generated_at)
        ordered = sorted(milestones)
        return tuple(
            WindowBoundary(index, ordered[index], ordered[index + 1])
            for index in range(len(ordered) - 1)
        )

    def result(self) -> LegResult:
        return LegResult(
            name=self.name,
            plant=self.plant,
            policy=self.policy,
            patch=self.patch,
            start=self.episode.start,
            end=self.episode.end,
            step_s=self.episode.step_s,
            steps=self.steps,
            control_steps=self.control_steps,
            replan_steps=self.replan_steps,
            guard_steps=self.guard_steps,
            policy_notes=self._policy_notes(),
            refusals=tuple(self.refusals),
            tally=self.tally,
            initial_battery_states=tuple(self.episode.initial_battery_states()),
            final_battery_states=tuple(
                driver.read_state() for driver in self.drivers
            ),
            windows=self._boundaries,
            window_tallies=tuple(
                self._window_tallies[index] for index in sorted(self._window_tallies)
            ),
            vintage_calls=tuple(self.vintage_calls),
            buckets_written=tuple(sorted(self._buckets)),
            measurement_rows=tuple(sorted(self._written_measurements.items())),
            measurement_buckets=tuple(sorted(self._measurement_buckets)),
            measurement_failures=tuple(self.measurement_failures),
            facade=self.ha.summary(),
            events=self._events(),
            store_failures={
                name: count for name, count in sorted(self.store_audit.failures.items()) if count
            },
            store=self.episode.store(),
            episode=self.episode,
        )


def run_leg(
    episode: Episode,
    *,
    name: str,
    plant: str,
    policy: str = "production",
    settings_patch: Mapping[str, Any] | None = None,
    command_source: Callable[[BatteryConfig, datetime], float | None] | None = None,
) -> LegResult:
    """Build a runner and drive its whole window (the CLI's one call per leg)."""
    runner = Runner(
        episode,
        name=name,
        plant=plant,
        policy=policy,
        settings_patch=settings_patch,
        command_source=command_source,
    )
    return runner.run()
