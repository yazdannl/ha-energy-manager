"""Entity-mapping driver for batteries that expose plain HA entities (SPEC sections 5 and 9.2).

Accepted mappings (validated at save time, never at dispatch time):

* ``power_setpoint_entity`` — one signed number entity (+ charge / - discharge), or
* ``charge_power_entity`` + ``discharge_power_entity`` — two unsigned number entities,
* optional ``mode_select_entity`` + ``mode_manual_value`` to switch the device to manual mode,
* optional ``soc_entity`` and ``capacity_entity`` for state reporting.
"""

from __future__ import annotations

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


class GenericDriver(BaseDriver):
    """Signed-setpoint or charge/discharge-pair battery."""

    driver_name = "generic"

    @classmethod
    def validate_mapping(cls, mapping: dict[str, str]) -> list[str]:
        problems: list[str] = []
        setpoint = mapping.get("power_setpoint_entity")
        charge = mapping.get("charge_power_entity")
        discharge = mapping.get("discharge_power_entity")

        if setpoint and (charge or discharge):
            problems.append(
                "mapping: use either power_setpoint_entity or the charge/discharge pair, not both"
            )
        if not setpoint and not (charge and discharge):
            problems.append(
                "mapping: a signed power_setpoint_entity or both charge_power_entity and "
                "discharge_power_entity are required"
            )
        for key in ("power_setpoint_entity", "charge_power_entity", "discharge_power_entity"):
            entity_id = mapping.get(key)
            if not entity_id:
                continue
            if not is_entity_id(entity_id):
                problems.append(f"mapping: {key} is not an entity id")
            elif entity_id.split(".", 1)[0] not in WRITABLE_NUMBER_DOMAINS:
                problems.append(
                    f"mapping: {key} must be a number or input_number entity, "
                    f"got {entity_id.split('.', 1)[0]!r}"
                )
        mode_entity = mapping.get("mode_select_entity")
        if mode_entity:
            if not is_entity_id(mode_entity):
                problems.append("mapping: mode_select_entity is not an entity id")
            elif mode_entity.split(".", 1)[0] not in WRITABLE_SELECT_DOMAINS:
                problems.append("mapping: mode_select_entity must be a select entity")
            if not mapping.get("mode_manual_value"):
                problems.append("mapping: mode_manual_value is required with mode_select_entity")
        for key in ("soc_entity", "capacity_entity"):
            entity_id = mapping.get(key)
            if entity_id and not is_entity_id(entity_id):
                problems.append(f"mapping: {key} is not an entity id")
        return problems

    # -- reads ------------------------------------------------------------------------

    def _read_power(self) -> float | None:
        mapping = self.config.mapping
        setpoint = mapping.get("power_setpoint_entity")
        if setpoint:
            return self._number(setpoint)
        charge = self._number(mapping.get("charge_power_entity"))
        discharge = self._number(mapping.get("discharge_power_entity"))
        if charge is None and discharge is None:
            return None
        return (charge or 0.0) - (discharge or 0.0)

    def _read_soc(self) -> float | None:
        entity_id = self.config.mapping.get("soc_entity")
        if not entity_id:
            return None
        state = self._state(entity_id)
        if state is None:
            return None
        return parse_soc_pct(state.get("state"), state.get("attributes"))

    def _read_capacity(self) -> float | None:
        entity_id = self.config.mapping.get("capacity_entity")
        if not entity_id:
            return None
        state = self._state(entity_id)
        if state is None:
            return None
        return parse_energy_kwh(state.get("state"), state.get("attributes"))

    # -- writes -----------------------------------------------------------------------

    def _plan_writes(self, target_w: float) -> list[_Write]:
        mapping = self.config.mapping
        writes: list[_Write] = []
        mode_entity = mapping.get("mode_select_entity")
        mode_value = mapping.get("mode_manual_value")
        if mode_entity and mode_value:
            current = self._state(mode_entity)
            if str((current or {}).get("state")) != str(mode_value):
                writes.append(self.make_write(mode_entity, mode_value))
        setpoint = mapping.get("power_setpoint_entity")
        if setpoint:
            writes.append(self.make_write(setpoint, target_w))
            return writes
        charge = mapping.get("charge_power_entity")
        discharge = mapping.get("discharge_power_entity")
        if not charge or not discharge:
            raise DriverError("mapping has no usable power entity")
        writes.append(self.make_write(charge, max(0.0, target_w)))
        writes.append(self.make_write(discharge, max(0.0, -target_w)))
        return writes
