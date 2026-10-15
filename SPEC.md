# Smart Energy Manager — frozen build spec

Authoritative contract for all build workers. If code and this spec disagree, the spec wins or the
spec is amended in a PR that says why. Read `models.py` next to this file: those dataclasses are the
shared vocabulary and are not redefined here.

---

## 0. Hard rules (violating these fails the build)

**R1 — No personal, instance or account data in the repository. Ever.**
The repo is public. Nothing that identifies the user, their house, their hardware or their account
may be committed:

- no latitude/longitude/elevation/address/postcode values,
- no real Home Assistant entity IDs, unique IDs, device IDs or device serial numbers,
- no tokens, API keys, passwords, webhook IDs, `auth.json` content,
- no real energy/price figures, tariffs, meter numbers or supplier account data,
- no hostnames, internal IPs, domains or URLs belonging to the user's setup,
- no captured API payloads or logs from the live instance (screenshots included).

Allowed in committed files: integration-level generic identifiers that are identical on every
install (e.g. the HA domain `zendure_ha`, entity translation keys such as `input_limit`), public
API endpoints, and obviously fake examples. Fake examples must be recognisably fake and use a
reserved namespace:

```
sensor.example_pv_power        sensor.example_battery_soc_1
sensor.example_price_now       sensor.example_load_power
latitude: 0.0 default only as "unset" placeholder — never a plausible real value
docs/README examples use "Example Street 1", 52.0 / 5.0 style round numbers
```

Everything instance-specific is **resolved at runtime** and kept out of git:

| Kind | Where it lives at runtime |
| --- | --- |
| Location, timezone, currency | read from HA `GET /api/config` |
| Entity/device mapping | entity picker in the UI, confirmed by the user |
| API keys, tariff params, limits | add-on options or UI settings |
| Learned profiles, plans, actuals | SQLite in the add-on `/data` volume |

`.gitignore` must exclude `*.db`, `data/`, `*.log`, `*.json` runtime artefacts, `.venv/`,
`__pycache__/`, and any local `.env`.

**R2 — Audit gate: no `git push`, no remote deploy, no add-on-store registration** until the user
explicitly approves after auditing. Workers commit locally only. Do not add a remote. Do not run
`gh`, `git push`, `supervisor` write calls, or anything that publishes. Push is a separate,
user-approved step performed by the orchestrator.

**R3 — Zero third-party runtime dependencies.** The add-on runs on the HA base Python image with
the standard library only: `http.server`, `urllib.request`, `sqlite3`, `json`, `threading`,
`zoneinfo`, `dataclasses`, `statistics`, `socket`, `hashlib`, `base64`, `ssl`, `zlib`. No pip
installs, no frameworks, no numpy/pandas/pulp. `requirements.txt` stays empty (it exists only to
document that fact). Vendored browser assets under `app/web/vendor/` are the only third-party
files and they must carry their upstream licence header.

**R4 — Safety over cleverness.** Battery control code must fail safe (hold/zero power), never
exceed configured SoC or power limits, and must be testable without hardware.

**R5 — Ponytail.** Smallest correct solution; no speculative abstraction, no config option that
nothing reads, no dependency we can avoid. Mark deliberate simplifications in code with a
`ponytail:` comment naming the ceiling and the upgrade path. Every non-trivial algorithm gets one
small runnable check.

---

## 1. What the product is

A Home Assistant add-on that plans and (optionally) executes battery dispatch to minimise
electricity cost, using four inputs: solar production forecast, dynamic price forecast, learned
household load profile, and live battery/grid measurements.

Modes (`mode`):

| Mode | Behaviour |
| --- | --- |
| `off` | No planning, no writes. Data collection, alerts and the UI/API keep working, and a plan computed before the switch is dropped so the UI never shows a stale plan as current. An explicit user-triggered recompute is still served as a preview (it is a read-only what-if), but no scheduled planning happens and nothing is written. |
| `advisory` | **(default)** Plan is computed, published and shown. **Never writes to the battery**, with the single exception in §9.4: a battery the add-on really commanded and has not verified neutral is neutralised on Stop, on a mode/`dry_run` change, on exit and on boot. |
| `auto` | Plan is executed through the configured battery drivers. |

`dry_run` (bool, default `true`): in `auto`, compute and log the exact writes that *would* happen
without performing them. A fresh install therefore cannot move hardware even if someone switches to
`auto` and forgets `dry_run`.

Shipping defaults must be safe on a factory-fresh HA: `mode: advisory`, `dry_run: true`.

---

## 2. Repository layout and ownership

```
repository.yaml                     WP4   add-on repository descriptor
README.md                           WP4   user-facing overview, install, screenshots-in-words
LICENSE                             WP4   MIT
.gitignore                          WP4   see R1
.github/workflows/validate.yml      WP4   HA add-on lint + pytest + shellcheck-lite
docs/EXTERNAL_APIS.md               WP1   verified external contracts
docs/ARCHITECTURE.md                WP4   how it fits together (short)
docs/UI.md                          WP3   UI map, screenshots-free walkthrough
tools/audit.sh                      WP4   PII/secret scanner (see §14)
SPEC.md                             orch  this file
energy_manager/
  app/                              (python package, imported as `app`)
    __init__.py                     WP2A
    models.py                       orch  FROZEN dataclasses
    config.py                       WP2A defaults + option parsing + settings merge
    harest.py                       WP2A HA REST client
    haws.py                         WP2A HA WebSocket client (minimal, stdlib)
    store.py                        WP2A SQLite store
    sources/__init__.py             WP2A
    sources/price.py                WP2A price forecast providers
    sources/solar.py                WP2A PV forecast providers
    sources/load.py                 WP2A load forecast + learning
    sources/measure.py              WP2A measurement sampling
    optimize/__init__.py            WP2B
    optimize/dp.py                  WP2B SoC dynamic program
    optimize/allocate.py            WP2B per-battery allocation
    control/__init__.py             WP2B
    control/safety.py               WP2B guard rails (pure functions)
    control/dispatcher.py           WP2B tick loop -> driver writes
    control/drivers/__init__.py     WP2B driver registry
    control/drivers/base.py         WP2B BatteryDriver protocol
    control/drivers/zendure.py      WP2B Zendure driver
    control/drivers/generic.py      WP2B entity-mapping driver
    control/drivers/native.py       WP2B native Zendure zenSDK HTTP driver
    engine.py                       WP2B application core, owns threads and shared state
    api.py                          WP2B HTTP JSON API (frozen contract §10)
    notify.py                       WP2B HA notifications + persistent notices
    analytics.py                    WP2B savings + accuracy accounting
    main.py                         WP2B entrypoint: bootstrap, serve
    web/index.html                  WP3
    web/app.js                      WP3
    web/style.css                   WP3
    web/charts.js                   WP3
    web/vendor/uPlot.min.js         WP3 vendored, licence header preserved
    web/vendor/uPlot.min.css        WP3 vendored
  config.yaml                       WP4 add-on manifest
  Dockerfile                        WP4
  build.yaml                        WP4
  run.sh                            WP4
  requirements.txt                  WP4 empty by design (R3)
  CHANGELOG.md                      WP4
  DOCS.md                           WP4 add-on store documentation tab
  icon.png  logo.png                WP4 generated, no deps
tests/                              WP5 pytest suite (stdlib + pytest only)
```

**File ownership is exclusive.** Never create or edit a file owned by another worker; if a change
is needed there, report it to the orchestrator instead. Shared fixtures go in `tests/` (WP5) or the
area you own.

---

## 3. Runtime configuration

Two layers, merged with UI settings winning over add-on options:

1. **Add-on options** (`config.yaml`) — the small set an install needs at first boot.
2. **UI settings** — everything else, persisted in SQLite `settings`, editable without a restart.

Frozen option schema (`energy_manager/config.yaml`):

```yaml
options:
  mode: advisory
  dry_run: true
  log_level: info
  currency: null          # null = read from HA
  timezone: null          # null = read from HA
  latitude_override: null # null = read from HA /api/config
  longitude_override: null
schema:
  mode: list(off|advisory|auto)
  dry_run: bool
  log_level: list(debug|info|warning|error)
  currency: str?
  timezone: str?
  latitude_override: float?
  longitude_override: float?
```

Frozen settings keys (SQLite `settings`, all optional, each with a default in `config.py`; the
defaults below are the shipped values):

```
price.source              zonneplan_entity | energy_charts | entity_forecast | static
price.entity              entity id of the price sensor (runtime-picked, stored in DB only)
price.static_schedule     list of {from_hour, to_hour, price_eur_per_kwh, days}
price.import_fee_eur_per_kwh   0.0   # supplier markup added on buy
price.export_fee_eur_per_kwh   0.0   # feed-in cost: deducted from the sell price (derived feed-in)
price.vat_pct             21.0       # applied when a source yields ex-VAT prices
price.energy_tax_eur_per_kwh   0.0   # applied when a source yields ex-tax prices
price.feed_in_eur_per_kwh  null  # explicit flat feed-in tariff; null = derive it from the buy price
price.feed_in_entity       ""    # optional HA sensor with the live feed-in price (overrides the flat tariff)
price.feed_in_tax_inclusive true  # false = add price.vat_pct to the flat tariff/entity value
price.feed_in_energy_tax_refund true  # false = the energy-tax component is not paid back on feed-in
price.feed_in_mode       derived|explicit|buy_minus_fee|saldering|tiered  # derived = pre-1b rule (byte-for-byte)
price.energy_tax_confirmed false # true = the energy-tax component is a stated value, including a confirmed 0
price.feed_in_tax_deduction_eur_per_kwh null  # stated per-kWh tax/levy after saldering; null = slot component
price.saldering_assume_nettable false # saldering only: assume the period allowance is still positive
price.unit               auto|eur_per_kwh|eur_per_mwh|micro_eur_per_kwh
price.scale_divisor      1.0   # fixed-point divisor for providers publishing integers (per source)
price.bzn                NL    # bidding zone for the energy_charts day-ahead source
solar.enabled            true
solar.source             forecast_solar|capacity|derived|learned   # derived = learn the array from the meter (§7.1.2); learned = the trained residual model (§7.1.4)
solar.planes             []   # [{kwp, tilt_deg, azimuth_deg, damping}]
solar.api_key            ""   # optional forecast.solar personal key (raises rate limits, enables multi-plane)
solar.bias_correction    true # learn multiplicative correction from measured PV; the master switch for both the hourly EWMA and the fine-tune head (§7.5)
solar.measured_entity    ""   # runtime-picked PV power sensor for bias learning
solar.nasa_irradiance    true # capacity/derived/learned: observed NASA POWER clearness per hour-of-day
solar.capacity_wp        null # capacity/learned: total DC watt peak; the learned source requires it
solar.ac_capacity_wp     null # learned only: the inverter's AC limit; null = assumed equal to the watt peak (§7.1.4)
learning head            -    # §7.5 fits per-hour factors into learning_profiles and applies them only when promoted
solar.sources            []   # merge several sources into one: [{source, weight, ...}] (§7.1.3)
solar.merge_strategy     weighted_mean|median|max   # how several sources become one value per slot
load.mode                auto|entity|derived   # auto uses load entity if mapped else derived
load.entity              ""   # runtime-picked load power sensor
load.temperature_entity  ""   # optional outdoor temperature for the regression term
load.base_kwh_per_day    0.0  # fallback flat profile when nothing is learned yet
# grid.* empty -> resolved at runtime from the HA Energy dashboard (energy/get_prefs, section 3); explicit wins
grid.power_entity        ""   # signed grid power sensor, positive = import (enables the derived load tier)
grid.import_entity       ""   # separate import power sensor, optional
grid.export_entity       ""   # separate export power sensor, optional
optimizer.slot_minutes   60
optimizer.horizon_hours  30   # matches the ~+30 h price feed
optimizer.soc_step_kwh   0.02
optimizer.power_steps    24
optimizer.salvage_multiplier 1.0   # terminal SoC valued at multiplier x median buy price
optimizer.cycle_cost_eur_per_kwh 0.0   # advanced override; 0 = derive wear per battery (price/cycle life)
optimizer.allow_grid_charge  true
optimizer.allow_export       false  # off until a feed-in tariff is configured; sell is 0.00
optimizer.reserve_soc_pct    0.0   # extra floor above battery min_soc
optimizer.max_grid_import_w  0     # 0 = unlimited
optimizer.max_grid_export_w  0     # 0 = unlimited
optimizer.min_spread_eur_per_kwh 0.0  # ignore arbitrage thinner than this
optimizer.pv_charge_preference auto|prefer_pv|avoid_pv  # 0.19.0: price PV charging at sell (default), 0, or buy
optimizer.target_soc_pct_end  null   # hard end-of-horizon SoC target if set
optimizer.sell_floor_enabled  true   # 0.18.0: refuse a discharge/export below what the pack's energy cost
optimizer.sell_floor_margin_eur_per_kwh 0.0  # 0..1: require this much above the break-even
control.tick_seconds     30
control.replan_minutes   15
control.grid_follow      true
control.grid_follow_deadband_w 30
control.grid_follow_max_ratio  0.5
control.grid_follow_max_w      0     # 0 = derive the absolute grid-follow budget from capability x ratio (min 500 W)
control.stale_state_seconds    180   # older measurement => hold power
control.failsafe_on_error       true # write 0 W on unrecoverable error
protection.cell_voltage_enabled false # 0.16.0: watch the lowest cell and charge it out of danger
protection.cell_voltage_warning_v 3.10 # advisory per-cell threshold inside the cutoff..resume band
protection.cell_voltage_cutoff_v  3.00 # below this: latch emergency charge, block discharge
protection.cell_voltage_resume_v  3.18 # at/above this: clear the latch and plan normally
protection.emergency_charge_power_w 0  # 0 = use the battery's own max_charge_w
maintenance.full_charge_enabled false  # 0.17.0: reach the target SoC every interval so the BMS can balance
maintenance.full_charge_interval_days 30   # 1..365; how often every enabled battery must reach the target
maintenance.full_charge_target_soc_pct 100 # 50..100; terminal target while a full charge is due
notify.enabled           true
notify.persistent        true        # HA persistent notification
notify.mobile_service    ""          # e.g. notify.example_mobile_app, empty = off
notify.events            [plan_changed, plan_applied, battery_floor_reached,
                          negative_price, forecast_unavailable, driver_error]
batteries                []          # see §5, list of battery config objects
```

`config.py` exposes exactly:

```python
DEFAULTS: dict[str, object]
class Settings:                      # read-only view over DEFAULTS + options + DB overrides
    def get(self, key: str, default=None): ...
    def set(self, key: str, value) -> None: ...        # persists to store
    def all(self) -> dict[str, object]: ...
    def battery_list(self) -> list[BatteryConfig]: ...
    def update_battery(self, battery_id: str, patch: dict) -> BatteryConfig: ...
def load_options(path: str = "/data/options.json") -> dict: ...   # {} when missing
def resolve_location(ha_config: dict, options: dict) -> tuple[float, float, str]: ...
```

Location resolution order: option override → HA `/api/config` → raise `ConfigError`. A missing
location disables solar forecasting with a visible warning; it never invents coordinates.

---

## 4. Pricing semantics

All internal prices are **EUR/kWh, tax included, buy side**; sell prices are **EUR/kWh received**
(feed-in compensation net of fees). Providers normalise into that shape and record `raw_unit` plus
the normalisation applied in the plan payload so the UI can explain a number.

`PriceForecast` = ordered list of `PriceSlot(t, buy, sell, source, is_actual, energy_tax)`, 15-minute
resolution, covering at least `now` → `now + horizon_hours`; slots are gap-free and future-filled by
the last known value with `is_actual=False` and a `stale` flag so the UI can grey it out. The grid
starts at the start of the plan slot containing `now` (`optimizer.slot_minutes`): the plan grid is
floored to that step, so any later start would leave the plan's first slot - the slot the dispatcher
acts on - filled and stale, and a stale current slot is never executed (SPEC 9.1).

Unit handling: providers declare their own unit and, when they publish fixed-point integers, their
`scale_divisor`; `models.eur_per_kwh_from_raw` does the conversion. Magnitude-based guessing is
forbidden — a wrong guess here silently corrupts every cost number. `price.unit: auto` means "use the
provider's declared unit".

Sources (each a small pure function `fetch(...) -> PriceForecast` behind one registry):

| key | how | notes |
| --- | --- | --- |
| `zonneplan_entity` | read one HA price sensor's `forecast` attribute | default; unit auto-detected (§5 of docs/EXTERNAL_APIS.md); stale attribute detection is mandatory |
| `energy_charts` | `https://api.energy-charts.info/price?bzn=<zone>` keyless | day-ahead, `EUR/MWh`, zone from settings (`price.bzn`, default `NL`) |
| `entity_forecast` | any HA sensor whose attribute is a list of `{start/datetime, price}` | covers Nordpool/Tibber/ENTSO-E integrations generically |
| `static` | configured schedule | always available, used as last-resort fallback |

Mandatory behaviour: **a price source that yields no future slots is a failure**, not an empty
forecast. On failure, fall back in order `configured → energy_charts → static`, and raise a
`forecast_unavailable` event naming the source that failed. Never plan against a stale price list
without flagging `stale=True`; when every future slot is stale the optimizer must treat the price
as flat (equal prices) rather than guessing.

### 4.1 Provider and contract selection (0.3.0)

Choosing a source used to mean knowing which HA sensor to paste. The user now states **where** prices
come from (`price.provider`) and **what shape their contract has** (`price.contract`); the add-on
resolves the concrete source, and reports what it resolved to.

| `price.provider` | resolves to | allowed contracts |
| --- | --- | --- |
| `entity` (default) | the pre-0.3.0 explicit path: `price.source` + `price.entity`, chain `configured → energy_charts → static` | any |
| `zonneplan` | a Zonneplan tariff sensor **found in Home Assistant**: an entity whose attribute holds a fixed-point price list (`price_tax_included`/`price_tax_excluded` dicts, `value_factor 1e-7`) and whose own state agrees with its current slot (the existing scale check). The grid is detected structurally, from the slot spacing, never from the entity name: `dynamic_quarter_hourly` prefers the 15-minute grid, `dynamic_hourly` the hourly one, and falling back to the other grid is recorded as a note, never silent | `dynamic_*` |
| `day_ahead` | `energy_charts` for `price.bzn` (keyless wholesale day-ahead) | `dynamic_*` |
| `fixed` | the configured flat or day-night tariff (`fixed_tariff` source) on local-time windows. **No market fallback**: a fixed contract whose price is missing fails loudly instead of silently planning against market prices | `fixed_*` |

`price.contract` fixes the expected slot granularity (`dynamic_quarter_hourly` 15-minute,
`dynamic_hourly` hourly upsampled onto the 15-minute grid) and selects the day/night windows for
`fixed_day_night`: day = `price.fixed_day_start_hour` until `price.fixed_night_start_hour`, night =
the rest, evaluated per slot in the configured timezone so DST is handled by the window, not by an
hour count.

Fixed prices are **consumer prices incl. VAT and tax** (`price.fixed_price_eur_per_kwh`,
`price.fixed_day_price_eur_per_kwh`, `price.fixed_night_price_eur_per_kwh`); a value may instead be
read from an HA sensor (`price.fixed_price_entity`, `price.fixed_day_price_entity`,
`price.fixed_night_price_entity`), which wins when set. No adders are applied to either, because
they are already consumer prices. No price is ever inferred from an entity's *name*: a missing fixed
price is a `forecast_unavailable` failure carrying a hint that names the field to fill.

`price.vat_pct` / `price.energy_tax_eur_per_kwh` / fees keep their meaning: adders applied only to
sources that declare themselves ex-tax. `day_ahead` is one, so its forecast carries a note naming the
adders actually applied — a wholesale price with no adders configured is not a consumer price, and
the UI must be able to say so.

A provider/contract combination outside the table above is a **warning, not a failure**: it is
reported as `price_warnings` in `/api/v1/config` and the engine keeps the configured provider, because
refusing to plan is worse than planning with a stated mismatch.

### 4.2 Feed-in price (0.6.0)

The sell price is **EUR/kWh received**. It resolves in this order, once per forecast, in
`_apply_feed_in`:

1. `price.feed_in_entity` — a live HA sensor (its declared unit), plus `price.vat_pct` when
   `price.feed_in_tax_inclusive` is off.
2. `price.feed_in_eur_per_kwh` — a flat tariff, same VAT rule.
3. **Derived** (the default, and what the setup card shows): `sell = buy − price.export_fee_eur_per_kwh`
   (the feed-in cost, default 0), and additionally `− slot.energy_tax` when
   `price.feed_in_energy_tax_refund` is off (a contract that does not pay the energy tax back on
   redelivery). This replaces the pre-0.6.0 rule that a sell price is never inferred from the buy
   price: the Zonneplan consumer price *is* what a saldering contract pays back, and the user states
   the exceptions (feed-in cost, energy tax not refunded) instead of re-entering a tariff.

`slot.energy_tax` is the energy-tax component of `buy`, filled where the provider publishes both
sides (Zonneplan's `price_tax_included`/`price_tax_excluded` and
`electricity_price`/`electricity_price_excl_tax`; the difference is the tax), the configured
`price.energy_tax_eur_per_kwh` for wholesale sources (exactly the adder the normaliser applied), and
`0.0` for consumer sensors, where the tax is embedded and unknown. A configured feed-in source that
cannot be read degrades to the derived price with a note — pricing export must never fail the buy
forecast — and only an explicit `price.feed_in_eur_per_kwh = 0` forces a zero sell price.
`optimizer.allow_export` still defaults to off, so nothing exports until the user asks for it.

---

## 5. Batteries (pluggable, N of them)

```python
@dataclass(frozen=True)
class BatteryConfig:
    id: str                 # stable slug chosen at runtime, e.g. "battery_1"
    name: str
    driver: str             # "zendure" | "zendure_mqtt" | "zendure_native" | "generic"
    capacity_kwh: float     # usable AC-side capacity
    min_soc_pct: float
    max_soc_pct: float
    max_charge_w: float
    max_discharge_w: float
    eta_charge: float       # 0<eta<=1, AC->cell
    eta_discharge: float    # 0<eta<=1, cell->AC
    price_eur: float = 0.0  # purchase price (EUR); 0 = unknown, no derived wear
    cycle_life: float = 0.0 # rated full cycles; 0 = unknown, no derived wear
    capacity_source: str = "manual"  # manual | learned (SPEC 5)
    mapping: dict[str, str] # driver-specific entity mapping, resolved at runtime
    enabled: bool = True
```

**Learned capacity (``capacity_source``, additive 0.14.0).** A pack rarely holds its label's figure.
``capacity_source="manual"`` (the shipped default) keeps ``capacity_kwh`` as the effective capacity.
``capacity_source="learned"`` keeps ``capacity_kwh`` as a seed/fallback and uses an estimate learned
from the pack's own measured cycles for planning, allocation, UI and the published summaries. The
estimate pairs AC energy with SoC change over 5-minute rows (SPEC 7.3): for a charge window
``capacity = AC_charge_kWh * eta_charge / dSoC`` and for a discharge window
``capacity = AC_discharge_kWh / eta_discharge / |dSoC|`` (each efficiency sits on its own leg). A
window is accepted only with ``|dSoC| >= 10`` points, ``mean |power| >= 20 %`` of that battery's own
limit for the direction, finite values and no gap longer than one sample; accepted windows are
combined with a median after a median-absolute-deviation outlier filter, kept to the last 20, clamped
to a plausible 1..200 kWh and published only with at least 3 accepted windows. An estimate older than
30 days is stale. ``manual`` always wins; in ``learned`` mode a missing, stale or low-confidence
estimate falls back to the seed, and the transition is logged with a stable reason slug
(``capacity_learned`` / ``capacity_learned_stale`` / ``capacity_learned_low_confidence`` /
``capacity_learned_unavailable``). The estimate is runtime data (a ``learning_profiles`` payload per
battery), never written back into user settings.

Efficiency is entered per battery as a **round trip**: the UI/API field `round_trip_efficiency_pct` is
split evenly into `eta_charge = eta_discharge = sqrt(pct/100)`, and explicit per-direction values (when
given) override the split, after which the round trip is their product.

Wear is **derived per battery**, never typed in as a global number:
`wear_eur_per_kwh = price_eur / (cycle_life × usable_kwh × eta_discharge)` (0.0 while either input
is unset; the UI states the derived value and a full cycle's cost). Dividing by the AC energy a full
discharge actually delivers (`usable_kwh × eta_discharge`) is what makes one full cycle cost exactly
`price_eur / cycle_life`. It is the cost of one kWh **delivered** to the house, so the plan uses the
delivered-energy-weighted mean over the enabled fleet (`usable_kwh × eta_discharge` weights) and the
DP charges it on the discharge leg only. `optimizer.cycle_cost_eur_per_kwh` remains as an advanced
**override**: a non-zero value replaces the derived wear for planning and break-even. `usable_kwh` is
`capacity_kwh × (max_soc_pct − min_soc_pct)/100`, the same window the planner cycles.

The cost model uses `round_trip = eta_charge * eta_discharge`, so the break-even sale price for energy
bought at `buy` EUR/kWh is `buy / round_trip + wear_eur_per_kwh`: only the round trip appears because
the wear term is already per delivered kWh. Buying at 20 ct with a 75 % round trip breaks even at
**26.7 ct** before wear, not 25 ct (the 80 % case).

#### Cost basis of the stored energy and the sell floor (0.18.0)

The forward rule above only asks what a sale must earn back *from here on*; it has no memory of what
the energy already in the pack cost. ``optimizer.sell_floor_enabled`` (default **true**) adds that
memory: a per-battery **cost basis** and an optional floor that refuses a loss-making sale. The
basis unit is **EUR per kWh charged into the pack (AC)**, the weighted average price of the energy
currently held, so the floor is the frozen break-even helper with ``buy := basis``:
``floor = battery.break_even_sell_eur_per_kwh(basis, wear) + optimizer.sell_floor_margin_eur_per_kwh``.

**The ledger (``app/cost_basis.py``, ``learning_profiles`` kind ``battery_cost_basis``).**
Intervals are event driven, never timed buckets: a slice opens on an applied change the add-on
really made for that battery -- a ``dispatch_log`` row with ``mode auto``, ``dry_run`` false,
``result`` ``ok``/``skipped`` and an ``applied_w`` that differs from the last booked command by more
than 25 W (grid-follow micro corrections do not fragment the ledger) -- and closes at the next such
change or at the update's own ``now``. One row books at most one slice and the slice's end is stored
as ``last_ts``, so replaying an update books nothing twice. The slice's energy is **measured, not
commanded**: the persisted ``battery_power:<id>`` rows (else the ``battery_charge --
battery_discharge`` pair) are integrated piecewise-linearly between samples, so uncommanded device
flow, ramps and a vendor manager moving the pack are all accounted; a data gap longer than one
sample plus one missing sample (10 minutes) is never bridged, only the covered part is booked. Each
slice is priced at the plan slot it was charged in (the persisted plan that steered that slot,
newest plan winning for a slot start, split at every price-slot boundary an interval spans):
``grid_part = min(charge_ac, import_ac)`` from the measured meter import at the slot's ``buy`` and
``pv_part = charge_ac - grid_part`` at the slot's ``sell`` (the export forgone, the same convention
the DP's salvage uses). A missing ``sell`` falls back to ``buy``; when neither resolves the
sub-slice is skipped and counted, never priced by a guess. When the meter reading is missing the
plan slot's own reason is the fallback attribution (``pv_surplus_charge`` means PV, anything else
means grid). The running basis is
``basis = (basis * E + cost_eur) / (E + charge_ac)`` with ``E += charge_ac``; a discharge reduces
``E -= discharged_ac / round_trip_efficiency`` and leaves ``basis`` unchanged, clearing it when
``E`` reaches 0. At or below the SoC floor (``soc <= min_soc_pct + 1``) nothing usable is held, so
the ledger resets to ``basis=None``, ``E=0`` (before a slice that starts there and after one that
dips there); ``E`` is clamped at ``capacity_kwh / eta_charge``. A first run starts fresh and may
back-fill at most the last 7 days from the persisted dispatch log, budgeting only slices whose slot
price resolves exactly; a gap between updates longer than that lookback also starts clean, because
the contents can no longer be attributed. The last 200 booked slices are kept as an audit journal
(``ts``, applied power, reason, source split, price, energy, resulting basis); ``/status`` exposes
the last five per battery.

**The floor.** Each battery's floor is the frozen helper applied to its own ``basis``, per-direction
etas and effective wear (the advanced override when set, else the derived value) plus the margin;
the fleet floor the plan enforces is the **energy-weighted mean** of those per-battery floors, so a
battery holding nothing usable contributes nothing. In the DP a discharge/export transition whose
*value leg* sits below the floor is removed from the action set: the leg is the slot's ``sell`` when
the flow exports and the slot's ``buy`` when it serves the house, which is exactly the
``grid_kw_at(slot, p) < 0`` sign test. A slot that holds because of the floor reports
``Reason.SELL_FLOOR``. Charging (including the cell-voltage emergency charge and the scheduled full
charge), SoC-floor holds and everything outside ``auto`` are untouched, and with the setting off or
no basis yet the schedule is bit-for-bit the pre-0.18.0 one. A live *planned* discharge whose value
leg is below the floor is also clamped to a hold at dispatch, reported like the other clamped
reasons; an explicit override is never second-guessed. Exposure: ``/api/v1/status`` carries per
battery ``cost_basis_eur_per_kwh`` and ``stored_value_eur`` (defined once as ``basis * E``) plus a
``cost_basis`` block with the fleet floor, ``PlanSummary.sell_floor_eur_per_kwh`` carries it in
``/plan``, and the entities ``sensor.sem_battery_cost_basis`` (EUR/kWh) and
``sensor.sem_battery_value`` (EUR) mirror the fleet basis and the value of the held energy through
the same idempotent publisher, publishing ``unknown`` rather than inventing a figure.

**0.18.1 amendment (the quantity follows the settlement grid and the pack).** A live 0.18.0 ledger
under-reported the pack's energy by ~12 % on a 5-minute sampler cadence: each update booked its own
tail flat at the last visible sample, so the ramp the next sample revealed was never recovered, and
142 of 250 command intervals booked nothing at all. Energy now follows the samples and price
follows the commands: the covered sample spans are integrated between consecutive samples, split at
the applied command changes and the price-slot boundaries they cross, attributed to the command in
force, and accumulated into that command interval's single journal entry, so a command shorter than
the sample spacing still books its measured share. ``last_ts`` is the end of the last processed
sample span -- never a command timestamp -- and nothing beyond the newest settled sample is booked,
so a command interval whose next sample has not arrived waits for the next update instead of being
extrapolated flat, and replaying an update still books nothing twice. A settled command interval
with no covered sample span is counted in ``skipped_no_power``. After booking, the *quantity* is
reconciled with the pack's own content:
``E_phys = clamp(capacity_kwh * (soc - min_soc_pct) / 100 / eta_charge, 0, capacity_kwh / eta_charge)``
from the last SoC sample at or before the booking end (stale or missing: skip and count
``skipped_no_soc``). The difference is applied to ``energy_kwh`` and never to ``basis`` (the basis
remains the tracked average price of the energy we priced); the signed cumulative correction is
``reconciled_kwh``, the last step ``reconciled_last_kwh``, and a single correction beyond half the
pack capacity is unattributable: the ledger resets (``basis=None``, ``E=0``) and
``reconciled_resets`` counts it.

**0.19.0 amendment (the floor restricts discharge, and PV charging gets its own price).** The 0.18.0
floor branch used ``grid_lo = max(grid_lo, -base)``, so in a PV-surplus slot
(``base = load - pv < 0``) the lower bound became *positive* and the pack was forced to swallow the
whole surplus at any price: a live 30 h plan charged 08:00-10:00 instead of the cheaper 11:00-13:00
hours. The floor now uses ``grid_lo = max(grid_lo, -max(0.0, base))`` -- a deficit caps the
discharge at the house's own demand and a surplus leaves ``p >= 0`` while the PV exports freely. The
floor exists to stop *stored* energy being sold below its cost basis, and PV that would go straight
to the meter has no basis; ``_sell_floor_blocks`` and the dispatcher clamp are unchanged (they
already judge discharges only). ``optimizer.pv_charge_preference`` (``auto`` | ``prefer_pv`` |
``avoid_pv``, default ``auto``) sets the price the solver assigns to PV energy it charges into the
pack: the slot's ``sell`` (the export forgone; ``auto`` is then preferred exactly when
``sell < buy``), ``0`` (absorb the surplus before buying anything) or the slot's ``buy`` (export the
PV and charge from the grid in the cheapest hours). In ``slot_cost_eur``, for ``p > 0`` with
``surplus = max(0, pv - load)`` and ``pv_kwh = min(p, surplus)``, the chosen price adds
``dt * (pv_price - sell) * pv_kwh``. That is an internal shadow price only: ``cost_estimate_eur``,
``baseline_cost_eur`` and ``saved_eur`` remain the true grid cost of the returned schedule (SPEC
8.1), so no preference can invent a saving. ``allow_grid_charge=false`` keeps its PV-only bound and
``min_spread`` keeps charging only the grid part.

**0.21.0 amendment (the ledger prices only the energy above the reserve).**
``optimizer.reserve_soc_pct`` (SPEC 3) is a per-battery floor *above* ``min_soc_pct`` that the plan
never discharges below, and the allocator already measures the discharge room from
``floor_energy_kwh = capacity_kwh * max(min_soc_pct, reserve_soc_pct) / 100`` -- but the cost-basis
ledger still measured the held energy from ``min_soc_pct`` up. The reserve band is therefore never
sellable, yet its cost was weighted into the basis and into the fleet floor the DP enforces, so a
stale or dear reserve could refuse sales of cheap energy held above it. The ledger's floor level is
now
``floor_soc_pct = max(battery.min_soc_pct, optimizer.reserve_soc_pct)``: both the reset (nothing
sellable is held) and the SoC reconciliation
``E_phys = clamp(capacity_kwh * (soc - floor_soc_pct) / 100 / eta_charge, 0, capacity_kwh / eta_charge)``
use it, so the basis, ``energy_kwh`` and the energy-weighted fleet floor price only the energy above
the level the plan keeps. With the default reserve of 0 the level is ``min_soc_pct`` and the ledger
is exactly the 0.18.1--0.20.6 one; a pack sitting at its reserve holds nothing sellable, so its
basis is cleared and the floor is inactive until energy above the reserve is charged again.
Everything else -- slice boundaries, measured energy, price attribution, the stall marker and the
reconciliation reset -- is unchanged.

Runtime discovery is mandatory for `driver: zendure`: find candidate entities by asking HA for the
entity registry entries whose `platform` is `zendure_ha` (or `unique_id` prefix `zendure_ha.`) and
matching **translation keys** (`input_limit`, `output_limit`, `electric_level`,
`solar_input_power`, `output_home_power`, `available_kwh`, `total_kwh`, `soc_set`, `min_soc`,
`ac_mode`, `connection`). Never hardcode a device-derived entity id. The wizard shows the resolved
mapping and requires explicit user confirmation before the mapping is stored.

`driver: generic` takes an explicit mapping: `power_setpoint_entity` (signed W, + charge /
− discharge), or the pair `charge_power_entity` + `discharge_power_entity`, plus optional
`mode_select_entity`/`mode_manual_value`, `soc_entity`, `capacity_entity`. A generic battery with no
usable setpoint entity must be rejected at save time with a clear message, not at dispatch time.

`driver: zendure_native` (0.15.0) talks to the inverter's own local zenSDK HTTP API with the
standard library alone -- no `zendure_ha` entities, no MQTT broker, no cloud. Its `mapping` holds
**connection settings**, not entities: `host` and `serial` required, `scheme` (`http`/`https`),
`port`, `api_path` (default `/properties`), optional `token` (sent as `Authorization: Bearer`),
`timeout_s`, `insecure_tls` and `allow_unreachable`. `GET <api_path>/report` supplies SoC, the
measured AC pair, PV, the device's own limits and per-pack voltages; `POST <api_path>/write` sends
`{"sn": …, "properties": {acMode, inputLimit, outputLimit, smartMode}}` with the property sign
convention (magnitudes plus direction; positive charge maps to `inputLimit`, never a negative
number -- see `docs/EXTERNAL_APIS.md` section 6). Writes are clamped to the configured limit **and**
the device's reported maximum, and every write is read back against the device's own limit echo
within 5 W; a mismatch or timeout is a driver error, so the existing failsafe hold decides what
happens next. `read_cell_voltage()` exposes the lowest `packData[].minVol` (0.01 V) for a low-voltage
guard, and the driver reports no capacity in kWh because the device does not:
`capacity_kwh` stays the user's figure. One battery per inverter: a second battery with the same
`host`+`serial` is refused (only the first is driven), because both would overwrite each other's
limits. Saving a native battery probes the device once (bounded to
5 s): an unreachable host, a report with no properties or a mismatched serial is refused at save
time with the reason named, while `allow_unreachable` is the explicit opt-out (the card then shows
the read error). The doc records what is verified from the vendor documentation/source versus what
still needs live-hardware validation; the local MQTT transport is documented there as planned work,
not stubbed.

Aggregation for optimisation uses `capacity_kwh`, summed power limits and capacity-weighted
efficiencies; per-battery allocation then honours each battery's own limits (§8.3) and spends the
best-efficiency battery first. The weighted mean is an approximation when batteries differ
materially, so the plan reports the aggregate `eta_charge`/`eta_discharge`/round trip it assumed, and
every per-battery break-even is computed from that battery's own values.

---

## 6. `models.py` (frozen)

Exact contents live in `energy_manager/app/models.py`; the orchestrator owns that file. Summary of
the public surface every worker may rely on:

```
Enums:      Mode(off|advisory|auto), Action(hold|charge|discharge|pv_charge|export|
            self_consumption|reserve), Reason(str, human readable, stable slugs)
Dataclasses: BatteryConfig, BatteryState, SystemState, PriceSlot, PriceForecast, SolarSlot,
            SolarForecast, LoadSlot, LoadForecast, PlanSlot, PlanSummary, Plan, Measurement,
            AlertRecord, DispatchResult, SavingsBucket, AccuracyReport, ProviderStatus
Helpers:     to_iso(dt) / from_iso(s), quarter_floor(dt), slot_range(start, end, minutes),
             eur_per_kwh_from_raw(value, unit)  (documented, unit-tested conversions)
Constants:   PRICE_UNITS = {"auto", "eur_per_kwh", "eur_per_mwh", "micro_eur_per_kwh"}
Helpers:     BatteryConfig.round_trip_efficiency, BatteryConfig.wear_eur_per_kwh,
             BatteryConfig.break_even_sell_eur_per_kwh(buy, wear=None);
             Reason.EFFICIENCY_LOSS (a slot is idle because the spread does not cover the round-trip loss)
```

`SystemState` is the single source of truth the API reads: `mode, dry_run, ha_connected, location,
now, price_now, price_rank_pct, pv_now_w, load_now_w, grid_now_w, batteries[list[BatteryState]],
active_plan: Plan|None, last_plan_at, last_tick_at, alerts[list[AlertRecord]], provider_status,
version, uptime_s`. The API `/status` payload additionally reports `pending_holds[list[str]]` (batteries whose
last real command is not yet verified neutral) and `mode_persisted: bool`; the engine owns those two
values and they are not `SystemState` fields.

---

## 7. Forecasts

### 7.1 Solar (`sources/solar.py`)
`ForecastSolarProvider` calls the keyless v1 estimate endpoint for each configured plane, sums the
planes on a common 15-minute grid, and caches the raw response in SQLite (`forecast_cache`) with a
refresh policy of 30 minutes (keyless rate limits permitting; on HTTP 429 back off exponentially and
serve cache). Providers return `SolarForecast` with per-slot `pv_kw` plus `uncertainty_pct`
(default 25% until learned, then from measured error).

**Keyless resolution is hourly, not 15-minute** (verified, see appendix A): request with `?time=utc`, then
interpolate the hourly curve onto the 15-minute slot grid (linear between hourly samples, and keep the
first partial sample anchored at its own timestamp). Keyless also means **single plane** and a ~2-day
horizon; a personal key in the URL path enables multiple planes. Never log the response `message.info`
field: it echoes the requested coordinates back.

Measured bias learning: when `solar.bias_correction` and a measured PV entity are configured,
`analytics` compares realised PV against the forecast for the same slot and maintains a per
"slot-of-day bucket" (24 hourly buckets) multiplicative factor as an EWMA with `alpha=0.2`, clamped
to `[0.3, 1.8]`, stored in `learning_profiles`. Factors older than 14 days decay toward 1.0.

**No source to size the array (0.9.0).** `solar.source=forecast_solar` with an empty `solar.planes`
has nothing to forecast with. Instead of failing straight away, the provider runs the meter-derived
estimate (§7.1.2) when a meter resolves: an explicit `grid.*` mapping, or the Home Assistant Energy
dashboard (§3). An explicit `solar.capacity_wp` still wins inside the derived tier. Only when that
is impossible as well is the forecast empty and the status `no_pv_source`, with the derived failure
attached in the forecast notes and `status.detail.derived_fallback`.

#### 7.1.1 Capacity-based estimate (`solar.source = capacity`, 0.4.0)

An AC-coupled array whose geometry is not worth modelling needs no API and no planes: the user states
the array's watt peak and the add-on estimates production from astronomy, then refines it with the
same measured-PV bias learning above. This is deliberately a *simple, documented* model whose
errors are meant to be corrected by measurement, not a solar simulator.

Settings: `solar.capacity_wp` (required for this source, total Wp), `solar.tilt_deg` /
`solar.azimuth_deg` (optional; compass azimuth, 0 = north, 180 = south), `solar.performance_ratio`
(default 0.85) and `solar.weather_entity` (optional HA weather entity for cloud attenuation).

Per 15-minute slot, with latitude/longitude and timezone from Home Assistant:

1. **Solar position** from the standard declination/hour-angle equations (day-of-year declination,
   equation of time, longitude correction): elevation and compass azimuth. Elevation ≤ 0 → `pv_kw = 0`.
2. **Clear-sky global horizontal irradiance**, Haurwitz: `GHI = 1098·cos(z)·exp(-0.059/cos(z))` W/m²
   with `z` the zenith angle. Split beam/diffuse with a documented constant diffuse fraction
   (`0.25`), then transpose to the plane with the isotropic-sky model
   (`POA = DNI·max(0, cos(AOI)) + DHI·(1+cos β)/2 + GHI·ρ·(1-cos β)/2`, ground albedo `ρ = 0.2`).
3. **Cloud attenuation** when the weather entity yields cloud coverage for the slot, Kasten-Czeplak:
   `k = 1 - 0.75·(cc/100)^3.4`. Hourly forecast values cover the four slots of their hour. Without
   cloud data the estimate is clear-sky and the forecast says so.
3b. **NASA POWER observed clearness** (`solar.nasa_irradiance`, default on; capacity and derived
   sources only): observed hourly all-sky and clear-sky irradiance (W/m², keyless, ~2-3 days
   near-real-time latency) over the last 21 days ending before that latency are reduced to a median
   all-sky/clear-sky ratio per local hour-of-day — daylight samples only, at least 3 samples per
   hour, clamped to `0.05…1.1`, cached daily per location and timezone (the buckets are local hours,
   so the timezone is part of the cache identity). That ratio is applied to slots the weather
   entity does not cover (step 3 wins where it does), and it refines the derived fit (7.1.2). It is
   an addition to the model, never a replacement: a failure is a note and the slot stays clear-sky,
   and a failed fetch is remembered for 3 hours so an offline NASA costs one request, not one per
   refresh. The learned per-hour bias (§7.1) is applied on top unchanged: it was fitted against the
   NASA-free model, so an install with a trained bias may under-forecast for a few days until the
   hourly EWMA adapts to the new baseline.
4. **Power**: `pv_kw = (capacity_wp/1000)·(POA/1000)·performance_ratio`.

Uncertainty is higher than an API forecast because the model is coarse: `35%` without cloud data,
`30%` with the NASA clearness and `25%` with hourly cloud coverage, and the learned per-hour
correction narrows it in practice. The forecast **always**
carries notes stating the capacity used, the tilt/azimuth actually assumed when they were left unset,
the performance ratio, whether cloud attenuation was applied and from which entity, and that a
measured PV entity will refine the curve.

Assumed tilt/azimuth are stated, never silent: an unset value is reported as an assumption. The
measured entity for an AC-coupled array is its **inverter's AC output**, not a meter's export
register, because self-consumed energy never reaches the meter and would bias the learned factor
low - the setting's help text and the forecast note both say so.

A missing capacity is a failure with a hint naming `solar.capacity_wp`, never a guess from the array's
history: the bias correction may only adjust a forecast that already exists.

#### 7.1.2 Meter-derived estimate (`solar.source = derived`, 0.6.0)
An AC-coupled array with no inverter sensor and no geometry to enter can still be estimated, because
the meter already sees it: whenever the signed grid power is negative the house is exporting, which
means production exceeds the house load. With the project's sign convention `grid = load − pv + p`
(§8.1, `p > 0` = charging), the array is producing at least `pv = −grid + p` at that moment.

`load.collect_pv_export_samples` assembles those samples from the same roles the derived load tier
uses: `grid.power_entity` (or the import/export pair, or the Energy-dashboard fallback: its power
sensor, else the import/export energy counters as `grid_energy_import`/`grid_energy_export`) and,
when a battery is configured, its AC flow
(`charge_power_entity`/`discharge_power_entity`, else the signed `power_setpoint_entity`; a battery
whose flow cannot be read refuses the tier rather than letting its discharge read as PV). Each series
is read from long-term statistics (`recorder/statistics_during_period`: the hourly `mean` of a power
statistic, the hourly `change` of an energy counter converted to average kW) whenever a WebSocket
client is available, with recorder history as the fallback — so the window is the never-purged
statistics table, not the recorder's purge window. Stamps
without export carry no information and are skipped; the house load at export moments is *not*
subtracted (that load is what the PV forecast is needed to learn), so every sample is a lower bound.

`solar.capacity_wp` then learns itself: each sample is divided by the clear-sky model's output per
installed watt at that timestamp (the §7.1.1 model, with the same tilt/azimuth assumptions and
performance ratio), **multiplied by the NASA POWER clearness of that sample's local hour** when
`solar.nasa_irradiance` is on, and the 90th percentile of those ratios is the array size — overcast
hours pull the ratio down, so the top of the distribution is the cloudless envelope, and the
clearness term stops a partly cloudy window from halving the learned size. At least 24 usable daylight
samples are required; fewer is a status naming the meter, never a crash. The learned value is stated
in the notes and in the status detail (`capacity_source`, `derived_samples`); it is not written back
to the settings, and an explicit `solar.capacity_wp > 0` wins over it (the same "explicit beats
derived" rule the load tier follows). The fit itself is cached in `learning_profiles`
(`solar_derived`/`capacity`) with the entity mapping it was fitted from and whether the NASA
clearness was part of the fit, and is refit at most once a day, because it reads weeks of history
(long-term statistics when the WebSocket client answers, recorder history otherwise) and the array
size does not change between refreshes.
The forecast itself is then the §7.1.1 capacity model, so
`solar.tilt_deg`, `solar.azimuth_deg`, `solar.performance_ratio` and `solar.weather_entity` apply
unchanged, and the forecast carries a note that the learned size is a conservative lower bound.

#### 7.1.3 Multi-source merge (`solar.sources`, 0.12.0)
One provider is rarely the best estimate at every hour, and Home Assistant may already be running
several solar-forecast integrations. `solar.sources` is an **ordered** list of entries
`{source, weight, ...provider-specific keys}`; while it is empty (the shipped default) the
single-source `solar.source` path above runs unchanged. A non-empty list builds one series per
source on the same 15-minute grid and merges them per slot:

* **Sources.** `forecast_solar` (the §7.1 API), `ha_energy` (whatever solar-forecast providers the
  Home Assistant Energy dashboard has configured, read over the WebSocket command
  `energy/solar_forecast` — Solcast, Forecast.Solar and any other integration that registers an
  energy platform), `capacity` and `derived` (§7.1.1/§7.1.2). Provider-specific keys (`planes`,
  `api_key`, `capacity_wp`, `tilt_deg`, `azimuth_deg`, `performance_ratio`, `weather_entity`,
  `nasa_irradiance`, and `entry_id`/`name` for `ha_energy`) override the matching `solar.*` setting
  for that entry only; an unknown key is a validation error.
* **Alignment.** Each source's samples are placed on the grid with the same linear interpolation and
  timezone conventions the single-source path uses, so a mix of hourly and 15-minute sources lines
  up without a special case.
* **Merge.** `solar.merge_strategy` = `weighted_mean` (default: `Σ wᵢ·vᵢ / Σ wᵢ` over the sources
  present at that slot), `median` (equal say per source, ignoring weights) or `max` (the most
  optimistic source). Weights must be finite and `> 0`. A source that is disabled, empty or failed
  is skipped with a note; the remaining sources still produce a forecast. If **no** source produces
  a usable series, the §7.1/§7.1.1/§7.1.2 single-source chain runs as the fallback and the notes say
  so. Bias correction (and NASA clearness where it applies) keeps working inside each source before
  the merge, so the merged curve is corrected once, per source.
* **Status.** Every configured source is reported with its name, weight and a coarse state
  (`ok | degraded | failed | disabled`) plus a one-line note, on the solar provider list and in the
  `/api/v1/forecast?kind=solar` payload (`sources`). The Solar setup card renders the same list and
  can add, remove and reorder entries through the ordinary dirty/Save flow; the JSON field in All
  settings is the fallback editing path.

The websocket contract is recorded in docs/EXTERNAL_APIS.md section 5: the command takes no
parameters and answers `{config_entry_id: {"wh_hours": {<ISO timestamp>: <Wh>}}}`; several providers
are summed point by point, and one `entry_id` selects a single provider. A missing command, an empty
dashboard or an unavailable provider is a note, never a failed forecast.

#### 7.1.4 Learned residual model (`solar.source = learned`, 0.22.0; irradiance chain and weather terms 0.23.0)

A model trained offline on 174 other Dutch rooftop arrays, shipped as a committed artifact and served
by `app/forecast/pv.py` with the standard library only (R3). It answers the one question a fresh
install cannot answer from its own history: what will *this* array produce, before there is any local
history to learn from.

**What is predicted, and why it is a ratio.** The artifact outputs a dimensionless multiplier on a
physical estimate, never a power in watts. Capacity, location and orientation enter as *features*, so
one model serves an array it has never seen; a direct power regression would have to be refitted for
every install, which is the opposite of a cold-start prior.

**The reference physics is not the capacity tier's.** This is the part that must not be got wrong,
because the two differ by tens of percent at low sun and high cell temperature:

| | §7.1.4 (this model's reference) | §7.1.1 capacity estimate |
| --- | --- | --- |
| diffuse split | Erbs, from the clearness index | constant 25 % |
| module temperature | NOCT model from air temperature | not modelled |
| inverter | `eta = 0.96` plus hard AC clipping | folded into one `performance_ratio` |

So the residual multiplies *this* reference, and **`solar.performance_ratio` is not applied** when
the learned source is active: the artifact already carries the inverter, module-temperature and
soiling terms that ratio stands in for. The forecast notes say so. Multiplying a residual fitted
against one reference by an estimate derived from the other would be wrong by the difference between
them.

**The model is fitted on the chain it is served, and that is not a detail.** Through 0.22.0 the
corpus carried *satellite-observed* horizontal global irradiance from PVGIS-SARAH2, while the add-on
reconstructs irradiance as clear-sky Haurwitz attenuated by forecast cloud. Those are two different
estimators of the same quantity, so the residual the model learned was partly a correction for an
irradiance model that is never used in service. Measured on the chain it would actually see, the
0.22.0 artifact degrades from 115.8 W to 150.5 W in-sample and picks up an ~8 % energy bias.

From 0.23.0 the corpus is built from the serving chain — `Haurwitz(geometry) x Kasten-Czeplak(observed
cloud)` — and the model is handed the ingredients of that chain as features rather than only its
result: `cloud_pct`, `attenuation`, `clear_sky_ghi_wm2`, `wind_ms`, `rh_pct` and `rain_mm`, each
computed per system from per-system geometry. Every one of these is a field a runtime weather forecast
publishes, which is the entry requirement: a feature the add-on cannot observe at serving time would
quietly make the model worse.

| training chain → served chain | held-out systems | held-out years |
| --- | --- | --- |
| SARAH2 → SARAH2 (0.22.0, as shipped) | 5.102 % | 5.548 % |
| SARAH2 → runtime chain (0.22.0, in service) | ~8 % (degrades) | — |
| runtime → runtime, 22 features | 5.454 % | — |
| **runtime → runtime, 28 features (0.23.0)** | **4.742 %** | **5.620 %** |

Energy bias falls to +0.087 % against a physics prior of +9.518 %. `attenuation` is single-sourced
through `pv.cloud_attenuation`, and the NASA clearness path inverts that same expression into a
consistent `cloud_pct` rather than passing `cloud_pct = 0` beside an attenuation of 0.75 — two
contradictory statements about one sky.

**The clearness index is still normalised by extraterrestrial irradiance**, so an estimated value lands
inside the range the model was trained on rather than outside it, which is the property that makes
serving it viable at all. It does not make the estimate measured, so the uncertainty band stays the
§7.1.1 one (35 % clear-sky, 30 % with NASA clearness, 25 % with cloud coverage) rather than the
artifact's own held-out accuracy, and a note states that the model is driven by an estimated clearness
index. Wind speed, humidity and precipitation come from the same `weather.get_forecasts` call as the
cloud map; a field the entity does not publish is passed as zero **and stated as an assumption**, and
its unit is converted from the entity's own (`_WIND_TO_MS`, `_RAIN_TO_MM`) because a km/h value read
as m/s is a 3.6x error that still looks like a plausible number.

**What is still not modelled, stated rather than implied.** Snow cover, soiling and horizon shading are
absent. The corpus is Dutch and its *targets* contain all three, so the model partly absorbs them as
average weather rather than responding to them: a snow-covered array will be over-forecast, and a
shaded one will be over-forecast at low sun. Rain is a feature, but instantaneous rain is a weak proxy
for the multi-week soiling it would really stand for, and no rolling accumulation is used because the
add-on has no stored precipitation history to compute one from. These are named here so a wrong
forecast in those conditions can be recognised as a known limitation rather than debugged as a bug.

**Air temperature** comes from the weather entity's hourly forecast when there is one, else the
current reading of `load.temperature_entity` held flat, else a stated constant
(`DEFAULT_AIR_TEMPERATURE_C = 15`). The module-temperature term moves power about 4 % per 10 °C, so
which of the three applied is stated in the notes rather than left implicit.

**Settings.** `solar.capacity_wp` (DC watt peak, required — a missing one is a failure naming the
setting, never a guess from history) and the optional `solar.ac_capacity_wp` (the inverter's AC
limit). When the AC limit is unset it is assumed equal to the watt peak, which is a stated assumption:
a DC/AC ratio above one clips, and a model that is not told about the inverter limit will keep
forecasting output the inverter cannot deliver. `solar.tilt_deg`, `solar.azimuth_deg`,
`solar.weather_entity`, `solar.nasa_irradiance` and `solar.bias_correction` apply as in §7.1.1,
including the assumed-geometry notes.

**Status and provenance.** The provider status carries the artifact's own provenance — the training
corpus, its licence, when it was fitted, how many arrays, and the held-out error on both a
held-out-system and a held-out-year split — so a forecast can always be traced to a model rather than
referred to as "the learned thing".

**Precision.** Weights are stored as IEEE-754 bit patterns held in integers. Two reasons, both
load-bearing: a decimal round-trip changes comparisons for rows sitting exactly on a split threshold,
and dense arrays of four-to-five-decimal values are the shape R1's audit correctly treats as
coordinates. `tests/test_forecast_pv.py` pins the port against golden vectors emitted by the training
pipeline, whose own `--cross-check` proves those vectors come from the arithmetic that fitted the
model. A transcription error in the port would otherwise be invisible: it would merely look like a
slightly worse forecast.

### 7.2 Load (`sources/load.py`)
Sources in priority order, all producing 15-minute `LoadForecast` slots with `p10/p50/p90`:

1. **Long-term statistics** via `haws.statistics_during_period` for the mapped load entity (60 days,
   `hour` period) — preferred, survives recorder purging.
2. **Recorder history** (`GET /api/history/period`) if statistics are unavailable.
3. **Derived** when no load entity is mapped: `load = grid_import − grid_export + pv + discharge −
   charge`, assembled from mapped power sensors (the Energy-dashboard roles when `grid.*` is empty;
   each series is read through long-term statistics when a WebSocket client is available, energy
   counters included).
3b. **Night-derived** (0.24.0) when the derived tier cannot run because there is no `pv` series at
   all. Once the sun is below −6° — deeper than the horizon, because a tilted plane still collects
   diffuse light at 0° — production is zero for any array, so `load = grid − battery` needs no pv
   reading and the circularity is broken: the derived tier needs pv, pv can only be inferred from
   load, and an install whose inverter reports nothing has neither. A missing location is refused
   rather than guessed, since guessing it would admit daylight as if production were zero.

   The night tier observes roughly half the day and therefore **cannot learn a day shape**; it fills
   the hours it never saw from the documented default residential shape. Two things must be true for
   that to be safe, and neither is automatic:

   * the fill happens *inside* profile building, before the level is derived. An unobserved bucket
     otherwise reads as a measured 0.0 kW — forecasting a literal zero all day — and, worse,
     `profile_mean` is then averaged over a mostly-empty day, so `scale` corrects the mistake by
     inflating **every** hour up to its clamp, including the hours that were actually measured;
   * the fill is normalised so the finished day averages the observed level, rather than by the default
     shape's own mean. Those differ, because only the unobserved hours are being filled and the
     daytime half of a residential shape sits above its own average; normalising by the shape mean made
     a synthetic 1 kW house forecast 2.0 kW at night.

   The forecast reports the assumption (`assumed_daytime_shape` in the profile, a note on the tier),
   because a level measured at night and a shape taken from a table are different kinds of knowledge.
4. **Fallback** flat profile from `load.base_kwh_per_day` spread by a default residential shape.

**Which series a label came from is always recorded** (0.24.0). The sampler writes inferred values to
`actuals.pv_derived_kw` / `actuals.load_derived_kw`, never into `pv_kw` / `load_kw`, so an inferred row
and a measured one stay tellable apart; rows written earlier keep their NULLs rather than being
retroactively claimed as inferred. The fine-tune head (§7.5) prefers a measurement and falls back to
the inferred column only where the measured one is empty throughout its window, reporting which it
used (`label_source`). A correction fitted on a number this add-on produced itself is only as good as
the arithmetic behind it, and the user needs to be able to see that it is that kind of number.

#### Named load devices (`load.devices`)

One house-wide profile plus one house-wide temperature slope is the wrong shape for a heat pump (which
answers to the weather) and for an EV charger (which answers to its schedule): blending them produces
one compromise term that fits neither. `load.devices` is a list of `{entity, kind, name}` entries, each
naming the meter of one such load. `kind` is a closed set — `heat_pump`, `ev_charger`, `water_heater`,
`pool_pump` — and it decides whether the device gets its own temperature term: a heat pump, a water
heater and a pool pump do; an EV charger's draw is a schedule and gets none, so a slope fitted to it
cannot absorb whatever else varies with the weather as if it were the car.

Each device is a second profile under the same `learning_profiles` kind as the base profile, keyed
`device:<entity>` — the entity id is the only identity that stays unique when two devices share a kind
— and the forecast is `base + sum(modelled devices)`.

**The subtraction is what makes it safe, not a detail of it.** The measured total already contains the
device, so the base profile is learned from the total minus every device that was actually measurable,
and each of those is then added back above it. Adding a device to a base that still contains it
double-counts it, and the doubling hides inside the temperature term: it reads as a steeper slope, not
as an error. Concretely:

* an hour is kept for the base only when the total and *every* subtracted device have a reading for it.
  A missing device reading is unknown, not zero — reading it as zero would leave that device's draw in
  the base and then add the device's profile on top of it. The hours lost this way are counted, and a
  non-zero count is reported in the forecast notes;
* a device whose meter covers less than half of the house's hours (or fewer than 48 usable hours) is
  refused outright rather than subtracted from the few hours that overlap: it stays inside the base
  profile, and the forecast names it with the reason recorded when the base was learned;
* the base profile records which devices were `devices_subtracted` and why the rest were
  `devices_excluded`. A stored device profile is added back only when the *current* base names its key
  as subtracted, so a stale profile (device removed and re-added) is never added to a base that still
  contains the device;
* the kind is the authority on the temperature term at serving time as well as at learning time: a
  device re-configured from heat pump to EV charger drops its stored slope immediately (the profile is
  re-fitted on the next learning run) rather than contradicting the kind it is configured as now;
* a device with no profile yet — configured after the last learning run, or unreadable — is not added,
  because the base still contains it. Nothing is guessed to make the forecast look complete.

A device's own profile is learned from the device's own meter (not from the leftover), with a
temperature term only for the kinds above; it is deliberately *not* filled from the default
residential shape, because that shape describes a house's occupancy and not one appliance's schedule.
The forecast notes name every device that was modelled and every one that was not, with the reason,
and the load provider's `detail` carries the two counts: an exclusion changes what the base profile
*means*, and that is not visible in a kW curve.

Cold start is the one case with no subtraction at all: `load.base_kwh_per_day` is a whole-house
fallback, so while no base profile has been learned no device is added and the notes say so.

Model: median per (weekend|weekday, hour) profile over the last 8 weeks, scaled by
`recent_7d_mean / profile_mean` (clamped `[0.5, 2.0]`), then an EWMA bias correction per hour bucket
over the last 7 days, then an optional least-squares temperature term
(`load = a + b*T_forecast`, applied only when the regression explains ≥ 5% of variance).
Hourly values are linearly interpolated to 15-minute slots. Cold-start (no history) uses the
fallback profile and reports `confidence: low`.

### 7.3 Measurement sampling (`sources/measure.py`)
Every `control.tick_seconds`, sample grid power, PV power, load power, per-battery power and SoC into
`actuals` (5-minute resolution, upsert, targeted per-entity reads only). These rows are the ground truth for savings and accuracy
accounting, so sampling must never be skipped because a write failed.

---

### 7.4 Forecast archive and forecast skill (0.22.0)

The `forecast_archive` table records what each provider predicted for each future slot, so forecast
skill is a measurement rather than an impression, and a change to a learned correction can be
accepted or rejected on evidence.

Rows are **upserted, keeping the newest issue per `(kind, source, slot)`**, and only future slots are
written. Appending would store thousands of rows a day for one number per slot that matters — the
most recent forecast made *before* the slot, which is the one the planner acted on — and rewriting an
elapsed slot from a later issue would let a provider quietly improve its own record. The forecast is
archived after the learned correction, because the correction is what the planner used and scoring
the uncorrected curve would grade a forecast nobody acted on.

`Store.forecast_accuracy` reports per-source error against the measured series, compared on **hourly
means of both sides**: the archive holds 15-minute slots while the sampler writes at its own cadence,
and joining those on an exact timestamp would match almost nothing and report a flattering empty
result. A kind with no measured series is a `ValueError`, never an empty score — reporting "no error"
for a series nothing measured would be the most flattering possible bug. Archived forecasts are
trimmed with the actuals they are scored against (§11): it is the comparison that is worth keeping,
not the prediction.

### 7.5 Nightly fine-tune head (0.22.0)

`app/forecast/local_head.py` fits a per-hour-of-day multiplier from this house's own archived
forecast-versus-measured record, so the base model's systematic error for *this* array is corrected
without a rebuild.

**Closed form.** `m_h = (Σ f·a + λ) / (Σ f² + λ)` — ridge regression against a prior of exactly 1.0.
With no data the estimate *is* 1.0 rather than a division by zero; with little data it barely moves;
with enough it becomes the least-squares fit. `λ = 25` (about three hours of a 3 kW array), chosen
over-strength so that one afternoon cannot rewrite an hour of the day. Buckets are **local** hours,
matching the §7.1 EWMA, and an hour with no scored slots keeps exactly 1.0 rather than borrowing a
neighbour's. Factors are clamped to the §7.1 bounds `[0.3, 1.8]`.

**The gate is part of the feature.** A fit is only a candidate. It is adopted only when it beats the
incumbent — whatever correction is in force, the shipped EWMA factors or an already-promoted head — on
a holdout of the newest 3 days by more than `PROMOTION_MARGIN = 2 %` of that error. Fewer than 36
scored slots across 10 hours of the day is refused with a reason. The previous profile is kept, so
`revert` undoes a promotion in one write: a head that looked better on the holdout and turns out worse
in service must be removable without a rebuild.

**It replaces, it does not stack.** When a head is promoted, the learned source uses its factors
*instead of* the §7.1 EWMA factors. Two multiplicative corrections fitted to the same signal would
count the same systematic error twice, and the failure would look like an ordinary cloudy week.
`solar.bias_correction` remains the master switch for both.

Refits happen at most once every 20 hours, from inside the provider, in the same shape as the
derived-capacity refit (§7.1.2). Every failure mode is a non-event: a fit that cannot be computed is
not promoted, a profile that cannot be stored is not adopted, and neither can fail a forecast.

## 8. Optimizer

### 8.1 Model
Discrete time slots of `optimizer.slot_minutes` (default 60, matching the common hourly dynamic
tariff) over `optimizer.horizon_hours` (default 30, clamped to `[12, 48]`). For slot `i`: `pv_i` (kW), `load_i` (kW), `buy_i` (EUR/kWh),
`sell_i` (EUR/kWh), `dt` = hours.

Aggregated battery: usable energy `E_cap = Σ capacity_kwh * (max_soc−min_soc)/100` (kWh),
AC-side charge limit `P_chg = Σ max_charge_w/1000`, discharge limit `P_dis = Σ max_discharge_w/1000`,
`eta_chg`/`eta_dis` = capacity-weighted means, floor `E_min` and ceiling `E_max` in kWh derived from
the strictest min/max SoC across enabled batteries.

Sign convention: AC-side battery power `p_i` in kW, **positive = charging, negative = discharging**.

```
grid_i = load_i − pv_i + p_i        (positive p_i charges, which *raises* grid import)
cost_i = dt * ( buy_i * max(grid_i, 0) − sell_i * max(−grid_i, 0) )
```

Subject to
`grid_i ≤ max_grid_import_w/1000`, `−grid_i ≤ max_grid_export_w/1000` (when set),
`−P_dis ≤ p_i ≤ P_chg` (0 if `allow_grid_charge` is false and the slot has no PV surplus),
`E_min ≤ e_i ≤ E_max`, energy transition
`e_{i+1} = e_i + dt*(p_i*eta_chg)` for `p_i ≥ 0`, `e_i + dt*(p_i/eta_dis)` for `p_i < 0`
(single-expression form allowed but must be exact at `p=0`).

Objective: minimise `Σ cost_i + wear_eur_per_kwh * delivered_kwh_i − salvage * e_N`, where
`delivered_kwh_i = max(0, e_i − e_{i+1}) · eta_dis` (wear is charged on the discharge leg only: a
charge-only schedule pays none, and a full cycle pays `price / cycle_life`) and
`salvage = optimizer.salvage_multiplier * median(buy over horizon) * eta_dis` — stored energy delivers
only `eta_dis` AC per stored kWh, so valuing it at the full buy price would over-charge the pack —
exists so the schedule does not dump the pack for a trivial gain.

Arbitrage is only planned when the sale price clears the round-trip break-even
(`sell_j > buy_i / round_trip + wear_eur_per_kwh`, with `buy_i` the charge slot's price); a slot
left idle for that reason reports `Reason.EFFICIENCY_LOSS`, which is distinct from the user's own
`optimizer.min_spread_eur_per_kwh` floor (`Reason.SPREAD_BELOW_THRESHOLD`). `optimizer.target_soc_pct_end` adds a hard terminal constraint when set
(infeasible target ⇒ drop to a soft penalty and raise an alert, never crash).

0.19.0: the PV part of a charge may carry an internal shadow price set by
``optimizer.pv_charge_preference`` (§5) -- ``auto`` prices it at ``sell`` (exactly the formula
above), ``prefer_pv`` at 0 and ``avoid_pv`` at ``buy`` -- so the objective gains
``dt * (pv_price - sell) * min(p, max(0, pv - load))``. The reported money
(``cost_estimate_eur``, ``baseline_cost_eur``, ``saved_eur``) stays the true grid cost of the
returned schedule, never the shadow-priced objective.

Scheduled full-charge maintenance (0.17.0) reuses that same terminal-target mechanism: while a
tracked battery is due, the solver aims for ``maintenance.full_charge_target_soc_pct`` (or the
higher ``optimizer.target_soc_pct_end`` when the user set one) so the DP charges in the cheapest
slots before the deadline. The engine computes the per-battery due dates (``app/full_charge.py``)
and passes a maintenance block to ``solve``; a block whose ``active`` is false never changes the
terminal target, so a non-due battery cannot distort the schedule. Reaching charging slots keep
the stable ``terminal_target`` reason. Reaching the target (measured SoC ≥ target − 1 point)
records ``last_completed`` and moves the due date an interval out; a deadline past ``due + 24 h``
raises a rate-limited ``full_charge_overdue`` alert and keeps trying, never marking a completion
it did not observe. Due-date arithmetic keeps the local wall-clock time across DST
(``full_charge.add_days_local``).

### 8.2 Algorithm (`optimize/dp.py`)
Dynamic programming over discretised energy:

- state grid: `E_min → E_max` step `optimizer.soc_step_kwh`, at most **400** states (coarsen the step
  if the pack is large; log the effective step).
- action set per state: `optimizer.power_steps` evenly spaced values in `[p_lo, p_hi]` where `p_lo/p_hi`
  are the feasible bounds after clipping by grid limits and SoC reachability; always include `0`.
- transition cost uses the interpolated value of the next slot's value function at the exact
  reachable energy, so quantisation does not bias the decision; reconstruct the optimal path by
  storing the argmin per (slot, state).
- complexity target: `slots * states * actions ≤ 2_000_000` iterations; if exceeded, widen the step
  and log a `debug` line.

Return `Plan` with `slots`, `summary` (`cost_estimate_eur`, `baseline_cost_eur` = do-nothing
schedule cost over the same horizon, `saved_eur`, `charged_kwh`, `discharged_kwh`, `cycles`,
`arbitrage_kwh`), and `feasible: bool` + `infeasible_reason`. **Determinism is required**: identical
inputs produce byte-identical plans (no dict/set iteration order leaking into results).

Each `PlanSlot` also carries a human-stable `reason` slug: `arbitrage_charge`, `pv_surplus_charge`,
`peak_discharge`, `self_consumption`, `reserve_hold`, `grid_limit_hold`, `spread_below_threshold`,
`terminal_target`.

### 8.3 Allocation (`optimize/allocate.py`)
Greedy, deterministic: distribute the slot's total charge demand across enabled batteries
proportionally to remaining headroom (`max_charge_w`, and SoC room), and discharge demand
proportionally to available energy above floor; clamp per battery, redistribute the shortfall once
(no unbounded loops), and return `list[tuple[BatteryConfig, float]]` summing to the requested total
minus any physically impossible remainder. Efficiency differences must be respected: prefer the
battery with the better efficiency when only part of the demand fits.

The discharge floor is **per battery**, `max(min_soc_pct, reserve_soc_pct)` of *that* battery's own
capacity — never an aggregate-only constraint, or a nearly empty pack gets drained past its floor while
a fuller one is untouched. `reserve_soc_pct` is a parameter of the allocation helpers and every caller
(dispatcher, engine) must pass it; the end-to-end path is covered by a test, not just the helper.

---

## 9. Control loop, drivers, safety

### 9.1 Loop (`control/dispatcher.py`)
Runs on its own thread (`control.tick_seconds`, default 30 s):

1. Sample measurements (§7.3).
2. Replan if: no plan, plan older than `control.replan_minutes`, price/PV/load inputs changed
   materially (price vector changed in any future slot, PV changed >10%, SoC out of plan tolerance
   >3%), or a forced replan was requested through the API.
3. Compute the current slot's target power from the plan, interpolating the slot boundary change.
4. Optional grid-follow correction: `Δp = −(grid_measured − grid_planned)`, applied when
   `|Δp| > deadband` and `|Δp|` is inside an absolute watt budget — `control.grid_follow_max_w`
   when non-zero, else `max(500 W, battery capability * control.grid_follow_max_ratio)` — keeping the
   result inside every limit. The budget is independent of the planned power, so a zero planned
   power can still be corrected.
5. `control.safety.check(...)` returns an adjusted, allowed power plus reasons.
6. In `auto` and not `dry_run`: write through the drivers; otherwise log the would-be write as a
   `dry_run` dispatch entry (visible in the UI log).

Every step is recorded in `dispatch_log`, including rejections with their reason — the UI's answer to
"why didn't it charge?" must always exist.

### 9.2 Drivers (`control/drivers/`)
```python
class BatteryDriver(Protocol):
    def id(self) -> str: ...
    def read_state(self) -> BatteryState: ...
    def apply(self, power_w: float, *, dry_run: bool) -> DispatchResult: ...
    def hold(self, *, dry_run: bool) -> DispatchResult: ...   # safe zero/neutral
    def capabilities(self) -> dict: ...                        # limits, what it can express
```
Writes go through the HA service API (`POST /api/services/...`) or state-setting only where a
service does not exist. Each `apply` is idempotent within a slot: skip the write when the device
already reports the target within tolerance (avoid hammering the device — the SolarFlow is a BLE/cloud
device with finite write budget). On a *device-limits* path (`control_path: "device"`, and the
`zendure_mqtt` bridge, whose mapping has no setpoint entity) "the device already reports the target"
means the **`input_limit`/`output_limit` echo**, never the measured AC flow: the flow can read 0 W
while the limits still execute, which is how a hold was skipped as "already there" and left a 153 W
charge limit armed on the live 2026-10-14 install (0.21.1). If that echo cannot be read, the check
fails **open** -- the write happens rather than the skip.

`zendure.py` must: put the Zendure manager into manual dispatch before controlling power, express
charge vs discharge with the driver's own entity semantics (verified in `docs/EXTERNAL_APIS.md`),
respect device limits when writing, verify read-back after each write inside tolerance, and treat a
mismatch as a driver error (not a silent success). Its engine hook `commanded_power()` reports what
the device was last told to do: on `control_path: "device"` that is the `input_limit`/
`output_limit` read-back, never the measured flow, because a momentary 0 W flow while the limits
still execute must not read as "neutral" (0.17.1). It must never write to `soc_set`/`min_soc` on
every tick — SoC floors are expressed as optimized power, with the device SoC limits configured
separately and rarely.

Every driver reads an optional mapped `cell_voltage_entity` (0.16.0) into
`BatteryState.extra["cell_voltage_v"]` — the lowest per-cell voltage in V, with the same freshness
rules as every other measurement, never a pack total and never a guess. That mapped entity is the
primary cell-voltage source; `read_cell_voltage()` is the optional driver hook
(`zendure_native` implements it) used only when no entity is mapped. The hook stays off the
`BatteryDriver` protocol so drivers that cannot read a cell voltage simply ignore it.

`native.py` (0.15.0) must: speak the device's local HTTP JSON API with `urllib`/`http.client`/`ssl`
only (no third-party dependency), address the device by its required `sn`, read SoC / the measured
AC pair / PV / the device's own limits / pack voltages, write charge and discharge as the property
command shape (`inputLimit`/`outputLimit` magnitudes plus `acMode`, `smartMode` part of every
command), clamp to the device's own maximum as well as the configured limit, verify every write
against the device's own read-back inside a bounded window (mismatch or timeout = `driver_error`, so
the failsafe hold path stays in charge), degrade every transport/parse failure into a note/`last_error`
without ever raising out of `read_state`/`apply`, and expose the optional `read_cell_voltage()` hook.
It reads and writes no Home Assistant entities at all, so the battery works without `zendure_ha`;
saving it probes the device once and refuses a malformed or unreachable config unless the user set
`allow_unreachable`. The transport contract and its documented-vs-assumed split live in
`docs/EXTERNAL_APIS.md` section 6.

### 9.3 Safety (`control/safety.py`, pure functions, unit-tested)
`check(requested_w, plan_slot, batteries, state, settings, now) -> SafetyDecision(allowed_w,
clamped: bool, reasons: list[str], block: bool)` enforcing, in order:

1. `mode != auto` → block (`advisory`/`off` write nothing, guard holds included). The one exception
   is the neutralising hold in §9.4, which runs only for a battery the add-on really wrote to and
   has not verified neutral (an explicit de-escalation: mode change, `dry_run` change, Stop, exit)
   or for a previous run's latch restored at boot; the periodic recovery guard may neutralise only
   that boot latch, never a command this run applied and has not verified neutral. A `dry_run` tick
   never writes.
2. Kill switch / override active → block or return the override power.
3. State older than `control.stale_state_seconds` → block (hold).
4. HA disconnected or battery unavailable → block for that battery.
4b. **Cell-voltage guard (0.16.0, `protection.cell_voltage_*`, off by default).** The lowest cell
   voltage is read per battery from the mapped `cell_voltage_entity`, else the driver's optional
   `read_cell_voltage()` hook. Below `cell_voltage_cutoff_v` the battery **latches** an emergency
   charge: discharge is blocked and it is charged at
   `min(max_charge_w, emergency_charge_power_w or max_charge_w)` regardless of price/plan, still
   inside its own limits, max SoC and the ramp. Between the cutoff and `cell_voltage_resume_v`
   discharge is blocked and no charge is forced; at or above the resume voltage the latch clears.
   A stale or unreadable reading keeps the previous latch (fail-safe) and is noted; a voltage that
   was never seen leaves the guard inactive. Reasons `cell_voltage_guard` /
   `cell_voltage_emergency_charge` / `cell_voltage_stale`; the latch is persisted per battery and
   restored on boot. The guard is a pure function evaluated before the authority gate, so
   advisory/off and dry-run alert and log without writing, and a latched battery may not be
   discharged until it recovers. `cell_voltage_warning_v` and `cell_voltage_resume_v` must sit
   above the cutoff, validated at save time.
5. SoC clamp: no charge above `E_max`, no discharge below `E_min + reserve`.
6. Power clamp per battery limits (after allocation) and aggregated limits.
7. Grid limits: predicted import/export with the requested power must stay inside
   `max_grid_import_w`/`max_grid_export_w`; clamp, and block if the resulting power would still
   violate them.
8. Ramp limit: never change a battery's AC power by more than 1000 W per tick.

Blocked actions must be visible: alert + `dispatch_log` row + UI banner. Thresholds are configurable,
and the function must be pure (no I/O) so tests can cover it exhaustively.

### 9.4 Override and kill switch
`POST /api/v1/override {power_w, minutes}` pins a battery (or all) to a fixed power for a bounded
time (max 240 min, auto-expires, visible countdown). `mode: off` is the global kill switch.

**"Stop now" must actually stop the hardware.** Switching to `advisory` only stops *this add-on* from
writing; the Zendure integration has no watchdog, so the device keeps its last setpoint and the battery
would carry on charging or discharging (see appendix A). Therefore `POST /api/v1/stop` is a dedicated,
atomic safety action that:

1. clears any active override,
2. writes `hold()` (0 W / neutral) **for real** to every battery the add-on actually commanded and
   has not verified neutral (a persisted per-battery applied-command record; only a real, verified
   non-zero write enters it, never a blocked or dry-run tick), deliberately bypassing the
   `mode != auto` and `dry_run` blocks — de-escalating to zero power is the safe direction, and a
   stop button that silently does nothing under `dry_run` would be worse than useless. The record
   is cleared only on verified neutral: an explicit hold the driver confirmed inside its own
   read-back window, or `NEUTRAL_CONFIRM_READS` (2) consecutive readings where the driver's own
   setpoint *and* the measured flow both read ~0; a momentary 0 W measurement never clears it. A
   battery the add-on has never commanded is not touched, so an advisory install cannot switch the
   user's own battery mode,
3. sets `mode: advisory` so no further plan writes happen,
4. records an `event` plus one `dispatch_log` row per battery, and returns per-battery results.

It is the only write path allowed to run outside `auto`, and it must never raise: a failing battery is
reported in the response, not thrown. `control.safety` stays pure and is not involved in this path.

---

## 10. HTTP API (frozen contract — UI codes against this)

Plain JSON over the add-on ingress port, no auth (ingress supplies it), `Content-Type:
application/json`, all timestamps ISO-8601 with offset. Errors: `{"error": {"code": "...", "message":
"..."}}` with 4xx/5xx. All GETs must work in `mode: off` and with no batteries configured —
degraded but not broken.

```
GET  /api/v1/health            -> {status:"ok", version, uptime_s, commit}
GET  /api/v1/status            -> SystemState (§6) serialised
GET  /api/v1/plan?hours=36     -> {generated_at, horizon_start, horizon_end, feasible,
                                   summary:{…, eta_charge, eta_discharge, round_trip_efficiency,
                                            wear_eur_per_kwh, break_even_sell_eur_per_kwh},
                                   slots:[{t, buy, sell, pv_kw, load_kw, load_p10, load_p90,
                                           soc_kwh, soc_pct, power_w, action, reason,
                                           price_stale, pv_source, price_source}],
                                   batteries:[{id, name, share_w, round_trip_efficiency,
                                               wear_eur_per_kwh, break_even_sell_eur_per_kwh}]}
                               `reason` is a stable `Reason` slug; a slot that stays idle because the
                               sale price does not cover the round-trip loss reports `efficiency_loss`,
                               which is distinct from the user's own spread floor
                               (`spread_below_threshold`). The efficiency keys are additive.
POST /api/v1/plan/recompute    -> {queued: true}       (async; poll status)
GET  /api/v1/forecast?kind=price|solar|load&hours=36
                               -> {kind, generated_at, source, stale, slots:[...],
                                   provider, contract, notes:[...], reason, hint}
                                   `provider` is the effective provider (or `unavailable`), `contract`
                                   the configured contract type, `notes` the source's own caveats (e.g.
                                   the adders applied to a wholesale price). `reason`/`hint` are
                                   additive (0.3.0): when a panel has no slots `reason` is a stable
                                   slug (`not_configured`, `provider_failed`, `fixed_price_missing`,
                                   `no_pv_source`, `no_history`, `empty_horizon`, `unknown`) and
                                   `hint` one sentence naming the setting or condition to fix, so the
                                   UI never has to say only "the source failed".
GET  /api/v1/batteries         -> {batteries:[{config, state, capabilities, mapping_resolved,
                                               discovery:[...]}]}
POST /api/v1/batteries         -> create  {config}
PATCH /api/v1/batteries/{id}   -> update  {patch}
DELETE /api/v1/batteries/{id}  -> delete
POST /api/v1/batteries/{id}/test -> {power_w, seconds}  (bounded, requires mode:auto + !dry_run,
                                   refuses otherwise with 409 and an explanatory message)
GET  /api/v1/discover?kind=battery|price|solar|load|grid
                               -> {candidates:[{entity_id, name, unit, device_class, state,
                                                platform, suggested_role, confidence}]}
GET  /api/v1/config            -> {options:{...}, settings:[{key, value, type, default,
                                                          label, help, group, choices,
                                                          advanced}],
                                   price_warnings:[{key, message}],
                                   grid:{source, note, explicit:{grid.*}, dashboard:{role}}}
PATCH /api/v1/config           -> {settings:{key:value}}  -> 200 {applied:[...], errors:[...]}
                                   Unknown or invalid keys are reported per key in errors (never as a whole
                                   request failure), because the settings form renders them field by field.
                                   Only a malformed body (not an object / empty) is a 400.
POST /api/v1/mode              -> {mode?, dry_run?}   (partial update; omitted field is unchanged)
POST /api/v1/stop              -> {mode, override_cleared, stopped:[{battery_id, ok, applied_w, code,
                                   message, dry_run}]}   (atomic safety stop, see §9.4)
POST /api/v1/override          -> {power_w, minutes, battery_id?}
DELETE /api/v1/override        -> {}
GET  /api/v1/savings?range=day|week|month|year|all
                               -> {range, buckets:[SavingsBucket], totals:{...}, baseline_note}
GET  /api/v1/accuracy?days=14  -> {pv:AccuracyReport, load:AccuracyReport, price:AccuracyReport}
GET  /api/v1/log?limit=100&level=&kind=  -> {entries:[{ts, kind, level, code, message, detail}]}
GET  /api/v1/providers         -> {solar:[ProviderStatus], price:[ProviderStatus], load:[...]}
GET  /api/v1/export?what=plan|actuals|savings&format=json|csv   -> file download
POST /api/v1/notify/test       -> {}  (sends a test notification if enabled)
GET  /                         -> SPA index.html (+ /app.js, /style.css, /charts.js, /vendor/*)
```

`GET /api/v1/discover` is what makes the mapping wizard possible: it lists candidate entities with a
`suggested_role` and a `confidence` in `[0,1]`, derived from the entity registry (platform,
translation key) plus state attributes. It must never leak the whole entity list by default — cap at
50 rows per request with a `search` filter, and mark instance-specific values as runtime-only.

Candidate rows of every kind carry `entity_id`, `name`, `unit`, `device_class`, `state`, `platform`,
`suggested_role` and `confidence`. They additionally carry the state attributes `min` and `max` when
those are finite numbers, and `kind=battery` rows carry the entity's `device_id` plus, when the device
registry is reachable, `device_name`, `manufacturer` and `model` (all nullable). Every kind's rows
also carry `setting_key`: the settings key a confirmed role is saved to (`grid.*` for `grid`,
`price.entity` for `price`, `solar.measured_entity` for `solar`, `load.entity`/`load.temperature_entity`
for `load`; null when the role has no settings key). The setup-card pickers use it, so an export-only
grid sensor is saved to `grid.export_entity` rather than to the card's `grid.power_entity`. A battery is a
*device*, so these fields are what let the wizard offer a searchable list of batteries instead of
asking for a hand-typed slug, and prefill real device limits instead of invented ones. These keys are
additive: a missing device registry, an absent attribute or an older consumer must leave every
existing field untouched, and `control/drivers` stays the only authority on which role maps to which
mapping key.

Details the UI needs, so they are contract and not implementation detail:

* `/plan` slots carry `soc_pct` (capacity-weighted mean SoC), `pv_source` and `price_source` alongside the
  fields in `models.PlanSlot`.
* `/config` settings entries use `type` ∈ `bool|int|float|str|password|json|list`, include `choices` for
  enumerations and multi-selects, and group hardware-moving keys under the group name `Safety` so the UI
  can gate them behind a confirmation step.
* `/savings` totals include `arbitrage_kwh` and `self_consumption_kwh` so the UI can split realised value
  between price arbitrage and avoided self-consumption.
* `/savings` buckets and totals carry the measured AC battery flows `charged_kwh`, `discharged_kwh`,
  `charge_cost_eur` (priced with the buy series at flow time) and `discharge_value_eur` (priced with
  the sell series), per-bucket coverage flags `grid_measured`, `load_measured`, `pv_measured` and
  `battery_measured`, and totals `measured_buckets` counts (additive, 0.13.0). A flow not measured
  for every interval of a bucket is `null`/unknown, never the planned value; only a confirmed
  measurement earns battery credit.
* `/batteries` returns a flat `capabilities` object and a cycle count.
* `/log` entries have `kind` ∈ `dispatch|event|alert`; dispatch entries carry
  `detail.{requested_w, applied_w, battery_id, mode, dry_run}`.
* `/export` must work as a plain browser download link (correct `Content-Disposition`).

Static asset serving must be safe: normalise paths, reject anything escaping `web/`, and serve with
correct `Content-Type` + `Cache-Control: no-cache` for the SPA entry.

---

## 11. Persistence (SQLite, `store.py`)

Opened with `PRAGMA journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`, and a schema
version table with an explicit forward-only migration list. Thread-safe via one connection per thread
(`threading.local`) or a lock — declare which and keep it consistent.

```
plans(id INTEGER PK, created_at TEXT, horizon_start TEXT, horizon_end TEXT, mode TEXT,
      dry_run INTEGER, feasible INTEGER, summary_json TEXT, batteries_json TEXT)
plan_slots(plan_id INTEGER, t TEXT, buy REAL, sell REAL, pv_kw REAL, load_kw REAL,
           soc_kwh REAL, power_w REAL, action TEXT, reason TEXT,
           PRIMARY KEY(plan_id, t)) WITHOUT ROWID
dispatch_log(id PK, ts TEXT, battery_id TEXT, requested_w REAL, applied_w REAL, soc_pct REAL,
             mode TEXT, dry_run INTEGER, result TEXT, code TEXT, message TEXT)
measurements(ts TEXT, entity_role TEXT, value REAL, unit TEXT, PRIMARY KEY(ts, entity_role))
actuals(ts TEXT PK, pv_kw REAL, load_kw REAL, grid_kw REAL, battery_kw REAL, soc_kwh REAL,
        price_buy REAL, price_sell REAL, plan_power_w REAL, dispatch_power_w REAL)
forecast_cache(kind TEXT, key TEXT, fetched_at TEXT, payload_json TEXT, PRIMARY KEY(kind,key))
learning_profiles(kind TEXT, key TEXT, updated_at TEXT, payload_json TEXT, PRIMARY KEY(kind,key))
savings_daily(day TEXT PK, import_kwh REAL, export_kwh REAL, cost_eur REAL, revenue_eur REAL,
              baseline_cost_eur REAL, savings_eur REAL, pv_kwh REAL, battery_cycles REAL,
              charged_kwh REAL, discharged_kwh REAL, charge_cost_eur REAL, discharge_value_eur REAL,
              grid_measured INTEGER, load_measured INTEGER, pv_measured INTEGER,
              battery_measured INTEGER)
events(id PK, ts TEXT, level TEXT, code TEXT, message TEXT, payload_json TEXT)
settings(key TEXT PK, value_json TEXT, updated_at TEXT)
meta(key TEXT PK, value TEXT)
```

Scheduled-full-charge runtime state is not a new table: it is a per-battery record in the existing
``learning_profiles`` profile store (``kind='maintenance'``, ``key='full_charge'``) with
``last_completed``/``next_due``/``first_seen``/``overdue_alerted_at`` per battery id. A missing or
unreadable profile means "no history" (the battery is due immediately), never an invented date.

Retention: `measurements`/`actuals` raw rows trimmed to 400 days on a daily maintenance run,
`dispatch_log` to 20 000 rows, `events` to 5 000, and plans to the `plans_rows` (default 200) most
recent, whose `plan_slots` rows go with them. Measurements are upserted into 5-minute buckets, and
only mapped entities are read (targeted `GET /api/states/<entity>`, never the full state snapshot). Savings baselines are computed **before** trimming
and stored in `savings_daily`.

Savings accounting: `baseline_cost_eur` = the do-nothing **net** cost under the same realised load, PV
and tariff — import cost minus export revenue, with no battery flows — so `savings = baseline −
actual` (net). When the load is not knowable (no load forecast or measurement), baseline and savings
are `null`/unknown, never a fabricated zero. This definition must appear in the UI and in `docs/ARCHITECTURE.md`,
because it is the number the user will judge the add-on by.

Savings are earned only by measured energy: `charged_kwh`/`discharged_kwh` integrate the measured AC
battery power over time (a clamped or ramp-limited write counts the measured power, not the request),
and `charge_cost_eur`/`discharge_value_eur` price those flows with the buy/sell series in force at
flow time. A dry-run, blocked, stale-refused or unconfirmed-setpoint interval is never credited; if
any interval in a day lacks a confirmed measurement, that day's battery flows and cycle count are
`null`/unknown rather than a planned or partially measured number. `grid_measured`, `load_measured`,
`pv_measured` and `battery_measured` record which measured series were present for every interval of
the day; rows written before these columns existed keep working and read as unknown.

---

## 12. Published HA entities

The add-on mirrors its state into HA so users can chart/automate without our UI. Implemented with
`POST /api/states/<entity_id>` (no MQTT dependency, no YAML to install), entity ids in the reserved
namespace documented in the README:

```
sensor.sem_autopilot_state      off|advisory|auto|dry_run
sensor.sem_plan_action          action slug, with attributes: reason, target_power_w, soc_target_pct
sensor.sem_price_now            EUR/kWh, attributes: rank_pct, cheap_window_start, expensive_hour_start
sensor.sem_pv_forecast_today    kWh, attributes: tomorrow_kwh, peak_w, peak_at
sensor.sem_load_forecast_now    kW, attributes: today_kwh, confidence
sensor.sem_savings_today        EUR, attributes: month_eur, year_eur, baseline_kwh
sensor.sem_battery_soc_avg      %
sensor.sem_forecast_accuracy    %, attributes: pv_mape, load_mape, price_mae
binary_sensor.sem_autopilot_active
binary_sensor.sem_battery_at_floor
```

Publishing is idempotent, throttled (min 20 s between writes per entity), and skipped entirely when
HA is unreachable — never block the control loop on a state publish. `sensor.sem_*` is the documented
contract; README lists the table.

---

## 13. UI (`app/web/`, WP3)

Single page app, no build step, no runtime CDN (assets vendored). Layers: `index.html`, `style.css`,
`app.js` (state, routing, fetch, render), `charts.js` (uPlot wrappers). Dark-first design, respects
`prefers-color-scheme`, keyboard navigable, colour-blind-safe palette (blue/green/orange, no
red/green price encoding without a shape cue too), works from 360 px wide up to desktop, honours
`prefers-reduced-motion`.

Views (hash routing `#/dashboard`, `#/plan`, `#/forecasts`, `#/batteries`, `#/savings`, `#/settings`,
`#/log`):

1. **Dashboard** — now-cards (SoC, PV, load, grid, price + rank, current action + reason,
   autopilot state, next cheap and next expensive window), 24 h SoC/price mini-chart, alert strip,
   "Stop now" and mode switches with a confirmation step for `auto`.
2. **Plan** — main timeline: price bars + PV/load areas + SoC line + charge/discharge bars in one
   aligned chart, per-slot table with reason, plan summary (cost, baseline, saving, cycles), horizon
   selector (12/24/36/48 h), "Recompute" button, feasibility warnings, and a marked maintenance
   plan when the payload's `maintenance.active` is true (0.17.0).
3. **Forecasts** — three panels (price, solar, load) with source, freshness badge, `stale` greying,
   P10/P90 band for load, per-day totals, plus accuracy summary (MAPE/bias) with a 14-day trend.
4. **Batteries** — card per battery: live power/SoC/limits/efficiency, mapping state
   (resolved vs needs confirmation), add/edit wizard using `/api/v1/discover` with role assignment
   and a confirm step, per-battery enable toggle, bounded test discharge (disabled unless `auto` and
   not `dry_run`), computed round-trip efficiency and cycle count, and the scheduled-full-charge
   state (due in N days / due now / overdue / last completed / off, 0.17.0).
5. **Savings** — today/week/month/year/all, savings chart vs baseline, import/export kWh, realised
   arbitrage vs self-consumption split, a *Measured battery flows* panel (charged/discharged kWh,
   charge cost and discharge value, with per-series coverage where a measurement was missing), and a
   plain-language explanation of the baseline definition.
6. **Settings** — grouped, searchable form generated from `GET /api/v1/config` (no hand-written
   field list per key), immediate validation errors from the PATCH response, safety group visually
   separated with an "are you sure" step for limits that can move hardware.
7. **Log** — filterable decision log: every dispatch attempt with requested vs applied power and the
   blocking reason, plus events/alerts. This is the debugging surface; it must never be empty when
   something surprising happened.

Cross-cutting UI rules: no colour-only meaning; every number shows its unit; stale data is visibly
stale (badge + desaturated); loading and error states for every panel; optimistic UI is forbidden for
writes (show pending → confirmed using the server response); the whole UI must render with fake data
via `?mock=1` so it can be developed and tested without the backend (mock payloads live in
`app/web/mock.js`, obviously synthetic).

### 13.1 Design system (frozen)

The look is Bootstrap 5.3.3, vendored as one minified CSS file (`vendor/bootstrap.min.css`, MIT,
checksum recorded in `vendor/README.md`) and loaded before `style.css`. Reasons this is vendored CSS
and not a Tailwind build: §13 forbids a build step, the add-on must not fetch anything at runtime,
and the vendoring + checksum + refresh-step convention already exists for uPlot. Bootstrap supplies
the reset, grid, utilities, form controls and `--bs-*` tokens; `style.css` is the design layer and
maps those tokens onto the palette below, so the result is this product's design, not Bootstrap's
default look. Bootstrap's JavaScript is **not** vendored: interactions stay in `app.js`.

- Component vocabulary is fixed: existing names (`panel`, `now-card`, `chip`, `badge`, `field`,
  `grid`, `row`, `muted`, `stale`, `btn`, `dialog`) stay in use and are restyled, plus new
  `settings-rail`, `settings-group`, `disclosure`, `save-bar`, `segmented`, `switch`, `unit-input`,
  and - for the setup cards and their entity pickers - `setup-cards`, `setup-card`, `setup-list`,
  `picker`, `picker-current`, `picker-search`, `picker-results`, `picker-row`, `picker-text`,
  `picker-name`, `picker-meta` and `all-settings`.
- Tokens: the colour-blind-safe palette stays; the `--chart-*` custom properties are a frozen
  interface (`charts.js` reads them through `getComputedStyle`). Dark and light are both first class
  via `data-bs-theme` plus `prefers-color-scheme`, with an explicit user override that persists.
- Minimalist rules: one accent colour, 1px borders instead of shadows, one radius scale, an 8 px
  spacing scale, tabular figures for numbers, no gradients, no external fonts, no images.
- Accessibility is unchanged and normative: keyboard reachable, visible `:focus-visible` ring,
  `prefers-reduced-motion` honoured, contrast >= 4.5:1, never colour-only, skip link kept.

### 13.2 Setup cards and the All settings drawer (frozen; amended 0.6.0)

The settings form was first a single wall of ~75 fields, then a rail of groups that still showed 55
essential rows. It now opens on **five setup cards** — Price, Meter, Solar, Load, Battery — each
carrying only the primary choice of its topic, with everything else one disclosure below in the **All
settings drawer**:

- The cards are the default surface. `SEM_APP.setupCardKeys({priceProvider, priceContract,
  solarSource})` is the pure table of the keys the cards own for the current selection; a card renders
  only keys that apply (an entity picker for `price.provider=entity`, the bidding zone for `day_ahead`,
  the fixed rates for a fixed contract, no measured-PV picker when `solar.source=derived`).
- The drawer holds the existing rail (one group at a time, with the change count per group), the
  search box, the per-group `Advanced (n)` disclosures and the `Show all advanced` switch, all
  remembered in localStorage. It is collapsed by default (`sem.settings.allOpen`).
- **Every** key of `GET /api/v1/config` is rendered exactly once: on a card while the card shows it,
  otherwise inside its server-provided group. While a search query is active the cards are hidden, so
  every match is listed once in the drawer.
- A row whose payload has `advanced: true` sits behind its group's `Advanced (n)` disclosure, which is
  collapsed by default, remembers its state per group, and can be revealed for every group at once
  with the `Show all advanced` switch (also remembered). Rows in the `Safety` group are never folded.
- Entity pickers ask `GET /api/v1/discover`, list the candidates with a `Use` action and write the
  row's `setting_key` (falling back to the card's own key); a typed entity id can always be used
  directly, so a sensor discovery ranks poorly is still reachable.
- The Meter card names the grid meter's provenance (§3): while every `grid.*` key is empty it shows
  the roles the Home Assistant Energy dashboard resolves (`config.grid.dashboard`); a saved or
  edited `grid.*` value is labelled as an override of that fallback, the dashboard's roles stay
  visible, and a `Use the Energy dashboard` action clears every explicit `grid.*` key through the
  normal dirty/Save flow.
- The Load card carries the house meter, its optional outdoor-temperature sensor and the named-device
  editor (§7.2): one row per `load.devices` entry, each with a meter picker, a kind dropdown fed from
  the backend descriptor's choices (so a newer backend kind needs no UI release) and an optional
  display name, plus add/remove/reorder. A row with no meter or a repeated meter is refused
  client-side, never sent for the server to reject: a repeated meter would be subtracted twice.
- A `save-bar` (sticky, outside the drawer) shows how many keys are changed with Save and Revert;
  navigating away with unsaved changes asks first.

### 13.3 `advanced` classification (frozen; amended 0.6.0)

`_Meta.advanced` (default `False`) travels to the UI as the `advanced` row field, and `config.py` is
the authority: the test suite asserts the exact set in both directions. The essential (never folded)
keys are now only the ones the setup cards and the safety group need:

`mode`, `dry_run`, `notify.enabled`, `price.provider`, `price.export_fee_eur_per_kwh`,
`price.feed_in_energy_tax_refund`, `price.feed_in_mode`, `price.energy_tax_confirmed`,
`price.feed_in_tax_deduction_eur_per_kwh`, `price.saldering_assume_nettable`, the fixed rates (`price.fixed_price_eur_per_kwh`,
`price.fixed_day_price_eur_per_kwh`, `price.fixed_night_price_eur_per_kwh`), `grid.power_entity`,
`solar.enabled`, `solar.source`, `solar.measured_entity`, `optimizer.horizon_hours`,
`optimizer.allow_grid_charge`, `optimizer.min_spread_eur_per_kwh`,
plus the Safety keys (`optimizer.reserve_soc_pct`, `optimizer.max_grid_import_w`,
`optimizer.max_grid_export_w`, `optimizer.allow_export`). Everything else is advanced, including the
wear override `optimizer.cycle_cost_eur_per_kwh` (a non-zero value replaces each battery's derived
wear; 0 = derive it) and `solar.nasa_irradiance`; the `Safety` group is always visible regardless of
the flag, and a label never carries a manual "(advanced)"
suffix - folding is the only signal.

`control.*` split in two: the control-loop tuning knobs (`control.tick_seconds`,
`control.replan_minutes`, `control.grid_follow*`, `control.stale_*`) moved out of the never-folding
Safety group into a foldable `Control` group, while `control.failsafe_on_error` stays in `Safety`.
The Safety group is now the keys that can move hardware or change a hard limit — the five
SoC/grid/fail-safe keys above plus the five `protection.cell_voltage_*` keys (0.16.0) — so "never
folded" still means what it says.

### 13.4 UI tests

Beyond the existing asset checks: the settings IA is tested through pure helpers exported on
`SEM_APP` (`settingsGroups`, `settingsVisible`) so node can exercise them without a DOM, the frozen
classification is asserted against the server payload, and the mock payload in `mock.js` mirrors the
new row field.

---

## 14. Tests and acceptance (WP5)

`python3 -m pytest tests/ -q` must pass with no network access and no HA instance. Required coverage:

- **Optimizer vs brute force**: on random small instances (4–8 slots, coarse SoC grid), DP optimum
  equals exhaustive-search optimum within 1e-6; DP is deterministic across 3 runs.
- **Optimizer sanity**: with a high PV window the plan charges then discharges instead of cycling at
  a loss; with flat prices the plan does not arbitrage below `min_spread_eur_per_kwh`; with a hard
  terminal target the plan reaches it; infeasible terminal target yields `feasible: false` and a
  reason, not an exception.
- **Safety**: exhaustive table over (mode, dry_run, staleness, SoC at floor/ceiling, grid limit,
  ramp limit, override active) asserting the exact clamp/block outcome, including that `advisory`
  never produces a non-blocked decision and `auto + dry_run` never performs I/O.
- **Price normalisation**: `eur_per_mwh`, `micro_eur_per_kwh`, VAT/tax adders, `auto` detection, plus
  stale-forecast handling and the fallback chain (configured → energy_charts → static), all with
  synthetic fixtures; no fixture may contain a real instance value.
- **Load forecast**: profile learning from synthetic history, weekday/weekend separation, level
  scaling, EWMA correction, cold start.
- **WS client** (`haws.py`): frame codec unit tests (masking, 126/127-length, ping/pong, close,
  fragmented text frames) plus an integration test against a local mini WS echo server built from the
  same socket helpers.
- **API contract**: every route from §10 answers with the documented shape against a fake engine,
  including error paths (404 unknown battery, 409 test-discharge while advisory, 400 invalid config
  key), and static file traversal attempts return 404.
- **End-to-end**: a `tests/fake_ha.py` HTTP server emulating `/api/config`, `/api/states`,
  `/api/services/*`, history and the WS handshake; the app boots against it in `advisory`, produces a
  plan, publishes states, and never issues a service call (assert on the fake server's request log).
  Then in `auto` + `dry_run` it still issues no service call; in `auto` (dry_run off) it issues exactly
  the expected setpoint writes to the mapped entities.
- **Audit** (`tools/audit.sh`) returns non-zero if any committed file contains a plausibly real
  coordinate, private IP, complete private dotted quad (`192.168.*.*`/`10.*.*.*`/`172.16-31.*.*`;
  bare prefixes in prose do not match), `sensor.<realm-identifying>`
  instance id, bearer/JWT-looking string, or an email address. It must pass on the final tree.

Acceptance for the whole build: fresh checkout → `pytest -q` green, `tools/audit.sh` green,
`docker build` of `energy_manager/` succeeds (or, if Docker is unavailable in the build environment,
`python3 -m compileall` + a documented dry-run boot with `HA_URL` pointing at `tests/fake_ha.py`),
and the SPA renders all seven views against `?mock=1` with `node --check` clean JS.

---

## A. Verified external facts that constrain the code

Full detail, sources and confidence tags live in `docs/EXTERNAL_APIS.md` (WP1). These are the ones that
change what the code must do, and they override any assumption elsewhere in this spec:

**Forecast.Solar** — keyless responses are **hourly, single-plane, ~2 days**, so the provider must
interpolate to the 15-minute grid and a keyless multi-plane config must degrade to the first plane with a
warning. Response keys are `result.watts`, `result.watt_hours_period`, `result.watt_hours_day` (there is no
`result.watthours.*`). `?time=utc` is mandatory (default timestamps carry no offset). Keyless limit is
12 requests/hour/IP. Azimuth is compass−180 (0 = south), declination 0–90 (90 = vertical). Never log
`message.info`, it contains the coordinates.

**Energy-Charts price** — returns **today only** (96 × 15-min slots, `unit: "EUR / MWh"`); tomorrow's data
404s until early afternoon CET. Prices are ex-tax wholesale, so apply the configured VAT and energy tax
adders, do not add them twice for `zonneplan_entity`. Attribution is required in the README/DOCS
(CC BY 4.0, Bundesnetzagentur | SMARD.de).

**Zonneplan** — both `electricity_price` and `price_tax_included.amount` are fixed-point with a scale of
**1e-7 (divide by 10^7)**. `price_tax_included` is already the consumer price incl. VAT and energy tax:
apply no further adders. The list **contains past slots** (~a day back) and reaches ~+30 h, so providers
must drop past slots and judge staleness by **coverage of the requested horizon**, not by entity
availability — a failed refresh keeps old data and still looks available.

**Home Assistant REST** — service calls take a **flat body** (top-level `entity_id`/data keys); a nested
`target` object is rejected with 400. `POST /api/states/<id>` is admin-only, creates no `unique_id` and
disappears on restart (fine for our mirror sensors, documented in the README). `/api/states` is ~855 KB for
1 322 entities: never fetch it unfiltered on a schedule; use the entity registry plus targeted reads.
History needs `filter_entity_id` and only covers `purge_keep_days`; long-term statistics are never purged,
which is why §7.2 prefers them. Error codes are 400/401/404/405/500 — there is no 422 and no documented
rate limit.

**Home Assistant WebSocket** — `auth_required → auth → auth_ok`; the server PINGs every ~55 s and the
client **must PONG** or the connection is dropped. Zendure entity-registry `unique_id`s are **not**
prefixed with the integration domain, so discovery must key on `platform` + `translation_key`, never on a
`unique_id` prefix (this is what §5 requires). `statistics_during_period` rows carry ms-epoch `start/end`
plus only the requested statistic types, and need no admin rights — likewise
`recorder/get_statistics_metadata`, whose rows carry `unit_class` (`power`/`energy`), `has_mean`/`has_sum`
and the unit, so an external statistic (no entity to read) is still classifiable. `energy/get_prefs` is
read-only and **not** admin-gated (the sibling `energy/save_prefs` is): it returns the Energy
dashboard's `energy_sources`, where the grid source carries the signed power sensor (`stat_rate`,
generated by Home Assistant for every `power_config` shape, positive = import) and the import/export
energy counters.

**Zendure driver (highest-risk item in the build)** — `manual_power` uses the **opposite sign convention to
this application's `models.py`: negative = charge, positive = discharge**, so the driver must negate when
writing and when reading back. It is system-wide, not per device. Writes only take effect when the
integration processes the next P1 event, so read-back verification must tolerate a short delay and retry
rather than declaring failure immediately. The integration's own smart modes overwrite external writes, so
`operation` must be `manual` before and re-asserted during control. `electric_level` is the authoritative
SoC (`soc_status` is calibration bookkeeping). `available_kwh` is usable energy above `min_soc`;
`total_kwh` is installed capacity. DC PV charging happens regardless of the AC setpoint. There is **no
device watchdog**: the last setpoint persists if the controller disappears, which is why the engine must
write `hold()` on shutdown and on fatal error, and why the README must tell users to set the manager back
to a safe mode if they uninstall the add-on.

**Also required**: `tests/test_wp2a_haws.py` currently contains literal `unique_id=` strings that the PII
scanner flags; fix by using the allowlisted example namespace or an explicit `audit-allow:` marker rather
than by weakening `tools/audit.sh`.

---

## 15. Packaging (WP4)

- `config.yaml`: `name: Smart Energy Manager`, `version: 0.1.0`, `slug: energy_manager`, `arch:
  [aarch64, amd64, armv7, armhf, i386]`, `startup: application`, `boot: auto`, `init: false`,
  `ingress: true`, `ingress_port: 8099`, `panel_icon: mdi:home-battery`, `panel_title: Energy`,
  `homeassistant_api: true`, `hassio_api: false`, `options:`/`schema:` exactly as §3 (all
  instance-specific values `null`/empty), `description` ≤ 200 chars, `url` the repo URL,
  `image: {ghcr.io/yazdannl/energy-manager-{arch}}` optional and commented for local builds.
- `Dockerfile`: `FROM ghcr.io/home-assistant/{arch}-base-python:3.13-alpine3.21` (build-arg `BUILD_ARCH`),
  copy `app/` to `/app`, `WORKDIR /app`, `ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1`,
  `CMD ["/bin/sh", "/run.sh"]` so the fail-fast wrapper is the real entrypoint, no pip layer (R3),
  runs as root as HA add-ons expect,
  healthcheck hitting `/api/v1/health`.
- `run.sh`: fail fast with a clear message when neither `SUPERVISOR_TOKEN` nor the legacy
  `HASSIO_TOKEN` is set, then `exec python3 -m app.main`, keeping the shell wrapper POSIX `sh`.
- `README.md`: what it does, requirements (HAOS/Supervised, a dynamic-price source, a battery),
  install steps, the `sensor.sem_*` table, safety model and default-safety explanation, a
  "privacy: what leaves your system" section (Forecast.Solar and the day-ahead price provider; none
  of your data is sent anywhere else), troubleshooting, and the savings-baseline definition.
- `DOCS.md`: add-on store tab, short, points at README.
- `icon.png` (128×128) and `logo.png` (250×100): generated by a small committed script
  `tools/make_icons.py` using only `zlib`/`struct`; no binaries with unclear provenance.
- `.github/workflows/validate.yml`: `python -m compileall`, `pytest -q`, `tools/audit.sh`, and the
  official `hassio-addons/workflow-general`/`builder` lint if it can be pinned safely (skip if it
  requires secrets).

---

## 16. Amendment log (why the spec changed)

**Optimizer v2 stage 1b — contract-correct feed-in accounting, pricing only (no version bump).** The
DP prices export at `slot.sell`, and a 0.10 EUR/kWh marginal export error moves more money than the
whole search this stage prepares for (docs/OPTIMIZER_V2.md section 1), so the settlement function has
to be exact before anything is dispatched against it. This stage ships the accounting as a pure
module and changes no live behaviour: `price.feed_in_mode` defaults to `derived`, the live forecast
path is byte-for-byte the pre-1b `sources/price.py::_apply_feed_in`, and it does **not** consult the
mode at all until a consumer enforces the module's complete result.

- **`sources/feed_in.py` is a pure function of `(settings, slots, period_state, limits)`.** It returns
a `FeedInResult` — mode, per-slot `sell`, `export_allowed`, a reason slug, the forced-export floor and
notes — and never reads HA, the clock or the store. `period_state` (period import/export totals) and
the reachability limits are inputs; wiring them to real sensors and to the optimizer is the follow-up
stage, so no `optimize/*`, `engine.py`, `models.py` or `api.py` changes here (the offline mock's
settings parity fixture carries the four new keys). `derived` is
rejected by `settle_feed_in` rather than re-implemented: it is the live path, and a second copy could
diverge from what actually prices the forecast.
- **Exact-or-refuse, never a guessed rate.** `explicit` and `buy_minus_fee` are exogenous and
energy-only, so exact. `saldering` is exact only while the period allowance `I − E` covers the
reachable export bound; otherwise it refuses the *battery-sourced* export leg
(`Reason.UNPRICED_EXPORT`) and prices forced PV export at the minimum over every reachable settlement
rate. `tiered` is exact only while the reachable volume stays below the next tier boundary above the
starting volume. When no justified floor exists the result carries `floor = None` and the plan must
perform no tariff-dependent optimisation. The live forecast path has no channel for
`export_allowed`/`reason`/`floor`, so it never calls the module: a refusal must not become
ordinary zero-priced slots.
- **Unknown is never zero.** A missing, malformed or non-finite input refuses instead of being
substituted: an infinite discharge capability does not become zero capability (which would wrongly
declare a threshold contract exact), a non-finite explicit tariff is unusable, a confirmed but
missing slot tax is not a confirmed zero, and a malformed tier table (non-increasing bounds,
non-finite rates, `+inf` anywhere but last) is refused whole instead of silently extending another
rate across the interval. Negative samples are malformed too: a negative PV/load sample would
understate reachable exports, and negative cumulative period imports/exports or a negative tier
volume would manufacture allowance or distance to a boundary. A configured but unresolved or invalid
explicit source refuses rather than substituting the derived `buy − fee − tax` rate (which could
exceed the real explicit rate and break the lower-bound guarantee); the derived post-saldering
formula is used only when no explicit source is configured. `settle_saldering_schedule` requires
equal-length series and non-negative, finite totals/flows rather than truncating or accepting a
negative balance as allowance.
- **The reachable export bound is not capped by pack energy.** It is
`Σ min(grid/inverter export limit, max(0, pv − load) + max_discharge_power) · dt`: PV surplus can
export without the battery, and a grid recharge followed by a discharge exports more than one pack's
worth. A pack-energy bound under-counts exactly the paths a threshold contract makes expensive, so a
missing `max_discharge_kw` or a PV/load series that does not cover every slot means "unbounded",
never zero.
- **`derived` keeps its exact meaning; the new modes add the tax gate.** With
`price.feed_in_energy_tax_refund` off, `derived` still subtracts whatever per-slot component the
provider published, as before. `buy_minus_fee` and `saldering` price `sell = buy − fee − tax` only
when the component is *confirmed* (`price.energy_tax_confirmed`, default false); an unstated
component is unknown, not zero, so they refuse instead of silently pricing `sell = buy`.
`price.feed_in_tax_deduction_eur_per_kwh` states the component once saldering ends, and a confirmed
`0.0` is a valid confirmation (the legacy path never reads the override).
- **`saldering` uses the supported in-slot convention only.** Import settles before export within a
slot (`eligible = min(export, max(0, I + import − E))`, excess at the post-saldering price), and a
contract that nets the whole period at settlement requires an explicit export price. The module
exposes `settle_saldering_schedule` for the whole-schedule arithmetic, tested against independently
written arithmetic. `price.saldering_assume_nettable` applies only when the period state is
unavailable; a supplied state, including a known exhausted allowance, wins over the assumption.
- **The `advanced` classification does not move.** The four new feed-in keys are essential, like the
existing feed-in keys, so the frozen `ADVANCED_KEYS` set is unchanged.
- **Known limits, named rather than implied away:** the VAT-share exclusion of the 2027
`buy_minus_fee` formula is not modelled yet (a flat fee and the confirmed tax are); the `tiered` tier
table arrives through the reachability inputs rather than a setting until the wiring lands; and
`price.feed_in_mode` is config-only today — selecting another mode stores the contract for the
follow-up but does not change the live forecast, which keeps using the derived price.

**0.25.0 named load devices (user request: add named load devices such as a heat pump with a device type
and its own energy meter, so temperature-driven loads are modelled).** The base model fitted one
whole-house profile and one whole-house temperature slope, so every device landed in that single
compromise term. A heat pump responds strongly to temperature, an EV charger responds to nothing but
its schedule, and averaging them fits neither: the pump's weather response drags the house slope upward
and the charger's flat draw flattens it back.

`load.devices` is a list of `{entity, kind, name?}`. Each device gets its own (weekend|weekday, hour)
profile and its own temperature term, and the forecast becomes `base + Σ devices`.

- **The subtraction is the load-bearing half.** The measured total already contains the device, so a
  base learned from the total and then added to the device's own profile counts it twice. That is worth
  belabouring because the error is invisible: a device added twice reads as a *steeper* temperature
  response, not as a bug, and a doubled constant looks like a discovery. So an hour enters the base only
  if the total **and every** subtracted device have a reading there — a missing reading is unknown, not
  zero, and treating it as zero leaves that device's draw in the base while its own profile is still
  added on top. The hours lost that way are counted and reported, and a meter covering under 48 hours or
  under 50% of the house's hours is refused whole rather than modelled from whatever few hours the two
  happened to share.
- **The interlock is recorded, not re-derived.** The base profile stores `devices_subtracted` and
  `devices_excluded`, and a stored device profile is added back only when the *current* base names its
  key as subtracted. Re-deriving this at serving time from the current settings would let a stale device
  profile be added to a base learned without subtracting it, and a device that is not in the base
  *should* be inside it.
- **The kind decides the temperature term, at serving time as well as at learning time.** Only
  `heat_pump`, `water_heater` and `pool_pump` get a slope. An EV charger's draw is a schedule; fitting a
  slope to it would absorb whatever else varies with temperature as if it were the car. A device
  re-configured from heat pump to EV charger therefore drops its stored slope immediately, rather than
  contradicting the kind it is configured as now until the next learning run.
- **Nothing is silently absorbed.** The forecast names every modelled and every unmodelled device with
  the reason recorded when the base was learned ("its meter overlapped 3 of 720 hours"), because a device
  left inside the base changes what the base *means* and that is invisible in a kW curve. Provider
  `detail` carries the counts. Cold start models no device at all: the fallback shape is the whole house
  and already contains every one of them.
- **Two devices of one kind are told apart by their entity**, not their display name, since that is the
  only identity that stays unique when both are heat pumps. The profile key is `device:<entity>` under
  the existing `load` kind, so a rename keeps the learning history.
- **Known limits, named rather than implied away:** a mostly-unreadable meter is refused entirely rather
  than subtracted only where readable; the coverage and sample floors are judgement calls rather than
  settings; each device gets one slope for the whole week, the same limitation the base model has; the
  p10/p90 of the sum are the sums of the components' bands, which approximates the quantiles of the sum;
  and a removed device's stored profile is left behind and inert rather than garbage-collected.

**0.24.0 break the load/pv circularity (user request: calibrate from the meter, since we know the grid
export and we know the night load).** The observation was correct and the blind spot was structural:
`load = grid + pv − battery` is the identity that makes both quantities recoverable, and the add-on
already used it — but only in one direction, and only when *both* pv and the battery flow were already
mapped. An install measuring nothing but grid and battery therefore had no load labels, no pv labels,
and a fine-tune head that could never fit anything. It would have reported "insufficient" forever while
looking like it was waiting for data.

- **§7.2 tier 3b, night-derived load.** The sun below −6° (deeper than the horizon, since a tilted plane
  still collects diffuse light at 0°) makes production zero for any array, so `load = grid − battery`
  needs no pv series at all. About half of every day qualifies. A missing location is refused rather
  than guessed: guessing it would admit daylight hours as if production were zero and inflate the
  learned consumption everywhere.
- **The night tier cannot learn a day shape, and says so.** It fills the hours it never saw from the
  default residential shape, and that fill has to happen *inside* profile building. Patching the
  finished profile is not equivalent: the unobserved buckets contribute 0.0 to `profile_mean`, so the
  level `scale` is derived from a mostly-empty day and then inflates every hour up to its clamp. In a
  synthetic 1 kW house the first version forecast 2.0 kW at night — wrong on precisely the hours it had
  measured, and wrong in the direction that looks like a discovery rather than a bug. The fill is also
  normalised so the day averages the observed level, not the shape's own mean, for the same reason at
  smaller magnitude (5.7% high, which `scale` then clawed back by pushing the measured hours off their
  level).
- **Inferred labels get their own columns** (`actuals.pv_derived_kw` / `load_derived_kw`, schema 4).
  Writing them into `pv_kw`/`load_kw` would have been fewer lines and would have destroyed the ability
  to tell a measurement from this add-on's own arithmetic forever, in a table the fine-tune head learns
  from. The head prefers the measurement, falls back only when the measured column is empty across its
  whole window, and reports which it used. The engine refuses to infer anything when a battery is
  configured but its flow is unreadable, because the cycling would land on the house or on the roof.
- **Not done, and the next piece:** named load devices (`load.devices[]`), so a heat pump's own meter
  can be modelled with its own temperature response instead of being averaged into one house-wide
  slope. The design is fixed — subtract each measured device from the total before learning the base
  profile, learn the device's own profile and slope, add it back in the forecast — and the subtraction
  is what makes it safe: adding a device on top of a base that already contains it double-counts it.

**0.23.0 the PV model is fitted on the chain it is served, and given the weather (user challenge: "are
we taking cloud cover, temperature and rain into account? if we're not, what use does it have?").**
The challenge was right, and the audit found something worse than a missing feature.

- **The real defect was a train/serve mismatch, not an omission.** Training carried satellite-observed
  GHI; service builds GHI from a cloud percentage. So the learned residual was partly correcting an
  irradiance model the add-on never uses. Scored on the chain it would actually see, the 0.22.0
  artifact fell from 115.8 W to 150.5 W with an ~8 % energy bias — meaning the number validated
  offline was not the number in service. Fixing the chain and adding weather terms takes held-out
  systems to 4.742 % and held-out years to 5.620 %, against 5.102 % / 5.548 % before. Both splits
  travel in the artifact.
- **What was already right, and was verified rather than assumed.** Cloud cover, air and module
  temperature, panel tilt/azimuth/capacity and the inverter limit were all in the model. PVGIS `G(i)`
  is genuinely horizontal for this corpus (its clearness index never exceeds 0.80; plane-of-array
  data misread as horizontal would exceed 1.0), so the training irradiance was not silently
transposed twice, and the corpus/timestamp alignment was checked by lag correlation rather than
  trusted (lag 0 wins at r = 0.879).
- **What was missing:** wind (the NOCT model ignores convective cooling), rain, humidity, and any
  ability for the model to learn this site's own cloud-to-irradiance mapping rather than inheriting
  Kasten-Czeplak's. All four are now features, and all four are obtainable at runtime from the same
  `weather.get_forecasts` call the cloud map already uses.
- **The golden-vector gate earned its keep twice.** It caught the clear-sky and irradiance features
  being built per-system on one path and at the corpus centroid on the other (4.3 and 4.0 W/m²) — the
  same defect in miniature, and exactly the kind that shows up as "the forecast is a bit off" rather
  than as a failure. Its tolerance is now documented as 1e-4: four orders below the smallest error it
  has caught, four orders above float32 round-trip noise.
- **Deliberately not done:** the load model, and a scoring route over the forecast archive. The
  fine-tune head still cannot learn on an install with no PV measurement, which is the next piece.

**0.22.0 learned PV residual model, forecast archive and fine-tune head (user request: finish the
forecasting work and deploy it).** Three additions, and the reasoning matters more than the shape:

- **§7.1.4 `solar.source = learned`.** A model trained offline on the Utrecht 175 corpus (CC-BY-4.0)
  and served from the committed artifact by standard-library arithmetic only (R3). It predicts a
  *ratio* on its own physical reference rather than a power, so one artifact serves an array it has
  never seen — the cold-start prior a fresh install has no way to build for itself. Validated on
  held-out *systems* (5.102 % of nameplate) and on held-out *years* (5.548 %, 2014 → 2015-2017), with
  an aggregate specific-yield gate at +0.15 %; against a physics prior whose own bias is +14.4 %.
  Both numbers travel in the artifact so the UI reports the honest one.
- **§7.4 forecast archive.** Upserted per `(kind, source, slot)`, keeping the newest pre-event issue.
  Without it, "the forecast improved" is an opinion; with it, the §7.5 gate has ground truth and the
  user can read their own error.
- **§7.5 nightly fine-tune head.** Closed-form ridge per local hour, shrunk toward 1.0, adopted only
  by beating the incumbent on a holdout by 2 %, revertible in one write. It **replaces** the §7.1
  hourly EWMA rather than stacking on it; two corrections fitted to one signal would double-count the
  same error, and the failure would look like an ordinary cloudy week.

Also amended: `solar.ac_capacity_wp` (§3), and the clear-sky notes no longer state a hard-coded
"uncertainty 35 %" — the real band is 30 % wherever the NASA POWER clearness profile applies, and
prose that contradicts the `uncertainty_pct` field beside it is worse than prose that omits it.

Two bugs were found and fixed rather than embodied in the shipped artifact. The training pipeline's
day-of-year was `days_since_epoch % 365 + 1`, offset by 11-13 days *and* drifting by a day per year as
leap days accumulated, so the seasonal feature meant something different in every training year; the
add-on's skill breakdown was also exercised on both horizons after the fix, which improved every
number including the physics prior (176.1 → 163.4 W). And the settings view called
`hasOwnProperty` with an absent choice's value as the receiver, so adding a backend choice value the
UI's label table had not heard of blanked the entire page; that is now a cosmetic fallback.

**0.5.0 UI redesign (user request: "cluttered and not modern at all", "modern minimalist ui with more
options available if the user wants to change them", "use either bootstrap or tailwindcss").** §13.1
fixes the design system (vendored Bootstrap 5.3.3 CSS, restyled through `--bs-*` token mapping) and
§13.2 replaces the truncated settings wall with grouped, folded disclosure: every key renders, the
server decides what is `advanced` (§13.3), and nothing is invisible just because it holds its default.
§10 gains the additive `advanced` row field.

Three independent adversarial audits (gpt-6-luna, deepseek-v4.1, step-5-preview) reviewed the built
tree. The fixes they triggered changed shipped details, so the frozen contract is amended here, in one
round, with the reason:

- **§1/§9.3/§9.4** — `advisory`/`off` never write, except the neutralising hold for a setpoint the
  add-on itself left live (persisted per-battery latch). Audits 1 and 2 independently reproduced the
  old `hold()` silently skipping on lazy measured power, and advisory exits writing to hardware.
- **§3/§8.1** — `optimizer.horizon_hours` default 36 → 30 (matches the ~+30 h feed);
  `optimizer.allow_export` default true → false, plus `price.feed_in_*`. Both auditors found the sell
  price was silently the consumer price plus VAT, which over-exports at fictional revenue.
- **§3/§9.1** — `control.grid_follow_max_w` added, and the grid-follow budget is absolute rather than
  a fraction of the current target, because a zero target could never be corrected.
- **§7.3/§11** — targeted entity reads, 5-minute measurement buckets and plan retention
  (`plans_rows`): the audit measured ~2.4 GB/day of JSON and unbounded plan growth.
- **§11** — the baseline is net cost and may be unknown; the old wording omitted export revenue and
  reported a missing load reading as a negative saving.
- **§6/§10** — the API `/status` reports `pending_holds` and `mode_persisted` (the engine owns those values;
  they are not `SystemState` fields — see the later §6/§7.2 entry). The `/stop` body is unchanged
  (`{mode, override_cleared, stopped[]}`).
- **§15** — the image entrypoint is `/bin/sh /run.sh` and the wrapper accepts the legacy
  `HASSIO_TOKEN`, so the fail-fast actually runs.
- **§5/§6/§8.1** — per-battery round-trip efficiency is an input (`round_trip_efficiency_pct`), the
  break-even sale price is defined as `buy / round_trip` plus wear per delivered kWh, terminal stored
  energy is valued at `eta_dis` rather than face value, and an idle-but-unprofitable slot says
  `efficiency_loss`. Requested after the audit round: the schedules must price the conversion loss
  instead of showing a paper profit on a cycle that loses energy.
- **§6/§7.2 (polish round after the final verification)** — `load.temperature_entity` is wired into the
  history collection so the documented regression term is no longer a silent no-op; §6's status wording now
  says the API reports `pending_holds`/`mode_persisted` instead of claiming they are `SystemState` fields;
  and the R1 scanner gained rules for 4-decimal unlabelled coordinate pairs, private hostnames, MACs and
  bare 32-hex ids, all of which the final verifier demonstrated it previously missed.

- **§10 (battery picker, requested after the first live install)** — `/api/v1/discover` rows gained the
  additive device fields (`device_id`, `device_name`, `manufacturer`, `model`) and the optional numeric
  `min`/`max` attributes. The request was that a battery be *selected* from what the instance already
  has rather than typed as a slug; entity-level rows cannot express a device. The wizard therefore
  filters device groups to plausible batteries — a known battery platform, or at least one control role
  together with one state role — because discovery legitimately returns lone `soc`, `connection` or
  `manual_mode` matches for thermostats, bridges, printers and add-ons; it ranks control-ready groups
  first, labels what is missing, and still allows a slug to be typed for anything discovery cannot see.

- **§4.1 (price provider and contract selection; requested after the live install, "the forecast
  section is empty")** — the price panel was empty because no source was configured, and the UI could
  only say "the source failed or the horizon is empty". Checked against the live instance before
  building: the existing Zonneplan provider parses both of its tariff sensors correctly (quarter-hourly
  144 entries, hourly 36, fixed-point ÷1e7, and its scale check passes against the sensor state), so the
  source path was never broken. The request was to let the user select a **provider** and a **contract
  type** and have the add-on fetch the prices from somewhere, so provider resolution (zonneplan lookups
  in HA, day-ahead via energy_charts, fixed tariffs, and the untouched explicit entity path),
  `price_warnings`, and the forecast `reason`/`hint`/`notes`/`provider`/`contract` fields were added.
  A fixed tariff deliberately has **no** market fallback, and no price is inferred from an entity name.

- **§7.1.1 (capacity-based solar estimate; requested after the live install, "support ac coupled solar
  as well for forecast")** — the instance's PV is AC-coupled and measured at the meter (peaked at
  1875 W and roughly 3.3 kWh on 2026-10-08, hourly means from 73 W to 1022 W), the battery's own PV
  input has never produced anything, and no planes are configured, so the Forecast.Solar path cannot
  fill the solar panel at all. The request was to let the user state a watt peak and have the add-on
  estimate production, with real production sources refining it. §7.1.1 adds the `capacity` source
  (solar position, Haurwitz clear-sky irradiance, isotropic transposition, Kasten-Czeplak cloud
  attenuation from an optional weather entity) and relies on the existing measured-PV bias learning
  (per-hour EWMA, clamped, decaying) for the refinement. No optimizer change: PV is modelled as
  generation, which is exactly what an AC-coupled array is, and assumed tilt/azimuth are stated in the
  notes rather than silently defaulted.

- **§13.1/§13.2/§13.3 (setup cards; requested 2026-10-11, "the ux is cluttered, we have too many
  options")** — the settings page opened on a 10-group rail with 55 always-visible rows and nine
  never-folding `control.*` tuning knobs. It now opens on four setup cards (Price, Meter, Solar,
  Battery) whose primary choices are a provider select, two searchable entity pickers and a link into
  the battery wizard, with everything else in a collapsed "All settings" drawer. The essential set
  shrank from 55 rows to 21 (the card keys, the safety keys and four optimizer policy rows), the
  `control.*` tuning moved out of `Safety` into a foldable `Control` group, and `SEM_APP.setupCardKeys`
  is the pure table the render check asserts. The user explicitly chose "4 cards + one All settings
  drawer" over a setup group in the rail or a separate view, and "fit the existing sun model" over a
  learned daily shape for the derived solar source below.
- **§4.2 (derived feed-in price; requested 2026-10-11, "the feed in price is market price + energy
  tax - feed in cost")** — a sell price had to be entered as a flat tariff or a sensor, otherwise
  export was worth 0.00 and `optimizer.allow_export` stayed off. The sell price is now derived from
  the buy price (`sell = buy − price.export_fee_eur_per_kwh`, minus the slot's energy-tax component
  when `price.feed_in_energy_tax_refund` is off), with the explicit tariff/entity still winning. The
  user corrected an early misreading of this request: the toggle is about the **energy tax**, not VAT,
  and the feed-in cost defaults to 0. `PriceSlot` gained the additive `energy_tax` field, filled
  per slot from the Zonneplan payload's included/excluded pair and otherwise from the configured
  wholesale adder.
- **§7.1.2 (meter-derived solar; same request, "we could also estimate from the negative values of
  our meter minus the battery output")** — `solar.source=derived` learns the array's watt peak from
  the meter's export moments (`pv = export + battery net charge`, the house load not subtracted, so
  the sample is a lower bound), takes the 90th percentile of `pv / model-per-watt` as the cloudless
  envelope, and then runs the existing §7.1.1 capacity model. An explicit `solar.capacity_wp` wins;
  fewer than 24 usable daylight samples, no export history or an unmappable battery flow is a status,
  never a crash.
- **§10 (discovery `setting_key` for every kind)** — the setup-card pickers need to know which
  settings key a confirmed candidate is saved to, so `price`, `solar` and `load` rows now carry
  `setting_key` alongside `grid` and `battery` (additive; null when a role has no key).

- **§5/§6/§7.1.1/§7.1.2/§8.1/§10 (wear from battery price, NASA clearness; requested
  2026-10-10, "the pricing model uses both discharge efficiency and round-trip efficiency", "let the
  user put in the cycle life of the battery and price and calculate the wear from that", "does the
  solar capacity calculation take into account the solar irradiation data … at the specific
  time")** — the user reviewed the cost model and the forecast and asked for three changes.
  (a) Wear is no longer a global number typed into a settings field: each battery gains `price_eur`
  and `cycle_life`, and `wear_eur_per_kwh = price / (cycle life × usable kWh × eta_discharge)` is the
  cost of one kWh **delivered** (the eta_discharge divisor was added after an independent review
  showed the un-divided form charged a full cycle only `price/cycle_life × eta_discharge`); the fleet
  plans with the delivered-energy-weighted mean, and `optimizer.cycle_cost_eur_per_kwh` stays as an
  advanced override (user chose "keep as an advanced override" over removing it).
  (b) Because the wear term is now per delivered kWh, the break-even no longer needs a per-direction
  efficiency: `break_even_sell = buy / round_trip + wear` (the user's request: "we should just use
  roundtrip efficiency"). The DP objective charges wear on the discharge leg only (the user chose
  "on delivered kWh only" over splitting it across both legs), so a charge-only schedule pays none
  and a full cycle costs exactly `price / cycle_life`. (c) The capacity/derived solar model already
  uses the home's location and the exact slot time for its clear-sky irradiance, but no observed or
  forecast irradiance data; the user asked to add NASA POWER as an accuracy addition (not a
  replacement), so `solar.nasa_irradiance` (default on) learns a per-hour-of-day clearness index from
  the last 21 days of observed all-sky/clear-sky irradiance and applies it where the weather entity
  has no cloud coverage, and uses it in the derived watt-peak fit. Weather-entity cloud attenuation
  and the measured-PV bias keep priority; `forecast_solar` is unchanged. The user's other choices
  from the three options offered: per-battery fields with a fleet average, and keeping the global
  setting as an override. A later independent review (2026-10-11) added: a 3-hour failure backoff so
  an unreachable NASA costs one request rather than one per refresh, the timezone in the profile's
  cache identity, and the same cache-I/O tolerance the rest of the forecast has. Two review findings
  are knowingly accepted rather than fixed: the fleet wears a single delivered-energy-weighted mean
  although `allocate_power` favours particular packs, and the derived fit scales historical export
  samples by the *recent* 21-day clearness median rather than the weather of each sample's own date.

- **§3/§7.1/§7.1.2/§7.2 (Energy-dashboard grid mapping and statistics history; requested
  2026-10-11, "can we get the grid power export/import from home assistants energy section by
  default … which would let us do a good estimate of solar production")** — the live install's
  Energy dashboard already named the house meter, but the add-on asked for the mapping again and,
  with `solar.planes` empty, showed `no_pv_source`. Three coupled changes. (a) `app/grid.py` reads
  `energy/get_prefs` (WebSocket, not admin-gated) and resolves `grid_power`/`grid_import`/
  `grid_export` from the grid source's `stat_rate` (Home Assistant generates it for every power
  configuration, and its sign already matches `grid.power_entity`), falling back to the two-sensor
  `power_config` and then to the import/export energy counters as `grid_energy_import`/
  `grid_energy_export` (history only; an `stat_rate_inverted` without the generated sensor is
  ignored rather than guessed). An explicit `grid.*` setting replaces the fallback as a whole; the
  read is cached 5 minutes and a failed read is cached too. The user chose "runtime default
  everywhere" over persisting the resolved values into the settings or limiting the fallback to the
  estimation tiers. (b) The derived load and PV tiers read every series from long-term statistics
  when a WebSocket client is available (hourly `mean` for a power statistic, hourly `change` for an
  energy counter, converted to average kW using the row's own duration), so the learning window is
  the never-purged statistics table rather than the recorder's default 10-day purge; recorder
  history remains the fallback. (c) `solar.source=forecast_solar` with no planes now runs the
  meter-derived estimate instead of failing, and reports `no_pv_source` only when no meter
  resolves, attaching the derived failure (`status.detail.derived_fallback`) and a note to the
  hint. The user chose the automatic fallback over leaving the hint.

- **§13.2 (Meter-card grid provenance; requested 2026-10-11, "the meter info is still not coming
  from ha energy section")** — the runtime fallback above landed in 0.9.0 but was invisible: the
  card showed only the (empty) setting, and a saved `grid.*` value silently disabled the fallback
  with no way to clear it from the panel. `GET /api/v1/config` now carries
  `grid:{source, note, explicit, dashboard}` (`engine.grid_resolution()`; `dashboard` is every role
  the Energy dashboard offers, explicit overrides included), and the Meter card renders it: the
  resolved roles while the settings are empty, an explicit "Set here; this overrides …" label plus
  the dashboard's roles, and a `Use the Energy dashboard` button that clears `grid.power_entity`,
  `grid.import_entity` and `grid.export_entity` through the normal dirty/Save flow. Additive
  payload key; the settings themselves stay blank ("runtime default everywhere").

- **§3/§7.1.3/§10/§13.2 (multi-source PV forecast merge; requested 2026-10-12, "combine several
  solar forecast providers into one hourly forecast")** — a single provider is an assumption about
  which service is most accurate, and the live install already had more than one forecast source
  available. Two coupled choices drove the design. (a) Home Assistant exposes every provider the
  Energy dashboard has configured through the read-only, non-admin `energy/solar_forecast`
  WebSocket command, so the new `ha_energy` source reads them there instead of asking the user to
  repeat the array's geometry; its response is keyed by the dashboard's own config entry id, so an
  entry may select one with `entry_id` or sum all of them. (b) The merge is a new `solar.sources`
  list rather than a change to `solar.source`, because an empty list must keep today's single-source
  path byte-for-byte; the pure `merge_solar_series` function aligns each source with the existing
  interpolation/timezone rules and merges with `solar.merge_strategy` (`weighted_mean` default,
  `median`, `max`), skipping failed sources and falling back to the single-source chain only when
  none produces a usable series. Per-source status (`name`, `weight`, `state`, `note`) is additive on
  `ProviderStatus`/`SolarForecast` and exposed on the solar provider list and the solar forecast
  payload; the Solar card renders the list with add/remove/reorder through the normal dirty/Save
  flow. The alternatives considered and rejected: a per-slot provider election (needs a
  trust model the data cannot support), silently reading all HA providers without a settings list
  (no way to weight or disable one), and storing merged values back into `solar.source` (would lose
  the "empty list = unchanged" guarantee).

- **§10/§11/§13 (measured savings; requested 2026-10-12, "savings must be earned by energy the
  battery actually moved, not by plan values")** — the savings pipeline integrates only confirmed
  measured AC battery power (`BatteryState.power_measured`; the generic driver's setpoint read-back
  and a Zendure whose measured pair is incomplete report unconfirmed), adds
  `charged_kwh`/`discharged_kwh`/`charge_cost_eur`/`discharge_value_eur` and per-day coverage flags
  (`grid_measured`/`load_measured`/`pv_measured`/`battery_measured`) to `savings_daily` via
  forward-only migration 2, and exposes them on `/api/v1/savings` and the Savings view with a
  `measured_buckets` coverage count; a missing or unconfirmed measurement is `null`/unknown, never
  the planned value.

- **§5/§10/§13 (self-learning usable capacity; requested 2026-10-12, "learn each pack's real usable
  capacity from measured cycles")** — the configured ``capacity_kwh`` is a label, not a measurement,
  and an aged pack holds less. ``BatteryConfig`` gains the additive ``capacity_source`` (``manual``
  default, ``learned``) and a pure estimator (``app/capacity.py``) derives capacity from the
  persisted 5-minute AC power and SoC rows: charge windows divide ``AC * eta_charge`` and discharge
  windows divide ``AC / eta_discharge`` by the SoC fraction, each direction's efficiency on its own
  leg; windows need a 10-point SoC move, 20 % of the battery's own power limit, no gap beyond one
  sample and finite values, and are combined with a median after a MAD outlier filter, kept to 20
  windows, clamped to 1..200 kWh and published from 3 windows. The engine resolves the effective
  capacity wherever capacity is consumed (drivers, DP, allocation, savings baseline, published
  summaries) and falls back to the manual seed for a missing, stale or low-confidence estimate,
  journaling a stable reason slug. The estimate is stored per battery in ``learning_profiles``, never
  in user settings; ``GET /api/v1/batteries`` and the Batteries card expose the configured seed, the
  estimate, its sample count and last update, and the effective value, and the card's Edit dialog
  switches source through the normal dirty/Save flow. Alternatives rejected: writing the learned
  value back to ``capacity_kwh`` (destroys the seed and the manual fallback) and trusting a single
  cycle (one bad meter sample would redefine the pack).

- **§5/§9.2 (native Zendure transport; requested 2026-10-12, "drive Zendure hardware directly over
  its local API")** — the device's own local zenSDK HTTP API (`GET /properties/report`,
  `POST /properties/write`) is added as driver `zendure_native` with stdlib-only `urllib`/`ssl`: its
  `mapping` carries connection settings (`host`, required `serial`, optional `scheme`/`port`/`token`/
  `api_path`/`timeout_s`/`insecure_tls`/`allow_unreachable`), it needs no `zendure_ha` entities,
  clamps to the device's own maxima, verifies every write against the device's limit echo (mismatch
  or timeout = `driver_error`), exposes `read_cell_voltage()` from `packData[].minVol`, and refuses
  an unreachable/mismatched-serial config at save time unless `allow_unreachable` is set. The local
  MQTT transport is documented as planned work (it needs a broker), never stubbed; the
  verified-vs-assumed split is in `docs/EXTERNAL_APIS.md` section 6.

- **§3/§9.2/§9.3 (minimum cell-voltage protection; requested 2026-10-12, "protect the pack on low
  cell voltage and charge it out of danger")** — a per-battery guard is added as clause 4b of the
  ordered safety table and as the pure function `control.safety.cell_voltage_guard`. The settings
  group `protection.cell_voltage_*` ships disabled (`enabled false`, warning 3.10 V, cutoff 3.00 V,
  resume 3.18 V, `emergency_charge_power_w 0` = the battery's own `max_charge_w`) and is
  cross-validated at save time (warning and resume must both sit above the cutoff). The lowest cell
  voltage comes from a mapped `cell_voltage_entity` read with the same freshness rules as every
  other measurement, falling back to the optional `read_cell_voltage()` driver hook only when no
  entity is mapped; the hook stays off the `BatteryDriver` protocol so other drivers ignore it. Below
  the cutoff the battery latches: discharge is blocked and it is charged at
  `min(max_charge_w, emergency_charge_power_w or max_charge_w)` regardless of price/plan, still
  inside its own limits, max SoC and the ramp; between cutoff and resume only discharge is blocked;
  at or above resume the latch clears. A stale reading keeps the previous latch and is noted, a
  never-seen voltage leaves the guard inactive, and the per-battery latch is persisted in the runtime
  profile and restored on boot. The guard is evaluated before the authority gate so advisory/off and
  dry-run alert and log with stable reasons (`cell_voltage_guard`, `cell_voltage_emergency_charge`,
  `cell_voltage_stale`) without writing; `/status` gains a `cell_voltage` block and each battery a
  `cell_voltage_v`, and the Batteries card states the voltage and marks a latched battery.

- **§3/§8.1/§11/§13 (scheduled full-charge maintenance; requested 2026-10-12, "schedule a periodic
  full charge so the BMS can balance")** — `maintenance.full_charge_*` ships disabled (`enabled
  false`, interval 30 days, target 100 %), per-battery runtime state (last completed, next due) is
  kept in the store profile rather than user settings, and while a battery is due the plan reuses
  the existing terminal-target mechanism with the higher of the maintenance target and
  `optimizer.target_soc_pct_end`; reaching the target records the completion and alerts are
  rate-limited, never a faked completion.
- **§3/§9.2/§9.3/§9.4/§10 (setpoint ownership; fixed 2026-10-12 after the live-test storm)** — the live run showed the recovery guard fighting its own plan, blocked advisory ticks latching from `requested_w`, and device-path drivers clearing the latch on a momentary 0 W, so the latch is now a per-battery applied-command record: only a real, verified non-zero write sets it, only an explicit verified hold (or a driver-reported zero across a confirmation window) clears it, only a previous run's restored latch is guard-recoverable, and advisory/off/dry-run runs issue zero write calls — guard holds included — except the documented boot/Stop/exit de-escalations.

- **§3/§5/§8.1/§10/§12 (cost basis of the stored energy and the sell floor; requested 2026-10-12, "value the energy in the battery and refuse loss-making sales")** — the optimiser's forward break-even is paired with a persisted per-battery ledger (``app/cost_basis.py``, profile kind ``battery_cost_basis``): applied dispatch changes open event-driven slices, the slice energy is the *measured* ``battery_power:<id>`` integral (never the commanded setpoint), each slice is priced at the plan slot it was charged in (grid at ``buy``, PV at ``sell``, split at price-slot boundaries, skip rather than guess when neither resolves, the plan slot's own reason as the fallback attribution when the meter is missing), and the basis is the weighted average ``(basis*E + cost)/(E + charge_ac)`` with a discharge reducing ``E`` by ``discharged/round_trip`` and a reset to ``basis=None, E=0`` at the SoC floor. ``E`` is clamped at ``capacity_kwh / eta_charge`` and a first run (or a gap beyond the 7-day lookback) starts clean; a bounded 200-slice journal keeps the provenance. ``optimizer.sell_floor_enabled`` (default true) and ``optimizer.sell_floor_margin_eur_per_kwh`` (0..1, default 0) turn that basis into a floor through the frozen ``BatteryConfig.break_even_sell_eur_per_kwh`` helper — per battery, with the fleet value the plan enforces as the energy-weighted mean — and the DP blocks a discharge/export whose value leg (``sell`` when exporting, ``buy`` when serving the house) is below it with the new ``Reason.SELL_FLOOR``; a live planned discharge below the floor is also clamped to a hold at dispatch, without touching charging, SoC-floor holds or overrides. ``/status`` gains the per-battery ``cost_basis_eur_per_kwh``/``stored_value_eur`` and a fleet block, the plan summary gains ``sell_floor_eur_per_kwh``, and ``sensor.sem_battery_cost_basis`` / ``sensor.sem_battery_value`` mirror the fleet basis and held value (or ``unknown``, never a fabricated figure). Off, or with no basis yet, behaviour is exactly the pre-0.18.0 one.
- **§3/§5/§8.1/§10 (measured energy on the sample grid and reconciliation with the pack; fixed 2026-10-12 after the live 0.18.0 ledger under-reported the pack by ~12 %)** — the ledger now integrates the *covered sample spans* themselves (split at the applied command changes and price-slot boundaries they cross) and accumulates each command interval's parts into its journal entry, so a command shorter than the sample spacing still books its measured share; ``last_ts`` is the end of the last processed sample span (never a command timestamp) and nothing beyond the newest settled sample is booked, so an update tail can no longer be extrapolated flat and the ramp the next sample reveals is never lost (replaying an update books nothing twice, and a settled command interval with no covered sample span counts in ``skipped_no_power``). After booking, the quantity is reconciled with the pack's own measured SoC content ``E_phys = clamp(capacity_kwh*(soc-min_soc_pct)/100/eta_charge, 0, capacity_kwh/eta_charge)`` from the last SoC sample at or before the booking end (stale/missing skips and counts ``skipped_no_soc``); the difference is applied to ``energy_kwh`` and never to ``basis``, the signed cumulative correction is ``reconciled_kwh`` plus ``reconciled_last_kwh``, and a single correction beyond half the pack capacity resets the ledger (``basis=None``, ``E=0``) and counts ``reconciled_resets``.

- **§3/§5/§8.1 (the sell floor must not force PV charging, and the PV charge preference; reported
  2026-10-12 after the live plan charged 08:00-10:00 instead of the cheaper 11:00-13:00 hours)** —
  the 0.18.0 floor branch's ``grid_lo = max(grid_lo, -base)`` is a *positive* lower bound in a
  PV-surplus slot, so the pack was forced to swallow the whole surplus at any price (live:
  +303/+849/+727 W at 08:00-10:00 while the same 1879 Wh was cheaper at 11:00-13:00). The branch
  now uses ``max(grid_lo, -max(0.0, base))``: only a deficit caps the discharge, a surplus leaves
  ``p >= 0`` and the PV exports freely (a PV kWh headed for the meter has no cost basis).
  ``optimizer.pv_charge_preference`` (``auto`` | ``prefer_pv`` | ``avoid_pv``, default ``auto``)
  prices PV energy charged into the pack at the slot's ``sell`` (the export forgone; ``auto`` is
  economically exact and prefers PV exactly when ``sell < buy``), at ``0`` (absorb the surplus
  first) or at the slot's ``buy`` (export the PV and charge from the grid in the cheapest hours),
  as an internal shadow price ``dt * (pv_price - sell) * min(p, max(0, pv - load))`` on the charging
  leg only; the summary money stays the true grid cost of the schedule (SPEC 8.1).

- **§5/§9.3/§10 (per-direction manual limits and a bounded manual force; requested 2026-10-12,
  "cap charge/discharge per battery and force a direction from the dashboard")** —
  `SystemState.manual_limits` (additive) is the mapping `{"*": {...}, "<battery_id>": {...}}` with
  `charge_w`/`discharge_w`: absent/`null` = no limit, `0.0` = that direction is blocked, a number is
  a cap in watts. The resolution order for one battery direction is the per-battery entry, then the
  `"*"` fleet entry, then the configured `BatteryConfig.max_charge_w`/`max_discharge_w`; the result
  never exceeds the configured maximum (`models.resolve_manual_limit` clamps a hand-edited or stale
  cap, and the resolver is the only reader of the mapping). The cap is applied by lowering the
  *effective* config
  (`replace(config, max_charge_w=min(configured, cap), max_discharge_w=min(configured, cap))`), so
  the plan (the DP's own `p_charge_kw`/`p_discharge_kw` bounds), the aggregate safety clamp
  (`p_min`/`p_max`), the per-battery allocation and headroom, the dispatcher's per-battery clamp,
  the HA publish and `/status` all read one number and no second enforcement path can drift. The
  stored config and the battery PATCH path are untouched: a later maximum update is stored exactly
  as requested and the cap keeps applying on top. A limit clamps *any* commanded power, including a
  manual force/hold pin, and never bypasses the cell-voltage protection, the SOC floor/ceiling, the
  reserve or the full-charge maintenance state machine. Limits persist exactly like `mode`/`dry_run`
  (the settings store's `manual_limits` key; deliberately **not** a `DEFAULTS` setting, so it never
  becomes an editable `/config` field) and are journaled (`limits_set`, `limits_cleared`).
  `POST /limits {battery_id?, charge_w?, discharge_w?}` writes the named battery (unknown id = 404)
  or the `"*"` fleet entry (missing/`null` id); an explicit `null` clears that direction and an
  absent field is left unchanged. A value above the configured maximum, or below 0, is refused with
  400 `invalid_body` naming the maximum -- never silently clamped (the fleet entry is judged against
  the largest configured maximum, and each battery still caps it at its own). `DELETE /limits`
  clears every entry, `DELETE /limits?battery_id=<id>` one entry. `/status` gains `manual_limits` (the
  raw mapping) and every `batteries[]` row gains `max_charge_w`/`max_discharge_w` (the configured
  hardware maxima the UI needs as ranges) plus `cap_charge_w`/`cap_discharge_w` (`null` = unlimited,
  `min(configured, limit)` otherwise). The force override API is restated unchanged --
  `POST /override {power_w, minutes, battery_id}` with a signed power (charge `+`, discharge `-`,
  hold `0`) and `DELETE /override` -- and the Dashboard panel is its only new client.

- **§6/§8.1/§8.2/§10 (the current partial slot is modelled with the time it really has; requested
  2026-10-13 after the live 05:16Z plan undersold the 05:00 slot and the pack stopped at 37 %)** —
  `solve_from_forecasts` floors the horizon start to the slot grid, so the first slot can begin up
  to a full slot before `now`, while the pre-0.20.1 `solve` applied one uniform
  `dt = slot_minutes / 60` to every slot: the modelled 05:00 hour really covered only 05:16-06:00,
  its end-of-slot SoC was unattainable in the clock, and every later slot's price was applied to a
  window shifted by the same elapsed part. `PlanSlot` gains the additive field `minutes` (the
  duration the solver actually modelled): full `slot_minutes` for every slot after the first, the
  remaining part of the current clock slot for the first, `0.0` meaning "unknown -- fall back to
  `plan.slot_minutes`" for an older stored plan. `solve` gains the keyword `now` (`None` keeps the
  old uniform model, so an existing caller is bit-for-bit unchanged) and computes the first slot's
  duration as `clamp((slots[0].t + slot_delta - now).total_seconds() / 60, 1.0, slot_minutes)`; the
  DP transitions, `power_bounds_kw`, the slot cost, the SoC trajectory, the summary money
  (`cost_estimate_eur`, `baseline_cost_eur`, `saved_eur`) and every other consumer of plan-slot
  energy (the execution view's intended side, `/plan` and `/plan/execution` slots, `/status`'s
  current slot, the per-battery allocation, the web window energy/cost) use `minutes / 60`. The
  timestamps stay clock-aligned and the grid is unchanged, so the API and the UI labels do not
  move; only the durations, the energies and the money do. A `now` exactly on the grid reproduces
  the uniform schedule, and a sub-minute or past remainder floors at 1 minute so no `dt` is ever
  zero or negative.

- **§8.2/§8.14 (a plan reason names the constraint that really bound the power; requested
  2026-10-13 after the live 12:00Z slot)** — `_classify` must not read `power_bounds_kw`'s
  `binding` flag as "the chosen power is on a grid bound". The interval computation is factored
  into `soc_window_kw(e_kwh, agg, dt_hours)` (the hardware envelope narrowed by SoC reachability)
  and `grid_window_kw(slot, agg, opts)` (import/export caps, the no-export rules and the cost-basis
  sell floor); `power_bounds_kw` intersects them and keeps its public `(lo, hi, grid_binding)`
  signature and `binding` semantics unchanged (any grid rule that tightens either side). The
  classifier receives both windows per slot, and a slot is at a *grid* bound only when the power
  sits on that grid bound and the bound is tighter than the SoC bound on the same side (charge: the
  `hi` side, discharge: the `lo` side, hold: a zero bound on either side, still excluding the
  floor's own `sell_floor` reason). A bound that coincides with the SoC bound counts as the SoC
  bound. The 0.19.0 sell-floor rule (`grid_lo = max(grid_lo, -max(0, base))`, §8.14) is exactly
  the case that made `binding` true in a PV-surplus slot while the charge filled the pack at its
  ceiling, so the live 12:00 slot's +990.3 W is a PV-surplus charge, not a grid hold. No schedule
  change: for the live-shaped case and a deterministic sweep of 40 horizons the powers, SoC
  trajectory, energies and money are byte-identical to 0.20.1; only the action/reason labels move.
  No new reason slug (the vocabulary stays additive).

- **§5/§7.3 (the sampler reads the driver's measured roles; a stalled ledger is visible and its
  unbooked energy recoverable; requested 2026-10-13 after the live ledger froze for 18 h)** — the
  measurement sampler must not keep a private copy of the battery mapping keys. A driver declares
  the roles its own reads use with the classmethod `measured_roles(mapping)` (generic
  `charge_power_entity` / `discharge_power_entity` / `pv_power_entity` / `output_power_entity` /
  `soc_entity` by default; the Zendure driver overrides it with the native pair
  `grid_input_power_entity` = charge and `output_power_entity` = discharge, requires both sides
  like `_power_is_measured`, and lets the generic keys win when both vocabularies are present).
  `sources/measure.role_entities` resolves that table through the driver registry and accepts the
  engine's driver-resolved mapping per battery (`Engine.resolved_mappings`) as an override, so a
  role the runtime registry resolver filled in is sampled exactly like a user-confirmed one, while
  an unmapped battery resolves nothing. `battery_power:<id>` keeps its meaning: the signed
  `power_setpoint_entity`, a command, never measured energy. `book_slices` must record a stall
  instead of returning the previous payload unchanged: a `skipped_no_samples` counter, a persisted
  `no_samples_since` marker (with a `stale` boolean) that survives restarts, one journal warning
  per stall cleared when booking resumes, all visible in `/status.cost_basis` (per battery) and in
  the `sensor.sem_battery_cost_basis` attributes. When the per-battery measurement series holds no
  usable power point in the window and exactly one battery is enabled, `update_basis` may fall
  back to the fleet-aggregate 5-minute `actuals` rows (`battery_kw`/`grid_kw`/`soc_kwh`) to recover
  measured energy that was never booked; with several enabled batteries the aggregate is never
  split by guesswork and the stall is reported instead. Every basis semantic is unchanged: the
  measured integral, the plan/price attribution, the quantity reconciliation, no invented price
  and no extrapolation beyond the newest row.

- **§10/§13 (the Dashboard shows the ledger's average charged price and its break-even sell price;
  requested 2026-10-13, "can we show the current price floor as well in the dashboard")** — the
  Dashboard gains one compact **Battery energy price** card, display-only. It reads
  ``GET /status.cost_basis`` (the ledger the optimiser acts on, not the forward break-even alone)
  and renders the energy-weighted ``fleet_basis_eur_per_kwh`` (the average charged price) and
  ``sell_floor_eur_per_kwh`` in EUR/kWh with three decimals, the floor's own breakdown ``basis ÷
  round trip + wear + margin`` (round trip and wear from the plan summary's
  ``round_trip_efficiency`` / ``wear_eur_per_kwh``, the margin from
  ``cost_basis.margin_eur_per_kwh``; a term the payload does not carry is omitted, never printed
  as a fake zero), the stored energy ``energy_kwh`` and its value ``stored_value_eur``, and the
  ledger's ``updated_at`` in local time. With more than one enabled battery the fleet figures are
  joined by one row per battery (both prices, energy, value, freshness). 0.20.3's stall marker
  (``stale`` / ``no_samples_since``, per battery) must stay visible: the card marks both readings
  as stale and shows a warning naming the battery and the moment its samples stopped. A ledger
  with no basis yet (or an empty pack) and ``optimizer.sell_floor_enabled = false`` each render
  ``--`` with the reason, and a missing field is never rendered as a number. The card adds no
  fetch beyond ``/status`` and the already-fetched plan, and changes no optimiser, ledger,
  dispatcher or sampler behaviour.

- **§5/§10 (a stall is a freshness failure, not an empty booking window; requested 2026-10-13
  after the live 0.20.4 false positive at 12:23:02Z)** — the 0.20.3 stall marker must report
  *measurements that stopped arriving*, never a booking window that happens to be empty.  A ledger
  already settled up to the newest sample finds the covered ranges of ``(last_ts, newest sample)``
  empty by construction, so ``book_slices`` must not derive ``stalled`` from that coverage (live:
  the healthy ledger advanced 368 -> 371 intervals and wrote ``stale: true`` in the same update,
  then read healthy five minutes later).  ``stalled`` is true only when no usable power sample
  exists at all or the newest one is older than ``STALL_TOLERANCE = 3 * SAMPLE_MINUTES`` (15 min:
  ``MAX_GAP_MINUTES``, the 10-min booking gap tolerance, plus one whole sample bucket, so a healthy
  sampler -- or one that merely dropped a single bucket -- can never trip it).  Everything else from
  0.20.3 is unchanged: ``skipped_no_samples`` steps once per stalling update, the persisted
  ``no_samples_since`` marker (and the ``stale`` boolean) is set once per stall and cleared when a
  fresh sample resumes the booking, one warning per stall, and the ``sample_source`` field.  The
  single-battery ``actuals`` fallback stays exactly as it was: it fires only when the per-battery
  series holds no usable power point at or after the window start, which the empty-window case never
  is (its newest sample *is* the window start), so a healthy ledger updating twice inside one sample
  bin books nothing extra and never switches ``sample_source`` to ``actuals``.  No optimiser,
  dispatcher, sampler or ledger-economics change.

The 398-passes/13-mutations figures in earlier revisions of this file and in `docs/VERIFICATION.md`
were true of the pre-audit tree; the final numbers are re-measured on a frozen tree hash and recorded
in `docs/VERIFICATION.md`.
