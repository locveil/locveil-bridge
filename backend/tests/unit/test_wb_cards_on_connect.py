"""CORE-14: WB device cards + scenario cards publish on first MQTT connect.

The 2026-08-01 power-outage cold boot lost the race to the host network:
bootstrap's 30 s MQTT wait timed out and the old connect-or-skip gate skipped
WB emulation + scenario cards PERMANENTLY — connect attempt 5/5 then succeeded
20 s later, and the persistence-less broker was left with no bridge cards until
a manual restart (house symptom: wb-rules `failed to SetValue for unexisting
control kitchen_hood/set_light`). Card publishing now lives in a one-shot
on-connect callback built by `make_wb_cards_publisher`: whichever comes first —
the boot-time direct call or the first (re)connect — publishes, every later
invocation is a no-op (setup publishes config-derived initial control values,
so re-running on a reconnect would clobber live values with defaults).
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiomqtt import MqttError

from locveil_bridge.app.bootstrap import make_wb_cards_publisher
from locveil_bridge.infrastructure.mqtt.client import MQTTClient


def _make_fixture(device_ids=("dev_a", "dev_b")):
    """Fake device manager + scenario adapter; returns (manager, adapter)."""
    manager = MagicMock()
    manager.devices = {}
    for device_id in device_ids:
        device = MagicMock()
        device.setup_wb_emulation_if_enabled = AsyncMock()
        manager.devices[device_id] = device
    adapter = MagicMock()
    adapter.setup = AsyncMock()
    return manager, adapter


@pytest.mark.asyncio
async def test_publishes_all_cards_exactly_once():
    """First invocation sets up every device + the scenario adapter; the latch
    makes a second invocation (a later reconnect) a no-op."""
    manager, adapter = _make_fixture()
    publish = make_wb_cards_publisher(manager, lambda: adapter)

    await publish()
    await publish()

    for device in manager.devices.values():
        device.setup_wb_emulation_if_enabled.assert_awaited_once()
    adapter.setup.assert_awaited_once()


@pytest.mark.asyncio
async def test_one_failing_device_does_not_stop_the_rest():
    """A per-device setup failure is contained — the remaining devices and the
    scenario cards still publish (same containment the old inline loop had)."""
    manager, adapter = _make_fixture()
    manager.devices["dev_a"].setup_wb_emulation_if_enabled = AsyncMock(
        side_effect=RuntimeError("boom")
    )

    publish = make_wb_cards_publisher(manager, lambda: adapter)
    await publish()

    manager.devices["dev_b"].setup_wb_emulation_if_enabled.assert_awaited_once()
    adapter.setup.assert_awaited_once()


@pytest.mark.asyncio
async def test_scenario_adapter_failure_is_contained():
    """A raising adapter.setup() must not escape into the client's callback loop."""
    manager, adapter = _make_fixture()
    adapter.setup = AsyncMock(side_effect=RuntimeError("boom"))

    publish = make_wb_cards_publisher(manager, lambda: adapter)
    await publish()  # must not raise

    for device in manager.devices.values():
        device.setup_wb_emulation_if_enabled.assert_awaited_once()


@pytest.mark.asyncio
async def test_scenario_adapter_resolved_at_fire_time():
    """The adapter accessor is called when the cards publish, not when the
    publisher is built — a /reload's adapter rebind must be honored."""
    manager, old_adapter = _make_fixture()
    holder = {"adapter": old_adapter}
    publish = make_wb_cards_publisher(manager, lambda: holder["adapter"])

    new_adapter = MagicMock()
    new_adapter.setup = AsyncMock()
    holder["adapter"] = new_adapter

    await publish()

    old_adapter.setup.assert_not_awaited()
    new_adapter.setup.assert_awaited_once()


class _ConnectThenDrop:
    """One connect episode: connection succeeds, then the stream raises — an
    MqttError for the first `drops` episodes, CancelledError afterwards (the
    test_mqtt_reconnect harness)."""

    def __init__(self, episodes: dict, drops: int):
        self._episodes = episodes
        self._drops = drops

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def subscribe(self, *_a, **_k):
        return None

    @property
    def messages(self):
        return self._gen()

    async def _gen(self):
        self._episodes["n"] += 1
        if self._episodes["n"] > self._drops:
            raise asyncio.CancelledError()
        raise MqttError("simulated transient drop")
        yield  # noqa: unreachable — makes this an async generator


@pytest.mark.asyncio
async def test_lost_race_boot_publishes_on_first_connect_then_latches():
    """The CORE-14 scenario end-to-end at the client level: the publisher is
    registered before any connect (the boot that lost the race), the client then
    connects, drops, and reconnects — cards publish exactly once (on the first
    connect; the latch blocks the reconnect's re-fire)."""
    client = MQTTClient({
        "host": "localhost", "port": 1883, "client_id": "test", "keepalive": 60,
        "auth": {},
    })
    manager, adapter = _make_fixture()
    publish = make_wb_cards_publisher(manager, lambda: adapter)
    client.on_connect_callbacks.append(publish)

    episodes = {"n": 0}
    with patch(
        "locveil_bridge.infrastructure.mqtt.client.Client",
        side_effect=lambda *a, **k: _ConnectThenDrop(episodes, 1),
    ), patch(
        "locveil_bridge.infrastructure.mqtt.client.asyncio.sleep", new=AsyncMock()
    ):
        await client._run_mqtt_client({"hostname": "h", "port": 1883}, [])

    assert episodes["n"] == 2  # initial connect + one reconnect
    for device in manager.devices.values():
        device.setup_wb_emulation_if_enabled.assert_awaited_once()
    adapter.setup.assert_awaited_once()
