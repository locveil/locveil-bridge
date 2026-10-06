"""SCN-19: scenario jobs — the tier-3 async switch API (docs/design/scenarios/scenario_jobs.md §10).

Real capability maps + real topology + real scenario configs (the reconciler-test recipe);
devices are fakes recording (device_id, command) and optionally HOLDING a command on an
asyncio.Event so a chain can be caught mid-step. Tests 1–7 of the design's plan; test 8
(the contract) lives in test_contracts_golden.py.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest
from fastapi import FastAPI

from locveil_bridge.app.bootstrap import create_app
from locveil_bridge.domain.scenarios.jobs import JobRegistry, ScenarioJob, ScenarioJobInProgress, new_job_id
from locveil_bridge.domain.scenarios.models import ScenarioDefinition
from locveil_bridge.domain.scenarios.proxy import ScenarioProxy
from locveil_bridge.domain.scenarios.scenario import Scenario
from locveil_bridge.domain.scenarios.service import ScenarioManager
from locveil_bridge.domain.topology.loader import load_topology
from locveil_bridge.infrastructure.capabilities.loader import attach_capability_maps, load_capability_map
from locveil_bridge.infrastructure.config.manager import ConfigManager
from locveil_bridge.presentation.api.routers import devices as devices_router
from locveil_bridge.presentation.api.routers import scenarios as scenarios_router
from locveil_bridge.presentation.api.sse_manager import SSEChannel, SSEManager

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
CAPS = ROOT / "config" / "capabilities"
TOPOLOGY = load_topology(ROOT / "config" / "topology.json")
GOLDEN = json.loads((ROOT / "contracts" / "catalog" / "catalog.golden.json").read_text(encoding="utf-8"))


# --- fakes ---------------------------------------------------------------------


def _fake_device(calls, device_class, device_id, holds: Optional[Dict[str, asyncio.Event]] = None,
                 confirm: bool = True, **state):
    caps = load_capability_map(device_class, device_id, CAPS)
    st = SimpleNamespace(**{"power": "off", **state})

    async def execute_action(command, params, source="unknown", _id=device_id):
        calls.append((_id, command))
        if holds and _id in holds:
            await holds[_id].wait()  # the chain is caught mid-step here
        p = params or {}
        if command in ("power_on", "power"):
            st.power = p.get("assume_state", "on")
            st.connected = True  # the Auralic's power gate reads `connected`
            if p.get("zone") == 2:
                st.zone2_power = "on"
        elif command == "power_off":
            if p.get("zone") == 2:
                st.zone2_power = "off"
            else:
                st.power = "off"
                st.connected = False
        elif command in ("set_input_source", "set_input") and confirm:
            st.input_source = p.get("source") or p.get("input")
        return {"success": True}

    return SimpleNamespace(
        capabilities=caps,
        get_current_state=lambda _st=st: _st,
        execute_action=execute_action,
        get_name=lambda _id=device_id: f"name:{_id}",
        get_room=lambda: "living_room",
    )


def _devices(calls, holds=None, unconfirmed=()):
    base = {
        "appletv_living": ("AppleTVDevice", {}),
        "processor": ("EMotivaXMC2", {"zone2_power": None, "input_source": None}),
        "living_room_tv": ("LgTv", {"input_source": None}),
        "mf_amplifier": ("WirenboardIRDevice", {"input": None}),
        "streamer": ("AuralicDevice", {"connected": False}),
    }
    return {
        device_id: _fake_device(calls, cls, device_id, holds=holds,
                                confirm=device_id not in unconfirmed, **state)
        for device_id, (cls, state) in base.items()
    }


class _Store:
    def __init__(self):
        self.data: Dict[str, Any] = {}

    async def load(self, key):
        return self.data.get(key)

    async def save(self, key, value):
        self.data[key] = value

    async def delete(self, key):
        self.data.pop(key, None)


def _manager(devices, scenarios=("movie_appletv", "music_auralic"), active=None):
    """`active` marks that scenario active AND believed powered (its involved devices
    report on), the way a running room looks to the planners."""
    device_manager = SimpleNamespace(devices=devices, get_device=lambda i: devices.get(i))
    sm = ScenarioManager(
        device_manager=device_manager,  # type: ignore[arg-type]
        room_manager=SimpleNamespace(),  # type: ignore[arg-type]
        state_repository=_Store(),
        scenario_dir=ROOT / "config" / "scenarios",
    )
    sm.topology = TOPOLOGY
    for name in scenarios:
        defn = ScenarioDefinition.model_validate(
            json.loads((ROOT / "config" / "scenarios" / f"{name}.json").read_text())
        )
        sm.scenario_definitions[name] = defn
        sm.scenario_map[name] = Scenario(defn, device_manager)
    if active:
        sm.active = {"living_room": sm.scenario_map[active]}
        from locveil_bridge.domain.scenarios.reconciler import resolve_targets
        for device_id in resolve_targets(sm.scenario_definitions[active], TOPOLOGY)[2]:
            if device_id in devices:
                st = devices[device_id].get_current_state()
                st.power, st.connected = "on", True
    return sm


def _record(sm) -> List[Tuple[str, Dict[str, Any]]]:
    """Attach observers that record every event the SSE producers would broadcast —
    the job events AND the terminal (active-changed) notification, in one list."""
    events: List[Tuple[str, Dict[str, Any]]] = []

    async def job_observer(event_type, payload):
        events.append((event_type, payload))

    async def terminal_observer(room_id):
        job = sm.jobs.running(room_id)
        active = sm.active.get(room_id)
        payload = {"room_id": room_id, "scenario_id": active.scenario_id if active else None}
        if job is not None and job.finished_at is not None:
            payload.update(job_id=job.job_id, job_state=job.state, duration_ms=job.duration_ms,
                           failures=list(job.failures), powered_off=list(job.powered_off))
        events.append(("scenario_switched" if active else "scenario_shutdown", payload))

    sm.job_observers.append(job_observer)
    sm.active_changed_observers.append(terminal_observer)
    return events


async def _tick():
    """One loop turn — without asyncio.sleep, which the fixture below neutralises."""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    loop.call_soon(fut.set_result, None)
    await fut


async def _until(pred, timeout=1.0):
    """Spin the loop until `pred()` holds (the chain reaches its held dispatch)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        assert loop.time() < deadline, "condition never held"
        await _tick()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    import locveil_bridge.domain.scenarios.reconciler as rec

    async def _nosleep(*a, **k):
        return None

    monkeypatch.setattr(rec.asyncio, "sleep", _nosleep)


# --- 1. the lock against REL-3's ordering ---------------------------------------


async def test_lock_refuses_second_switch_before_first_dispatches_its_second_step():
    calls: list = []
    holds = {"appletv_living": asyncio.Event()}  # the first step (the source's power) is held
    sm = _manager(_devices(calls, holds=holds))
    job = await sm.start_switch("movie_appletv", source="canonical")
    assert job is not None and sm.jobs.running("living_room") is job
    await _until(lambda: calls)  # let the chain reach its first (held) dispatch
    assert calls == [("appletv_living", "power_on")]

    with pytest.raises(ScenarioJobInProgress) as refused:
        await sm.switch_scenario("movie_appletv")
    assert refused.value.job is job
    with pytest.raises(ScenarioJobInProgress):
        await sm.start_switch("music_auralic")
    assert calls == [("appletv_living", "power_on")]  # nothing slipped in

    holds["appletv_living"].set()
    await job.done.wait()
    assert job.state == "succeeded" and job.failures == []
    # REL-3's ARC choreography, byte-for-byte: TV power → processor power → TV input → processor input
    order = [c for c in calls if c[0] in ("living_room_tv", "processor")]
    assert order == [
        ("living_room_tv", "power_on"), ("processor", "power_on"), ("processor", "power_on"),
        ("living_room_tv", "set_input_source"), ("processor", "set_input"),
    ]
    assert sm.jobs.running("living_room") is None
    assert not sm.jobs.lock_for("living_room").locked()
    assert sm.jobs.recent("living_room") == [job]


async def test_rooms_are_independent():
    calls: list = []
    holds = {"living_room_tv": asyncio.Event()}
    sm = _manager(_devices(calls, holds=holds))
    other = ScenarioDefinition(
        scenario_id="children_tv", names={"ru": "ТВ", "en": "TV"}, room_id="children_room",
        source="appletv_children", display="children_room_tv",
    )
    sm.scenario_definitions["children_tv"] = other
    sm.scenario_map["children_tv"] = Scenario(other, sm.device_manager)

    living = await sm.start_switch("movie_appletv")
    assert living is not None
    children = await sm.switch_scenario("children_tv")  # another room: proceeds while living_room is held
    assert children["success"] and children["job_id"] and sm.active["children_room"].scenario_id == "children_tv"
    assert sm.jobs.running("living_room") is living
    holds["living_room_tv"].set()
    await living.done.wait()


# --- 2. event ordering --------------------------------------------------------------


async def test_event_ordering_with_a_not_confirmed_step():
    calls: list = []
    sm = _manager(_devices(calls, unconfirmed=("processor",)), active="music_auralic")
    events = _record(sm)
    result = await sm.switch_scenario("movie_appletv", source="canonical")
    assert result["success"] is False
    job_id = result["job_id"]

    types = [t for t, _ in events]
    assert types[0] == "scenario_job_started"
    assert types[-1] == "scenario_switched"
    assert types.count("scenario_switched") == 1 and "scenario_shutdown" not in types
    assert all(p.get("job_id") == job_id for _, p in events)
    # phase precedes its steps; teardown precedes activation
    phases = [i for i, (t, p) in enumerate(events) if t == "scenario_phase"]
    assert [events[i][1]["phase"] for i in phases] == ["teardown", "activation"]
    teardown_steps = [p for t, p in events if t == "scenario_step" and p["phase"] == "teardown"]
    assert teardown_steps and all(s["device_id"] == "streamer" for s in teardown_steps)
    first_teardown_step = next(i for i, (t, p) in enumerate(events) if t == "scenario_step")
    assert phases[0] < first_teardown_step < phases[1]
    # each step's started precedes its outcome, and outcomes are the executor's words
    seen: Dict[Tuple[str, int], List[str]] = {}
    for t, p in events:
        if t == "scenario_step":
            seen.setdefault((p["phase"], p["index"]), []).append(p["status"])
    for statuses in seen.values():
        assert statuses[0] == "started" and len(statuses) == 2
        assert statuses[1] in ("done", "failed", "not_confirmed")
    processor_input = [p for t, p in events if t == "scenario_step"
                       and p["device_id"] == "processor" and p["domain"] == "input"]
    assert processor_input[-1]["status"] == "not_confirmed"
    assert processor_input[-1]["error"].startswith("gate timeout: input did not reach 'source2'")
    assert all("timestamp" in p for _, p in events[:-1])
    assert all("elapsed_ms" in p for t, p in events if t == "scenario_step")

    terminal = events[-1][1]
    assert terminal["job_state"] == "failed"
    assert [f["device"] for f in terminal["failures"]] == ["processor"]
    assert terminal["powered_off"] == ["streamer"]
    assert isinstance(terminal["duration_ms"], int)

    # GET after the fact shows the same statuses
    record = sm.jobs.get(job_id)
    assert record is not None
    d = record.to_dict()
    assert d["state"] == "failed" and d["from"] == "music_auralic" and d["result_scenario"] == "movie_appletv"
    statuses = {(ph["phase"], s["index"]): s["status"] for ph in d["phases"] for s in ph["steps"]}
    for key, ev in seen.items():
        assert statuses[key] == ev[1]
    assert all(s["started_at"] and s["finished_at"] for ph in d["phases"] for s in ph["steps"])


async def test_stop_is_its_own_job_and_shutdown_is_its_terminal_event():
    calls: list = []
    sm = _manager(_devices(calls), active="movie_appletv")
    for d in ("living_room_tv", "processor", "mf_amplifier", "appletv_living"):
        sm.device_manager.devices[d].get_current_state().power = "on"
    events = _record(sm)
    result = await sm.deactivate("living_room", source="canonical")
    assert result["success"] and result["no_op"] is False
    assert [t for t, _ in events][0] == "scenario_job_started"
    assert events[0][1]["kind"] == "stop" and events[0][1]["target"] == "none"
    assert [p["phase"] for t, p in events if t == "scenario_phase"] == ["teardown"]
    assert events[-1][0] == "scenario_shutdown" and events[-1][1]["job_state"] == "succeeded"
    assert sm.jobs.get(result["job_id"]).to_dict()["result_scenario"] == "none"


# --- 3/4. every door: 409, idempotency, the wait matrix ----------------------------


def _world(calls, holds=None, active=None):
    """The canonical + REST doors wired over one manager (devices router needs the proxy)."""
    sm = _manager(_devices(calls, holds=holds), active=active)
    proxy = ScenarioProxy(sm, sm.device_manager)
    scenarios_router.initialize(sm, SimpleNamespace(), None)  # type: ignore[arg-type]
    devices_router.initialize(SimpleNamespace(), sm.device_manager, None, proxy)
    app = FastAPI()
    app.include_router(scenarios_router.router)
    app.include_router(devices_router.router)
    return sm, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


CANON = "/devices/scenario_manager_living_room/canonical"


async def test_409_on_every_door_and_the_idempotency_table():
    calls: list = []
    holds = {"living_room_tv": asyncio.Event()}
    sm, client = _world(calls, holds=holds)
    async with client:
        r = await client.post(CANON, json={"capability": "scenario", "action": "set",
                                           "params": {"value": "movie_appletv"}, "wait": False})
        assert r.status_code == 202, r.text
        job_id = r.json()["state"]["job_id"]
        await _until(lambda: ("living_room_tv", "power_on") in calls)  # caught at the held TV step

        # canonical: set (same target), off
        for body in ({"capability": "scenario", "action": "set", "params": {"value": "movie_appletv"}},
                     {"capability": "scenario", "action": "off"}):
            r = await client.post(CANON, json=body)
            assert r.status_code == 409, r.text
            err = r.json()["detail"]["error"]
            assert err["code"] == "job_in_progress" and err["job_id"] == job_id
            assert r.json()["detail"]["success"] is False and r.json()["detail"]["state"] is None
        # REST: switch, start, shutdown (shutdown's "not active" check comes first — the
        # room is idle until the job ends, so start/switch are the REST doors that meet it)
        r = await client.post("/scenario/switch", json={"id": "music_auralic"})
        assert r.status_code == 409 and r.json()["detail"] == {
            "code": "job_in_progress", "room_id": "living_room", "job_id": job_id,
            "kind": "switch", "target": "movie_appletv"}
        r = await client.post("/scenario/start", json={"id": "music_auralic"})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "job_in_progress"
        assert calls == [("appletv_living", "power_on"), ("living_room_tv", "power_on")]

        holds["living_room_tv"].set()
        await sm.jobs.get(job_id).done.wait()

        # force_reconcile + REST shutdown meet the lock while a reconcile runs
        holds["processor"] = asyncio.Event()
        reconcile = asyncio.create_task(client.post(
            "/scenario/movie_appletv/force_reconcile", json={"device_id": "processor"}))
        await _until(lambda: any(c == ("processor", "power_on") for c in calls[1:]) and sm.jobs.running("living_room") is not None)
        running = sm.jobs.running("living_room")
        assert running is not None and running.kind == "reconcile"
        r = await client.post("/scenario/shutdown", json={"id": "movie_appletv"})
        assert r.status_code == 409 and r.json()["detail"]["kind"] == "reconcile"
        r = await client.post("/scenario/movie_appletv/force_reconcile", json={"device_id": "living_room_tv"})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "job_in_progress"
        holds["processor"].set()
        r = await reconcile
        assert r.status_code == 200 and r.json()["job_id"] == running.job_id

        # idempotency: already at target → 200 no_op, no job
        before = len(sm.jobs.recent("living_room"))
        r = await client.post(CANON, json={"capability": "scenario", "action": "set",
                                           "params": {"value": "movie_appletv"}})
        assert r.status_code == 200 and r.json()["no_op"] is True
        assert "job_id" not in r.json()["state"] and len(sm.jobs.recent("living_room")) == before
        r = await client.post("/scenario/switch", json={"id": "movie_appletv", "wait": False})
        assert r.status_code == 200 and "already active" in r.json()["message"]

        # stop → a new job after the switch; then idle off → 200 no_op
        r = await client.post(CANON, json={"capability": "scenario", "action": "off"})
        assert r.status_code == 200 and r.json()["no_op"] is False and r.json()["state"]["scenario"] == "none"
        r = await client.post(CANON, json={"capability": "scenario", "action": "off"})
        assert r.status_code == 200 and r.json()["no_op"] is True
        r = await client.post(CANON, json={"capability": "scenario", "action": "off", "wait": False})
        assert r.status_code == 200 and r.json()["no_op"] is True


async def test_stop_during_switch_is_refused_not_queued():
    calls: list = []
    holds = {"living_room_tv": asyncio.Event()}
    sm, client = _world(calls, holds=holds)
    async with client:
        r = await client.post("/scenario/switch", json={"id": "movie_appletv", "wait": False})
        assert r.status_code == 202
        body = r.json()
        assert body == {"job_id": body["job_id"], "room_id": "living_room", "kind": "switch",
                        "target": "movie_appletv", "max_duration_ms": body["max_duration_ms"]}
        r = await client.post(CANON, json={"capability": "scenario", "action": "off", "wait": False})
        assert r.status_code == 409 and r.json()["detail"]["error"]["job_id"] == body["job_id"]
        with pytest.raises(ScenarioJobInProgress):
            await sm.start_stop("living_room")
        holds["living_room_tv"].set()
        await sm.jobs.get(body["job_id"]).done.wait()
        # no second chain ran, and nothing was powered off:
        assert sum(1 for c in calls if c == ("living_room_tv", "power_on")) == 1
        assert not any(c[1] == "power_off" for c in calls)


async def test_wait_matrix_on_the_canonical_endpoint():
    calls: list = []
    sm, client = _world(calls, active="music_auralic")
    async with client:
        # wait: true — as today plus state.job_id
        r = await client.post(CANON, json={"capability": "scenario", "action": "set",
                                           "params": {"value": "movie_appletv"}, "wait": True})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["success"] is True and body["no_op"] is False
        assert set(body["state"]) == {"scenario", "job_id", "powered_off", "failures"}
        assert body["state"]["scenario"] == "movie_appletv" and body["state"]["powered_off"] == ["streamer"]
        assert sm.jobs.get(body["state"]["job_id"]).state == "succeeded"

        # wait: false — 202, state.scenario is the PRE-switch value, then the job runs out
        r = await client.post(CANON, json={"capability": "scenario", "action": "set",
                                           "params": {"value": "music_auralic"}, "wait": False})
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["success"] is True and body["no_op"] is False and body["error"] is None
        assert body["state"]["scenario"] == "movie_appletv"
        assert set(body["state"]) == {"scenario", "job_id", "max_duration_ms"}
        job = sm.jobs.get(body["state"]["job_id"])
        assert job.state == "running"
        await job.done.wait()
        assert job.state == "succeeded" and sm.active["living_room"].scenario_id == "music_auralic"


def _golden_ceiling(value: str) -> int:
    sm = next(d for d in GOLDEN["devices"] if d["id"] == "scenario_manager_living_room")
    cap = next(c for c in sm["capabilities"] if c["name"] == "scenario")
    field = next(f for f in cap["fields"] if f["name"] == "scenario")
    return next(v["max_duration_ms"] for v in field["values"] if v["canonical"] == value)


async def test_202_max_duration_equals_the_catalogs_number(monkeypatch):
    """The 202's `max_duration_ms` is the golden's — same ceiling function over the same
    config (§5.4). Built over the offline catalog's world: every device as a stand-in
    with its real capability map, every living-room scenario loaded."""
    monkeypatch.chdir(ROOT)
    typed = ConfigManager(config_dir="config").get_all_typed_configs()
    devices: Dict[str, Any] = {}
    calls: list = []
    for device_id, cfg in typed.items():
        st = SimpleNamespace(power="off", input_source=None, input=None, zone2_power=None, connected=False)

        async def execute_action(command, params, source="unknown", _id=device_id, _st=st):
            calls.append((_id, command))
            if command in ("power_on", "power"):
                _st.power = "on"
            return {"success": True}

        devices[device_id] = SimpleNamespace(
            config=cfg, capabilities=None, room=getattr(cfg, "room", None),
            get_current_state=lambda _st=st: _st, execute_action=execute_action,
        )
        devices[device_id].get_room = lambda _d=devices[device_id]: _d.room
    attach_capability_maps(devices, ROOT / "config" / "capabilities")
    device_manager = SimpleNamespace(devices=devices, get_device=lambda i: devices.get(i))
    sm = ScenarioManager(device_manager=device_manager, room_manager=SimpleNamespace(),  # type: ignore[arg-type]
                         state_repository=_Store(), scenario_dir=ROOT / "config" / "scenarios")
    await sm.load_scenarios()
    sm.topology = TOPOLOGY
    proxy = ScenarioProxy(sm, device_manager)

    job = await proxy.start_activate("living_room", "movie_zappiti")
    assert job is not None
    assert job.max_duration_ms == _golden_ceiling("movie_zappiti") == proxy.max_duration_ms("living_room", "movie_zappiti")
    await job.done.wait()
    stop = await proxy.start_deactivate("living_room")
    assert stop is not None
    assert stop.max_duration_ms == _golden_ceiling("none") == proxy.deactivate_ceiling_ms("living_room")
    await stop.done.wait()


async def test_no_op_starts_no_job_and_emits_no_event():
    calls: list = []
    sm = _manager(_devices(calls), active="movie_appletv")
    events = _record(sm)
    assert await sm.start_switch("movie_appletv") is None
    result = await sm.switch_scenario("movie_appletv")
    assert result == {"success": True, "powered_off": [], "failures": [], "job_id": None, "no_op": True}
    assert await sm.start_stop("children_room") is None
    assert events == [] and calls == [] and sm.jobs.rooms() == []


# --- 5. the restart path ------------------------------------------------------------


async def test_fresh_manager_knows_no_job_and_the_ring_keeps_twenty():
    calls: list = []
    sm, client = _world(calls)
    async with client:
        r = await client.get("/scenario/jobs/j-living_room-20261006T153112Z-7f3a")
        assert r.status_code == 404
        assert r.json()["detail"] == {"code": "job_unknown", "job_id": "j-living_room-20261006T153112Z-7f3a"}
        r = await client.get("/scenario/jobs", params={"room": "living_room"})
        assert r.status_code == 200 and r.json() == {"room_id": "living_room", "running": None, "recent": []}
        r = await client.get("/scenario/jobs")
        assert r.status_code == 200 and r.json() == {"rooms": {}}

    registry = JobRegistry()
    ids = []
    for _ in range(21):
        job = ScenarioJob(job_id=new_job_id("living_room"), room_id="living_room", kind="switch",
                          target="x", from_="none", source="rest", max_duration_ms=1)
        await registry.accept(job)
        job.finish("x")
        registry.release(job)
        ids.append(job.job_id)
    recent = [j.job_id for j in registry.recent("living_room")]
    assert len(recent) == 20 and recent == list(reversed(ids[1:]))  # newest first; the 21st-oldest forgotten
    assert registry.get(ids[0]) is None and registry.get(ids[-1]) is not None


async def test_jobs_endpoints_serve_the_record():
    calls: list = []
    sm, client = _world(calls, active="music_auralic")
    async with client:
        r = await client.post(CANON, json={"capability": "scenario", "action": "set",
                                           "params": {"value": "movie_appletv"}})
        job_id = r.json()["state"]["job_id"]
        r = await client.get(f"/scenario/jobs/{job_id}")
        assert r.status_code == 200
        d = r.json()
        assert d["job_id"] == job_id and d["from"] == "music_auralic" and d["source"] == "canonical"
        assert d["state"] == "succeeded" and d["result_scenario"] == "movie_appletv"
        assert [p["phase"] for p in d["phases"]] == ["teardown", "activation"]
        assert all(s["status"] == "done" for p in d["phases"] for s in p["steps"])
        assert d["duration_ms"] >= 0 and d["started_at"].endswith("Z") and d["finished_at"].endswith("Z")
        r = await client.get("/scenario/jobs", params={"room": "living_room"})
        assert r.json()["running"] is None and r.json()["recent"][0]["job_id"] == job_id
        r = await client.get("/scenario/jobs")
        assert list(r.json()["rooms"]) == ["living_room"]


# --- 6. the slow consumer -----------------------------------------------------------


async def test_slow_subscriber_is_dropped_and_the_producer_never_blocks():
    mgr = SSEManager()
    slow: asyncio.Queue = asyncio.Queue(maxsize=2)
    fast: asyncio.Queue = asyncio.Queue(maxsize=100)
    await mgr.add_connection(SSEChannel.SCENARIOS, slow)
    await mgr.add_connection(SSEChannel.SCENARIOS, fast)
    for i in range(5):
        await asyncio.wait_for(
            mgr.broadcast(SSEChannel.SCENARIOS, "scenario_step", {"index": i}), timeout=0.2
        )
    assert fast.qsize() == 5
    assert mgr.is_dropped(slow)
    stats = await mgr.get_channel_stats()
    assert stats["scenarios"] == 1  # the slow one is gone, the fast one stays


async def test_executor_timing_is_unaffected_by_a_stalled_subscriber():
    calls: list = []
    sm = _manager(_devices(calls))
    stalled: asyncio.Queue = asyncio.Queue(maxsize=1)
    mgr = SSEManager()
    await mgr.add_connection(SSEChannel.SCENARIOS, stalled)

    async def producer(event_type, payload):
        await mgr.broadcast(SSEChannel.SCENARIOS, event_type, payload)

    sm.job_observers.append(producer)
    result = await asyncio.wait_for(sm.switch_scenario("movie_appletv"), timeout=2.0)
    assert result["success"] and mgr.is_dropped(stalled)


# --- 7. reload under a job ----------------------------------------------------------


async def test_reload_waits_for_the_running_job_then_proceeds_after_the_bound():
    calls: list = []
    holds = {"living_room_tv": asyncio.Event()}
    sm = _manager(_devices(calls, holds=holds))
    assert await sm.wait_for_jobs() is True  # nothing running: immediate
    job = await sm.start_switch("movie_appletv")
    assert job is not None
    job.max_duration_ms = 0  # the bound is max_duration_ms + extra
    assert await sm.wait_for_jobs(extra_s=0.05) is False  # wedged: proceeds after the bound
    holds["living_room_tv"].set()
    assert await asyncio.wait_for(sm.wait_for_jobs(extra_s=1.0), timeout=2.0) is True
    assert job.state == "succeeded"


async def test_process_shutdown_cancels_a_running_job_without_a_terminal_event():
    calls: list = []
    holds = {"living_room_tv": asyncio.Event()}
    sm = _manager(_devices(calls, holds=holds))
    events = _record(sm)
    job = await sm.start_switch("movie_appletv")
    assert job is not None
    await _until(lambda: ("living_room_tv", "power_on") in calls)  # parked inside the held dispatch
    await sm.shutdown()
    await _until(lambda: not sm.jobs.lock_for("living_room").locked())
    assert job.state == "running" and not job.done.is_set()
    types = [t for t, _ in events]
    assert types[0] == "scenario_job_started" and types[-1] == "scenario_step"  # stopped at a step boundary
    assert "scenario_switched" not in types and "scenario_shutdown" not in types  # no terminal event
    assert not sm.jobs.lock_for("living_room").locked()


def test_openapi_carries_the_job_surface():
    schema = create_app().openapi()
    schemas = schema["components"]["schemas"]
    for name in ("ScenarioJob", "ScenarioJobPhase", "ScenarioJobStep", "ScenarioJobAccepted",
                 "ScenarioJobStartedEvent", "ScenarioPhaseEvent", "ScenarioStepEvent",
                 "ScenarioSwitchedEvent", "ScenarioShutdownEvent"):
        assert name in schemas, name
    assert "job_in_progress" in schemas["CanonicalErrorCode"]["enum"]
    assert "job_id" in schemas["CanonicalError"]["properties"]
    assert "/scenario/jobs/{job_id}" in schema["paths"] and "/scenario/jobs" in schema["paths"]
    for model in ("SwitchScenarioRequest", "StartScenarioRequest", "ShutdownScenarioRequest"):
        assert "wait" in schemas[model]["properties"], model
    assert "202" in schema["paths"]["/devices/{device_id}/canonical"]["post"]["responses"]
