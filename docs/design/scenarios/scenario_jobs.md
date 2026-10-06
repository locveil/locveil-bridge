# Scenario jobs — the tier-3 async switch API (VWB-47 design)

**Status: DESIGN — council-decided scope, implementation pending** (board PROD-18 round 2,
decisions 9–10, owner paste 2026-10-06; bridge keeper position taken as the starting point).
Implementation: **SCN-19** (this document is its spec). Voice's durable-job design is reviewed
against this document before SCN-19 starts; the two must meet at §8. Cut: **`catalog-v1.12.0`**
(minor, additive; CORE-12 stays at 1.13.0). Siblings: [`../confirmation_timing.md`](../confirmation_timing.md)
(tiers 1–2, `max_duration_ms` — the ceiling a job is bounded by) and the as-built scenario
spec [`scenario_system_redesign.md`](scenario_system_redesign.md) §7 (the reconciler this
design wraps, unchanged).

## 1. Why this arc exists

A scenario switch is awaited end-to-end by every caller today — `scenario.set(value)` on the
canonical endpoint, `POST /scenario/switch|start|shutdown`, the WB card — through
`ScenarioManager.switch_scenario` → two `execute_plan` runs (`domain/scenarios/service.py:289`).
A cold movie start costs up to 61.5 s of the bridge's own waiting (tier 2's table). Voice's
request timeout is 20 s. When it fires, voice retries, and **nothing in the bridge stops the
second switch from running on top of the first**: there is no per-room lock, the two plans
interleave at every `await`, and the eMotiva's silence-while-busy hold (DRV-39) is the only
thing between that and a wedged processor. The lock is the strongest reason for this arc;
the job API is how a consumer gets the answer without holding a socket open for a minute.

What this design does NOT change: the reconciler, the plan order (REL-3's ARC choreography —
TV power → processor power → TV input → processor input — is `_order`'s output and is
executed by the same `execute_plan`), the gates, the `scenario_switched` /
`scenario_shutdown` events' existing fields, `wait: true` callers' results, device-level
long actions (tier 1 keeps them synchronous).

## 2. Decisions this design executes

| # | Decision (board PROD-18, round 2) | Where |
|---|---|---|
| 9 | Build now, own arc: bridge job-API design → voice durable-job design (both reviewed) → implementation both sides → WB7 sitting (measures real switch/stop times) → `catalog-v1.12.0` → the second voice re-pin. One-cut condition waived for tier 3 only. | §11 |
| 10 | One job per room; a second request while one runs → `409`. | §4 |
| 10 | No cancel in the minimum — «stop» is a new job. | §3, §4 |
| 10 | Device-level long actions stay synchronous under tier 1. | §4.4 |
| 10 | Voice consumes step events over SSE (new adapter); `GET /scenario/jobs/{id}` for pollers and the UI; the UI progress stepper is a later task. | §5, §6 |
| keeper | `202 + {job_id, max_duration_ms}`; `scenario_step` events at the two reconciler points on the EXISTING `/events/scenarios` channel; terminal event extended with `job_id` + failures; auth orthogonal to PROD-4; REL-3 ARC ordering must not regress. | §3, §5, §9 |

## 3. The job

A **job** is one run of the room's switch chain: a *switch* (to scenario S, from whatever is
active) or a *stop* (deactivate the room). It is created at the domain chokepoint —
`ScenarioManager.switch_scenario` / `deactivate` — so every entry path produces one: the
canonical endpoint (voice, the UI), the REST routers, the WB scenario card, and SCN-11's
`force_reconcile` (a one-device forced plan; `kind: "reconcile"`).

**Lifecycle.** `running` → `succeeded` | `failed`. No `queued` (a contended request is refused,
never queued), no `cancelled` (no cancel in the minimum). A job is `failed` when any step
failed or was not confirmed; the switch still completes (today's `abort_on_failure=False`
semantics — the room's active scenario moves regardless, as it does now) so `failed` means
"done, with these devices not where the plan wanted them", never "nothing happened".

**The record** (`domain/scenarios/jobs.py`, `ScenarioJob`), served by `GET /scenario/jobs/{id}`:

```json
{
  "job_id": "j-living_room-20261006T153112Z-7f3a",
  "room_id": "living_room",
  "kind": "switch",
  "target": "movie_zappiti",
  "from": "music_auralic",
  "source": "canonical",
  "state": "running",
  "max_duration_ms": 61500,
  "started_at": "2026-10-06T15:31:12.418Z",
  "finished_at": null,
  "duration_ms": null,
  "phases": [
    {
      "phase": "teardown",
      "steps": [
        {"index": 0, "device_id": "streamer", "domain": "power", "target": "off",
         "command": "power_off", "zone": null, "feedback": true, "poll_timeout_ms": 25000,
         "delay_ms": 0, "pre_delay_ms": 0,
         "status": "done", "started_at": "…", "finished_at": "…", "error": null}
      ]
    },
    {
      "phase": "activation",
      "steps": [
        {"index": 0, "device_id": "living_room_tv", "domain": "power", "target": "on",
         "command": "power_on", "zone": null, "feedback": true, "poll_timeout_ms": 8000,
         "delay_ms": 0, "pre_delay_ms": 0, "status": "running", "started_at": "…",
         "finished_at": null, "error": null},
        {"index": 1, "device_id": "mf_amplifier", "domain": "power", "target": "on",
         "command": "power", "zone": null, "feedback": false, "poll_timeout_ms": null,
         "delay_ms": 4000, "pre_delay_ms": 0, "status": "pending", "started_at": null,
         "finished_at": null, "error": null}
      ],
      "manual_steps": []
    }
  ],
  "failures": [],
  "powered_off": ["streamer"],
  "result_scenario": null
}
```

- `kind`: `switch` | `stop` | `reconcile`. `target`: the scenario id, or `none` for a stop.
  `from`: the room's active scenario at acceptance (`none` if idle). `source`: `canonical` |
  `rest` | `wb_card` (who asked — the UI and voice both use `canonical`).
- `phases` appear **as they are planned**, which is exactly when they are planned today: the
  teardown phase at acceptance (its plan is built from believed state before anything runs),
  the activation phase after the teardown finished (its plan is built then, as
  `_switch_via_reconciler` does now — building it earlier would be a behaviour change for
  `graceful: false`, where the teardown changes the state the activation diffs against). A
  stop has one phase, `teardown`. A reconcile has one phase, `activation` (the forced plan).
- Step `status`: `pending` → `running` → `done` | `failed` (dispatch returned an error or
  raised, incl. the SCN-17 dispatch timeout) | `not_confirmed` (dispatched and acked, the
  feedback gate timed out — SCN-14's "a gate timeout is a failure"). `error` is the executor's
  string, verbatim.
- `failures` mirrors `switch_scenario`'s today (`{device, command, error}` per failed or
  unconfirmed step). `powered_off` is the teardown's device list. `result_scenario` is the
  room's active scenario id after the job (`none` after a stop), set at the terminal event.
- `duration_ms` is wall time from acceptance to the terminal event — the measurement the WB7
  sitting compares with `max_duration_ms` (§10).

**Where it lives, how long.** In memory, in a per-room registry on the `ScenarioManager`:
the running job plus the **last 20 finished jobs per room** (a ring; older ones are
forgotten). Nothing is persisted — a job is a transient run, not state; the room's active
scenario is the state and it is persisted as today. **After a bridge restart every job id is
unknown**: `GET /scenario/jobs/{id}` → `404 job_unknown`. That is the honest answer — the
bridge cannot know whether a chain it did not finish left the gear anywhere in particular;
the consumer's recovery is `GET /scenario/state` (what is active) and, if it must, a new job.

**Job ids** are opaque strings; the shape above (`j-<room>-<utc stamp>-<4 hex>`) is for
humans reading logs and is not a contract (a consumer never parses one).

## 4. The lock

**Scope: one `asyncio.Lock` per room, held by a job from acceptance to its terminal event.**
Rooms are the concurrency unit already (another room's active scenario is untouched by a
switch); the lock makes that true for *in-flight* switches too. It is acquired **non-blocking**:
a request that finds the room's lock held is refused with `409` — it is never queued, so a
voice retry can never line up a second run behind the first.

**What the lock covers** (every path that runs a plan in a room):

| Path | Under the lock | On contention |
|---|---|---|
| canonical `scenario.set(value)` / `scenario.off` (voice, UI, WB card via the adapter) | yes | `409 job_in_progress` |
| `POST /scenario/switch`, `/start`, `/shutdown` (REST) | yes | `409 job_in_progress` |
| `POST /scenario/{id}/force_reconcile` (SCN-11) | yes — a one-device forced plan is a plan | `409 job_in_progress` |
| `GET /scenario/{id}/reconcile_preview` | no — read-only planning | — |
| `POST /scenario/role_action`, inherited-domain canonical actions (volume, playback…) | **no** — device-level, synchronous under tier 1 by decision | — |
| `POST /reload` | waits (blocking acquire of every room lock, bounded by the largest `max_duration_ms` + 5 s; on timeout the reload proceeds and logs) — a reload mid-chain would rebuild scenario cards under a moving active scenario | — |
| startup restore (`_restore_state`) | no actuation today (tracking only) — nothing to lock | — |

**Idempotency under the lock.** Requests are compared to the room's *state*, not to the
running job:

- `set(S)` when S is already the room's active scenario and no job runs → `200`, `no_op:
  true`, no job created (today's "already active" short-circuit, now explicit).
- `set(S)` while a job to S is running → `409 job_in_progress` (the owner's rule: a second
  request is refused — the body names the running job so the caller can *join* it by
  subscribing; the idempotent-join alternative, "return 202 with the same job id", was
  considered and set aside by decision 10).
- `off` / `set(none)` when the room is idle and no job runs → `200`, `no_op: true`.
- `off` while a job runs → `409`. «Stop» during a switch is therefore refused, not queued:
  voice's "still working" answer (§8). A stop after the job's terminal event is a new job.
- `start`/`switch`/`shutdown` on REST follow the same table; `shutdown` of a scenario that
  is not the active one stays `409` with today's message (a different 409 — see §5.1).

**`graceful: false`** (REST only; the UI's door, never the catalog's): the same job machinery
— the teardown phase lists *every* outgoing device, and the activation phase, planned after
it, re-powers the shared ones. Its ceiling is not the published `max_duration_ms` (tier 2
documents that); the job's `max_duration_ms` field carries the published number anyway, and
the `graceful: false` job may exceed it. Documented, not fixed: the door is operator-only.

**CORE-12's staged writes** do not interact: staging validates an overlay and stores a
proposal; promotion is a human commit plus `update.sh`, i.e. a restart, after which every
job is forgotten (§3). No live config mutates under a running job.

**The UI's synchronous waits** are unchanged: the scenario page's `wait: true` canonical call
still returns when the chain ends. What changes for the UI is the `409` it can now receive
when two clients race (today the second call silently double-runs); the page shows "a switch
is already running" and follows the room's job via SSE or `GET /scenario/jobs/{id}`. The
progress stepper is the later UI task (decision 10).

**Not held across process shutdown.** The lifespan's shutdown cancels the request tasks;
the executor stops at a step boundary; the job never reaches a terminal event; the SSE
closes (the manager's shutdown signal). §6 tells the consumer what that means.

## 5. The API — exact shapes

### 5.1 Canonical endpoint (the consumer surface)

`POST /devices/scenario_manager_{room}/canonical` — request unchanged
(`CanonicalActionRequest`): `{"capability": "scenario", "action": "set", "params":
{"value": "movie_zappiti"}, "wait": false}`.

| Case | Status | Body |
|---|---|---|
| `wait: true` (default) — the chain ran to its end | `200` | `CanonicalActionResponse` exactly as today, plus `job_id` inside `state`: `{"success": true, "device_id": "scenario_manager_living_room", "capability": "scenario", "action": "set", "state": {"scenario": "movie_zappiti", "job_id": "j-…", "powered_off": ["streamer"], "failures": []}, "error": null, "no_op": false}`. `success` is `false` when `failures` is non-empty (today's rule). |
| `wait: false` — accepted | **`202`** | `{"success": true, "device_id": "scenario_manager_living_room", "capability": "scenario", "action": "set", "state": {"scenario": "music_auralic", "job_id": "j-…", "max_duration_ms": 61500}, "error": null, "no_op": false}` — `state.scenario` is the room's scenario **at acceptance** (the chain has not run); `max_duration_ms` is tier 2's published ceiling for the target (`none`'s for a stop). |
| already at target, no job running | `200` | as today with `no_op: true`; no job, no event. |
| a job is running in the room | **`409`** | `{"success": false, "device_id": "…", "capability": "scenario", "action": "set", "state": null, "error": {"code": "job_in_progress", "message": "a scenario job is running in room 'living_room' (job j-…, switch → movie_zappiti)", "job_id": "j-…"}}` |
| unknown scenario / wrong room / unbound role | `404` / `400` / `409` | unchanged (`unknown_scenario`, `scenario_room_mismatch`, `no_active_scenario`, …). |

Schema changes: `CanonicalErrorCode` gains `JOB_IN_PROGRESS = "job_in_progress"`;
`CanonicalError` gains `job_id: Optional[str]` (set for `job_in_progress` only). The
`wait` field's description gains the sentence "On a Scenario Manager's `scenario`
capability `wait: false` returns `202` with a job to follow (since contract v1.12)." —
today `wait` is silently ignored on that capability; the change is additive (a `wait:
false` caller today gets the `200` after the chain; from v1.12 it gets the `202`, which is
what `wait: false` has always meant everywhere else).

### 5.2 REST routers (UI / operator)

`POST /scenario/switch` `{"id": "movie_zappiti", "graceful": true, "wait": true}`,
`POST /scenario/start` `{"id": "…", "wait": true}`, `POST /scenario/shutdown` `{"id": "…",
"graceful": true, "wait": true}` — each gains `wait: bool = true`.

| Case | Status | Body |
|---|---|---|
| `wait: true` | `200` | `ScenarioResponse` as today + `"job_id": "j-…"`. |
| `wait: false` | `202` | `ScenarioJobAccepted`: `{"job_id": "j-…", "room_id": "living_room", "kind": "switch", "target": "movie_zappiti", "max_duration_ms": 61500}` |
| job running | `409` | `{"detail": {"code": "job_in_progress", "room_id": "living_room", "job_id": "j-…", "kind": "switch", "target": "movie_zappiti"}}` |
| shutdown of a non-active scenario | `409` | unchanged: `{"detail": "Cannot shutdown scenario 'x': scenario 'y' is currently active in room 'r'"}` (a string, as today — the two 409s are told apart by the body's shape; the REST door is not the catalog's surface). |

`POST /scenario/{id}/force_reconcile` keeps its synchronous response, gains `job_id`, and
returns the same `409 job_in_progress` body on contention.

### 5.3 Jobs

`GET /scenario/jobs/{job_id}` → `200` the record of §3 (`ScenarioJob`), or `404`
`{"detail": {"code": "job_unknown", "job_id": "j-…"}}` — including every id from before the
last restart.

`GET /scenario/jobs?room={room_id}` → `200` `{"room_id": "living_room", "running": <ScenarioJob
| null>, "recent": [<ScenarioJob>, …]}` — the running job and the ring of finished ones,
newest first. Without `room`: `{"rooms": {"living_room": {…}, …}}`. For pollers and for the
UI after a page reload ("is something running in my room?").

### 5.4 Events — `GET /events/scenarios` (the existing channel)

The transport is the existing SSE manager: one `data:` line per event carrying JSON with the
event type embedded as `eventType`, an `id:` of epoch milliseconds, a `connected` event on
subscribe, a `keepalive` roughly every second of silence. Job events are **additional event
types on this channel**; nothing existing is removed or renamed.

```
data: {"eventType":"scenario_job_started","job_id":"j-living_room-20261006T153112Z-7f3a","room_id":"living_room","kind":"switch","target":"movie_zappiti","from":"music_auralic","source":"canonical","max_duration_ms":61500,"timestamp":"2026-10-06T15:31:12.418Z"}

data: {"eventType":"scenario_phase","job_id":"j-…","room_id":"living_room","phase":"teardown","steps":[{"index":0,"device_id":"streamer","domain":"power","target":"off","command":"power_off","zone":null,"feedback":true,"poll_timeout_ms":25000,"delay_ms":0,"pre_delay_ms":0}],"manual_steps":[],"timestamp":"…"}

data: {"eventType":"scenario_step","job_id":"j-…","room_id":"living_room","phase":"teardown","index":0,"device_id":"streamer","domain":"power","target":"off","command":"power_off","zone":null,"status":"started","error":null,"elapsed_ms":2,"timestamp":"…"}

data: {"eventType":"scenario_step","job_id":"j-…","room_id":"living_room","phase":"teardown","index":0,"device_id":"streamer","domain":"power","target":"off","command":"power_off","zone":null,"status":"done","error":null,"elapsed_ms":3410,"timestamp":"…"}

data: {"eventType":"scenario_phase","job_id":"j-…","room_id":"living_room","phase":"activation","steps":[…8 steps…],"manual_steps":[],"timestamp":"…"}

data: {"eventType":"scenario_step", … "phase":"activation","index":3,"device_id":"processor","domain":"input","target":"source1","status":"not_confirmed","error":"gate timeout: input did not reach 'source1' within 3000ms (device reported state never confirmed)","elapsed_ms":28904, …}

data: {"eventType":"scenario_switched","scenario_id":"movie_zappiti","room_id":"living_room","timestamp":"…","state":{…ScenarioState as today…},"job_id":"j-…","job_state":"failed","duration_ms":41377,"failures":[{"device":"processor","command":"set_input","error":"gate timeout: …"}],"powered_off":["streamer"]}
```

- **`scenario_job_started`** — once per job, emitted inside the request before any device
  is touched (and before the `202`/`200` is written).
- **`scenario_phase`** — once per phase, when the phase is planned, listing its steps with
  their gates (the force-reconcile dialog's `ReconcilePlanStep` shape, plus `index` and
  `zone`) and the manual steps the plan needs from a person. A consumer that wants "step 3
  of 8" reads `len(steps)`; a consumer that only wants the end ignores it.
- **`scenario_step`** — twice per step, at the executor's two points: `started` just before
  dispatch (after the step's pre-delay), and one of `done` / `failed` / `not_confirmed`
  after the gate. `elapsed_ms` is since job acceptance.
- **The terminal event is the existing `scenario_switched` (a switch or a reconcile) or
  `scenario_shutdown` (a stop)**, extended with `job_id`, `job_state` (`succeeded` |
  `failed`), `duration_ms`, `failures`, `powered_off`. Their existing fields are byte-for-byte
  what they are today, so the scenario page's current subscriber keeps working unchanged.
  A terminal event without `job_id` is still possible from one producer: the startup
  restore's tracking notification — consumers filter on `job_id` and ignore it.
- `timestamp` is the bridge's wall clock, ISO-8601 UTC.

### 5.5 OpenAPI

Additive. New component schemas: `ScenarioJob`, `ScenarioJobPhase`, `ScenarioJobStep`,
`ScenarioJobAccepted`, `ScenarioJobsResponse`; the event payloads
`ScenarioJobStartedEvent`, `ScenarioPhaseEvent`, `ScenarioStepEvent`,
`ScenarioSwitchedEvent`, `ScenarioShutdownEvent` registered through `OPENAPI_EXTRA_MODELS`
(SSE has no operation to hang them on — the same mechanism the device-state models use), so
a consumer's generated types cover the stream. Changed: `CanonicalErrorCode` (+1 value),
`CanonicalError` (+`job_id`), the three REST request models (+`wait`), `ScenarioResponse` /
`ForceReconcileResponse` (+`job_id`), two new operations under `/scenario/jobs`. Nothing
removed, nothing re-typed → **minor**.

## 6. What a consumer may rely on (the event contract)

1. **Every job emits `scenario_job_started` first and exactly one terminal event last**, on
   the same channel, in that order; between them `scenario_phase` precedes the
   `scenario_step` events of its phase, and each step's `started` precedes its outcome.
   Events of one job never interleave out of execution order on one connection (the
   manager's per-connection FIFO).
2. **No duplicates, no replay.** The channel does not honour `Last-Event-ID`; a reconnect
   starts a fresh stream. The truth after a reconnect is `GET /scenario/jobs/{id}`, which
   carries every step's status — the stream is a view of the record, not the record.
3. **The first event races the HTTP response.** A consumer that subscribed *before* sending
   the request misses nothing. One that subscribes after reading the `202` may have missed
   `scenario_job_started` and early steps; it reconciles with one `GET`. Voice subscribes
   first (§8).
4. **The terminal event always arrives, or the stream ends.** The bridge never leaves a job
   `running` while alive: the executor's exceptions are caught per step, the terminal
   notification runs after the chain regardless of failures. The only way to see no terminal
   event is the bridge going away — then the SSE closes (the manager's shutdown path) and
   keepalives stop; a consumer whose stream dies treats the job as lost (restart → `404
   job_unknown`) and speaks accordingly.
5. **Timing.** `scenario_job_started` is emitted within the request; the first `scenario_step`
   follows within the first step's `pre_delay_ms` (0 on every first step today). The
   terminal event arrives within the job's `max_duration_ms` **of the bridge's own waiting**
   (tier 2's promise; dispatch holds are outside it — the DRV-39 ruling); a consumer that
   wants a watchdog uses `max_duration_ms × 1.25 + 2 s` (round-2's sizing rule) and, when it
   fires with the stream still alive, `GET`s the job rather than assuming.
6. **Keepalives are not progress.** `keepalive` events say the connection lives, nothing
   about the job.
7. **Slow consumers are dropped, not waited for.** A connection whose queue is full is
   closed by the bridge (the consumer reconnects and `GET`s) — an implementation change in
   the SSE manager (today's `await queue.put` would block the executor on a stalled
   subscriber, i.e. a slow voice box could pace the house; SCN-19 replaces it with a
   non-blocking put that discards the connection).

## 7. Failure reporting

Three distinct answers, each at its level:

- **Refused** (never started): `409 job_in_progress`, `404 unknown_scenario`, `400
  scenario_room_mismatch` — no job, no event.
- **Step failed inside a job**: `scenario_step` with `status: failed` (dispatch error, driver
  exception, SCN-17 dispatch timeout, eMotiva DRV-39 fail-closed refusal) or `not_confirmed`
  (feedback gate timed out); the chain continues (today's semantics). The terminal event's
  `job_state: failed` and `failures[]` summarise; `GET /scenario/jobs/{id}` has every step.
- **Lost**: no terminal event, stream closed → the bridge died or was restarted; `GET` →
  `404 job_unknown`; `GET /scenario/state` says what is active now.

A `wait: true` caller sees the same facts folded into the synchronous response (`success`,
`failures`), as today.

## 8. Where voice meets this design (what voice said it will build, and what it may rely on)

Voice's round-2 position, folded in: it launches the job as a **durable action** (`wait:
false`, keeps the `job_id`), **subscribes to `/events/scenarios` by `job_id`** through a new
SSE adapter, **re-subscribes on its own restart**, treats a job it **cannot find after a
bridge restart as a spoken failure**, maps a **second user command mid-job to "still
working"**, and «stop» to a **new job**. Against this design:

| Voice behaviour | Relies on |
|---|---|
| Launch: `POST …/canonical {scenario, set, {value}, wait:false}` → read `state.job_id`, `state.max_duration_ms`. | §5.1 `202` shape; `max_duration_ms` equals the catalog's published value for the target (tier 2). |
| Subscribe before launching; filter the stream on `job_id`. | §6.1–§6.3 ordering; §5.4 field names: every job event carries `job_id` and `room_id`; the terminal event carries `job_id`, `job_state`, `failures`, `duration_ms`. |
| Speak the acknowledgement on the `202` (round-2 decision 8, behind its flag), the confirmation or failure on the terminal event. | §6.4 — the terminal event always arrives or the stream ends. |
| Watchdog: `max_duration_ms × 1.25 + 2 s`; on expiry with the stream alive → `GET /scenario/jobs/{id}`, speak from `state`. | §6.5; §5.3. |
| Second command for the same room mid-job → "still working" (locally, no request) — or send it and map `409 job_in_progress` to the same phrase. | §4 idempotency table; §5.1 `409` body carries `error.job_id`. |
| «Stop» → a new job (`scenario.off`, `wait:false`) once the running job is terminal; mid-job it is the `409` above. | §4; `none`'s `max_duration_ms` (29 000 today) bounds it. |
| Voice restart mid-job: re-subscribe, `GET /scenario/jobs/{id}` for the ids it remembers; `404 job_unknown` → the bridge restarted too → spoken failure + `GET /scenario/state`. | §3 lifetime; §5.3. |
| Reading `scenario_step` for narration is optional; phrases key off `device_id` + `status` only (device names come from the catalog's `names`, never from the event). | §5.4 — events carry ids, not words (the language-data convention: nouns live in the catalog). |

**Open points for voice** (to settle in its design review, not here): (a) whether it
narrates steps at all in the first cut or only acknowledgement + terminal; (b) how many
remembered `job_id`s it reconciles after its own restart (one per room is enough); (c)
whether a `409` mid-job is answered locally or by sending anyway — the bridge supports both;
(d) the adapter's reconnect backoff — the bridge sends keepalives every ~1 s, so a 5 s
silence is a dead stream.

## 9. Contract classification and the guide

**Minor, additive**, cut as `catalog-v1.12.0`: new operations, new schemas, new event types,
one new error code, optional fields added; no field removed or re-typed; `wait: false` on
the scenario capability changes from ignored to honoured (additive — the field has always
meant "do not wait"). **Golden impact: none** — the golden carries no job data and no
version; its content hash does not move (the cut is a STAMP + guide + openapi change; the
golden is byte-identical and re-enumerated). `CONTRACT_VERSION` → `"1.12.0"`; CORE-12 keeps
`1.13.0`.

The guide gains this section, verbatim, after "Timing (since contract v1.11)":

```markdown
## Jobs (since contract v1.12)

A scenario switch is a chain of device steps that can take most of a minute. A consumer
that does not want to hold a request open for that long starts it as a job and follows it.

- **Starting a job.** `scenario.set(value)` or `scenario.off` on a room's scenario manager
  with `wait: false` returns `202` and, in `state`, a `job_id` and the `max_duration_ms`
  of the target. With `wait: true` (the default) the same request returns when the chain
  has finished, as before, and `state` also names the `job_id`. A request that finds the
  room already at the target returns `200` with `no_op: true` and starts nothing.
- **One job per room.** While a job runs in a room, every further scenario request for
  that room is refused with `409` and the error code `job_in_progress`; the error names
  the running `job_id`. Nothing is queued: a repeated request never runs the chain twice,
  and a stop during a switch is a refusal, not an interruption. There is no cancel — a
  stop is a new job, started after the running one has finished.
- **Following a job.** `GET /scenario/jobs/{job_id}` returns the job: its phases, every
  step with its status, failures, and the result; after a bridge restart every earlier
  job is unknown (`404`, `job_unknown`) — the bridge does not pretend to know what a chain
  it did not finish left behind; `GET /scenario/state` says what is active now. The
  scenarios event stream carries the same facts as they happen: `scenario_job_started`,
  `scenario_phase` (a phase's planned steps), `scenario_step` (a step starting, then
  `done`, `failed` or `not_confirmed`), and the terminal `scenario_switched` or
  `scenario_shutdown`, which carries the `job_id`, the job's state, its duration and its
  failures. Every job emits its start first and exactly one terminal event last, in
  execution order; the stream is not replayed on reconnect — the job record is the truth.
- **What the events are not.** They carry device ids and canonical values, never words:
  a consumer that narrates a step takes the device's name from the catalog. Device-level
  actions (a volume step, a power command on one device) are not jobs; they confirm within
  the capability's `confirm_timeout_ms` as before.
```

## 10. Test plan and the WB7 sitting

**Unit / integration (SCN-19, before the cut):**

1. **Lock concurrency against REL-3's ordering.** Two concurrent `switch_scenario` calls
   in `living_room` (fakes with gated feedback, the real topology): the second gets
   `job_in_progress` **before** the first dispatches its second step; the first's executed
   order is byte-for-byte today's `test_movie_appletv_ordering_matches_manual_sequence`
   expectation (TV power → processor power → TV input → processor input). A concurrent
   switch in another room proceeds (rooms are independent).
2. **Event ordering.** A fake publisher records the sequence for a switch with one
   not-confirmed step: `started` → `phase(teardown)` → steps → `phase(activation)` → steps
   (each `started` before its outcome) → one `scenario_switched` with `job_state: failed`,
   `failures` naming the step; no event after the terminal one; `GET` after the fact shows
   the same statuses.
3. **The 409 path**, on every door: canonical (`error.code`, `error.job_id`), REST
   switch/start/shutdown (`detail.code`), force_reconcile; and the idempotency table of §4
   (already-active → `200 no_op`, idle `off` → `200 no_op`).
4. **The `wait` matrix on the canonical endpoint:** `wait: true` body unchanged except
   `state.job_id`; `wait: false` → `202`, `state.scenario` is the pre-switch value,
   `max_duration_ms` equals the golden's number for the target; the job then runs to a
   terminal event.
5. **Restart path.** A fresh `ScenarioManager` answers `404 job_unknown` for any id; the
   registry ring keeps the last 20 per room and forgets the 21st.
6. **Slow consumer.** A subscriber that never drains is disconnected; the executor's step
   timings are unaffected (the plan finishes in the same wall time as with no subscriber).
7. **Reload under a job.** `POST /reload` waits for the running job, then proceeds; it
   proceeds after the bound if the job wedges (a step hung at `DISPATCH_TIMEOUT_S`).
8. **Contract.** `test_contracts_golden`: golden byte-identical to v1.11.0's; openapi gains
   the schemas; STAMP `1.12.0`; the guide holds `## Jobs`.

**The WB7 sitting (the owner; after both implementations are deployed, before the cut):**
measures the real numbers tier 2 only bounds — "today's figures are ceilings". For each
living-room scenario: a **cold start** (room idle, every device off at the wall-state the
owner normally leaves it in), a **warm switch** from each of two neighbours (one movie →
movie, one music → movie or the reverse), and a **stop**; read `duration_ms` off the terminal
event (or `GET /scenario/jobs/{id}`), compare with `max_duration_ms`, note any step that was
`failed`/`not_confirmed`. The results land in SCN-19's DONE entry as a table and, if a
published ceiling is ever *exceeded* by the bridge's own waiting, that is a bug in the
derivation (a gate the plan did not count) and blocks the cut; if it is merely approached,
nothing changes — the number is a ceiling.

**One-page checklist for the owner** (copy into the sitting's notes; each line is one
request and one number):

```
WB7 SITTING — scenario jobs (SCN-19)            date: ________   bridge: ________  voice: ________

Preparation
[ ] bridge + voice redeployed with the job build; `GET /scenario/jobs?room=living_room` → {"running": null}
[ ] one terminal open on `curl -N http://<bridge host:port>/events/scenarios` (watch the events arrive)
[ ] room idle: `GET /scenario/state` shows no active scenario; streamer in standby, TV off, amp off

Cold starts (room idle → scenario; by voice, «включи …»)        published   measured   steps failed
[ ] movie_appletv  ..........................................  60 500 ms  ________   ________
[ ] movie_zappiti  ..........................................  61 500 ms  ________   ________
[ ] movie_ld (switch the Dodocus when asked) ...............  55 500 ms  ________   ________
[ ] movie_vhs ..............................................  55 500 ms  ________   ________
[ ] music_auralic ..........................................  54 500 ms  ________   ________
[ ] tv_on_speakers .........................................  55 500 ms  ________   ________
[ ] music_reel / music_tape / music_turntable (one of them)   29 500 ms  ________   ________
    (stop the room between cold starts; wait for the streamer to be in standby again)

Warm switches (from the scenario named → to the next; by voice)
[ ] music_auralic → movie_zappiti ..........................  61 500 ms  ________   ________
[ ] movie_zappiti → movie_appletv ..........................  60 500 ms  ________   ________
[ ] movie_appletv → music_auralic ..........................  54 500 ms  ________   ________
[ ] movie_ld → movie_vhs ...................................  55 500 ms  ________   ________

Stops («выключи …» with a scenario running)
[ ] stop from movie_appletv ................................  29 000 ms  ________   ________
[ ] stop from music_auralic (streamer → standby) ...........  29 000 ms  ________   ________

Behaviour (tick = as designed)
[ ] voice acknowledges at once («включаю …»), confirms at the end; with the flag off, only the end
[ ] a second command mid-switch → "still working" (and the bridge logged a 409)
[ ] «stop» mid-switch → refused as "still working"; after the end → a new job that powers down
[ ] the scenario page shows the switch (no double-run visible on the processor / no wedge)
[ ] voice restart mid-switch → it re-subscribes and still confirms the end (or reports the job)
[ ] bridge restart mid-switch → voice reports a failure; `GET /scenario/jobs/<id>` → 404 job_unknown

Hand back: this page + the SSE terminal's saved output. Any measured > published = blocks the cut.
```

## 11. Sequencing

1. **This design reviewed; voice's durable-job design written and reviewed against §8**
   (the two meet on: the `202` shape, the event field names, "terminal always arrives or the
   stream ends", the `409` body).
2. **SCN-19 — bridge implementation** (domain `jobs.py` + the per-room lock + executor step
   hooks + the SSE producers in bootstrap + the routers + schemas + the SSE manager's
   non-blocking put + tests 1–8). Hexagonal: the job registry and lock live in the domain
   (`ScenarioManager`), events leave through the existing observer/publisher port, the
   presentation layer only maps shapes — zero new import-linter exceptions. No contract cut
   yet; `CONTRACT_VERSION` unchanged until step 5.
3. **Voice implementation** (its SSE adapter, the durable action, the flag).
4. **The WB7 sitting** (§10) — both deployed; the table lands in SCN-19's DONE entry.
5. **Cut `catalog-v1.12.0`** — `CONTRACT_VERSION "1.12.0"`, guide §9 verbatim, openapi +
   STAMP + tag; golden byte-identical; README history row; `re-pin owed: voice (both
   copies), commons`.
6. **The second voice re-pin.**

CORE-12's reserved version stays `catalog-v1.13.0`. The UI stepper (reads `scenario_phase` +
`scenario_step` for the room's running job, shows "step 3 of 8 — processor: input →
source1") is filed as its own UI task when SCN-19 lands.

## 12. Considered and set aside

- **Idempotent join** (`set(S)` during a job to S returns `202` with the same `job_id`):
  friendlier to naive retries, but decision 10 says a second request is refused; the `409`
  body carrying the job id gives a caller the join anyway.
- **A dedicated `scenario_job_finished` event** instead of extending `scenario_switched` /
  `scenario_shutdown`: two terminal events for one fact, and the scenario page's existing
  subscriber would have to learn a new one; extending the existing events keeps one
  terminal fact and changes nothing for today's subscriber.
- **Persisting jobs** so a `GET` answers after a restart: the bridge cannot vouch for a
  chain it did not finish; a persisted `running` record would be a lie and a persisted
  `lost` record adds nothing `404` does not say.
- **Planning both phases at acceptance** so the record is complete from the first event:
  changes behaviour for `graceful: false` and gains only an earlier "N steps" count; the
  phase event carries the count when the phase is planned.
- **Cancel**: out of the minimum by decision; the executor already stops at step boundaries,
  so a later `DELETE /scenario/jobs/{id}` is a small addition when wanted.
