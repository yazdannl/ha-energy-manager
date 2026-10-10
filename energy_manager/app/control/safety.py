"""Guard rails in front of every battery write (SPEC section 9.3).

Pure: no I/O, no clock reads (``now`` is passed in), no module-level mutable state, no mutation of
the caller's objects.  ``check`` enforces the clauses of SPEC 9.3 in order and returns the power
that may actually be written plus the stable reason slugs for the decision log.

The decision is an aggregate over the *usable* batteries: batteries that are disabled,
unavailable or stale are excluded (and reported in ``blocked_ids``); no write at all happens when
none is usable.  The dispatcher then allocates the approved power per battery and applies the
per-battery ramp clamp.

Ponytail: the ramp clause uses the summed per-battery budget of 1000 W/tick, so the aggregate
guarantee is exact only when one battery is connected; the dispatcher clamps each battery
individually as well.  Upgrade path: move allocation in front of ``check`` and return a
per-battery decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from ..models import (
    BatteryConfig,
    BatteryState,
    Mode,
    PlanSlot,
    Reason,
    SystemState,
    clamp,
    from_iso,
    utcnow,
)

#: Per-battery AC power step limit per tick (SPEC 9.3 clause 8).
RAMP_LIMIT_W = 1000.0
EPS_W = 0.5


@dataclass(frozen=True)
class SafetyDecision:
    """What may be written, and why (SPEC 9.3)."""

    allowed_w: float
    clamped: bool
    reasons: list[str]
    block: bool
    blocked_ids: list[str] = field(default_factory=list)
    usable_ids: list[str] = field(default_factory=list)
    dry_run: bool = False

    def __bool__(self) -> bool:  # a non-blocked, non-zero decision is "true"
        return not self.block


def _energy_kwh(config: BatteryConfig, state: BatteryState | None) -> float | None:
    if state is None:
        return None
    if state.energy_kwh is not None:
        return float(state.energy_kwh)
    if state.soc_pct is not None:
        return config.capacity_kwh * float(state.soc_pct) / 100.0
    return None


def _age_seconds(state: BatteryState, now: datetime) -> float | None:
    if state.read_at is None:
        return None
    return (now - state.read_at).total_seconds()


def active_override(state: SystemState, now: datetime) -> dict[str, Any] | None:
    """The override pin when it is live, else None.  Pure; expiry is evaluated against ``now``."""
    override = state.override or {}
    if not override:
        return None
    expires = override.get("expires_at")
    if expires is None:
        return dict(override)
    moment = expires if isinstance(expires, datetime) else from_iso(str(expires))
    if moment is None or moment <= now:
        return None
    return dict(override)


def check(
    requested_w: float,
    plan_slot: PlanSlot | None,
    batteries: Sequence[BatteryConfig],
    state: SystemState,
    settings: Any,
    now: datetime | None = None,
) -> SafetyDecision:
    """Apply SPEC 9.3 and return the write that is allowed (``block`` = nothing)."""
    moment = now or utcnow()
    get = settings.get

    def decision(
        allowed_w: float,
        clamped: bool,
        reasons: Sequence[str],
        block: bool,
        blocked: Sequence[str] = (),
        usable: Sequence[str] = (),
    ) -> SafetyDecision:
        unique: list[str] = []
        for reason in reasons:
            if reason and reason not in unique:
                unique.append(reason)
        return SafetyDecision(
            allowed_w=float(allowed_w),
            clamped=bool(clamped),
            reasons=unique or [Reason.OK],
            block=bool(block),
            blocked_ids=list(blocked),
            usable_ids=list(usable),
            dry_run=bool(state.dry_run),
        )

    def blocked(reasons: Sequence[str], blocked_ids: Sequence[str] = ()) -> SafetyDecision:
        return decision(0.0, False, reasons, True, blocked_ids)

    # 1. Authority: only `auto` may write.  `off` is the kill switch (SPEC 9.4).
    if str(getattr(state, "mode", "")) != Mode.AUTO.value:
        return blocked([Reason.MODE_NOT_AUTO])

    # 2. Override / kill switch: an active override pins the power (still subject to every limit).
    override = active_override(state, moment)
    requested = float(requested_w)
    reasons: list[str] = []
    if override is not None:
        target = float(override.get("power_w") or 0.0)
        reasons.append(Reason.OVERRIDE_ACTIVE)
    else:
        target = requested

    # 3./4. Usable batteries: connected, available and fresh.
    enabled = sorted((c for c in batteries if c.enabled), key=lambda c: c.id)
    states = {s.id: s for s in getattr(state, "batteries", []) or []}
    if not enabled:
        # The block is the same either way; reporting both causes keeps the more actionable one
        # (HA is down) visible even on an install with no batteries configured yet.
        reasons = [Reason.BATTERY_UNAVAILABLE]
        if not state.ha_connected:
            reasons.append(Reason.HA_DISCONNECTED)
        return blocked(reasons)
    if not state.ha_connected:
        return blocked([Reason.HA_DISCONNECTED], [c.id for c in enabled])

    stale_after = float(get("control.stale_state_seconds", 180) or 180)
    blocked_ids: list[str] = []
    usable: list[tuple[BatteryConfig, BatteryState]] = []
    for config in enabled:
        live = states.get(config.id)
        if live is None or not live.available:
            blocked_ids.append(config.id)
            reasons.append(Reason.BATTERY_UNAVAILABLE)
            continue
        age = _age_seconds(live, moment)
        if live.stale or age is None or age > stale_after:
            blocked_ids.append(config.id)
            reasons.append(Reason.STALE_STATE)
            continue
        usable.append((config, live))
    if not usable:
        return blocked(reasons, blocked_ids)

    usable_ids = [config.id for config, _ in usable]
    tick_seconds = float(get("control.tick_seconds", 30) or 30)
    dt_hours = max(tick_seconds, 1.0) / 3600.0

    reserve = float(get("optimizer.reserve_soc_pct", 0.0) or 0.0)
    e_min = sum(c.capacity_kwh * max(c.min_soc_pct, reserve) / 100.0 for c, _ in usable)
    e_max = sum(c.capacity_kwh * c.max_soc_pct / 100.0 for c, _ in usable)
    energies = [_energy_kwh(c, s) for c, s in usable]
    e_now = sum(
        energy if energy is not None else c.capacity_kwh * c.min_soc_pct / 100.0
        for (c, _), energy in zip(usable, energies)
    )
    eta_chg = sum(c.capacity_kwh * c.eta_charge for c, _ in usable) / max(
        1e-9, sum(c.capacity_kwh for c, _ in usable)
    )
    eta_dis = sum(c.capacity_kwh * c.eta_discharge for c, _ in usable) / max(
        1e-9, sum(c.capacity_kwh for c, _ in usable)
    )
    p_max = sum(c.max_charge_w for c, _ in usable)
    p_min = -sum(c.max_discharge_w for c, _ in usable)
    previous = sum(float(s.power_w or 0.0) for _, s in usable)

    # 5. SoC clamp: no charge above the ceiling, no discharge below floor + reserve.
    soc_lo, soc_hi = p_min, p_max
    soc_ceiling_binding = soc_floor_binding = False
    if dt_hours > 0 and eta_chg > 0 and eta_dis > 0:
        room = max(0.0, e_max - e_now)
        soc_hi = min(soc_hi, room / (dt_hours * eta_chg) * 1000.0)
        headroom = max(0.0, e_now - e_min)
        soc_lo = max(soc_lo, -headroom * eta_dis / dt_hours * 1000.0)
    if soc_hi < p_max - EPS_W:
        soc_ceiling_binding = True
    if soc_lo > p_min + EPS_W:
        soc_floor_binding = True

    # 6. Power clamp: aggregated device limits (per-battery limits are applied after allocation).
    power_hi_binding = soc_hi > p_max - EPS_W
    power_lo_binding = soc_lo < p_min + EPS_W

    # 7. Grid limits: predicted import/export with the requested power must stay inside.
    load_kw = pv_kw = None
    if plan_slot is not None:
        load_kw, pv_kw = float(plan_slot.load_kw), float(plan_slot.pv_kw)
    elif state.load_now_w is not None and state.pv_now_w is not None:
        load_kw, pv_kw = float(state.load_now_w) / 1000.0, float(state.pv_now_w) / 1000.0
    import_w = float(get("optimizer.max_grid_import_w", 0) or 0)
    export_w = float(get("optimizer.max_grid_export_w", 0) or 0)
    grid_lo, grid_hi = p_min, p_max
    grid_import_binding = grid_export_binding = False
    base_kw: float | None = None
    if load_kw is not None and pv_kw is not None:
        base_kw = load_kw - pv_kw
    # The live meter is ground truth: a forecast that says 0.5 kW import must not let a real 4 kW
    # import slide past the cap.  ``grid_now_w`` already contains the battery's current action, so
    # it is corrected back to the "battery idle" base before clamping (audit finding 7).
    measured_base_kw: float | None = None
    if state.grid_now_w is not None:
        current_kw = sum(float(live.power_w or 0.0) for _, live in usable) / 1000.0
        measured_base_kw = float(state.grid_now_w) / 1000.0 - current_kw
    elif base_kw is not None:
        # Telemetry is missing: fail closed by never planning more than the cap itself.
        if import_w > 0:
            grid_hi = min(grid_hi, import_w)
            grid_import_binding = True
        if export_w > 0:
            grid_lo = max(grid_lo, -export_w)
            grid_export_binding = True
    if base_kw is not None or measured_base_kw is not None:
        if import_w > 0:
            base_for_import = base_kw if base_kw is not None else measured_base_kw
            if base_kw is not None and measured_base_kw is not None:
                base_for_import = max(base_kw, measured_base_kw)
            grid_hi = min(grid_hi, import_w - float(base_for_import) * 1000.0)
            grid_import_binding = True
        if export_w > 0:
            base_for_export = base_kw if base_kw is not None else measured_base_kw
            if base_kw is not None and measured_base_kw is not None:
                base_for_export = min(base_kw, measured_base_kw)
            grid_lo = max(grid_lo, -(export_w + float(base_for_export) * 1000.0))
            grid_export_binding = True

    # 8. Ramp: never move a battery by more than RAMP_LIMIT_W per tick.
    budget = RAMP_LIMIT_W * len(usable)
    ramp_lo, ramp_hi = previous - budget, previous + budget

    lo = max(soc_lo, p_min, grid_lo, ramp_lo)
    hi = min(soc_hi, p_max, grid_hi, ramp_hi)
    if lo > hi:
        # No power satisfies every clause: block and name each constraint that cannot be met.
        reasons = list(reasons)
        if grid_export_binding and grid_lo > hi:
            reasons.append(Reason.GRID_EXPORT_LIMIT)
        if grid_import_binding and grid_hi < lo:
            reasons.append(Reason.GRID_IMPORT_LIMIT)
        if soc_ceiling_binding and soc_hi < lo:
            reasons.append(Reason.SOC_CEILING)
        if soc_floor_binding and soc_lo > hi:
            reasons.append(Reason.SOC_FLOOR)
        if ramp_hi < lo - EPS_W or ramp_lo > hi + EPS_W:
            reasons.append(Reason.RAMP_LIMIT)
        if lo > p_max + EPS_W or hi < p_min - EPS_W:
            reasons.append(Reason.POWER_CLAMP)
        return blocked(reasons or [Reason.POWER_CLAMP], blocked_ids)

    allowed = clamp(target, lo, hi)
    clamped = abs(allowed - target) > EPS_W

    # Attribute reasons: every bound the result sits on, plus every unusable battery.
    if soc_ceiling_binding and abs(allowed - soc_hi) <= EPS_W and target > 0:
        reasons.append(Reason.SOC_CEILING)
    if soc_floor_binding and abs(allowed - soc_lo) <= EPS_W and target < 0:
        reasons.append(Reason.SOC_FLOOR)
    if power_hi_binding and abs(allowed - p_max) <= EPS_W and target > 0:
        reasons.append(Reason.POWER_CLAMP)
    if power_lo_binding and abs(allowed - p_min) <= EPS_W and target < 0:
        reasons.append(Reason.POWER_CLAMP)
    if grid_import_binding and abs(allowed - grid_hi) <= EPS_W and abs(grid_hi - p_max) > EPS_W:
        reasons.append(Reason.GRID_IMPORT_LIMIT)
    if grid_export_binding and abs(allowed - grid_lo) <= EPS_W and abs(grid_lo - p_min) > EPS_W:
        reasons.append(Reason.GRID_EXPORT_LIMIT)
    if abs(allowed - ramp_hi) <= EPS_W and allowed < target:
        reasons.append(Reason.RAMP_LIMIT)
    if abs(allowed - ramp_lo) <= EPS_W and allowed > target:
        reasons.append(Reason.RAMP_LIMIT)

    # Still violating after the clamp means the battery physically cannot fix the grid flow.
    if base_kw is not None:
        grid_now = base_kw + allowed / 1000.0
        if import_w > 0 and grid_now > import_w / 1000.0 + 1e-6:
            return blocked(reasons + [Reason.GRID_IMPORT_LIMIT], blocked_ids)
        if export_w > 0 and -grid_now > export_w / 1000.0 + 1e-6:
            return blocked(reasons + [Reason.GRID_EXPORT_LIMIT], blocked_ids)

    return decision(allowed, clamped, reasons, False, blocked_ids, usable_ids)
