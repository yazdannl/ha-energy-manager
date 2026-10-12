# Smart Energy Manager

Plans and (optionally) executes battery dispatch to cut electricity cost: charge when power is
cheap or PV is producing a surplus, discharge when power is expensive.

The add-on ships safe: the default mode is `advisory` with `dry_run: true`, so a fresh install
computes and displays plans but cannot write to any device until you deliberately change that.
Advisory and dry-run installs issue **zero** writes — during ticks, on `Stop now`, on SIGTERM and on
a fatal error — with one exception: if the add-on itself left a real setpoint running (tracked in a
per-battery live-write latch that is persisted), that setpoint is neutralised, and it is neutralised
on the next start too, before control resumes.

The full documentation lives in the [repository README](https://github.com/yazdannl/ha-energy-manager#readme).

## Quick start

1. Settings → Add-ons → Add-on store → ⋮ → Repositories, and add
   `https://github.com/yazdannl/ha-energy-manager`.
2. Install **Smart Energy Manager** and start it.
3. Open the **Energy** panel, then *Batteries → Add battery*. For a Zendure SolarFlow the entities
   are discovered through the `zendure_ha` integration; you confirm the mapping before it is stored.
   A device with no integration (or one you want to drive **without it**) uses the `zendure_native`
   driver instead: enable the device's local API in the Zendure app (add HEMS and exit to apply),
   then type its address and serial — the add-on reads the device once before it saves the battery.
   Enter the battery's **round trip** efficiency as one number (the default is a 90 % round trip); the
   card then shows what a stored kWh costs and the sale price at which a cycle breaks even.
4. Open *Settings*: pick your price provider on the **Price** card and (optionally) choose how the
   add-on should know your PV on the **Solar** card. The **Meter** card can stay empty: the add-on
   reads the meter from your Home Assistant **Energy dashboard** (its power sensor, or its
   import/export energy counters) — search and pick one there only to override that. The card shows
   the resolved roles, and **Use the Energy dashboard** clears an override again. Leave the mode at
   `advisory` for a few days.
5. Watch *Plan* and *Log* to see what the add-on would have done, then enable `auto` (with
   `dry_run` still on) when you trust it.

## Battery efficiency and wear

You enter **one** efficiency number per battery: the **round trip**, i.e. how much of the energy put
in comes back out. The add-on splits it evenly between the two directions — the default 95 % in and
95 % out is a round trip of just over 90 % — so the loss is never hidden in one direction. If you
know the charger and inverter figures separately, use the *charge/discharge efficiency* override
instead and the round trip becomes their product.

Wear is derived from the pack's own price and cycle life (both optional fields on the battery card),
not typed in as a global number:

```
wear = price / (cycle_life × usable kWh × η_dis)   # EUR per kWh delivered
break_even_sell = buy / round_trip + wear
```

Buying at 20 ct/kWh with a **75 %** round trip breaks even at **26.7 ct, not 25 ct** — 25 ct would be
the 80 % case — plus the derived wear (for example 3000 EUR over 6000 cycles on 9.6 kWh usable at a
95 % discharge efficiency is about 5.5 ct/kWh delivered). With no price or cycle life entered the
wear is 0 and the formula reduces to `buy / round_trip`: 20 ct at 90 % breaks even at 22.2 ct. The
advanced `optimizer.cycle_cost_eur_per_kwh` setting overrides the derived wear when non-zero.

The add-on plans an arbitrage cycle only when the sell price clears that figure, values stored energy
at `eta_discharge` (a stored kWh returns only part of itself to the house as AC, so it is not worth
face value), and reports a slot left idle because the spread does not cover the loss as
`efficiency_loss` — the conversion loss, which is not the same as your own
`optimizer.min_spread_eur_per_kwh` floor (`spread_below_threshold`). Wear is charged per kWh
delivered, so charging alone costs none.

With several batteries the plan uses capacity-weighted mean efficiencies and delivered-energy-weighted
mean wear (an approximation when the packs differ materially), spends the most efficient battery
first, reports the aggregate round trip and wear it assumed, and computes each battery's break-even
from that battery's own values.

Where to look: the battery card shows the round trip, the derived wear and the break-even sale price
that follows from it; the Plan view shows the assumed round trip and wear and draws the break-even
line against the sell prices.

## Safety and privacy in one paragraph

Nothing leaves your system except the forecast requests (Forecast.Solar, NASA POWER irradiance for
the capacity/derived solar sources, and — if used — the energy-charts day-ahead price API), and no
location or entity mapping is stored in this
repository or in its logs. Hard SoC, power, grid and ramp limits are enforced in front of every
write, every rejection is logged with its reason, and `mode: off` is the kill switch.

## Zendure SolarFlow: read this before you uninstall

The Zendure integration has **no device watchdog**: it keeps the last setpoint it was given. If you
stop using this add-on, put the Zendure manager back into one of its own modes (its smart mode) or
switch the device off, instead of leaving it in manual with a stale setpoint. The add-on writes a
hold (0 W) setpoint on a clean shutdown and on a fatal error, but a container that is killed cannot
do that for you.

While it is in control the add-on switches the manager to `manual` and keeps re-asserting it, because
the integration's own smart modes overwrite external writes. A new setpoint only takes effect when
the device processes its next P1 event, so the setpoint can take a short while to show up; the
dashboard and the log show *requested* versus *applied* power for exactly that reason.

## Baselines

`savings = baseline − actual`, where the baseline is the *net* cost the same metered consumption
would have had with the battery doing nothing: import cost at the same prices minus export revenue,
with no battery flows. If no load figure is available for an interval, the baseline and the savings
are reported as unknown (the UI shows `--`), never as a zero. See the README for the full
definition, because that number is the one to judge the add-on by.

## Access and authentication

Use the **Energy** panel inside Home Assistant: the UI is served through ingress, which supplies the
login, and the add-on publishes no port. The HTTP API itself has no authentication, and a caller
that can reach it can switch the mode to `auto` or turn `dry_run` off — which moves real hardware.
Do not add a `ports:` mapping for 8099 or put it behind a reverse proxy.

## Support

Questions, bugs and feature requests: open an issue in the repository. This add-on is not
affiliated with Home Assistant, Zendure, Forecast.Solar or energy-charts.

Data credits: the day-ahead price series from `api.energy-charts.info` — fetched only when the
`energy_charts` price source is selected — is licensed **CC BY 4.0**, provided by
**Fraunhofer ISE (Energy-Charts.info)** and sourced from **Bundesnetzagentur | SMARD.de**. The
bundled uPlot chart library is vendored (no CDN) under the **MIT** licence; its licence text is at
`app/web/vendor/LICENSE.uPlot.txt`.
