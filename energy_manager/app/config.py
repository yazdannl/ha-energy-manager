"""Runtime configuration (SPEC.md section 3).

Two layers merge into :class:`Settings`, later layers win:

1. :data:`DEFAULTS` — shipped values for every option and setting key.
2. add-on options from ``/data/options.json`` (top-level keys only).
3. UI settings persisted in the SQLite ``settings`` table (dotted keys).

No instance-specific value may appear here (SPEC.md R1): location, currency, entity ids and keys are
all ``None``/empty and resolved at runtime.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass, fields
from zoneinfo import ZoneInfo
from typing import Any, Mapping

from .models import BatteryConfig, ConfigError
# The control layer is the single authority on which drivers exist (SPEC 5/9.2): asking it here
# keeps save-time validation from drifting away from the registry the dispatcher builds from.
from .control.drivers import names as driver_names

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULTS",
    "OPTION_OVERRIDE_KEYS",
    "SETTINGS_META",
    "Settings",
    "battery_payload",
    "coerce_setting",
    "load_options",
    "merge_mapping_patch",
    "protection_patch_errors",
    "protection_problem",
    "redact_battery_mapping",
    "resolve_location",
    "setting_descriptors",
    "validate_battery_config",
]

#: Mapping values that are credentials, never echoed by the API/UI (the driver still gets them).
SECRET_MAPPING_KEYS = ("token", "password", "secret", "api_key")
#: What a masked secret reads as in `battery_payload`; a PATCH sending it back keeps the stored one.
MASKED_SECRET = "***"


def redact_battery_mapping(mapping: Mapping[str, Any] | None) -> dict[str, str]:
    """The mapping as the API/UI may see it: credential values replaced by :data:`MASKED_SECRET`."""
    out: dict[str, str] = {}
    for key, value in (mapping or {}).items():
        name = str(key)
        if name.lower() in SECRET_MAPPING_KEYS and str(value or "") != "":
            out[name] = MASKED_SECRET
        else:
            out[name] = str(value)
    return out


def merge_mapping_patch(
    current: Mapping[str, Any] | None, patch: Mapping[str, Any] | None
) -> dict[str, str]:
    """A PATCH mapping that keeps stored credentials the client never saw (masked/absent).

    The UI reads the redacted mapping back, so a save that echoes :data:`MASKED_SECRET` -- or
    omits the key entirely -- means "keep the stored value"; anything else is a new value.
    """
    merged = {str(key): str(value) for key, value in (patch or {}).items()}
    for key, value in (current or {}).items():
        name = str(key)
        if name.lower() not in SECRET_MAPPING_KEYS or str(value or "") == "":
            continue
        if name not in merged or merged[name] == MASKED_SECRET:
            merged[name] = str(value)
    return merged

#: Every option (SPEC.md section 3 option schema) and every setting, with shipped defaults.
DEFAULTS: dict[str, Any] = {
    # -- add-on options ---------------------------------------------------------------
    "mode": "advisory",
    "dry_run": True,
    "log_level": "info",
    "currency": None,  # None = read from HA /api/config
    "timezone": None,  # None = read from HA /api/config
    "latitude_override": None,  # None = read from HA /api/config
    "longitude_override": None,
    # -- price ------------------------------------------------------------------------
    # Where prices come from and what shape the contract has (SPEC.md 4.1). The add-on resolves the
    # concrete source from these two; `entity` keeps the pre-0.3.0 explicit path.
    "price.provider": "entity",
    "price.contract": "dynamic_hourly",
    "price.source": "zonneplan_entity",
    "price.entity": "",
    "price.static_schedule": [],
    "price.import_fee_eur_per_kwh": 0.0,
    "price.export_fee_eur_per_kwh": 0.0,
    "price.vat_pct": 21.0,
    "price.energy_tax_eur_per_kwh": 0.0,
    "price.unit": "auto",
    "price.scale_divisor": 1.0,
    "price.bzn": "NL",
    # Fixed tariff (provider=fixed): consumer prices incl. VAT/tax, EUR/kWh. An entity, when set,
    # wins over the typed value. Unset price = failure, never a guessed or market-derived number.
    "price.fixed_price_eur_per_kwh": None,
    "price.fixed_day_price_eur_per_kwh": None,
    "price.fixed_night_price_eur_per_kwh": None,
    "price.fixed_price_entity": "",
    "price.fixed_day_price_entity": "",
    "price.fixed_night_price_entity": "",
    "price.fixed_day_start_hour": 7,
    "price.fixed_night_start_hour": 23,
    # Export tariff (SPEC.md section 4): slot.sell is EUR/kWh *received*, net of fees. Unset
    # feed-in = unknown -> sell 0.00 and no export value, never the buy price.
    "price.feed_in_eur_per_kwh": None,  # None = no flat feed-in tariff configured
    "price.feed_in_entity": "",  # optional HA sensor with the current feed-in price
    "price.feed_in_tax_inclusive": True,  # False = add price.vat_pct
    # Derived feed-in (the setup card): with no flat tariff or feed-in entity, sell = the consumer
    # buy price minus price.export_fee_eur_per_kwh, and minus its energy-tax component too when this
    # is off (a contract that does not pay the energy tax back).
    "price.feed_in_energy_tax_refund": True,
    # Contract-correct feed-in accounting (SPEC.md 4.2, optimizer v2 stage 1b). "derived" keeps the
    # pre-stage-1b behaviour byte-for-byte; the other modes route through sources/feed_in.py, which
    # prices exactly or refuses the battery-sourced export leg (never approximates silently).
    "price.feed_in_mode": "derived",
    # A stated energy-tax/levy component is usable only when it is *confirmed*: False means "never
    # stated", not zero. With the refund off, an unconfirmed component refuses the export leg
    # instead of silently pricing sell = buy.
    "price.energy_tax_confirmed": False,
    # Per-kWh tax/levy deducted from the feed-in price after saldering ends. None = use the slot's
    # own energy-tax component (or 0 for a source that publishes none).
    "price.feed_in_tax_deduction_eur_per_kwh": None,
    # saldering only: when the period import/export state is unavailable, explicitly assume the
    # period still has nettable import. Shown as an assumption in the plan, never a fact.
    "price.saldering_assume_nettable": False,
    # -- solar --------------------------------------------------------------------------
    "solar.enabled": True,
    "solar.source": "forecast_solar",
    "solar.planes": [],
    "solar.api_key": "",
    "solar.bias_correction": True,
    "solar.measured_entity": "",
    # Capacity-based estimate (solar.source=capacity), for AC-coupled arrays whose geometry is not
    # worth modelling: the user states the array's watt peak and the add-on estimates the rest from
    # astronomy plus, when available, cloud cover. Unset tilt/azimuth = the documented assumption
    # (south-facing, 35 degrees) and the forecast says so.
    "solar.capacity_wp": None,
    "solar.ac_capacity_wp": None,
    "solar.tilt_deg": None,
    "solar.azimuth_deg": None,
    "solar.performance_ratio": 0.85,
    "solar.weather_entity": "",
    # NASA POWER observed irradiance (keyless, near-real-time): a per-hour clearness factor that
    # fills in for slots without weather-entity cloud data and refines the derived watt-peak fit.
    "solar.nasa_irradiance": True,
    # Multi-source merge (0.12.0): an ordered list of {source, weight, ...provider keys}. Empty =
    # today's single-source behaviour via solar.source, byte-for-byte unchanged.
    "solar.sources": [],
    "solar.merge_strategy": "weighted_mean",
    # -- load ---------------------------------------------------------------------------
    "load.mode": "auto",
    "load.entity": "",
    "load.temperature_entity": "",
    "load.base_kwh_per_day": 0.0,
    # Named devices (SPEC.md section 7.2): a list of {entity, kind, name}. Each entry's meter is
    # subtracted from the house total before the base profile is learned and added back as its own
    # component, with its own temperature term when the kind has one. Empty = one house profile.
    "load.devices": [],
    # -- grid (roles confirmed from /discover?kind=grid; grid.power_entity enables the derived load tier) --
    "grid.power_entity": "",  # signed grid power sensor, positive = import
    "grid.import_entity": "",  # separate import power sensor, optional
    "grid.export_entity": "",  # separate export power sensor, optional
    # -- optimizer ----------------------------------------------------------------------
    # 60 min by default: most dynamic tariffs (and the Zonneplan sensor's own hourly list) are
    # hourly, and a plan grid finer than the price resolution only produces forward-filled slots
    # the optimiser must treat as flat (SPEC section 4). Set 5-60 to trade resolution for
    # fidelity; providers are always resampled onto this grid.
    "optimizer.slot_minutes": 60,
    # 30 h: the default Zonneplan feed reaches only ~+30 h, so a longer horizon always renders a
    # degraded (grey) tail; still covers the rest of today plus most of tomorrow.
    "optimizer.horizon_hours": 30,
    "optimizer.soc_step_kwh": 0.02,
    "optimizer.power_steps": 24,
    "optimizer.salvage_multiplier": 1.0,
    "optimizer.cycle_cost_eur_per_kwh": 0.0,
    "optimizer.allow_grid_charge": True,
    "optimizer.allow_export": False,  # until a feed-in tariff is confirmed (SPEC.md section 4)
    "optimizer.reserve_soc_pct": 0.0,
    "optimizer.max_grid_import_w": 0,
    "optimizer.max_grid_export_w": 0,
    "optimizer.min_spread_eur_per_kwh": 0.0,
    # How the plan values PV energy it charges into the pack (SPEC 8.1, 0.19.0): auto = the export
    # price it forgoes (the economically exact default), prefer_pv = 0 (absorb the surplus first),
    # avoid_pv = the slot's buy price (export the PV and charge from the grid in the cheapest
    # hours). An internal shadow price: the reported plan money is the true grid cost either way.
    "optimizer.pv_charge_preference": "auto",
    "optimizer.target_soc_pct_end": None,
    # Optimizer v2 stage 1b wiring (docs/OPTIMIZER_V2.md section 7): one kill switch for the whole
    # contract-correct feed-in integration. off = v1 pricing byte-for-byte and the settlement is
    # never called; shadow = settle and record a plan diff (learning profile + journal) while
    # dispatching v1 unchanged; on = dispatch the plan built from the settlement.
    "optimizer.feed_in_wiring": "off",
    # -- cost basis and the sell floor (SPEC 5, 0.18.0) ------------------------------------
    # The ledger is derived from the applied dispatch log and the measured flows, so the floor
    # refuses a discharge/export below what the energy actually in the pack cost. Default on: with
    # no ledger yet (basis None) it is inactive and behaviour is exactly as before.
    "optimizer.sell_floor_enabled": True,
    "optimizer.sell_floor_margin_eur_per_kwh": 0.0,
    # -- control --------------------------------------------------------------------------
    "control.tick_seconds": 30,
    "control.replan_minutes": 15,
    "control.grid_follow": True,
    "control.grid_follow_deadband_w": 30,
    "control.grid_follow_max_ratio": 0.5,
    # 0 = derive the absolute grid-follow budget from capability x ratio (min 500 W)
    "control.grid_follow_max_w": 0.0,
    "control.stale_state_seconds": 180,
    "control.stale_plan_seconds": 1800,
    "control.failsafe_on_error": True,
    # -- cell-voltage protection (SPEC 9.3 clause 4b, 0.16.0) -----------------------------
    # Disabled by default. Voltages are per cell, never a pack total; enabling it needs a real
    # min-cell-voltage source (a mapped entity, or the zendure_native driver's own read).
    # ``emergency_charge_power_w`` 0 means "use the battery's own max_charge_w".
    "protection.cell_voltage_enabled": False,
    "protection.cell_voltage_warning_v": 3.10,
    "protection.cell_voltage_cutoff_v": 3.00,
    "protection.cell_voltage_resume_v": 3.18,
    "protection.emergency_charge_power_w": 0.0,
    # -- scheduled full-charge maintenance (SPEC 8.1, 0.17.0) ------------------------------
    # Off by default. When on, a pack must reach ``full_charge_target_soc_pct`` at least every
    # ``full_charge_interval_days`` days so the BMS can balance its cells; the plan aims for the
    # target in the cheapest slots before the deadline. Per-battery runtime state (last completed,
    # next due) lives in the store profile, never in these user settings.
    "maintenance.full_charge_enabled": False,
    "maintenance.full_charge_interval_days": 30,
    "maintenance.full_charge_target_soc_pct": 100.0,
    # -- notifications ---------------------------------------------------------------------
    "notify.enabled": True,
    "notify.persistent": True,
    "notify.mobile_service": "",
    "notify.events": [
        "plan_changed",
        "plan_applied",
        "battery_floor_reached",
        "negative_price",
        "forecast_unavailable",
        "driver_error",
    ],
    # -- batteries (SPEC.md section 5) -------------------------------------------------------
    "batteries": [],
}

#: Add-on options whose stored value wins; a mismatch is warned about at load (never flipped).
OPTION_OVERRIDE_KEYS = ("mode", "dry_run")

#: The cell-voltage protection keys (SPEC 3/9.3). They are validated as a group, because the
#: ordering constraints span keys: ``warning_v`` and ``resume_v`` must both sit above ``cutoff_v``.
_PROTECTION_KEYS = (
    "protection.cell_voltage_enabled",
    "protection.cell_voltage_warning_v",
    "protection.cell_voltage_cutoff_v",
    "protection.cell_voltage_resume_v",
    "protection.emergency_charge_power_w",
)

_MODE_CHOICES = ("off", "advisory", "auto")
_LOG_CHOICES = ("debug", "info", "warning", "error")
_PRICE_SOURCE_CHOICES = ("zonneplan_entity", "energy_charts", "entity_forecast", "static")
_PRICE_PROVIDER_CHOICES = ("entity", "zonneplan", "day_ahead", "fixed")
#: Feed-in contract (optimizer v2 stage 1b). ``derived`` is the pre-1b behaviour; the others are
#: priced by :mod:`app.sources.feed_in`. The live forecast path still uses ``derived`` until the
#: optimizer consumes the module's complete result (``export_allowed``/``reason``/``floor``); a
#: refusal must never be silently turned into zero-priced slots. ``tiered`` needs the tier table from
#: the plan's reachability inputs, so a mode without one refuses safely until the wiring lands.
_FEED_IN_MODE_CHOICES = ("derived", "explicit", "buy_minus_fee", "saldering", "tiered")
#: Optimizer v2 stage 1b wiring (docs/OPTIMIZER_V2.md section 7): one kill switch for the whole
#: feed-in integration.  ``off`` never calls the settlement and keeps v1 byte-identical;
#: ``shadow`` settles and records a plan diff while dispatching v1 unchanged; ``on`` dispatches the
#: plan built from the settlement.
_FEED_IN_WIRING_CHOICES = ("off", "shadow", "on")
_PRICE_CONTRACT_CHOICES = ("dynamic_quarter_hourly", "dynamic_hourly", "fixed_single", "fixed_day_night")
_PRICE_UNIT_CHOICES = ("auto", "eur_per_kwh", "eur_per_mwh", "micro_eur_per_kwh")
_SOLAR_SOURCE_CHOICES = ("forecast_solar", "capacity", "derived", "learned")
#: Source types a ``solar.sources`` merge entry may name (0.12.0). ``ha_energy`` reads whatever
#: solar-forecast providers the Home Assistant Energy dashboard already has configured. ``learned``
#: (0.22.0) is the trained residual model in SPEC.md section 7.1.4.
_SOLAR_MERGE_SOURCE_CHOICES = ("forecast_solar", "ha_energy", "capacity", "derived", "learned")
#: How several per-source series become one (0.12.0).
_SOLAR_MERGE_STRATEGIES = ("weighted_mean", "median", "max")
#: Provider-specific keys a merge entry may carry, per source type. The key is the setting suffix
#: (``capacity_wp`` -> ``solar.capacity_wp``), overlaid on the global settings for that source.
_SOLAR_SOURCE_KEYS: dict[str, tuple[str, ...]] = {
    "forecast_solar": ("planes", "api_key"),
    "ha_energy": ("entry_id",),
    "capacity": (
        "capacity_wp",
        "tilt_deg",
        "azimuth_deg",
        "performance_ratio",
        "weather_entity",
        "nasa_irradiance",
    ),
    "derived": (
        "capacity_wp",
        "tilt_deg",
        "azimuth_deg",
        "performance_ratio",
        "weather_entity",
        "nasa_irradiance",
    ),
    # The learned source takes no performance_ratio: the artifact carries its own inverter,
    # temperature and clipping terms, so accepting the key here would let a user ask for a number to
    # be applied that deliberately is not.
    "learned": (
        "capacity_wp",
        "ac_capacity_wp",
        "tilt_deg",
        "azimuth_deg",
        "weather_entity",
        "nasa_irradiance",
    ),
}
_LOAD_MODE_CHOICES = ("auto", "entity", "derived")
#: The closed ``load.devices[].kind`` set (SPEC.md section 7.2). A device profile is learned per kind
#: (and per meter), and the kind decides whether the device gets its own temperature term; a closed
#: set is what lets the UI offer a dropdown and the model pick a default the user need not know.
#: ``sources/load.py`` keys its kind table by these names, and a test pins the two together.
LOAD_DEVICE_KINDS = ("heat_pump", "ev_charger", "water_heater", "pool_pump")
#: The only keys a ``load.devices`` entry may carry: the meter, the kind, and an optional display
#: name. Everything else the model needs is derived from the kind rather than entered per device.
_LOAD_DEVICE_KEYS = ("entity", "kind", "name")
_PV_CHARGE_PREFERENCE_CHOICES = ("auto", "prefer_pv", "avoid_pv")
_NOTIFY_EVENT_CHOICES = (
    "plan_changed",
    "plan_applied",
    "battery_floor_reached",
    "negative_price",
    "forecast_unavailable",
    "driver_error",
)


@dataclass(frozen=True)
class _Meta:
    type: str  # bool | int | float | str | choice | multi_choice | json
    group: str
    label: str = ""
    help: str = ""
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    nullable: bool = False  # None restores the "read from HA" / unset behaviour
    # Hidden behind the group's Advanced disclosure in the Settings UI (SPEC 13.2/13.3). The Safety
    # group is always visible regardless, and a label never carries a manual "(advanced)" suffix.
    advanced: bool = False

    def as_payload(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "group": self.group,
            "label": self.label,
            "help": self.help,
            "choices": list(self.choices),
            "min": self.minimum,
            "max": self.maximum,
            "nullable": self.nullable,
            "advanced": self.advanced,
        }


#: UI/validation metadata for every key in :data:`DEFAULTS` (SPEC.md section 10 `/api/v1/config`).
SETTINGS_META: dict[str, _Meta] = {
    "mode": _Meta("choice", "general", "Mode", "off, advisory or auto; advisory never writes.", _MODE_CHOICES),
    "dry_run": _Meta("bool", "general", "Dry run", "Log the writes that would happen without doing them."),
    "log_level": _Meta("choice", "general", "Log level", "", _LOG_CHOICES, advanced=True),
    "currency": _Meta("str", "general", "Currency", "Unset = read from HA.", nullable=True, advanced=True),
    "timezone": _Meta("str", "general", "Timezone", "Unset = read from HA.", nullable=True, advanced=True),
    "latitude_override": _Meta("float", "general", "Latitude override", "Unset = read from HA.", minimum=-90.0, maximum=90.0, nullable=True, advanced=True),
    "longitude_override": _Meta("float", "general", "Longitude override", "Unset = read from HA.", minimum=-180.0, maximum=180.0, nullable=True, advanced=True),
    "price.provider": _Meta("choice", "price", "Price provider", "Where prices come from. entity = a sensor you pick (advanced); zonneplan = the Zonneplan integration's tariff sensor found in Home Assistant; day_ahead = energy_charts wholesale day-ahead prices for the bidding zone; fixed = a flat or day/night tariff.", _PRICE_PROVIDER_CHOICES),
    "price.contract": _Meta("choice", "price", "Contract type", "dynamic_quarter_hourly / dynamic_hourly = market prices per quarter hour or per hour; fixed_single = one rate all day; fixed_day_night = two rates split by the day window below. Dynamic providers pick their own grid, so this only matters for a fixed tariff.", _PRICE_CONTRACT_CHOICES, advanced=True),
    "price.source": _Meta("choice", "price", "Price source", "Only used by the entity provider: configured source with fallback to energy_charts then static.", _PRICE_SOURCE_CHOICES, advanced=True),
    "price.entity": _Meta("str", "price", "Price entity", "Runtime-picked HA sensor entity id.", advanced=True),
    "price.fixed_price_eur_per_kwh": _Meta("float", "price", "Fixed rate", "Consumer price incl. VAT and tax, EUR/kWh. Unset = read the entity below.", minimum=-1.0, maximum=5.0, nullable=True),
    "price.fixed_day_price_eur_per_kwh": _Meta("float", "price", "Day rate", "Consumer price incl. VAT and tax for the day window, EUR/kWh.", minimum=-1.0, maximum=5.0, nullable=True),
    "price.fixed_night_price_eur_per_kwh": _Meta("float", "price", "Night rate", "Consumer price incl. VAT and tax outside the day window, EUR/kWh.", minimum=-1.0, maximum=5.0, nullable=True),
    "price.fixed_price_entity": _Meta("str", "price", "Fixed rate entity", "HA sensor holding the consumer price; overrides the typed fixed rate.", advanced=True),
    "price.fixed_day_price_entity": _Meta("str", "price", "Day rate entity", "HA sensor holding the day rate; overrides the typed day rate.", advanced=True),
    "price.fixed_night_price_entity": _Meta("str", "price", "Night rate entity", "HA sensor holding the night rate; overrides the typed night rate.", advanced=True),
    "price.fixed_day_start_hour": _Meta("int", "price", "Day starts at", "Local hour the day rate starts, 0-23.", minimum=0, maximum=23, advanced=True),
    "price.fixed_night_start_hour": _Meta("int", "price", "Night starts at", "Local hour the night rate starts, 0-23.", minimum=0, maximum=23, advanced=True),
    "price.static_schedule": _Meta("json", "price", "Static price schedule", "List of {from_hour,to_hour,price_eur_per_kwh,days}.", advanced=True),
    "price.import_fee_eur_per_kwh": _Meta("float", "price", "Import fee", "Supplier markup on buy, EUR/kWh.", minimum=-10.0, maximum=10.0, advanced=True),
    "price.export_fee_eur_per_kwh": _Meta("float", "price", "Feed-in cost", "Deducted per kWh from the feed-in price; default 0.", minimum=-10.0, maximum=10.0),
    "price.vat_pct": _Meta("float", "price", "VAT %", "Applied when a source publishes ex-VAT prices.", minimum=0.0, maximum=100.0, advanced=True),
    "price.energy_tax_eur_per_kwh": _Meta("float", "price", "Energy tax", "Applied when a source publishes ex-tax prices.", minimum=0.0, maximum=10.0, advanced=True),
    "price.unit": _Meta("choice", "price", "Declared unit", "auto = use the provider's declared unit.", _PRICE_UNIT_CHOICES, advanced=True),
    "price.scale_divisor": _Meta("float", "price", "Scale divisor", "For providers publishing fixed-point integers.", minimum=1e-9, maximum=1e9, advanced=True),
    "price.bzn": _Meta("str", "price", "Bidding zone", "energy_charts zone, e.g. NL, DE-LU.", advanced=True),
    "price.feed_in_eur_per_kwh": _Meta("float", "price", "Flat feed-in price", "Explicit EUR/kWh received; overrides the derived feed-in price.", minimum=-1.0, maximum=2.0, nullable=True, advanced=True),
    "price.feed_in_entity": _Meta("str", "price", "Feed-in price entity", "HA sensor with the current feed-in price; overrides the derived and flat feed-in prices.", advanced=True),
    "price.feed_in_tax_inclusive": _Meta("bool", "price", "Feed-in incl. VAT", "For the flat tariff/entity only: off = add price.vat_pct to the value.", advanced=True),
    "price.feed_in_energy_tax_refund": _Meta("bool", "price", "Energy tax refunded", "On = feed-in pays the same consumer price you buy at, minus the feed-in cost. Off = the energy-tax component is not paid back and is subtracted too."),
    "price.feed_in_mode": _Meta("choice", "price", "Feed-in contract", "How export is credited. derived = today's rule (the consumer price you pay minus the feed-in cost, minus the energy-tax component when it is not refunded). explicit = a stated feed-in tariff. buy_minus_fee = the consumer price minus the feed-in cost and the confirmed tax. saldering = the period nets export against import at the buy price while the allowance lasts, then pays the feed-in price. tiered = volume tiers (the tier table comes from the plan's reachability inputs). A contract the add-on cannot price exactly refuses the battery-sourced export leg instead of guessing. Not wired into the live forecast yet: until the optimizer consumes the full settlement result, the forecast keeps using the derived price.", _FEED_IN_MODE_CHOICES),
    "price.energy_tax_confirmed": _Meta("bool", "price", "Energy tax confirmed", "On = the energy-tax component is a confirmed value, including a confirmed 0. A contract that does not refund the tax refuses the battery-sourced export leg while this is off, rather than silently pricing sell = buy."),
    "price.feed_in_tax_deduction_eur_per_kwh": _Meta("float", "price", "Feed-in tax deduction", "Per-kWh tax/levy deducted from the feed-in price once saldering ends. Unset = use the slot's own energy-tax component.", minimum=0.0, maximum=1.0, nullable=True),
    "price.saldering_assume_nettable": _Meta("bool", "price", "Assume nettable", "saldering only: when the period import/export state is unavailable, assume this period still has nettable import. Shown as an assumption in the plan, never a fact."),
    "solar.enabled": _Meta("bool", "solar", "Solar forecast", "Enable the PV forecast."),
    "solar.source": _Meta("choice", "solar", "Solar source", "forecast_solar = the Forecast.Solar API (needs planes); capacity = estimate from the array's watt peak; derived = learn the array from the meter's export moments minus what the battery was charging, no numbers to enter; learned = a model trained on 174 other Dutch arrays that corrects its own physics for this array (needs solar.capacity_wp).", _SOLAR_SOURCE_CHOICES),
    "solar.planes": _Meta("json", "solar", "PV planes", "forecast_solar only. List of {kwp, tilt_deg, azimuth_deg (compass: 0=north), damping}.", advanced=True),
    "solar.api_key": _Meta("str", "solar", "Forecast.Solar key", "Optional personal key; keyless is single-plane hourly.", nullable=True, advanced=True),
    "solar.bias_correction": _Meta("bool", "solar", "Bias correction", "Learn a multiplicative correction from measured PV.", advanced=True),
    "solar.measured_entity": _Meta("str", "solar", "Measured PV entity", "Runtime-picked PV generation sensor. Use the inverter's AC output; a meter's export register under-reports because self-consumed energy never reaches the meter. The derived source reads the meter instead."),
    "solar.capacity_wp": _Meta("float", "solar", "Array watt peak", "Total Wp of the array (all panels added). Used by the capacity source; the derived source learns it. Unset = no capacity estimate.", minimum=0.0, maximum=200000.0, nullable=True, advanced=True),
    "solar.ac_capacity_wp": _Meta("float", "solar", "Inverter AC limit", "The learned source only. The inverter's maximum AC output in watts; when the array's DC watt peak is higher, the model clips past this. Unset = assumed equal to the watt peak (no clipping).", minimum=0.0, maximum=200000.0, nullable=True, advanced=True),
    "solar.tilt_deg": _Meta("float", "solar", "Tilt", "Panel tilt from horizontal. Unset = assume 35 degrees (stated in the forecast notes).", minimum=0.0, maximum=90.0, nullable=True, advanced=True),
    "solar.azimuth_deg": _Meta("float", "solar", "Azimuth", "Compass direction the panels face: 0 = north, 90 = east, 180 = south, 270 = west. Unset = assume south.", minimum=0.0, maximum=360.0, nullable=True, advanced=True),
    "solar.performance_ratio": _Meta("float", "solar", "Performance ratio", "Inverter, temperature and soiling losses applied to the clear-sky estimate.", minimum=0.5, maximum=1.1, advanced=True),
    "solar.weather_entity": _Meta("str", "solar", "Weather entity", "Optional HA weather entity; its hourly cloud coverage attenuates the capacity estimate. Unset = clear-sky, refined by the measured-PV correction.", advanced=True),
    "solar.nasa_irradiance": _Meta("bool", "solar", "NASA irradiance", "Refine the capacity/derived estimate with NASA POWER's observed hourly irradiance (keyless, 2-3 day latency): a per-hour clearness factor for slots without weather-entity cloud data, also used when fitting the meter-derived array. Weather-entity cloud and the measured-PV correction still win where they apply. Only the array's location is sent.", advanced=True),
    "solar.sources": _Meta("json", "solar", "Forecast sources", "Merge several PV forecasts into one: an ordered list of {source, weight, ...}. source is forecast_solar, ha_energy (any solar-forecast provider the HA Energy dashboard already has), capacity or derived; weight is a finite number > 0. Empty = use solar.source alone. Per-source keys override the matching solar.* setting for that entry only.", advanced=True),
    "solar.merge_strategy": _Meta("choice", "solar", "Merge strategy", "How several sources become one value per slot: weighted_mean = the weighted average (default), median = the middle value (equal say per source), max = the most optimistic source. Skipped/unavailable sources never contribute.", _SOLAR_MERGE_STRATEGIES, advanced=True),
    "load.mode": _Meta("choice", "load", "Load source", "auto uses the load entity if mapped, else derived.", _LOAD_MODE_CHOICES, advanced=True),
    "load.entity": _Meta("str", "load", "Load entity", "Runtime-picked load power sensor.", advanced=True),
    "load.temperature_entity": _Meta("str", "load", "Outdoor temperature entity", "Optional regression term.", advanced=True),
    "load.base_kwh_per_day": _Meta("float", "load", "Fallback daily energy", "Flat profile until something is learned.", minimum=0.0, maximum=1000.0, advanced=True),
    "load.devices": _Meta(
        "json",
        "load",
        "Named load devices",
        "Meters for individual loads that need their own profile and their own temperature response: "
        "a heat pump, an EV charger, a water heater or a pool pump. Each entry is {entity, kind, "
        "name}; kind is one of " + ", ".join(LOAD_DEVICE_KINDS) + "; name is optional. A device's "
        "meter is subtracted from the house total before the base profile is learned and added back "
        "on top of it, so a device that cannot be measured is left inside the base and named in the "
        "forecast notes rather than counted twice. Only a heat pump, water heater or pool pump gets "
        "its own temperature term; an EV charger's draw is a schedule.",
        LOAD_DEVICE_KINDS,
        advanced=True,
    ),
    "grid.power_entity": _Meta("str", "grid", "Grid power entity", "Signed grid power sensor, positive = import; enables the derived load tier."),
    "grid.import_entity": _Meta("str", "grid", "Grid import entity", "Separate import power sensor (optional).", advanced=True),
    "grid.export_entity": _Meta("str", "grid", "Grid export entity", "Separate export power sensor (optional).", advanced=True),
    "optimizer.slot_minutes": _Meta(
        "int",
        "optimizer",
        "Slot minutes",
        "Plan and dispatch resolution. Set it to the tariff's own slot length (60 for hourly "
        "prices): a finer grid than the price feed only adds forward-filled slots the optimiser "
        "treats as flat.",
        minimum=5,
        maximum=60,
        advanced=True,
    ),
    "optimizer.horizon_hours": _Meta("int", "optimizer", "Horizon hours", "Clamped to 12..48 by the optimizer. Default 30 h matches the ~+30 h Zonneplan feed.", minimum=1, maximum=72),
    "optimizer.soc_step_kwh": _Meta("float", "optimizer", "SoC step", "kWh per DP state.", minimum=0.001, maximum=5.0, advanced=True),
    "optimizer.power_steps": _Meta("int", "optimizer", "Power steps", "Discrete actions per state.", minimum=3, maximum=200, advanced=True),
    "optimizer.salvage_multiplier": _Meta("float", "optimizer", "Salvage multiplier", "Terminal SoC valued at multiplier x median buy price.", minimum=0.0, maximum=3.0, advanced=True),
    "optimizer.cycle_cost_eur_per_kwh": _Meta("float", "optimizer", "Wear override", "Advanced: a non-zero value replaces every battery's derived wear (price / (cycle life x usable kWh x discharge efficiency)) for planning and break-even. Leave 0 to use the derived per-fleet average wear.", minimum=0.0, maximum=1.0, advanced=True),
    "optimizer.allow_grid_charge": _Meta("bool", "optimizer", "Allow grid charging", ""),
    "optimizer.allow_export": _Meta("bool", "optimizer", "Allow export", "Off = never export on purpose. The feed-in price comes from the Price card."),
    "optimizer.reserve_soc_pct": _Meta("float", "optimizer", "Reserve SoC", "Extra floor above battery min_soc. The cost-basis ledger and the sell floor price only the energy above it.", minimum=0.0, maximum=100.0),
    "optimizer.max_grid_import_w": _Meta("float", "optimizer", "Max grid import W", "0 = unlimited.", minimum=0.0),
    "optimizer.max_grid_export_w": _Meta("float", "optimizer", "Max grid export W", "0 = unlimited.", minimum=0.0),
    "optimizer.min_spread_eur_per_kwh": _Meta("float", "optimizer", "Min spread", "Ignore arbitrage thinner than this.", minimum=0.0, maximum=10.0),
    "optimizer.pv_charge_preference": _Meta("choice", "optimizer", "PV charge preference", "How the plan values PV energy it charges into the pack: auto = the export price it forgoes (the economically exact default, so PV wins whenever its export price is below the buy price), prefer_pv = free (absorb the surplus before buying anything), avoid_pv = the slot's buy price (export the PV and charge from the grid in the cheapest hours). A shadow price for planning only: the reported plan money stays the true grid cost.", _PV_CHARGE_PREFERENCE_CHOICES),
    "optimizer.sell_floor_enabled": _Meta("bool", "optimizer", "Sell floor", "Refuse a discharge or export below the break-even of the energy actually in the pack (the cost-basis ledger, learned from measured charge intervals and the prices they were charged at). Off, or before a basis exists, behaves exactly as before."),
    "optimizer.sell_floor_margin_eur_per_kwh": _Meta("float", "optimizer", "Sell floor margin", "Require this much above the break-even before a sale is allowed.", minimum=0.0, maximum=1.0, advanced=True),
    "optimizer.feed_in_wiring": _Meta("choice", "optimizer", "Feed-in wiring", "How the optimizer consumes the contract-correct feed-in settlement (price.feed_in_mode). off = today's behaviour; the settlement is never called. shadow = settle and record a plan diff against the v1 schedule (learning profile feed_in_shadow + journal) while dispatching v1 unchanged. on = dispatch the plan built from the settlement: the contract's per-slot price replaces the derived one and, when the battery-sourced export leg is refused, only battery-sourced export is blocked while forced PV surplus is still priced at the settlement's justified floor. An unpriceable contract (derived, or no justified floor) never re-prices with the v1 formula: planning fails safe by holding, and the independent safety layer (cell-voltage emergency charge, watchdog) covers an unsafe hold. Requires price.feed_in_mode to be a supported contract before turning it on.", _FEED_IN_WIRING_CHOICES),
    "optimizer.target_soc_pct_end": _Meta("float", "optimizer", "End SoC target %", "Unset = no hard terminal target.", minimum=0.0, maximum=100.0, nullable=True, advanced=True),
    "control.tick_seconds": _Meta("int", "control", "Tick seconds", "", minimum=5, maximum=3600, advanced=True),
    "control.replan_minutes": _Meta("int", "control", "Replan minutes", "", minimum=1, maximum=1440, advanced=True),
    "control.grid_follow": _Meta("bool", "control", "Grid follow", "Correct small grid deviations.", advanced=True),
    "control.grid_follow_deadband_w": _Meta("float", "control", "Grid deadband W", "", minimum=0.0, maximum=10000.0, advanced=True),
    "control.grid_follow_max_ratio": _Meta("float", "control", "Grid follow max ratio", "", minimum=0.0, maximum=1.0, advanced=True),
    "control.grid_follow_max_w": _Meta("float", "control", "Grid follow max W", "0 = derive the absolute budget from the battery capability x ratio (min 500 W).", minimum=0.0, advanced=True),
    "control.stale_state_seconds": _Meta("int", "control", "Stale state seconds", "Older measurement means hold power.", minimum=5, maximum=3600, advanced=True),
    "control.stale_plan_seconds": _Meta("int", "control", "Stale plan seconds", "Refuse to dispatch an active plan older than this.", minimum=60, maximum=86400, advanced=True),
    "control.failsafe_on_error": _Meta("bool", "control", "Fail safe on error", "Write 0 W on unrecoverable error."),
    "protection.cell_voltage_enabled": _Meta("bool", "safety", "Cell-voltage protection", "Watch the lowest cell voltage and charge the pack out of danger. Needs a min-cell-voltage entity mapped on the battery, or the zendure_native driver's own read; never a pack total."),
    "protection.cell_voltage_warning_v": _Meta("float", "safety", "Warning cell voltage", "Volts per cell; must be above the cutoff. Informational only.", minimum=0.0, maximum=10.0),
    "protection.cell_voltage_cutoff_v": _Meta("float", "safety", "Cutoff cell voltage", "Below this the battery is latched into an emergency charge and may not discharge.", minimum=0.0, maximum=10.0),
    "protection.cell_voltage_resume_v": _Meta("float", "safety", "Resume cell voltage", "At or above this the latch clears and normal planning resumes; must be above the cutoff.", minimum=0.0, maximum=10.0),
    "protection.emergency_charge_power_w": _Meta("float", "safety", "Emergency charge power W", "Charge power for a latched battery, capped by its own max charge. 0 = use max_charge_w.", minimum=0.0, maximum=100000.0),
    "maintenance.full_charge_enabled": _Meta("bool", "maintenance", "Scheduled full charge", "Reach the target SoC at least every interval so the BMS can balance its cells. The optimiser charges in the cheapest slots before the deadline; advisory still plans it and writes nothing."),
    "maintenance.full_charge_interval_days": _Meta("int", "maintenance", "Full-charge interval (days)", "How often every enabled battery must reach the target SoC.", minimum=1, maximum=365, advanced=True),
    "maintenance.full_charge_target_soc_pct": _Meta("float", "maintenance", "Full-charge target %", "Terminal SoC the plan aims for while a full charge is due; a higher optimizer.target_soc_pct_end still wins.", minimum=50.0, maximum=100.0, advanced=True),
    "notify.enabled": _Meta("bool", "notify", "Notifications", ""),
    "notify.persistent": _Meta("bool", "notify", "Persistent notification", "Also create an HA persistent notification.", advanced=True),
    "notify.mobile_service": _Meta("str", "notify", "Mobile service", "e.g. notify.example_mobile_app; empty = off.", advanced=True),
    "notify.events": _Meta("multi_choice", "notify", "Notify on events", "", _NOTIFY_EVENT_CHOICES, advanced=True),
    "batteries": _Meta("json", "batteries", "Batteries", "Managed by the battery wizard, not the settings form.", advanced=True),
}


def _number_or(value: Any, fallback: float) -> float:
    """A finite float for a settings value, else the fallback (never raises, never guesses)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return float(fallback)
    number = float(value)
    return number if math.isfinite(number) else float(fallback)


def protection_problem(values: Mapping[str, Any]) -> str | None:
    """Cross-key validation of the protection trio (SPEC 3/9.3): warning and resume above cutoff.

    Applies regardless of ``cell_voltage_enabled``: an ordering that is invalid while disabled
    would silently become dangerous the moment it is switched on.
    """
    cutoff = _number_or(values.get("protection.cell_voltage_cutoff_v"), 3.00)
    warning = _number_or(values.get("protection.cell_voltage_warning_v"), 3.10)
    resume = _number_or(values.get("protection.cell_voltage_resume_v"), 3.18)
    if warning <= cutoff:
        return "protection.cell_voltage_warning_v must be above protection.cell_voltage_cutoff_v"
    if resume <= cutoff:
        return "protection.cell_voltage_resume_v must be above protection.cell_voltage_cutoff_v"
    return None


def protection_patch_errors(
    values: Mapping[str, Any], current: Mapping[str, Any]
) -> dict[str, str]:
    """Per-key errors for a protection patch, judged on the patch's *final* state.

    A save that moves cutoff and warning together must be accepted or rejected as a whole, never
    rejected because the first key happened to be applied before the second.
    """
    pending = {key: value for key, value in values.items() if key in _PROTECTION_KEYS}
    if not pending:
        return {}
    effective = {key: current.get(key) for key in _PROTECTION_KEYS}
    errors: dict[str, str] = {}
    for key, value in pending.items():
        try:
            effective[key] = coerce_setting(key, value)
        except ConfigError as err:
            errors[key] = str(err)
    if errors:
        return errors
    problem = protection_problem(effective)
    return {key: problem for key in pending} if problem else {}


def load_options(path: str = "/data/options.json") -> dict:
    """Read add-on options; ``{}`` when the file is missing or unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as err:
        log.warning("ignoring unreadable options file %s: %s", path, err)
        return {}
    return data if isinstance(data, dict) else {}


def resolve_location(ha_config: Mapping[str, Any], options: Mapping[str, Any]) -> tuple[float, float, str]:
    """Return ``(latitude, longitude, timezone)``.

    Order: option override -> HA ``/api/config`` -> :class:`ConfigError`. HA reports ``0, 0`` when the
    location is unset, so that pair is treated as missing, and the zone comes from HA's ``time_zone``
    field (validated with :mod:`zoneinfo`). A missing location disables solar forecasting with a
    visible warning; it never invents coordinates (SPEC.md section 3).
    """
    override_latitude = options.get("latitude_override")
    override_longitude = options.get("longitude_override")
    latitude = override_latitude if override_latitude is not None else ha_config.get("latitude")
    longitude = override_longitude if override_longitude is not None else ha_config.get("longitude")
    if latitude is None or longitude is None:
        raise ConfigError(
            "location unavailable: set latitude_override/longitude_override or expose /api/config"
        )
    try:
        latitude_f = float(latitude)
        longitude_f = float(longitude)
    except (TypeError, ValueError) as err:
        raise ConfigError(f"location is not numeric: {err}") from None
    if (
        latitude_f == 0.0
        and longitude_f == 0.0
        and override_latitude is None
        and override_longitude is None
    ):
        raise ConfigError(
            "location unavailable: /api/config reports 0, 0 (unset); set "
            "latitude_override/longitude_override"
        )
    name = str(options.get("timezone") or ha_config.get("time_zone") or ha_config.get("timezone") or "UTC")
    try:
        ZoneInfo(name)
    except (ValueError, KeyError):
        raise ConfigError(f"unknown timezone: {name!r}") from None
    return latitude_f, longitude_f, name


# --------------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------------


class Settings:
    """Read view over DEFAULTS + options + DB overrides, with validated writes.

    ``store`` may be ``None`` for read-only use (then :meth:`set` keeps overrides in memory only).
    """

    def __init__(
        self,
        store: Any = None,
        options: Mapping[str, Any] | None = None,
        options_path: str = "/data/options.json",
    ) -> None:
        self.store = store
        self.options: dict[str, Any] = dict(options) if options is not None else load_options(options_path)
        self._memory: dict[str, Any] = {}
        self._warn_option_conflicts()

    # -- add-on options vs stored settings --------------------------------------
    def option_conflicts(self) -> list[dict[str, str]]:
        """Add-on options the stored settings override (``mode``/``dry_run``), for the UI to show.

        A value written by the app (e.g. "Stop now" persisting ``mode=advisory``) beats the add-on
        option forever; editing the option in Home Assistant then has no effect. Nothing is flipped
        either way — the conflict is only reported so the user learns where the value lives.
        """
        conflicts: list[dict[str, str]] = []
        if self.store is None or not callable(getattr(self.store, "get_setting", None)):
            return conflicts
        for key in OPTION_OVERRIDE_KEYS:
            if key not in self.options:
                continue
            stored = self.store.get_setting(key, _MISSING)
            if stored is _MISSING:
                continue
            try:
                option_value = coerce_setting(key, self.options[key])
            except ConfigError:
                continue
            if stored == option_value:
                continue
            conflicts.append(
                {
                    "key": key,
                    "option": str(self.options[key]),
                    "stored": str(stored),
                    "message": (
                        f"add-on option {key}={self.options[key]!r} is ignored: the stored setting "
                        f"{key}={stored!r} wins (usually written by the app, e.g. Stop now); change "
                        "it in the app's settings, not in the add-on options"
                    ),
                }
            )
        return conflicts

    def _warn_option_conflicts(self) -> None:
        for conflict in self.option_conflicts():
            log.warning("%s", conflict["message"])

    def price_warnings(self) -> list[dict[str, str]]:
        """Price provider/contract problems for the settings UI (SPEC.md 4.1, GET /api/v1/config).

        Returns a list of ``{"key", "message"}``. A mismatch is a **warning, never a failure**: the
        engine keeps the configured provider, because refusing to plan is worse than planning with a
        stated mismatch. Cheap and side-effect free (settings only, no network).
        """
        out: list[dict[str, str]] = []
        provider = str(self.get("price.provider", "entity") or "entity").strip()
        contract = str(self.get("price.contract", "dynamic_hourly") or "dynamic_hourly").strip()
        dynamic_contracts = ("dynamic_quarter_hourly", "dynamic_hourly")
        fixed_contracts = ("fixed_single", "fixed_day_night")

        # 1) provider/contract mismatch (SPEC 4.1 table): dynamic providers take a dynamic
        #    contract, the fixed provider a fixed one; ``entity`` accepts any.
        if provider in ("zonneplan", "day_ahead") and contract not in dynamic_contracts:
            out.append(
                {
                    "key": "price.contract",
                    "message": (
                        f"price.provider {provider!r} supplies dynamic market prices, but "
                        f"price.contract is {contract!r}; choose dynamic_quarter_hourly or "
                        "dynamic_hourly"
                    ),
                }
            )
        elif provider == "fixed" and contract not in fixed_contracts:
            out.append(
                {
                    "key": "price.contract",
                    "message": (
                        "price.provider 'fixed' needs a fixed contract, but price.contract is "
                        f"{contract!r}; choose fixed_single or fixed_day_night"
                    ),
                }
            )

        # 2) fixed-tariff completeness. A value may come from the typed field or, winning when set,
        #    its entity; an absent one fails loudly at planning time, so warn here first.
        if contract == "fixed_single":
            if not self._has_fixed_price(
                "price.fixed_price_eur_per_kwh", "price.fixed_price_entity"
            ):
                out.append(
                    {
                        "key": "price.fixed_price_eur_per_kwh",
                        "message": (
                            "fixed_single needs a single rate: set price.fixed_price_eur_per_kwh or "
                            "price.fixed_price_entity"
                        ),
                    }
                )
        elif contract == "fixed_day_night":
            for label, price_key, entity_key in (
                ("day", "price.fixed_day_price_eur_per_kwh", "price.fixed_day_price_entity"),
                ("night", "price.fixed_night_price_eur_per_kwh", "price.fixed_night_price_entity"),
            ):
                if not self._has_fixed_price(price_key, entity_key):
                    out.append(
                        {
                            "key": price_key,
                            "message": (
                                f"fixed_day_night needs a {label} rate: set {price_key} or {entity_key}"
                            ),
                        }
                    )
            if self._fixed_day_equals_night_start():
                out.append(
                    {
                        "key": "price.fixed_day_start_hour",
                        "message": (
                            "price.fixed_day_start_hour equals price.fixed_night_start_hour; the "
                            "day and night windows would be empty"
                        ),
                    }
                )

        # 3) wholesale day-ahead with no adders configured is not a consumer price (SPEC 4.1): the
        #    forecast stays ex-VAT/ex-tax unless VAT, energy tax or a fee is set.
        if provider == "day_ahead" and self._adders_are_zero():
            out.append(
                {
                    "key": "price.vat_pct",
                    "message": (
                        "wholesale prices with no adders: this is not a consumer price; set "
                        "price.vat_pct, price.energy_tax_eur_per_kwh and/or the import/export fees"
                    ),
                }
            )
        return out

    def _has_fixed_price(self, typed_key: str, entity_key: str) -> bool:
        if self.get(typed_key) is not None:
            return True
        return bool(str(self.get(entity_key, "") or "").strip())

    def _fixed_day_equals_night_start(self) -> bool:
        try:
            return int(self.get("price.fixed_day_start_hour")) == int(
                self.get("price.fixed_night_start_hour")
            )
        except (TypeError, ValueError):
            return False

    def _adders_are_zero(self) -> bool:
        def is_zero(key: str) -> bool:
            try:
                return float(self.get(key, 0.0) or 0.0) == 0.0
            except (TypeError, ValueError):
                return True

        return (
            is_zero("price.vat_pct")
            and is_zero("price.energy_tax_eur_per_kwh")
            and is_zero("price.import_fee_eur_per_kwh")
            and is_zero("price.export_fee_eur_per_kwh")
        )

    # -- reads ----------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        if self.store is not None:
            value = self.store.get_setting(key, _MISSING)
            if value is not _MISSING:
                return _copy_value(value)
        if key in self._memory:
            return _copy_value(self._memory[key])
        if key in self.options:
            return _copy_value(self.options[key])
        if key in DEFAULTS:
            return _copy_value(DEFAULTS[key])
        return default

    def all(self) -> dict[str, Any]:
        return {key: self.get(key) for key in DEFAULTS}

    def __getitem__(self, key: str) -> Any:
        if key not in DEFAULTS:
            raise KeyError(key)
        return self.get(key)

    def __contains__(self, key: str) -> bool:
        return key in DEFAULTS

    # -- writes ----------------------------------------------------------------
    def set(self, key: str, value: Any) -> Any:
        """Validate and persist one setting; returns the coerced value."""
        coerced = coerce_setting(key, value)
        if key in _PROTECTION_KEYS:
            # Single-key saves are still judged against the trio's current values (SPEC 3/9.3).
            problem = protection_patch_errors(
                {key: coerced}, {name: self.get(name) for name in _PROTECTION_KEYS}
            ).get(key)
            if problem:
                raise ConfigError(problem)
        if key in ("load.devices", "load.entity"):
            # Same rule for the device list: the house meter cannot be one of its own devices.
            problem = load_device_patch_errors(
                {key: coerced}, {name: self.get(name) for name in ("load.devices", "load.entity")}
            ).get(key)
            if problem:
                raise ConfigError(problem)
        self._persist_setting(key, coerced)
        return coerced

    def _persist_setting(self, key: str, coerced: Any) -> None:
        if key == "batteries":
            raise ConfigError("use the battery helpers to change batteries")
        if self.store is not None:
            self.store.set_setting(key, coerced)
        self._memory[key] = coerced

    def update(self, values: Mapping[str, Any]) -> tuple[list[str], list[dict[str, str]]]:
        """Apply a settings patch; returns ``(applied keys, errors)``.

        The protection trio is validated as a whole first, so a patch that moves several of its
        keys at once is judged on its final state rather than on a half-applied one.
        """
        applied: list[str] = []
        errors: list[dict[str, str]] = []
        protection_errors = protection_patch_errors(
            values, {name: self.get(name) for name in _PROTECTION_KEYS}
        )
        # A patch that moves load.entity and load.devices together is judged on its final state, so
        # a device list is never rejected because the house meter happened to be applied first.
        device_errors = load_device_patch_errors(
            values, {name: self.get(name) for name in ("load.devices", "load.entity")}
        )
        for key, value in values.items():
            if key in protection_errors:
                errors.append({"key": key, "message": protection_errors[key]})
                continue
            if key in device_errors:
                errors.append({"key": key, "message": device_errors[key]})
                continue
            try:
                self._persist_setting(key, coerce_setting(key, value))
                applied.append(key)
            except ConfigError as err:
                errors.append({"key": key, "message": str(err)})
        return applied, errors

    # -- batteries ----------------------------------------------------------------
    def battery_list(self) -> list[BatteryConfig]:
        raw = self.get("batteries")
        if not isinstance(raw, list):
            return []
        out: list[BatteryConfig] = []
        for entry in raw:
            if isinstance(entry, BatteryConfig):
                out.append(entry)
                continue
            if not isinstance(entry, Mapping):
                continue
            try:
                out.append(_coerce_battery(entry))
            except ConfigError as err:
                # Never drop silently: an unchanged battery must still be reported and repaired.
                log.warning("skipping malformed battery entry %r: %s", entry.get("id"), err)
        return out

    def battery(self, battery_id: str) -> BatteryConfig | None:
        for battery in self.battery_list():
            if battery.id == battery_id:
                return battery
        return None

    def add_battery(self, battery: BatteryConfig | Mapping[str, Any]) -> BatteryConfig:
        config = _coerce_battery(battery)
        batteries = self.battery_list()
        if any(existing.id == config.id for existing in batteries):
            raise ConfigError(f"battery id already exists: {config.id}")
        _require_valid_battery(config)
        batteries.append(config)
        self._save_batteries(batteries)
        return config

    def update_battery(self, battery_id: str, patch: Mapping[str, Any]) -> BatteryConfig:
        batteries = self.battery_list()
        for index, current in enumerate(batteries):
            if current.id != battery_id:
                continue
            payload = asdict(current)
            known = {f.name for f in fields(BatteryConfig)}
            unknown = set(patch) - known - set(_CONVENIENCE_BATTERY_FIELDS)
            if unknown:
                raise ConfigError(f"unknown battery fields: {sorted(unknown)}")
            if patch.get("round_trip_efficiency_pct") is not None and not _has_explicit_eta(patch):
                # A pct-only patch asks for the symmetric split: drop the inherited per-direction
                # values so the split applies instead of the old values winning.
                payload.pop("eta_charge", None)
                payload.pop("eta_discharge", None)
            payload.update(patch)
            if "mapping" in patch:
                payload["mapping"] = {str(k): str(v) for k, v in (patch["mapping"] or {}).items()}
            updated = _coerce_battery(payload)
            if updated.id != battery_id:
                raise ConfigError("battery id cannot be changed")
            _require_valid_battery(updated)
            batteries[index] = updated
            self._save_batteries(batteries)
            return updated
        raise ConfigError(f"unknown battery: {battery_id}")

    def remove_battery(self, battery_id: str) -> bool:
        batteries = self.battery_list()
        remaining = [battery for battery in batteries if battery.id != battery_id]
        if len(remaining) == len(batteries):
            return False
        self._save_batteries(remaining)
        return True

    def _save_batteries(self, batteries: list[BatteryConfig]) -> None:
        payload = [asdict(battery) for battery in batteries]
        if self.store is not None:
            self.store.set_setting("batteries", payload)
        self._memory["batteries"] = payload


_MISSING = object()


def _copy_value(value: Any) -> Any:
    """Return mutable setting values by value so callers cannot corrupt DEFAULTS/options."""
    if isinstance(value, list):
        return [_copy_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _copy_value(item) for key, item in value.items()}
    return value


#: Convenience/derived inputs accepted from the UI/API but never persisted as settings keys
#: (SPEC.md §5): the requested round trip and the wear the server derives from price/cycle life.
_CONVENIENCE_BATTERY_FIELDS = ("round_trip_efficiency_pct", "wear_eur_per_kwh")


def _coerce_round_trip_pct(value: Any) -> float:
    """Validate the percent round-trip input (0 < pct <= 100); raises :class:`ConfigError`."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"round_trip_efficiency_pct must be a number in (0, 100], got {value!r}")
    pct = float(value)
    if not 0.0 < pct <= 100.0:
        raise ConfigError(f"round_trip_efficiency_pct must be in (0, 100], got {value!r}")
    return pct


def _has_explicit_eta(source: Mapping[str, Any]) -> bool:
    """True when the payload itself pins a per-direction efficiency (a JSON null does not)."""
    return any(source.get(key) is not None for key in ("eta_charge", "eta_discharge"))


def _split_round_trip(pct: float) -> tuple[float, float]:
    """Split a round trip into per-direction efficiencies whose float product is exactly the
    requested fraction: sqrt() squared is one ULP off for most percents, so a single-ULP
    asymmetry is accepted when it makes ``eta_charge * eta_discharge == pct / 100`` exact.
    """

    def step(value: float, ulps: int) -> float:
        for _ in range(abs(ulps)):
            value = math.nextafter(value, math.inf if ulps > 0 else -math.inf)
        return value

    target = pct / 100.0
    eta = math.sqrt(target)
    for charge_ulps, discharge_ulps in ((0, 0), (0, 1), (1, 0), (0, -1), (-1, 0), (0, 2), (2, 0)):
        charge = step(eta, charge_ulps)
        discharge = step(eta, discharge_ulps)
        if charge * discharge == target:
            return charge, discharge
    return eta, eta


def battery_payload(config: BatteryConfig) -> dict[str, Any]:
    """Serializable battery view for the UI/API: the stored fields plus the derived
    ``round_trip_efficiency_pct`` and ``wear_eur_per_kwh`` (SPEC.md section 5). Both are
    convenience/derived values and are never persisted as settings keys."""
    payload = asdict(config)
    # Credentials are stored for the driver but never echoed back to a client (SPEC 3.3/3.4).
    payload["mapping"] = redact_battery_mapping(config.mapping)
    payload["round_trip_efficiency_pct"] = round(config.round_trip_efficiency * 100.0, 6)
    payload["wear_eur_per_kwh"] = round(config.wear_eur_per_kwh, 9)
    return payload


def _coerce_battery(battery: BatteryConfig | Mapping[str, Any]) -> BatteryConfig:
    if isinstance(battery, BatteryConfig):
        return battery
    if not isinstance(battery, Mapping):
        raise ConfigError("battery must be an object")
    known = {f.name for f in fields(BatteryConfig)}
    payload = {k: v for k, v in battery.items() if k in known}
    missing = {"id", "name", "driver", "capacity_kwh"} - set(payload)
    if missing:
        raise ConfigError(f"battery missing required fields: {sorted(missing)}")
    if not isinstance(payload.get("mapping", {}), Mapping):
        raise ConfigError("battery mapping must be an object")
    payload["mapping"] = {str(k): str(v) for k, v in (payload.get("mapping") or {}).items()}
    # An explicit per-direction efficiency wins; otherwise the requested round trip is split
    # symmetrically so its product is exactly the number the user entered (SPEC.md section 5).
    if battery.get("round_trip_efficiency_pct") is not None:
        pct = _coerce_round_trip_pct(battery["round_trip_efficiency_pct"])
        if not _has_explicit_eta(battery):
            eta_charge, eta_discharge = _split_round_trip(pct)
            payload["eta_charge"] = eta_charge
            payload["eta_discharge"] = eta_discharge
    try:
        return BatteryConfig(**payload)
    except TypeError as err:
        raise ConfigError(f"invalid battery: {err}") from None


def _require_valid_battery(config: BatteryConfig) -> None:
    problems = list(config.validate()) + validate_battery_config(config)
    if problems:
        raise ConfigError("; ".join(problems))


def validate_battery_config(config: BatteryConfig) -> list[str]:
    """Extra SPEC.md section 5 rules beyond :meth:`BatteryConfig.validate`."""
    problems: list[str] = []
    if config.driver not in driver_names():
        problems.append(f"unknown driver: {config.driver}")
    if config.driver == "generic":
        mapping = config.mapping or {}
        setpoint = mapping.get("power_setpoint_entity")
        pair = mapping.get("charge_power_entity") and mapping.get("discharge_power_entity")
        if not (setpoint or pair):
            problems.append(
                "generic battery needs power_setpoint_entity or "
                "charge_power_entity + discharge_power_entity"
            )
    return problems


# --------------------------------------------------------------------------------------------
# Coercion / validation
# --------------------------------------------------------------------------------------------


def coerce_setting(key: str, value: Any) -> Any:
    """Validate ``value`` for ``key`` and return the typed value; raises :class:`ConfigError`."""
    if key not in DEFAULTS:
        raise ConfigError(f"unknown setting: {key}")
    meta = SETTINGS_META.get(key)
    if meta is None:
        return value
    if value is None and meta.nullable:
        return None
    if meta.type == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0", "yes", "no"):
            return value.strip().lower() in ("true", "1", "yes")
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        raise ConfigError(f"{key} must be a boolean")
    if meta.type in ("int", "float"):
        if isinstance(value, bool):
            raise ConfigError(f"{key} must be a number")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{key} must be a number") from None
        if meta.type == "int":
            if number != int(number):
                raise ConfigError(f"{key} must be a whole number")
            number = int(number)
        if meta.minimum is not None and number < meta.minimum:
            raise ConfigError(f"{key} must be >= {meta.minimum}")
        if meta.maximum is not None and number > meta.maximum:
            raise ConfigError(f"{key} must be <= {meta.maximum}")
        return number
    if meta.type == "choice":
        text = str(value)
        if text not in meta.choices:
            raise ConfigError(f"{key} must be one of {list(meta.choices)}")
        return text
    if meta.type == "multi_choice":
        if value is None:
            raise ConfigError(f"{key} must be a list")
        items = value if isinstance(value, (list, tuple)) else [value]
        unknown = [str(item) for item in items if str(item) not in meta.choices]
        if unknown:
            raise ConfigError(f"{key} has unknown values: {unknown}")
        return [str(item) for item in items]
    if meta.type == "str":
        return "" if value is None else str(value)
    if meta.type == "json":
        return _validate_json_setting(key, value)
    return value


def _validate_json_setting(key: str, value: Any) -> Any:
    if key == "price.static_schedule":
        return _validate_static_schedule(value)
    if key == "solar.planes":
        return _validate_planes(value)
    if key == "solar.sources":
        return _validate_solar_sources(value)
    if key == "load.devices":
        return _validate_load_devices(value)
    if key == "batteries":
        if not isinstance(value, list):
            raise ConfigError("batteries must be a list")
        return value
    return value


def _validate_static_schedule(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ConfigError("price.static_schedule must be a list")
    out: list[dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            raise ConfigError("each schedule entry must be an object")
        try:
            price = float(entry["price_eur_per_kwh"])
            start = int(entry.get("from_hour", 0))
            end = int(entry.get("to_hour", 24))
        except (KeyError, TypeError, ValueError):
            raise ConfigError(
                "schedule entries need price_eur_per_kwh, from_hour and to_hour"
            ) from None
        if not 0 <= start <= 24 or not 0 <= end <= 24:
            raise ConfigError("schedule hours must be within 0..24")
        days = entry.get("days")
        if days is None:
            day_list: list[int] = []
        else:
            day_list = _coerce_days(days)
        out.append(
            {
                "from_hour": start,
                "to_hour": end,
                "price_eur_per_kwh": price,
                "days": day_list,
            }
        )
    return out


def _coerce_days(days: Any) -> list[int]:
    names = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    if not isinstance(days, (list, tuple)):
        raise ConfigError("days must be a list")
    out: list[int] = []
    for day in days:
        if isinstance(day, bool):
            raise ConfigError(f"invalid day: {day!r}")
        if isinstance(day, int):
            if not 0 <= day <= 6:
                raise ConfigError(f"invalid day: {day}")
            out.append(day)
            continue
        text = str(day).strip().lower()[:3]
        if text not in names:
            raise ConfigError(f"invalid day: {day!r}")
        out.append(names.index(text))
    return out


def _validate_planes(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ConfigError("solar.planes must be a list")
    out: list[dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            raise ConfigError("each plane must be an object")
        try:
            kwp = float(entry["kwp"])
            tilt = float(entry.get("tilt_deg", 35))
            azimuth = float(entry.get("azimuth_deg", 0))
        except (KeyError, TypeError, ValueError):
            raise ConfigError("planes need kwp, tilt_deg and azimuth_deg") from None
        if kwp <= 0:
            raise ConfigError("plane kwp must be > 0")
        if not 0 <= tilt <= 90:
            raise ConfigError("plane tilt_deg must be within 0..90")
        if not 0 <= azimuth <= 360:
            raise ConfigError(
                "plane azimuth_deg is a compass bearing: 0 = north, 90 = east, 180 = south, 270 = west"
            )
        plane: dict[str, Any] = {"kwp": kwp, "tilt_deg": tilt, "azimuth_deg": azimuth}
        damping = entry.get("damping")
        if damping is not None:
            plane["damping"] = damping
        out.append(plane)
    return out


def _validate_load_devices(value: Any) -> list[dict[str, Any]]:
    """Validate ``load.devices`` (SPEC.md section 7.2): named loads, each with its own meter.

    Every shape this refuses is a silent double-count downstream: the same meter twice, or the house
    meter as one of its own devices, subtracts that meter more than once from the total and then
    adds it back more than once, and the error reads as a steeper temperature slope rather than as a
    mistake. The kind is checked against the closed set, because the kind is what decides whether
    the device gets a temperature term -- an unknown kind would silently get none.
    """
    if not isinstance(value, list):
        raise ConfigError("load.devices must be a list")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(value):
        where = f"load.devices[{index}]"
        if not isinstance(entry, Mapping):
            raise ConfigError(f"{where} must be an object")
        unknown = sorted(set(entry) - set(_LOAD_DEVICE_KEYS))
        if unknown:
            raise ConfigError(f"{where} has unknown keys {unknown}; allowed: {list(_LOAD_DEVICE_KEYS)}")
        entity = str(entry.get("entity", "") or "").strip()
        if not entity:
            raise ConfigError(f"{where}.entity is required: the device's power or energy meter")
        if any(char.isspace() for char in entity):
            raise ConfigError(
                f"{where}.entity {entity!r} contains whitespace; it must be a Home Assistant entity "
                "id or statistic id"
            )
        if entity in seen:
            raise ConfigError(
                f"{where}.entity {entity!r} is listed twice; one meter cannot be two devices"
            )
        seen.add(entity)
        kind = str(entry.get("kind", "") or "").strip().lower()
        if kind not in LOAD_DEVICE_KINDS:
            raise ConfigError(f"{where}.kind must be one of {list(LOAD_DEVICE_KINDS)}")
        item: dict[str, Any] = {"entity": entity, "kind": kind}
        name = str(entry.get("name", "") or "").strip()
        if name:
            item["name"] = name
        out.append(item)
    return out


def load_device_patch_errors(
    values: Mapping[str, Any], current: Mapping[str, Any]
) -> dict[str, str]:
    """Cross-key check for ``load.devices``: no device meter may be the house meter itself.

    ``load.entity`` is the total the device meters are subtracted from, so listing it as one of its
    own devices subtracts the whole house from itself: the base profile would be learned from zeros
    and the device's profile added back on top, producing a forecast that looks like a single
    appliance and is wrong everywhere. Judged on the patch's *final* state so moving both keys in
    one save is accepted or refused as a whole, the same rule the protection trio follows.
    """
    touched = set(values) & {"load.devices", "load.entity"}
    if not touched:
        return {}
    merged = dict(current)
    merged.update(values)
    house = str(merged.get("load.entity", "") or "").strip()
    raw = merged.get("load.devices")
    if not house or not isinstance(raw, list):
        return {}
    clash = any(
        isinstance(entry, Mapping) and str(entry.get("entity", "") or "").strip() == house
        for entry in raw
    )
    if not clash:
        return {}
    message = (
        f"load.entity {house!r} is also listed in load.devices; a device meter cannot be the house "
        "total it is subtracted from"
    )
    if "load.devices" in touched:
        return {"load.devices": message}
    return {"load.entity": message}


def _validate_solar_sources(value: Any) -> list[dict[str, Any]]:
    """Validate ``solar.sources`` (SPEC.md section 7.1.3): an ordered list of merge entries.

    A malformed entry fails here with a message naming the entry, so ``PATCH /api/v1/config``
    returns 400 and the engine never has to guess. Provider-specific keys are validated against
    their own setting type (an unknown key is an error, not silently dropped).
    """
    if not isinstance(value, list):
        raise ConfigError("solar.sources must be a list")
    out: list[dict[str, Any]] = []
    for index, entry in enumerate(value):
        where = f"solar.sources[{index}]"
        if not isinstance(entry, Mapping):
            raise ConfigError(f"{where} must be an object")
        source = str(entry.get("source", "") or "").strip().lower()
        if source not in _SOLAR_MERGE_SOURCE_CHOICES:
            raise ConfigError(
                f"{where}.source must be one of {list(_SOLAR_MERGE_SOURCE_CHOICES)}"
            )
        weight = entry.get("weight", 1.0)
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise ConfigError(f"{where}.weight must be a number")
        weight = float(weight)
        if not math.isfinite(weight) or weight <= 0.0:
            raise ConfigError(f"{where}.weight must be a finite number > 0")
        item: dict[str, Any] = {"source": source, "weight": weight}
        name = entry.get("name")
        if name is not None:
            item["name"] = str(name).strip()
        allowed = _SOLAR_SOURCE_KEYS[source]
        for key, raw in entry.items():
            if key in ("source", "weight", "name"):
                continue
            if key not in allowed:
                raise ConfigError(
                    f"{where} has unknown key {key!r} for source {source!r}; allowed: {list(allowed)}"
                )
            item[key] = _coerce_solar_source_key(f"{where}.{key}", key, raw)
        out.append(item)
    return out


def _coerce_solar_source_key(where: str, key: str, raw: Any) -> Any:
    """Validate one provider-specific merge-entry key using the matching setting's rules."""
    if key == "planes":
        try:
            return _validate_planes(raw)
        except ConfigError as err:
            raise ConfigError(f"{where}: {err}") from None
    if key in ("api_key", "entry_id", "weather_entity"):
        return "" if raw is None else str(raw)
    if key == "nasa_irradiance":
        if not isinstance(raw, bool):
            raise ConfigError(f"{where} must be a boolean")
        return raw
    meta = SETTINGS_META.get(f"solar.{key}")
    if meta is None:
        return raw
    try:
        return coerce_setting(f"solar.{key}", raw)
    except ConfigError as err:
        raise ConfigError(f"{where}: {err}") from None


# --------------------------------------------------------------------------------------------
# UI descriptors
# --------------------------------------------------------------------------------------------


def setting_descriptors(settings: Settings | None = None) -> list[dict[str, Any]]:
    """Flat, JSON-safe metadata for every settings key (SPEC.md section 10 GET /api/v1/config)."""
    out: list[dict[str, Any]] = []
    for key in DEFAULTS:
        meta = SETTINGS_META.get(key)
        payload: dict[str, Any] = {
            "key": key,
            "default": DEFAULTS[key],
            "value": settings.get(key) if settings is not None else DEFAULTS[key],
        }
        if meta is not None:
            payload.update(meta.as_payload())
        else:  # pragma: no cover - every DEFAULTS key has metadata
            payload.update({"type": "str", "group": "general", "label": key, "help": ""})
        out.append(payload)
    return out
