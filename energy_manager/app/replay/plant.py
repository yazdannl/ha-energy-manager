"""The replay plant: recorded and simulated battery drivers (docs/REPLAY_HARNESS.md section 2).

The plant is the actuator side of a replay.  ``recorded`` holds the recording's trajectory fixed so
E1 can compare decisions against it; ``simulated`` integrates the commanded power so a policy change
can be measured at all.  Both read only through :meth:`Episode.at` snapshots, so the plant obeys the
same non-anticipation rule as the engine.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from ..control.drivers.base import BatteryDriver
from ..control.safety import RAMP_LIMIT_W
from ..models import BatteryConfig, BatteryState, DispatchResult, Reason, clamp
from .episode import Episode

RECORDED = "recorded"
SIMULATED = "simulated"
MODES = (RECORDED, SIMULATED)


def max_export_kw(settings: Any) -> float:
    """The planner's own export bound in kW.

    ``optimizer.max_grid_export_w`` is the single source of the convention: ``optimize/dp.py`` reads
    it the same way when it builds the slot's grid window, and the plant must not invent a second
    constant that could drift from it.
    """
    return float(settings.get("optimizer.max_grid_export_w", 0) or 0.0) / 1000.0


class PlantFleet:
    """Site-level bookkeeping shared by every driver one :func:`plant_factory` creates.

    One replay is one site, not a set of independent packs: the export bound applies to
    ``base + sum(power)`` with ``base = load - pv`` at the current instant, and ``grid_kw()`` must
    report that same site flow.  Both are only true when the drivers share this object.

    Allocation is deterministic: the engine dispatches in ``Dispatcher._ordered_drivers`` order
    (batteries sorted by id), each driver settles and clamps when its turn comes, and it may use only
    the site headroom still free at that moment -- so with two packs the second sees what the first
    left.  With a single pack every number is identical to a per-driver bound.

    Every aggregation settles all members first (:meth:`settle_all`), so the site flow is the same
    number whichever driver a caller asks first: a pack that has not been read since the last
    command still has to contribute the flow it is really delivering.
    """

    def __init__(self, episode: Episode) -> None:
        self.episode = episode
        self.drivers: list[PlantDriver] = []

    def register(self, driver: "PlantDriver") -> None:
        """Add a driver, replacing one of the same id so a rebuilt driver cannot double-count."""
        self.drivers = [entry for entry in self.drivers if entry.id() != driver.id()]
        self.drivers.append(driver)

    def settle_all(self) -> None:
        """Bring every member to the current replay instant before any aggregation."""
        for driver in self.drivers:
            driver.settle()

    @property
    def mode(self) -> str:
        """The mode the registered drivers were built in (one replay runs one mode)."""
        return self.drivers[0].mode if self.drivers else SIMULATED

    def total_power_w(self) -> float | None:
        """The site's battery flow, or ``None`` as soon as any member's flow is unknown.

        A pack whose flow sample is missing is not a pack at zero: ``base + 0`` would be a site flow
        -- and an export headroom -- that nobody measured, so the sum is refused, not invented.
        """
        self.settle_all()
        powers = [driver.realized_power() for driver in self.drivers]
        if any(power is None for power in powers):
            return None
        return sum(float(power) for power in powers)

    def other_power_w(self, driver: "PlantDriver") -> float | None:
        """The other batteries' realized power, i.e. what this one must respect as already spent."""
        self.settle_all()
        powers = [entry.realized_power() for entry in self.drivers if entry.id() != driver.id()]
        if any(power is None for power in powers):
            return None
        return sum(float(power) for power in powers)

    def base_kw(self) -> float | None:
        """``load - pv`` at the current instant, or ``None`` when the recording lacks it."""
        snapshot = self.episode.at(self.episode.now)
        load = snapshot.latest("load_kw")
        pv = snapshot.latest("pv_kw")
        if load is None or pv is None or load[1] is None or pv[1] is None:
            return None
        return float(load[1]) - float(pv[1])

    def grid_reconstruction_kw(self) -> float | None:
        """``base + sum(power)``: the site flow rebuilt from this arm's own battery powers.

        Recorded mode answers :meth:`grid_kw` with the recording's own measurement, so this is how
        far the replayed packs would have been from it -- a recording-data comparison a report can
        attribute, never a substitute for the measurement.  ``None`` when either side is unknown.
        """
        base = self.base_kw()
        total = self.total_power_w()
        if base is None or total is None:
            return None
        return base + total / 1000.0

    def grid_kw(self) -> float | None:
        """The site's AC grid flow at the current instant (section 2's convention).

        Recorded mode answers with the recording's own site measurement: the recorded drivers only
        echo what was recorded, so reconstructing ``base + battery`` would derive a number the
        recording already measured -- and would have to invent one whenever a pack sample is
        missing.  Simulated mode computes the flow from the plant's own powers and refuses it when
        either side is unknown.
        """
        if self.mode == RECORDED:
            snapshot = self.episode.at(self.episode.now)
            grid = snapshot.latest("grid_kw")
            return None if grid is None or grid[1] is None else float(grid[1])
        return self.grid_reconstruction_kw()


class PlantDriver:
    """One battery of the replay plant, in ``recorded`` or ``simulated`` mode.

    ``recorded`` answers :meth:`read_state` from the recording's per-battery SoC/pack-power samples
    and only records the setpoint on :meth:`apply`; ``simulated`` keeps a *realized* power -- what
    the pack can actually do, not just what was asked -- and integrates it into stored DC energy
    with the episode's effective config (``capacity_kwh``, ``eta_charge``/``eta_discharge``, power
    limits, ``min_soc_pct`` and the per-tick ramp).

    A simulated pack is seeded from the recording's as-of state at the instant the driver is built
    (the runner builds it at ``start``) and integrates only forward from there.  Its accounting
    quantity is :attr:`moved_kwh`: the DC energy that actually moved, signed, which is not
    ``realized_power() * dt`` because an interval can saturate.  ``last_infeasible`` is public: it is
    set when the site export bound could not be met inside the pack and ramp limits, so the report
    can read the actuator's verdict instead of the plant inventing power.
    """

    driver_name = "replay_plant"

    def __init__(
        self,
        episode: Episode,
        config: BatteryConfig,
        mode: str,
        fleet: PlantFleet | None = None,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown plant mode {mode!r}; have {', '.join(MODES)}")
        self.episode = episode
        self.config = config
        self.mode = mode
        self.fleet = fleet if fleet is not None else PlantFleet(episode)
        self.fleet.register(self)
        #: Every accepted command of this run, oldest first: (instant, applied W, dry run).
        self.commands: list[tuple[datetime, float, bool]] = []
        #: True when the last ``apply`` could not satisfy the export bound within the pack's limits.
        self.last_infeasible = False
        #: True when the last ``apply`` could not evaluate the site export bound at all (the site
        #: ``base`` or a peer pack's flow was unknown), so the command was checked against this
        #: pack's own limits only.  Reported rather than claiming the bound was satisfied.
        self.last_bound_unverified = False
        #: True once an interval elapsed with no known flow: the stored energy stopped being an
        #: observation, so the pack reports itself unavailable instead of integrating from a value
        #: nobody measured (a later command cannot re-establish it either).
        self.energy_unknown = False
        self._at: datetime | None = None
        self._commanded_w: float | None = None
        #: The power the pack is currently delivering (simulated mode), never a saturating command.
        self._power_w: float | None = None
        self._energy_kwh: float | None = None
        #: Signed DC energy this pack actually moved (kWh, positive = charged).  The accounting uses
        #: this, or the SoC series, rather than power times dt: a saturating interval moves less.
        self.moved_kwh = 0.0
        seed = episode.at(episode.now).battery_state(config)
        self._at = episode.now
        # A seed without a power sample stays unknown: ``None`` is an absent measurement, never a
        # zero flow, so the fleet sums refuse it instead of inventing a site flow from it and the
        # ramp has nothing to ramp from until the first command sets the flow.
        self._power_w = None if seed.power_w is None else float(seed.power_w)
        self._energy_kwh = seed.energy_kwh
        #: The floor the current flow is judged against: ``min_soc``, or the pack's own energy when
        #: it was seeded below the reserve, so it is never lifted to a floor it never had.
        self._floor_kwh: float | None = (
            None if seed.energy_kwh is None else min(self._e_min(), float(seed.energy_kwh))
        )

    # -- BatteryDriver protocol -------------------------------------------------

    def id(self) -> str:
        return self.config.id

    def read_state(self) -> BatteryState:
        if self.mode == RECORDED:
            return self.episode.at(self.episode.now).battery_state(self.config)
        self.settle()
        return self._state()

    def apply(self, power_w: float, *, dry_run: bool) -> DispatchResult:
        now = self.episode.now
        requested = float(power_w)
        if self.mode == RECORDED:
            # The recorded arm holds the trajectory fixed: write what was commanded, and let the
            # recording's own samples answer reads.  Clamping here would be the harness inventing an
            # actuator limit the live driver may not have had, which would corrupt the E1 comparison.
            self._commanded_w = requested
            self.last_infeasible = False
            # The recorded arm never clamps, so it never evaluates the site bound either: say so
            # rather than leaving the flag reading as "checked and satisfied".
            self.last_bound_unverified = True
            self.commands.append((now, requested, bool(dry_run)))
            return self._result(now, requested, requested, dry_run)
        self.settle()
        applied = self._clamp(requested)
        self._commanded_w = applied
        self._power_w = applied
        self.commands.append((now, applied, bool(dry_run)))
        return self._result(now, requested, applied, dry_run)

    def hold(self, *, dry_run: bool) -> DispatchResult:
        return self.apply(0.0, dry_run=dry_run)

    def capabilities(self) -> dict[str, Any]:
        return {
            "driver": self.driver_name,
            "mode": self.mode,
            "signed": True,
            "max_charge_w": float(self.config.max_charge_w),
            "max_discharge_w": float(self.config.max_discharge_w),
            "mapping_resolved": True,
            "soc_writable": False,
            "last_infeasible": self.last_infeasible,
            "last_bound_unverified": self.last_bound_unverified,
            "energy_unknown": self.energy_unknown,
            "fleet_size": len(self.fleet.drivers),
            "entities": dict(self.config.mapping),
        }

    # -- driver extras the engine reads opportunistically ------------------------

    def effective_mapping(self) -> dict[str, Any]:
        return dict(self.config.mapping)

    def mapping_resolved(self) -> bool:
        return True

    def commanded_power(self) -> float | None:
        """The last accepted setpoint, for the guard's watchdog trace."""
        return self._commanded_w

    def realized_power(self) -> float | None:
        """The power this battery is actually delivering (recorded: the sample's own value).

        Simulated mode settles first and answers with the boundary-consistent flow -- never the
        elapsed interval's average -- because the ramp window and the fleet sums reference the
        pack's flow *now*: a pack pinned at a wall is not flowing, and the average is an accounting
        quantity (:attr:`moved_kwh`) rather than an instantaneous one.
        """
        if self.mode == RECORDED:
            state = self.episode.at(self.episode.now).battery_state(self.config)
            return state.power_w
        self.settle()
        return self._instant_power()

    # -- plant state -------------------------------------------------------------

    def grid_kw(self) -> float | None:
        """The site's AC grid flow at the current instant (section 2's convention).

        ``load - pv + sum(realized battery power)``: the flow the whole fleet contributes to, not
        just this pack, so a two-battery site reads the same number from either driver.
        """
        return self.fleet.grid_kw()

    def soc_pct(self) -> float | None:
        return self.read_state().soc_pct

    # -- internals ---------------------------------------------------------------

    def _result(
        self, now: datetime, requested: float, applied: float, dry_run: bool
    ) -> DispatchResult:
        return DispatchResult(
            battery_id=self.config.id,
            ts=now,
            requested_w=requested,
            applied_w=applied,
            ok=True,
            dry_run=bool(dry_run),
            code=Reason.DRY_RUN if dry_run else Reason.OK,
            message=(
                "site export bound not evaluated for this command"
                if self.last_bound_unverified
                else ""
            ),
        )

    def _state(self) -> BatteryState:
        """The simulated battery's state, from its stored DC energy and realized power.

        ``power_measured`` is only true when the pack actually knows its flow: a seed with no power
        sample answers ``None``, which is an absent measurement rather than a measured zero.
        """
        capacity = float(self.config.capacity_kwh)
        energy = self._energy_kwh
        power = self._instant_power()
        soc_pct = (
            None
            if energy is None or capacity <= 0
            else clamp(energy / capacity * 100.0, 0.0, 100.0)
        )
        return BatteryState(
            id=self.config.id,
            available=energy is not None,
            soc_pct=soc_pct,
            power_w=power,
            capacity_kwh=capacity,
            energy_kwh=energy,
            read_at=self.episode.now,
            power_measured=power is not None,
            extra={"energy_unknown": True} if self.energy_unknown else {},
        )

    def settle(self) -> None:
        """Integrate the realized power over the elapsed replay time (simulated mode only).

        The update is ``optimize/dp.py``'s own energy model: ``e += dt*p*eta_charge`` when charging,
        ``e += dt*p/eta_discharge`` when discharging, with ``p`` the AC-side power.  The DC energy
        that really moved is accumulated in :attr:`moved_kwh` and the floor for this interval is the
        pack's own energy when it is below its reserve, so a below-reserve pack is never lifted to a
        floor it never had.  Idempotent for a given instant: a second call moves nothing, which is
        what lets the fleet aggregate in any read order.
        """
        now = self.episode.now
        if self.mode != SIMULATED or self._at is None or now <= self._at:
            self._at = now
            return
        hours = (now - self._at).total_seconds() / 3600.0
        self._at = now
        energy = self._energy_kwh
        power = self._power_w
        if hours <= 0:
            return
        if power is None:
            # No known flow over an elapsed interval means the energy that moved is unknown, so the
            # stored energy stops being an observation instead of standing as a silent zero-flow
            # assumption -- and a later command cannot responsibly integrate from it either.
            self._energy_kwh = None
            self.energy_unknown = True
            return
        if energy is None:
            return
        before = float(energy)
        floor = min(self._e_min(), before)
        self._floor_kwh = floor
        if power > 0:
            delta = power / 1000.0 * self._eta("eta_charge") * hours
        elif power < 0:
            delta = power / 1000.0 / self._eta("eta_discharge") * hours
        else:
            delta = 0.0
        after = clamp(before + delta, floor, max(self._e_max(), before))
        self._energy_kwh = after
        self.moved_kwh += after - before

    def _instant_power(self) -> float | None:
        """The applied power reduced to the flow the pack can deliver at the current energy.

        Charging is zero at or above the ceiling and discharging is zero at or below the floor, so a
        pack pinned at a wall reports no flow even though it moved energy inside the interval.
        Anything in between is physically possible, so the applied power stands as-is.  Without a
        stored energy there is no way to say whether the pack can deliver the command at all, so the
        realized flow is unknown too -- the command itself is kept in ``_commanded_w`` -- and the
        fleet refuses to sum it rather than reporting a flow nobody could verify.
        """
        power = self._power_w
        energy = self._energy_kwh
        if power is None or energy is None:
            return None
        if power == 0.0:
            return 0.0
        if power > 0.0 and energy >= self._e_max():
            return 0.0
        floor = self._floor_kwh
        if power < 0.0 and floor is not None and energy <= floor:
            return 0.0
        return power

    def _clamp(self, power_w: float) -> float:
        """The command narrowed to the actuator's window, or the actuator's limits when the export
        bound cannot be met inside them (``last_infeasible``)."""
        low, high, infeasible, unverified = self._window()
        self.last_infeasible = infeasible
        self.last_bound_unverified = unverified
        return clamp(power_w, low, high)

    def _window(self) -> tuple[float, float, bool, bool]:
        """The allowed power window, in priority order: physical, ramp, then export.

        Returns ``(low, high, infeasible, unverified)``: ``unverified`` is true when a configured
        site export bound could not be evaluated at all, so the caller must not report the command as
        having passed it.

        The pack's own power limits intersected with the SoC gates are the *physical* window and are
        never violated: the ramp may only narrow inside it (so an inverted interval cannot happen,
        whatever power the pack was seeded with), and the site export bound narrows further.  A
        contradictory configuration that leaves no physical window keeps the actuator's own limits
        and is reported infeasible rather than violating one of the two limits silently.  A peer
        whose flow is unknown makes the site bound unenforceable, so the pack keeps its own limits
        rather than allocating against an invented zero.
        """
        config = self.config
        low = -float(config.max_discharge_w)
        high = float(config.max_charge_w)
        infeasible = False
        energy = self._energy_kwh
        if self.mode == SIMULATED and energy is not None:
            if energy <= self._e_min():
                low = max(low, 0.0)  # at or below the reserve: charge only, never further discharge
            if energy >= self._e_max():
                high = min(high, 0.0)
        if low > high:
            # No physical window at all: the actuator's limits are the truth, and it is reported.
            low = -float(config.max_discharge_w)
            high = float(config.max_charge_w)
            infeasible = True
        realized = self.realized_power() if self.mode == SIMULATED else None
        if realized is not None:
            low = clamp(realized - RAMP_LIMIT_W, low, high)
            high = clamp(realized + RAMP_LIMIT_W, low, high)
        export_kw = max_export_kw(self.episode.settings)
        base = self.fleet.base_kw()
        other_w = self.fleet.other_power_w(self)
        # An unknown peer flow or site base means the bound cannot be evaluated at all: say so
        # instead of reporting an unchecked command as though the constraint had been cleared.
        unverified = export_kw > 0 and (base is None or other_w is None)
        if base is not None and other_w is not None and export_kw > 0:
            # site grid = base + other + p >= -export  =>  p >= -(export + base) - other
            required = -(export_kw + base) * 1000.0 - other_w
            if required > low:
                if required <= high:
                    low = required
                else:
                    # No point of the window can respect the site bound, so the interval collapses to
                    # the value nearest the grid window inside the physical envelope -- maximum
                    # charging here -- exactly as production's ``power_bounds_kw`` does, and the
                    # caller still learns it was impossible.
                    low = high
                    infeasible = True
        return low, high, infeasible, unverified

    def _eta(self, attribute: str) -> float:
        value = float(getattr(self.config, attribute))
        return value if value > 0 else 1.0

    def _e_min(self) -> float:
        return float(self.config.capacity_kwh) * float(self.config.min_soc_pct) / 100.0

    def _e_max(self) -> float:
        return float(self.config.capacity_kwh) * float(self.config.max_soc_pct) / 100.0


def plant_factory(episode: Episode, mode: str) -> Callable[[BatteryConfig], BatteryDriver]:
    """A driver factory for ``Engine(driver_factory=...)``; one plant driver per engine config.

    The drivers it creates share one :class:`PlantFleet`, so the site export bound and
    ``grid_kw()`` are site-wide rather than per pack.
    """
    if mode not in MODES:
        raise ValueError(f"unknown plant mode {mode!r}; have {', '.join(MODES)}")
    fleet = PlantFleet(episode)

    def create(config: BatteryConfig) -> BatteryDriver:
        return PlantDriver(episode, config, mode, fleet=fleet)

    return create
