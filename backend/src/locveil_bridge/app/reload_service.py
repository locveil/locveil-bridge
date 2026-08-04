"""Application-layer system reload (CORE-1 Item A).

The background work behind ``POST /reload``, extracted from the system router
so presentation stays a thin adapter with no infrastructure imports. The
composition root constructs this service and keeps everything composition-
shaped on its own side:

- ``client_factory`` builds the replacement MQTT client exactly the way
  startup builds the original (maintenance guard, traffic observer) — the
  reload client used to be a bare re-creation that silently lost both.
- ``on_new_client`` fires the moment the replacement client exists: bootstrap
  re-points every router global at it, re-registers the on-connect catalog
  publish (VWB-32), updates its own shutdown reference, and returns a fresh
  WB virtual-device service bound to the new client (the old one kept
  publishing into the stopped client).
- ``rewire_fleet`` fires after ``initialize_devices`` rebuilds the fleet
  (CORE-15): bootstrap runs the SAME ``wire_fleet`` recipe boot uses (MQTT
  client, WB service, SSE event publisher, dispatch ring, capability maps,
  exposure check) and then reloads the config consumers built after the fleet
  — room definitions + membership, scenario definitions + Layer-0 topology.
  Before this hook the re-initialized fleet ran with NO capability maps
  (canonical dispatch dead fleet-wide, catalog published empty capabilities),
  no SSE publisher, no dispatch ring, and stale rooms/scenarios/topology —
  until the next container restart.
- ``rebuild_scenario_cards`` fires once the new client is CONNECTED (card
  publishes + command-topic subscriptions need a live client, mirroring
  startup's ordering): bootstrap builds a fresh ScenarioWBAdapter over the
  new client + WB service and runs its setup(), which also re-points the
  scenario manager's ``on_active_changed`` hook at the new adapter — the old
  adapter's cards, subscriptions, and hook all died with the old client.
- ``publish_catalog_version`` is the same guarded closure startup uses; the
  explicit call at the end preserves the reload's guaranteed post-reload
  catalog-version nudge.
"""

import logging
from typing import Awaitable, Callable, Dict, Optional, cast

from locveil_bridge.domain.devices.service import DeviceManager
from locveil_bridge.infrastructure.config.manager import ConfigManager
from locveil_bridge.infrastructure.devices.base import BaseDevice
from locveil_bridge.infrastructure.mqtt.client import MQTTClient
from locveil_bridge.infrastructure.wb_device.service import WBVirtualDeviceService

logger = logging.getLogger(__name__)


class ReloadService:
    """Owns the /reload sequence: stop the current client, reload configs and
    device classes, build + adopt a replacement client, re-initialize the
    fleet, re-subscribe, redo WB emulation, republish the catalog version."""

    def __init__(
        self,
        config_manager: ConfigManager,
        device_manager: DeviceManager,
        client_factory: Callable[[], MQTTClient],
        on_new_client: Callable[[MQTTClient], WBVirtualDeviceService],
        rewire_fleet: Callable[[MQTTClient, WBVirtualDeviceService], Awaitable[None]],
        rebuild_scenario_cards: Callable[[MQTTClient, WBVirtualDeviceService], Awaitable[None]],
        publish_catalog_version: Callable[[], Awaitable[None]],
    ):
        self._config_manager = config_manager
        self._device_manager = device_manager
        self._client_factory = client_factory
        self._on_new_client = on_new_client
        self._rewire_fleet = rewire_fleet
        self._rebuild_scenario_cards = rebuild_scenario_cards
        self._publish_catalog_version = publish_catalog_version
        # The current live client — seeded by bootstrap at startup, swapped
        # here on every reload.
        self.mqtt_client: Optional[MQTTClient] = None

    async def reload(self) -> None:
        """Reload configurations and device modules (background task)."""
        try:
            if self.mqtt_client:
                await self.mqtt_client.stop()

            self._config_manager.reload_configs()
            await self._device_manager.load_device_modules()

            new_client = self._client_factory()
            self.mqtt_client = new_client
            wb_service = self._on_new_client(new_client)

            # Shutdown any existing devices, then re-initialize with typed
            # configs. Wire the shared MQTT client BEFORE `initialize_devices`
            # so WB-passthrough devices' setup() can register their
            # state_topic + meta/error subscriptions on the right client.
            # Existing AV drivers don't use mqtt_client in setup() so this is
            # a no-op for them.
            await self._device_manager.shutdown_devices()
            self._device_manager.set_runtime_services(
                mqtt_client=new_client, wb_service=wb_service
            )
            await self._device_manager.initialize_devices(
                self._config_manager.get_all_device_configs()
            )

            # CORE-15: full fleet + config-bundle re-wiring via the composition
            # root's shared recipe (collaborators, capability maps, exposure
            # check, rooms + scenarios + topology). Replaces the old
            # mqtt_client-only safety net — the recipe assigns that too.
            await self._rewire_fleet(new_client, wb_service)

            # Create topic to handler mapping
            topic_handlers: Dict[str, Callable] = {}
            for device_id, device in self._device_manager.devices.items():
                handler = self._device_manager.get_message_handler(device_id)
                if handler:
                    for topic in device.subscribe_topics():
                        topic_handlers[topic] = handler

            # Connect to MQTT broker with topics and handlers
            if topic_handlers:
                await new_client.connect_and_subscribe(topic_handlers)
            else:
                await new_client.connect()

            # Wait for MQTT connection to be fully established
            logger.info("Waiting for MQTT connection to be established after reload...")
            connection_success = await new_client.wait_for_connection(timeout=30.0)
            if not connection_success:
                logger.error(
                    "Failed to establish MQTT connection within timeout after "
                    "reload - WB emulation and scenario cards will be skipped"
                )
            else:
                logger.info("MQTT connection established successfully after reload")

                # Now that MQTT is connected, set up Wirenboard virtual device
                # emulation for all devices
                logger.info("Setting up Wirenboard virtual device emulation after reload...")
                for device_id, device in self._device_manager.devices.items():
                    try:
                        await cast(BaseDevice, device).setup_wb_emulation_if_enabled()
                        logger.debug(
                            f"WB emulation setup completed for device {device_id} after reload"
                        )
                    except Exception as e:
                        logger.error(
                            f"Failed to setup WB emulation for device {device_id} "
                            f"after reload: {str(e)}"
                        )

                # Rebuild the per-room scenario cards over the new client —
                # their publishes, subscriptions, and the on_active_changed
                # hook were all bound to the old one. Needs the live
                # connection, same as startup.
                await self._rebuild_scenario_cards(new_client, wb_service)

            # Bump the retained catalog version so catalog-aware subscribers
            # refetch. Done at the END so the post-reload hash is published;
            # the closure guards its own failures (never masks a successful
            # reload).
            await self._publish_catalog_version()

            logger.info("System reload completed successfully")
        except Exception:
            logger.exception("Error during system reload")
