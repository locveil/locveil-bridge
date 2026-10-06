import asyncio
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

from locveil_bridge.domain.scenarios.models import ScenarioDefinition, ScenarioState, DeviceState, ManualStep
from locveil_bridge.domain.scenarios.scenario import Scenario, ScenarioError
from locveil_bridge.domain.devices.service import DeviceManager
from locveil_bridge.domain.rooms.service import RoomManager
from locveil_bridge.domain.ports import StateRepositoryPort
from locveil_bridge.domain.topology.loader import load_topology
from locveil_bridge.domain.topology.models import Topology
from locveil_bridge.domain.scenarios.jobs import (
    JobPhase,
    JobRegistry,
    JobStep,
    STEP_DONE,
    STEP_FAILED,
    STEP_NOT_CONFIRMED,
    STEP_RUNNING,
    ScenarioJob,
    iso_utc,
    new_job_id,
    utc_now,
)
from locveil_bridge.domain.scenarios.reconciler import (
    DevicePreview,
    ExecutionResult,
    PlannedAction,
    ReconcilePlan,
    STEP_STARTED,
    build_forced_device_plan,
    build_plan,
    build_power_off_plan,
    build_reconcile_preview,
    deactivate_ceiling_ms,
    execute_plan,
    plan_ceiling_ms,
    resolve_targets,
    scenario_ceiling_ms,
)

logger = logging.getLogger(__name__)

class ScenarioManager:
    """
    Manager for scenario definitions, transitions, and state persistence.
    
    Responsibilities:
    - Load scenario definitions from JSON files
    - Track the currently active scenario
    - Manage transitions between scenarios
    - Persist and restore scenario state
    - Execute role-based actions in the current scenario
    """
    
    def __init__(self, 
                 device_manager: DeviceManager, 
                 room_manager: RoomManager,
                 state_repository: StateRepositoryPort,
                 scenario_dir: Path):
        """
        Initialize the scenario manager.
        
        Args:
            device_manager: Manager for accessing devices
            room_manager: Manager for room definitions
            store: State persistence store
            scenario_dir: Directory containing scenario JSON files
        """
        self.device_manager = device_manager
        self.room_manager = room_manager
        self.state_repository = state_repository
        self.scenario_dir = scenario_dir
        
        self.scenario_map: Dict[str, Scenario] = {}  # scenario_id -> Scenario
        self.scenario_definitions: Dict[str, ScenarioDefinition] = {}  # scenario_id -> definition
        # Rooms are the concurrency unit (SCN-6, canonical_first.md §3): each
        # scenario-bearing room has at most ONE active scenario, independent of the
        # others. room_id -> active Scenario.
        self.active: Dict[str, Scenario] = {}
        # Manual notes (e.g. "set Dodocus to LD") from the most recent activation/switch,
        # per room; cleared on shutdown/deactivate. Surfaced via get_scenario_state() so
        # the UI can display Dodocus prompts that movie_ld/vhs need for audio.
        self._activation_manual_steps: Dict[str, List[ManualStep]] = {}
        # scenario_id/filename -> reason, for scenarios skipped at load (Bug 2: never fatal).
        self.scenario_load_errors: Dict[str, str] = {}
        # Layer 0 topology (scenario transitions route through the reconciler).
        self.topology: Topology = Topology()
        # SCN-6: optional observer invoked with (room_id) after a room's active
        # scenario changes (switch/deactivate). Set by the composition root to the WB
        # card adapter's value-topic publisher; sync or async callables accepted.
        self.on_active_changed: Optional[Any] = None  # legacy single slot (the WB card adapter)
        self.active_changed_observers: List[Any] = []  # additional observers (SSE fan-out, ...)
        # SCN-19: scenario jobs — the per-room registry (running + last 20) and lock live
        # here, at the chokepoint every door passes through. Job events (started / phase /
        # step) leave through `job_observers`, called with (event_type, payload); the
        # terminal event rides the active-changed notification (the observer reads the
        # finishing job from the registry). Sync or async callables accepted.
        self.jobs = JobRegistry()
        self.job_observers: List[Any] = []
        self._job_tasks: Dict[str, asyncio.Task] = {}
    
    async def initialize(self) -> None:
        """
        Initialize the scenario manager by loading scenarios and restoring state.

        This method:
        1. Loads all scenario definitions from JSON files
        2. Creates Scenario instances for each definition
        3. Validates room membership for every scenario that declares `room_id`
        4. Attempts to restore the previously active scenario
        """
        # DEBUG: Log scenario manager initialization
        logger.debug("[SCENARIO_DEBUG] ScenarioManager.initialize() called")

        # Load all scenario definitions
        await self.load_scenarios()

        # Activate the room-membership invariant the schema has long promised
        # ("All devices must be in this room"). Hard-fails the bootstrap on mismatch --
        # a scenario whose devices live in a different room than the scenario declares
        # would silently misroute voice/UI commands. Catches typos, stale references,
        # and configs that drift out of sync with rooms.json.
        self._validate_room_membership()

        # Load the signal topology (Layer 0) used by the reconciler.
        self.topology = load_topology(self.scenario_dir.parent / "topology.json")
        logger.info(
            f"Loaded topology: {len(self.topology.links)} links, "
            f"{len(self.topology.ordering)} ordering edges"
        )

        # DEBUG: Log loaded scenarios
        logger.debug(f"[SCENARIO_DEBUG] Loaded scenarios: {list(self.scenario_map.keys())}")
        
        # Try to restore previous state
        await self._restore_state()
        
        logger.info("Scenario manager initialized")
    
    async def load_scenarios(self) -> None:
        """
        Load all scenario definitions from the scenarios directory.
        
        This reads all JSON files in the scenario_dir, validates them as
        ScenarioDefinition objects, and creates Scenario instances.
        """
        self.scenario_map.clear()
        self.scenario_definitions.clear()
        self.scenario_load_errors.clear()

        if not self.scenario_dir.exists():
            logger.warning(f"Scenarios directory does not exist: {self.scenario_dir}")
            return

        for scenario_file in self.scenario_dir.glob("*.json"):
            try:
                scenario_data = json.loads(scenario_file.read_text(encoding="utf-8"))
                definition = ScenarioDefinition.model_validate(scenario_data)

                # Create scenario instance
                scenario = Scenario(definition, self.device_manager)

                # Validate scenario configuration - this will raise ScenarioConfigurationError if invalid
                scenario.validate_configuration()

                # Only add to maps if validation passes
                self.scenario_definitions[definition.scenario_id] = definition
                self.scenario_map[definition.scenario_id] = scenario

                logger.info(f"Loaded and validated scenario: {definition.scenario_id}")
            except Exception as e:
                # Bug 2: a bad or currently-unavailable scenario must NOT bring down the whole
                # bridge (e.g. a referenced device that's off/unreachable at boot). Log it loudly
                # and skip it; the rest of the system still starts. (Catches JSON, Pydantic, and
                # scenario-configuration errors alike.)
                name = scenario_file.stem
                logger.error(f"Skipping scenario '{name}' — failed to load/validate: {str(e)}")
                self.scenario_load_errors[name] = str(e)
                continue

        if self.scenario_load_errors:
            logger.warning(
                f"Loaded {len(self.scenario_map)} scenario(s); skipped "
                f"{len(self.scenario_load_errors)}: {sorted(self.scenario_load_errors)}"
            )
        else:
            logger.info(f"Loaded {len(self.scenario_map)} scenarios")

    def _validate_room_membership(self) -> None:
        """Enforce the long-dormant `ScenarioDefinition.room_id` invariant: every device
        a scenario references must report the SAME room via `DevicePort.get_room()`.

        Activated 2026-06-08 as part of the room-refactor (single source of truth: each
        device declares its room via `config.room`; RoomManager + this validator both
        consume via the port). Hard-fails bootstrap on mismatch -- the only other shape
        means voice/UI would silently misroute commands.

        For each scenario with `room_id` set, walks the union of all device references:
          - `devices` (legacy explicit list)
          - `source` / `display` / `audio` (thin-scenario selection fields)
          - `roles` values
        Devices that aren't in `DeviceManager.devices` (e.g. PARKED ESP32-bridged sources
        like b215 / kuzma) are skipped -- their absence is handled by other validation.
        """
        violations: list[str] = []
        for sid, defn in self.scenario_definitions.items():
            if not defn.room_id:
                continue
            referenced: set[str] = set(defn.devices)
            for ref in (defn.source, defn.display, defn.audio):
                if ref:
                    referenced.add(ref)
            referenced.update(defn.roles.values())
            for did in sorted(referenced):
                device = self.device_manager.devices.get(did)
                if device is None:
                    continue
                dev_room = device.get_room() if hasattr(device, "get_room") else None
                if dev_room != defn.room_id:
                    violations.append(
                        f"scenario {sid!r} declares room_id={defn.room_id!r} but device "
                        f"{did!r} reports room={dev_room!r}"
                    )
        if violations:
            raise ScenarioError(
                "Scenario room-membership validation failed (each scenario's devices must "
                "live in the room the scenario declares):\n  - " + "\n  - ".join(violations),
                error_type="room_membership",
                critical=True,
            )
        logger.info(
            f"Scenario room-membership validation passed for "
            f"{sum(1 for d in self.scenario_definitions.values() if d.room_id)} scenarios."
        )

    def rooms_with_scenarios(self) -> List[str]:
        """Rooms that carry at least one loaded scenario (the Scenario Manager entity set)."""
        return sorted({d.room_id for d in self.scenario_definitions.values() if d.room_id})

    def active_in_room(self, room_id: str) -> Optional[Scenario]:
        """The room's active scenario, or None."""
        return self.active.get(room_id)

    def find_role_owner(self, role: str) -> Optional[Scenario]:
        """The single active scenario that binds `role`, or None (0 or >1 matches)."""
        matches = [sc for sc in self.active.values() if role in sc.definition.roles]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _room_key(room_id: str) -> str:
        return f"active_scenario:{room_id}"

    def _room_of(self, scenario: Scenario) -> str:
        """The scenario's room (validated non-None at load)."""
        room = scenario.definition.room_id
        assert room is not None  # enforced by Scenario.validate_configuration
        return room

    # --- SCN-19: scenario jobs (docs/design/scenarios/scenario_jobs.md) ---------------
    #
    # Every door — canonical `scenario.set/off`, REST switch/start/shutdown, the WB card,
    # force-reconcile — lands on `start_switch` / `start_stop` / `force_reconcile_device`.
    # Each creates ONE job per run of the room's chain, takes the room's lock non-blocking
    # (a second request while one runs is refused with ScenarioJobInProgress, never
    # queued), emits `scenario_job_started` and the teardown phase inside the caller's
    # request (before any device is touched), and runs the chain in its own task so a
    # `wait: false` caller returns at once and a `wait: true` caller simply awaits the
    # job. Phases are planned exactly when today's code planned them: the teardown at
    # acceptance, the activation after the teardown ran — so REL-3's ordering and the
    # `graceful: false` semantics cannot move.

    def _room_definitions(self, room: str) -> List[ScenarioDefinition]:
        return [d for d in self.scenario_definitions.values() if d.room_id == room]

    async def _emit_job_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Best-effort fan-out of a job event to the registered observers."""
        payload = {**payload, "timestamp": iso_utc(utc_now())}
        for observer in list(self.job_observers):
            try:
                result = observer(event_type, payload)
                if result is not None and hasattr(result, "__await__"):
                    await result
            except Exception as e:
                logger.error(f"job observer failed for {event_type}: {str(e)}")

    async def _accept_job(self, job: ScenarioJob) -> None:
        """Take the room's lock for `job` and announce it (`scenario_job_started`)."""
        await self.jobs.accept(job)
        logger.info(
            f"scenario job {job.job_id} accepted: {job.kind} → {job.target} in '{job.room_id}' "
            f"(from {job.from_}, source {job.source}, ceiling {job.max_duration_ms} ms)"
        )
        await self._emit_job_event("scenario_job_started", {
            "job_id": job.job_id,
            "room_id": job.room_id,
            "kind": job.kind,
            "target": job.target,
            "from": job.from_,
            "source": job.source,
            "max_duration_ms": job.max_duration_ms,
        })

    async def _add_phase(self, job: ScenarioJob, name: str, plan: ReconcilePlan) -> JobPhase:
        """Record a phase as it is planned and announce it (`scenario_phase`)."""
        phase = JobPhase(
            phase=name,
            steps=[
                JobStep(
                    index=i, device_id=a.device_id, domain=a.domain, target=a.target,
                    command=a.command, zone=a.zone, feedback=a.feedback,
                    poll_timeout_ms=a.poll_timeout_ms, delay_ms=a.delay_ms,
                    pre_delay_ms=a.pre_delay_ms,
                )
                for i, a in enumerate(plan.actions)
            ],
            manual_steps=[{"node": m.node, "instruction": m.instruction} for m in plan.manual_steps],
        )
        job.phases.append(phase)
        await self._emit_job_event("scenario_phase", {
            "job_id": job.job_id,
            "room_id": job.room_id,
            "phase": name,
            "steps": [st.planned() for st in phase.steps],
            "manual_steps": list(phase.manual_steps),
        })
        return phase

    def _step_observer(self, job: ScenarioJob, phase: JobPhase):
        """The executor hook for one phase: updates the step record and emits
        `scenario_step` at the two points (started, then the outcome)."""

        async def on_step(index: int, action: PlannedAction, status: str, error: Optional[str]) -> None:
            step = phase.steps[index]
            now = utc_now()
            if status == STEP_STARTED:
                step.status = STEP_RUNNING
                step.started_at = now
            else:
                step.status = {
                    STEP_DONE: STEP_DONE, STEP_FAILED: STEP_FAILED,
                    STEP_NOT_CONFIRMED: STEP_NOT_CONFIRMED,
                }.get(status, STEP_FAILED)
                step.finished_at = now
                step.error = error
            await self._emit_job_event("scenario_step", {
                "job_id": job.job_id,
                "room_id": job.room_id,
                "phase": phase.phase,
                "index": index,
                "device_id": action.device_id,
                "domain": action.domain,
                "target": action.target,
                "command": action.command,
                "zone": action.zone,
                "status": status,
                "error": error,
                "elapsed_ms": job.elapsed_ms(),
            })

        return on_step

    @staticmethod
    def _failures_of(*results: ExecutionResult) -> List[Dict[str, Any]]:
        return [
            {"device": a.device_id, "command": a.command, "error": err}
            for r in results for a, err in r.failures
        ]

    def _launch(self, job: ScenarioJob, coro: Any) -> ScenarioJob:
        task = asyncio.create_task(coro, name=f"scenario-job:{job.job_id}")
        self._job_tasks[job.room_id] = task
        task.add_done_callback(lambda t, room=job.room_id: self._job_tasks.pop(room, None)
                               if self._job_tasks.get(room) is t else None)
        return job

    async def _seal(self, job: ScenarioJob, room: str) -> None:
        """Finish the record, fire the terminal notification (the SSE observer reads the
        sealed job from the registry while it is still the room's running one), then
        release the lock — held from acceptance to the terminal event (§4)."""
        active = self.active.get(room)
        job.finish(active.scenario_id if active else "none")
        try:
            await self._notify_active_changed(room)
        finally:
            self.jobs.release(job)
        logger.info(
            f"scenario job {job.job_id} {job.state} in {job.duration_ms} ms "
            f"({len(job.failures)} failed step(s))"
        )

    def _job_result(self, job: ScenarioJob) -> Dict[str, Any]:
        return {
            "success": job.succeeded,
            "powered_off": list(job.powered_off),
            "failures": list(job.failures),
            "job_id": job.job_id,
            "no_op": False,
        }

    async def start_switch(self, target_id: str, *, graceful: bool = True,
                           source: str = "rest") -> Optional[ScenarioJob]:
        """Accept a switch job for the target's room and start its chain.

        Returns the running job, or None when the target is already the room's active
        scenario (no job, no event — the `no_op` answer). Raises ValueError for an
        unknown scenario and ScenarioJobInProgress when the room is busy.
        """
        logger.debug(f"[SCENARIO_DEBUG] start_switch called: target_id={target_id}, graceful={graceful}")
        if target_id not in self.scenario_map:
            raise ValueError(f"Scenario '{target_id}' not found")

        incoming = self.scenario_map[target_id]
        room = self._room_of(incoming)
        outgoing = self.active.get(room)
        logger.debug(f"[SCENARIO_DEBUG] Transition in room '{room}': outgoing={outgoing.scenario_id if outgoing else 'None'}, incoming={incoming.scenario_id}")

        if outgoing and outgoing.scenario_id == incoming.scenario_id and self.jobs.running(room) is None:
            logger.info(f"Scenario '{target_id}' is already active")
            return None

        devices = self.device_manager.devices
        job = ScenarioJob(
            job_id=new_job_id(room), room_id=room, kind="switch", target=target_id,
            from_=outgoing.scenario_id if outgoing else "none", source=source,
            max_duration_ms=scenario_ceiling_ms(
                incoming.definition, self._room_definitions(room), self.topology, devices
            ),
        )
        await self._accept_job(job)

        logger.info(
            f"Switching room '{room}' from '{outgoing.scenario_id if outgoing else 'None'}' "
            f"to '{incoming.scenario_id}' (job {job.job_id})"
        )
        try:
            # The teardown is planned at acceptance, from believed state before anything
            # runs — exactly as _switch_via_reconciler did.
            incoming_involved = resolve_targets(incoming.definition, self.topology)[2]
            outgoing_involved = self._involved_devices(outgoing) if outgoing else set()
            to_power_off = (outgoing_involved - incoming_involved) if graceful else outgoing_involved
            job.powered_off = sorted(to_power_off)
            teardown_plan = build_power_off_plan(job.powered_off, devices)
            teardown_phase = await self._add_phase(job, "teardown", teardown_plan)
        except Exception:
            self.jobs.release(job)
            raise
        return self._launch(job, self._run_switch(job, room, incoming, teardown_plan, teardown_phase))

    async def _run_switch(self, job: ScenarioJob, room: str, incoming: Scenario,
                          teardown_plan: ReconcilePlan, teardown_phase: JobPhase) -> None:
        devices = self.device_manager.devices
        try:
            teardown = await execute_plan(
                teardown_plan, devices, on_step=self._step_observer(job, teardown_phase)
            )
            # The activation is planned AFTER the teardown ran (its diff reads the state
            # the teardown left — the `graceful: false` semantics depend on this order).
            activation_plan = build_plan(incoming.definition, self.topology, devices)
            activation_phase = await self._add_phase(job, "activation", activation_plan)
            activation = await execute_plan(
                activation_plan, devices, on_step=self._step_observer(job, activation_phase)
            )

            self.active[room] = incoming
            # Capture the activation's manual notes; get_scenario_state() threads them into the
            # live recompute so /scenario/state surfaces them (single source of truth).
            self._activation_manual_steps[room] = [
                ManualStep(node=m.node, instruction=m.instruction) for m in activation.manual_steps
            ]
            await self._persist_state(room)

            job.failures = self._failures_of(teardown, activation)
            if job.failures:
                logger.warning(
                    f"Scenario '{incoming.scenario_id}' activated with {len(job.failures)} failed step(s); "
                    f"correct affected devices via their UI page"
                )
            logger.info(f"Successfully switched to scenario '{incoming.scenario_id}' (reconciler)")
        except asyncio.CancelledError:
            # Process shutdown: the executor stopped at a step boundary; no terminal event.
            self.jobs.release(job)
            raise
        except Exception as e:
            logger.error(f"Error switching to scenario '{incoming.scenario_id}': {str(e)}", exc_info=True)
            job.failures.append({"device": "scenario_manager", "command": "switch", "error": str(e)})
        await self._seal(job, room)

    async def switch_scenario(self, target_id: str, *, graceful: bool = True,
                              source: str = "rest") -> Dict[str, Any]:
        """
        Perform a diff-based transition via the reconciler, scoped to the target
        scenario's ROOM (rooms are the concurrency unit — another room's active
        scenario is untouched), and WAIT for it: the `wait: true` door.

        Devices involved in the room's outgoing but not the incoming scenario are
        powered off; the incoming activity is then reconciled from topology +
        capabilities + assumed state (shared devices are left running and only
        re-targeted).

        Args:
            target_id: ID of the scenario to switch to
            graceful: If True (default), power off only outgoing-only devices;
                      if False, power off every outgoing device before activation
            source: who asked — `canonical` | `rest` | `wb_card` (the job record)

        Returns `{success, powered_off, failures, job_id, no_op}`.

        Raises:
            ValueError: If the target scenario doesn't exist
            ScenarioJobInProgress: a job is already running in the room
        """
        job = await self.start_switch(target_id, graceful=graceful, source=source)
        if job is None:
            return {"success": True, "powered_off": [], "failures": [], "job_id": None, "no_op": True}
        await job.done.wait()
        return self._job_result(job)

    def _involved_devices(self, scenario: Scenario) -> set:
        """Devices a scenario touches, derived from the topology."""
        return resolve_targets(scenario.definition, self.topology)[2]

    async def start_stop(self, room_id: str, *, source: str = "rest") -> Optional[ScenarioJob]:
        """Accept a stop job for the room (power its active scenario down) and start it.

        Returns None when the room is idle and no job runs (`no_op`). Raises
        ScenarioJobInProgress when a job is running in the room.
        """
        sc = self.active.get(room_id)
        if sc is None and self.jobs.running(room_id) is None:
            return None
        devices = self.device_manager.devices
        job = ScenarioJob(
            job_id=new_job_id(room_id), room_id=room_id, kind="stop", target="none",
            from_=sc.scenario_id if sc else "none", source=source,
            max_duration_ms=deactivate_ceiling_ms(
                self._room_definitions(room_id), self.topology, devices
            ),
        )
        await self._accept_job(job)
        assert sc is not None  # the room was busy otherwise, and accept() raised
        logger.info(f"Deactivating scenario '{sc.scenario_id}' in '{room_id}' (powering off its devices; job {job.job_id})")
        try:
            involved = sorted(resolve_targets(sc.definition, self.topology)[2])
            job.powered_off = involved
            plan = build_power_off_plan(involved, devices)
            phase = await self._add_phase(job, "teardown", plan)
        except Exception:
            self.jobs.release(job)
            raise
        return self._launch(job, self._run_stop(job, room_id, sc, plan, phase))

    async def _run_stop(self, job: ScenarioJob, room: str, sc: Scenario,
                        plan: ReconcilePlan, phase: JobPhase) -> None:
        devices = self.device_manager.devices
        try:
            exec_result = await execute_plan(plan, devices, on_step=self._step_observer(job, phase))
            job.failures = self._failures_of(exec_result)
        except asyncio.CancelledError:
            self.jobs.release(job)
            raise
        except Exception as e:
            logger.error(f"Error deactivating scenario '{sc.scenario_id}': {str(e)}")
            job.failures.append({"device": "scenario_manager", "command": "stop", "error": str(e)})
        finally:
            self.active.pop(room, None)
            self._activation_manual_steps.pop(room, None)
            # Clear the persisted intent atomically with the in-memory clear — otherwise a
            # bridge restart resurrects the deactivated scenario via _restore_state and powers
            # the gear back on. deactivate() ONLY: process shutdown() deliberately leaves the
            # keys so still-active scenarios survive a restart.
            try:
                await self.state_repository.delete(self._room_key(room))
            except Exception as e:
                logger.error(f"Failed to clear persisted active scenario for '{room}': {str(e)}")
        await self._seal(job, room)

    # --- SCN-11: per-device force-reconcile (user-mediated desync repair) --------

    def _active_scenario_by_id(self, scenario_id: str) -> Optional[Scenario]:
        for sc in self.active.values():
            if sc.scenario_id == scenario_id:
                return sc
        return None

    def reconcile_preview(self, scenario_id: str) -> List[DevicePreview]:
        """Believed-vs-desired rows for every device the ACTIVE scenario involves.

        Active-only by design: the desired state is defined by the *running* scenario
        (on an inactive scenario the same gesture is just "start it"). Raises
        ScenarioError('not_active') otherwise.
        """
        scenario = self._active_scenario_by_id(scenario_id)
        if scenario is None:
            raise ScenarioError(
                f"Scenario '{scenario_id}' is not active", "not_active", False
            )
        return build_reconcile_preview(
            scenario.definition, self.topology, self.device_manager.devices
        )

    async def force_reconcile_device(
        self, scenario_id: str, device_id: str, *, source: str = "rest"
    ) -> Tuple[ReconcilePlan, ExecutionResult, ScenarioJob]:
        """Force ONE device into the active scenario's desired state (SCN-11).

        Builds the single-device forced plan (diff skipped, ``force`` injected,
        toggles claim their target via ``assume_state``) and runs it through the
        normal executor — per-capability gates and polls included. The user picking
        the row is the feedback channel the optimistic model lacks.
        """
        scenario = self._active_scenario_by_id(scenario_id)
        if scenario is None:
            raise ScenarioError(
                f"Scenario '{scenario_id}' is not active", "not_active", False
            )
        devices = self.device_manager.devices
        plan = build_forced_device_plan(
            scenario.definition, self.topology, devices, device_id
        )
        if not plan.actions:
            reason = "; ".join(plan.warnings) or f"no reconcilable actions for '{device_id}'"
            raise ScenarioError(reason, "nothing_to_force", False)
        logger.info(
            "force-reconcile '%s' in scenario '%s': %d action(s)",
            device_id, scenario_id, len(plan.actions),
        )
        # SCN-19: a one-device forced plan is a plan — it runs as a `reconcile` job under
        # the room's lock (one phase, `activation`), terminal event `scenario_switched`.
        room = self._room_of(scenario)
        job = ScenarioJob(
            job_id=new_job_id(room), room_id=room, kind="reconcile", target=scenario_id,
            from_=scenario_id, source=source, max_duration_ms=plan_ceiling_ms(plan.actions),
        )
        await self._accept_job(job)
        try:
            phase = await self._add_phase(job, "activation", plan)
        except Exception:
            self.jobs.release(job)
            raise
        holder: Dict[str, ExecutionResult] = {}
        self._launch(job, self._run_reconcile(job, room, plan, phase, holder))
        await job.done.wait()
        return plan, holder.get("result", ExecutionResult(failures=[])), job

    async def _run_reconcile(self, job: ScenarioJob, room: str, plan: ReconcilePlan,
                             phase: JobPhase, holder: Dict[str, ExecutionResult]) -> None:
        try:
            result = await execute_plan(
                plan, self.device_manager.devices, on_step=self._step_observer(job, phase)
            )
            holder["result"] = result
            job.failures = self._failures_of(result)
        except asyncio.CancelledError:
            self.jobs.release(job)
            raise
        except Exception as e:
            logger.error(f"Error force-reconciling in '{room}': {str(e)}", exc_info=True)
            job.failures.append({"device": "scenario_manager", "command": "reconcile", "error": str(e)})
        await self._seal(job, room)

    async def execute_role_action(self, role: str, command: str, params: Dict[str, Any]) -> Any:
        """
        Execute an action on a device bound to a role in the current scenario.
        
        Args:
            role: The role name as defined in the current scenario
            command: The command to execute
            params: Parameters to pass to the command
            
        Returns:
            Dict[str, Any]: The result returned by the device command
            
        Raises:
            ScenarioError: If no scenario is active or the role is invalid
        """
        # DEBUG: Log role action execution
        logger.debug(f"[SCENARIO_DEBUG] execute_role_action called: role={role}, command={command}, params={params}, active={sorted(sc.scenario_id for sc in self.active.values())}")
        
        if not self.active:
            raise ScenarioError("No scenario is currently active", "no_active_scenario", True)

        matches = [sc for sc in self.active.values() if role in sc.definition.roles]
        if not matches:
            raise ScenarioError(
                f"Role '{role}' not defined in any active scenario", "invalid_role", True
            )
        if len(matches) > 1:
            raise ScenarioError(
                f"Role '{role}' is bound in {len(matches)} active scenarios "
                f"({', '.join(sorted(sc.scenario_id for sc in matches))}); target the room's "
                f"scenario manager instead",
                "ambiguous_role",
                True,
            )
        scenario = matches[0]

        try:
            # DEBUG: Log before executing role action
            logger.debug(f"[SCENARIO_DEBUG] Executing role action on scenario {scenario.scenario_id}: {role}.{command}")

            result = await scenario.execute_role_action(role, command, **params)

            # DEBUG: Log role action result
            logger.debug(f"[SCENARIO_DEBUG] Role action result: {result}")

            return result
        except Exception as e:
            logger.error(f"Error executing role action {role}.{command}: {str(e)}")
            raise
    
    async def _notify_active_changed(self, room_id: str) -> None:
        """Best-effort observer calls after a room's active scenario changed.

        This is the single notification chokepoint for EVERY activation path
        (REST, canonical scenario.set, restore, deactivate) — observers must
        not assume which path fired it.
        """
        observers = ([self.on_active_changed] if self.on_active_changed else []) + list(
            self.active_changed_observers
        )
        for observer in observers:
            try:
                result = observer(room_id)
                if result is not None and hasattr(result, "__await__"):
                    await result
            except Exception as e:
                logger.error(f"active-changed observer failed for '{room_id}': {str(e)}")

    async def _persist_state(self, room_id: str) -> None:
        """
        Persist the room's active scenario under its per-room key.
        """
        scenario = self.active.get(room_id)
        if scenario:
            await self.state_repository.save(
                self._room_key(room_id),
                {"scenario_id": scenario.scenario_id},
            )

    @staticmethod
    def _stored_scenario_id(stored: Any) -> Optional[str]:
        """Extract a scenario_id from a persisted value. Dict envelope is the current
        shape; tolerate the legacy bare-string form for one upgrade."""
        if isinstance(stored, dict):
            return stored.get("scenario_id")
        if isinstance(stored, str):
            return stored
        return None

    async def _restore_state(self) -> None:
        """
        Restore each room's previously active scenario — TRACKING ONLY (SCN-18).

        Restore marks the scenario active (state, persistence, WB card, SSE) and
        executes NOTHING: no reconcile plan, zero device commands. Owner decision
        2026-07-15 off the 2026-07-14 incident, where a redeploy's boot-restore
        re-ran the full `movie_appletv` cold-start plan unattended and wedged the
        eMotiva. The reasoning generalizes: when executing at boot would be safe
        (crash mid-activity — devices still in the believed state) the plan is a
        no-op anyway, and when it would DO something (deploy, power-outage
        recovery) unattended hardware actuation is exactly what must not happen.
        Drift shows honestly on the scenario page (SCN-11 out-of-sync dialog) and
        reconciles on demand via force-reconcile.

        Reads the per-room keys (`active_scenario:<room_id>`); additionally performs a
        ONE-SHOT migration of the legacy global `active_scenario` key (pre-SCN-6
        bridges persisted a single slot): its scenario restores into its room unless
        that room already has a per-room key, and the legacy key is deleted either way.
        """
        # DEBUG: Log state restoration attempt
        logger.debug("[SCENARIO_DEBUG] _restore_state() called")

        targets: Dict[str, str] = {}  # room_id -> scenario_id

        # Legacy one-shot migration (lowest precedence).
        try:
            legacy = await self.state_repository.load("active_scenario")
            legacy_id = self._stored_scenario_id(legacy)
            if legacy_id and legacy_id in self.scenario_map:
                legacy_room = self._room_of(self.scenario_map[legacy_id])
                targets[legacy_room] = legacy_id
                logger.info(
                    f"Migrating legacy 'active_scenario' key → '{self._room_key(legacy_room)}' "
                    f"({legacy_id})"
                )
            if legacy is not None:
                await self.state_repository.delete("active_scenario")
        except Exception as e:
            logger.error(f"Error migrating legacy active scenario key: {str(e)}")

        # Per-room keys (highest precedence).
        for room_id in self.rooms_with_scenarios():
            try:
                stored = await self.state_repository.load(self._room_key(room_id))
                scenario_id = self._stored_scenario_id(stored)
                if scenario_id and scenario_id in self.scenario_map:
                    targets[room_id] = scenario_id
            except Exception as e:
                logger.error(f"Error loading active scenario for room '{room_id}': {str(e)}")

        for room_id, scenario_id in sorted(targets.items()):
            logger.info(
                f"Restoring previously active scenario in '{room_id}': {scenario_id} "
                f"(tracking only — no device commands at boot)"
            )
            try:
                await self._restore_tracking(room_id, self.scenario_map[scenario_id])
                # DEBUG: Log successful restoration
                logger.debug(f"[SCENARIO_DEBUG] Successfully restored scenario: {scenario_id}")
            except Exception as e:
                logger.error(f"Error restoring scenario {scenario_id}: {str(e)}")

    async def _restore_tracking(self, room: str, incoming: Scenario) -> None:
        """Mark ``incoming`` active in ``room`` WITHOUT executing anything (SCN-18).

        The state-setting half of a switch — active map, persistence, WB card +
        SSE observers — with zero device dispatch. No activation ran, so there
        are no activation manual steps to surface."""
        self.active[room] = incoming
        self._activation_manual_steps[room] = []
        await self._persist_state(room)
        await self._notify_active_changed(room)
    
    async def deactivate(self, room_id: Optional[str] = None, *,
                         source: str = "rest") -> Dict[str, Any]:
        """Deactivate a room's active scenario by powering off the devices it involves,
        and WAIT for it (the `wait: true` door of `start_stop`).

        This is the explicit user action ("turn it all off", via POST /scenario/shutdown or
        the room's Scenario Manager `scenario.off`) and is DISTINCT from process
        ``shutdown()``: it intentionally drives the hardware off.

        Args:
            room_id: The room to deactivate. None = deactivate EVERY room's active
                     scenario (whole-house off) — one job per room, in turn.
            source: who asked (the job record).

        Returns `{success, powered_off, manual_steps, failures, job_id, no_op}`; for the
        whole-house form `job_id` is the last room's. Raises ScenarioJobInProgress when a
        job is running in the room.
        """
        rooms = [room_id] if room_id is not None else sorted(self.active)
        result: Dict[str, Any] = {
            "success": True, "powered_off": [], "manual_steps": [], "failures": [],
            "job_id": None, "no_op": True,
        }
        for room in rooms:
            job = await self.start_stop(room, source=source)
            if job is None:
                continue
            await job.done.wait()
            result["no_op"] = False
            result["job_id"] = job.job_id
            result["powered_off"].extend(job.powered_off)
            result["failures"].extend(job.failures)
            result["success"] = result["success"] and job.succeeded
        return result

    async def wait_for_jobs(self, extra_s: float = 5.0) -> bool:
        """Block until every running job reached its terminal event, bounded by the
        largest running ceiling plus `extra_s` (POST /reload, §4). Returns False when
        the bound fired with a job still running."""
        running = self.jobs.running_jobs()
        if not running:
            return True
        bound = max(j.max_duration_ms for j in running) / 1000 + extra_s
        logger.info(f"waiting for {len(running)} running scenario job(s), up to {bound:.0f} s")
        try:
            await asyncio.wait_for(
                asyncio.gather(*(j.done.wait() for j in running)), timeout=bound
            )
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "scenario job(s) still running after the bound; proceeding: "
                + ", ".join(j.job_id for j in self.jobs.running_jobs())
            )
            return False

    async def shutdown(self) -> None:
        """Process shutdown: stop tracking active scenarios WITHOUT touching the hardware.

        Restarting/stopping the bridge must NOT power down the user's AV gear — the scenarios
        stay active on the devices and the assumed state is preserved across the restart. Use
        ``deactivate()`` for the explicit "turn it off" action.
        """
        # SCN-19: a running job never reaches its terminal event across a process
        # shutdown — the executor stops at a step boundary (§4 "not held across shutdown").
        for room, task in list(self._job_tasks.items()):
            if not task.done():
                logger.info(f"Bridge shutdown: cancelling the running scenario job in '{room}'")
                task.cancel()
        self._job_tasks.clear()
        for room, sc in self.active.items():
            logger.info(
                f"Bridge shutdown: leaving scenario '{sc.scenario_id}' active in '{room}' "
                f"on the hardware (call deactivate() to power off)"
            )
        self.active.clear()
        self._activation_manual_steps.clear()

    def get_scenario_state(self, scenario_id: str) -> ScenarioState:
        """
        Get the state of a specific scenario.
        
        Args:
            scenario_id: ID of the scenario to get state for
            
        Returns:
            ScenarioState: Current state if scenario is active, or basic state with empty devices if inactive
            
        Raises:
            ValueError: If scenario_id doesn't exist
        """
        # Check if scenario exists
        if scenario_id not in self.scenario_map:
            raise ValueError(f"Scenario '{scenario_id}' not found")
        
        # For an active scenario: read each device's LIVE state fresh on every call. The single
        # source of truth is device.get_current_state(); we hold no snapshot. manual_steps are
        # activation-scoped (not derivable from device state) and threaded in from the room's
        # slot in self._activation_manual_steps so transition notes (Dodocus hub, "press Play",
        # etc.) survive every query. See ui_backend_contract.md "Scenario state binding".
        scenario = self.scenario_map[scenario_id]
        room = self._room_of(scenario)
        active = self.active.get(room)
        if active and active.scenario_id == scenario_id:
            device_states: Dict[str, DeviceState] = {}
            for dev_id in active.definition.devices:
                device = self.device_manager.get_device(dev_id)
                if device:
                    device_states[dev_id] = self._convert_device_state(device.get_current_state())
            return ScenarioState(
                scenario_id=scenario_id,
                devices=device_states,
                manual_steps=list(self._activation_manual_steps.get(room, [])),
            )

        # For inactive scenarios, return a basic state without device states
        # since we can't get real-time device states for inactive scenarios
        return ScenarioState(
            scenario_id=scenario_id,
            devices={}  # Empty device states for inactive scenarios
        )
    
    def _convert_device_state(self, state) -> DeviceState:
        """
        Convert a device's Pydantic state model to a standardized DeviceState.
        
        This reuses the safe field access logic from the Scenario class
        to ensure consistent handling across the system.
        
        Args:
            state: Pydantic device state model (e.g., LgTvState, EmotivaXMC2State)
            
        Returns:
            DeviceState: Standardized state representation
        """
        # Extract and convert power state using the shared safe field access
        power_value = None
        raw_power = Scenario._safe_get_device_field(state, "power")
        if raw_power is not None:
            if isinstance(raw_power, bool):
                power_value = raw_power
            elif isinstance(raw_power, str):
                # Convert string power states to boolean
                power_value = raw_power.lower() in ("on", "true", "1", "powered_on", "active")

        # Extract input using safe field access (handles variations automatically)
        input_value = Scenario._safe_get_device_field(state, "input")

        # Build extra dict with all other fields, excluding base fields and already mapped ones
        base_fields = {"device_id", "device_name", "last_command", "error"}
        mapped_fields = {"power", "input", "input_source", "video_input", "audio_input"}
        excluded_fields = base_fields | mapped_fields

        extra = {}
        if hasattr(state, "model_dump"):
            # Get all fields from the Pydantic model
            all_fields = state.model_dump()
            for field_name, field_value in all_fields.items():
                if field_name not in excluded_fields and field_value is not None:
                    extra[field_name] = field_value

        return DeviceState(
            power=power_value,
            input=input_value,
            extra=extra
        )