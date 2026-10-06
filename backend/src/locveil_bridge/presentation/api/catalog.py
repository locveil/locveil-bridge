"""Build the flat capability-shaped catalog response for `GET /system/catalog`.

§P3.7 voice-integration slice #17. The catalog is the stable read contract any non-UI
consumer (Irene first) talks to — it's deliberately separate from the Layer-3 layout
manifest, which is UI-shaped (panels, slider widgets, positions).

Resolution:

- Rooms: read directly from `RoomManager.list()` (which reads `rooms.json`); the room's
  `devices` list IS the authored membership ("which devices live here"). Reused verbatim,
  so the catalog room shape matches the long-standing `/room/list` shape.
- Devices: iterate `DeviceManager.devices`, project each device's
  `config.{device_id, names, device_class, room}` and walk its attached `capabilities`
  (`CapabilityMap` — the class-default merged with the §P3.7 capability profile and any
  per-instance override; see #14). For each capability, surface the canonical action
  names only; param introspection lands with the vocab extension (#19).
- Version: short SHA-256 of the canonical JSON of {rooms, devices}, so Irene can
  subscribe to retained `bridge/catalog/version` and only re-fetch on a real change.

The builder takes the live managers as arguments (no global state, no hidden imports of
the FastAPI router state) so the tests can pin behaviour with simple fakes.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping, Optional

from locveil_bridge.domain.capabilities.models import CapabilityMap
from locveil_bridge.presentation.api.param_projection import project_action_params
from locveil_bridge.presentation.api.schemas import (
    CatalogAction,
    CatalogParam,
    CatalogCapability,
    CatalogDevice,
    CatalogField,
    CatalogResponse,
    CatalogRoom,
    CatalogValueLabel,
)

# The catalog contract's version — always three-part since 1.10.0 (family tags
# `catalog-vX.Y.Z`; process/contracts.md §3 in locveil-commons). Three levels:
#   major = breaking; minor = the surface changed (additive — including the pinned set
#   gaining a file); patch = enumerated bytes moved with no surface change (a
#   config-driven golden refresh, an editorial fix to contracts/catalog/catalog-contract.md).
# Distinct from the content-hash `CatalogResponse.version`: that hash moves on every
# house-config change and is what runtime consumers watch; this constant is never served
# at runtime. When a config change moves the committed golden, bump the PATCH level here
# and regenerate — the STAMP.json beside the golden carries the value, and the tag is
# cut on the same commit.
CONTRACT_VERSION = "1.11.0"


def _by_value_labels(act: Optional[Any]) -> Optional[dict]:
    """The labels a by-value select option carries (VWB-46), or None."""
    labels = getattr(act, "labels", None)
    return labels.model_dump() if labels is not None else None


def _project_capability_actions(
    cap_map: Optional[CapabilityMap],
    mirrored_field_names: Optional[set[str]] = None,
    commands: Optional[Mapping[str, Any]] = None,
) -> list[CatalogCapability]:
    """Walk a CapabilityMap and project to the catalog's capability shape — actions
    (with param descriptors since VWB-15 — the shared §6 projection, canonical-name
    view: constraints from the native config specs, names through the reversed
    ``param_map``, capability-fixed params excluded) and read-only fields.

    `mirrored_field_names` (§P3.7 #23 / sauna-sensor case): the set of field names the
    device actually mirrors via `state_topics`. When provided, profile fields[] are
    FILTERED to only the names in the set -- so a device with `sensor_room` profile that
    only mirrors `temperature` + `humidity` emits only those two in the catalog, not the
    full 5 the profile declares. When `None` (AV devices that don't have `state_topics`
    at all), no filtering happens and every profile-declared field is emitted as before.
    """
    out: list[CatalogCapability] = []
    if cap_map is None:
        return out
    # VWB-20/G5: capabilities whose choice set is runtime-dynamic get an `options_from`
    # hint on their string params — the set is enumerable via GET /devices/{id}/options/*
    # (a static enum would drift on every app install). Keyed by capability name.
    options_kind = {"input": "inputs", "apps": "apps"}
    for cap_name, cap in cap_map.root.items():
        dynamic_kind = options_kind.get(cap_name) if cap.list is not None else None
        # VWB-24: enum field tables double as the closed value set of same-named action
        # params (HVAC `set_mode(mode)`, `set_fan(fan)`, …) — the table stays authored
        # ONCE on the field; the param derives it. The correspondence is by canonical
        # param name == field name (the hvac profile's param_map renames guarantee it).
        enum_tables = {
            f.name: f.values for f in cap.fields
            if f.type == "enum" and f.values is not None
        }
        actions: list[CatalogAction] = []
        for action_name, cap_action in cap.actions.items():
            raw = (
                project_action_params(cap_action, dict(commands), canonical_names=True)
                if commands else None
            )
            params: Optional[list[CatalogParam]] = None
            if raw:
                params = [CatalogParam(**d) for d in raw]
                for p in params:
                    table = enum_tables.get(p.name)
                    if table is None and p.name == "value" and cap.state_field:
                        # DRV-28: the `set {value}` convention (VWB-19's shape on
                        # command-form actions) — the canonical `value` param sets the
                        # capability's state field, so it inherits THAT field's table
                        # (MitsubishiHvac mode/fan/vane/widevane). The by-name rule
                        # above stays for the legacy same-named-param shape.
                        table = enum_tables.get(cap.state_field)
                    if table is not None and p.type == "string" and p.values is None:
                        p.values = [
                            CatalogValueLabel(
                                wire=v.wire,
                                canonical=v.canonical,
                                labels=v.labels.model_dump() if v.labels is not None else None,
                            )
                            for v in table
                        ]
                if dynamic_kind:
                    for p in params:
                        if p.type == "string" and p.values is None:
                            p.options_from = dynamic_kind
            actions.append(CatalogAction(name=action_name, params=params))
        # VWB-19: select-form capabilities advertise the reserved `set` action (an
        # authored `set` action wins, mirroring dispatch precedence). by_value selects
        # carry their closed option set statically (`values` — voice validates at
        # resolve time, no round-trip); parametric selects keep the runtime-dynamic
        # `options_from` dance (`GET /devices/{id}/options/*`).
        if cap.select is not None and not any(a.name == "set" for a in actions):
            static = cap.select.option_values()
            # VWB-46: a by-value option carries its own labels (the Localization rule —
            # every value of a `set(value)` table is labelled); the slot is the value's
            # CapabilityAction.labels, authored in place beside its command.
            by_value = cap.select.by_value or {}
            actions.append(CatalogAction(name="set", params=[CatalogParam(
                name="value",
                type="string",
                required=True,
                description="Target option (canonical value).",
                values=(
                    [
                        CatalogValueLabel(
                            wire=v, canonical=v,
                            labels=_by_value_labels(by_value.get(v)),
                        )
                        for v in static
                    ]
                    if static is not None else None
                ),
                options_from=dynamic_kind if static is None else None,
            )]))
        fields: list[CatalogField] = []
        for f in cap.fields:
            if mirrored_field_names is not None and f.name not in mirrored_field_names:
                continue
            projected_values: Optional[list[CatalogValueLabel]] = None
            if f.values is not None:
                projected_values = [
                    CatalogValueLabel(
                        wire=v.wire,
                        canonical=v.canonical,
                        labels=v.labels.model_dump() if v.labels is not None else None,
                    )
                    for v in f.values
                ]
            fields.append(CatalogField(
                name=f.name,
                type=f.type,
                encoding=f.encoding,
                values=projected_values,
                unit=f.unit,
                labels=f.labels.model_dump() if f.labels is not None else None,
            ))
        # VWB-20 (voice review, minor flag): suppress empty husks — a capability with
        # neither invocable actions nor readable fields says nothing a consumer can use.
        # Since VWB-19 select-form capabilities project a real `set`, so the once-husk
        # TV `input` is back in the catalog; the guard stays for future husk shapes.
        if not actions and not fields:
            continue
        out.append(CatalogCapability(
            name=cap_name,
            actions=actions if actions else None,
            fields=fields if fields else None,
            # VWB-46 (tier 1): the confirmation window the canonical endpoint actually
            # waits (DRV-29 — `gate.poll_timeout_ms` when set, else 500 ms). The SAME
            # truthiness test as the endpoint, so the published number and the window
            # can never disagree; `delay_ms` paces the reconciler and is not published.
            confirm_timeout_ms=cap.gate.poll_timeout_ms if cap.gate.poll_timeout_ms else None,
            # VWB-23 (§10): always-explicit effective group so consumers never
            # reimplement the defaulting rule; null = opted out of group addressing.
            group=cap.effective_group(cap_name),
        ))
    return out


def _project_devices(devices_iterable: Iterable) -> list[CatalogDevice]:
    """Build a list of CatalogDevice from anything iterating to device objects with
    `.config` (carrying device_id, names, device_class, room) and `.capabilities`."""
    out: list[CatalogDevice] = []
    for device in devices_iterable:
        cfg = getattr(device, "config", None)
        if cfg is None:
            continue
        names = cfg.names
        # `names` may be a Pydantic LocalizedName or a dict-shaped fallback from a fake.
        if hasattr(names, "model_dump"):
            names_dict = names.model_dump()
        elif isinstance(names, Mapping):
            names_dict = dict(names)
        else:
            names_dict = dict(vars(names))
        # Field filtering by what the device actually mirrors (§P3.7 #23 sauna-sensor case).
        # WB-passthrough configs declare `state_topics`; AV configs don't have that attribute.
        # `None` = no filter (AV devices keep emitting every profile-declared field).
        state_topics = getattr(cfg, "state_topics", None)
        mirrored_field_names: Optional[set[str]] = (
            set(state_topics.keys()) if state_topics is not None else None
        )
        out.append(CatalogDevice(
            id=cfg.device_id,
            names=names_dict,
            aliases=getattr(cfg, "aliases", None),
            device_class=cfg.device_class,
            room=getattr(cfg, "room", None),
            capabilities=_project_capability_actions(
                getattr(device, "capabilities", None), mirrored_field_names,
                commands=getattr(cfg, "commands", None),
            ),
        ))
    out.sort(key=lambda d: d.id)  # stable order -> stable hash
    return out


def _project_rooms(rooms_iterable: Iterable) -> list[CatalogRoom]:
    """Build a list of CatalogRoom from anything iterating to room definitions with
    `.room_id`, `.names`, `.devices`."""
    out: list[CatalogRoom] = []
    for room in rooms_iterable:
        names = room.names if hasattr(room, "names") else {}
        names_dict = dict(names) if isinstance(names, Mapping) else dict(names)
        out.append(CatalogRoom(
            id=room.room_id,
            names=names_dict,
            aliases=getattr(room, "aliases", None),
            devices=list(room.devices),
            group_defaults=getattr(room, "group_defaults", None),
        ))
    out.sort(key=lambda r: r.id)
    return out


def _content_hash(rooms: list[CatalogRoom], devices: list[CatalogDevice]) -> str:
    """Deterministic short hash of the catalog content. Same content -> same value; any
    change to rooms or devices flips it. Truncated to 16 hex chars (64 bits) -- plenty
    for a "did anything change?" signal."""
    payload = {
        "rooms": [r.model_dump() for r in rooms],
        "devices": [d.model_dump() for d in devices],
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _project_scenario_managers(scenario_proxy: Any) -> list[CatalogDevice]:
    """SCN-6: one Scenario Manager entity per scenario-bearing room
    (`scenario_manager_<room_id>`, canonical_first.md §3). Carries:

    - the `scenario` capability — `set(value)` over the room's scenario-id enum
      (labels from the scenario names) + `off`, plus a `scenario` enum field whose
      value table is the ids + `none` (the WB card's value topic mirrors this);
    - the STATIC UNION of inheritable domains (volume/playback/…): action-name union
      over the room's scenarios' role-bound devices. Static per config — the catalog
      stays byte-stable across scenario switches; an unbound domain 409s at fire time.
    """
    out: list[CatalogDevice] = []
    if scenario_proxy is None:
        return out
    for room_id in scenario_proxy.rooms():
        defs = scenario_proxy.room_scenarios(room_id)
        # Localized labels (VWB-20/G3): scenario `names` (ru/en + extras, required since
        # SCN-8) is the voice surface — «включи кино» needs a Russian label.
        # VWB-46 (tier 2): `max_duration_ms` per value — the ceiling for activating the
        # scenario from ANY state of the room (worst graceful teardown + cold activation,
        # derived by the planners over state overrides; confirmation_timing.md §4.3).
        value_table = [
            CatalogValueLabel(
                wire=d.scenario_id, canonical=d.scenario_id,
                labels=d.names.model_dump(),
                max_duration_ms=scenario_proxy.max_duration_ms(room_id, d.scenario_id),
            )
            for d in defs
        ]
        scenario_cap = CatalogCapability(
            name="scenario",
            group="scenario",  # default rule made explicit (VWB-23)
            actions=[
                # Typed CatalogParam (VWB-20/G1) — the same descriptor model the §6
                # projection produces, so both params producers share one schema.
                CatalogAction(name="set", params=[CatalogParam(
                    name="value", type="enum", required=True,
                    description="Scenario id to activate ('none' deactivates)",
                    values=value_table,
                )]),
                CatalogAction(name="off", params=None),
            ],
            fields=[CatalogField(
                name="scenario", type="enum",
                values=value_table + [CatalogValueLabel(
                    wire="none", canonical="none",
                    labels={"ru": "выключено", "en": "off"},
                    # the ceiling for deactivating the room (slowest full power-down)
                    max_duration_ms=scenario_proxy.deactivate_ceiling_ms(room_id),
                )],
                labels={"ru": "сценарий", "en": "scenario"},
            )],
        )
        inherited = [
            CatalogCapability(
                name=domain,
                group=domain,  # default rule made explicit (VWB-23)
                actions=[CatalogAction(name=a, params=None) for a in actions],
            )
            for domain, actions in scenario_proxy.union_actions(room_id).items()
        ]
        out.append(CatalogDevice(
            id=f"scenario_manager_{room_id}",
            names={"ru": "Сценарии", "en": "Scenarios", "de": "Szenarien"},
            device_class="ScenarioManager",
            room=room_id,
            capabilities=[scenario_cap] + inherited,
        ))
    return out


def build_catalog(device_manager: Any, room_manager: Any, scenario_proxy: Any = None) -> CatalogResponse:
    """Build the full catalog response from live managers. `device_manager.devices` is a
    `{device_id: device}` dict (per DeviceManager); `room_manager.list()` returns
    RoomDefinitions (per RoomManager); `scenario_proxy` (SCN-6) contributes the per-room
    Scenario Manager entities."""
    rooms = _project_rooms(room_manager.list() if room_manager is not None else [])
    devices = _project_devices(
        (device_manager.devices.values() if device_manager is not None else [])
    )
    devices = devices + _project_scenario_managers(scenario_proxy)
    return CatalogResponse(
        version=_content_hash(rooms, devices),
        rooms=rooms,
        devices=devices,
    )
