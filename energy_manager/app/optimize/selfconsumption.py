"""Zero-export self-consumption planning for a horizon without prices (SPEC 8.31, 0.34.0).

The optimiser needs a tariff to choose between charging, holding and discharging.  When the
configured price provider cannot resolve -- a Zonneplan integration that is down and a contract
that refuses the market curve, a dead feed -- there is nothing to optimise, but there is still one
unambiguous physical objective: **catch the redelivery**.  Every kWh the rooftop would have
exported instead charges the pack, and the house draws it back later, so the plan neither imports
power to charge nor sells the surplus while the pack still has room.

The schedule is built straight from the PV and load forecasts instead of the DP:

* ``surplus = max(0, pv - load)`` charges at ``min(surplus, p_charge, headroom)``;
* ``deficit = max(0, load - pv)`` discharges at ``min(deficit, p_discharge, available)``;
* everything else holds, bounded by the aggregate's SoC floor and ceiling.

That greedy rule *is* the optimum for a zero-export objective with no price signal (no look-ahead
can improve it: energy that is not caught now is gone, and energy caught now is worth the same in
any later slot), so the module stays small and testable.  Slot prices are reported as unknown
(``0.0`` with ``price_stale=True`` and ``price_source="none"``) -- never as a guessed rate -- so the
UI, the journal and ``/plan`` cannot mistake this schedule for a costed one.
"""

from __future__ import annotations

import logging
from bisect import bisect_right
from datetime import datetime, timedelta
from typing import Any, Sequence

from ..models import (
    Action,
    BatteryConfig,
    LoadForecast,
    Plan,
    PlanSlot,
    PlanSummary,
    Reason,
    SolarForecast,
    clamp,
    quarter_floor,
    slot_range,
    utcnow,
)
from .dp import BatteryAggregate

log = logging.getLogger(__name__)

#: Machine-readable marker on ``Plan.sources["plan"]``.  The dispatcher reads it to skip the
#: cost-basis sell floor (a discharge here serves the house, it does not sell energy) and the API
#: and UI use it to caption the plan.
PLAN_SOURCE = "self_consumption"

#: Below this the modelled flow is noise: hold at 0 W instead of ramping for 3 W.
MIN_POWER_KW = 0.005


def _follow(times: Sequence[datetime], slots: Sequence[Any]) -> list[Any]:
    """Forward-filled slot per timestamp (``None`` before the first slot), like ``dp.align_slots``."""
    if not slots:
        return [None] * len(times)
    ordered = sorted(slots, key=lambda s: s.t)
    stamps = [s.t for s in ordered]
    out: list[Any] = []
    for t in times:
        index = bisect_right(stamps, t) - 1
        out.append(ordered[max(0, index)] if index >= 0 else None)
    return out


def plan_self_consumption(
    *,
    solar: SolarForecast | None,
    load: LoadForecast | None,
    battery_configs: Sequence[BatteryConfig],
    start_kwh: float,
    settings: Any,
    now: datetime | None = None,
    mode: str = "advisory",
    dry_run: bool = True,
) -> Plan:
    """Build the zero-export schedule for the horizon (SPEC 8.31).

    Raises ``ValueError`` when the PV or load forecast has no slot: the caller then keeps the
    documented "no plan" behaviour instead of acting on a half-known horizon.
    """
    moment = now or utcnow()
    if load is None or not getattr(load, "slots", None):
        raise ValueError("load forecast has no slots")
    if solar is None or not getattr(solar, "slots", None):
        raise ValueError("solar forecast has no slots")
    minutes = int(clamp(_number(settings.get("optimizer.slot_minutes", 60), 60.0), 15, 60))
    hours = int(clamp(_number(settings.get("optimizer.horizon_hours", 30.0), 30.0), 12, 48))
    reserve = _number(settings.get("optimizer.reserve_soc_pct", 0.0), 0.0)
    aggregate = BatteryAggregate.from_configs(battery_configs, reserve)
    start = quarter_floor(moment, minutes)
    times = slot_range(start, start + timedelta(hours=hours), minutes)
    if not times:
        raise ValueError("empty horizon")

    pv_slots = _follow(times, solar.slots)
    load_slots = _follow(times, load.slots)
    dt = minutes / 60.0
    energy = clamp(start_kwh, aggregate.e_min_kwh, aggregate.e_max_kwh)
    slots: list[PlanSlot] = []
    charged_kwh = 0.0
    discharged_kwh = 0.0
    for index, t in enumerate(times):
        pv_kw = max(0.0, float(getattr(pv_slots[index], "pv_kw", 0.0) or 0.0))
        load_kw = max(0.0, float(getattr(load_slots[index], "load_kw", 0.0) or 0.0))
        power_kw = 0.0
        action = Action.HOLD
        reason = Reason.SELF_CONSUMPTION
        surplus_kw = max(0.0, pv_kw - load_kw)
        deficit_kw = max(0.0, load_kw - pv_kw)
        if surplus_kw > 0.0:
            # Catch the redelivery: a charge is bounded by the surplus itself (never import to
            # charge) and by the room left under the ceiling, in AC kWh at the charge efficiency.
            headroom_kw = max(0.0, aggregate.e_max_kwh - energy) / dt / aggregate.eta_charge
            power_kw = min(surplus_kw, aggregate.p_charge_kw, headroom_kw)
            if power_kw < MIN_POWER_KW:
                power_kw = 0.0
                reason = Reason.FULL
            else:
                action = Action.PV_CHARGE
                reason = Reason.PV_SURPLUS_CHARGE
        elif deficit_kw > 0.0:
            # Serve the house from the pack, but only down to the floor (delivered AC kWh).
            available_kw = max(0.0, energy - aggregate.e_min_kwh) * aggregate.eta_discharge / dt
            power_kw = -min(deficit_kw, aggregate.p_discharge_kw, available_kw)
            if abs(power_kw) < MIN_POWER_KW:
                power_kw = 0.0
                reason = Reason.RESERVE_HOLD
            else:
                action = Action.SELF_CONSUMPTION
                reason = Reason.SELF_CONSUMPTION
        if power_kw > 0.0:
            energy += dt * power_kw * aggregate.eta_charge
            charged_kwh += dt * power_kw
        elif power_kw < 0.0:
            energy += dt * power_kw / aggregate.eta_discharge
            discharged_kwh += dt * -power_kw
        energy = clamp(energy, aggregate.e_min_kwh, aggregate.e_max_kwh)
        slots.append(
            PlanSlot(
                t=t,
                # No tariff resolved for this horizon: report it as unknown, never as 0 EUR/kWh
                # (``price_stale`` makes the UI draw it as the muted "stale" series).
                buy=0.0,
                sell=0.0,
                pv_kw=pv_kw,
                load_kw=load_kw,
                soc_kwh=energy,
                power_w=round(power_kw * 1000.0),
                action=action,
                reason=reason,
                load_p10_kw=getattr(load_slots[index], "p10_kw", None),
                load_p90_kw=getattr(load_slots[index], "p90_kw", None),
                price_stale=True,
                soc_pct=aggregate.soc_pct_for(energy),
                pv_source=str(
                    getattr(pv_slots[index], "source", "")
                    or getattr(solar, "source", "")
                    or ""
                ),
                price_source="none",
                minutes=float(minutes),
            )
        )

    summary = PlanSummary(
        # No tariff, so no cost, baseline or saving is claimed: 0.0 here means "not priced",
        # which ``sources["price"] == "none"`` states explicitly.
        cost_estimate_eur=0.0,
        baseline_cost_eur=0.0,
        saved_eur=0.0,
        charged_kwh=round(charged_kwh, 4),
        discharged_kwh=round(discharged_kwh, 4),
        grid_charge_kwh=0.0,
        pv_charge_kwh=round(charged_kwh, 4),
        cycles=round(discharged_kwh / aggregate.usable_kwh, 4) if aggregate.usable_kwh > 0 else 0.0,
        eta_charge=aggregate.eta_charge,
        eta_discharge=aggregate.eta_discharge,
        round_trip_efficiency=round(aggregate.eta_charge * aggregate.eta_discharge, 4),
    )
    return Plan(
        generated_at=moment,
        horizon_start=times[0],
        horizon_end=times[-1] + timedelta(minutes=minutes),
        slot_minutes=minutes,
        feasible=True,
        infeasible_reason=None,
        slots=slots,
        summary=summary,
        mode=mode,
        dry_run=dry_run,
        sources={"plan": PLAN_SOURCE, "price": "none", "solar": str(getattr(solar, "source", "") or ""), "load": str(getattr(load, "source", "") or "")},
    )


def is_self_consumption_plan(plan: Plan | None) -> bool:
    """True when ``plan`` is a zero-export self-consumption schedule (no tariff behind it)."""
    sources = getattr(plan, "sources", None)
    return isinstance(sources, dict) and sources.get("plan") == PLAN_SOURCE


def _number(value: Any, fallback: float) -> float:
    """A finite float, else the fallback (never raises, never guesses)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return float(fallback)
    number = float(value)
    return number if number == number and abs(number) != float("inf") else float(fallback)
