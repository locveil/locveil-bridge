"""Tests for the scenario reconciler (Layer R): resolve -> diff -> translate -> order.

Pure planning, no execution: devices are fakes carrying a real CapabilityMap + an assumed
state. Verifies the movie_appletv plan, the manual-node path, ordering/delay, and diffing.
"""

import json
from pathlib import Path
from types import SimpleNamespace

from locveil_bridge.domain.scenarios.models import ScenarioDefinition
from locveil_bridge.infrastructure.capabilities.loader import load_capability_map
from locveil_bridge.domain.capabilities.models import CapabilityMap
import pytest

from locveil_bridge.domain.scenarios.reconciler import (
    PlannedAction,
    ReconcilePlan,
    build_plan,
    build_power_off_plan,
    execute_plan,
    resolve_targets,
)
from locveil_bridge.domain.topology.loader import load_topology
from locveil_bridge.domain.topology.models import Topology

ROOT = Path(__file__).resolve().parents[3]
CAPS = ROOT / "config" / "capabilities"
TOPOLOGY = load_topology(ROOT / "config" / "topology.json")


def _device(device_class: str, device_id: str, **state_fields):
    caps = load_capability_map(device_class, device_id, CAPS)
    state_fields.setdefault("power", "off")
    state = SimpleNamespace(**state_fields)
    return SimpleNamespace(capabilities=caps, get_current_state=lambda s=state: s)


def _movie_appletv_devices(**overrides):
    base = {
        "appletv_living": _device("AppleTVDevice", "appletv_living"),
        "processor": _device("EMotivaXMC2", "processor", zone2_power=None, input_source=None),
        "living_room_tv": _device("LgTv", "living_room_tv", input_source=None),
        "mf_amplifier": _device("WirenboardIRDevice", "mf_amplifier", input=None),
    }
    base.update(overrides)
    return base


def _scenario(name: str) -> ScenarioDefinition:
    data = json.loads((ROOT / "config" / "scenarios" / f"{name}.json").read_text())
    return ScenarioDefinition.model_validate(data)


def _idx(plan, device_id, domain, zone=None):
    for i, a in enumerate(plan.actions):
        if a.device_id == device_id and a.domain == domain and (zone is None or a.zone == zone):
            return i
    return -1


def _find(plan, device_id, domain, zone=None):
    i = _idx(plan, device_id, domain, zone)
    return plan.actions[i] if i >= 0 else None


# --- the headline: movie_appletv from a cold (all-off) start -----------------


def test_movie_appletv_full_plan_from_cold():
    scenario = _scenario("movie_appletv")
    plan = build_plan(scenario, TOPOLOGY, _movie_appletv_devices())

    assert plan.warnings == []
    assert plan.manual_steps == []  # Apple TV path doesn't touch the manual Dodocus hub

    # every involved device powers on; the eMotiva powers both zones
    assert _find(plan, "appletv_living", "power") is not None
    assert _find(plan, "living_room_tv", "power") is not None
    assert _find(plan, "processor", "power", zone="1") is not None
    assert _find(plan, "processor", "power", zone="2") is not None
    assert _find(plan, "mf_amplifier", "power") is not None

    # inputs derived from topology destination ports
    assert _find(plan, "living_room_tv", "input").target == "hdmi2"
    assert _find(plan, "processor", "input").target == "source2"
    assert _find(plan, "mf_amplifier", "input").target == "aux2"
    # Apple TV is a pure source: no input action
    assert _find(plan, "appletv_living", "input") is None


def test_movie_appletv_translation_fixes_rc1_and_value_maps():
    plan = build_plan(_scenario("movie_appletv"), TOPOLOGY, _movie_appletv_devices())

    # RC1: canonical input -> native `source`
    lg_input = _find(plan, "living_room_tv", "input")
    assert lg_input.command == "set_input_source" and lg_input.params == {"source": "hdmi2"}
    # eMotiva identity param
    emo_input = _find(plan, "processor", "input")
    assert emo_input.command == "set_input" and emo_input.params == {"input": "source2"}
    # IR amp: toggle power + value-mapped input
    assert _find(plan, "mf_amplifier", "power").command == "power"
    assert _find(plan, "mf_amplifier", "input").command == "input_aux2"
    # eMotiva zone power carries the zone param
    assert _find(plan, "processor", "power", zone="2").params == {"zone": 2}


def test_movie_appletv_ordering_matches_manual_sequence():
    plan = build_plan(_scenario("movie_appletv"), TOPOLOGY, _movie_appletv_devices())

    # TV.power -> eMotiva.power -> TV.input -> eMotiva.input
    assert _idx(plan, "living_room_tv", "power") < _idx(plan, "processor", "power", zone="1")
    assert _idx(plan, "living_room_tv", "power") < _idx(plan, "processor", "power", zone="2")
    assert _idx(plan, "processor", "power", zone="1") < _idx(plan, "living_room_tv", "input")
    assert _idx(plan, "processor", "power", zone="2") < _idx(plan, "living_room_tv", "input")
    assert _idx(plan, "living_room_tv", "input") < _idx(plan, "processor", "input")
    # per-device: amp power before amp input
    assert _idx(plan, "mf_amplifier", "power") < _idx(plan, "mf_amplifier", "input")


def test_diff_skips_already_satisfied():
    devices = _movie_appletv_devices(
        appletv_living=_device("AppleTVDevice", "appletv_living", power="on"),
        processor=_device("EMotivaXMC2", "processor", power="on", zone2_power="on", input_source="source2"),
        living_room_tv=_device("LgTv", "living_room_tv", power="on", input_source="hdmi2"),
        mf_amplifier=_device("WirenboardIRDevice", "mf_amplifier", power="on", input="aux2"),
    )
    plan = build_plan(_scenario("movie_appletv"), TOPOLOGY, devices)
    assert plan.actions == []
    assert "living_room_tv.power" in plan.already_satisfied
    assert "processor.input" in plan.already_satisfied


# --- manual node (LD path through the Dodocus hub) ---------------------------


def test_resolve_ld_path_emits_manual_step_and_cd_input():
    ld = ScenarioDefinition(
        scenario_id="movie_ld", names={"ru": "LD", "en": "LD"},
        source="ld_player", display="living_room_tv", audio="mf_amplifier",
    )
    input_targets, _source_targets, involved, manual_steps, warnings, _used = resolve_targets(ld, TOPOLOGY)

    assert input_targets["upscaler"] == "video"
    assert input_targets["processor"] == "source3"
    assert input_targets["mf_amplifier"] == "cd"  # via the manual hub
    assert {"ld_player", "upscaler", "processor", "living_room_tv", "mf_amplifier"} <= involved
    assert "dodocus" not in involved  # manual node is not a device
    assert any(m.node == "dodocus" and "to LD" in m.instruction for m in manual_steps)


def test_manual_source_node_anchors_path_without_being_controlled():
    """A manual-node *source* (a turntable/tape with no driver) anchors the topology path
    so the sink input + the hub's manual note still resolve — but the manual node itself is
    never added to `involved` (nothing to power/control)."""
    topo = Topology.model_validate(
        {
            "nodes": {
                "hub": {"kind": "manual", "name": "RCA hub", "positions": {"tt": "Set the hub to the Phono position"}},
                "turntable": {"kind": "manual", "name": "Kuzma turntable"},
            },
            "links": [
                {"from": "turntable:out", "to": "hub:tt", "carries": ["audio"]},
                {"from": "hub:out", "to": "amp:cd", "carries": ["audio"]},
            ],
        }
    )
    scn = ScenarioDefinition(scenario_id="m", names={"ru": "m", "en": "m"}, source="turntable", audio="amp")
    input_targets, _source_targets, involved, manual_steps, warnings, _used = resolve_targets(scn, topo)

    assert input_targets == {"amp": "cd"}        # sink input resolved through the manual source
    assert involved == {"amp"}                   # the manual source is NOT a device to control
    assert "turntable" not in involved
    assert any(m.node == "hub" and "Phono" in m.instruction for m in manual_steps)
    assert warnings == []


# --- ordering delay propagation (synthetic) ---------------------------------


def test_ordering_edge_delay_becomes_pre_delay():
    topo = Topology.model_validate({
        "links": [{"from": "src:out", "to": "sink:in1", "carries": ["video"]}],
        "ordering": [{"first": "src.power", "then": "sink.input", "delay_ms": 4500}],
    })
    cap_src = CapabilityMap.model_validate({
        "power": {"kind": "stateful", "feedback": False, "state_field": "power",
                  "actions": {"toggle": {"command": "power"}}, "gate": {"delay_ms": 100}}
    })
    cap_sink = CapabilityMap.model_validate({
        "power": {"kind": "stateful", "feedback": False, "state_field": "power",
                  "actions": {"on": {"command": "power_on"}}},
        "input": {"kind": "stateful", "feedback": False, "state_field": "input",
                  "select": {"by_value": {"in1": {"command": "input_1"}}}},
    })
    devices = {
        "src": SimpleNamespace(capabilities=cap_src,
                               get_current_state=lambda: SimpleNamespace(power="off")),
        "sink": SimpleNamespace(capabilities=cap_sink,
                                get_current_state=lambda: SimpleNamespace(power="off", input=None)),
    }
    scenario = ScenarioDefinition(scenario_id="s", names={"ru": "s", "en": "s"}, source="src", display="sink")
    plan = build_plan(scenario, topo, devices)

    sink_input = _find(plan, "sink", "input")
    assert sink_input.pre_delay_ms == 4500
    assert _idx(plan, "src", "power") < _idx(plan, "sink", "input")


# --- execution ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_plan_runs_actions_in_plan_order(monkeypatch):
    import locveil_bridge.domain.scenarios.reconciler as rec

    async def _nosleep(*a, **k):
        return None

    monkeypatch.setattr(rec.asyncio, "sleep", _nosleep)

    # This test pins dispatch ORDER; the fakes' static states never reach the
    # targets, and since SCN-14 an unconfirmed feedback gate is a failure — so
    # neutralize gating here (dedicated gate tests live below).
    async def _gate_ok(*a, **k):
        return True

    monkeypatch.setattr(rec, "_gate", _gate_ok)

    calls: list = []

    def fake(device_id, caps, **state):
        st = SimpleNamespace(power="off", **state)

        async def execute_action(command, params, source="unknown", _id=device_id):
            calls.append((_id, command))
            return {"success": True}

        return SimpleNamespace(capabilities=caps, get_current_state=lambda _st=st: _st,
                               execute_action=execute_action)

    devices = {
        "appletv_living": fake("appletv_living", load_capability_map("AppleTVDevice", "appletv_living", CAPS)),
        "processor": fake("processor", load_capability_map("EMotivaXMC2", "processor", CAPS),
                          zone2_power=None, input_source=None),
        "living_room_tv": fake("living_room_tv", load_capability_map("LgTv", "living_room_tv", CAPS),
                               input_source=None),
        "mf_amplifier": fake("mf_amplifier", load_capability_map("WirenboardIRDevice", "mf_amplifier", CAPS),
                             input=None),
    }
    plan = build_plan(_scenario("movie_appletv"), TOPOLOGY, devices)
    result = await execute_plan(plan, devices)

    assert result.success
    assert len(result.executed) == len(plan.actions)
    # actions are dispatched in exactly the planned order
    assert calls == [(a.device_id, a.command) for a in plan.actions]


@pytest.mark.asyncio
async def test_execute_plan_surfaces_failure_and_continues(monkeypatch):
    import locveil_bridge.domain.scenarios.reconciler as rec

    async def _nosleep(*a, **k):
        return None

    monkeypatch.setattr(rec.asyncio, "sleep", _nosleep)

    async def good(command, params, source="unknown"):
        return {"success": True}

    async def bad(command, params, source="unknown"):
        return {"success": False, "error": "boom"}

    devices = {
        "d1": SimpleNamespace(get_current_state=lambda: SimpleNamespace(), execute_action=bad),
        "d2": SimpleNamespace(get_current_state=lambda: SimpleNamespace(), execute_action=good),
    }
    plan = ReconcilePlan(actions=[
        PlannedAction("d1", "power", "on", "power_on"),
        PlannedAction("d2", "power", "on", "power_on"),
    ])
    result = await execute_plan(plan, devices)

    assert not result.success
    assert len(result.failures) == 1 and result.failures[0][1] == "boom"
    assert len(result.executed) == 1  # d2 still ran (failures don't abort by default)


# --- SCN-14: gate comparison canonicalization + timeout-as-failure ------------
# REL-3 rack findings F2+F3 (docs/review/rel3_rack_findings_2026-07-10.md): the
# gate compared the canonical target to the raw wire state ('hdmi2' vs the LG's
# 'HDMI_2') and could NEVER confirm, and a timeout was advisory — tv_on_speakers
# reported success twice while ARC never engaged.


def _feedback_action(target: str, state_field: str = "input_source", **kw) -> PlannedAction:
    return PlannedAction(
        "d", "input", target, "set_input",
        feedback=True, state_field=state_field, poll_timeout_ms=1000, **kw,
    )


def _static_device(**state):
    st = SimpleNamespace(**state)

    async def execute_action(command, params, source="unknown"):
        return {"success": True}

    return SimpleNamespace(get_current_state=lambda: st, execute_action=execute_action)


@pytest.mark.asyncio
async def test_gate_confirms_wire_state_via_normalization(monkeypatch):
    """The LG stores 'HDMI_2'/'HDMI2'; the plan targets canonical 'hdmi2' — the gate
    must confirm instead of burning its full poll window (F2)."""
    import locveil_bridge.domain.scenarios.reconciler as rec

    async def _nosleep(*a, **k):
        return None

    monkeypatch.setattr(rec.asyncio, "sleep", _nosleep)

    for wire in ("HDMI_2", "HDMI2", "hdmi2"):
        devices = {"d": _static_device(input_source=wire)}
        plan = ReconcilePlan(actions=[_feedback_action("hdmi2")])
        result = await execute_plan(plan, devices)
        assert result.success, f"gate must confirm wire form {wire!r}"
        assert not result.failures


@pytest.mark.asyncio
async def test_gate_confirms_via_value_table(monkeypatch):
    """A capability enum table (wire->canonical) translates the observed state before
    comparison — numeric-wire devices gate correctly."""
    import locveil_bridge.domain.scenarios.reconciler as rec

    async def _nosleep(*a, **k):
        return None

    monkeypatch.setattr(rec.asyncio, "sleep", _nosleep)

    devices = {"d": _static_device(mode="1")}
    plan = ReconcilePlan(actions=[
        _feedback_action("heat", state_field="mode", value_table={"1": "heat", "3": "cool"}),
    ])
    result = await execute_plan(plan, devices)
    assert result.success and not result.failures


@pytest.mark.asyncio
async def test_gate_timeout_is_a_failed_step(monkeypatch):
    """F3: a feedback:true step whose reported state never reaches the target is a
    FAILURE in the result (it stays in `executed` — it was dispatched and acked)."""
    import locveil_bridge.domain.scenarios.reconciler as rec

    async def _nosleep(*a, **k):
        return None

    monkeypatch.setattr(rec.asyncio, "sleep", _nosleep)

    devices = {"d": _static_device(input_source="source2")}  # never reaches 'arc'
    plan = ReconcilePlan(actions=[_feedback_action("arc")])
    result = await execute_plan(plan, devices)

    assert not result.success
    assert len(result.executed) == 1          # dispatched fine
    assert len(result.failures) == 1          # …but never confirmed
    action, err = result.failures[0]
    assert action.target == "arc" and "gate timeout" in err


def test_preview_in_sync_across_wire_canonical_vocabularies():
    """REL-3 sitting #2 follow-up (DRV-33's tail): the SCN-11 dialog showed the TV
    'out of sync' with believed 'HDMI_2' vs desired 'hdmi2' — the preview compared
    raw equality while the execution gate normalized. All comparison sites share
    `_satisfies` now; a TV honestly on HDMI_2 must read in sync."""
    from locveil_bridge.domain.scenarios.reconciler import build_reconcile_preview

    devices = {
        "living_room_tv": _device("LgTv", "living_room_tv", power="on", input_source="HDMI_2"),
        "processor": _device("EMotivaXMC2", "processor", power="on", zone2_power="on",
                             input_source="source1"),
        "mf_amplifier": _device("WirenboardIRDevice", "mf_amplifier", power="on", input="aux2"),
        "video": _device("WirenboardIRDevice", "video", power="on"),
    }
    rows = {p.device_id: p for p in
            build_reconcile_preview(_scenario("movie_zappiti"), TOPOLOGY, devices)}
    tv = rows["living_room_tv"]
    assert tv.in_sync, [f"{c.domain}: {c.believed!r} vs {c.desired!r}" for c in tv.comparisons]


def test_plan_diff_skips_wire_form_already_satisfied():
    """Same vocabulary rule in the build_plan diff: a TV already on 'HDMI_2' gets NO
    input action for target 'hdmi2' (before this, every switch re-dispatched the TV
    input as a phantom diff)."""
    devices = {
        "living_room_tv": _device("LgTv", "living_room_tv", power="on", input_source="HDMI_2"),
        "processor": _device("EMotivaXMC2", "processor", power="on", zone2_power="on",
                             input_source="source1"),
        "mf_amplifier": _device("WirenboardIRDevice", "mf_amplifier", power="on", input="aux2"),
        "video": _device("WirenboardIRDevice", "video", power="on"),
    }
    plan = build_plan(_scenario("movie_zappiti"), TOPOLOGY, devices)
    assert not [a for a in plan.actions if a.device_id == "living_room_tv"], \
        [f"{a.device_id}.{a.domain}" for a in plan.actions]
    assert "living_room_tv.input" in plan.already_satisfied


@pytest.mark.asyncio
async def test_gate_timeout_only_gates_feedback_steps(monkeypatch):
    """Feedback-less steps keep the optimistic path — an IR toggle has nothing to
    report, so no gate failure can exist for it."""
    import locveil_bridge.domain.scenarios.reconciler as rec

    async def _nosleep(*a, **k):
        return None

    monkeypatch.setattr(rec.asyncio, "sleep", _nosleep)

    devices = {"d": _static_device(power="off")}  # state never changes
    plan = ReconcilePlan(actions=[
        PlannedAction("d", "power", "on", "power", feedback=False, delay_ms=500),
    ])
    result = await execute_plan(plan, devices)
    assert result.success and not result.failures


def test_build_power_off_plan_powers_off_on_devices():
    devices = {
        "living_room_tv": _device("LgTv", "living_room_tv", power="on", input_source="hdmi2"),
        "mf_amplifier": _device("WirenboardIRDevice", "mf_amplifier", power="on", input="aux2"),
        "processor": _device("EMotivaXMC2", "processor", power="on", zone2_power="on", input_source="source2"),
    }
    plan = build_power_off_plan(["living_room_tv", "mf_amplifier", "processor"], devices)
    cmds = {(a.device_id, a.command) for a in plan.actions}
    assert ("living_room_tv", "power_off") in cmds
    assert ("mf_amplifier", "power") in cmds  # toggle off
    assert sum(1 for a in plan.actions if a.device_id == "processor") == 2  # both zones


def test_build_power_off_plan_skips_already_off():
    devices = {"living_room_tv": _device("LgTv", "living_room_tv", power="off")}
    plan = build_power_off_plan(["living_room_tv"], devices)
    assert plan.actions == []


def test_build_power_off_plan_skips_reconcile_false_power():
    """SCN-12: teardown must honour reconcile:false, mirroring build_plan's power-on
    guard. The upscaler's power capability is reconcile:false (it auto-powers with the
    LD) — even when it is ON, a shutdown/switch must not emit a power_off to it."""
    devices = {"upscaler": _device("WirenboardIRDevice", "upscaler", power="on")}
    plan = build_power_off_plan(["upscaler"], devices)
    assert plan.actions == []


# --- all four scenarios, end to end -----------------------------------------


def _all_devices():
    return {
        "appletv_living": _device("AppleTVDevice", "appletv_living"),
        "processor": _device("EMotivaXMC2", "processor", zone2_power=None, input_source=None),
        "living_room_tv": _device("LgTv", "living_room_tv", input_source=None),
        "mf_amplifier": _device("WirenboardIRDevice", "mf_amplifier", input=None),
        "ld_player": _device("WirenboardIRDevice", "ld_player"),
        "vhs_player": _device("WirenboardIRDevice", "vhs_player"),
        "video": _device("WirenboardIRDevice", "video"),
        "upscaler": _device("WirenboardIRDevice", "upscaler", input=None),
    }


@pytest.mark.parametrize("name", ["movie_appletv", "movie_ld", "movie_vhs", "movie_zappiti"])
def test_all_scenarios_build_clean_plans(name):
    plan = build_plan(_scenario(name), TOPOLOGY, _all_devices())
    assert plan.warnings == [], f"{name}: {plan.warnings}"
    assert plan.actions, f"{name} produced no actions"


def test_movie_ld_plan_uses_manual_hub_and_upscaler_delay():
    plan = build_plan(_scenario("movie_ld"), TOPOLOGY, _all_devices())

    # manual Dodocus step (audio routed via the hub to amp `cd`)
    assert any(m.node == "dodocus" and "to LD" in m.instruction for m in plan.manual_steps)
    assert _find(plan, "mf_amplifier", "input").command == "input_cd"
    # upscaler input via topology (video) with the 4.5s settle; no power action (auto-powers)
    ups_in = _find(plan, "upscaler", "input")
    assert ups_in.target == "video" and ups_in.command == "input_video" and ups_in.pre_delay_ms == 4500
    assert _find(plan, "upscaler", "power") is None
    # processor routed to source3; LD powered via toggle
    assert _find(plan, "processor", "input").target == "source3"
    assert _find(plan, "ld_player", "power").command == "power"


def test_movie_zappiti_has_no_manual_steps():
    plan = build_plan(_scenario("movie_zappiti"), TOPOLOGY, _all_devices())
    assert plan.manual_steps == []  # audio runs through the eMotiva, not the manual hub
    assert _find(plan, "processor", "input").target == "source1"
    assert _find(plan, "mf_amplifier", "input").command == "input_aux2"


# --- round-2 music scenarios (P3.6) -----------------------------------------


def _music_devices(**overrides):
    base = {
        "streamer": _device("AuralicDevice", "streamer", input=None),
        "reel_to_reel": _device("RevoxA77ReelToReel", "reel_to_reel"),
        "mf_amplifier": _device("WirenboardIRDevice", "mf_amplifier", input=None),
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    "name,amp_input,manual_pos,passive_source",
    [
        ("music_auralic", "balanced", None, None),
        ("music_reel", "cd", "to REEL", None),
        ("music_tape", "cd", "to TAPE", "b215"),
        ("music_turntable", "cd", "to LP", "kuzma"),
    ],
)
def test_music_scenarios_resolve_and_build_clean(name, amp_input, manual_pos, passive_source):
    scn = _scenario(name)
    input_targets, _source_targets, involved, manual_steps, warnings, _used = resolve_targets(scn, TOPOLOGY)

    assert input_targets["mf_amplifier"] == amp_input  # amp input auto-selected from topology
    assert "mf_amplifier" in involved
    if manual_pos:  # analog sources route via the Dodocus hub → a manual position note
        assert any(m.node == "dodocus" and manual_pos in m.instruction for m in manual_steps)
    if passive_source:  # a manual-node source is never controlled
        assert passive_source not in involved

    plan = build_plan(scn, TOPOLOGY, _music_devices())
    assert plan.warnings == [], f"{name}: {plan.warnings}"
    assert _find(plan, "mf_amplifier", "input").command == f"input_{amp_input}"


def test_music_turntable_surfaces_both_manual_hops():
    """The turntable path runs Kuzma → Sugden PA4 (power on) → Dodocus (Phono) → amp:cd,
    so BOTH inline manual nodes surface a note; neither passive node is controlled."""
    _, _source_targets, involved, manual_steps, warnings, _used = resolve_targets(_scenario("music_turntable"), TOPOLOGY)
    notes = {m.node: m.instruction for m in manual_steps}
    assert "Power on" in notes.get("sugden_pa4", "")
    assert "to LP" in notes.get("dodocus", "")
    assert "kuzma" not in involved and "sugden_pa4" not in involved
    assert warnings == []


# --- Symmetric src_port mechanism (HDMI ARC) ---------------------------------


def _tv_on_speakers_devices(**overrides):
    base = {
        "living_room_tv": _device("LgTv", "living_room_tv", input_source=None),
        "processor": _device("EMotivaXMC2", "processor", zone2_power=None, input_source=None),
        "mf_amplifier": _device("WirenboardIRDevice", "mf_amplifier", input=None),
    }
    base.update(overrides)
    return base


def test_tv_on_speakers_resolves_with_source_targets():
    """tv_on_speakers has source=living_room_tv: the TV is at the START of the audio path.
    The symmetric src_port mechanism puts the TV in source_targets (not input_targets) with
    src_port='arc' from the topology link `living_room_tv:arc → processor:arc`."""
    scn = _scenario("tv_on_speakers")
    input_targets, source_targets, involved, _manual_steps, warnings, _used = resolve_targets(scn, TOPOLOGY)

    # Destinations on the audio path get input_targets per dst_port (existing behavior)
    assert input_targets.get("processor") == "arc"
    assert input_targets.get("mf_amplifier") == "aux2"
    # TV is source — gets source_targets from src_port (new behavior)
    assert source_targets.get("living_room_tv") == "arc"
    # TV is NOT in input_targets (it's not a destination on this path)
    assert "living_room_tv" not in input_targets
    # All three on the audio path are involved
    assert {"living_room_tv", "processor", "mf_amplifier"}.issubset(involved)
    assert warnings == []


def test_tv_on_speakers_plan_emits_tv_internal_via_source_modes():
    """build_plan emits set_input_source(arc) on the TV (driven by source_targets +
    LgTv input capability declaring 'arc' in source_modes). Driver translates to
    handle_home internally — the reconciler stays generic."""
    scn = _scenario("tv_on_speakers")
    plan = build_plan(scn, TOPOLOGY, _tv_on_speakers_devices())

    # TV gets an input action with target='arc'
    tv_input = next(
        (a for a in plan.actions if a.device_id == "living_room_tv" and a.domain == "input"),
        None,
    )
    assert tv_input is not None, "expected an input action on living_room_tv"
    assert tv_input.target == "arc"
    assert tv_input.command == "set_input_source"
    # Processor also gets set_input(arc) from its dst_port
    processor_input = next(
        (a for a in plan.actions if a.device_id == "processor" and a.domain == "input"),
        None,
    )
    assert processor_input is not None and processor_input.target == "arc"

    assert plan.warnings == []


def test_tv_on_speakers_already_satisfied_when_tv_on_internal_and_processor_on_arc():
    """If the TV's input_source is already 'arc' (driver maps 'home'/internal there) AND
    the processor's input_source is already 'arc', the reconciler emits no input actions
    (both already_satisfied). Guards against the power-cycle firing on every activation
    when ARC is already engaged."""
    scn = _scenario("tv_on_speakers")
    devices = _tv_on_speakers_devices(
        living_room_tv=_device("LgTv", "living_room_tv", power="on", input_source="arc"),
        processor=_device(
            "EMotivaXMC2", "processor", power="on", zone2_power="off", input_source="arc",
        ),
        mf_amplifier=_device(
            "WirenboardIRDevice", "mf_amplifier", power="on", input="aux2",
        ),
    )
    plan = build_plan(scn, TOPOLOGY, devices)

    tv_input = [a for a in plan.actions if a.device_id == "living_room_tv" and a.domain == "input"]
    processor_input = [a for a in plan.actions if a.device_id == "processor" and a.domain == "input"]
    assert tv_input == [], "TV input should be already_satisfied"
    assert processor_input == [], "Processor input should be already_satisfied"
    assert "living_room_tv.input" in plan.already_satisfied
    assert "processor.input" in plan.already_satisfied


def test_source_targets_skipped_when_device_has_no_source_modes():
    """A source device with an `input` capability but NO `source_modes` declaration must
    NOT trigger a set_input action from the src_port mechanism. Example: Auralic streamer
    is the source of `streamer:out → mf_amplifier:balanced`, has an input capability, but
    declares no source_modes — reconciler must silently skip the src-side action (Auralic
    would fail set_input('out') since 'out' isn't a valid Auralic input)."""
    # music_auralic: source=streamer, audio=mf_amplifier. Streamer→amp link has src_port='out'.
    scn = _scenario("music_auralic")
    _, source_targets, _, _, _, _ = resolve_targets(scn, TOPOLOGY)
    # source_targets DOES record the src_port (resolve_targets is unconditional)
    assert source_targets.get("streamer") == "out"
    # ...but build_plan must NOT emit an action for it (no source_modes in Auralic cap)
    devices = {
        "streamer": _device("AuralicDevice", "streamer", source=None),
        "mf_amplifier": _device("WirenboardIRDevice", "mf_amplifier", input=None),
    }
    plan = build_plan(scn, TOPOLOGY, devices)
    streamer_input = [a for a in plan.actions if a.device_id == "streamer" and a.domain == "input"]
    assert streamer_input == [], "streamer should not get a set_input action — no source_modes"
    # And no warning is emitted for the silently-skipped src target
    assert not any("streamer" in w and "input" in w for w in plan.warnings)


def test_source_targets_skipped_when_device_has_no_input_capability():
    """A source device with no input capability at all (Apple TV) must be silently skipped
    by the src_port mechanism — no warnings (warnings are reserved for dst_port targets
    that fail, where the topology made a hard claim about a destination's input)."""
    scn = _scenario("movie_appletv")
    _, source_targets, _, _, _, _ = resolve_targets(scn, TOPOLOGY)
    assert source_targets.get("appletv_living") == "hdmi"  # src_port from appletv→processor link

    plan = build_plan(scn, TOPOLOGY, _movie_appletv_devices())
    appletv_input = [a for a in plan.actions if a.device_id == "appletv_living" and a.domain == "input"]
    assert appletv_input == []
    # No warning for appletv input (Apple TV has no input capability — expected for sources)
    assert not any("appletv_living" in w and "input capability" in w for w in plan.warnings)


# --- SCN-3 rack finding: bool power gates burned the full poll timeout ---------


def test_power_off_targets_bool_complement_for_auralic():
    """The Auralic power gate keys on `connected: true` (bool). The off-plan used
    to target the STRING "off" — never equal to a bool — so every teardown
    burned the full 25 s poll_timeout (rack: the music_auralic -> music_reel
    switch hung the UI spinner for ~29 s)."""
    devices = {"streamer": _device("AuralicDevice", "streamer", power="on", connected=True)}
    plan = build_power_off_plan(["streamer"], devices)
    acts = [a for a in plan.actions if a.device_id == "streamer"]
    assert len(acts) == 1
    assert acts[0].state_field == "connected"
    assert acts[0].target is False  # bool complement, not "off"


async def test_gate_satisfied_immediately_when_bool_off_reached():
    from locveil_bridge.domain.scenarios.reconciler import _gate

    devices = {"streamer": _device("AuralicDevice", "streamer", power="on", connected=True)}
    plan = build_power_off_plan(["streamer"], devices)
    action = plan.actions[0]
    # After the command executes, the driver flips connected to False.
    done = _device("AuralicDevice", "streamer", power="off", connected=False)

    assert await _gate(done, action, poll_interval_ms=10) is True


def test_power_off_keeps_string_off_for_string_fields():
    """eMotiva/LG-style string power fields keep the 'off' convention."""
    devices = {"living_room_tv": _device("LgTv", "living_room_tv", power="on")}
    plan = build_power_off_plan(["living_room_tv"], devices)
    tv = [a for a in plan.actions if a.device_id == "living_room_tv"][0]
    assert tv.target == "off"


# --- SCN-11: forced single-device plan + reconcile preview -------------------


from locveil_bridge.domain.scenarios.reconciler import (  # noqa: E402
    build_forced_device_plan,
    build_reconcile_preview,
)


def _movie_zappiti_devices():
    return {
        "video": _device("WirenboardIRDevice", "video", input=None),
        "processor": _device("EMotivaXMC2", "processor", zone2_power=None, input_source=None),
        "living_room_tv": _device("LgTv", "living_room_tv", input_source=None),
        "mf_amplifier": _device("WirenboardIRDevice", "mf_amplifier", input=None),
    }


def test_forced_plan_emits_despite_satisfied_state():
    """The core SCN-11 inversion: a device the reconciler calls already_satisfied gets a
    full forced chain anyway — 'satisfied' only means the BELIEF matches; the user
    standing in the room is the feedback channel saying it doesn't."""
    devices = _movie_appletv_devices(
        mf_amplifier=_device("WirenboardIRDevice", "mf_amplifier", power="on", input="aux2"),
    )
    scenario = _scenario("movie_appletv")

    normal = build_plan(scenario, TOPOLOGY, devices)
    assert _find(normal, "mf_amplifier", "power") is None
    assert "mf_amplifier.power" in normal.already_satisfied

    forced = build_forced_device_plan(scenario, TOPOLOGY, devices, "mf_amplifier")
    power = _find(forced, "mf_amplifier", "power")
    inp = _find(forced, "mf_amplifier", "input")
    assert power is not None and inp is not None
    # every forced action bypasses the driver idempotence guards (DRV-5 foundation)
    assert power.params["force"] is True and inp.params["force"] is True
    # toggle power claims the plan TARGET, not a blind flip of the (wrong) belief
    assert power.command == "power" and power.params["assume_state"] == "on"
    # single-device plan: nothing else rides along
    assert {a.device_id for a in forced.actions} == {"mf_amplifier"}
    # within-device ordering preserved (power before input)
    assert _idx(forced, "mf_amplifier", "power") < _idx(forced, "mf_amplifier", "input")


def test_forced_plan_non_toggle_power_carries_no_assume_state():
    devices = _movie_appletv_devices()
    forced = build_forced_device_plan(_scenario("movie_appletv"), TOPOLOGY, devices, "living_room_tv")
    power = _find(forced, "living_room_tv", "power")
    assert power is not None
    assert power.params.get("force") is True
    assert "assume_state" not in power.params


def test_forced_plan_drops_cross_device_ordering_edges():
    """Full movie_zappiti plan: the processor.input -> video.power edge imposes a 5 s
    settle. The single-device forced plan for `video` presumes the processor already
    settled long ago — the edge must drop out (its other endpoint isn't in the plan)."""
    devices = _movie_zappiti_devices()
    scenario = _scenario("movie_zappiti")

    full = build_plan(scenario, TOPOLOGY, devices)
    full_video_power = _find(full, "video", "power")
    assert full_video_power is not None and full_video_power.pre_delay_ms == 5000

    forced = build_forced_device_plan(scenario, TOPOLOGY, devices, "video")
    forced_video_power = _find(forced, "video", "power")
    assert forced_video_power is not None and forced_video_power.pre_delay_ms == 0
    assert {a.device_id for a in forced.actions} == {"video"}


def test_forced_plan_uninvolved_device_is_empty_with_warning():
    devices = _movie_appletv_devices()
    forced = build_forced_device_plan(_scenario("movie_appletv"), TOPOLOGY, devices, "kitchen_hood")
    assert forced.actions == []
    assert any("not involved" in w for w in forced.warnings)


def test_reconcile_preview_rows():
    devices = _movie_appletv_devices(
        mf_amplifier=_device("WirenboardIRDevice", "mf_amplifier", power="on", input="aux2"),
    )
    previews = build_reconcile_preview(_scenario("movie_appletv"), TOPOLOGY, devices)
    by_id = {p.device_id: p for p in previews}

    # the satisfied amp reads in_sync — yet still carries a forced plan (the inversion)
    amp = by_id["mf_amplifier"]
    assert amp.in_sync is True
    assert all(c.in_sync for c in amp.comparisons)
    assert amp.plan.actions

    # a cold TV is out of sync on both domains
    tv = by_id["living_room_tv"]
    assert tv.in_sync is False
    doms = {c.domain: c for c in tv.comparisons}
    assert doms["power"].believed == "off" and doms["power"].desired == "on"
    assert doms["input"].desired == "hdmi2"

    # eMotiva zoned power: believed/desired are per-zone dicts
    proc = by_id["processor"]
    pdom = {c.domain: c for c in proc.comparisons}["power"]
    assert isinstance(pdom.desired, dict) and isinstance(pdom.believed, dict)
    assert set(pdom.desired.keys()) == set(pdom.believed.keys())


# --- SCN-16: zone-aware power planning (DRV-38(b) review remediation) ---------
# docs/review/topology_readiness_review_2026-07-13.md lane R3: movie_ld/movie_vhs
# powered the eMotiva's zone 2 although their audio rides the Dodocus RCA path —
# the 2026-07-12 wedge trigger fired with zero functional benefit. A zone with a
# declared `port` is planned only when the resolved path uses that port.


def _movie_ld_devices():
    return {
        "ld_player": _device("WirenboardIRDevice", "ld_player"),
        "upscaler": _device("WirenboardIRDevice", "upscaler", input=None),
        "processor": _device("EMotivaXMC2", "processor", zone2_power=None, input_source=None),
        "living_room_tv": _device("LgTv", "living_room_tv", input_source=None),
        "mf_amplifier": _device("WirenboardIRDevice", "mf_amplifier", input=None),
    }


def test_resolve_targets_collects_used_ports_both_endpoints():
    """used_ports keeps BOTH endpoints of every path link — including the src port
    of a mid-chain device that source_targets drops when dst wins (the eMotiva is
    dst 'source2' AND src 'zone2' in the same appletv start)."""
    *_, used = resolve_targets(_scenario("movie_appletv"), TOPOLOGY)
    assert "source2" in used["processor"]
    assert "zone2" in used["processor"]

    *_, used_ld = resolve_targets(_scenario("movie_ld"), TOPOLOGY)
    assert "source3" in used_ld["processor"]
    assert "zone2" not in used_ld["processor"]  # audio rides Dodocus RCA, not zone2


def test_movie_ld_plan_skips_off_path_zone2():
    plan = build_plan(_scenario("movie_ld"), TOPOLOGY, _movie_ld_devices())
    assert _find(plan, "processor", "power", zone="1") is not None
    assert _find(plan, "processor", "power", zone="2") is None  # spurious step gone


def test_movie_appletv_plan_keeps_on_path_zone2():
    plan = build_plan(_scenario("movie_appletv"), TOPOLOGY, _movie_appletv_devices())
    assert _find(plan, "processor", "power", zone="1") is not None
    assert _find(plan, "processor", "power", zone="2") is not None  # audio path uses it


def test_preview_desired_set_agrees_with_planner_on_zones():
    """SCN-11 parity: the force-reconcile dialog must not show an off-path zone as
    desired — else movie_ld would read 'out of sync' forever after SCN-16."""
    from locveil_bridge.domain.scenarios.reconciler import build_reconcile_preview

    previews = {p.device_id: p for p in
                build_reconcile_preview(_scenario("movie_ld"), TOPOLOGY, _movie_ld_devices())}
    pdom = {c.domain: c for c in previews["processor"].comparisons}["power"]
    assert set(pdom.desired.keys()) == {"1"}

    previews = {p.device_id: p for p in
                build_reconcile_preview(_scenario("movie_appletv"), TOPOLOGY,
                                        _movie_appletv_devices())}
    pdom = {c.domain: c for c in previews["processor"].comparisons}["power"]
    assert set(pdom.desired.keys()) == {"1", "2"}


# --- SCN-17: bounded per-step dispatch (DRV-38(b) review remediation) ---------


@pytest.mark.asyncio
async def test_dispatch_timeout_fails_the_step_and_the_plan_continues(monkeypatch):
    """A hung driver dispatch costs ONE failed step, not the whole switch — steps
    are globally serialized, so an unbounded hang would freeze every later device."""
    import asyncio
    from locveil_bridge.domain.scenarios import reconciler as r

    monkeypatch.setattr(r, "DISPATCH_TIMEOUT_S", 0.05)

    async def hang(command, params, source):
        await asyncio.sleep(30)

    async def respond(command, params, source):
        return {"success": True}

    hung = SimpleNamespace(execute_action=hang)
    healthy = SimpleNamespace(execute_action=respond)

    plan = ReconcilePlan(actions=[
        PlannedAction(device_id="hung", domain="power", target="on",
                      command="power_on", params={}, feedback=False),
        PlannedAction(device_id="healthy", domain="power", target="on",
                      command="power_on", params={}, feedback=False),
    ])
    result = await execute_plan(plan, {"hung": hung, "healthy": healthy})

    assert len(result.failures) == 1
    failed_action, err = result.failures[0]
    assert failed_action.device_id == "hung" and "dispatch timeout" in err
    assert [a.device_id for a in result.executed] == ["healthy"]


# --- VWB-46: timing ceilings — the catalog's `max_duration_ms` derivation ----------
# (confirmation_timing.md §4.3). A fixture topology, not the house: two scenarios
# sharing a sink, one exclusive slow feedback device, one ordering delay. Pins that the
# COLD override emits every action, the HOT override every power-off, and that the
# published number is worst graceful teardown + cold activation.

from locveil_bridge.domain.scenarios.reconciler import (  # noqa: E402
    cold_activation_plan,
    deactivate_ceiling_ms,
    hot_teardown_plan,
    plan_ceiling_ms,
    scenario_ceiling_ms,
)


def _fixture_device(cap_json: dict, **state):
    caps = CapabilityMap.model_validate(cap_json)
    st = SimpleNamespace(**state)
    return SimpleNamespace(capabilities=caps, get_current_state=lambda s=st: s)


def _timing_world():
    power_feedback = lambda ms: {  # noqa: E731
        "kind": "stateful", "feedback": True, "state_field": "power",
        "actions": {"on": {"command": "power_on"}, "off": {"command": "power_off"}},
        "gate": {"poll_timeout_ms": ms},
    }
    sink_caps = {
        "power": power_feedback(8000),
        "input": {
            "kind": "stateful", "feedback": True, "state_field": "input",
            "select": {"command": "set_input", "param_map": {"input": "input"}},
            "gate": {"poll_timeout_ms": 3000},
        },
    }
    slow_caps = {"power": power_feedback(25000)}          # the exclusive slow source
    ir_caps = {"power": {                                   # a no-feedback IR source
        "kind": "stateful", "feedback": False, "state_field": "power",
        "actions": {"on": {"command": "power"}, "off": {"command": "power"}},
        "gate": {"delay_ms": 1000},
    }}
    devices = {
        "tv": _fixture_device(sink_caps, power="on", input="hdmi1"),   # WARM: already on
        "slow": _fixture_device(slow_caps, power="on"),
        "ir": _fixture_device(ir_caps, power="off"),
    }
    topology = Topology.model_validate({
        "nodes": {},
        "links": [
            {"from": "slow:out", "to": "tv:hdmi1", "carries": ["video"]},
            {"from": "ir:out", "to": "tv:hdmi2", "carries": ["video"]},
        ],
        "ordering": [{"first": "ir.power", "then": "tv.input", "delay_ms": 4500}],
    })
    s_slow = ScenarioDefinition(scenario_id="s_slow", names={"ru": "а", "en": "a"},
                                room_id="r", source="slow", display="tv")
    s_ir = ScenarioDefinition(scenario_id="s_ir", names={"ru": "б", "en": "b"},
                              room_id="r", source="ir", display="tv")
    return devices, topology, [s_slow, s_ir]


def test_cold_override_emits_every_action_regardless_of_belief():
    devices, topology, (s_slow, s_ir) = _timing_world()
    # the warm planner skips the TV (believed on, on hdmi1) …
    warm = build_plan(s_slow, topology, devices)
    assert [(a.device_id, a.domain) for a in warm.actions] == []
    # … the COLD override plans everything, in `_order`'s order
    cold = cold_activation_plan(s_slow, topology, devices)
    assert [(a.device_id, a.domain) for a in cold.actions] == [
        ("slow", "power"), ("tv", "power"), ("tv", "input"),
    ]
    assert plan_ceiling_ms(cold.actions) == 25000 + 8000 + 3000


def test_hot_override_emits_every_power_off():
    devices, _topology, _ = _timing_world()
    plan = hot_teardown_plan(["ir", "slow", "tv"], devices)   # ir is believed OFF
    assert sorted(a.device_id for a in plan.actions) == ["ir", "slow", "tv"]
    assert plan_ceiling_ms(plan.actions) == 1000 + 25000 + 8000


def test_scenario_ceiling_is_worst_teardown_plus_cold_activation():
    devices, topology, defs = _timing_world()
    s_slow, s_ir = defs
    # s_ir: teardown = the slow source (exclusive to s_slow) 25 s;
    #       activation = ir power 1 s + tv power 8 s + tv input (4.5 s edge delay + 3 s)
    assert scenario_ceiling_ms(s_ir, defs, topology, devices) == 25000 + (1000 + 8000 + 4500 + 3000)
    # s_slow: teardown = the IR source 1 s; activation = 25 s + 8 s + 3 s
    assert scenario_ceiling_ms(s_slow, defs, topology, devices) == 1000 + (25000 + 8000 + 3000)
    # none: the slowest full power-down of any scenario (s_slow: slow 25 s + tv 8 s)
    assert deactivate_ceiling_ms(defs, topology, devices) == 25000 + 8000


def test_plan_ceiling_counts_pre_delays_and_gates_only():
    actions = [
        PlannedAction("a", "power", "on", "p", feedback=True, poll_timeout_ms=5000, delay_ms=999),
        PlannedAction("b", "power", "on", "p", feedback=False, poll_timeout_ms=5000, delay_ms=700),
        PlannedAction("c", "input", "x", "i", feedback=True, poll_timeout_ms=None, delay_ms=300,
                      pre_delay_ms=4000),
    ]
    assert plan_ceiling_ms(actions) == 5000 + 700 + (4000 + 300)
