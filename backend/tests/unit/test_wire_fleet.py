"""CORE-15: the shared boot/reload fleet-wiring recipe.

`wire_fleet` is the ONE copy of the post-`initialize_devices` composition step
— boot and `/reload` both call it, so the two paths cannot drift. The drift is
what CORE-15 fixed: the reload re-assigned only `mqtt_client`, leaving the
re-initialized fleet with no capability maps (canonical dispatch answered
`capability_not_supported` fleet-wide, the catalog published empty
capabilities), no SSE event publisher, and no problem-report dispatch ring
until the next container restart.
"""
from unittest.mock import MagicMock, patch

from locveil_bridge.app.bootstrap import wire_fleet


def _fixture(device_ids=("dev_a", "dev_b")):
    manager = MagicMock()
    manager.devices = {device_id: MagicMock() for device_id in device_ids}
    return manager, MagicMock(), MagicMock(), MagicMock(), MagicMock()


def test_assigns_all_runtime_collaborators_to_every_device(tmp_path):
    manager, client, wb, sse, ring = _fixture()

    with patch("locveil_bridge.app.bootstrap.attach_capability_maps"), patch(
        "locveil_bridge.app.bootstrap.validate_command_exposure", return_value=[]
    ):
        wire_fleet(manager, client, wb, sse, ring, tmp_path)

    for device in manager.devices.values():
        assert device.mqtt_client is client
        assert device.wb_service is wb
        assert device.event_publisher is sse
        assert device.dispatch_ring is ring


def test_attaches_capability_maps_and_runs_exposure_check(tmp_path):
    manager, client, wb, sse, ring = _fixture()

    with patch(
        "locveil_bridge.app.bootstrap.attach_capability_maps"
    ) as attach, patch(
        "locveil_bridge.app.bootstrap.validate_command_exposure", return_value=[]
    ) as exposure:
        wire_fleet(manager, client, wb, sse, ring, tmp_path)

    attach.assert_called_once_with(manager.devices, tmp_path)
    exposure.assert_called_once_with(manager.devices)


def test_exposure_violations_warn_but_do_not_raise(tmp_path):
    manager, client, wb, sse, ring = _fixture()

    with patch("locveil_bridge.app.bootstrap.attach_capability_maps"), patch(
        "locveil_bridge.app.bootstrap.validate_command_exposure",
        return_value=["dev_a.mystery_cmd"],
    ):
        wire_fleet(manager, client, wb, sse, ring, tmp_path)  # must not raise
