"""Zendure (SolarFlow) driver (SPEC sections 5 and 9.2, appendix A).

Verified facts this driver is built on (docs/EXTERNAL_APIS.md section 6):

* The control surface is the **manager**: ``select.<manager>_operation`` must read ``manual`` and
  ``number.<manager>_manual_power`` carries the setpoint **system-wide**.  ``manual_power`` uses the
  opposite sign convention to ``models.py``: negative = charge, positive = discharge.  Every write
  and every read-back is therefore negated here.
* Nothing is dispatched by the write itself: the manager applies the setpoint when it processes the
  next P1 event (>=2.2 s later), so read-back verification runs inside a bounded retry window
  (``BaseDriver._verify_writes``) and a mismatch after that window is a driver error, never a
  silent success.
* The integration's smart modes overwrite external writes, so ``operation`` is asserted to be
  ``manual`` before every power write and re-asserted (and re-verified) when it has drifted.
* ``electric_level`` is the authoritative SoC (``soc_status`` is calibration bookkeeping and is
  never read); ``available_kwh`` is usable energy above ``min_soc`` and ``total_kwh`` the installed
  capacity.
* Per-device ``input_limit``/``output_limit`` writes fight the manager, so they are only used when
  no manager entity is mapped at all.
* Discovery keys on ``platform`` + ``translation_key``; Zendure unique ids are not domain-prefixed,
  so they are never used to identify an entity.
* There is no device watchdog: the manager keeps re-applying the last setpoint, which is why the
  engine writes ``hold()`` on shutdown (``control/drivers/base.py`` cannot do that by itself).
"""

from __future__ import annotations

import time
from typing import Any, Iterable

from .base import (
    WRITABLE_NUMBER_DOMAINS,
    WRITABLE_SELECT_DOMAINS,
    BaseDriver,
    DriverError,
    _Write,
    is_entity_id,
    parse_energy_kwh,
    parse_soc_pct,
)

ZENDURE_PLATFORM = "zendure_ha"

#: integration translation key -> driver mapping key.  Registry rows are matched on
#: ``platform == zendure_ha`` plus this translation key (never on a unique-id prefix).
TRANSLATION_TO_MAPPING: dict[str, str] = {
    "manual_power": "manual_power_entity",
    "operation": "operation_entity",
    "input_limit": "charge_limit_entity",
    "output_limit": "discharge_limit_entity",
    "electric_level": "soc_entity",
    "solar_input_power": "pv_power_entity",
    "output_home_power": "output_power_entity",
    "grid_input_power": "grid_input_power_entity",
    "available_kwh": "available_kwh_entity",
    "total_kwh": "capacity_entity",
    "soc_set": "soc_target_entity",
    "min_soc": "min_soc_entity",
    "ac_mode": "ac_mode_entity",
    "connection": "connection_entity",
}

#: mapping keys this driver can resolve from the registry, for the mapping wizard.
MAPPING_ROLES = tuple(TRANSLATION_TO_MAPPING.values())

#: value the manager's ``operation`` select must have before we command power.
DEFAULT_OPERATION_VALUE = "manual"

#: Manager power entities are the *opposite* sign of models.py (negative = charge).
MANAGER_SIGN = -1.0

#: The integration itself skips a write when the reported power is within 5 W (docs section 6).
ZENDURE_TOLERANCE_W = 5.0

#: manager-level roles (system-wide, shared by every device of the manager).
MANAGER_ROLES: dict[str, str] = {"manual_power": "manual_power_entity", "operation": "operation_entity"}


def is_zendure_entry(entry: dict[str, Any]) -> bool:
    """True for entity-registry rows belonging to the Zendure integration (SPEC section 5).

    Zendure ``unique_id``s are **not** prefixed with the integration domain, so only the platform
    (and, for a specific role, the translation key) identifies an entity here.
    """
    return str(entry.get("platform") or "") == ZENDURE_PLATFORM


def resolve_mapping(
    entries: Iterable[dict[str, Any]],
    device_id: str | None = None,
    anchor_entity: str | None = None,
) -> dict[str, str]:
    """Resolve mapping keys from registry entries; ``{}`` when nothing matches (never guess).

    Manager-level roles (``manual_power``, ``operation``) are system-wide.  Device-scoped roles are
    taken from one device: ``device_id`` when given, else the device that owns ``anchor_entity``
    (typically the already-confirmed ``soc_entity`` of that battery), else the lowest device id --
    deterministic, and it never mixes two SolarFlows into one battery mapping.
    """
    candidates = [entry for entry in entries if is_zendure_entry(entry)]
    by_device: dict[str, list[dict[str, Any]]] = {}
    for entry in candidates:
        by_device.setdefault(str(entry.get("device_id") or ""), []).append(entry)
    if device_id is None and anchor_entity:
        for entry in candidates:
            if str(entry.get("entity_id") or "") == str(anchor_entity):
                device_id = str(entry.get("device_id") or "") or None
                break
    if device_id is None:
        device_id = next((key for key in sorted(by_device) if key), None)
    scoped = by_device.get(device_id or "", [])
    resolved: dict[str, str] = {}
    for entry in sorted(scoped, key=lambda e: str(e.get("entity_id") or "")):
        key = TRANSLATION_TO_MAPPING.get(str(entry.get("translation_key") or ""))
        entity_id = entry.get("entity_id")
        if key and is_entity_id(entity_id) and key not in resolved:
            resolved[key] = str(entity_id)
    for entry in sorted(candidates, key=lambda e: str(e.get("entity_id") or "")):
        key = MANAGER_ROLES.get(str(entry.get("translation_key") or ""))
        entity_id = entry.get("entity_id")
        if key and is_entity_id(entity_id) and key not in resolved:
            resolved[key] = str(entity_id)
    return resolved


class ZendureDriver(BaseDriver):
    """Zendure SolarFlow battery behind the ``zendure_ha`` integration."""

    driver_name = "zendure"
    default_tolerance_w = ZENDURE_TOLERANCE_W

    @classmethod
    def validate_mapping(cls, mapping: dict[str, str]) -> list[str]:
        problems: list[str] = []
        manager = mapping.get("manual_power_entity")
        setpoint = mapping.get("power_setpoint_entity")
        charge = mapping.get("charge_limit_entity")
        discharge = mapping.get("discharge_limit_entity")
        if not manager and not setpoint and not (charge and discharge):
            problems.append(
                "mapping: zendure needs manual_power_entity (manager), power_setpoint_entity or "
                "both charge_limit_entity (input_limit) and discharge_limit_entity (output_limit)"
            )
        for key in ("manual_power_entity", "power_setpoint_entity", "charge_limit_entity", "discharge_limit_entity"):
            entity_id = mapping.get(key)
            if not entity_id:
                continue
            if not is_entity_id(entity_id):
                problems.append(f"mapping: {key} is not an entity id")
            elif entity_id.split(".", 1)[0] not in WRITABLE_NUMBER_DOMAINS:
                problems.append(f"mapping: {key} must be a number entity")
        operation = mapping.get("operation_entity")
        if operation:
            if not is_entity_id(operation):
                problems.append("mapping: operation_entity is not an entity id")
            elif operation.split(".", 1)[0] not in WRITABLE_SELECT_DOMAINS:
                problems.append("mapping: operation_entity must be a select entity")
        elif manager:
            # The manager path only works when we can put it into manual dispatch.
            problems.append(
                "mapping: manual_power_entity needs operation_entity so the manager can be set to "
                f"{mapping.get('operation_manual_value') or DEFAULT_OPERATION_VALUE!r}"
            )
        for key in (
            "soc_entity",
            "capacity_entity",
            "available_kwh_entity",
            "output_power_entity",
            "grid_input_power_entity",
        ):
            entity_id = mapping.get(key)
            if entity_id and not is_entity_id(entity_id):
                problems.append(f"mapping: {key} is not an entity id")
        return problems

    def effective_mapping(self) -> dict[str, str]:
        """The mapping in use.  The engine merges registry-resolved roles in before construction."""
        return {key: value for key, value in self.config.mapping.items() if value}

    def capabilities(self) -> dict[str, Any]:
        caps = super().capabilities()
        mapping = self.effective_mapping()
        caps.update(
            {
                "manager": "zendure_ha",
                "control_path": self._control_path(),
                "operation_entity": mapping.get("operation_entity"),
                "operation_value": mapping.get("operation_manual_value") or DEFAULT_OPERATION_VALUE,
                # Documented in the payload so the UI can explain a negated raw number.
                "raw_sign_convention": "positive = discharge, negative = charge (negated by the driver)",
                "soc_source": "electric_level" if mapping.get("soc_entity") else None,
                "soc_limits_writable": False,
                "measured_readback": bool(self._measured_entity()),
            }
        )
        return caps

    # -- mapping helpers ---------------------------------------------------------------

    def _control_path(self) -> str:
        mapping = self.effective_mapping()
        if mapping.get("manual_power_entity") and self._operation_entity():
            return "manager_manual_power"
        if mapping.get("power_setpoint_entity"):
            return "device_setpoint"
        if mapping.get("charge_limit_entity") and mapping.get("discharge_limit_entity"):
            return "device_limits"
        return "unresolved"

    def _operation_entity(self) -> str | None:
        entity = self.effective_mapping().get("operation_entity")
        return entity if is_entity_id(entity) else None

    def _operation_value(self) -> str:
        value = self.effective_mapping().get("operation_manual_value")
        return str(value) if value else DEFAULT_OPERATION_VALUE

    def _measured_entity(self) -> str | None:
        """The entity that shows what the battery is *actually* doing on the AC side."""
        mapping = self.effective_mapping()
        for key in ("output_power_entity", "grid_input_power_entity"):
            entity = mapping.get(key)
            if is_entity_id(entity):
                return str(entity)
        return None

    def readback_entities(self) -> list[str]:
        """Entities the verification reads back (measured pair first, then the setpoint)."""
        mapping = self.effective_mapping()
        entities = [
            str(mapping[key])
            for key in ("grid_input_power_entity", "output_power_entity", "manual_power_entity")
            if is_entity_id(mapping.get(key))
        ]
        return entities

    # -- reads ------------------------------------------------------------------------

    def _measured_pair(self) -> tuple[float | None, float | None]:
        """The measured AC entities: ``(grid_input, output_home)`` in W, either may be None.

        Both sides are required: treating an unavailable side as 0 W made an idle battery read as
        800 W of charging (and a real discharge read as 0 W), which then drove the ramp clamp, the
        SoC clamp and grid-follow (audit finding 4).  The caller falls back to the commanded
        setpoint when the pair is incomplete.
        """
        mapping = self.effective_mapping()
        drawn = None
        output = None
        if is_entity_id(mapping.get("grid_input_power_entity")):
            drawn = self._number(mapping["grid_input_power_entity"])
        if is_entity_id(mapping.get("output_power_entity")):
            output = self._number(mapping["output_power_entity"])
        return drawn, output

    def _setpoint_entity(self) -> str | None:
        """The entity a write actually commands (manager manual_power, else the device setpoint)."""
        mapping = self.effective_mapping()
        if self._control_path() == "manager_manual_power":
            entity = mapping.get("manual_power_entity")
        else:
            entity = mapping.get("power_setpoint_entity")
        return str(entity) if is_entity_id(entity) else None

    def _commanded_power(self) -> float | None:
        """Power the integration was last *told* to apply, in models.py sign convention."""
        entity = self._setpoint_entity()
        if entity is None:
            return None
        value = self._number(entity)
        if value is None:
            return None
        return MANAGER_SIGN * value if self._control_path() == "manager_manual_power" else value

    def _idempotent_power(self) -> float | None:
        """Audit finding 1: never decide from measured flow on the manager path.

        The manager applies a manual setpoint only on the next P1 event, so ``grid_input``/
        ``output_home`` still show the *previous* command (often 0/0) right after a real write.
        Comparing the commanded setpoint is what makes an "already there" skip truthful; the
        measured value is only a fallback when the setpoint cannot be read.
        """
        commanded = self._commanded_power()
        return commanded if commanded is not None else self._read_power()

    def _read_power(self) -> float | None:
        """Battery AC power in models.py convention (positive = charging).

        The *measured* entities win over the setpoint: ``manual_power`` only proves what we wrote,
        while ``output_home_power``/``grid_input_power`` prove what the battery is doing (which is
        also what the integration itself compares against, docs section 6).
        """
        mapping = self.effective_mapping()
        drawn, output = self._measured_pair()
        if drawn is not None and output is not None:
            # output_home_power = discharging, grid_input_power = charging (same direction as ours).
            return drawn - output
        manual = mapping.get("manual_power_entity")
        if is_entity_id(manual):
            raw = self._number(manual)
            if raw is not None:
                return MANAGER_SIGN * raw  # negative raw = charge
        setpoint = mapping.get("power_setpoint_entity")
        if is_entity_id(setpoint):
            value = self._number(setpoint)
            if value is not None:
                return value
        charge = self._number(mapping.get("charge_limit_entity"))
        discharge = self._number(mapping.get("discharge_limit_entity"))
        if charge is None and discharge is None:
            return None
        # input_limit/output_limit are magnitudes: charge adds, discharge subtracts.
        return abs(charge or 0.0) - abs(discharge or 0.0)

    def _read_soc(self) -> float | None:
        entity_id = self.effective_mapping().get("soc_entity")
        state = self._state(entity_id)
        if state is None:
            return None
        return parse_soc_pct(state.get("state"), state.get("attributes"))

    def _read_capacity(self) -> float | None:
        entity_id = self.effective_mapping().get("capacity_entity")
        state = self._state(entity_id)
        if state is None:
            return None
        return parse_energy_kwh(state.get("state"), state.get("attributes"))

    def _min_soc_pct(self) -> float:
        """Device SoC floor in percent: the device's own limit when readable, else the config."""
        mapping = self.effective_mapping()
        if is_entity_id(mapping.get("min_soc_entity")):
            value = self._number(mapping["min_soc_entity"])
            if value is not None:
                return max(0.0, min(100.0, value))
        return max(0.0, float(self.config.min_soc_pct))

    def _read_energy(self, soc: float | None, capacity: float | None) -> float | None:
        """``available_kwh`` is energy *above* min_soc (appendix A): convert it to absolute.

        Returning it raw applied the floor twice and made the plan's SoC disagree with the battery
        card's ``electric_level`` (audit finding 9).
        """
        entity_id = self.effective_mapping().get("available_kwh_entity")
        state = self._state(entity_id)
        if state is not None:
            usable = parse_energy_kwh(state.get("state"), state.get("attributes"))
            if usable is not None:
                cap = float(capacity or self.config.capacity_kwh or 0.0)
                return max(0.0, cap * self._min_soc_pct() / 100.0 + usable)
        return super()._read_energy(soc, capacity)

    def _read_pv_power(self) -> float | None:
        return self._number(self.effective_mapping().get("pv_power_entity"))

    # -- writes -----------------------------------------------------------------------

    def _plan_writes(self, target_w: float) -> list[_Write]:
        mapping = self.effective_mapping()
        write_path = self._control_path()
        if write_path == "unresolved":
            raise DriverError("zendure mapping has no usable power entity")
        writes: list[_Write] = []
        if write_path == "manager_manual_power":
            # (a) assert manual dispatch when it is not already there, (b) then the
            # (sign-inverted, system-wide) setpoint.  _verify_writes re-asserts if a smart mode
            # grabs the manager during the write.
            if not self._manual_is_asserted():
                writes.append(self._manual_write())
            raw = MANAGER_SIGN * target_w
            writes.append(self.make_write(mapping["manual_power_entity"], 0.0 if raw == 0 else raw))
            return writes
        if write_path == "device_setpoint":
            writes.append(self.make_write(mapping["power_setpoint_entity"], target_w))
            return writes
        # Fallback for a SolarFlow without the manager: magnitudes on the device limits.
        writes.append(self.make_write(mapping["charge_limit_entity"], max(0.0, target_w)))
        writes.append(self.make_write(mapping["discharge_limit_entity"], max(0.0, -target_w)))
        return writes

    def _manual_write(self) -> _Write:
        """The select write that puts the manager into manual dispatch."""
        entity = self._operation_entity()
        assert entity is not None  # validate_mapping guarantees this for the manager path
        return self.make_write(entity, self._operation_value())

    def _manual_is_asserted(self) -> bool:
        entity = self._operation_entity()
        if entity is None:
            return True
        state = self._state(entity)
        return str((state or {}).get("state") or "").strip().lower() == self._operation_value().lower()

    def _verify_writes(self, target_w: float) -> tuple[float | None, str | None]:
        """Verify the *commanded setpoint* and that manual dispatch survived.

        The measured pair lags by one P1 event, so it can neither confirm nor refute a fresh
        write: it is only consulted as a secondary signal for non-zero targets (audit finding 1).
        """
        if self._control_path() == "manager_manual_power":
            return self._verify_manager_write(target_w)
        readback, problem = super()._verify_writes(target_w)
        if problem is not None:
            return readback, problem
        entity = self._operation_entity()
        if entity is None:
            return readback, None
        if self._manual_is_asserted():
            return readback, None
        # A smart mode took over during the write: re-assert once and re-check.
        try:
            self._perform(self._manual_write())
        except Exception as exc:
            return readback, f"could not re-assert manual operation: {str(exc)[:120]}"
        if self._manual_is_asserted():
            return readback, None
        return readback, (
            f"manager operation is not {self._operation_value()!r} after re-assert; "
            "refusing to report success"
        )

    def _verify_manager_write(self, target_w: float) -> tuple[float | None, str | None]:
        """Verify the command without ever reporting a false success or a false failure.

        * A **hold** (``target ~ 0``) is proven by the *command*: the measured pair can still show
          the previous command until the next P1 event, which is exactly the trap that made a hold
          a silent no-op (audit finding 1).  The measured pair is only a fallback.
        * A **non-zero** command is proven by the *flow*: the setpoint entity matching only shows
          that the write landed in Home Assistant, never that the device followed it.
        Manual dispatch must survive either way (a smart mode can grab the manager back).
        """
        if abs(target_w) <= self.tolerance_w:
            readback, problem = self._verify_against(
                target_w, self._commanded_power, "setpoint read-back"
            )
            if problem is not None:
                readback, problem = self._verify_against(
                    target_w, self._read_power, "measured read-back"
                )
        else:
            readback, problem = super()._verify_writes(target_w)
        if problem is not None:
            return readback, problem
        return self._verify_operation(readback)

    def _verify_operation(self, readback: float | None) -> tuple[float | None, str | None]:
        """Manual dispatch must survive the write (a smart mode can grab the manager)."""
        entity = self._operation_entity()
        if entity is None or self._manual_is_asserted():
            return readback, None
        # A smart mode took over during the write: re-assert once and re-check.
        try:
            self._perform(self._manual_write())
        except Exception as exc:
            return readback, f"could not re-assert manual operation: {str(exc)[:120]}"
        if self._manual_is_asserted():
            return readback, None
        return readback, (
            f"manager operation is not {self._operation_value()!r} after re-assert; "
            "refusing to report success"
        )

    def soc_limits(self) -> dict[str, float | None]:
        """Read-only view of the device SoC limits (never written by the control loop)."""
        mapping = self.effective_mapping()
        return {
            "soc_target_pct": self._number(mapping.get("soc_target_entity")),
            "min_soc_pct": self._number(mapping.get("min_soc_entity")),
            "soc_pct": self._read_soc(),
        }
