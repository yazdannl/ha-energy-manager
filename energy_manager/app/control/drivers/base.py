"""Battery driver protocol and shared write/read-back plumbing (SPEC section 9.2).

The control layer talks to Home Assistant through the small ``HAFacade`` surface below; that is
exactly the slice of ``app.harest.HAClient`` (WP2A) the drivers use, and it keeps the drivers
testable with an in-process fake.  No driver ever hardcodes an entity id: everything comes from
``BatteryConfig.mapping``, which the user confirms at runtime.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, runtime_checkable

from ...models import (
    BatteryConfig,
    BatteryState,
    DispatchResult,
    Reason,
    clamp,
    from_iso,
    utcnow,
)

log = logging.getLogger(__name__)

#: Consecutive polls whose source timestamps did not advance before a reading that never
#: follows an outstanding command is treated as a frozen integration.
FROZEN_POLLS = 3

# A command smaller than this is considered "already there": the SolarFlow is a BLE/cloud device
# with a finite write budget and reporting steps in the tens of watts.
IDEMPOTENT_TOLERANCE_W = 30.0

# Read-back verification window.  A Zendure-style integration only applies a manual setpoint on the
# next P1 event, so an immediate read-back legitimately still shows the old value: retry inside a
# bounded window before calling it a driver error (docs/EXTERNAL_APIS.md section 6).  Never a false
# success (a mismatch after the window is an error) and never a false failure (a stale first read is
# not an error on its own).
READBACK_ATTEMPTS = 4
READBACK_DELAY_S = 1.5
READBACK_DEADLINE_S = 8.0

WRITABLE_NUMBER_DOMAINS = ("number", "input_number")
WRITABLE_SELECT_DOMAINS = ("select", "input_select")
WRITABLE_SWITCH_DOMAINS = ("switch", "input_boolean", "light")


class DriverError(RuntimeError):
    """Raised when a driver cannot express a mapping at all (config-time problem)."""


@runtime_checkable
class HAFacade(Protocol):
    """The Home Assistant calls the control layer needs.

    ``get_state`` returns the raw HA state dict (``{"state": ..., "attributes": {...}}``) or None;
    ``call_service`` posts ``/api/services/<domain>/<service>``; ``set_state`` posts
    ``/api/states/<entity_id>`` (SPEC section 12).
    """

    def get_state(self, entity_id: str) -> dict[str, Any] | None: ...

    def call_service(self, domain: str, service: str, data: dict[str, Any]) -> Any: ...

    def set_state(
        self, entity_id: str, state: Any, attributes: dict[str, Any] | None = None
    ) -> Any: ...


@runtime_checkable
class BatteryDriver(Protocol):
    """One controllable battery (SPEC section 9.2)."""

    def id(self) -> str: ...

    def read_state(self) -> BatteryState: ...

    def apply(self, power_w: float, *, dry_run: bool) -> DispatchResult: ...

    def hold(self, *, dry_run: bool) -> DispatchResult: ...

    def capabilities(self) -> dict[str, Any]: ...


def parse_number(raw: Any) -> float | None:
    """Parse a HA state value into a float; unknown/unavailable/empty become None."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return 1.0 if raw else 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    if text == "" or text.lower() in ("unknown", "unavailable", "none", "nan"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_soc_pct(raw: Any, attributes: dict[str, Any] | None = None) -> float | None:
    """Percentage SoC from a state, honouring a ``%`` unit and 0..1 fractions."""
    value = parse_number(raw)
    if value is None:
        return None
    unit = str((attributes or {}).get("unit_of_measurement") or "")
    if "%" in unit:
        return clamp(value, 0.0, 100.0)
    if 0.0 <= value <= 1.0:
        return clamp(value * 100.0, 0.0, 100.0)
    return clamp(value, 0.0, 100.0)


def parse_timestamp(raw: Any) -> float | None:
    """Epoch seconds for an HA ``last_updated`` value; None when it cannot be parsed."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    parsed = from_iso(str(raw))
    return parsed.timestamp() if parsed is not None else None


def parse_energy_kwh(raw: Any, attributes: dict[str, Any] | None = None) -> float | None:
    """Energy in kWh from a state, honouring a Wh/MWh unit."""
    value = parse_number(raw)
    if value is None:
        return None
    unit = str((attributes or {}).get("unit_of_measurement") or "").lower()
    if unit.startswith("wh"):
        value /= 1000.0
    elif unit.startswith("mwh"):
        value *= 1000.0
    return max(0.0, value)


def is_entity_id(text: Any) -> bool:
    return isinstance(text, str) and "." in text and not text.startswith(".") and " " not in text


def entity_domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0]


@dataclass
class _Write:
    entity_id: str
    service: str  # "<domain>.<service>"
    value: Any

    def as_dict(self) -> dict[str, Any]:
        return {"entity_id": self.entity_id, "service": self.service, "value": self.value}


class BaseDriver:
    """Shared behaviour: mapping access, clamped writes, idempotency, read-back verification."""

    driver_name = "base"
    # Subclasses that can read the commanded power back set this True; the read-back check is a
    # driver error when it cannot be performed at all (SPEC section 9.2).
    verify_readback = True

    def __init__(
        self,
        config: BatteryConfig,
        ha: HAFacade,
        *,
        tolerance_w: float = IDEMPOTENT_TOLERANCE_W,
        clock=utcnow,
        sleep: Any = None,
        readback_attempts: int = READBACK_ATTEMPTS,
        readback_delay_s: float = READBACK_DELAY_S,
        readback_deadline_s: float = READBACK_DEADLINE_S,
    ) -> None:
        # One command at a time per device: two threads (tick vs stop/exit) must never interleave
        # a read/write/read-back sequence (audit finding 4).
        self._lock = threading.RLock()
        self.config = config
        self._ha = ha
        self.tolerance_w = max(0.0, float(tolerance_w))
        self._clock = clock
        # Time-based retry needs a sleep; injectable so tests never wait for wall-clock time.
        self._sleep = sleep or time.sleep
        self.readback_attempts = max(1, int(readback_attempts))
        self.readback_delay_s = max(0.0, float(readback_delay_s))
        self.readback_deadline_s = max(0.0, float(readback_deadline_s))
        # True once an HA state payload has been seen *with* a timestamp: only then can a missing
        # timestamp mean "unknown age" rather than "this HA facade does not expose one".
        self._timestamps_seen = False
        self._last_read_error: str | None = None
        # Frozen-integration detection across polls (audit finding 1).
        self._entity_stamps: dict[str, float] = {}
        self._stall_polls = 0
        self._frozen_logged = False
        self._last_command: float | None = None

    # -- identity ---------------------------------------------------------------------

    def id(self) -> str:
        return self.config.id

    def capabilities(self) -> dict[str, Any]:
        return {
            "driver": self.driver_name,
            "signed": True,
            "max_charge_w": self.config.max_charge_w,
            "max_discharge_w": self.config.max_discharge_w,
            "mapping_resolved": self.mapping_resolved(),
            "soc_writable": False,
            "tolerance_w": self.tolerance_w,
            "readback_attempts": self.readback_attempts,
            "readback_deadline_s": self.readback_deadline_s,
            "entities": dict(self.effective_mapping()),
        }

    def effective_mapping(self) -> dict[str, str]:
        """The mapping the driver actually uses (config plus any runtime-resolved roles)."""
        return dict(self.config.mapping)

    def mapping_resolved(self) -> bool:
        return not self.validate_mapping(self.config.mapping)

    @classmethod
    def measured_roles(cls, mapping: Mapping[str, Any]) -> dict[str, str]:
        """The measured sampling roles this driver's own reads use, by unsuffixed role name.

        The measurement sampler (``app.sources.measure``) builds its per-battery role map from
        this table, so the ledger and the capacity learner read exactly what the driver reads
        instead of a private copy of the mapping keys that can drift (SPEC 7.3, 0.20.3).  The
        default is the generic vocabulary: the measured AC pair ``charge_power_entity`` /
        ``discharge_power_entity`` plus ``pv_power_entity``, ``output_power_entity`` and
        ``soc_entity``.  A driver whose reads use different keys overrides this.

        ``battery_power`` is deliberately absent: it is the signed ``power_setpoint_entity``, a
        command rather than a confirmed measurement, and keeps its own meaning.  Only mapped
        roles are returned; an unmapped role is never guessed.
        """
        candidates: dict[str, Any] = {
            "battery_charge": mapping.get("charge_power_entity"),
            "battery_discharge": mapping.get("discharge_power_entity"),
            "battery_pv": mapping.get("pv_power_entity"),
            "battery_output": mapping.get("output_power_entity"),
            "battery_soc": mapping.get("soc_entity"),
        }
        return {role: str(entity) for role, entity in candidates.items() if entity}

    # -- reading ----------------------------------------------------------------------

    def _state(self, entity_id: str | None) -> dict[str, Any] | None:
        if not entity_id:
            return None
        try:
            state = self._ha.get_state(entity_id)
        except Exception as exc:  # HA unreachable / bad entity: never raise out of read_state
            log.debug("driver %s: read %s failed: %s", self.id(), entity_id, exc)
            # Recorded so the engine can tell "Home Assistant is unreachable" apart from "the
            # battery is unavailable" (audit finding 10 / ha_connected reporting).
            self._last_read_error = f"{entity_id}: {str(exc)[:160]}"
            return None
        if self._last_read_error and isinstance(state, dict):
            self._last_read_error = None
        return state

    def _number(self, entity_id: str | None) -> float | None:
        state = self._state(entity_id)
        if state is None:
            return None
        return parse_number(state.get("state"))

    def _attributes(self, entity_id: str | None) -> dict[str, Any]:
        state = self._state(entity_id)
        if not isinstance(state, dict):
            return {}
        attributes = state.get("attributes")
        return attributes if isinstance(attributes, dict) else {}

    def mapped_entities(self) -> list[str]:
        """Entity ids the driver may read or write, in stable order (no duplicates)."""
        seen: list[str] = []
        for key in sorted(self.config.mapping):
            value = self.config.mapping[key]
            if is_entity_id(value) and value not in seen:
                seen.append(value)
        return seen

    def _poll_timestamps(self) -> bool:
        """Track whether the source timestamps advanced since the previous poll.

        ``read_at`` is the moment of a *successful* read (we just read it), so a battery that is
        simply idle -- HA only advances ``last_updated`` when a state actually changes -- is never
        stale.  A frozen integration is detected *across* polls instead: the counter here feeds
        ``_frozen_now``, which only bites when we have an outstanding command the measurement never
        follows (audit: the previous version made an idle battery permanently stale).
        """
        seen = False
        advanced = False
        for entity_id in self.mapped_entities():
            state = self._state(entity_id)
            if not isinstance(state, dict):
                continue
            for key in ("last_updated", "last_changed"):
                raw = state.get(key)
                if raw is None:
                    continue
                seen = True
                stamp = parse_timestamp(raw)
                if stamp is None:
                    continue
                cache_key = f"{entity_id}:{key}"
                previous = self._entity_stamps.get(cache_key)
                if previous is None or stamp > previous:
                    advanced = True
                self._entity_stamps[cache_key] = stamp
        if not seen:
            self._stall_polls = 0
            return False
        self._stall_polls = 0 if advanced else self._stall_polls + 1
        if self._stall_polls >= FROZEN_POLLS and not self._frozen_logged:
            self._frozen_logged = True
            log.warning(
                "driver %s: source timestamps have not advanced for %d polls (possible frozen "
                "integration)",
                self.id(),
                self._stall_polls,
            )
        elif self._stall_polls == 0:
            self._frozen_logged = False
        return seen

    def _frozen_now(self, power: float | None) -> bool:
        """True when a *commanded* power is not showing up while the timestamps stand still.

        Never blocks a reading on its own: without an outstanding non-zero command a quiet
        integration is perfectly healthy.
        """
        command = self._last_command
        if command is None or abs(float(command)) <= self.tolerance_w:
            return False
        if power is None or abs(float(power) - float(command)) <= self.tolerance_w:
            return False
        return self._stall_polls >= FROZEN_POLLS

    def read_state(self) -> BatteryState:
        moment = self._clock()
        try:
            power = self._read_power()
            soc = self._read_soc()
            reported_capacity = self._read_capacity()
            capacity = reported_capacity or self.config.capacity_kwh
            energy = self._read_energy(soc, capacity)
            available = self._available(power, soc)
            timestamps_seen = self._poll_timestamps()
            frozen = self._frozen_now(power)
            return BatteryState(
                id=self.config.id,
                available=available,
                soc_pct=soc,
                power_w=power,
                capacity_kwh=capacity,
                energy_kwh=energy,
                pv_power_w=self._read_pv_power(),
                temperature_c=self._read_temperature(),
                read_at=moment,
                # Only a confirmed measurement may stand in for delivered battery energy.
                power_measured=power is not None and self._power_is_measured(),
                # Only a failed transport read, or a commanded setpoint the measurement keeps
                # ignoring, blocks control (audit finding 1).
                stale=(not available) or frozen,
                last_error=(
                    "source timestamps stalled and the measurement does not follow the command"
                    if frozen
                    else None
                ),
                extra={
                    "timestamps_seen": bool(timestamps_seen),
                    "stalled_polls": self._stall_polls,
                    "frozen": frozen,
                    # Only a capacity the pack itself states may cap a learned estimate; the
                    # config fallback is the plan's own scale and feeding it back would let the
                    # ceiling grow every replan (0.37.2).
                    "capacity_reported": reported_capacity is not None,
                    # Optional mapped min-cell voltage (SPEC 9.2/9.3): per cell, never a guess.
                    "cell_voltage_v": self._mapped_cell_voltage(),
                },
            )
        except Exception as exc:  # pragma: no cover - defensive, HA facade must not raise
            log.debug("driver %s: read_state failed: %s", self.id(), exc)
            return BatteryState(
                id=self.config.id,
                available=False,
                read_at=moment,
                stale=True,
                last_error=str(exc)[:200],
            )

    def _available(self, power: float | None, soc: float | None) -> bool:
        for entity_id in self.mapped_entities():
            if parse_number(self._state(entity_id)) is not None:
                return True
        return power is not None or soc is not None

    def _read_power(self) -> float | None:
        raise NotImplementedError

    def _power_is_measured(self) -> bool:
        """True when ``_read_power`` returned a real flow, not a setpoint this add-on wrote.

        Setpoint-only drivers (see ``generic.py``) read back the value they command, so their power
        is unconfirmed and must not be credited to savings as delivered energy (0.13.0).
        """
        return False

    def _read_soc(self) -> float | None:
        return None

    def _read_capacity(self) -> float | None:
        return None

    def _read_energy(self, soc: float | None, capacity: float | None) -> float | None:
        if soc is None or not capacity:
            return None
        return max(0.0, float(capacity) * soc / 100.0)

    def _read_pv_power(self) -> float | None:
        return None

    def _read_temperature(self) -> float | None:
        """Battery/enclosure temperature in °C when the driver can report one."""
        return None

    def read_cell_voltage(self) -> float | None:
        """Optional driver hook: lowest cell voltage in V, or None when unavailable (SPEC 9.2).

        A driver that can read a real cell voltage (``zendure_native``) overrides this so a
        low-voltage guard can act on it.  The default is None -- an unreadable cell voltage is
        never guessed from a pack total voltage.  The hook is the *fallback*: a battery with a
        ``cell_voltage_entity`` mapped reads that entity instead (``_mapped_cell_voltage``).
        """
        return None

    def _mapped_cell_voltage(self) -> float | None:
        """The mapped ``cell_voltage_entity`` value in V, else None (per cell, never a guess)."""
        entity_id = self.config.mapping.get("cell_voltage_entity")
        if not is_entity_id(entity_id):
            return None
        value = self._number(entity_id)
        return value if value is not None and value > 0.0 else None

    # -- writing ----------------------------------------------------------------------

    def apply(self, power_w: float, *, dry_run: bool) -> DispatchResult:
        with self._lock:
            return self._apply(power_w, dry_run=dry_run, allow_idempotent_skip=True)

    @property
    def last_read_error(self) -> str | None:
        """The last transport-level read failure, if any (cleared by a successful read)."""
        return self._last_read_error

    def commanded_power(self) -> float | None:
        """What the device was last *told* to do (public wrapper used by the engine).

        For most drivers this is the measured flow; drivers whose device applies commands
        asynchronously return the commanded setpoint instead, which is what makes the engine's
        "is a real setpoint still live?" latch honest (audit findings 1/2).
        """
        value = self._idempotent_power()
        return value if value is not None else self._last_command

    def _idempotent_power(self) -> float | None:
        """The power used for the pre-write "already there" check.

        Defaults to the measured flow; drivers whose device applies commands asynchronously
        override this with the *commanded* value (audit finding 1).
        """
        return self._read_power()

    def _apply(
        self, power_w: float, *, dry_run: bool, allow_idempotent_skip: bool = True
    ) -> DispatchResult:
        moment = self._clock()
        target = clamp(
            float(power_w), -abs(self.config.max_discharge_w), abs(self.config.max_charge_w)
        )

        def result(**kwargs) -> DispatchResult:
            return DispatchResult(
                battery_id=self.config.id,
                ts=moment,
                requested_w=target,
                applied_w=kwargs.pop("applied_w", target),
                ok=kwargs.pop("ok", True),
                dry_run=dry_run,
                **kwargs,
            )

        try:
            current = self._idempotent_power()
        except Exception as exc:  # pragma: no cover - defensive
            current = None
            log.debug("driver %s: pre-write read failed: %s", self.id(), exc)

        if allow_idempotent_skip and current is not None and abs(current - target) <= self.tolerance_w:
            return result(
                applied_w=current,
                code=Reason.IDEMPOTENT_SKIP,
                message=f"device already at {current:.0f} W (target {target:.0f} W)",
            )

        try:
            planned = self._plan_writes(target)
        except DriverError as exc:
            return result(ok=False, code=Reason.DRIVER_ERROR, message=str(exc)[:200])

        if dry_run:
            return result(
                code=Reason.DRY_RUN,
                message=f"dry_run: {len(planned)} would-be write(s), no I/O",
                writes=[write.as_dict() for write in planned],
            )
        self._last_command = target

        writes: list[_Write] = []
        try:
            for write in planned:
                self._perform(write)
                writes.append(write)
        except Exception as exc:
            return result(
                ok=False,
                applied_w=0.0,
                code=Reason.DRIVER_ERROR,
                message=f"service call failed: {str(exc)[:160]}",
                writes=[write.as_dict() for write in writes],
            )

        if not self.verify_readback:
            return result(code=Reason.OK, writes=[write.as_dict() for write in writes])

        readback, problem = self._verify_writes(target)
        if problem is not None:
            return result(
                ok=False,
                applied_w=readback if readback is not None else 0.0,
                code=Reason.DRIVER_ERROR,
                message=problem,
                writes=[write.as_dict() for write in writes],
                readback_w=readback,
            )
        assert readback is not None  # _verify_writes only returns None-with-problem
        return result(
            applied_w=readback,
            code=Reason.OK,
            writes=[write.as_dict() for write in writes],
            readback_w=readback,
        )

    def _verify_writes(self, target_w: float) -> tuple[float | None, str | None]:
        """Read the commanded power back inside a bounded window; ``(power, problem)``.

        ``problem`` is non-None when the device never confirmed the write -- reporting success then
        would be a lie, and reporting failure on the first slow poll would be a false alarm.
        """
        return self._verify_against(target_w, self._read_power, "read-back")

    def _verify_against(
        self, target_w: float, reader: Any, label: str = "read-back"
    ) -> tuple[float | None, str | None]:
        """Verify ``target_w`` against ``reader()`` inside the bounded retry window."""
        attempts = self.readback_attempts
        deadline = time.monotonic() + self.readback_deadline_s
        readback: float | None = None
        for attempt in range(attempts):
            try:
                readback = reader()
            except Exception as exc:  # pragma: no cover - defensive
                readback = None
                log.debug("driver %s: %s failed: %s", self.id(), label, exc)
            if readback is not None and abs(readback - target_w) <= self.tolerance_w:
                return readback, None
            if attempt + 1 >= attempts or time.monotonic() >= deadline:
                break
            self._sleep(min(self.readback_delay_s, max(0.0, deadline - time.monotonic())))
        if readback is None:
            return None, f"no {label} after write; refusing to report success"
        return readback, f"{label} {readback:.0f} W differs from target {target_w:.0f} W"

    def hold(self, *, dry_run: bool) -> DispatchResult:
        """Neutralise the battery (SPEC 9.4): the setpoint write is never skipped.

        A device can still be executing the previous command while its *measured* flow reads 0 --
        the Zendure manager only applies a manual setpoint on the next P1 event -- so deciding a
        hold from measured flow would silently leave the stale command running (audit finding 1).
        The command is issued unconditionally and verified against the commanded value.
        """
        with self._lock:
            return self._apply(0.0, dry_run=dry_run, allow_idempotent_skip=False)

    def _plan_writes(self, target_w: float) -> list[_Write]:
        raise NotImplementedError

    def _perform(self, write: _Write) -> None:
        domain, _, service = write.service.partition(".")
        data: dict[str, Any]
        if domain in WRITABLE_SELECT_DOMAINS:
            data = {"entity_id": write.entity_id, "option": write.value}
        elif domain in WRITABLE_SWITCH_DOMAINS:
            data = {"entity_id": write.entity_id}
        else:
            data = {"entity_id": write.entity_id, "value": write.value}
        self._ha.call_service(domain, service, data)

    def make_write(self, entity_id: str, value: Any) -> _Write:
        """Describe the service call needed to write ``value`` to ``entity_id``."""
        if not is_entity_id(entity_id):
            raise DriverError(f"not an entity id: {entity_id!r}")
        domain = entity_domain(entity_id)
        if domain in WRITABLE_NUMBER_DOMAINS:
            return _Write(entity_id, f"{domain}.set_value", float(value))
        if domain in WRITABLE_SELECT_DOMAINS:
            return _Write(entity_id, f"{domain}.select_option", value)
        if domain in WRITABLE_SWITCH_DOMAINS:
            return _Write(entity_id, f"{domain}.{'turn_on' if value else 'turn_off'}", value)
        raise DriverError(f"{entity_id}: domain {domain!r} cannot be written")

    @classmethod
    def validate_mapping(cls, mapping: dict[str, str]) -> list[str]:
        raise NotImplementedError


def default_tolerance(config: BatteryConfig) -> float:
    """Tolerance scaled a little with the device size, never below the reporting step."""
    return max(IDEMPOTENT_TOLERANCE_W, abs(config.max_charge_w) * 0.02)
