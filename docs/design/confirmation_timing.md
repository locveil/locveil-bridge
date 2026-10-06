# Confirmation timing published in the contract — design (VWB-34)

**Status: COUNCIL-DECIDED 2026-10-06, implementation pending** (board PROD-18 — round 1
decided tiers 1 and 2 and the cut; round 2, the same day, decided the consumer's policy and
moved tier 3 into its own arc). The owner's "Round 1 DECIDED" / "Round 2 DECIDED" paragraphs
in `../../../locveil-commons/board/BOARD.md` are the decisions of record; this document designs
to them and reopens nothing. Implementation: **VWB-46** (the `catalog-v1.11.0` cut, shared
with [`language_data_convention.md`](language_data_convention.md)). Tier 3: **VWB-47** (§7).
**Where the machine rule will live:** the pinned guide
[`contracts/catalog/catalog-contract.md`](../../contracts/catalog/catalog-contract.md), new
section "Timing (since contract v1.11)" — §5 of this document is its text, verbatim.

## 1. The problem, restated against the code

DRV-29 made the canonical endpoint honour a capability's `gate.poll_timeout_ms` as its echo
window (`presentation/api/routers/devices.py:589-595`, default `CANONICAL_ECHO_TIMEOUT_S =
0.5`). The number that decides whether a voice request succeeds or times out therefore lives
in the bridge's capability maps — and reached the voice repo as a sentence in a handover note
("your HTTP timeout must exceed 15 s"). Voice today sizes every bridge request with one 20 s
constant. Retune a gate, or add a device that confirms slowly, and voice's timeouts fire with
no signal in the pinned catalog. Scenarios are worse: `scenario.set(value)` is awaited
end-to-end (`scenario_proxy.activate` → `switch_scenario` → two `execute_plan` runs,
`domain/scenarios/service.py:289-323`), the cold movie scenarios take 30–36 s of gate budget
alone (§4), so a 20 s client timeout fires on every cold start — and a retry runs the plan
again on top of the first (the in-flight lock is missing; tier 3's problem, §7).

The fix at this layer: publish what a consumer may expect to wait, where the action is.

## 2. Decisions this design executes

| # | Decision | Where it lands |
|---|---|---|
| 5 (round 1) | **Tier 1:** optional `confirm_timeout_ms` per capability, present iff the gate has a poll timeout, equal to it; absent = the 500 ms default echo window; `delay_ms` stays unexposed. | §3 |
| 5 (round 1) | **Tier 2:** optional `max_duration_ms` on each scenario value label, computed from the cold plan — sequential execution ⇒ the sum is the ceiling. | §4 |
| 5, 7 | Both in the minor `catalog-v1.11.0`; CORE-12 takes the next; every PROD-18 task `[release]`. | VWB-46 |
| riders | The HvacPanel progress expectation is a separate later UI task; `delay_ms` is implementation. | §6 |
| 8 (round 2) | Voice sizes requests from the field (× 1.25 + 2 s), the config value is the fallback; acknowledge-then-confirm above ~3 s, behind one flag. | §6 (cited, not designed here) |
| 9–10 (round 2) | Tier 3 is its own arc, own cut `catalog-v1.12.0`, one-cut condition waived for it. | §7 → VWB-47 |

## 3. Tier 1 — `confirm_timeout_ms` on a capability

**Shape.** `CatalogCapability` (`presentation/api/schemas.py:292`) gains

```python
confirm_timeout_ms: Optional[int] = Field(
    None,
    description="Longest the bridge waits for this capability's device to confirm an "
                "action before reporting failure. Absent = the default 500 ms echo window.",
)
```

emitted with `response_model_exclude_none` semantics the catalog already uses, so an absent
value is absent, not `null`-noise.

**Derivation.** In `_project_capability_actions` (`presentation/api/catalog.py:167`):
`confirm_timeout_ms = cap.gate.poll_timeout_ms if cap.gate.poll_timeout_ms else None`. That is
the whole rule — the same field, the same truthiness test the endpoint applies
(`devices.py:592`), so the published number and the window the endpoint actually waits can
never disagree. `feedback` is not consulted: the endpoint does not consult it either (every
gate with a poll timeout in today's fleet is on a feedback capability, but the rule follows
the endpoint, not the fleet). `gate.delay_ms` is not published: it paces the reconciler
between scenario steps and never bounds a canonical request. The scenario manager's own
`scenario` capability carries no `confirm_timeout_ms` — its bound is tier 2's per-value number.

**What it promises.** A `wait: true` request (the default) on this capability returns a
verdict within `confirm_timeout_ms` plus transport: `200` with the confirmed state, or `503
device_unreachable` when the echo never landed. It is a promise about the bridge's own
waiting, not a measurement of the device — the HVAC confirms in 5–7 s typically against its
15 s window (DRV-29's derivation from the mitsubishi2wb packet cadence), the LG TV's power in
~2 s against 8 s.

**The published numbers — today's fleet** (from the resolved capability maps,
`config/capabilities/`, 2026-10-06; 27 capabilities carry the field, every other capability in
the catalog is absent = 500 ms):

| Device(s) | Capability | `confirm_timeout_ms` | Source |
|---|---|---|---|
| `bedroom_hvac`, `children_room_hvac`, `living_room_hvac` | `power`, `mode`, `fan`, `vane`, `widevane`, `temperature` (18 rows) | **15000** | `classes/MitsubishiHvac.json` — DRV-29: 1 s send interval + 6-slot × 2 s info rotation + margin |
| `living_room_tv`, `children_room_tv` | `power` | **8000** | `classes/LgTv.json` |
| `living_room_tv`, `children_room_tv` | `input` | **3000** | `classes/LgTv.json` |
| `appletv_living`, `appletv_children` | `power` | **5000** | `classes/AppleTVDevice.json` |
| `streamer` | `power` | **25000** | `classes/AuralicDevice.json` — standby/halt wake |
| `streamer` | `input` | **3000** | `classes/AuralicDevice.json` |
| `processor` | `input` | **3000** | `classes/EMotivaXMC2.json` |
| *(absent — 500 ms)* | every WB-passthrough capability (the relay fleet echoes in milliseconds), `kitchen_hood.*`, the IR devices' `power`/`input` (`mf_amplifier`, `ld_player`, `vhs_player`, `video`, `upscaler` — no feedback; their state settles synchronously inside dispatch), every momentary capability | — | |

Observation recorded, not acted on: `processor.power` is zone-form (`zones` with no
capability-level `actions`) and is **not in the catalog** at all (the projection walks
`cap.actions` only), so its 6 s gate has no capability to ride on. The eMotiva's power is
reachable through scenarios, not through the canonical device surface — a pre-existing gap,
outside PROD-18.

**UI.** The `ForceReconcileDialog` already renders `confirm ≤Ns` from the preview rows'
`poll_timeout_ms`; tier 1 gives the catalog the same number for the same reason. No component
changes; `openapi.gen.ts` regenerates with the new optional field.

## 4. Tier 2 — `max_duration_ms` on a scenario value

### 4.1 Shape

`CatalogValueLabel` (`schemas.py:264`) gains

```python
max_duration_ms: Optional[int] = Field(
    None,
    description="Scenario values only: the ceiling for activating this scenario from any "
                "state in its room (the full sequential switch chain, worst case). On the "
                "`none` entry: the ceiling for deactivating the room.",
)
```

Set by `_project_scenario_managers` (`catalog.py:246`) on every entry of the scenario
`set(value)` param table and of the `scenario` field table — including the field's `none`
entry (deactivation has a ceiling too). Every other value table leaves it absent. Values are
exact millisecond sums, never rounded: the same configuration always publishes the same
number, so the content hash moves only when timing really moves.

### 4.2 Why a ceiling and not an estimate

A switch's real duration is diff-dependent: `build_plan` emits an action only where believed
state ≠ target (`reconciler.py:269-271, 363-365`), so a warm switch between scenarios sharing
the TV and the processor costs seconds while a cold start walks the whole topology. A single
published number can only be honest as the upper bound of the bridge's own waiting. Execution
is strictly sequential — `execute_plan` awaits each dispatch, then its gate, then the next
step's pre-delay (`reconciler.py:739-782`) — so the worst case is a plain sum, not a critical
path.

### 4.3 The derivation, exactly

For scenario **S** in room **R**, with `involved(X)` = the device set `resolve_targets(X,
topology)` returns (`reconciler.py:101`):

```
max_duration_ms(S) = teardown_ceiling(S) + activation_ceiling(S)

activation_ceiling(S) = plan_ceiling( build_plan(S, topology, COLD(devices)) )
teardown_ceiling(S)   = max over O in scenarios(R), O ≠ S, of
                        plan_ceiling( build_power_off_plan( involved(O) − involved(S), HOT(devices) ) )

plan_ceiling(plan) = Σ over plan.actions of
                        pre_delay_ms
                      + ( poll_timeout_ms  if feedback and poll_timeout_ms
                          else delay_ms )

max_duration_ms(none, R) = max over S in scenarios(R) of
                        plan_ceiling( build_power_off_plan( involved(S), HOT(devices) ) )
```

`plan_ceiling` is today's `_eta_ms` (`presentation/api/routers/scenarios.py:190`) — the
implementation moves that formula into `domain/scenarios/reconciler.py` as the one home and
has the preview endpoint call it, so the dialog's ETA and the catalog's ceiling can never be
computed two ways.

**The state-override variant of `build_plan`.** `build_plan` and `build_power_off_plan` read
`device.get_current_state()` and nothing else about state; the "cold" and "hot" plans are the
ordinary planners run over stand-ins whose state answers every question the same way:

- `COLD(devices)`: each stand-in's `get_current_state()` returns an object whose every
  attribute is `None`. `_satisfies(None, target)` is `target is None` (`reconciler.py:201`),
  and no plan target is `None`, so **every** power action (every zone on the resolved path —
  SCN-16's `_zone_on_path` still applies, which is why the eMotiva's zone 2 is planned for
  `movie_appletv` and not for `movie_ld`) and every input action is emitted; `reconcile:
  false` capabilities are skipped exactly as at runtime (the upscaler's power, which follows
  the LD). The result is the maximal activation plan the real planner can ever produce for S,
  ordered by `_order` with the topology's `delay_ms` edges as `pre_delay_ms`.
- `HOT(devices)`: each stand-in's state returns, for every attribute, a value equal to
  anything (`__eq__` → `True`), so `_satisfies(observed, on_value)` holds for every zone and
  field and every power-off action is emitted — the maximal teardown.

The planners are not modified; the override lives in a small helper beside them
(`cold_activation_plan(scenario, topology, devices)` / `hot_teardown_plan(device_ids,
devices)`), the stand-ins wrap the real device objects' capability maps. `ScenarioProxy`
exposes `max_duration_ms(room_id, scenario_id)` and `deactivate_ceiling_ms(room_id)`; the
catalog builder reads those. Offline (`locveil-catalog`) and runtime builds share the code
path, as the whole catalog does.

**Teardown is in the sum because a switch runs it.** `_switch_via_reconciler` powers off the
outgoing-only devices before activating (graceful, the canonical path's only mode —
`scenario_proxy.activate` calls `switch_scenario` with the default); the worst outgoing
scenario is the one whose exclusive devices confirm slowest. The REST door's `graceful:
false` (power off every outgoing device, then re-power the shared ones) is NOT bounded by the
published number — the shared devices' power-offs are additional to the cold activation that
re-powers them. That door is the UI's, not the catalog's; its bound is
`Σ off-gates(involved(O)) + activation_ceiling(S)` (today at most 29 000 + 36 500), documented
here and not published.

### 4.4 The numbers — today's living-room fleet

Computed 2026-10-06 by running the two overrides through the real planners on `config/`
(topology, scenarios, resolved capability maps). Gates: TV power 8 s / input 3 s, processor
power 6 s per zone / input 3 s, Apple TV power 5 s, streamer power 25 s, amplifier power 4 s
(IR, delay) / input 0.5 s, LD/VHS/Zappiti power 1 s, upscaler input 0.5 s; ordering delays
`processor.input → video.power` 5 s, `ld_player|vhs_player.power → upscaler.input` 4.5 s.

**Cold activation plans** (the `COLD` variant; order = `_order`'s output):

| Scenario | Steps (pre-delay + gate, ms) | `activation_ceiling` |
|---|---|---|
| `movie_appletv` | appletv power 5000 · tv power 8000 · amp power 4000 · amp input 500 · processor power z1 6000 · processor power z2 6000 · tv input 3000 · processor input 3000 | **35 500** |
| `movie_ld` | ld power 1000 · tv power 8000 · amp power 4000 · amp input 500 · processor power z1 6000 · tv input 3000 · processor input 3000 · upscaler input 4500+500 | **30 500** |
| `movie_vhs` | tv power 8000 · amp power 4000 · amp input 500 · processor power z1 6000 · tv input 3000 · processor input 3000 · vhs power 1000 · upscaler input 4500+500 | **30 500** |
| `movie_zappiti` | tv power 8000 · amp power 4000 · amp input 500 · processor power z1 6000 · z2 6000 · tv input 3000 · processor input 3000 · video power 5000+1000 | **36 500** |
| `music_auralic` | amp power 4000 · amp input 500 · streamer power 25000 | **29 500** |
| `music_reel` | amp power 4000 · amp input 500 (the A77 has no reconciled power) | **4 500** |
| `music_tape` | amp power 4000 · amp input 500 (manual: Dodocus → TAPE) | **4 500** |
| `music_turntable` | amp power 4000 · amp input 500 (manual: Sugden on, Dodocus → LP) | **4 500** |
| `tv_on_speakers` | tv power 8000 · amp power 4000 · amp input 500 · processor power z1 6000 · z2 6000 · tv input (arc) 3000 · processor input (arc) 3000 | **30 500** |

**Teardown ceilings** (the `HOT` variant, max over outgoing): every movie scenario and
`tv_on_speakers` inherit **25 000** from `music_auralic` (the streamer's 25 s power-off poll);
every music scenario inherits **25 000** from `movie_appletv` (Apple TV 5000 + TV 8000 +
processor z1 6000 + z2 6000). Per-device power-off gates: appletv 5000, ld 1000, tv 8000, amp
4000, processor 6000 per zone, streamer 25000, vhs 1000, video 1000.

**Published `max_duration_ms`:**

| Value | teardown | activation | **`max_duration_ms`** |
|---|---|---|---|
| `movie_appletv` | 25 000 | 35 500 | **60 500** |
| `movie_ld` | 25 000 | 30 500 | **55 500** |
| `movie_vhs` | 25 000 | 30 500 | **55 500** |
| `movie_zappiti` | 25 000 | 36 500 | **61 500** |
| `music_auralic` | 25 000 | 29 500 | **54 500** |
| `music_reel` | 25 000 | 4 500 | **29 500** |
| `music_tape` | 25 000 | 4 500 | **29 500** |
| `music_turntable` | 25 000 | 4 500 | **29 500** |
| `tv_on_speakers` | 25 000 | 30 500 | **55 500** |
| `none` (deactivate) | — | — | **29 000** (= max of `movie_appletv` 29 000, `music_auralic` 29 000, `movie_ld` 25 000, `movie_zappiti` 25 000, `tv_on_speakers` 24 000) |

The council's "~30 s for the cold movie scenario" was the activation half; the switch a
consumer actually requests can also pay for the outgoing scenario, which is why the published
number carries both.

### 4.5 What the sum does not see — recorded for the owner (not decided here)

`plan_ceiling` counts the bridge's *waiting between* steps. One thing happens *inside* a
step's dispatch and is invisible to the plan: the eMotiva driver's silence-while-busy hold
(DRV-39, `infrastructure/devices/emotiva_xmc2/driver.py:83-89, 592-647`). After any
power/input transition the driver holds the NEXT control-port command until the device is
quiet for 2 s, capped at `INPUT_READY_TIMEOUT_S = 15 s` from the transition, and the step
gate's clock starts only after dispatch returns ("the hold never eats the gate budget"). The
cap is measured from the arming transition, so the gates that elapse in between count toward
it; on today's plans the extra is at most **15 s per scenario** (`movie_zappiti`: zone-2 power
held ≤ 9 s after zone 1's 6 s gate, then `set_input` held ≤ 6 s after the TV-input gate — the
two bounds sum to the cap; music-from-movie teardowns: zone-2 power-off held ≤ 9 s). Nothing
else in the fleet holds inside dispatch; `DISPATCH_TIMEOUT_S = 60 s` is a hang guard, not
pacing.

So the decided sum is a ceiling on the plan's gates and a near-ceiling on wall time. Two
honest treatments, for the owner to choose at the cut (VWB-46 carries the question):

- **(a) Publish the plan sum and say so** — the guide text below already words the promise as
  "the bridge's own confirmation windows and settle delays"; a consumer adds its usual
  margin (round-2's × 1.25 + 2 s absorbs 15 s on every movie value).
- **(b) Declare the hold** — `CapabilityGate` gains `dispatch_ms: int = 0` ("the longest a
  single dispatch of this capability may block before its gate starts"), `plan_ceiling` adds
  it per step, and `EMotivaXMC2.json` declares `15000` on `input` and `power`. Mechanical and
  config-tunable, but it over-counts (three held steps → +45 s on `movie_zappiti`, a 106 s
  ceiling) because the real cap is shared across steps. A per-step exact rule would couple the
  derivation to the driver's exemption logic; not proposed.

This design recommends **(a)** and words the guide accordingly; it does not decide.

## 5. The guide section — verbatim text for `catalog-contract.md`

To be inserted after "Localization (since contract v1.11)" and before "Versioning", at the
`catalog-v1.11.0` cut. No task, no file outside the pin folder, no current version named.

```markdown
## Timing (since contract v1.11)

What a consumer may expect to wait for a confirmed action, published beside the action it
applies to, so that request timeouts are sized from the catalog and never from a
conversation.

- **`confirm_timeout_ms` on a capability** is the longest the bridge itself waits for the
  device to confirm an action on that capability before it reports failure. It is present
  only where the device confirms slowly — an air conditioner that reads back on its packet
  cadence, a streamer waking from standby, a television's power; absent means the default
  window of 500 ms. A request that waits for confirmation (`wait: true`, the default)
  returns within this bound plus transport: a confirmed state, or a failure saying the
  device never confirmed. A consumer sizes its request timeout above the value, never
  below. The value is a promise about the bridge's own waiting, not a measurement of the
  device: a confirmed action usually returns well before it.
- **`max_duration_ms` on a scenario value** — in the scenario manager's `scenario`
  `set(value)` table and its `scenario` field — is the ceiling for activating that
  scenario from any state of its room. The bridge executes a switch as one sequential
  chain: it powers down what the outgoing scenario no longer needs, then brings the
  incoming scenario's devices up in topology order, confirming or settling each step
  before the next; the value is the worst-case sum of those confirmation windows and
  settle delays. It is never exceeded by the bridge's own waiting and it is usually beaten
  by a wide margin — a warm switch between scenarios that share devices takes seconds. On
  the field's `none` entry the value is the ceiling for deactivating the room (powering
  its active scenario down). A manual step the scenario needs from a person is not timed.
- **Where the numbers come from.** Both are derived from the bridge's configuration — each
  capability's confirmation gate, the topology's settle delays, the scenario definitions —
  and never typed by hand. When a gate is retuned the catalog's content hash moves and a
  running consumer re-fetches; the contract does not change. The numbers describe how long
  the bridge is prepared to wait, not how fast a device is.
```

## 6. Consumer guidance

- **Voice sizes every request from the field; the configured constant is the fallback** for
  a capability that carries none (round-2 decision 8: `confirm_timeout_ms × 1.25 + 2 s`; for
  a scenario value, the same rule on `max_duration_ms`). With today's numbers that is 20.75 s
  for an HVAC command, 33.25 s for the streamer's power, 78 s for `movie_zappiti`; the flat
  20 s voice uses today is below every movie ceiling, which is the double-run DRV-29-class
  failure the lock in tier 3 closes from the other side.
- **Speech policy is voice's, decided in round 2** — acknowledge-then-confirm above ~3 s, no
  optimistic success speech, one flag to silence the acknowledgement. This design prescribes
  nothing about speech; it only guarantees the two numbers the policy keys off.
- **`wait: false`** stays what it is (fire-and-return-current-state); nothing here changes
  the endpoint's behaviour, only what it tells the consumer about itself.
- **The UI's HvacPanel** may show an honest expectation from `confirm_timeout_ms` — a separate
  later UI task by decision; nothing in VWB-46 touches a component.
- **Satellite descriptors** already carry `timing.confirm_latency_ms` (device-integration
  convention) → `gate.poll_timeout_ms` at load (DRV-36 design §b) → `confirm_timeout_ms` in
  the catalog by this rule, with no further mapping.

## 7. Tier 3 is out of this design — where it continues

The async-job pattern (`202 Accepted` + step events + completion, dissolving the timeout
question for composites) is **not designed here**. The owner's round-2 answer makes it its
own arc with its own cut: **VWB-47** (bridge job-API design) → voice durable-job design →
implementation both sides → the WB7 sitting that measures real switch and stop times (today's
figures are ceilings) → `catalog-v1.12.0` → the second voice re-pin; the one-cut condition is
waived for tier 3 only. Shape riders already accepted and recorded on VWB-47: one job per
room with `409` on a second request (the in-flight lock this design found missing), no cancel
in the minimum, device-level long actions stay synchronous under tier 1, step events over SSE
(new — the scenarios channel today carries only switched/shutdown), `GET /scenario/jobs/{id}`
for pollers and the UI, the UI stepper later. Tier 2's `max_duration_ms` stays meaningful
under tier 3 — it bounds the job, not the request.

## 8. Contract impact summary (for VWB-46)

- `openapi.json`: `CatalogCapability.confirm_timeout_ms?`, `CatalogValueLabel.max_duration_ms?`
  — additive; `backend/openapi.json` + `ui/src/types/openapi.gen.ts` + the workbench plugin's
  types regenerate.
- Golden: 27 capabilities gain `confirm_timeout_ms`; 10 scenario value entries (9 + `none`)
  gain `max_duration_ms`; content hash moves.
- Guide: the "Timing" section above; `test_guide_holds_the_normative_text…` gains `## Timing`.
- Tests: tier-1 equality (`confirm_timeout_ms == gate.poll_timeout_ms` for every gated
  capability, absent otherwise) and a derivation test on a fixture topology (two scenarios
  sharing a device, one exclusive slow device, one ordering delay) pinning teardown + activation
  + `none`; the golden's living-room numbers are pinned by the drift test as always.
- MINOR cut `catalog-v1.11.0`; `re-pin owed: voice, commons`.
