"""Scenario reconciler (Layer R): derive the ordered action plan to reach an activity's
desired state.

Pipeline (see docs/design/scenarios/scenario_system_redesign.md §7):

1. **resolve** desired state from the thin scenario selection (``source``/``display``/``audio``)
   + the signal topology: which devices are involved, each sink's target input (the link's
   destination port), and any manual-node instructions on the path.
2. **diff** the targets against assumed device state (``device.get_current_state()``); emit an
   action only where current != target.
3. **translate** each action to a native command/params via the device's capability map
   (``param_map`` rename or value-mapped ``by_value``; multi-zone power; toggle-from-assumed-state).
4. **order** by the topology's ``ordering`` edges plus the universal "power before input" rule.

Execution is a separate concern (the ``ScenarioManager`` wires an executor around this plan).
This module is pure: it reads capability maps and assumed state and returns a plan.
"""

import asyncio
import heapq
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# SCN-17: hard bound on a single step's dispatch (`device.execute_action`). Steps are
# globally serialized, so an unbounded driver hang would freeze the whole switch; any
# LEGITIMATE driver-side readiness hold (the eMotiva's is capped at 15 s) plus a slow
# command must fit comfortably under this — it is a guard rail, not pacing.
DISPATCH_TIMEOUT_S = 60.0

from locveil_bridge.domain.topology.models import Topology, TopologyLink


@dataclass
class PlannedAction:
    """One translated, ready-to-execute step in the plan."""

    device_id: str
    domain: str  # "power" | "input"
    target: str  # "on" or the input value
    command: str  # native command name
    params: Dict[str, Any] = field(default_factory=dict)
    feedback: bool = False
    state_field: Optional[str] = None
    poll_timeout_ms: Optional[int] = None  # feedback: poll the state field to target
    delay_ms: int = 0  # device-declared post-action wait (no-feedback)
    pre_delay_ms: int = 0  # extra wait before this action (from a topology ordering edge)
    zone: Optional[str] = None  # multi-zone power
    reason: str = ""
    value_table: Optional[Dict[str, str]] = None  # state_field wire -> canonical (SCN-14)


@dataclass
class ManualStep:
    """A human action required on the path (e.g. a manual RCA hub)."""

    node: str
    instruction: str


@dataclass
class ReconcilePlan:
    actions: List[PlannedAction] = field(default_factory=list)
    manual_steps: List[ManualStep] = field(default_factory=list)
    already_satisfied: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# --- 1. resolve desired state from the topology ------------------------------


def _adjacency(topology: Topology) -> Dict[str, List[TopologyLink]]:
    adj: Dict[str, List[TopologyLink]] = defaultdict(list)
    for link in topology.links:
        adj[link.src_node].append(link)
    return adj


def _find_path(
    adj: Dict[str, List[TopologyLink]], source: str, target: str, signal: str
) -> Optional[List[TopologyLink]]:
    """DFS for a path ``source`` -> ``target`` following links that carry ``signal``."""
    stack: List[Tuple[str, List[TopologyLink]]] = [(source, [])]
    visited: Set[str] = set()
    while stack:
        node, path = stack.pop()
        if node == target:
            return path
        if node in visited:
            continue
        visited.add(node)
        for link in adj.get(node, []):
            if signal in link.carries and link.dst_node not in visited:
                stack.append((link.dst_node, path + [link]))
    return None


def resolve_targets(scenario, topology: Topology):
    """Return ``(input_targets, source_targets, involved, manual_steps, warnings,
    used_ports)`` for a thin scenario.

    ``used_ports[device] = {port, ...}`` collects BOTH endpoints of every link on the
    walked signal paths — unlike ``source_targets`` it never drops a port when dst
    wins for a mid-chain device (the eMotiva is dst ``source2`` AND src ``zone2`` in
    the same start). SCN-16 keys zone-aware power planning off it.

    Symmetric src/dst handling: ``input_targets[dst] = link.dst_port`` for destination
    devices (the existing path — the sink's input must be set to receive the signal),
    AND ``source_targets[src] = link.src_port`` for source devices (the new path —
    the source's "output mode" may need to be engaged for the link to carry the
    signal). The src-side mechanism is opt-in via the device's ``input.source_modes``
    capability declaration; build_plan only emits an action when src_port appears in
    that allowlist. Conflict resolution: dst wins for mid-chain devices (a device that
    is destination of one link and source of the next keeps its dst_port target; the
    src-port slot is skipped via the ``not in source_targets and not in input_targets``
    guards below).
    """
    adj = _adjacency(topology)
    manual_nodes = set(topology.nodes)
    input_targets: Dict[str, str] = {}
    source_targets: Dict[str, str] = {}
    involved: Set[str] = set()
    manual_steps: List[ManualStep] = []
    warnings: List[str] = []
    used_ports: Dict[str, Set[str]] = {}

    # A manual-node source (a turntable/tape with no driver) is not a device to control —
    # it only anchors the topology path so the sink input + manual notes still resolve.
    if scenario.source and scenario.source not in manual_nodes:
        involved.add(scenario.source)

    def walk(target_node: Optional[str], signal: str) -> None:
        if not target_node or not scenario.source:
            return
        path = _find_path(adj, scenario.source, target_node, signal)
        if path is None:
            warnings.append(f"no {signal} path from '{scenario.source}' to '{target_node}'")
            return
        for link in path:
            dst = link.dst_node
            if link.src_node not in manual_nodes:
                used_ports.setdefault(link.src_node, set()).add(link.src_port)
            if dst not in manual_nodes:
                used_ports.setdefault(dst, set()).add(link.dst_port)
            if dst in manual_nodes:
                instruction = topology.nodes[dst].positions.get(link.dst_port)
                if instruction:
                    manual_steps.append(ManualStep(node=dst, instruction=instruction))
                continue
            if dst in input_targets and input_targets[dst] != link.dst_port:
                warnings.append(
                    f"conflicting input for '{dst}': {input_targets[dst]} vs {link.dst_port}"
                )
            input_targets[dst] = link.dst_port
            involved.add(dst)

            # Symmetric source-side: record src_port as a soft target. build_plan
            # only emits an action if the source device's input capability opts in
            # via `source_modes` (default: skip — most sources output passively).
            # dst wins for mid-chain devices: skip if already set as a destination
            # or as a source from a prior link.
            src = link.src_node
            if (
                src not in manual_nodes
                and src not in input_targets
                and src not in source_targets
            ):
                source_targets[src] = link.src_port
                involved.add(src)

    walk(scenario.display, "video")
    walk(scenario.audio, "audio")
    return input_targets, source_targets, involved, manual_steps, warnings, used_ports


# --- 2/3. diff + translate ---------------------------------------------------


def _state_value(state: Any, field_name: Optional[str]) -> Any:
    return getattr(state, field_name, None) if field_name else None


def _norm_value(v: Any) -> str:
    """Vocabulary-insensitive form for state-vs-target comparison: lowercase,
    alphanumerics only. Bridges canonical targets to raw driver state where no enum
    table exists — 'hdmi2' == 'HDMI2' == 'HDMI_2' (the LG webOS forms; REL-3 finding
    F2: exact comparison meant the TV-input gate NEVER confirmed)."""
    return "".join(ch for ch in str(v).lower() if ch.isalnum())


def _satisfies(observed: Any, target: Any, value_table: Optional[Dict[str, str]] = None) -> bool:
    """Does an observed state value satisfy a plan target? (SCN-14, extended to ALL
    comparison sites by DRV-33's sitting-#2 follow-up: the diff in `_input_action`/
    `_power_actions` and the SCN-11 preview must agree with the execution gate —
    'HDMI_2' vs 'hdmi2' read as out-of-sync in the dialog while the gate matched.)
    Exact match first; then the capability's wire→canonical table when declared;
    then normalized comparison as the tableless fallback."""
    if observed is None:
        return target is None
    if observed == target:
        return True
    if value_table and value_table.get(str(observed)) == target:
        return True
    return _norm_value(observed) == _norm_value(target)


def _wire_table(cap: Any, field_name: Optional[str]) -> Optional[Dict[str, str]]:
    """wire -> canonical map for the capability field backing ``field_name`` (SCN-14).

    Attached to the PlannedAction so the execution gate can compare the device's
    *reported* (wire) state against the plan's *canonical* target. None when the
    capability declares no enum table for the field — the gate then falls back to
    normalized comparison."""
    if not field_name:
        return None
    for f in getattr(cap, "fields", None) or []:
        if f.name == field_name and f.values:
            return {str(v.wire): str(v.canonical) for v in f.values}
    return None


def _zone_on_path(zone, used_ports: Optional[Set[str]]) -> bool:
    """SCN-16: a zone with a declared ``port`` is planned only when the scenario's
    resolved path uses that port on this device. Portless zones (the main zone) and
    calls with no path context (``used_ports is None``) always plan."""
    return zone.port is None or used_ports is None or zone.port in used_ports


def _power_actions(device_id, cap, state, warnings, force: bool = False,
                   used_ports: Optional[Set[str]] = None) -> List[PlannedAction]:
    """Emit power actions to bring a device ON (multi-zone aware, toggle aware).

    ``force`` (SCN-11): skip the believed-vs-target diff — emit unconditionally toward
    the target — and inject the reserved ``force`` param so driver idempotence guards
    don't re-swallow the command. Toggle-only power additionally carries
    ``assume_state`` = the plan target: a forced toggle must claim the *target* state
    afterwards, not blind-flip the (possibly wrong) belief — otherwise forcing a
    desynced toggle device recreates the desync mirrored.
    """
    out: List[PlannedAction] = []
    gate = cap.gate

    if cap.zones:
        for zone_key, zone in cap.zones.items():
            if not _zone_on_path(zone, used_ports):
                continue  # off the used signal path — never touched (SCN-16)
            if not force and _satisfies(_state_value(state, zone.state_field), zone.on_value,
                                        _wire_table(cap, zone.state_field)):
                continue
            act = zone.actions.get("on")
            if not act:
                warnings.append(f"{device_id} power zone {zone_key} has no 'on' action")
                continue
            params = dict(act.params)
            if force:
                params["force"] = True
            out.append(PlannedAction(
                device_id=device_id, domain="power", target=zone.on_value,
                command=act.command, params=params, feedback=cap.feedback,
                state_field=zone.state_field, poll_timeout_ms=gate.poll_timeout_ms,
                delay_ms=gate.delay_ms, zone=zone_key, reason=f"power zone {zone_key} on",
                value_table=_wire_table(cap, zone.state_field),
            ))
        return out

    if not force and _satisfies(_state_value(state, cap.state_field), cap.on_value,
                                _wire_table(cap, cap.state_field)):
        return out
    act = cap.actions.get("on") or cap.actions.get("toggle")
    if not act:
        warnings.append(f"{device_id} power has no 'on' or 'toggle' action")
        return out
    is_toggle = "on" not in cap.actions and "toggle" in cap.actions
    params = dict(act.params)
    if force:
        params["force"] = True
        if is_toggle:
            params["assume_state"] = cap.on_value
    out.append(PlannedAction(
        device_id=device_id, domain="power", target=cap.on_value,
        command=act.command, params=params, feedback=cap.feedback,
        state_field=cap.state_field, poll_timeout_ms=gate.poll_timeout_ms,
        delay_ms=gate.delay_ms, reason="power on (toggle)" if is_toggle else "power on",
        value_table=_wire_table(cap, cap.state_field),
    ))
    return out


def _off_target(on_value: Any) -> Any:
    """The gate-poll comparison value for power OFF.

    Boolean on_values complement (the Auralic power gate keys on `connected:
    true` — polling for the string "off" against a bool burned the full
    25 s poll_timeout on every teardown, rack finding 2026-07-07); string
    fields keep the "off" convention (eMotiva/LG power fields).
    """
    return (not on_value) if isinstance(on_value, bool) else "off"


def _power_off_actions(device_id, cap, state) -> List[PlannedAction]:
    """Emit power-off actions for a device that is currently on (multi-zone/toggle aware)."""
    out: List[PlannedAction] = []
    gate = cap.gate
    if cap.zones:
        for zone_key, zone in cap.zones.items():
            if not _satisfies(_state_value(state, zone.state_field), zone.on_value,
                              _wire_table(cap, zone.state_field)):
                continue  # already off
            act = zone.actions.get("off") or zone.actions.get("toggle")
            if not act:
                continue
            out.append(PlannedAction(
                device_id=device_id, domain="power", target=_off_target(zone.on_value),
                command=act.command, params=dict(act.params), feedback=cap.feedback,
                state_field=zone.state_field, poll_timeout_ms=gate.poll_timeout_ms,
                delay_ms=gate.delay_ms, zone=zone_key, reason=f"power zone {zone_key} off",
                value_table=_wire_table(cap, zone.state_field),
            ))
        return out

    if not _satisfies(_state_value(state, cap.state_field), cap.on_value,
                      _wire_table(cap, cap.state_field)):
        return out  # already off
    act = cap.actions.get("off") or cap.actions.get("toggle")
    if not act:
        return out
    out.append(PlannedAction(
        device_id=device_id, domain="power", target=_off_target(cap.on_value),
        command=act.command, params=dict(act.params), feedback=cap.feedback,
        state_field=cap.state_field, poll_timeout_ms=gate.poll_timeout_ms,
        delay_ms=gate.delay_ms, reason="power off",
        value_table=_wire_table(cap, cap.state_field),
    ))
    return out


def build_power_off_plan(device_ids, devices: Dict[str, Any]) -> ReconcilePlan:
    """Build a teardown plan that powers off the given devices (those currently on)."""
    plan = ReconcilePlan()
    for device_id in device_ids:
        device = devices.get(device_id)
        if device is None:
            plan.warnings.append(f"device '{device_id}' not found")
            continue
        cap_map = getattr(device, "capabilities", None)
        power_cap = cap_map.get("power") if cap_map else None
        # SCN-12: honour reconcile:false on teardown too — mirror build_plan's power-on
        # guard (line ~406). A reconcile:false power capability (e.g. the upscaler, which
        # auto-powers with the LD) is exposed on the page/WB/HTTP but the reconciler must
        # not drive it; without this guard a graceful switch / shutdown emitted an
        # unwanted IR power_off to it, and any future always-on / toggle-power sink would
        # be actively mis-driven.
        if power_cap is None or not power_cap.reconcile:
            continue
        plan.actions.extend(_power_off_actions(device_id, power_cap, device.get_current_state()))
    return plan


def _input_action(device_id, cap, state, target_value, warnings, force: bool = False) -> Optional[PlannedAction]:
    if not force and _satisfies(_state_value(state, cap.state_field), target_value,
                                _wire_table(cap, cap.state_field)):
        return None
    sel = cap.select
    if sel is None:
        warnings.append(f"{device_id} has no input.select")
        return None
    gate = cap.gate

    # Shared select resolution (VWB-19) — same expansion the canonical endpoint uses.
    try:
        steps = sel.expand(target_value)
    except ValueError as e:
        warnings.append(f"{device_id} input: {e}")
        return None
    if len(steps) != 1:
        warnings.append(
            f"{device_id} input '{target_value}' expands to {len(steps)} steps; "
            "multi-step select is not reconcilable"
        )
        return None
    command, params = steps[0].command, dict(steps[0].params)
    if force:
        params["force"] = True  # SCN-11: bypass driver idempotence guards

    return PlannedAction(
        device_id=device_id, domain="input", target=target_value, command=command,
        params=params, feedback=cap.feedback, state_field=cap.state_field,
        poll_timeout_ms=gate.poll_timeout_ms, delay_ms=gate.delay_ms,
        reason=f"input -> {target_value}",
        value_table=_wire_table(cap, cap.state_field),
    )


# --- 4. order ----------------------------------------------------------------


def _matches(action: PlannedAction, key: str) -> bool:
    device, _, domain = key.partition(".")
    return action.device_id == device and action.domain == domain


def _order(actions: List[PlannedAction], topology: Topology) -> List[PlannedAction]:
    """Topologically order actions by power-before-input + topology ordering edges.

    Stable: ties break by original index. ``delay_ms`` from an ordering edge becomes the
    successor action's ``pre_delay_ms``.
    """
    n = len(actions)
    succ: Dict[int, Set[int]] = defaultdict(set)
    indeg = [0] * n
    pre_delay = [0] * n

    def add_edge(i: int, j: int, delay: int = 0) -> None:
        if i != j and j not in succ[i]:
            succ[i].add(j)
            indeg[j] += 1
        if delay:
            pre_delay[j] = max(pre_delay[j], delay)

    # universal: power before input, per device
    for i, a in enumerate(actions):
        if a.domain != "power":
            continue
        for j, b in enumerate(actions):
            if b.domain == "input" and b.device_id == a.device_id:
                add_edge(i, j)

    # topology ordering edges (first -> then), applied to all matching action pairs
    for edge in topology.ordering:
        firsts = [i for i, a in enumerate(actions) if _matches(a, edge.first)]
        thens = [j for j, b in enumerate(actions) if _matches(b, edge.then)]
        for i in firsts:
            for j in thens:
                add_edge(i, j, edge.delay_ms)

    ready = [i for i in range(n) if indeg[i] == 0]
    heapq.heapify(ready)
    order: List[int] = []
    while ready:
        i = heapq.heappop(ready)
        order.append(i)
        for j in sorted(succ[i]):
            indeg[j] -= 1
            if indeg[j] == 0:
                heapq.heappush(ready, j)

    if len(order) != n:  # cycle (shouldn't happen); fall back to input order
        order = list(range(n))

    result: List[PlannedAction] = []
    for idx in order:
        actions[idx].pre_delay_ms = pre_delay[idx]
        result.append(actions[idx])
    return result


# --- top-level ---------------------------------------------------------------


def build_plan(scenario, topology: Topology, devices: Dict[str, Any]) -> ReconcilePlan:
    """Build the ordered reconcile plan for a thin ``scenario``.

    ``devices`` maps device_id -> device (each with ``.capabilities`` and ``.get_current_state()``).
    """
    plan = ReconcilePlan()
    input_targets, source_targets, involved, manual_steps, warnings, used_ports = resolve_targets(
        scenario, topology
    )
    plan.manual_steps = manual_steps
    plan.warnings.extend(warnings)

    raw: List[PlannedAction] = []
    for device_id in sorted(involved):
        device = devices.get(device_id)
        if device is None:
            plan.warnings.append(f"device '{device_id}' not found")
            continue
        cap_map = getattr(device, "capabilities", None)
        if cap_map is None:
            plan.warnings.append(f"device '{device_id}' has no capability map")
            continue
        state = device.get_current_state()

        power_cap = cap_map.get("power")
        if power_cap is not None and power_cap.reconcile:
            power_actions = _power_actions(device_id, power_cap, state, plan.warnings,
                                           used_ports=used_ports.get(device_id))
            if power_actions:
                raw.extend(power_actions)
            else:
                plan.already_satisfied.append(f"{device_id}.power")

        if device_id in input_targets:
            input_cap = cap_map.get("input")
            if input_cap is None:
                plan.warnings.append(f"device '{device_id}' has no input capability")
            elif input_cap.reconcile:
                action = _input_action(device_id, input_cap, state, input_targets[device_id], plan.warnings)
                if action is not None:
                    raw.append(action)
                else:
                    plan.already_satisfied.append(f"{device_id}.input")
        elif device_id in source_targets:
            # Source-side (src_port → set_input) is opt-in via input.source_modes. Most
            # sources output passively (Apple TV / Zappiti / Auralic → always-on HDMI/SPDIF
            # output, no engagement needed), so the default of None silently skips. Only
            # devices that declare the src_port in source_modes get an action emitted.
            input_cap = cap_map.get("input")
            src_port = source_targets[device_id]
            if (
                input_cap is not None
                and input_cap.reconcile
                and input_cap.source_modes is not None
                and src_port in input_cap.source_modes
            ):
                action = _input_action(device_id, input_cap, state, src_port, plan.warnings)
                if action is not None:
                    raw.append(action)
                else:
                    plan.already_satisfied.append(f"{device_id}.input")

    plan.actions = _order(raw, topology)
    return plan


# --- VWB-34/VWB-46: timing ceilings (the catalog's `max_duration_ms`) ----------


def plan_ceiling_ms(actions: List[PlannedAction]) -> int:
    """Worst-case wall clock of the bridge's own waiting over an ordered chain:
    per step its pre-delay plus its gate (feedback devices may poll up to
    ``poll_timeout_ms``; no-feedback devices wait ``delay_ms``). Execution is strictly
    sequential, so the sum IS the ceiling. The one formula behind the force-reconcile
    dialog's ETA and the catalog's scenario ``max_duration_ms`` (confirmation_timing.md
    §4.3) — never computed two ways. Dispatch time itself is not counted: it is bounded
    by ``DISPATCH_TIMEOUT_S`` as a hang guard, and the eMotiva's readiness hold (DRV-39)
    is absorbed by the consumer's margin by decision (PROD-18)."""
    total = 0
    for a in actions:
        total += a.pre_delay_ms
        total += a.poll_timeout_ms if (a.feedback and a.poll_timeout_ms) else a.delay_ms
    return total


class _ColdState:
    """Every state attribute reads ``None`` — nothing is satisfied, every action is
    emitted: the maximal activation plan the real planner can produce."""

    def __getattr__(self, name: str) -> None:
        return None


class _AnyValue:
    """Equal to anything — a state value that satisfies every ``on_value``."""

    def __eq__(self, other: object) -> bool:
        return True

    def __hash__(self) -> int:
        return 0

    def __str__(self) -> str:
        return "on"


class _HotState:
    """Every state attribute reads as 'on' — every power-off is emitted: the maximal
    teardown plan."""

    def __getattr__(self, name: str) -> Any:
        return _AnyValue()


class _StateOverride:
    """A stand-in around a real device object: the same capability map, a fixed
    answer to every state question. The planners read nothing else about a device."""

    def __init__(self, device: Any, state: Any):
        self.capabilities = getattr(device, "capabilities", None)
        self._state = state

    def get_current_state(self) -> Any:
        return self._state


def _override(devices: Dict[str, Any], state: Any) -> Dict[str, Any]:
    return {device_id: _StateOverride(d, state) for device_id, d in devices.items()}


def cold_activation_plan(scenario, topology: Topology, devices: Dict[str, Any]) -> ReconcilePlan:
    """``build_plan`` with every device believed in no state at all (the COLD
    override): every power action on the resolved path (zone-aware, SCN-16), every
    input action, `reconcile: false` capabilities skipped exactly as at runtime."""
    return build_plan(scenario, topology, _override(devices, _ColdState()))


def hot_teardown_plan(device_ids, devices: Dict[str, Any]) -> ReconcilePlan:
    """``build_power_off_plan`` with every device believed ON (the HOT override):
    every power-off action for the given devices, every zone."""
    return build_power_off_plan(device_ids, _override(devices, _HotState()))


def scenario_ceiling_ms(scenario, room_scenarios, topology: Topology, devices: Dict[str, Any]) -> int:
    """``max_duration_ms`` for activating ``scenario`` from ANY state of its room: the
    worst graceful teardown (the outgoing scenario whose exclusive devices confirm
    slowest) plus the cold activation. ``room_scenarios`` are every definition of the
    room, the scenario itself included (it is skipped)."""
    activation = plan_ceiling_ms(cold_activation_plan(scenario, topology, devices).actions)
    incoming = resolve_targets(scenario, topology)[2]
    teardown = 0
    for other in room_scenarios:
        if other.scenario_id == scenario.scenario_id:
            continue
        outgoing = resolve_targets(other, topology)[2]
        plan = hot_teardown_plan(sorted(outgoing - incoming), devices)
        teardown = max(teardown, plan_ceiling_ms(plan.actions))
    return teardown + activation


def deactivate_ceiling_ms(room_scenarios, topology: Topology, devices: Dict[str, Any]) -> int:
    """``max_duration_ms`` for the ``none`` value: the slowest full power-down of any
    scenario of the room."""
    worst = 0
    for defn in room_scenarios:
        involved = resolve_targets(defn, topology)[2]
        plan = hot_teardown_plan(sorted(involved), devices)
        worst = max(worst, plan_ceiling_ms(plan.actions))
    return worst


# --- SCN-11: per-device force-reconcile (user-mediated desync repair) ---------


def _device_input_target(device_id, cap_map, input_targets, source_targets):
    """The device's resolved input target, honoring the dst-wins + source_modes opt-in
    rules build_plan applies. None when the device has no (reconcilable) input target."""
    input_cap = cap_map.get("input")
    if input_cap is None or not input_cap.reconcile:
        return None, None
    if device_id in input_targets:
        return input_cap, input_targets[device_id]
    if device_id in source_targets:
        src_port = source_targets[device_id]
        if input_cap.source_modes is not None and src_port in input_cap.source_modes:
            return input_cap, src_port
    return None, None


def build_forced_device_plan(scenario, topology: Topology, devices: Dict[str, Any], device_id: str) -> ReconcilePlan:
    """Single-device FORCED plan (SCN-11): emit the device's power + input actions
    unconditionally toward the scenario targets — the believed-vs-desired diff is
    deliberately skipped, because the whole point is that the belief may be wrong and
    the user (standing in the room) is the feedback channel. Each action carries the
    reserved ``force`` param (bypasses driver idempotence guards, DRV-5) and toggles
    carry ``assume_state`` (claim the target, don't blind-flip).

    Ordered through the same ``_order`` as a full plan; cross-device ordering edges
    drop out *correctly* — ``_order`` only applies an edge when both endpoints are in
    the plan, and the other devices are presumed already settled.
    """
    plan = ReconcilePlan()
    input_targets, source_targets, involved, _manual, _warnings, used_ports = resolve_targets(
        scenario, topology
    )

    if device_id not in involved:
        plan.warnings.append(f"device '{device_id}' is not involved in this scenario")
        return plan
    device = devices.get(device_id)
    if device is None:
        plan.warnings.append(f"device '{device_id}' not found")
        return plan
    cap_map = getattr(device, "capabilities", None)
    if cap_map is None:
        plan.warnings.append(f"device '{device_id}' has no capability map")
        return plan
    state = device.get_current_state()

    raw: List[PlannedAction] = []
    power_cap = cap_map.get("power")
    if power_cap is not None and power_cap.reconcile:
        raw.extend(_power_actions(device_id, power_cap, state, plan.warnings, force=True,
                                  used_ports=used_ports.get(device_id)))

    input_cap, target = _device_input_target(device_id, cap_map, input_targets, source_targets)
    if input_cap is not None and target is not None:
        action = _input_action(device_id, input_cap, state, target, plan.warnings, force=True)
        if action is not None:
            raw.append(action)

    plan.actions = _order(raw, topology)
    return plan


@dataclass
class DomainComparison:
    """Believed vs desired for one capability domain of one device."""

    domain: str  # "power" | "input"
    believed: Any
    desired: Any
    in_sync: bool


@dataclass
class DevicePreview:
    """One row of the force-reconcile dialog: what the bridge believes vs what the
    active scenario wants, plus the forced plan a confirm would run. NB the inversion:
    an ``in_sync`` row is exactly where force exists — "in sync" only means the
    *believed* state matches; the user may be looking at a device that disagrees."""

    device_id: str
    comparisons: List[DomainComparison] = field(default_factory=list)
    in_sync: bool = True
    plan: ReconcilePlan = field(default_factory=ReconcilePlan)


def build_reconcile_preview(scenario, topology: Topology, devices: Dict[str, Any]) -> List[DevicePreview]:
    """Per-device believed-vs-desired rows for the active scenario (SCN-11). Pure —
    reads capability maps + assumed state, mutates nothing."""
    input_targets, source_targets, involved, _manual, _warnings, used_ports = resolve_targets(
        scenario, topology
    )
    previews: List[DevicePreview] = []

    for device_id in sorted(involved):
        device = devices.get(device_id)
        if device is None:
            continue
        cap_map = getattr(device, "capabilities", None)
        if cap_map is None:
            continue
        state = device.get_current_state()
        comparisons: List[DomainComparison] = []

        power_cap = cap_map.get("power")
        if power_cap is not None and power_cap.reconcile:
            if power_cap.zones:
                # SCN-16 parity: the dialog's desired set matches the planner's —
                # a zone off the used signal path is neither desired nor compared.
                zones = {
                    zk: z for zk, z in power_cap.zones.items()
                    if _zone_on_path(z, used_ports.get(device_id))
                }
                believed: Any = {
                    zk: _state_value(state, z.state_field) for zk, z in zones.items()
                }
                desired: Any = {zk: z.on_value for zk, z in zones.items()}
                power_ok = all(
                    _satisfies(believed.get(zk), z.on_value, _wire_table(power_cap, z.state_field))
                    for zk, z in zones.items()
                )
            else:
                believed = _state_value(state, power_cap.state_field)
                desired = power_cap.on_value
                power_ok = _satisfies(believed, desired, _wire_table(power_cap, power_cap.state_field))
            comparisons.append(DomainComparison("power", believed, desired, power_ok))

        input_cap, target = _device_input_target(device_id, cap_map, input_targets, source_targets)
        if input_cap is not None and target is not None:
            believed = _state_value(state, input_cap.state_field)
            comparisons.append(DomainComparison(
                "input", believed, target,
                _satisfies(believed, target, _wire_table(input_cap, input_cap.state_field)),
            ))

        previews.append(DevicePreview(
            device_id=device_id,
            comparisons=comparisons,
            in_sync=all(c.in_sync for c in comparisons),
            plan=build_forced_device_plan(scenario, topology, devices, device_id),
        ))
    return previews


# --- execution ---------------------------------------------------------------


@dataclass
class ExecutionResult:
    executed: List[PlannedAction] = field(default_factory=list)
    failures: List[Tuple[PlannedAction, str]] = field(default_factory=list)
    manual_steps: List[ManualStep] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return not self.failures


def _response_ok(resp: Any) -> Tuple[bool, Optional[str]]:
    """Read success/error from a CommandResponse (dict-like) without assuming a type."""
    if isinstance(resp, dict):
        return bool(resp.get("success", True)), resp.get("error")
    return bool(getattr(resp, "success", True)), getattr(resp, "error", None)


def _gate_reached(observed: Any, action: PlannedAction) -> bool:
    """Does the device's reported state satisfy the plan's canonical target? (SCN-14)
    Delegates to the shared `_satisfies` — the diff, the SCN-11 preview, and this
    gate must never disagree about what "reached" means."""
    if observed is None:
        return False
    return _satisfies(observed, action.target, action.value_table)


async def _gate(device: Any, action: PlannedAction, poll_interval_ms: int) -> bool:
    """Wait for an action to take effect: poll feedback devices to the target value (up to
    ``poll_timeout_ms``); otherwise wait the fixed ``delay_ms``. Returns False on poll timeout."""
    if action.feedback and action.state_field and action.poll_timeout_ms:
        elapsed = 0
        while elapsed < action.poll_timeout_ms:
            observed = getattr(device.get_current_state(), action.state_field, None)
            if _gate_reached(observed, action):
                return True
            await asyncio.sleep(poll_interval_ms / 1000)
            elapsed += poll_interval_ms
        return False
    if action.delay_ms:
        await asyncio.sleep(action.delay_ms / 1000)
    return True


async def execute_plan(
    plan: ReconcilePlan,
    devices: Dict[str, Any],
    *,
    abort_on_failure: bool = False,
    poll_interval_ms: int = 200,
) -> ExecutionResult:
    """Execute an ordered plan: honor pre-delays, dispatch the native command, check success
    (failures are surfaced, not swallowed -- fixes RC2), then gate before the next step.

    SCN-14: a gate timeout on a ``feedback: true`` step is a FAILURE, not noise — the
    device reported state and it never reached the target (REL-3 finding F3:
    ``tv_on_speakers`` reported success twice while ARC never engaged). Such a step
    appears in BOTH ``executed`` (it was dispatched and acked) and ``failures`` (it
    did not take effect); ``success`` keys off ``failures``. Feedback-less steps keep
    the optimistic path — there is nothing to know."""
    result = ExecutionResult(manual_steps=list(plan.manual_steps))

    for action in plan.actions:
        if action.pre_delay_ms:
            await asyncio.sleep(action.pre_delay_ms / 1000)

        device = devices.get(action.device_id)
        if device is None:
            result.failures.append((action, "device not found"))
            if abort_on_failure:
                break
            continue

        try:
            resp = await asyncio.wait_for(
                device.execute_action(action.command, action.params, source="scenario"),
                timeout=DISPATCH_TIMEOUT_S,
            )
            ok, err = _response_ok(resp)
        except TimeoutError:
            # SCN-17: a wedged/hung driver must cost one failed step, not the plan.
            ok, err = False, f"dispatch timeout: no response within {DISPATCH_TIMEOUT_S:.0f}s"
        except Exception as exc:  # noqa: BLE001 - surface any driver error as a failure
            ok, err = False, str(exc)

        if not ok:
            result.failures.append((action, err or "command failed"))
            logger.error(
                "scenario step failed: %s %s(%s) -> %s",
                action.device_id, action.command, action.params, err,
            )
            if abort_on_failure:
                break
            continue

        result.executed.append(action)
        if not await _gate(device, action, poll_interval_ms):
            err = (
                f"gate timeout: {action.domain} did not reach {action.target!r} "
                f"within {action.poll_timeout_ms}ms (device reported state never confirmed)"
            )
            result.failures.append((action, err))
            logger.error("scenario step not confirmed: %s %s -> %s",
                         action.device_id, action.command, err)
            if abort_on_failure:
                break

    return result
