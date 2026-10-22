"""Offline: re-solve live (or synthetic) forecasts under several floor policies and score them alike.

The question this answers: a plan that discharges held energy below its cost basis is only better if
the cheaper refill inside the horizon pays for the round-trip loss and the wear. The solver's own
objective already prices that, but the number a human reads ("saved vs baseline") is cash only, so a
dump always looks good. This tool scores every candidate on one metric -- cash over the horizon plus
the stored energy it ends with, marked at the cheapest refill the horizon offers -- and prints the
comparison. It is the reference for `optimize/variants.py`.

Usage:
    python3 tools/variant_report.py /tmp/live.json
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "energy_manager"))

from app.models import (  # noqa: E402
    BatteryConfig,
    LoadForecast,
    LoadSlot,
    PriceForecast,
    PriceSlot,
    SolarForecast,
    SolarSlot,
)
from app.optimize import dp  # noqa: E402


class Settings(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def _feed(payload: dict) -> list[list[float]]:
    return payload["attributes"]["slots"] if "attributes" in payload else payload["slots"]


def load_live(path: str) -> tuple[PriceForecast, SolarForecast, LoadForecast]:
    raw = json.loads(Path(path).read_text())
    price = PriceForecast(source="zonneplan_entity")
    for row in _feed(raw["price"]):
        price.slots.append(
            PriceSlot(
                t=datetime.fromtimestamp(row[0], timezone.utc),
                buy=row[1],
                sell=row[2],
                stale=row[3] == 1,
            )
        )
    solar = SolarForecast(source="merge")
    for row in _feed(raw["solar"]):
        solar.slots.append(
            SolarSlot(t=datetime.fromtimestamp(row[0], timezone.utc), pv_kw=row[1], uncertainty_pct=row[2])
        )
    load = LoadForecast(source="profile")
    for row in _feed(raw["load"]):
        load.slots.append(
            LoadSlot(
                t=datetime.fromtimestamp(row[0], timezone.utc),
                load_kw=row[1],
                p10_kw=row[2],
                p90_kw=row[3],
            )
        )
    return price, solar, load


def battery() -> list[BatteryConfig]:
    # A SolarFlow 800 Plus: 2 kWh, 1 kW in, 0.8 kW out, 0.92/0.92 round trip (live plan's 0.847).
    return [
        BatteryConfig(
            id="solarflow_800_plus",
            name="SolarFlow",
            driver="solarflow",
            capacity_kwh=2.0,
            max_charge_w=1000.0,
            max_discharge_w=800.0,
            eta_charge=0.92,
            eta_discharge=0.921,
            min_soc_pct=10.0,
            max_soc_pct=100.0,
            enabled=True,
            mapping={"power_setpoint_entity": "number.b", "soc_entity": "sensor.b"},
        )
    ]


def score(plan, *, start_kwh: float, replacement: float) -> dict:
    """One metric for every candidate: cash saved + the stored energy it ends with."""
    summary = plan.summary
    end_kwh = plan.slots[-1].soc_kwh if plan.slots else start_kwh
    delta = end_kwh - start_kwh
    return {
        "cash_saved_eur": summary.saved_eur,
        "end_kwh": end_kwh,
        "energy_delta_kwh": delta,
        "stored_value_eur": delta * replacement,
        "net_eur": summary.saved_eur + delta * replacement,
        "discharged_kwh": summary.discharged_kwh,
        "charged_kwh": summary.charged_kwh,
        "cost_eur": summary.cost_estimate_eur,
    }


def main(path: str) -> None:
    price, solar, load = load_live(path)
    settings = Settings(
        {
            "optimizer.slot_minutes": 15,
            "optimizer.horizon_hours": 30,
            "optimizer.reserve_soc_pct": 10.0,
            "optimizer.terminal_mode": "off",
        }
    )
    configs = battery()
    start_kwh = 0.28 * 2.0  # live SoC at plan time
    reserve = 10.0
    aggregate = dp.BatteryAggregate.from_configs(configs, reserve)
    start = dp.quarter_floor(price.slots[0].t, 15)
    slots = dp.align_slots(price, solar, load, start, start + timedelta(hours=30), 15)
    options = dp.OptimizerOptions.from_settings(settings)
    cheapest = min(s.buy for s in slots if s.buy is not None)
    replacement = dp.aggregate_break_even_sell_eur_per_kwh(
        aggregate.eta_charge, aggregate.eta_discharge, cheapest, options.wear_eur_per_kwh
    )  # what a refill really costs
    ledger = 0.3049  # live cost basis

    # (label, floor, cap) -- the floor policies a plan could follow, plus "never discharge".
    variants = [
        ("no_floor (pure arbitrage)", None, True),
        ("refill_floor (live default)", ledger, True),
        ("ledger_floor (never below basis)", ledger, False),
        ("hold_only (charge, never sell)", 1e6, False),
    ]
    rows = []
    for label, floor, cap in variants:
        plan = dp.solve(
            slots,
            aggregate=aggregate,
            start_kwh=start_kwh,
            options=options,
            sell_floor_eur_per_kwh=floor,
            cap_floor=cap,
        )
        row = score(plan, start_kwh=start_kwh, replacement=replacement)
        row["label"] = label
        # What the floor question is really about: energy sold below what it cost.
        sold_below = 0.0
        for slot in plan.slots:
            if slot.power_w >= 0:
                continue
            leg = slot.sell if slot.grid_kw() < 0 else slot.buy
            if leg is not None and leg < ledger:
                sold_below += -slot.power_w / 1000.0 * slot.minutes / 60.0
        row["sold_below_basis_kwh"] = sold_below
        row["floor_eur"] = plan.summary.sell_floor_eur_per_kwh
        rows.append(row)
    hold = {
        "label": "hold (battery idle)",
        "cash_saved_eur": 0.0,
        "end_kwh": start_kwh,
        "energy_delta_kwh": 0.0,
        "stored_value_eur": 0.0,
        "net_eur": 0.0,
        "discharged_kwh": 0.0,
        "charged_kwh": 0.0,
        "cost_eur": 0.0,
        "sold_below_basis_kwh": 0.0,
        "floor_eur": None,
    }
    rows.append(hold)

    print(f"horizon {slots[0].t:%d %H:%M} -> {slots[-1].t:%d %H:%M} UTC, {len(slots)} slots")
    print(f"cheapest refill {cheapest:.4f} EUR/kWh -> a stored kWh is worth {replacement:.4f}")
    print(f"ledger basis {ledger:.4f}  start {start_kwh:.2f} kWh\n")
    header = (
        f"{'variant':34} {'floor':>7} {'chg':>6} {'dis':>6} {'below':>6} {'end':>6}"
        f" {'cash':>7} {'stored':>7} {'NET':>7}"
    )
    print(header)
    print("-" * len(header))
    for row in sorted(rows, key=lambda r: -r["net_eur"]):
        floor = row["floor_eur"]
        floor_text = "none" if floor is None else f"{floor:.3f}"
        print(
            f"{row['label']:34} {floor_text:>7}"
            f" {row['charged_kwh']:6.2f} {row['discharged_kwh']:6.2f}"
            f" {row['sold_below_basis_kwh']:6.2f} {row['end_kwh']:6.2f}"
            f" {row['cash_saved_eur']:7.3f} {row['stored_value_eur']:7.3f} {row['net_eur']:7.3f}"
        )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/live.json")
