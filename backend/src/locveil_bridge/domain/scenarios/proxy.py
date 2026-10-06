"""Scenario Manager proxy entities (SCN-6, canonical_first.md §3–§4).

One virtual entity per scenario-bearing room, id ``scenario_manager_<room_id>``.
The entity's ``scenario`` capability is activation (``set``/``off`` — the room's
reconciler transition / deactivate). Any OTHER capability fired at the entity is a
scenario-inherited device command: the proxy resolves role → device at FIRE time
against the room's active scenario and the caller re-enters the normal per-device
canonical dispatch.

This module is pure domain: it composes :class:`ScenarioManager` and the device
registry; REST, the WB card adapter, and the catalog builder are thin consumers.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

from locveil_bridge.domain.scenarios.models import ScenarioDefinition
from locveil_bridge.domain.scenarios.reconciler import deactivate_ceiling_ms, scenario_ceiling_ms
from locveil_bridge.domain.scenarios.service import ScenarioManager

logger = logging.getLogger(__name__)

# Canonical role name -> capability domain. Role names in scenario configs ARE the
# domain names (identity map); kept as an explicit table so a future divergence is a
# one-line change. Consumed by the proxy AND the Layer-3 scenario manifest builder.
SCENARIO_ROLE_DOMAIN: Dict[str, str] = {
    "volume": "volume", "playback": "playback", "tracks": "tracks",
    "menu": "menu", "screen": "screen", "apps": "apps", "pointer": "pointer",
}

# The static union of inheritable domains advertised on every manager entity
# (canonical_first.md §3). Static so the catalog stays byte-stable across scenario
# switches; a domain whose role is unbound RIGHT NOW fails at fire time instead.
INHERITABLE_DOMAINS: Tuple[str, ...] = ("volume", "playback", "menu", "tracks", "screen")

ENTITY_PREFIX = "scenario_manager_"

# The activation capability's canonical name + the "no scenario" wire value.
SCENARIO_CAPABILITY = "scenario"
NO_SCENARIO = "none"


def manager_entity_id(room_id: str) -> str:
    return f"{ENTITY_PREFIX}{room_id}"


class ScenarioProxyError(Exception):
    """Proxy resolution/activation failure with a machine-readable code.

    Codes: ``no_active_scenario`` | ``role_unbound`` | ``unknown_scenario`` |
    ``scenario_room_mismatch`` | ``device_missing``.
    """

    def __init__(self, msg: str, code: str):
        super().__init__(msg)
        self.code = code


class ScenarioProxy:
    """Fire-time role resolution + activation for the per-room manager entities."""

    def __init__(self, scenario_manager: ScenarioManager, device_manager: Any):
        self.scenario_manager = scenario_manager
        self.device_manager = device_manager

    # ---- entity registry -------------------------------------------------

    def rooms(self) -> List[str]:
        """Rooms that carry scenarios — one manager entity each."""
        return self.scenario_manager.rooms_with_scenarios()

    def entity_room(self, device_id: str) -> Optional[str]:
        """The room a manager entity id belongs to, or None if `device_id` is not a
        (known) manager entity."""
        if not device_id.startswith(ENTITY_PREFIX):
            return None
        room = device_id[len(ENTITY_PREFIX):]
        return room if room in self.rooms() else None

    # ---- read side (catalog / WB card) ------------------------------------

    def room_scenarios(self, room_id: str) -> List[ScenarioDefinition]:
        """The room's scenario definitions, sorted by id (the enum value table)."""
        return sorted(
            (d for d in self.scenario_manager.scenario_definitions.values()
             if d.room_id == room_id),
            key=lambda d: d.scenario_id,
        )

    def max_duration_ms(self, room_id: str, scenario_id: str) -> int:
        """The catalog's `max_duration_ms` for a scenario value (VWB-46): worst graceful
        teardown + cold activation, from the planners over state overrides —
        confirmation_timing.md §4.3. Config-stable, never typed by hand."""
        defs = self.room_scenarios(room_id)
        scenario = next(d for d in defs if d.scenario_id == scenario_id)
        return scenario_ceiling_ms(
            scenario, defs, self.scenario_manager.topology, self.device_manager.devices,
        )

    def deactivate_ceiling_ms(self, room_id: str) -> int:
        """The catalog's `max_duration_ms` for the `none` value: the slowest full
        power-down of any scenario of the room."""
        return deactivate_ceiling_ms(
            self.room_scenarios(room_id), self.scenario_manager.topology,
            self.device_manager.devices,
        )

    def active_id(self, room_id: str) -> str:
        """The room's active scenario id, or ``none``."""
        active = self.scenario_manager.active_in_room(room_id)
        return active.scenario_id if active else NO_SCENARIO

    def union_actions(self, room_id: str) -> Dict[str, List[str]]:
        """The advertised action-name union per inheritable domain, computed over the
        room's scenarios' role-bound devices' capability maps. Config-stable: changes
        only when configs change (the catalog version re-hashes then anyway)."""
        union: Dict[str, set] = {}
        for defn in self.room_scenarios(room_id):
            for role, device_id in defn.roles.items():
                domain = SCENARIO_ROLE_DOMAIN.get(role)
                if domain not in INHERITABLE_DOMAINS:
                    continue
                device = self.device_manager.get_device(device_id)
                cap_map = getattr(device, "capabilities", None) if device else None
                cap = cap_map.get(domain) if cap_map else None
                if cap is None:
                    continue
                union.setdefault(domain, set()).update(cap.actions.keys())
        return {d: sorted(a) for d, a in union.items() if a}

    # ---- fire-time resolution (inherited commands) -------------------------

    def resolve(self, room_id: str, capability: str) -> Tuple[str, Any]:
        """Resolve an inherited-domain command to the room's role-bound device.

        Returns ``(device_id, device)``. Raises :class:`ScenarioProxyError` with a
        speakable code when the room has no active scenario, the active scenario
        doesn't bind the domain's role, or the bound device is missing.
        """
        active = self.scenario_manager.active_in_room(room_id)
        if active is None:
            raise ScenarioProxyError(
                f"No scenario is active in room '{room_id}'", "no_active_scenario"
            )
        # Identity role<->domain mapping (see SCENARIO_ROLE_DOMAIN).
        role = next(
            (r for r, d in SCENARIO_ROLE_DOMAIN.items()
             if d == capability and r in active.definition.roles),
            None,
        )
        if role is None:
            raise ScenarioProxyError(
                f"Scenario '{active.scenario_id}' has no role for capability "
                f"'{capability}' in room '{room_id}'",
                "role_unbound",
            )
        device_id = active.definition.roles[role]
        device = self.device_manager.get_device(device_id)
        if device is None:
            raise ScenarioProxyError(
                f"Role '{role}' of scenario '{active.scenario_id}' is bound to missing "
                f"device '{device_id}'",
                "device_missing",
            )
        return device_id, device

    # ---- activation (the `scenario` capability) ----------------------------

    async def activate(self, room_id: str, scenario_id: str) -> Dict[str, Any]:
        """``scenario.set(<id>)`` — activate/switch the room's scenario (reconciler diff)."""
        defn = self.scenario_manager.scenario_definitions.get(scenario_id)
        if defn is None:
            raise ScenarioProxyError(f"Scenario '{scenario_id}' not found", "unknown_scenario")
        if defn.room_id != room_id:
            raise ScenarioProxyError(
                f"Scenario '{scenario_id}' belongs to room '{defn.room_id}', "
                f"not '{room_id}'",
                "scenario_room_mismatch",
            )
        return await self.scenario_manager.switch_scenario(scenario_id)

    async def deactivate(self, room_id: str) -> Dict[str, Any]:
        """``scenario.off`` — power the room's scenario down (explicit user action)."""
        return await self.scenario_manager.deactivate(room_id)

    # ---- WB-card execution (no echo-wait; REST keeps its own dispatch) -----

    async def execute(self, room_id: str, capability: str, action: str,
                      params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Execute an inherited-domain canonical action end-to-end (resolution +
        capability-map translation + native dispatch). Used by the WB card executor;
        the REST endpoint resolves via :meth:`resolve` and keeps its own echo-waiting
        dispatch. Sequence-form actions execute step-by-step with their inter-step
        delays (VWB-17 — same expansion the canonical endpoint uses)."""
        device_id, device = self.resolve(room_id, capability)
        cap_map = getattr(device, "capabilities", None)
        cap = cap_map.get(capability) if cap_map else None
        if cap is None or action not in cap.actions:
            raise ScenarioProxyError(
                f"Device '{device_id}' has no canonical action "
                f"'{capability}.{action}'",
                "role_unbound",
            )
        steps = cap.actions[action].expand(params)
        result: Any = None
        for i, step in enumerate(steps):
            result = await device.execute_action(step.command, step.params, source="scenario")
            if step.delay_after_ms and i < len(steps) - 1:
                await asyncio.sleep(step.delay_after_ms / 1000)
        return {
            "executed_on": device_id,
            "command": " → ".join(s.command for s in steps),
            "result": result,
        }
