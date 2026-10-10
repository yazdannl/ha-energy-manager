"""Control tick loop (SPEC section 9.1).

One tick: read the drivers, take the current plan target (interpolated across the slot boundary),
apply the optional grid-follow correction, ask ``safety.check``, allocate per battery, clamp the
per-battery ramp, write through the drivers and journal *every* attempt -- including rejections
and idempotent skips, so "why didn't it charge?" always has an answer.

The class is thread-agnostic: the engine owns the threads and calls ``tick``/``watchdog``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping, Protocol, Sequence

from ..models import (
    BatteryConfig,
    BatteryState,
    DispatchResult,
    Mode,
    Plan,
    PlanSlot,
    Reason,
    SystemState,
    clamp,
    utcnow,
)
from ..optimize.allocate import allocate_power, headroom_w
from . import safety
from .drivers.base import BatteryDriver
from .safety import RAMP_LIMIT_W

log = logging.getLogger(__name__)

#: Grid-follow budget when ``control.grid_follow_max_w`` is not configured: a share of the
#: batteries' own capability, never a fraction of the current target (audit finding 11).
GRID_FOLLOW_MAX_RATIO = 0.5
GRID_FOLLOW_MIN_BUDGET_W = 500.0

#: How long after a slot boundary the power is interpolated from the previous slot's value.
TRANSITION_SECONDS = 120.0


class Journal(Protocol):
    """Where dispatch attempts and events go (engine implements it over the store + rings)."""

    def dispatch(self, entry: dict[str, Any]) -> None: ...

    def event(
        self, level: str, code: str, message: str, payload: dict[str, Any] | None = None
    ) -> None: ...


@dataclass
class TickOutcome:
    ts: datetime
    mode: str
    dry_run: bool
    plan_power_w: float | None = None
    target_w: float = 0.0
    allowed_w: float = 0.0
    grid_follow_w: float = 0.0
    block: bool = False
    reasons: list[str] = field(default_factory=list)
    results: list[DispatchResult] = field(default_factory=list)
    battery_states: list[BatteryState] = field(default_factory=list)
    measurements: dict[str, Any] = field(default_factory=dict)
    #: Set when the active plan was refused as stale (SPEC 9.1/Reason.STALE_INPUT).
    stale_input: str | None = None

    @property
    def writes(self) -> int:
        return sum(1 for result in self.results if result.writes)


class Dispatcher:
    def __init__(
        self,
        settings: Any,
        journal: Journal,
        drivers: Mapping[str, BatteryDriver],
        *,
        clock=utcnow,
    ) -> None:
        self._settings = settings
        self._journal = journal
        self._drivers = dict(drivers)
        self._clock = clock
        self._last_tick: datetime | None = None

    # -- loop helpers -----------------------------------------------------------------

    def tick_interval_s(self) -> float:
        return max(1.0, float(self._settings.get("control.tick_seconds", 30) or 30))

    def replan_interval_s(self) -> float:
        return max(60.0, float(self._settings.get("control.replan_minutes", 15) or 15) * 60.0)

    def _reserve_soc_pct(self) -> float:
        """SPEC 3 ``optimizer.reserve_soc_pct``: a per-battery floor above ``min_soc_pct``."""
        return float(self._settings.get("optimizer.reserve_soc_pct", 0.0) or 0.0)

    def note_tick(self, moment: datetime) -> None:
        self._last_tick = moment

    def stalled(self, now: datetime | None = None) -> bool:
        """True when the control thread has not ticked within three tick intervals."""
        if self._last_tick is None:
            return False
        moment = now or self._clock()
        return (moment - self._last_tick).total_seconds() > 3.0 * self.tick_interval_s()

    def watchdog(self, now: datetime | None = None) -> list[DispatchResult]:
        """Fail-safe hold for all drivers after a stalled loop (SPEC 9.1 step 5 / R4)."""
        moment = now or self._clock()
        results: list[DispatchResult] = []
        for driver in self._ordered_drivers(None):
            result = driver.hold(dry_run=False)
            results.append(result)
            self._journal.dispatch(
                self._entry(result, mode=Mode.AUTO.value, dry_run=False, reason=Reason.STALE_STATE)
            )
        self._journal.event(
            "error",
            "watchdog_hold",
            f"control loop stalled since {self._last_tick}; holding all batteries",
            {"last_tick": self._last_tick.isoformat() if self._last_tick else None},
        )
        self._last_tick = moment
        return results

    def _stale_input_reason(
        self, plan: Plan | None, slot: PlanSlot | None, moment: datetime
    ) -> str | None:
        """Why the active plan may not be executed, or None (audit finding 2)."""
        if plan is None or slot is None:
            return None
        bound = max(
            2.0 * self.replan_interval_s(),
            float(self._settings.get("control.stale_plan_seconds", 0) or 0.0),
        )
        age = (moment - plan.generated_at).total_seconds()
        if age > bound:
            return f"plan is {age / 60.0:.0f} min old (> {bound / 60.0:.0f} min)"
        if slot.price_stale:
            return "the slot's price forecast is stale"
        return None

    def read_batteries(
        self, moment: datetime, batteries: Sequence[BatteryConfig]
    ) -> list[BatteryState]:
        """One live read per configured battery (SPEC 9.1 step 1).  Never raises."""
        states: list[BatteryState] = []
        for driver in self._ordered_drivers(batteries):
            try:
                states.append(driver.read_state())
            except Exception as exc:  # pragma: no cover - read_state must not raise
                log.warning("driver %s read failed: %s", driver.id(), exc)
                states.append(
                    BatteryState(id=driver.id(), available=False, read_at=moment, stale=True)
                )
        return states

    def needs_replan(
        self,
        state: SystemState,
        plan: Plan | None,
        batteries: Sequence[BatteryConfig],
        now: datetime | None = None,
        *,
        since_last_s: float | None = None,
    ) -> str | None:
        """Why the plan is stale, or None (SPEC 9.1 step 2)."""
        moment = now or self._clock()
        if plan is None:
            return "no_plan"
        age = (moment - plan.generated_at).total_seconds()
        if age > self.replan_interval_s():
            return "plan_age"
        if since_last_s is not None and since_last_s < 60.0:
            return None
        slot = plan.slot_at(moment)
        if slot is None:
            return "plan_expired"
        capacity = sum(c.capacity_kwh for c in batteries if c.enabled)
        if capacity > 0:
            live = sum(
                (b.energy_kwh if b.energy_kwh is not None else 0.0)
                for b in state.batteries
                if b.available
            )
            if abs(live - slot.soc_kwh) / capacity * 100.0 > 3.0:
                return "soc_deviation"
        return None

    # -- the tick ---------------------------------------------------------------------

    def tick(
        self,
        now: datetime | None = None,
        *,
        state: SystemState,
        plan: Plan | None,
        batteries: Sequence[BatteryConfig],
        measurements: Mapping[str, Any] | None = None,
        battery_states: Sequence[BatteryState] | None = None,
    ) -> TickOutcome:
        moment = now or self._clock()
        snapshot = dict(measurements or {})
        outcome = TickOutcome(ts=moment, mode=str(state.mode), dry_run=bool(state.dry_run))
        outcome.measurements = snapshot

        # 1. Live battery readings come from the drivers themselves (measurement sampling of the
        #    household sensors happens in the engine, before this call, and is never skipped).
        #    The engine may hand in states it already read while warming up, so a tick never reads
        #    a battery twice.
        if battery_states is None:
            outcome.battery_states.extend(self.read_batteries(moment, batteries))
        else:
            outcome.battery_states.extend(battery_states)
        live_by_id = {b.id: b for b in outcome.battery_states}
        state.batteries = list(outcome.battery_states)

        slot = plan.slot_at(moment) if plan is not None else None
        outcome.stale_input = self._stale_input_reason(plan, slot, moment)
        if outcome.stale_input is not None:
            # A plan older than the bound, or one whose prices went stale, must not keep steering
            # the hardware: hold instead and say why (audit finding 2).
            outcome.reasons.append(Reason.STALE_INPUT)
            slot = None
        # No usable slot (no plan, expired plan, or a stale one that must not execute) means
        # "nothing to follow", reported as None -- never as a 0 W plan power.
        outcome.plan_power_w = self._slot_power(plan, moment) if slot is not None else None

        # 2. Grid follow: correct the plan towards the measured grid flow.
        usable_configs = [
            c
            for c in sorted((c for c in batteries if c.enabled), key=lambda c: c.id)
            if self._usable(live_by_id.get(c.id), moment)
        ]
        target = float(outcome.plan_power_w or 0.0)
        capability_w = sum(
            max(config.max_charge_w, config.max_discharge_w) for config in usable_configs
        )
        corrected = self._grid_follow(target, slot, snapshot, state, capability_w)
        if abs(corrected - target) > 1e-6:
            outcome.grid_follow_w = corrected - target
        target = corrected
        outcome.target_w = target

        # 3. Guard rails.
        decision = safety.check(target, slot, batteries, state, self._settings, moment)
        outcome.allowed_w = decision.allowed_w
        outcome.block = decision.block
        outcome.reasons = list(decision.reasons)

        if decision.block and str(state.mode) != Mode.AUTO.value:
            # Advisory/off: nothing is written, but the log still explains the decision for every
            # configured battery.
            for config in sorted((c for c in batteries if c.enabled), key=lambda c: c.id):
                result = DispatchResult(
                    battery_id=config.id,
                    ts=moment,
                    requested_w=target,
                    applied_w=0.0,
                    ok=True,
                    dry_run=bool(state.dry_run),
                    code=decision.reasons[0],
                    message="; ".join(decision.reasons),
                )
                outcome.results.append(result)
                self._journal.dispatch(
                    self._entry(
                        result,
                        mode=str(state.mode),
                        dry_run=bool(state.dry_run),
                        reason=decision.reasons[0],
                        soc_pct=self._soc_of(config, live_by_id.get(config.id)),
                    )
                )
            self.note_tick(moment)
            return outcome

        # Batteries that were excluded (stale reading, unavailable device) still get an explicit
        # rejection row, so "why didn't it charge?" is answerable for them too.
        usable_ids = {config.id for config in usable_configs}
        for config in sorted((c for c in batteries if c.enabled), key=lambda c: c.id):
            if config.id in usable_ids:
                continue
            live = live_by_id.get(config.id)
            code = Reason.BATTERY_UNAVAILABLE
            if live is not None and live.available:
                code = Reason.STALE_STATE
            result = DispatchResult(
                battery_id=config.id,
                ts=moment,
                requested_w=target,
                applied_w=0.0,
                ok=True,
                dry_run=bool(state.dry_run),
                code=code,
                message=f"battery excluded from this tick: {code}",
            )
            outcome.results.append(result)
            self._journal.dispatch(
                self._entry(
                    result,
                    mode=str(state.mode),
                    dry_run=bool(state.dry_run),
                    reason=code,
                    soc_pct=self._soc_of(config, live),
                )
            )

        # 4. Per-battery split of the approved aggregate power, then the per-battery ramp clamp.
        override = safety.active_override(state, moment)
        if decision.block:
            entries = [(config, 0.0) for config in usable_configs]
        elif override and override.get("battery_id"):
            pinned = str(override.get("battery_id"))
            entries = []
            for config in usable_configs:
                if config.id != pinned:
                    entries.append((config, 0.0))
                    continue
                live = live_by_id.get(config.id)
                # An override (and a UI test discharge, which is an override) is still bounded by
                # this battery's own floor/ceiling/reserve: pinning must not spend energy it does
                # not have (audit finding 8).
                room = headroom_w(
                    config,
                    live.energy_kwh if live is not None else None,
                    charging=decision.allowed_w > 0,
                    slot_hours=self.tick_interval_s() / 3600.0,
                    reserve_soc_pct=self._reserve_soc_pct(),
                )
                entries.append((config, clamp(decision.allowed_w, -room, room)))
        else:
            entries = allocate_power(
                decision.allowed_w,
                usable_configs,
                {b.id: b.energy_kwh for b in outcome.battery_states},
                slot_hours=self.tick_interval_s() / 3600.0,
                reserve_soc_pct=self._reserve_soc_pct(),
            )

        for config, power in entries:
            live = live_by_id.get(config.id)
            previous = float(live.power_w or 0.0) if live is not None else 0.0
            applied_target = clamp(power, previous - RAMP_LIMIT_W, previous + RAMP_LIMIT_W)
            applied_target = clamp(applied_target, -config.max_discharge_w, config.max_charge_w)
            driver = self._drivers.get(config.id)
            if driver is None:
                result = DispatchResult(
                    battery_id=config.id,
                    ts=moment,
                    requested_w=target,
                    applied_w=0.0,
                    ok=False,
                    dry_run=bool(state.dry_run),
                    code=Reason.BATTERY_UNAVAILABLE,
                    message="no driver for this battery",
                )
            else:
                result = driver.apply(applied_target, dry_run=bool(state.dry_run))
                if not result.ok:
                    self._journal.event(
                        "warning",
                        "driver_error",
                        f"{config.id}: {result.message}",
                        {"requested_w": result.requested_w, "code": result.code},
                    )
                    if (
                        not state.dry_run
                        and str(state.mode) == Mode.AUTO.value
                        and bool(self._settings.get("control.failsafe_on_error", True))
                    ):
                        # Unrecoverable driver error: fall back to a neutral write, once.
                        hold = driver.hold(dry_run=False)
                        self._journal.dispatch(
                            self._entry(
                                hold,
                                mode=str(state.mode),
                                dry_run=False,
                                reason=Reason.DRIVER_ERROR,
                            )
                        )
            outcome.results.append(result)
            self._journal.dispatch(
                self._entry(
                    result,
                    mode=str(state.mode),
                    dry_run=bool(state.dry_run),
                    reason=decision.reasons[0] if decision.reasons else result.code,
                    soc_pct=self._soc_of(config, live_by_id.get(config.id)),
                )
            )

        if outcome.stale_input and Reason.STALE_INPUT not in outcome.reasons:
            # The safety decision owns ``reasons``; keep the stale-input refusal visible too.
            outcome.reasons.append(Reason.STALE_INPUT)
        self.note_tick(moment)
        return outcome


    # -- internals --------------------------------------------------------------------

    def _ordered_drivers(self, batteries: Sequence[BatteryConfig] | None) -> list[BatteryDriver]:
        if not batteries:
            return [self._drivers[key] for key in sorted(self._drivers)]
        order = [c.id for c in sorted(batteries, key=lambda c: c.id)]
        return [self._drivers[battery_id] for battery_id in order if battery_id in self._drivers]

    def _usable(self, live: BatteryState | None, now: datetime) -> bool:
        if live is None or not live.available:
            return False
        limit = float(self._settings.get("control.stale_state_seconds", 180) or 180)
        age = live.age_seconds(now)
        return not live.stale and age is not None and age <= limit

    def _soc_of(self, config: BatteryConfig, live: BatteryState | None) -> float | None:
        if live is None:
            return None
        if live.soc_pct is not None:
            return float(live.soc_pct)
        if live.energy_kwh is not None and config.capacity_kwh > 0:
            return clamp(live.energy_kwh / config.capacity_kwh * 100.0, 0.0, 100.0)
        return None

    def _slot_power(self, plan: Plan | None, now: datetime) -> float | None:
        """Plan power for the current slot, interpolated over the slot boundary."""
        if plan is None:
            return None
        slot = plan.slot_at(now)
        if slot is None:
            return None
        power = float(slot.power_w)
        elapsed = (now - slot.t).total_seconds()
        if elapsed <= TRANSITION_SECONDS:
            previous = self._previous_slot(plan, slot)
            if previous is not None and abs(previous.power_w - power) > 1e-6:
                fraction = clamp(elapsed / TRANSITION_SECONDS, 0.0, 1.0)
                return float(previous.power_w) + (power - previous.power_w) * fraction
        return power

    @staticmethod
    def _previous_slot(plan: Plan, slot: PlanSlot) -> PlanSlot | None:
        step = timedelta(minutes=plan.slot_minutes)
        for candidate in plan.slots:
            if candidate.t == slot.t - step:
                return candidate
        return None

    def _grid_follow(
        self,
        target_w: float,
        slot: PlanSlot | None,
        measurements: Mapping[str, Any],
        state: SystemState,
        capability_w: float = 0.0,
    ) -> float:
        if not bool(self._settings.get("control.grid_follow", True)) or slot is None:
            return target_w
        measured = measurements.get("grid_w")
        if measured is None:
            measured = state.grid_now_w
        if measured is None:
            return target_w
        planned_grid_kw = slot.load_kw - slot.pv_kw + target_w / 1000.0
        delta = -(float(measured) / 1000.0 - planned_grid_kw) * 1000.0
        deadband = float(self._settings.get("control.grid_follow_deadband_w", 30) or 30)
        if abs(delta) <= deadband:
            return target_w
        # The correction is bounded by an independent watt budget, not by a fraction of the plan:
        # with a planned 0 W that fraction was 0, so no deviation could ever be corrected
        # (audit finding 11).
        budget = float(self._settings.get("control.grid_follow_max_w", 0) or 0.0)
        if budget <= 0:
            budget = max(GRID_FOLLOW_MIN_BUDGET_W, capability_w * GRID_FOLLOW_MAX_RATIO)
        return target_w + clamp(delta, -budget, budget)

    @staticmethod
    def _entry(
        result: DispatchResult,
        *,
        mode: str,
        dry_run: bool,
        reason: str,
        soc_pct: float | None = None,
    ) -> dict[str, Any]:
        outcome = "ok"
        if not result.ok:
            outcome = "error"
        elif result.code == Reason.IDEMPOTENT_SKIP:
            outcome = "skipped"
        elif dry_run:
            outcome = "dry_run"
        return {
            "ts": result.ts,
            "battery_id": result.battery_id,
            "requested_w": result.requested_w,
            "applied_w": result.applied_w,
            "soc_pct": soc_pct,
            "mode": mode,
            "dry_run": dry_run,
            "result": outcome,
            "code": result.code,
            "message": result.message,
            "reason": reason,
            "writes": list(result.writes),
        }
