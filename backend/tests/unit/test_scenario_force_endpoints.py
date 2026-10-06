"""SCN-11: the per-device force-reconcile surface — ScenarioManager methods + the two
REST endpoints (GET /scenario/{id}/reconcile_preview, POST /scenario/{id}/force_reconcile).

Real capability maps + real topology + real scenario configs (the reconciler-test
recipe); devices are fakes recording (device_id, command, params) so the force /
assume_state injection is observable end-to-end through execute_plan.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from locveil_bridge.domain.scenarios.models import ScenarioDefinition
from locveil_bridge.domain.scenarios.scenario import Scenario, ScenarioError
from locveil_bridge.domain.scenarios.service import ScenarioManager
from locveil_bridge.infrastructure.capabilities.loader import load_capability_map
from locveil_bridge.domain.topology.loader import load_topology
from locveil_bridge.presentation.api.routers import scenarios as scenarios_router

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
CAPS = ROOT / "config" / "capabilities"
TOPOLOGY = load_topology(ROOT / "config" / "topology.json")


def _fake_device(calls, device_class, device_id, **state):
    caps = load_capability_map(device_class, device_id, CAPS)
    st = SimpleNamespace(**{"power": "off", **state})

    async def execute_action(command, params, source="unknown", _id=device_id):
        calls.append((_id, command, dict(params or {})))
        # Reflect the command on believed state the way a real feedback device
        # reports back — since SCN-14 an unconfirmed feedback gate is a FAILURE,
        # so a static-state fake would (correctly) fail the plan.
        p = params or {}
        if command in ("power_on", "power"):
            st.power = p.get("assume_state", "on")
        elif command == "power_off":
            st.power = "off"
        elif command in ("set_input_source", "set_input"):
            st.input_source = p.get("source") or p.get("input")
        return {"success": True}

    return SimpleNamespace(
        capabilities=caps,
        get_current_state=lambda _st=st: _st,
        execute_action=execute_action,
        get_name=lambda _id=device_id: f"name:{_id}",
    )


def _devices(calls, **state_overrides):
    base = {
        "appletv_living": ("AppleTVDevice", {}),
        "processor": ("EMotivaXMC2", {"zone2_power": None, "input_source": None}),
        "living_room_tv": ("LgTv", {"input_source": None}),
        "mf_amplifier": ("WirenboardIRDevice", {"input": None}),
    }
    out = {}
    for device_id, (cls, state) in base.items():
        merged = {**state, **state_overrides.get(device_id, {})}
        out[device_id] = _fake_device(calls, cls, device_id, **merged)
    return out


class _Store:
    async def load(self, key):
        return None

    async def save(self, key, value):
        pass

    async def delete(self, key):
        pass


def _manager(devices, active=True, scenario_name="movie_appletv"):
    device_manager = SimpleNamespace(devices=devices)
    sm = ScenarioManager(
        device_manager=device_manager,  # type: ignore[arg-type]
        room_manager=SimpleNamespace(),  # type: ignore[arg-type]
        state_repository=_Store(),
        scenario_dir=ROOT / "config" / "scenarios",
    )
    sm.topology = TOPOLOGY
    defn = ScenarioDefinition.model_validate(
        json.loads((ROOT / "config" / "scenarios" / f"{scenario_name}.json").read_text())
    )
    scenario = Scenario(defn, device_manager)
    sm.scenario_definitions = {scenario_name: defn}
    sm.scenario_map = {scenario_name: scenario}
    if active:
        sm.active = {defn.room_id or "living_room": scenario}
    return sm


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    import locveil_bridge.domain.scenarios.reconciler as rec

    async def _nosleep(*a, **k):
        return None

    monkeypatch.setattr(rec.asyncio, "sleep", _nosleep)


def _client(sm) -> TestClient:
    scenarios_router.initialize(sm, SimpleNamespace(), None)  # type: ignore[arg-type]
    app = FastAPI()
    app.include_router(scenarios_router.router)
    return TestClient(app)


# --- ScenarioManager methods -------------------------------------------------


def test_preview_requires_active_scenario():
    sm = _manager(_devices([]), active=False)
    with pytest.raises(ScenarioError) as e:
        sm.reconcile_preview("movie_appletv")
    assert e.value.error_type == "not_active"


@pytest.mark.asyncio
async def test_force_reconcile_executes_only_target_with_force_params():
    calls = []
    # Amp believed fully at target -> a normal activation would skip it entirely.
    sm = _manager(_devices(calls, mf_amplifier={"power": "on", "input": "aux2"}))

    plan, result, job = await sm.force_reconcile_device("movie_appletv", "mf_amplifier")
    assert job.kind == "reconcile" and job.state == "succeeded"  # SCN-19: a forced plan is a job

    assert result.success and not result.failures
    assert {c[0] for c in calls} == {"mf_amplifier"}  # nobody else was commanded
    assert all(c[2].get("force") is True for c in calls)
    power_calls = [c for c in calls if c[1] == "power"]
    assert power_calls and power_calls[0][2]["assume_state"] == "on"  # toggle claims target


@pytest.mark.asyncio
async def test_force_reconcile_uninvolved_device_raises():
    sm = _manager(_devices([]))
    with pytest.raises(ScenarioError) as e:
        await sm.force_reconcile_device("movie_appletv", "kitchen_hood")
    assert e.value.error_type == "nothing_to_force"


# --- REST endpoints ------------------------------------------------------------


def test_preview_endpoint_rows_and_sync_flags():
    calls = []
    sm = _manager(_devices(calls, mf_amplifier={"power": "on", "input": "aux2"}))
    r = _client(sm).get("/scenario/movie_appletv/reconcile_preview")
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["scenario_id"] == "movie_appletv"
    rows = {row["device_id"]: row for row in body["devices"]}

    amp = rows["mf_amplifier"]
    assert amp["in_sync"] is True  # believed matches — exactly where force matters
    assert amp["reconcilable"] is True and amp["steps"]
    assert amp["device_name"] == "name:mf_amplifier"

    tv = rows["living_room_tv"]
    assert tv["in_sync"] is False
    doms = {c["domain"]: c for c in tv["comparisons"]}
    assert doms["power"]["believed"] == "off" and doms["power"]["desired"] == "on"
    assert tv["eta_ms"] > 0

    assert not calls  # the preview is a pure read: nothing was commanded


def test_preview_endpoint_404_unknown_409_inactive():
    sm = _manager(_devices([]), active=False)
    client = _client(sm)
    assert client.get("/scenario/nope/reconcile_preview").status_code == 404
    assert client.get("/scenario/movie_appletv/reconcile_preview").status_code == 409


def test_force_endpoint_executes_and_reports_steps():
    calls = []
    sm = _manager(_devices(calls))
    r = _client(sm).post(
        "/scenario/movie_appletv/force_reconcile", json={"device_id": "living_room_tv"}
    )
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["success"] is True and body["failures"] == []
    assert {c[0] for c in calls} == {"living_room_tv"}
    assert [s["domain"] for s in body["executed"]] == ["power", "input"]


def test_force_endpoint_404_uninvolved_409_inactive():
    sm = _manager(_devices([]))
    client = _client(sm)
    r = client.post("/scenario/movie_appletv/force_reconcile", json={"device_id": "kitchen_hood"})
    assert r.status_code == 404

    sm_inactive = _manager(_devices([]), active=False)
    client = _client(sm_inactive)
    r = client.post("/scenario/movie_appletv/force_reconcile", json={"device_id": "living_room_tv"})
    assert r.status_code == 409
