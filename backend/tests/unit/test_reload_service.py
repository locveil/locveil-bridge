"""CORE-1: the app-layer reload service.

POST /reload's background work was extracted from the system router into
`app/reload_service.py` so presentation keeps zero infrastructure imports
(the import-linter contract now has an empty exception list). These tests pin
the sequence's contract: old client stopped, configs + device classes
reloaded, replacement client built by the composition root's factory and
adopted through the rewire hook BEFORE devices re-initialize, subscriptions
re-established, WB emulation redone, the retained catalog version republished,
and an error never escaping the background task.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from locveil_bridge.app.reload_service import ReloadService


def _make_service(devices=None, handler=None):
    """Build a ReloadService over mocks; returns (service, mocks-namespace)."""
    m = MagicMock()

    m.config_manager = MagicMock()
    m.config_manager.reload_configs = MagicMock()
    m.config_manager.get_all_device_configs = MagicMock(return_value={"cfg": "s"})

    m.device_manager = MagicMock()
    m.device_manager.load_device_modules = AsyncMock()
    m.device_manager.shutdown_devices = AsyncMock()
    m.device_manager.initialize_devices = AsyncMock()
    m.device_manager.devices = devices if devices is not None else {}
    m.device_manager.get_message_handler = MagicMock(return_value=handler)

    m.old_client = MagicMock()
    m.old_client.stop = AsyncMock()

    m.new_client = MagicMock()
    m.new_client.connect = AsyncMock()
    m.new_client.connect_and_subscribe = AsyncMock()
    m.new_client.wait_for_connection = AsyncMock(return_value=True)

    m.wb_service = MagicMock()
    m.client_factory = MagicMock(return_value=m.new_client)
    m.on_new_client = MagicMock(return_value=m.wb_service)
    m.rewire_fleet = AsyncMock()
    m.rebuild_scenario_cards = AsyncMock()
    m.publish_catalog_version = AsyncMock()

    service = ReloadService(
        config_manager=m.config_manager,
        device_manager=m.device_manager,
        client_factory=m.client_factory,
        on_new_client=m.on_new_client,
        rewire_fleet=m.rewire_fleet,
        rebuild_scenario_cards=m.rebuild_scenario_cards,
        publish_catalog_version=m.publish_catalog_version,
    )
    service.mqtt_client = m.old_client
    return service, m


def _fake_device(topics):
    dev = MagicMock()
    dev.subscribe_topics = MagicMock(return_value=topics)
    dev.setup_wb_emulation_if_enabled = AsyncMock()
    return dev


@pytest.mark.asyncio
async def test_reload_swaps_the_client_and_rewires_before_reinit():
    dev = _fake_device(["t/1", "t/2"])
    handler = MagicMock()
    service, m = _make_service(devices={"d1": dev}, handler=handler)

    await service.reload()

    m.old_client.stop.assert_awaited_once()
    m.config_manager.reload_configs.assert_called_once()
    m.device_manager.load_device_modules.assert_awaited_once()
    m.client_factory.assert_called_once()
    # The rewire hook saw the replacement client, and its WB service reached
    # the device manager together with the client, BEFORE initialize_devices.
    m.on_new_client.assert_called_once_with(m.new_client)
    m.device_manager.set_runtime_services.assert_called_once_with(
        mqtt_client=m.new_client, wb_service=m.wb_service
    )
    m.device_manager.shutdown_devices.assert_awaited_once()
    m.device_manager.initialize_devices.assert_awaited_once_with({"cfg": "s"})
    assert service.mqtt_client is m.new_client
    # CORE-15: the composition root's fleet re-wiring recipe ran over the new
    # composition (collaborators + capability maps + rooms/scenarios/topology).
    m.rewire_fleet.assert_awaited_once_with(m.new_client, m.wb_service)
    # Subscriptions re-established on the NEW client with the device's topics.
    m.new_client.connect_and_subscribe.assert_awaited_once_with(
        {"t/1": handler, "t/2": handler}
    )
    m.new_client.connect.assert_not_awaited()
    # WB emulation redone, scenario cards rebuilt over the new composition,
    # the retained catalog version republished.
    dev.setup_wb_emulation_if_enabled.assert_awaited_once()
    m.rebuild_scenario_cards.assert_awaited_once_with(m.new_client, m.wb_service)
    m.publish_catalog_version.assert_awaited_once()


@pytest.mark.asyncio
async def test_reload_without_handlers_connects_bare():
    service, m = _make_service(devices={}, handler=None)

    await service.reload()

    m.new_client.connect.assert_awaited_once()
    m.new_client.connect_and_subscribe.assert_not_awaited()
    m.publish_catalog_version.assert_awaited_once()


@pytest.mark.asyncio
async def test_connection_timeout_skips_wb_emulation_and_scenario_cards():
    dev = _fake_device(["t/1"])
    service, m = _make_service(devices={"d1": dev}, handler=MagicMock())
    m.new_client.wait_for_connection = AsyncMock(return_value=False)

    await service.reload()

    dev.setup_wb_emulation_if_enabled.assert_not_awaited()
    m.rebuild_scenario_cards.assert_not_awaited()
    m.publish_catalog_version.assert_awaited_once()
    # The config-bundle rewire is NOT connection-gated — capability maps,
    # rooms, scenarios, and topology reload even when the broker is down.
    m.rewire_fleet.assert_awaited_once()


@pytest.mark.asyncio
async def test_rewire_fleet_runs_after_reinit_and_before_subscribe():
    """CORE-15 ordering: the recipe needs the NEW device objects (after
    initialize_devices) and must precede subscription setup."""
    order = []
    dev = _fake_device(["t/1"])
    service, m = _make_service(devices={"d1": dev}, handler=MagicMock())
    m.device_manager.initialize_devices = AsyncMock(
        side_effect=lambda *_a, **_k: order.append("init")
    )
    m.rewire_fleet.side_effect = lambda *_a: order.append("rewire")
    m.new_client.connect_and_subscribe = AsyncMock(
        side_effect=lambda *_a, **_k: order.append("subscribe")
    )

    await service.reload()

    assert order == ["init", "rewire", "subscribe"]


@pytest.mark.asyncio
async def test_reload_error_is_contained():
    """The reload runs as a fire-and-forget background task -- an exception
    must be logged, never raised out of reload()."""
    service, m = _make_service()
    m.device_manager.load_device_modules = AsyncMock(side_effect=RuntimeError("boom"))

    await service.reload()  # must not raise

    # Died before the swap: the factory never ran, the old client stays adopted.
    m.client_factory.assert_not_called()
    assert service.mqtt_client is m.old_client


@pytest.mark.asyncio
async def test_no_seeded_client_still_reloads():
    """Defensive: a reload before the client was seeded skips the stop and
    proceeds (mirrors the old task's `if mqtt_client:` guard)."""
    service, m = _make_service()
    service.mqtt_client = None

    await service.reload()

    m.old_client.stop.assert_not_awaited()
    m.client_factory.assert_called_once()
    assert service.mqtt_client is m.new_client
