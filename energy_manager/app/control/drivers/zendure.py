"""Zendure (SolarFlow) driver (SPEC sections 5 and 9.2, appendix A).

Verified facts this driver is built on (docs/EXTERNAL_APIS.md section 6):

* The control surface is the **manager**: ``select.<manager>_operation`` must read ``manual`` and
  ``number.<manager>_manual_power`` carries the setpoint **system-wide**.  ``manual_power`` uses the
  opposite sign convention to ``models.py``: negative = charge, positive = discharge.  Every write
  and every read-back is therefore negated here.
* ``control_path = "device"`` bypasses the manager instead (same driver, per-device entities):
  the ``ac_mode`` select picks the direction, the ``input_limit``/``output_limit`` numbers carry
  the magnitudes, and the manager is put in ``off`` so it cannot overwrite them on its next P1 event.
  That path does not wait for a P1 event at all -- it is what makes control possible on a house
  whose only grid feed reaches Home Assistant every few minutes.
* ``ZendureMqttDriver`` (``driver = "zendure_mqtt"``) is the same device path for a device bridged
  by Home Assistant's own MQTT integration: the bridge publishes the zenSDK properties as
  ``<serial>_<property>`` entities on the ``mqtt`` platform, so the roles are resolved from the
  property name instead of a registry translation key, and the device's own ``smartMode`` switch
  joins the plan (the ``zendure_ha`` integration sets it to 1 with every non-zero AC command).
* Nothing is dispatched by the write itself: the manager applies the setpoint when it processes the
  next P1 event (>=2.2 s later), so read-back verification runs inside a bounded retry window
  (``BaseDriver._verify_writes``).  A setpoint that landed but whose flow has not followed inside
  that window is reported as ``Reason.COMMANDED`` (the command is in force, the device has not
  reached it yet), never as a driver error: a driver error triggers the failsafe hold, which would
  cancel the very command the manager is about to apply.  Only a write that did not land at all, or
  a manager that cannot be held in ``manual``, is a driver error.
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

import logging
import time
from dataclasses import replace
from typing import Any, Iterable

from ...models import DispatchResult, Reason
from .base import (
    WRITABLE_NUMBER_DOMAINS,
    WRITABLE_SELECT_DOMAINS,
    WRITABLE_SWITCH_DOMAINS,
    BaseDriver,
    DriverError,
    _Write,
    is_entity_id,
    parse_energy_kwh,
    parse_soc_pct,
)

ZENDURE_PLATFORM = "zendure_ha"

#: Platform of Home Assistant's own MQTT integration: a Zendure device bridged by the local MQTT
#: feature publishes one entity per zenSDK property (docs/EXTERNAL_APIS.md section 6).
MQTT_PLATFORM = "mqtt"

#: zenSDK property (as the MQTT bridge spells it, camelCase) -> driver mapping key.  The bridge
#: names every entity ``<serial>_<property>`` and carries no ``translation_key``, which is why this
#: driver resolves roles from the property suffix instead of a registry translation key.
MQTT_PROPERTY_TO_MAPPING: dict[str, str] = {
    "inputlimit": "charge_limit_entity",
    "outputlimit": "discharge_limit_entity",
    "acmode": "ac_mode_entity",
    "electriclevel": "soc_entity",
    "outputhomepower": "output_power_entity",
    "gridinputpower": "grid_input_power_entity",
    "solarinputpower": "pv_power_entity",
    "socset": "soc_target_entity",
    "minsoc": "min_soc_entity",
    "smartmode": "smart_mode_entity",
}

#: mapping keys this driver resolves from the registry, for the mapping wizard.
MQTT_MAPPING_ROLES = tuple(MQTT_PROPERTY_TO_MAPPING.values())

log = logging.getLogger(__name__)

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

#: value the manager's ``operation`` select gets on the device path, so it stops overwriting the
#: inverter's own AC limits on every P1 event (its ``off`` mode also runs one ``power_off()``).
DEFAULT_OPERATION_OFF_VALUE = "off"

#: direction select options on the device path (``ac_mode``).  The integration localises the
#: option labels ("input" vs "AC Input Mode"), so the entity's own options win when it has them.
DEFAULT_AC_MODE_VALUES: dict[str, str] = {"input": "input", "output": "output"}

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


def is_zendure_mqtt_entry(entry: dict[str, Any]) -> bool:
    """True for entity-registry rows of a device bridged by Home Assistant's MQTT integration.

    The device publishes the zenSDK properties itself (local MQTT feature), so its rows carry no
    integration domain to match on and no ``translation_key`` -- the platform and the property name
    inside the entity id are all there is (docs/EXTERNAL_APIS.md section 6).
    """
    return str(entry.get("platform") or "") == MQTT_PLATFORM


def mqtt_property_of(entity_id: str) -> str | None:
    """The zenSDK property an MQTT bridge entity carries (``<serial>_inputlimit`` -> inputLimit)."""
    object_id = str(entity_id or "").split(".", 1)[-1].lower()
    for property_name in MQTT_PROPERTY_TO_MAPPING:
        if object_id.endswith(f"_{property_name}"):
            return property_name
    return None


def resolve_mqtt_mapping(
    entries: Iterable[dict[str, Any]],
    device_id: str | None = None,
    anchor_entity: str | None = None,
) -> dict[str, str]:
    """Resolve mapping keys from one MQTT bridge device's own property entities.

    Like :func:`resolve_mapping`, one device only (``device_id``, else the device that owns
    ``anchor_entity``, else the lowest device id) so two inverters never mix into one battery, and
    a property this driver has no mapping key for is simply left out.
    """
    candidates = [entry for entry in entries if is_zendure_mqtt_entry(entry)]
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
    resolved: dict[str, str] = {}
    for entry in sorted(by_device.get(device_id or "", []), key=lambda e: str(e.get("entity_id") or "")):
        entity_id = entry.get("entity_id")
        if not is_entity_id(entity_id):
            continue
        key = MQTT_PROPERTY_TO_MAPPING.get(mqtt_property_of(str(entity_id)) or "")
        if key and key not in resolved:
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

    @classmethod
    def validate_control_path(cls, config: BatteryConfig) -> list[str]:
        """Problems with the *chosen* write path (SPEC 5: refused at save time, never at dispatch).

        ``control_path`` is a preference, not a fallback: a battery that asks for the device path
        must have the device's own AC limit entities, and one that asks for the manager must have
        the manager pair -- silently dispatching through the other path would be a surprise.
        """
        path = str(getattr(config, "control_path", "auto") or "auto").strip().lower()
        mapping = {key: value for key, value in (config.mapping or {}).items() if value}
        problems: list[str] = []
        if path == "manager":
            if not mapping.get("manual_power_entity"):
                problems.append("mapping: control_path 'manager' needs manual_power_entity")
            if not mapping.get("operation_entity"):
                problems.append("mapping: control_path 'manager' needs operation_entity")
        elif path == "device":
            for key in ("charge_limit_entity", "discharge_limit_entity"):
                if not mapping.get(key):
                    problems.append(f"mapping: control_path 'device' needs {key}")
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
        """The write path in use (SPEC 9.2).

        ``config.control_path`` picks it explicitly: ``manager`` is the Zendure manager's
        system-wide ``manual_power`` (applied on its next P1 event, and the only path that can
        split power across a fuse group), ``device`` is the inverter's own direction select plus
        ``input_limit``/``output_limit`` (applied by the device itself, no P1 event involved).
        ``auto`` keeps the manager first for compatibility, then a device setpoint, then the
        limits.
        """
        preference = str(getattr(self.config, "control_path", "auto") or "auto").strip().lower()
        if preference == "device":
            return "device_limits" if self._device_limits_mapped() else "unresolved"
        if preference == "manager":
            return "manager_manual_power" if self._manager_mapped() else "unresolved"
        if self._manager_mapped():
            return "manager_manual_power"
        if self.effective_mapping().get("power_setpoint_entity"):
            return "device_setpoint"
        if self._device_limits_mapped():
            return "device_limits"
        return "unresolved"

    def _manager_mapped(self) -> bool:
        return bool(self.effective_mapping().get("manual_power_entity")) and self._operation_entity() is not None

    def _device_limits_mapped(self) -> bool:
        mapping = self.effective_mapping()
        return bool(mapping.get("charge_limit_entity") and mapping.get("discharge_limit_entity"))

    def _unresolved_message(self) -> str:
        preference = str(getattr(self.config, "control_path", "auto") or "auto").strip().lower()
        if preference == "device":
            return (
                "control_path 'device' needs charge_limit_entity (input_limit) and "
                "discharge_limit_entity (output_limit)"
            )
        if preference == "manager":
            return "control_path 'manager' needs manual_power_entity and operation_entity"
        return "zendure mapping has no usable power entity"

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
            raise DriverError(self._unresolved_message())
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
        # Device path (SPEC 9.2, appendix A): magnitudes on the inverter's own AC limits -- input
        # for charging, output for discharging -- with the direction as a separate select.  The
        # device applies these itself (local zenSDK/MQTT), so this path does not wait for the
        # manager's next P1 event.  Anything that would overwrite them is quieted first.
        writes.extend(self._quiet_writes(target_w))
        mode = self._ac_mode_write(target_w)
        if mode is not None:
            writes.append(mode)
        writes.append(self.make_write(mapping["charge_limit_entity"], max(0.0, target_w)))
        writes.append(self.make_write(mapping["discharge_limit_entity"], max(0.0, -target_w)))
        return writes

    def _quiet_writes(self, target_w: float) -> list[_Write]:
        """Writes that take another controller out of the loop before the AC limits."""
        quiet = self._manager_quiet_write()
        return [quiet] if quiet is not None else []

    def _manager_quiet_write(self) -> _Write | None:
        """Put the Zendure manager into ``off`` on the device path (None when it already is).

        The manager re-issues ``inputLimit``/``outputLimit`` (in manual mode) or its smart-mode
        setpoints on every P1 event, so a device-path battery has to keep it off -- and ``off``
        makes the manager run one ``power_off()``, which the next tick's limits write replaces.
        """
        entity = self._operation_entity()
        if entity is None:
            return None
        off = self._operation_off_value()
        if self._select_reads(entity, off):
            return None
        return self.make_write(entity, off)

    def _ac_mode_write(self, target_w: float) -> _Write | None:
        """Direction select for the device path: input while charging, output while discharging.

        A hold (``target ~ 0``) only zeroes the limits: flipping the direction there would add a
        pointless write (and the device is idle anyway).  The option string is taken from the
        entity's own ``options`` because the integration localises the labels.
        """
        mapping = self.effective_mapping()
        entity = mapping.get("ac_mode_entity")
        if not is_entity_id(entity) or abs(target_w) <= self.tolerance_w:
            return None
        wanted = "input" if target_w > 0 else "output"
        option = self._ac_mode_option(str(entity), wanted)
        if option is None:
            return None
        if self._select_reads(str(entity), option):
            return None
        return self.make_write(str(entity), option)

    def _ac_mode_option(self, entity_id: str, wanted: str) -> str | None:
        mapping = self.effective_mapping()
        override = mapping.get(f"ac_mode_{wanted}_value")
        if override:
            return str(override)
        state = self._state(entity_id)
        options = (state or {}).get("attributes") or {}
        for option in options.get("options") or []:
            if wanted in str(option).strip().lower():
                return str(option)
        # The entity published no options (or none matches).  Its current state may still be the
        # option the device uses -- returning it makes the caller skip an identical write instead
        # of guessing a string this select may reject.
        current = str((state or {}).get("state") or "").strip()
        if current and wanted in current.lower():
            return current
        return DEFAULT_AC_MODE_VALUES.get(wanted)

    def _operation_off_value(self) -> str:
        value = self.effective_mapping().get("operation_off_value")
        return str(value) if value else DEFAULT_OPERATION_OFF_VALUE

    def _select_reads(self, entity_id: str, value: str) -> bool:
        state = self._state(entity_id)
        current = str((state or {}).get("state") or "").strip().lower()
        return current == value.strip().lower()

    def _manual_write(self) -> _Write:
        """The select write that puts the manager into manual dispatch."""
        entity = self._operation_entity()
        assert entity is not None  # validate_mapping guarantees this for the manager path
        return self.make_write(entity, self._operation_value())

    def _manual_is_asserted(self) -> bool:
        entity = self._operation_entity()
        if entity is None:
            return True
        return self._select_reads(entity, self._operation_value())

    def _verify_writes(self, target_w: float) -> tuple[float | None, str | None]:
        """Verify the *commanded setpoint* and that manual dispatch survived.

        The measured pair lags by one P1 event, so it can neither confirm nor refute a fresh
        write: it is only consulted as a secondary signal for non-zero targets (audit finding 1).
        """
        path = self._control_path()
        if path == "manager_manual_power":
            return self._verify_manager_write(target_w)
        if path == "device_limits":
            return self._verify_device_limits(target_w)
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

    def _verify_device_limits(self, target_w: float) -> tuple[float | None, str | None]:
        """Verify the device path against the inverter's own reported limits, then the flow.

        Unlike the manager's ``manual_power`` (a request the manager applies on its next P1
        event), ``input_limit``/``output_limit`` are device properties: the integration writes
        them over the local zenSDK/MQTT link and the device reports them back, so a matching
        read-back is real evidence, not an echo of our own write.  Both proofs are retried inside
        the bounded window; a command that has not been echoed *yet* is pending
        (``Reason.COMMANDED``) instead of a driver error, because the failsafe hold a driver error
        triggers would cancel a command the device is about to apply.
        """
        limits, limit_problem = self._verify_against(
            target_w, self._read_limit_setpoint, "limit read-back"
        )
        if limit_problem is None:
            return limits, None
        flow, flow_problem = self._verify_against(target_w, self._read_power, "measured read-back")
        if flow_problem is None:
            return flow, None
        echo = self._read_limit_setpoint()
        self._flow_pending = (
            target_w,
            echo if echo is not None else flow,
            "the inverter reports its own AC limits within a few seconds",
        )
        return target_w, None

    def _read_limit_setpoint(self) -> float | None:
        """The inverter's AC limits as one signed setpoint (positive = charge, models.py sign)."""
        mapping = self.effective_mapping()
        charge = self._number(mapping.get("charge_limit_entity"))
        discharge = self._number(mapping.get("discharge_limit_entity"))
        if charge is None and discharge is None:
            return None
        return abs(charge or 0.0) - abs(discharge or 0.0)

    def _verify_manager_write(self, target_w: float) -> tuple[float | None, str | None]:
        """Verify the command without ever reporting a false success or a false failure.

        * A **hold** (``target ~ 0``) is proven by the *command*: the measured pair can still show
          the previous command until the next P1 event, which is exactly the trap that made a hold
          a silent no-op (audit finding 1).  The measured pair is only a fallback.
        * A **non-zero** command is proven by the *flow*: the setpoint entity matching only shows
          that the write landed in Home Assistant, never that the device followed it.
        * A non-zero command the device has **not reached yet** is not a failure: when the setpoint
          itself reads back, the result is marked pending (``_flow_pending`` -> the caller reports
          ``Reason.COMMANDED``) instead of ``DRIVER_ERROR``.  The manager applies manual power on
          its next P1 event, and a house P1 feed can be minutes away (live 2026-10-12: a 5-minute
          meter made every 1 kW charge a 4.6 s blip -- the failsafe hold revoked each command
          before the manager read it).  The plan execution view is what turns "commanded" into
          "the device did not follow", without ever fighting the device.
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
                commanded = self._read_commanded()
                if commanded is None or abs(commanded - target_w) > self.tolerance_w:
                    # The write never landed: a real driver error, and the failsafe hold is right.
                    shown = "no reading" if commanded is None else f"{commanded:.0f} W"
                    return readback, (
                        f"{problem}; the setpoint read-back is {shown}, so the write did not land"
                    )
                self._flow_pending = (
                    target_w,
                    readback,
                    "the manager applies manual power on its next P1 event",
                )
                readback, problem = commanded, None
        if problem is not None:
            return readback, problem
        return self._verify_operation(readback)

    def _read_commanded(self) -> float | None:
        """``_commanded_power`` without raising: verification must not turn a read error into a
        crash (the pending/error decision falls back to the measured mismatch)."""
        try:
            return self._commanded_power()
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("driver %s: setpoint read-back failed: %s", self.id(), exc)
            return None

    def _apply(
        self, power_w: float, *, dry_run: bool, allow_idempotent_skip: bool = True
    ) -> DispatchResult:
        """Report a command the device has not confirmed yet as ``Reason.COMMANDED``.

        ``applied_w`` stays the setpoint that is in force (that is what "applied" means for an
        asynchronous surface), while ``readback_w`` carries what the device is measuring/echoing,
        and the message says which of the two the user is looking at.
        """
        self._flow_pending = None
        result = super()._apply(
            power_w, dry_run=dry_run, allow_idempotent_skip=allow_idempotent_skip
        )
        pending = self._flow_pending
        if pending is None or not result.ok or result.dry_run:
            return result
        target_w, measured, hint = pending
        direction = "charge" if target_w > 0 else ("discharge" if target_w < 0 else "hold")
        measured_text = f"{measured:.0f} W" if measured is not None else "no reading"
        raw = f" (raw manual_power {MANAGER_SIGN * target_w:.0f} W)" if self._control_path() == "manager_manual_power" else ""
        return replace(
            result,
            code=Reason.COMMANDED,
            message=(
                f"commanded {direction}{raw} {abs(target_w):.0f} W; the device has not confirmed "
                f"it yet (it still reports {measured_text}) -- {hint}"
            ),
            readback_w=measured,
        )

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


class ZendureMqttDriver(ZendureDriver):
    """A Zendure inverter driven through Home Assistant's own MQTT integration (SPEC 5 and 9.2).

    The local MQTT feature makes the device publish its zenSDK properties itself, one entity each
    (``number.<serial>_inputlimit``, ``select.<serial>_acmode``, ``sensor.<serial>_electriclevel``...),
    so this driver needs no manager: it writes the AC direction select plus the two magnitude limits
    and lets the device report them back.  That is ``ZendureDriver``'s ``device`` control path with
    three differences:

    * roles are resolved from the property name in the entity id (the bridge sets no
      ``translation_key``), never from ``zendure_ha``,
    * the device's own ``smartMode`` switch is part of the plan: the ``zendure_ha`` integration sets
      it to 1 with every non-zero AC command and to 0 in ``power_off()``, and the device only acts
      on the AC limits while it is set,
    * ``control_path`` does not apply -- there is no manager to choose between.
    """

    driver_name = "zendure_mqtt"
    default_tolerance_w = ZENDURE_TOLERANCE_W

    #: keys that make the path usable at all (the rest are optional extras).
    required_keys: tuple[str, ...] = (
        "charge_limit_entity",
        "discharge_limit_entity",
        "ac_mode_entity",
    )

    @classmethod
    def validate_mapping(cls, mapping: dict[str, str]) -> list[str]:
        mapping = {key: value for key, value in (mapping or {}).items() if value}
        problems: list[str] = []
        if not (mapping.get("charge_limit_entity") and mapping.get("discharge_limit_entity")):
            problems.append(
                "mapping: zendure_mqtt needs charge_limit_entity (inputLimit) and "
                "discharge_limit_entity (outputLimit)"
            )
        if not mapping.get("ac_mode_entity"):
            problems.append(
                "mapping: zendure_mqtt needs ac_mode_entity (the AC mode select, Input/Output)"
            )
        for key in ("charge_limit_entity", "discharge_limit_entity", "soc_target_entity", "min_soc_entity"):
            entity_id = mapping.get(key)
            if entity_id and entity_id.split(".", 1)[0] not in WRITABLE_NUMBER_DOMAINS:
                problems.append(f"mapping: {key} must be a number entity")
        for key in ("ac_mode_entity", "operation_entity"):
            entity_id = mapping.get(key)
            if entity_id and entity_id.split(".", 1)[0] not in WRITABLE_SELECT_DOMAINS:
                problems.append(f"mapping: {key} must be a select entity")
        for key in ("smart_mode_entity",):
            entity_id = mapping.get(key)
            if entity_id and entity_id.split(".", 1)[0] not in WRITABLE_SWITCH_DOMAINS:
                problems.append(f"mapping: {key} must be a switch entity")
        for key in (
            "soc_entity",
            "output_power_entity",
            "grid_input_power_entity",
            "pv_power_entity",
            "capacity_entity",
        ):
            entity_id = mapping.get(key)
            if entity_id and not is_entity_id(entity_id):
                problems.append(f"mapping: {key} is not an entity id")
        return problems

    def capabilities(self) -> dict[str, Any]:
        caps = super().capabilities()
        mapping = self.effective_mapping()
        caps.update(
            {
                "manager": None,
                "platform": MQTT_PLATFORM,
                "control_path": self._control_path(),
                "smart_mode_entity": mapping.get("smart_mode_entity"),
                "device_properties": sorted(MQTT_PROPERTY_TO_MAPPING),
            }
        )
        return caps

    # -- mapping helpers ---------------------------------------------------------------

    def _control_path(self) -> str:
        """This driver has one path only: the inverter's own AC limits."""
        return "device_limits" if self._device_limits_mapped() else "unresolved"

    def _unresolved_message(self) -> str:
        return (
            "zendure_mqtt needs charge_limit_entity (inputLimit) and discharge_limit_entity "
            "(outputLimit)"
        )

    # -- writes -----------------------------------------------------------------------

    def _quiet_writes(self, target_w: float) -> list[_Write]:
        """Manager ``off`` (when mapped) and then the device's own smart-mode switch."""
        writes = super()._quiet_writes(target_w)
        smart = self._smart_mode_write(target_w)
        if smart is not None:
            writes.append(smart)
        return writes

    def _smart_mode_write(self, target_w: float) -> _Write | None:
        """The device's ``smartMode`` switch: on for a live setpoint, off for a hold.

        ``zendure_ha`` writes ``smartMode: 1`` with every non-zero AC command and 0 in
        ``power_off()`` (docs/EXTERNAL_APIS.md section 6), and the inverter only applies
        ``inputLimit``/``outputLimit`` while it is set.  Only a change is written, so an unchanged
        tick costs no MQTT write.
        """
        entity = self.effective_mapping().get("smart_mode_entity")
        if not is_entity_id(entity):
            return None
        wanted = abs(target_w) > self.tolerance_w
        current = str((self._state(entity) or {}).get("state") or "").strip().lower()
        if current in ("on", "true", "1") and wanted:
            return None
        if current in ("off", "false", "0") and not wanted:
            return None
        return self.make_write(str(entity), wanted)
