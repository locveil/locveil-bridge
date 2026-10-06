import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse

from locveil_bridge.domain.devices.config import BaseDeviceConfig
from locveil_bridge.domain.scenarios.jobs import ScenarioJobInProgress
from locveil_bridge.domain.scenarios.proxy import (
    NO_SCENARIO,
    SCENARIO_CAPABILITY,
    ScenarioProxyError,
)
from locveil_bridge.presentation.api.schemas import (
    CanonicalActionRequest,
    CanonicalActionResponse,
    CanonicalError,
    CanonicalErrorCode,
    DeviceAction,
)
from locveil_bridge.presentation.api.layout_engine import build_device_manifest
from locveil_bridge.presentation.api.layout_manifest import LayoutManifest
from locveil_bridge.domain.devices.types import CommandResponse
from locveil_bridge.domain.capabilities.models import RESERVED_PARAMS


# §P3.7 #15: how long the canonical endpoint waits for a value-topic echo before
# declaring `device_unreachable`. Synchronous devices (AV) settle their state during
# perform_action so the wait completes immediately; WB-passthrough publishes-and-returns
# and the bridge subscription mirrors the echo into state, firing the waiter callback.
# Default echo window for the canonical dispatch. Right for the relay fleet
# (wb-mqtt-serial echoes in milliseconds); capabilities whose devices confirm slower
# override it via their capability map's `gate.poll_timeout_ms` (DRV-29) — the same
# per-capability timing the reconciler has always honored (LgTv power: 8000 ms;
# MitsubishiHvac: 15000 ms, derived from the mitsubishi2wb/HeatPump packet cadence —
# PACKET_SENT_INTERVAL 1 s + INFOMODE_LEN 6 × PACKET_INFO_INTERVAL 2 s + margin).
CANONICAL_ECHO_TIMEOUT_S = 0.5


def _err_response(
    device_id: str, capability: str, action: str,
    code: CanonicalErrorCode, message: str,
    field: str | None = None, reason: str | None = None,
) -> CanonicalActionResponse:
    return CanonicalActionResponse(
        success=False, device_id=device_id, capability=capability, action=action,
        state=None,
        error=CanonicalError(code=code, message=message, field=field, reason=reason),
    )

# SCN-6 proxy failure mapping: ScenarioProxyError.code -> canonical error code / HTTP status.
_PROXY_ERROR_CODE = {
    "no_active_scenario": CanonicalErrorCode.NO_ACTIVE_SCENARIO,
    "role_unbound": CanonicalErrorCode.ROLE_UNBOUND,
    "unknown_scenario": CanonicalErrorCode.DEVICE_NOT_FOUND,
    "scenario_room_mismatch": CanonicalErrorCode.PARAM_INVALID,
    "device_missing": CanonicalErrorCode.DEVICE_NOT_FOUND,
}
_PROXY_ERROR_STATUS = {
    "no_active_scenario": 409,
    "role_unbound": 409,
    "unknown_scenario": 404,
    "scenario_room_mismatch": 400,
    "device_missing": 404,
}


def _job_in_progress_response(
    entity_id: str, payload: CanonicalActionRequest, e: ScenarioJobInProgress
) -> HTTPException:
    """SCN-19 §5.1: the room is busy — `409 job_in_progress`, the body names the running
    job so the caller can join it (subscribe / GET /scenario/jobs/{id})."""
    resp = _err_response(
        entity_id, payload.capability, payload.action,
        CanonicalErrorCode.JOB_IN_PROGRESS, str(e),
    )
    assert resp.error is not None
    resp.error.job_id = e.job.job_id
    return HTTPException(status_code=409, detail=resp.model_dump())


async def _execute_scenario_capability(
    room_id: str, entity_id: str, payload: CanonicalActionRequest
) -> Any:
    """The manager entity's own `scenario` capability: `set(<id>)` activates/switches
    (reconciler diff), `off` deactivates (powers the room down). State in the response
    is the room's active scenario id (or `none`).

    SCN-19 (contract v1.12): the chain runs as a per-room JOB. `wait: true` (default)
    returns `200` when it ended, `state` carrying the `job_id`; `wait: false` returns
    `202` at acceptance with `state.job_id` + `state.max_duration_ms` (`success: true`
    means ACCEPTED, not done). A room already at the target answers `200 no_op` and
    starts nothing; a room with a running job answers `409 job_in_progress`."""
    assert scenario_proxy is not None
    try:
        if payload.action == "set":
            value = (payload.params or {}).get("value")
            if not isinstance(value, str) or not value:
                resp = _err_response(
                    entity_id, payload.capability, payload.action,
                    CanonicalErrorCode.PARAM_INVALID,
                    "scenario.set requires params.value = <scenario_id>",
                    field="value", reason="missing",
                )
                raise HTTPException(status_code=400, detail=resp.model_dump())
            stop = value == NO_SCENARIO
        elif payload.action == "off":
            value, stop = NO_SCENARIO, True
        else:
            resp = _err_response(
                entity_id, payload.capability, payload.action,
                CanonicalErrorCode.ACTION_NOT_SUPPORTED,
                f"Capability 'scenario' has no action {payload.action!r} (set | off)",
            )
            raise HTTPException(status_code=404, detail=resp.model_dump())

        if payload.wait:
            result = await (scenario_proxy.deactivate(room_id) if stop
                            else scenario_proxy.activate(room_id, value))
        else:
            job = await (scenario_proxy.start_deactivate(room_id) if stop
                         else scenario_proxy.start_activate(room_id, value))
            if job is None:
                result = {"success": True, "no_op": True, "powered_off": [], "failures": []}
            else:
                accepted = CanonicalActionResponse(
                    success=True,
                    device_id=entity_id,
                    capability=payload.capability,
                    action=payload.action,
                    state={
                        "scenario": scenario_proxy.active_id(room_id),
                        "job_id": job.job_id,
                        "max_duration_ms": job.max_duration_ms,
                    },
                    error=None,
                )
                return JSONResponse(status_code=202, content=accepted.model_dump())
    except ScenarioProxyError as e:
        resp = _err_response(
            entity_id, payload.capability, payload.action,
            _PROXY_ERROR_CODE.get(e.code, CanonicalErrorCode.INTERNAL_ERROR), str(e),
        )
        raise HTTPException(status_code=_PROXY_ERROR_STATUS.get(e.code, 500), detail=resp.model_dump())
    except ScenarioJobInProgress as e:
        raise _job_in_progress_response(entity_id, payload, e)

    state: Dict[str, Any] = {"scenario": scenario_proxy.active_id(room_id)}
    if not result.get("no_op"):
        state["job_id"] = result.get("job_id")
    state.update({k: v for k, v in result.items() if k in ("powered_off", "failures")})
    return CanonicalActionResponse(
        success=bool(result.get("success", True)),
        device_id=entity_id,
        capability=payload.capability,
        action=payload.action,
        state=state,
        error=None,
        no_op=bool(result.get("no_op", False)),
    )


# Create router with appropriate prefix and tags
router = APIRouter(
    tags=["Devices"]
)

# Global references that will be set during initialization
config_manager = None
device_manager = None
mqtt_client = None
scenario_proxy = None  # ScenarioProxy (SCN-6); set by initialize()

def initialize(cfg_manager, dev_manager, mqt_client, scenario_prx=None):
    """Initialize global references needed by router endpoints. `scenario_prx` is the
    per-room Scenario Manager proxy (SCN-6); None in minimal test wiring."""
    global config_manager, device_manager, mqtt_client, scenario_proxy
    config_manager = cfg_manager
    device_manager = dev_manager
    mqtt_client = mqt_client
    scenario_proxy = scenario_prx

@router.get("/config/device/{device_id}", response_model=BaseDeviceConfig)
async def get_device_config(device_id: str):
    """Get full configuration for a specific device."""
    if not config_manager:
        raise HTTPException(status_code=503, detail="Service not fully initialized")
    
    try:
        # Get the typed configuration for this device
        typed_configs = config_manager.get_all_typed_configs()
        if device_id in typed_configs:
            return typed_configs[device_id]
        
        # No typed config found - return 404
        logger = logging.getLogger(__name__)
        logger.error(f"Typed configuration for device {device_id} not found")
        raise HTTPException(status_code=404, detail=f"Typed device configuration for {device_id} not found")
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.error(f"Error retrieving device config for {device_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@router.get("/config/devices", response_model=Dict[str, BaseDeviceConfig])
async def get_all_device_configs():
    """
    Get configurations for all devices.
    
    Returns a dictionary where:
    - Keys are device IDs (strings)
    - Values are device configuration objects with all device-specific settings
    
    Each device configuration includes:
    - Basic device information (device_id, device_name, device_class, config_class)
    - MQTT topics and settings
    - Available commands and their parameters
    - Device-specific configuration sections (e.g., tv, emotiva, broadlink settings)
    
    Returns:
        Dict[str, BaseDeviceConfig]: Dictionary mapping device IDs to their configurations
    """
    if not config_manager:
        raise HTTPException(status_code=503, detail="Service not fully initialized")
    
    try:
        # Get all typed configurations
        typed_configs = config_manager.get_all_typed_configs()
        
        # Return typed configs (may be empty dict if none exist)
        return typed_configs
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.error(f"Error retrieving all device configs: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@router.post("/devices/{device_id}/action", response_model=CommandResponse)
async def execute_device_action(
    device_id: str, 
    action: DeviceAction,
    background_tasks: BackgroundTasks
):
    """Execute an action on a specific device.
    
    This endpoint allows executing various actions on devices. The available actions depend on the device type.
    
    ## LG TV Actions:
    
    * **move_cursor_relative** - Move cursor by dx,dy relative to current position.
      webOS exposes no absolute-positioning endpoint on its pointer socket — only
      deltas are meaningful (see the asyncwebostv pointer protocol notes). An
      absolute-coordinates action existed once but never worked and was removed.
      ```json
      {
        "action": "move_cursor_relative",
        "params": {
          "dx": 10,
          "dy": -5
        }
      }
      ```

    * **click** - Click at the current cursor position. webOS's pointer click
      protocol takes no coordinates; position the cursor with `move_cursor_relative`
      first if needed.
      ```json
      {
        "action": "click",
        "params": {}
      }
      ```
    
    * **launch_app** - Launch an app by name
      ```json
      {
        "action": "launch_app",
        "params": {
          "app_name": "Netflix"
        }
      }
      ```
    
    * **wake_on_lan** - Wake the TV using Wake-on-LAN
      ```json
      {
        "action": "wake_on_lan",
        "params": {}
      }
      ```
    
    ## Emotiva XMC2 Actions:
    
    * **power_on** - Turn the device on (supports zones)
      ```json
      {
        "action": "power_on",
        "params": {
          "zone": 1  # 1 for main zone, 2 for zone2
        }
      }
      ```
    
    * **power_off** - Turn the device off (supports zones)
      ```json
      {
        "action": "power_off",
        "params": {
          "zone": 2  # This powers off zone 2
        }
      }
      ```
      
    * **set_volume** - Set volume level (supports zones)
      ```json
      {
        "action": "set_volume",
        "params": {
          "level": -35.5,  # Volume in dB from -96.0 to 0.0
          "zone": 1  # 1 for main zone, 2 for zone2
        }
      }
      ```
      
    * **mute_toggle** - Toggle mute state (supports zones)
      ```json
      {
        "action": "mute_toggle",
        "params": {
          "zone": 2  # This toggles mute for zone 2
        }
      }
      ```
      
    * **set_input** - Change input source
      ```json
      {
        "action": "set_input",
        "params": {
          "input": "hdmi1"  # Input name (hdmi1-hdmi8, optical1-optical4, etc.)
        }
      }
      ```
    
    ## Common Actions for Other Devices:
    * **power_on** - Turn the device on
    * **power_off** - Turn the device off
    * **set_volume** - Set device volume (params: volume)
    * **set_mute** - Set device mute state (params: mute)
    * **set_input_source** - Change input source (params: input_source)
    * **send_action** - Send remote control command (params: command)
    """
    logger = logging.getLogger(__name__)
    if not device_manager:
        raise HTTPException(status_code=503, detail="Service not fully initialized")
    
    # Use the device_manager's perform_action method, which handles persistence
    logger.info(f"Executing action {action.action} for device {device_id} with params {action.params}")
    result = await device_manager.perform_action(device_id, action.action, action.params)
    
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
    
    # If there's an MQTT command to be published, do it in the background
    if "mqtt_command" in result and result["mqtt_command"] is not None and mqtt_client is not None:
        mqtt_cmd = result["mqtt_command"]
        background_tasks.add_task(
            mqtt_client.publish,
            mqtt_cmd["topic"],
            mqtt_cmd["payload"]
        )
    
    # Return the properly typed CommandResponse directly
    return result


# §P3.7 voice-integration slice #15.
_HTTP_FOR_CODE = {
    CanonicalErrorCode.DEVICE_NOT_FOUND: 404,
    CanonicalErrorCode.CAPABILITY_NOT_SUPPORTED: 404,
    CanonicalErrorCode.ACTION_NOT_SUPPORTED: 404,
    CanonicalErrorCode.PARAM_INVALID: 400,
    CanonicalErrorCode.DEVICE_UNREACHABLE: 503,
    CanonicalErrorCode.INTERNAL_ERROR: 500,
}


@router.post(
    "/devices/{device_id}/canonical",
    response_model=CanonicalActionResponse,
    responses={
        202: {"model": CanonicalActionResponse,
              "description": "A scenario job was accepted (`wait: false` on a Scenario "
                             "Manager's `scenario` capability): `state.job_id` and "
                             "`state.max_duration_ms` — `success: true` means accepted, not done."},
        404: {"model": CanonicalActionResponse},
        400: {"model": CanonicalActionResponse},
        409: {"model": CanonicalActionResponse,
              "description": "`job_in_progress`: a scenario job is already running in the "
                             "room (`error.job_id` names it); or a Scenario Manager proxy "
                             "refusal (`no_active_scenario`, `role_unbound`)."},
        503: {"model": CanonicalActionResponse},
        500: {"model": CanonicalActionResponse},
    },
)
async def execute_canonical_action(device_id: str, payload: CanonicalActionRequest):
    """Voice-friendly canonical action endpoint.

    Body: `{capability, action, params?}` -- the same canonical tuple Irene parses from
    an utterance. The bridge resolves it through the device's capability map
    (class → profile → per-device override) and invokes the native command
    via `perform_action`.

    Synchronous with a ~500 ms timeout: the response carries the **post-action**
    device state once the value-topic echo arrives. For WB-passthrough devices the
    echo flows through the MQTT subscription chain → `update_state` → registered
    callbacks; we register a one-shot callback for the duration of the call so the
    handler unblocks the instant the device acknowledges. AV drivers update state
    synchronously inside `perform_action`, so the wait returns immediately for them.

    Error codes (HTTP status mirrors `error.code`):
      - `device_not_found` (404)
      - `capability_not_supported` (404)
      - `action_not_supported` (404)
      - `param_invalid` (400) - currently mapped from any perform_action failure with
        param-shaped error text; refined later if/when handlers distinguish cleanly.
      - `device_unreachable` (503) - the device handler reported a reachability
        failure (connection lost/refused), the echo wait timed out, or
        `state.reachable` flipped False during the wait (a per-control `meta/error`
        flag landed, per the Wirenboard MQTT convention).
      - `internal_error` (500) - everything else.
      - `job_in_progress` (409) - a Scenario Manager's `scenario` request while a
        scenario job runs in the room; `error.job_id` names it (since contract v1.12).

    On a Scenario Manager's `scenario` capability `wait: false` returns `202` with
    `state.job_id` + `state.max_duration_ms`: the job is accepted, not done — follow it
    via `GET /scenario/jobs/{job_id}` or the scenarios event stream.
    """
    if not device_manager:
        raise HTTPException(status_code=503, detail="Service not fully initialized")

    # SCN-6 proxy seam: canonical commands at a per-room Scenario Manager entity.
    # `scenario` capability = activation; any other capability resolves role -> device
    # at FIRE time and falls through to the normal per-device dispatch below, with the
    # response keeping the entity identity + `executed_on` carrying the real target.
    response_device_id = device_id
    executed_on: Optional[str] = None
    if scenario_proxy is not None:
        proxy_room = scenario_proxy.entity_room(device_id)
        if proxy_room is not None:
            if payload.capability == SCENARIO_CAPABILITY:
                return await _execute_scenario_capability(proxy_room, device_id, payload)
            try:
                target_id, _target = scenario_proxy.resolve(proxy_room, payload.capability)
            except ScenarioProxyError as e:
                resp = _err_response(
                    device_id, payload.capability, payload.action,
                    _PROXY_ERROR_CODE.get(e.code, CanonicalErrorCode.INTERNAL_ERROR), str(e),
                )
                raise HTTPException(
                    status_code=_PROXY_ERROR_STATUS.get(e.code, 500),
                    detail=resp.model_dump(),
                )
            executed_on = target_id
            device_id = target_id  # dispatch below targets the role-bound device

    return await dispatch_device_canonical(device_id, payload, response_device_id, executed_on)


async def dispatch_device_canonical(
    device_id: str,
    payload: CanonicalActionRequest,
    response_device_id: Optional[str] = None,
    executed_on: Optional[str] = None,
) -> CanonicalActionResponse:
    """Per-device canonical dispatch core — capability-map resolution, VWB-17 step
    expansion, wait:false / no_op / echo-wait semantics. Split out of the route handler
    (VWB-23) so the room group endpoint fans out through the IDENTICAL path each member
    would take individually. Raises HTTPException carrying a CanonicalActionResponse
    envelope in `detail` on failure."""
    if response_device_id is None:
        response_device_id = device_id
    if not device_manager:
        raise HTTPException(status_code=503, detail="Service not fully initialized")

    device = device_manager.get_device(device_id)
    if not device:
        resp = _err_response(
            response_device_id, payload.capability, payload.action,
            CanonicalErrorCode.DEVICE_NOT_FOUND, f"Device {device_id!r} not found",
        )
        raise HTTPException(status_code=404, detail=resp.model_dump())

    # Resolve canonical -> native through the device's capability map.
    cap_map = getattr(device, "capabilities", None)
    if cap_map is None or payload.capability not in cap_map.root:
        resp = _err_response(
            response_device_id, payload.capability, payload.action,
            CanonicalErrorCode.CAPABILITY_NOT_SUPPORTED,
            f"Device {device_id!r} does not expose capability {payload.capability!r}",
        )
        raise HTTPException(status_code=404, detail=resp.model_dump())

    cap = cap_map.root[payload.capability]
    if payload.action in cap.actions:
        # VWB-17: canonical -> native via the shared domain expansion. Command form
        # yields one step; sequence form yields the ordered native steps (each step
        # applies its own param_map rename + fixed-params overlay; inter-step
        # delay_after_ms honored in the execution loop below).
        steps = cap.actions[payload.action].expand(payload.params)
    elif payload.action == "set" and cap.select is not None:
        # VWB-19: select-form routing. `set` is the reserved canonical action for
        # capabilities whose invocation lives in `select` — `{value}` resolves through
        # the same expansion the reconciler uses (parametric rename or by_value table).
        # An authored `set` action wins over this branch (checked above).
        value = (payload.params or {}).get("value")
        if value is None:
            resp = _err_response(
                response_device_id, payload.capability, payload.action,
                CanonicalErrorCode.PARAM_INVALID,
                "select-form `set` requires params.value",
                field="value", reason="required",
            )
            raise HTTPException(status_code=400, detail=resp.model_dump())
        try:
            steps = cap.select.expand(value)
            # DRV-21: select-form `expand` takes only `value`, so the reserved
            # cross-cutting params (force/assume_state) would be dropped — killing the
            # UI's re-tap-to-force escape hatch for input desync on AV inputs. Overlay
            # them onto each expanded step (step params win any conflict), mirroring how
            # the actions-form path forwards them via CapabilityAction.expand.
            reserved = {
                k: v for k, v in (payload.params or {}).items()
                if k in RESERVED_PARAMS and v is not None
            }
            if reserved:
                steps = [
                    step.model_copy(update={"params": {**reserved, **step.params}})
                    for step in steps
                ]
        except ValueError as e:
            resp = _err_response(
                response_device_id, payload.capability, payload.action,
                CanonicalErrorCode.PARAM_INVALID, str(e),
                field="value", reason="unknown_value",
            )
            raise HTTPException(status_code=400, detail=resp.model_dump())
    else:
        resp = _err_response(
            response_device_id, payload.capability, payload.action,
            CanonicalErrorCode.ACTION_NOT_SUPPORTED,
            f"Capability {payload.capability!r} has no action {payload.action!r}",
        )
        raise HTTPException(status_code=404, detail=resp.model_dump())
    if not steps:
        resp = _err_response(
            response_device_id, payload.capability, payload.action,
            CanonicalErrorCode.INTERNAL_ERROR,
            f"Action {payload.action!r} on {payload.capability!r} expanded to no steps",
        )
        raise HTTPException(status_code=500, detail=resp.model_dump())

    # Register a one-shot state-change waiter. perform_action will trigger callbacks
    # via update_state when state changes -- synchronously for AV (so the event is
    # already set when we await), asynchronously for WB-passthrough (set by the value-
    # topic echo subscription).
    state_changed = asyncio.Event()

    def _waiter(_device_id: str, _changed_fields):
        state_changed.set()

    register = getattr(device, "register_state_change_callback", None)
    if register is not None:
        register(_waiter)
    try:
        result: Dict[str, Any] = {}
        total = len(steps)
        for i, step in enumerate(steps):
            result = await device_manager.perform_action(device_id, step.command, step.params)

            if not result.get("success"):
                err_text = str(result.get("error") or "unknown error")
                if total > 1:
                    err_text = f"step {i + 1}/{total} ({step.command}): {err_text}"
                # Handlers don't return structured error codes yet, so classification is a
                # keyword sniff over the error text. Reachability wording maps to the same
                # DEVICE_UNREACHABLE/503 the echo-timeout branch below emits — a handler
                # that already knows the device is gone ("Not connected to processor")
                # must not surface as internal_error while a silent device gets a
                # speakable 503 (VWB-31). Deliberately narrow: protocol-limitation
                # messages ("… not available on this device") are real 500s, so bare
                # "available"/"unavailable" stays out of the set. Param-shaped wording
                # maps to a clean 400.
                low = err_text.lower()
                _UNREACHABLE_MARKERS = (
                    "not connected", "unreachable", "connection refused",
                    "connection failed", "connection error", "offline",
                )
                if any(marker in low for marker in _UNREACHABLE_MARKERS):
                    code = CanonicalErrorCode.DEVICE_UNREACHABLE
                    status = 503
                elif "param" in low or "missing" in low or "required" in low or "invalid" in low:
                    code = CanonicalErrorCode.PARAM_INVALID
                    status = 400
                else:
                    code = CanonicalErrorCode.INTERNAL_ERROR
                    status = 500
                resp = _err_response(
                    response_device_id, payload.capability, payload.action,
                    code, err_text,
                )
                raise HTTPException(status_code=status, detail=resp.model_dump())

            # Inter-step gap (IR macros need breathing room between presses).
            if step.delay_after_ms and i < total - 1:
                await asyncio.sleep(step.delay_after_ms / 1000)

        # The guarded handlers run synchronously inside perform_action, so the
        # idempotence-skip marker (DRV-5) is available on `result.data` even in
        # wait:false mode. Single-step only — same restriction as the no_op branch.
        result_data = (result.get("data") or {}) if total == 1 else {}
        skipped_reason = result_data.get("skipped_reason")

        # wait:false (SCN-7 — the UI's mash-click mode): fire-and-return-current-state.
        # No echo wait, no reachability verdict — the UI reads live state via SSE anyway,
        # and serializing rapid button presses on ~500ms echo waits would wreck the UX.
        # The skip marker still rides along so the UI can arm its re-tap-to-force offer.
        if not payload.wait:
            state = device.state.model_dump() if hasattr(device.state, "model_dump") else dict(device.state)
            return CanonicalActionResponse(
                success=True, device_id=response_device_id,
                capability=payload.capability, action=payload.action,
                state=state, error=None, executed_on=executed_on,
                no_op=bool(result_data.get("no_op")),
                skipped_reason=skipped_reason,
            )

        # No-op short-circuit (single-step actions only — the WB-passthrough semantics).
        # The driver flags `data.no_op = True` when the device is already at the requested
        # value -- the publish goes out but no echo lands, so waiting would 503
        # ("включи свет" when it's already on). Return success with the current state
        # immediately. AV devices don't set this flag and keep going through the echo wait.
        if result_data.get("no_op"):
            state = device.state.model_dump() if hasattr(device.state, "model_dump") else dict(device.state)
            return CanonicalActionResponse(
                success=True, device_id=response_device_id,
                capability=payload.capability, action=payload.action,
                state=state, error=None, executed_on=executed_on,
                no_op=True,  # VWB-23: group fan-out reports this member as already-at-target
                skipped_reason=skipped_reason,  # DRV-5: idempotence skips flow through here too
            )

        # DRV-29: the echo window honors the capability's gate — slow-confirm devices
        # (the mitsubishi2wb ACs read back on a multi-second packet rotation) declare
        # `gate.poll_timeout_ms` in their capability map; everything else keeps the
        # 500 ms relay default. Before this, every AC command 503'd while succeeding.
        echo_timeout_s = (
            cap.gate.poll_timeout_ms / 1000.0
            if cap.gate.poll_timeout_ms else CANONICAL_ECHO_TIMEOUT_S
        )
        try:
            await asyncio.wait_for(state_changed.wait(), timeout=echo_timeout_s)
        except asyncio.TimeoutError:
            resp = _err_response(
                response_device_id, payload.capability, payload.action,
                CanonicalErrorCode.DEVICE_UNREACHABLE,
                f"No state echo within {int(echo_timeout_s * 1000)} ms",
            )
            raise HTTPException(status_code=503, detail=resp.model_dump())

        # `state.reachable` is False when an `r`/`rw` meta/error landed during the wait
        # (Wirenboard MQTT convention -- A3). AV states don't carry that field; default True.
        if not getattr(device.state, "reachable", True):
            err_flags = getattr(device.state, "error_flags", {})
            resp = _err_response(
                response_device_id, payload.capability, payload.action,
                CanonicalErrorCode.DEVICE_UNREACHABLE,
                f"Device reported per-control error during the wait: {err_flags!r}",
            )
            raise HTTPException(status_code=503, detail=resp.model_dump())

        # Post-action state -- re-read AFTER the wait so WB-passthrough callers see the
        # echoed value (perform_action's result snapshot was taken before the echo).
        state = device.state.model_dump() if hasattr(device.state, "model_dump") else dict(device.state)
        return CanonicalActionResponse(
            success=True, device_id=response_device_id,
            capability=payload.capability, action=payload.action,
            state=state, error=None, executed_on=executed_on,
        )
    finally:
        callbacks = getattr(device, "_state_change_callbacks", None)
        if callbacks is not None and _waiter in callbacks:
            callbacks.remove(_waiter)


_OPTIONS_KIND_TO_CAPABILITY = {"inputs": "input", "apps": "apps"}


@router.get("/devices/{device_id}/options/{kind}", response_model=Dict[str, Any])
async def get_device_options(device_id: str, kind: str):
    """Option enumeration as a READ: the dropdown population that used to ride
    `POST /devices/{id}/action` (`get_available_inputs`/`get_available_apps`) moves to
    the read surface, keeping the canonical action path purely imperative. Resolves the
    capability's declared `list` query and executes it internally (`source="system"`,
    so a dormant/`exposed:false` list command still answers). Returns the driver's
    result envelope unchanged (`{success, data: [...]}`)."""
    if not device_manager:
        raise HTTPException(status_code=503, detail="Service not fully initialized")
    capability = _OPTIONS_KIND_TO_CAPABILITY.get(kind)
    if capability is None:
        raise HTTPException(status_code=404, detail=f"Unknown options kind {kind!r} (inputs | apps)")
    device = device_manager.get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail=f"Device {device_id!r} not found")
    cap_map = getattr(device, "capabilities", None)
    cap = cap_map.get(capability) if cap_map else None
    list_action = getattr(cap, "list", None) if cap else None
    if list_action is None or not list_action.command:
        # VWB-19: a by_value select has a closed, statically-known option set (the
        # table keys) and typically no `list` query (fixed IR/relay codes) — serve
        # the keys in the same result envelope a driver list query would return.
        sel = getattr(cap, "select", None) if cap else None
        static_values = sel.option_values() if sel is not None else None
        if static_values is not None:
            return {"success": True, "data": static_values}
        raise HTTPException(
            status_code=404,
            detail=f"Device {device_id!r} declares no '{capability}.list' query",
        )
    try:
        result = await device.execute_action(list_action.command, {}, source="system")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Option query failed: {e}")
    return result if isinstance(result, dict) else {"success": True, "data": result}


@router.get("/devices/{device_id}/layout", response_model=LayoutManifest, response_model_exclude_none=True)
async def get_device_layout(device_id: str):
    """Layer-3 layout manifest for a device — the backend-computed remote layout the UI renders at
    runtime (replaces build-time codegen). Built from the device's capability map by the placement
    engine (``presentation/api/layout_engine.py``).

    ``response_model_exclude_none=True`` omits null fields so the payload matches the build-time
    codegen contract (absent = not present). The UI checks ``content.xDropdown !== undefined``; an
    explicit ``null`` would read as "present" and trigger a spurious fetch (Step-2 hardening)."""
    if not device_manager:
        raise HTTPException(status_code=503, detail="Service not fully initialized")
    device = device_manager.devices.get(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail=f"Device {device_id} not found")
    try:
        return build_device_manifest(device)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to build layout manifest: {e}")