"""Candidate plans, scored alike, best one kept (0.35.0).

A discharge below the pack's own cost basis is only a gain if the horizon can put that energy back
more cheaply -- the round trip loses part of it and the wear is paid per delivered kWh. The solver
already prices that inside its objective, but the *reported* money ("saved vs baseline") is cash
only: a schedule that empties the pack always looks better than one that keeps it. This module makes
the comparison explicit and honest:

* every candidate is a complete plan, solved by the same DP, differing only in the floor policy it
  must respect;
* every candidate is scored with **one** metric -- the cash it saves over the horizon **plus** the
  stored energy it ends with, marked at what refilling it inside this horizon would cost;
* the most profitable candidate is kept, and the comparison rides on the plan so the UI can state
  why this plan and not another (SPEC 5/8.1).

The three candidates answer the three questions a user actually asks when a plan sells held energy
below its average basis:

``refill_floor``
    Today's rule: the ledger floor capped at the cheapest-refill break-even. It may sell below the
    basis, but only where a cheaper refill inside the horizon pays for the round trip and the wear.
``ledger_floor``
    The literal basis: never discharge (or export) at a value leg below what the energy in the pack
    cost. The ``do not sell below the floor`` candidate.
``hold_pack``
    Never discharge at all: only charge cheap energy and keep it. The ``just top up later``
    candidate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from . import dp

#: ``optimizer.plan_variants``: ``best`` solves the candidates and keeps the most profitable one,
#: ``off`` keeps the single solve of 0.34.x bit-for-bit.
PLAN_VARIANTS_CHOICES = ("best", "off")


@dataclass(frozen=True)
class Variant:
    """One candidate: a floor policy plus how to build the aggregate it is solved with."""

    id: str
    label: str
    #: The ledger floor handed to the solver (``None`` for "no floor at all").
    floor_eur_per_kwh: float | None
    #: Whether the solver caps that floor at the cheapest-refill break-even (the 0.33.0 rule).
    cap_floor: bool = True
    #: ``True`` builds a pack that can charge but not discharge (the "just top up" candidate).
    charge_only: bool = False

    def description(self) -> str:
        if self.charge_only:
            return "charge cheap energy and keep it; never discharge"
        if self.floor_eur_per_kwh is None:
            return "no cost-basis floor at all"
        if self.cap_floor:
            return "the ledger floor, capped at the cheapest refill inside the horizon"
        return "the ledger floor exactly: never sell energy below what it cost"


def candidates(ledger_floor_eur_per_kwh: float | None) -> list[Variant]:
    """The candidates worth solving for this plan.

    With no ledger floor there is nothing to vary: one candidate, so the sweep is a no-op and the
    plan stays exactly the single solve it is today. The same is true when the ledger is off (no
    stored cost basis yet) -- an empty ledger must not invent a policy.
    """
    if ledger_floor_eur_per_kwh is None:
        return [Variant("refill_floor", "Refill-aware floor", None)]
    return [
        Variant("refill_floor", "Refill-aware floor", float(ledger_floor_eur_per_kwh)),
        Variant(
            "ledger_floor",
            "Never below the ledger floor",
            float(ledger_floor_eur_per_kwh),
            cap_floor=False,
        ),
        Variant("hold_pack", "Charge only, never discharge", None, charge_only=True),
    ]


def normalize(value: Any) -> str:
    """``optimizer.plan_variants`` as one of :data:`PLAN_VARIANTS_CHOICES` (``best`` by default)."""
    text = str(value or "").strip().lower()
    return text if text in PLAN_VARIANTS_CHOICES else "best"


def replacement_value_eur_per_kwh(
    slots: Sequence[dp.SlotInput],
    aggregate: dp.BatteryAggregate,
) -> float | None:
    """What one **stored** kWh is worth: refilling it at the horizon's cheapest buy.

    Units matter here. The stored energy in the pack (``soc_kwh``) is not the delivered AC energy:
    buying 1 AC kWh charges ``eta_charge`` kWh, so replacing one stored kWh costs
    ``cheapest_buy / eta_charge``. The *sell* floor is the other way round -- it is per delivered AC
    kWh, ``cheapest_buy / round_trip + wear`` (``dp.refill_break_even_eur_per_kwh``) -- which is why
    the score marks ``energy_delta_kwh`` with this number and not with the floor.

    ``None`` when the horizon carries no price at all (the self-consumption path) -- there is
    nothing to mark the energy against, so the sweep does not run.
    """
    cheapest: float | None = None
    for slot in slots:
        if slot.buy is None:
            continue
        price = float(slot.buy)
        if cheapest is None or price < cheapest:
            cheapest = price
    if cheapest is None or aggregate.eta_charge <= 0.0:
        return None
    return cheapest / aggregate.eta_charge


def score_plan(
    plan: Any,
    *,
    start_kwh: float,
    replacement_eur_per_kwh: float | None,
    ledger_floor_eur_per_kwh: float | None,
) -> dict[str, Any]:
    """One plan's comparable figures (SPEC 5).

    ``net_eur`` is the only number the sweep compares: the cash saved over the horizon **plus** the
    stored energy the plan ends with, marked at ``replacement_eur_per_kwh``. Doing nothing scores
    exactly ``0``, so a candidate that cannot beat it is never worth dispatching.
    """
    summary = plan.summary
    slots = list(plan.slots or [])
    end_kwh = float(slots[-1].soc_kwh) if slots else float(start_kwh)
    delta = end_kwh - float(start_kwh)
    replacement = None if replacement_eur_per_kwh is None else float(replacement_eur_per_kwh)
    stored_value = None if replacement is None else delta * replacement
    # Cash includes the grid money; the wear the schedule spends is its own reported term, so the
    # score subtracts it and a plan cannot look better by cycling the pack for free.
    cash = float(summary.saved_eur) - float(getattr(summary, "cycle_cost_eur", 0.0) or 0.0)
    # Energy sold below what it cost: the figure the floor question is really about.
    sold_below = 0.0
    if ledger_floor_eur_per_kwh is not None:
        basis = float(ledger_floor_eur_per_kwh)
        for slot in slots:
            if slot.power_w >= 0:
                continue
            leg = slot.sell if slot.grid_kw() < 0 else slot.buy
            if leg is not None and float(leg) < basis:
                sold_below += -slot.power_w / 1000.0 * slot.minutes / 60.0
    return {
        "net_eur": None if stored_value is None else cash + stored_value,
        "cash_saved_eur": cash,
        "wear_eur": float(getattr(summary, "cycle_cost_eur", 0.0) or 0.0),
        "cost_eur": float(summary.cost_estimate_eur),
        "baseline_cost_eur": float(summary.baseline_cost_eur),
        "stored_value_eur": stored_value,
        "energy_delta_kwh": delta,
        "end_soc_kwh": end_kwh,
        "charged_kwh": float(summary.charged_kwh),
        "discharged_kwh": float(summary.discharged_kwh),
        "sold_below_basis_kwh": sold_below,
        "sell_floor_eur_per_kwh": getattr(summary, "sell_floor_eur_per_kwh", None),
    }


def choose(
    scored: Iterable[tuple[Variant, Any, dict[str, Any]]],
    *,
    replacement_eur_per_kwh: float | None = None,
) -> tuple[Variant, Any, dict[str, Any], dict[str, Any]]:
    """Pick the most profitable candidate and build the comparison block.

    Ties keep the first candidate, and the candidate order puts the incumbent rule first, so an
    unchanged horizon never flips the plan for a rounding error. A candidate that cannot be scored
    (no replacement value) or that the solver reported infeasible is never chosen while a feasible,
    scorable candidate exists; if none does, the first one is returned with a null score rather than
    failing the replan.
    """
    rows: list[dict[str, Any]] = []
    best: tuple[Variant, Any, dict[str, Any]] | None = None
    for variant, plan, figures in scored:
        row = {"id": variant.id, "label": variant.label, "rule": variant.description()}
        row.update(figures)
        row["feasible"] = bool(getattr(plan, "feasible", True))
        row["infeasible_reason"] = getattr(plan, "infeasible_reason", None)
        rows.append(row)
        if figures.get("net_eur") is None or not row["feasible"]:
            continue
        if best is None or float(figures["net_eur"]) > float(best[2]["net_eur"]) + 1e-9:
            best = (variant, plan, figures)
    if not rows:  # pragma: no cover - the caller never scores nothing
        raise ValueError("no plan variants were scored")
    if best is None:
        variant, plan, figures = next(iter(scored))
        block = {
            "chosen": variant.id,
            "chosen_label": variant.label,
            "replacement_eur_per_kwh": replacement_eur_per_kwh,
            "net_eur": figures.get("net_eur"),
            "cash_saved_eur": figures.get("cash_saved_eur"),
            "stored_value_eur": figures.get("stored_value_eur"),
            "energy_delta_kwh": figures.get("energy_delta_kwh"),
            "margin_eur": None,
            "tie": False,
            "candidates": rows,
        }
        return variant, plan, figures, block
    chosen_net = float(best[2]["net_eur"])
    alternatives = sorted(
        (
            row
            for row in rows
            if row["id"] != best[0].id and row["net_eur"] is not None and row["feasible"]
        ),
        key=lambda row: -float(row["net_eur"]),
    )
    margin = chosen_net - float(alternatives[0]["net_eur"]) if alternatives else None
    block = {
        "chosen": best[0].id,
        "chosen_label": best[0].label,
        "replacement_eur_per_kwh": replacement_eur_per_kwh,
        "net_eur": chosen_net,
        "cash_saved_eur": best[2]["cash_saved_eur"],
        "stored_value_eur": best[2]["stored_value_eur"],
        "energy_delta_kwh": best[2]["energy_delta_kwh"],
        # The margin over the best alternative, so the UI can say "by X EUR" -- or "ties with",
        # which is the common case: on a horizon whose floor never binds, every policy is one plan.
        "margin_eur": margin,
        "tie": margin is not None and abs(margin) <= 1e-9,
        "candidates": rows,
    }
    return best[0], best[1], best[2], block


def attach(plan: Any, block: dict[str, Any]) -> None:
    """Ride the comparison on the plan and its summary (additive, ``models.py`` is frozen)."""
    plan.variants = block
    summary = plan.summary
    summary.plan_variant = block.get("chosen")
    summary.plan_variant_label = block.get("chosen_label")
    summary.net_eur = block.get("net_eur")
    summary.stored_value_eur = block.get("stored_value_eur")
    summary.energy_delta_kwh = block.get("energy_delta_kwh")
    summary.replacement_eur_per_kwh = block.get("replacement_eur_per_kwh")
    summary.plan_variant_margin_eur = block.get("margin_eur")
