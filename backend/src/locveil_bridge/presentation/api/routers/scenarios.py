import logging
from typing import Dict, Any, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from locveil_bridge.domain.scenarios.models import ScenarioDefinition
from locveil_bridge.domain.scenarios.reconciler import plan_ceiling_ms
from locveil_bridge.domain.scenarios.scenario import ScenarioError, ScenarioExecutionError
from locveil_bridge.domain.scenarios.service import ScenarioManager
from locveil_bridge.domain.rooms.service import RoomManager

from locveil_bridge.presentation.api.layout_engine import build_scenario_manifest
from locveil_bridge.presentation.api.layout_manifest import LayoutManifest

# Note: the module-level mqtt_client global is typed Any deliberately --
# importing the concrete MQTTClient class from infrastructure here would
# add a presentation→infrastructure edge (only the system.py /reload one
# is currently allowed; see CONTRIBUTING + the import-linter contracts).
# The mqtt_client global is unused by any handler in this router; it's
# kept on initialize() for symmetry with other routers.

# Create logger for this module
logger = logging.getLogger(__name__)

# Create router with appropriate prefix and tags
router = APIRouter(
    tags=["Scenarios"]
)

# Global references that will be set during initialization
scenario_manager: Optional[ScenarioManager] = None
room_manager: Optional[RoomManager] = None
mqtt_client: Any = None  # see header note re: presentation→infrastructure edge


def initialize(scenario_mgr: ScenarioManager, room_mgr: RoomManager, mqt_client: Any) -> None:
    """Initialize global references needed by router endpoints."""
    global scenario_manager, room_manager, mqtt_client
    scenario_manager = scenario_mgr
    room_manager = room_mgr
    mqtt_client = mqt_client


def _require_scenario_manager() -> ScenarioManager:
    """Pyright-narrowing accessor: every handler dereferences scenario_manager.
    Globally typed Optional; this raises a 503 if the bootstrap forgot to wire
    the manager (shouldn't happen at runtime; safety + narrowing both)."""
    if scenario_manager is None:
        raise HTTPException(status_code=503, detail="Scenario manager not initialized")
    return scenario_manager

# Request and response models
class SwitchScenarioRequest(BaseModel):
    """Request model for switching scenarios."""
    id: str
    graceful: bool = True

class ActionRequest(BaseModel):
    """Request model for executing a role action."""
    role: str
    command: str
    params: Dict[str, Any] = {}

class ScenarioResponse(BaseModel):
    """Base response model for scenario operations.

    Manual notes from the activation (e.g. "set the Dodocus to LD") are NOT on this
    response — they live on ``ScenarioState.manual_steps`` (single source of truth, fetched
    via ``GET /scenario/state``; survives page reload).
    """
    status: str
    message: str

class StartScenarioRequest(BaseModel):
    """Request model for starting a scenario."""
    id: str

class ShutdownScenarioRequest(BaseModel):
    """Request model for shutting down a scenario."""
    id: str
    graceful: bool = True


# --- SCN-11: per-device force-reconcile DTOs ---------------------------------

class ReconcileDomainComparison(BaseModel):
    """Believed vs desired for one capability domain of one device. `believed` is the
    bridge's optimistic state — it may be WRONG, which is the whole reason the dialog
    exists; the user standing in the room is the missing feedback channel."""
    domain: str
    believed: Any = None
    desired: Any = None
    in_sync: bool

class ReconcilePlanStep(BaseModel):
    """One step of the forced chain a confirm would run (shown in the expanded row)."""
    command: str
    domain: str
    target: Any = None
    pre_delay_ms: int = 0
    delay_ms: int = 0
    poll_timeout_ms: Optional[int] = None
    feedback: bool = False

class ReconcilePreviewRow(BaseModel):
    """One device row of the force-reconcile dialog. NB `in_sync: true` rows are
    exactly where force matters — "in sync" only means the *believed* state matches."""
    device_id: str
    device_name: str
    comparisons: List[ReconcileDomainComparison]
    in_sync: bool
    reconcilable: bool
    steps: List[ReconcilePlanStep]
    eta_ms: int

class ReconcilePreviewResponse(BaseModel):
    scenario_id: str
    devices: List[ReconcilePreviewRow]

class ForceReconcileRequest(BaseModel):
    device_id: str

class ForceReconcileFailure(BaseModel):
    command: str
    error: str

class ForceReconcileResponse(BaseModel):
    success: bool
    device_id: str
    executed: List[ReconcilePlanStep]
    failures: List[ForceReconcileFailure]

def check_initialized():
    """Check if the router is properly initialized with required dependencies."""
    if not scenario_manager:
        raise HTTPException(
            status_code=503, 
            detail="Scenario service not fully initialized"
        )

@router.get("/scenario/definition/{id}", response_model=ScenarioDefinition)
async def get_scenario_definition(id: str):
    """
    Get the definition of a specific scenario.
    
    Returns:
        ScenarioDefinition: The scenario definition
        
    Raises:
        HTTPException: If scenario not found or service not initialized
    """
    check_initialized()
    assert scenario_manager is not None  # narrowed by check_initialized() above
    
    if id not in scenario_manager.scenario_definitions:
        raise HTTPException(status_code=404, detail=f"Scenario '{id}' not found")

    return scenario_manager.scenario_definitions[id]


@router.get("/scenario/{id}/layout", response_model=LayoutManifest, response_model_exclude_none=True)
async def get_scenario_layout(id: str):
    """Layer-3 layout manifest for a scenario — the composite remote (one renderer;
    controls carry their canonical capability/action and dispatch against the manifest's
    `canonicalEntityId`; the power zone is the scenario lifecycle). Built from the
    scenario definition + the role devices' capability maps by the placement engine. The
    `inputs` role is intentionally not rendered (reconciler-derived)."""
    check_initialized()
    assert scenario_manager is not None  # narrowed by check_initialized() above
    sdef = scenario_manager.scenario_definitions.get(id)
    if sdef is None:
        raise HTTPException(status_code=404, detail=f"Scenario '{id}' not found")
    try:
        return build_scenario_manifest(sdef, scenario_manager.device_manager)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to build scenario layout manifest: {e}")

def _plan_step(action: Any) -> ReconcilePlanStep:
    return ReconcilePlanStep(
        command=action.command,
        domain=action.domain,
        target=action.target,
        pre_delay_ms=action.pre_delay_ms,
        delay_ms=action.delay_ms,
        poll_timeout_ms=action.poll_timeout_ms,
        feedback=action.feedback,
    )


def _eta_ms(actions: List[Any]) -> int:
    """Worst-case wall clock for a forced chain — the domain's one ceiling formula
    (shared with the catalog's scenario `max_duration_ms`, VWB-46)."""
    return plan_ceiling_ms(actions)


@router.get("/scenario/{id}/reconcile_preview", response_model=ReconcilePreviewResponse)
async def get_reconcile_preview(id: str):
    """Believed-vs-desired state per involved device of the ACTIVE scenario, plus the
    forced chain a confirm would run. 404 unknown scenario; 409 when it isn't the active
    one (the desired state is only defined by the running scenario — on an inactive page
    the same gesture is just "start it")."""
    mgr = _require_scenario_manager()
    if id not in mgr.scenario_definitions:
        raise HTTPException(status_code=404, detail=f"Scenario '{id}' not found")
    try:
        previews = mgr.reconcile_preview(id)
    except ScenarioError as e:
        raise HTTPException(status_code=409, detail=str(e))

    rows: List[ReconcilePreviewRow] = []
    for p in previews:
        device = mgr.device_manager.devices.get(p.device_id)
        name_fn = getattr(device, "get_name", None)
        rows.append(ReconcilePreviewRow(
            device_id=p.device_id,
            device_name=str(name_fn()) if callable(name_fn) else p.device_id,
            comparisons=[
                ReconcileDomainComparison(
                    domain=c.domain, believed=c.believed, desired=c.desired, in_sync=c.in_sync
                )
                for c in p.comparisons
            ],
            in_sync=p.in_sync,
            reconcilable=bool(p.plan.actions),
            steps=[_plan_step(a) for a in p.plan.actions],
            eta_ms=_eta_ms(p.plan.actions),
        ))
    return ReconcilePreviewResponse(scenario_id=id, devices=rows)


@router.post("/scenario/{id}/force_reconcile", response_model=ForceReconcileResponse)
async def force_reconcile_device(id: str, data: ForceReconcileRequest):
    """Force ONE device into the active scenario's desired state — the
    believed-vs-desired diff is skipped (the belief may be wrong; the user picking the
    row is the feedback channel), driver idempotence guards are bypassed via the
    reserved `force` param, and toggle power claims the plan target (`assume_state`).
    Runs the device's chain through the normal executor (gates + polls); worst case a
    poll-timeout wait, so the call can take seconds."""
    mgr = _require_scenario_manager()
    if id not in mgr.scenario_definitions:
        raise HTTPException(status_code=404, detail=f"Scenario '{id}' not found")
    try:
        plan, result = await mgr.force_reconcile_device(id, data.device_id)
    except ScenarioError as e:
        status = 409 if e.error_type == "not_active" else 404
        raise HTTPException(status_code=status, detail=str(e))

    return ForceReconcileResponse(
        success=result.success,
        device_id=data.device_id,
        executed=[_plan_step(a) for a in result.executed],
        failures=[
            ForceReconcileFailure(command=a.command, error=err)
            for a, err in result.failures
        ],
    )


@router.post("/scenario/switch", response_model=ScenarioResponse)
async def switch_scenario(data: SwitchScenarioRequest):
    """
    Switch to a different scenario.
    
    This endpoint performs a transition between scenarios, handling
    device state changes efficiently.
    
    Args:
        data: The switch scenario request with scenario ID and graceful flag
        
    Returns:
        ScenarioResponse: Status of the operation
        
    Raises:
        HTTPException: If scenario not found or an error occurs
    """
    check_initialized()
    assert scenario_manager is not None  # narrowed by check_initialized() above
    
    try:
        await scenario_manager.switch_scenario(data.id, graceful=data.graceful)


        return ScenarioResponse(
            status="success",
            message=f"Successfully switched to scenario '{data.id}'",
        )
    except ValueError as e:
        # Scenario not found
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        # Log the full error with traceback for server logs
        logger.error(f"Error switching to scenario {data.id}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to switch scenario: {str(e)}")

@router.post("/scenario/start", response_model=ScenarioResponse)
async def start_scenario(data: StartScenarioRequest):
    """
    Start a scenario if no scenario is currently active.
    
    This endpoint starts a scenario only if there is no currently active scenario.
    If a scenario is already running, it returns an error.
    
    Args:
        data: The start scenario request with scenario ID
        
    Returns:
        ScenarioResponse: Status of the operation
        
    Raises:
        HTTPException: If scenario not found, another scenario is active, or an error occurs
    """
    check_initialized()
    assert scenario_manager is not None  # narrowed by check_initialized() above
    
    # Check if scenario exists
    if data.id not in scenario_manager.scenario_definitions:
        raise HTTPException(status_code=404, detail=f"Scenario '{data.id}' not found")
    
    # Check if another scenario is already active IN THE TARGET SCENARIO'S ROOM
    # (rooms are the concurrency unit — another room's scenario doesn't block).
    room_id = scenario_manager.scenario_definitions[data.id].room_id
    active = scenario_manager.active_in_room(room_id) if room_id else None
    if active:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot start scenario '{data.id}': scenario '{active.scenario_id}' is already active in room '{room_id}'"
        )
    
    try:
        # Use switch_scenario to start the scenario (since no current scenario exists)
        await scenario_manager.switch_scenario(data.id, graceful=True)


        return ScenarioResponse(
            status="success",
            message=f"Successfully started scenario '{data.id}'",
        )
    except Exception as e:
        # Log the full error with traceback for server logs
        logger.error(f"Error starting scenario {data.id}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to start scenario: {str(e)}")

@router.post("/scenario/shutdown", response_model=ScenarioResponse)
async def shutdown_scenario(data: ShutdownScenarioRequest):
    """
    Shutdown the currently active scenario.
    
    This endpoint shuts down the currently active scenario if it matches the provided ID.
    If no scenario is active or the active scenario doesn't match the provided ID, 
    it returns an appropriate error.
    
    Args:
        data: The shutdown scenario request with scenario ID and graceful flag
        
    Returns:
        ScenarioResponse: Status of the operation
        
    Raises:
        HTTPException: If no active scenario, scenario mismatch, or an error occurs
    """
    check_initialized()
    assert scenario_manager is not None  # narrowed by check_initialized() above
    
    # Resolve the scenario's room; activity checks are room-scoped.
    defn = scenario_manager.scenario_definitions.get(data.id)
    if defn is None:
        raise HTTPException(status_code=404, detail=f"Scenario '{data.id}' not found")
    room_id = defn.room_id
    active = scenario_manager.active_in_room(room_id) if room_id else None
    if not active:
        raise HTTPException(status_code=404, detail=f"No scenario is currently active in room '{room_id}'")

    # Check if the room's active scenario matches the requested shutdown ID
    if active.scenario_id != data.id:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot shutdown scenario '{data.id}': scenario '{active.scenario_id}' is currently active in room '{room_id}'"
        )

    try:
        current_scenario_id = active.scenario_id

        # Deactivate the room's scenario — this is the explicit "turn it all off" action and
        # DOES power off the gear (distinct from process shutdown, which leaves hardware as-is).
        await scenario_manager.deactivate(room_id)


        return ScenarioResponse(
            status="success",
            message=f"Successfully shut down scenario '{current_scenario_id}'",
        )
    except Exception as e:
        # Log the full error with traceback for server logs
        logger.error(f"Error shutting down scenario {data.id}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to shutdown scenario: {str(e)}")

@router.post("/scenario/role_action", response_model=Dict[str, Any])
async def execute_role_action(data: ActionRequest):
    """
    Execute an action on a device bound to a role in the current scenario.
    
    Args:
        data: The role action request with role, command and params
        
    Returns:
        Dict[str, Any]: The result of the command execution
        
    Raises:
        HTTPException: If no active scenario or an error occurs
    """
    check_initialized()
    assert scenario_manager is not None  # narrowed by check_initialized() above
    
    try:
        result = await scenario_manager.execute_role_action(data.role, data.command, data.params)

        # No scenario SSE here: a role action drives a DEVICE, not the room's
        # active-scenario slot — device state flows through the devices channel.
        # Lifecycle events are emitted by the domain chokepoint observer.
        return {"status": "success", "result": result}
    except ScenarioExecutionError as e:
        # Specifically handle execution errors
        logger.error(
            f"Execution error for role {data.role}, command {data.command}: {str(e)}",
            exc_info=True
        )
        raise HTTPException(status_code=500, detail=f"Command execution failed: {str(e)}")
    except ScenarioError as e:
        # Map error types to appropriate HTTP status codes
        error_status_map = {
            "invalid_role": 400,
            "missing_device": 404,
            "no_active_scenario": 400,
            "ambiguous_role": 409,
            "execution": 500
        }
        status_code = error_status_map.get(e.error_type, 500)
        raise HTTPException(status_code=status_code, detail=str(e))
    except Exception as e:
        # Log any other exceptions
        logger.error(
            f"Error executing role action {data.role}.{data.command}: {str(e)}",
            exc_info=True
        )
        raise HTTPException(status_code=500, detail=f"Failed to execute action: {str(e)}")

@router.get("/scenario/definition", response_model=List[ScenarioDefinition])
async def get_scenarios_for_room(room: Optional[str] = Query(None, description="Filter scenarios by room ID")):
    """
    Get definitions of scenarios, optionally filtered by room.
    
    Args:
        room: Optional room ID to filter scenarios by
        
    Returns:
        List[ScenarioDefinition]: List of scenario definitions
        
    Raises:
        HTTPException: If service not initialized
    """
    check_initialized()
    assert scenario_manager is not None  # narrowed by check_initialized() above
    
    try:
        if room:
            scenarios = []
            for scenario_id, definition in scenario_manager.scenario_definitions.items():
                if definition.room_id == room:
                    scenarios.append(definition)
            return scenarios
        else:
            return list(scenario_manager.scenario_definitions.values())
    except Exception as e:
        logger.error(f"Error retrieving scenario definitions: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to retrieve scenarios: {str(e)}") 
