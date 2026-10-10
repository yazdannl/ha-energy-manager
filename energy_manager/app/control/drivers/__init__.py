"""Driver registry (SPEC sections 5 and 9.2).

``create`` refuses an unusable mapping immediately (SPEC 5: "rejected at save time with a clear
message, not at dispatch time") and ``validate_config`` is what ``POST /api/v1/batteries`` calls
before anything is persisted.

Entity *discovery* is not implemented here: ``app.haws.discover_candidates`` is the single
registry-scoring helper for every kind (SPEC 10), so a driver only maps its role slugs onto
mapping keys (``ROLE_TO_MAPPING``) instead of duplicating the ranking logic.
"""

from __future__ import annotations

from typing import Any, Iterable

from ...models import BatteryConfig
from .base import (
    BaseDriver,
    BatteryDriver,
    DriverError,
    HAFacade,
    default_tolerance,
    parse_energy_kwh,
    parse_number,
    parse_soc_pct,
)
from .generic import GenericDriver
from .zendure import ZendureDriver, resolve_mapping

DRIVERS: dict[str, type[BaseDriver]] = {
    GenericDriver.driver_name: GenericDriver,
    ZendureDriver.driver_name: ZendureDriver,
}

#: ``haws.discover_candidates`` role slug -> battery mapping key, per driver.
ROLE_TO_MAPPING: dict[str, dict[str, str]] = {
    "generic": {
        "power_setpoint": "power_setpoint_entity",
        "charge_limit": "charge_power_entity",
        "discharge_limit": "discharge_power_entity",
        "soc": "soc_entity",
        "capacity": "capacity_entity",
    },
    "zendure": {
        "power_setpoint": "power_setpoint_entity",
        "charge_limit": "charge_limit_entity",
        "discharge_limit": "discharge_limit_entity",
        "soc": "soc_entity",
        "capacity": "capacity_entity",
        "pv_power": "pv_power_entity",
        "output_power": "output_power_entity",
        # The manager's manual control select: the driver needs `operation = manual` before it may
        # command power (appendix A), and `ac_mode` is per-device direction it never writes.
        "manual_mode": "operation_entity",
        "min_soc": "min_soc_entity",
        "soc_target": "soc_target_entity",
    },
}


def names() -> list[str]:
    return sorted(DRIVERS)


def driver_class(driver_name: str) -> type[BaseDriver] | None:
    return DRIVERS.get(driver_name)


def role_to_mapping(driver_name: str, role: str) -> str | None:
    """Translate a discovery role slug into this driver's mapping key."""
    return ROLE_TO_MAPPING.get(driver_name, {}).get(role)


def validate_config(config: BatteryConfig) -> list[str]:
    """All problems that make this battery unusable, empty when it is fine."""
    problems = list(config.validate())
    cls = DRIVERS.get(config.driver)
    if cls is None:
        problems.append(f"unknown driver: {config.driver!r} (have {', '.join(names())})")
    else:
        problems.extend(cls.validate_mapping(config.mapping))
    return problems


def create(
    config: BatteryConfig,
    ha: HAFacade,
    *,
    tolerance_w: float | None = None,
    clock: Any = None,
    sleep: Any = None,
) -> BatteryDriver:
    """Instantiate a driver, raising ``ValueError`` for an unusable driver/mapping."""
    cls = DRIVERS.get(config.driver)
    if cls is None:
        raise ValueError(f"unknown driver: {config.driver!r} (have {', '.join(names())})")
    problems = cls.validate_mapping(config.mapping)
    if problems:
        raise ValueError("; ".join(problems))
    if tolerance_w is not None:
        tolerance = float(tolerance_w)
    else:
        tolerance = float(getattr(cls, "default_tolerance_w", None) or default_tolerance(config))
    kwargs: dict[str, Any] = {"tolerance_w": tolerance}
    if clock is not None:
        kwargs["clock"] = clock
    if sleep is not None:
        kwargs["sleep"] = sleep
    return cls(config, ha, **kwargs)


__all__ = [
    "BaseDriver",
    "BatteryDriver",
    "DriverError",
    "GenericDriver",
    "HAFacade",
    "ROLE_TO_MAPPING",
    "ZendureDriver",
    "create",
    "driver_class",
    "names",
    "parse_energy_kwh",
    "parse_number",
    "parse_soc_pct",
    "resolve_mapping",
    "role_to_mapping",
    "validate_config",
]
