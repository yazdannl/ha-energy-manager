"""Effective grid mapping (SPEC.md 3): explicit ``grid.*`` settings, else the Energy dashboard.

The Energy dashboard already holds the house meter: ``energy/get_prefs`` (WebSocket, no admin
needed) carries the grid source's signed power sensor (``stat_rate``, positive = import) and the
import/export energy counters. This module turns that payload into the same role slugs the rest of
the application uses, so a fresh install samples and learns from the meter its owner has already
described to Home Assistant instead of staying blind until they map it again.

Priority:

1. An explicit ``grid.power_entity`` / ``grid.import_entity`` / ``grid.export_entity`` setting wins
   as a whole. Any explicit power role disables the Energy-dashboard fallback: mixing the two
   sources would risk double counting the same meter.
2. The Energy dashboard's power sensor. ``stat_rate`` is preferred because Home Assistant fills it
   for every grid power configuration (its own generated net-power sensor for an inverted or
   two-sensor config), and its sign already matches ``grid.power_entity`` (positive = import). An
   ``stat_rate_inverted`` without that generated sensor is deliberately ignored: this application
   cannot invert a live reading, and guessing would flip every control decision.
3. The Energy dashboard's energy counters, as ``grid_energy_import`` / ``grid_energy_export``:
   history only. A kWh meter cannot be sampled as live power, but its long-term statistics (never
   purged) are exactly what the derived load and meter-derived PV tiers need.

Multiple grid sources are possible in Home Assistant; the first one with a usable mapping wins, and
a power-bearing source outranks an energy-only one.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from .models import utcnow

log = logging.getLogger(__name__)

__all__ = [
    "GRID_ENERGY_ROLES",
    "GRID_POWER_ROLES",
    "GRID_SETTINGS_KEYS",
    "GridMapping",
    "roles_from_energy_prefs",
]

#: Roles that are live power (kW) and can be sampled; the order is the resolution order.
GRID_POWER_ROLES = ("grid_power", "grid_import", "grid_export")
#: Roles that are cumulative energy counters (kWh): long-term statistics only.
GRID_ENERGY_ROLES = ("grid_energy_import", "grid_energy_export")

#: Role -> the ``grid.*`` settings key an explicit confirmation is stored in (SPEC 3).
GRID_SETTINGS_KEYS = {
    "grid_power": "grid.power_entity",
    "grid_import": "grid.import_entity",
    "grid_export": "grid.export_entity",
}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _first_entry(rows: Any) -> Mapping[str, Any]:
    """The first mapping of a legacy ``flow_from``/``flow_to``/``power`` list, or ``{}``."""
    if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
        for row in rows:
            if isinstance(row, Mapping):
                return row
    return {}


def _power_config(grid: Mapping[str, Any], legacy_power: Mapping[str, Any]) -> Mapping[str, Any]:
    for candidate in (grid.get("power_config"), legacy_power.get("power_config")):
        if isinstance(candidate, Mapping):
            return candidate
    return {}


def _roles_from_grid(grid: Mapping[str, Any]) -> dict[str, str]:
    """Roles for one grid source; tolerates the legacy ``flow_from``/``flow_to``/``power`` shape."""
    roles: dict[str, str] = {}
    legacy_power = _first_entry(grid.get("power"))
    power = _power_config(grid, legacy_power)
    rate = (
        _text(grid.get("stat_rate"))
        or _text(power.get("stat_rate"))
        or _text(legacy_power.get("stat_rate"))
    )
    if rate:
        roles["grid_power"] = rate
    else:
        # ``stat_rate_inverted`` is only usable through the net sensor Home Assistant generates for
        # it, which appears above as ``stat_rate``; see the module docstring.
        imported = _text(power.get("stat_rate_from")) or _text(grid.get("stat_rate_from"))
        exported = _text(power.get("stat_rate_to")) or _text(grid.get("stat_rate_to"))
        if imported:
            roles["grid_import"] = imported
        if exported:
            roles["grid_export"] = exported
    legacy_from = _first_entry(grid.get("flow_from"))
    legacy_to = _first_entry(grid.get("flow_to"))
    energy_from = _text(grid.get("stat_energy_from")) or _text(legacy_from.get("stat_energy_from"))
    energy_to = _text(grid.get("stat_energy_to")) or _text(legacy_to.get("stat_energy_to"))
    if energy_from:
        roles["grid_energy_import"] = energy_from
    if energy_to:
        roles["grid_energy_export"] = energy_to
    return roles


def roles_from_energy_prefs(prefs: Any) -> dict[str, str]:
    """Grid role -> entity/statistic id from an ``energy/get_prefs`` payload.

    Tolerates a missing or empty dashboard (``{}``) and the pre-2026 legacy grid shape. Power
    roles win over energy counters, and the first usable grid source wins over later ones.
    """
    if not isinstance(prefs, Mapping):
        return {}
    sources = prefs.get("energy_sources")
    if not isinstance(sources, Sequence) or isinstance(sources, (str, bytes)):
        return {}
    grids = [
        source
        for source in sources
        if isinstance(source, Mapping) and _text(source.get("type")) == "grid"
    ]
    candidates = [_roles_from_grid(grid) for grid in grids]
    for roles in candidates:
        if any(role in roles for role in GRID_POWER_ROLES):
            return roles
    for roles in candidates:
        if any(role in roles for role in GRID_ENERGY_ROLES):
            return roles
    return {}


class GridMapping:
    """Effective grid roles: explicit ``grid.*`` settings, else the Energy dashboard.

    ``settings`` is any mapping-like with ``get`` (an :class:`app.config.Settings`); ``ws_factory``
    is a zero-argument callable returning a context-managed WebSocket client (``app.haws``
    :class:`HAWebSocket`). Both are duck-typed so the engine and the source modules never import
    each other.

    The Energy-dashboard read is cached for ``ttl`` seconds: the dashboard changes only when its
    owner edits it, and a dead Home Assistant must not turn every control tick into a socket
    attempt. A failed read is cached exactly like a successful one; recovery waits out the TTL
    instead of hammering the socket.
    """

    def __init__(
        self,
        settings: Any,
        *,
        ws_factory: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] = utcnow,
        ttl: float = 300.0,
    ) -> None:
        self._settings = settings
        self._ws_factory = ws_factory
        self._clock = clock
        self._ttl = float(ttl)
        self._lock = threading.Lock()
        self._fetched_at: datetime | None = None
        self._roles: dict[str, str] = {}

    def explicit_roles(self) -> dict[str, str]:
        """The user's own ``grid.*`` mappings; empty when none are set."""
        roles: dict[str, str] = {}
        for role, key in GRID_SETTINGS_KEYS.items():
            entity = _text(self._settings.get(key, ""))
            if entity:
                roles[role] = entity
        return roles

    def dashboard_roles(self) -> dict[str, str]:
        """Every role the Energy dashboard offers, explicit overrides included or not.

        The settings panel shows this so a saved override can be told apart from the fallback it
        replaces; the effective mapping itself stays :meth:`power_roles`/:meth:`history_roles`.
        """
        return dict(self._prefs_roles())

    def power_roles(self) -> dict[str, str]:
        """Live power roles for the sampler, control and the derived tiers."""
        explicit = self.explicit_roles()
        if explicit:
            return explicit
        return {
            role: entity
            for role, entity in self._prefs_roles().items()
            if role in GRID_POWER_ROLES
        }

    def history_roles(self) -> dict[str, str]:
        """Roles a history reader can use: power when available, else energy counters."""
        power = self.power_roles()
        return power if power else dict(self._prefs_roles())

    def source(self) -> str:
        """Where the effective mapping came from: ``settings``, ``energy_dashboard_*`` or ``""``."""
        if self.explicit_roles():
            return "settings"
        roles = self._prefs_roles()
        if any(role in roles for role in GRID_POWER_ROLES):
            return "energy_dashboard_power"
        if any(role in roles for role in GRID_ENERGY_ROLES):
            return "energy_dashboard_energy"
        return ""

    def note(self) -> str:
        """One-line provenance note for a status payload; empty when nothing was resolved."""
        source = self.source()
        if source == "settings":
            return ""
        roles = self.power_roles() or self.history_roles()
        if not roles:
            return "no grid meter mapped and the Energy dashboard has none either"
        kind = "power" if source == "energy_dashboard_power" else "energy"
        entities = ", ".join(f"{role}={entity}" for role, entity in sorted(roles.items()))
        return f"grid roles from the Home Assistant Energy dashboard ({kind}): {entities}"

    def _prefs_roles(self) -> dict[str, str]:
        now = self._clock()
        with self._lock:
            if (
                self._fetched_at is not None
                and (now - self._fetched_at).total_seconds() < self._ttl
            ):
                return dict(self._roles)
            self._fetched_at = now
            roles: dict[str, str] = {}
            if self._ws_factory is not None:
                try:
                    with self._ws_factory() as websocket:
                        roles = roles_from_energy_prefs(websocket.energy_prefs())
                except Exception as exc:  # noqa: BLE001 - a fallback must never break a tick
                    log.debug("Energy dashboard prefs unavailable: %s", exc)
                    roles = {}
            self._roles = roles
            return dict(roles)
