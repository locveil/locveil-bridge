"""WB «Сценарии» cards — one per scenario-bearing room (SCN-6, canonical_first.md §3).

Driven adapter: renders each per-room Scenario Manager entity as a Wirenboard virtual
device via :class:`WBVirtualDeviceService` and routes its control writes through the
domain :class:`ScenarioProxy` — the SAME fire-time resolution REST and the UI use.

Card shape per room (write-proxy / read-direct rule, design §3):

- ``scenario`` — text control, the entity's ONLY state: the room's active scenario id
  (or ``none``), retained. Writing a scenario id activates (reconciler diff transition);
  writing ``none`` deactivates.
- Curated stateless pushbuttons — play / pause / stop / volume_up / volume_down —
  each mapped to a canonical ``(capability, action)`` and resolved against the room's
  active scenario at press time. No state is mirrored for these (real device state
  lives on the real devices' cards).
"""

import logging
from typing import Any, Dict

from locveil_bridge.domain.ports import MessageBusPort
from locveil_bridge.domain.scenarios.jobs import ScenarioJobInProgress
from locveil_bridge.domain.scenarios.proxy import (
    NO_SCENARIO,
    ScenarioProxy,
    ScenarioProxyError,
    manager_entity_id,
)
from locveil_bridge.infrastructure.wb_device.service import WBVirtualDeviceService

logger = logging.getLogger(__name__)

# WB pushbutton control name -> canonical (capability, action).
_CONTROL_ACTIONS: Dict[str, tuple] = {
    "play": ("playback", "play"),
    "pause": ("playback", "pause"),
    "stop": ("playback", "stop"),
    "volume_up": ("volume", "up"),
    "volume_down": ("volume", "down"),
}


class ScenarioWBAdapter:
    """Publishes the per-room Scenario Manager WB cards and executes their writes."""

    def __init__(self, proxy: ScenarioProxy, wb_service: WBVirtualDeviceService,
                 message_bus: MessageBusPort):
        self.proxy = proxy
        self.wb_service = wb_service
        self.message_bus = message_bus

    def _card_config(self, room_id: str) -> Dict[str, Any]:
        """Synthetic device-config dict for one room's card (consumed by
        WBVirtualDeviceService's dict path)."""
        return {
            "device_id": manager_entity_id(room_id),
            "names": {"ru": "Сценарии", "en": "Scenarios"},
            "enable_wb_emulation": True,
            "commands": {
                "scenario": {
                    "action": "scenario_set",
                    "description": "Сценарий",
                    "params": [{
                        "name": "value", "type": "string", "required": True,
                        "description": "Scenario id to activate, or 'none' to deactivate",
                    }],
                },
                "play": {"action": "play", "description": "Play"},
                "pause": {"action": "pause", "description": "Pause"},
                "stop": {"action": "stop", "description": "Stop"},
                "volume_up": {"action": "volume_up", "description": "Volume +"},
                "volume_down": {"action": "volume_down", "description": "Volume -"},
            },
            "wb_controls": {
                "scenario": {"type": "text", "title": {"en": "Сценарий"}, "order": 1},
                "play": {"type": "pushbutton", "title": {"en": "Play"}, "order": 2},
                "pause": {"type": "pushbutton", "title": {"en": "Pause"}, "order": 3},
                "stop": {"type": "pushbutton", "title": {"en": "Stop"}, "order": 4},
                "volume_up": {"type": "pushbutton", "title": {"en": "Громче"}, "order": 5},
                "volume_down": {"type": "pushbutton", "title": {"en": "Тише"}, "order": 6},
            },
        }

    def _executor(self, room_id: str):
        """Command executor closure for one room's card (WBVirtualDeviceService callback)."""

        async def _execute(control_name: str, payload: str, params: Dict[str, Any]) -> None:
            try:
                if control_name == "scenario":
                    value = (params or {}).get("value") or payload
                    # SCN-19: the card is a door onto the room's job lock like any other;
                    # a busy room refuses (ScenarioJobInProgress) and the card's value is
                    # re-published by the running job's terminal notification.
                    if value == NO_SCENARIO:
                        await self.proxy.deactivate(room_id, source="wb_card")
                    else:
                        await self.proxy.activate(room_id, value, source="wb_card")
                    return
                mapped = _CONTROL_ACTIONS.get(control_name)
                if mapped is None:
                    logger.warning(f"Scenario card '{room_id}': unknown control '{control_name}'")
                    return
                capability, action = mapped
                result = await self.proxy.execute(room_id, capability, action)
                logger.info(
                    f"Scenario card '{room_id}': {capability}.{action} -> "
                    f"{result.get('executed_on')} ({result.get('command')})"
                )
            except ScenarioProxyError as e:
                # WB has no error channel for a card write; log loudly and move on.
                logger.warning(f"Scenario card '{room_id}': {control_name} rejected: {e}")
            except ScenarioJobInProgress as e:
                # The room is busy: the card write is refused, never queued (SCN-19 §4).
                logger.warning(f"Scenario card '{room_id}': {control_name} refused — {e}")
            except Exception as e:
                logger.error(f"Scenario card '{room_id}': {control_name} failed: {e}")

        return _execute

    def _make_wb_handler(self, entity_id: str):
        """Message-bus callback for one card's `/on` topics. Wraps handle_wb_message so
        the callback satisfies the port's `(str, str) -> Awaitable[None] | None` shape
        (handle_wb_message returns a bool we deliberately discard)."""

        async def _handler(topic: str, payload: str) -> None:
            await self.wb_service.handle_wb_message(topic, payload, entity_id)

        return _handler

    async def setup(self) -> None:
        """Publish one card per scenario-bearing room, subscribe its command topics, seed
        the value topic, and hook active-scenario changes to the value-topic publisher."""
        for room_id in self.proxy.rooms():
            config = self._card_config(room_id)
            entity_id = manager_entity_id(room_id)
            ok = await self.wb_service.setup_wb_device_from_config(
                config, self._executor(room_id), device_type="scenario_manager"
            )
            if not ok:
                logger.error(f"Failed to set up scenario WB card for room '{room_id}'")
                continue
            for topic in self.wb_service.get_subscription_topics_from_config(config):
                await self.message_bus.subscribe(topic, self._make_wb_handler(entity_id))
            await self.publish_active(room_id)
            logger.info(f"Scenario WB card ready for room '{room_id}' ({entity_id})")

        # Domain hook: value topic tracks activation changes from ANY path (REST, UI,
        # voice, restore) — the card is a read model of the room slot.
        self.proxy.scenario_manager.on_active_changed = self.publish_active

    async def publish_active(self, room_id: str) -> None:
        """Reflect the room's active scenario id (or `none`) on the card's value topic."""
        await self.wb_service.update_control_state(
            manager_entity_id(room_id), "scenario", self.proxy.active_id(room_id)
        )
