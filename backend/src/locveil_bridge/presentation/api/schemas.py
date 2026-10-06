from enum import Enum
from typing import Dict, Any, List, Literal, Optional, Union
from pydantic import BaseModel, ConfigDict, Field
from datetime import datetime
from locveil_bridge.__version__ import __version__


# ---- /config/system response DTOs ------------------------------------------
# Presentation owns its wire shape — these mirror the infrastructure config models
# (MQTTBrokerConfig, PersistenceConfig, MaintenanceConfig, SystemConfig) but decouple
# the API from infra. `from_attributes=True` lets us build them from the infra
# instances via `SystemConfigResponse.model_validate(infra_system_config)`.
# Half of the "system-router cleanup" hexagonal residual (action_plan §5.1); the
# other half (the /reload application-service extraction) is still HW-gated.

class MQTTBrokerConfigResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    host: str
    port: int
    client_id: str
    auth: Optional[Dict[str, str]] = None
    keepalive: int = 60


class PersistenceConfigResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    db_path: str = Field(default="data/state_store.db")


class MaintenanceConfigResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    duration: int
    topic: str


class ReportsConfigResponse(BaseModel):
    """Problem-reporting settings as served (the PAT itself never appears — only
    the name of the env var holding it). `repo` mirrors the config: no default,
    absent unless configured."""
    model_config = ConfigDict(from_attributes=True)
    enabled: bool = False
    repo: Optional[str] = None
    token_env: str = "WB_REPORTS_TOKEN"
    max_reports_per_hour: int = 3
    max_reports_per_day: int = 10
    dispatch_ring_depth: int = 50
    mqtt_window_seconds: int = 60
    mqtt_window_max_messages: int = 500


class SystemConfigResponse(BaseModel):
    """API response shape for ``GET /config/system`` — independent of the infra
    ``SystemConfig`` so the wire contract isn't a leak of internal config layout."""
    model_config = ConfigDict(from_attributes=True)
    service_name: str = Field(default="MQTT Web Service")
    mqtt_broker: MQTTBrokerConfigResponse
    web_service: Dict[str, Any]
    log_level: str
    log_file: str
    loggers: Optional[Dict[str, str]] = None
    devices: Optional[Dict[str, Dict[str, Any]]] = None
    persistence: PersistenceConfigResponse = Field(default_factory=PersistenceConfigResponse)
    maintenance: Optional[MaintenanceConfigResponse] = None
    reports: Optional[ReportsConfigResponse] = None
    device_directory: str = Field(default="devices")


class DeviceAction(BaseModel):
    """Schema for device action requests."""
    action: str = Field(..., description="Action to execute on the device")
    params: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional parameters for the action"
    )
    
    class Config:
        json_schema_extra = {
            "examples": [
                {
                    "action": "power_on",
                    "params": None
                },
                {
                    "action": "set_volume",
                    "params": {
                        "volume": 50
                    }
                }
            ]
        }

class DeviceActionsResponse(BaseModel):
    """Schema for device actions list response."""
    device_id: str
    actions: List[Dict[str, str]]


# ---- /devices/{id}/canonical request/response DTOs ------------------------
# §P3.7 voice-integration slice #15. Voice (Irene) speaks canonical
# (capability, action, params); the bridge resolves canonical -> native via the
# capability registry (class -> profile -> per-device override; see #14 + the
# capability-profile mechanism). Response is synchronous-with-timeout: the bridge
# waits up to ~500ms for the device's value-topic echo before returning.


class CanonicalActionRequest(BaseModel):
    """Request to invoke a canonical (capability, action, params) tuple — the one
    actuation grammar shared by voice and the UI (canonical-first)."""
    capability: str = Field(..., description="Canonical capability name (e.g. 'power', 'volume', 'cover').")
    action: str = Field(..., description="Action within the capability (e.g. 'on', 'set', 'up').")
    params: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Canonical parameter names (renamed to native via the capability's param_map).",
    )
    wait: bool = Field(
        default=True,
        description="Wait for the value-topic echo and return post-action state (voice wants "
                    "a speakable result). False = fire-and-return-current-state (the UI's "
                    "mash-click mode — button presses must not serialize on echo waits).",
    )


class CanonicalErrorCode(str, Enum):
    """Structured error codes for the canonical endpoint. HTTP status mirrors these
    (see /devices/{id}/canonical responses): 404 for the three 'not supported' codes,
    400 for param_invalid, 503 for device_unreachable, 500 for internal_error."""
    DEVICE_NOT_FOUND = "device_not_found"
    CAPABILITY_NOT_SUPPORTED = "capability_not_supported"
    ACTION_NOT_SUPPORTED = "action_not_supported"
    PARAM_INVALID = "param_invalid"
    DEVICE_UNREACHABLE = "device_unreachable"
    INTERNAL_ERROR = "internal_error"
    # SCN-6 Scenario Manager proxy codes (409): the room has no active scenario, or the
    # active scenario binds no role for the requested capability domain.
    NO_ACTIVE_SCENARIO = "no_active_scenario"
    ROLE_UNBOUND = "role_unbound"
    # VWB-23 room-scoped group addressing (canonical_first.md §10): no member of the
    # group in the room (404); scope=one without a configured room default (409); a
    # fan-out was required but the group isn't on the fan-out allow-list (409).
    NO_GROUP_MEMBERS = "no_group_members"
    NO_DEFAULT_DEVICE = "no_default_device"
    FANOUT_NOT_ALLOWED = "fanout_not_allowed"


class CanonicalError(BaseModel):
    """Error envelope. `field` + `reason` populated for param_invalid; both optional."""
    code: CanonicalErrorCode
    message: str
    field: Optional[str] = None
    reason: Optional[str] = None


class CanonicalActionResponse(BaseModel):
    """Response envelope for /devices/{id}/canonical. On success, `state` carries the
    post-action device state (after the value-topic echo within the 500ms window)."""
    success: bool
    device_id: str
    capability: str
    action: str
    state: Optional[Dict[str, Any]] = None
    error: Optional[CanonicalError] = None
    executed_on: Optional[str] = None  # SCN-6: the role-bound device a Scenario Manager proxy command landed on
    no_op: bool = Field(
        default=False,
        description="True when the device was already at the requested value (the driver's "
                    "no_op short-circuit) — succeeded without actuating. Surfaced so "
                    "room-group fan-out results can report members honestly.",
    )
    skipped_reason: Optional[str] = Field(
        default=None,
        description="Structured skip marker. 'idempotence' = an optimistic-state "
                    "guard swallowed the command (nothing was sent — the believed state may "
                    "be wrong); the UI offers a re-tap that re-sends with params.force=true. "
                    "Distinct from plain no_op, which reflects a feedback-verified value.",
    )


# ---- POST /rooms/{room_id}/canonical (VWB-23, canonical_first.md §10) -------------
# The THIRD canonical address form: room + semantic group + action, for utterances that
# name a capability, not a device («включи свет», «закрой шторы»). The resolver resolves
# only as deep as the utterance specifies; membership and default-vs-fan-out policy live
# in the bridge (the `group` overlay on capability maps + rooms.json `group_defaults`).


class RoomCanonicalRequest(BaseModel):
    """Request to invoke a canonical action on a room GROUP — the third canonical
    address form, for utterances that name a capability rather than a device."""
    group: str = Field(..., description="Semantic group name (e.g. 'light', 'cover') — a "
                                        "capability's group defaults to its domain name; "
                                        "profiles override (light_switch power → 'light').")
    action: str = Field(..., description="Action within each member's matching capability "
                                         "(e.g. 'on', 'off', 'open', 'close').")
    params: Optional[Dict[str, Any]] = Field(default=None)
    scope: Literal["auto", "all", "one"] = Field(
        default="auto",
        description="auto = the room's configured default device for the group, else fan-out; "
                    "all = force fan-out (the plural/«весь» signal); one = default device "
                    "required (409 no_default_device if the room declares none).",
    )
    wait: bool = Field(default=True, description="Per-member echo-wait (same semantics as the "
                                                 "device endpoint's `wait`).")


class GroupMemberResult(BaseModel):
    """Per-member outcome of a room group action. `skipped` = the member's
    matching capability lacks the requested action (reported, never an error);
    `no_op` = already at target; `failed` carries the member's error in `detail`."""
    device_id: str
    capability: str = Field(..., description="The member's OWN capability the action ran "
                                             "against (a light switch's 'power', the hood's 'light').")
    status: Literal["executed", "no_op", "skipped", "failed"]
    detail: Optional[str] = None


class RoomCanonicalResponse(BaseModel):
    """Response envelope for `POST /rooms/{room_id}/canonical`. 200 even with partial
    member failures (the caller decides how to speak them); `success` = at least one
    member executed or was already at target. Error envelopes (404/409) carry `error`
    and an empty `results`."""
    success: bool
    room_id: str
    group: str
    action: str
    scope_applied: Optional[Literal["default", "fan_out"]] = None
    results: List[GroupMemberResult] = Field(default_factory=list)
    error: Optional[CanonicalError] = None


# ---- GET /system/catalog DTOs ---------------------------------------------
# §P3.7 voice-integration slice #17. Flat capability-shaped projection of the whole
# house for any non-UI consumer (Irene first). All locales for both rooms and devices.
# The catalog deliberately stays separate from the Layer-3 layout manifest, which is
# UI-shaped (panels, slider widgets, positions). Catalog is the stable read contract.


class CatalogParam(BaseModel):
    """One client-facing parameter of a canonical action — the schema of record a
    voice consumer codes its parser against. Constraints come from the same native
    config specs the driver enforces (canonical names via the reversed `param_map`;
    capability-fixed params excluded). `values` carries the enum value table where
    the choice set is bridge-known (e.g. the scenario enum); `options_from` marks an
    intentionally OPEN set enumerable at runtime via
    `GET /devices/{id}/options/{options_from}` (e.g. installed apps)."""
    name: str
    type: str = "string"
    required: bool = False
    default: Optional[Any] = None
    min: Optional[float] = None
    max: Optional[float] = None
    description: str = ""
    unit: Optional[str] = None
    values: Optional[List["CatalogValueLabel"]] = None
    options_from: Optional[str] = None


class CatalogAction(BaseModel):
    """A canonical action a device supports under a capability. `params` is `None` for
    parameterless actions (`power.on`, `cover.open`) and a list of typed
    :class:`CatalogParam` descriptors otherwise."""
    name: str
    params: Optional[List[CatalogParam]] = None


class CatalogValueLabel(BaseModel):
    """One entry of an enum value table projected into the catalog. A voice consumer
    matches user utterances against `labels` in the active locale and posts
    canonical actions back; the UI renders dropdowns labelled per locale, sending
    `canonical` on selection. `wire` is informational for clients but authoritative
    on the bus."""
    wire: str
    canonical: str
    labels: Optional[Dict[str, str]] = None
    max_duration_ms: Optional[int] = Field(
        default=None,
        description="Scenario values only: the ceiling, in milliseconds, for activating this "
                    "scenario from any state of its room (the full sequential switch chain, "
                    "worst case). On the `none` entry: the ceiling for deactivating the room. "
                    "Absent on every other value table.",
    )


# CatalogParam forward-references CatalogValueLabel (defined above it in contract order,
# below it in file order) — resolve the deferred annotation now that both exist.
CatalogParam.model_rebuild()


class CatalogField(BaseModel):
    """A read-only field on a capability (e.g. `sensor.temperature`, `brightness.level`).
    Mirrors the domain `CapabilityField` shape so voice/UI consumers can render and parse
    values without out-of-band knowledge."""
    name: str
    type: str
    encoding: Optional[str] = None
    values: Optional[List[CatalogValueLabel]] = None
    unit: Optional[str] = None
    labels: Optional[Dict[str, str]] = None


class CatalogCapability(BaseModel):
    """One capability on a device, projected canonical-side. Sensor-shaped capabilities
    have `fields` and no `actions`; momentary capabilities (`power`) have `actions` and
    no `fields`; stateful action capabilities (brightness, color, climate, cover) may
    have both."""
    name: str
    actions: Optional[List[CatalogAction]] = None
    fields: Optional[List[CatalogField]] = None
    confirm_timeout_ms: Optional[int] = Field(
        default=None,
        description="Longest the bridge waits for this capability's device to confirm an "
                    "action before reporting failure, in milliseconds. Absent = the default "
                    "500 ms echo window.",
    )
    group: Optional[str] = Field(
        default=None,
        description="Effective semantic group for room-scoped addressing — "
                    "always explicit so consumers never reimplement the defaulting rule: "
                    "equals `name` unless the capability map overrides (a light switch's "
                    "'power' carries group 'light'); `null` = opted out of group addressing.",
    )


class CatalogDevice(BaseModel):
    """A device in the catalog. `room` is the device's single room (a device belongs
    to exactly one room), `null` for devices whose room isn't set yet."""
    id: str
    names: Dict[str, str]
    aliases: Optional[Dict[str, List[str]]] = Field(
        default=None,
        description="Spoken alias surfaces per locale — «люстра»/«подсветка» for the "
                    "same fixture. "
                    "None until authored.",
    )
    device_class: str
    room: Optional[str] = None
    capabilities: List[CatalogCapability] = Field(default_factory=list)


class CatalogRoom(BaseModel):
    """A room in the catalog. `devices` is the room's authored membership list
    (from `rooms.json`)."""
    id: str
    names: Dict[str, str]
    aliases: Optional[Dict[str, List[str]]] = Field(
        default=None,
        description="Spoken alias surfaces per locale — «зал» for «гостиная». "
                    "None until authored.",
    )
    devices: List[str] = Field(default_factory=list)
    group_defaults: Optional[Dict[str, str]] = Field(
        default=None,
        description="Group name -> default device_id for room-scoped addressing: "
                    "what scope=auto targets instead of fanning out — "
                    "the singular «включи свет». None = no defaults authored.",
    )


class CatalogResponse(BaseModel):
    """Response for `GET /system/catalog`. `version` is a deterministic short hash of
    the {rooms, devices} content -- the same payload always hashes to the same value,
    so Irene can subscribe to the retained `bridge/catalog/version` MQTT topic (bumped
    on `/reload` + config change) and only re-fetch when it differs from the last
    seen one."""
    version: str
    rooms: List[CatalogRoom]
    devices: List[CatalogDevice]

class SystemInfo(BaseModel):
    """Schema for system information."""
    version: str = __version__
    mqtt_broker: Dict[str, Any]  # Will be populated from MQTTBrokerConfig
    devices: List[str] = Field(default_factory=list, description="List of available devices")
    scenarios: List[str] = Field(default_factory=list, description="List of available scenarios")
    rooms: List[str] = Field(default_factory=list, description="List of available rooms")

class ServiceInfo(BaseModel):
    """Schema for service information."""
    service: str
    version: str
    status: str

class ReloadResponse(BaseModel):
    """Schema for system reload response."""
    status: str
    message: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.now)

class MQTTMessage(BaseModel):
    """Schema for MQTT messages."""
    topic: str = Field(
        ..., 
        description="MQTT topic to publish the message to"
    )
    payload: Optional[Union[str, int, float, dict, list, bool]] = Field(
        default=None,
        description="Message payload to publish. Can be string, number, boolean, object, or array. If not provided, defaults to 1."
    )
    qos: int = Field(
        default=0, 
        description="Quality of Service level (0, 1, or 2)",
        ge=0,
        le=2
    )
    retain: bool = Field(
        default=False, 
        description="Whether to retain the message on the broker"
    )
    
    class Config:
        json_schema_extra = {
            "examples": [
                {
                    "topic": "/devices/light/set",
                    "payload": "on",
                    "qos": 1,
                    "retain": False
                },
                {
                    "topic": "/devices/thermostat/set_temp",
                    "payload": 22.5,
                    "qos": 1,
                    "retain": True
                },
                {
                    "topic": "/devices/tv/command",
                    "payload": {
                        "action": "channel",
                        "value": 5
                    },
                    "qos": 0,
                    "retain": False
                }
            ]
        }

class MQTTPublishResponse(BaseModel):
    """Schema for MQTT publish response."""
    success: bool
    message: str
    topic: str
    timestamp: datetime = Field(default_factory=datetime.now)
    error: Optional[str] = None

class ErrorResponse(BaseModel):
    """Schema for error responses."""
    detail: str
    error_code: Optional[str] = None

class Group(BaseModel):
    """Schema for a function group."""
    id: str
    name: str

class ActionGroup(BaseModel):
    """Schema for a group of actions."""
    group_id: str
    group_name: str
    actions: List[Dict[str, Any]]  # Reusing existing action representations
    status: str = "ok"  # Can be "ok" or "no_actions"

class GroupedActionsResponse(BaseModel):
    """Schema for API responses that return actions grouped by function."""
    device_id: str
    groups: List[ActionGroup]
    default_included: bool = False  # Whether the default group is included in the response

class GroupActionsResponse(BaseModel):
    """Schema for API responses that return actions for a specific group with status information."""
    device_id: str
    group_id: str
    group_name: Optional[str] = None
    status: str  # "ok", "no_actions", "invalid_group", "unknown_group"
    message: Optional[str] = None
    actions: List[Dict[str, Any]] = Field(default_factory=list) 