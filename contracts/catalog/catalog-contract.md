# The catalog contract — normative guide

What a consumer of the bridge's device catalog may rely on. The bridge is the generator
and source of truth; a consumer holds a pinned copy of the whole artifact set and builds
against it. This guide is one of the versioned artifacts: it travels with every pinned
copy, beside the files it describes, and its text changes only together with a contract
version (see [Versioning](#versioning)).

## The artifact set

| File | What it is |
|---|---|
| `catalog.golden.json` | The golden catalog sample — one full house exactly as `GET /system/catalog` serves it: rooms, devices, capabilities, action param descriptors, value tables. |
| `openapi.json` | The API schema of record — `CatalogResponse`, the canonical action request and response shapes, and the problem-report evidence shape (`EvidenceEnvelope`, returned by `GET /reports/evidence`). |
| `catalog-contract.md` | This guide — the semantics the two machine artifacts cannot state on their own. |
| `STAMP.json` | The version stamp. It travels with the set and names the version and tag the other files belong to. |

The golden is a **sample**, not a fixture of every house: it shows the shapes and the
canonical vocabulary on a real configuration. A consumer validates its parsing against
the shapes; it must not assume another house carries the same devices.

## Param semantics (since contract v1.1)

- **`unit`** on a param is the semantic unit of the value (`°C`, `%`, `dB`, `min`) —
  what a voice consumer needs to parse «поставь двадцать два градуса» against a
  °C-shaped target. Constraints (min/max/type) always come from the same native spec
  the driver enforces.
- **`values`** carries the `{wire, canonical, labels}` table when the choice set is
  **bridge-known and static** (e.g. the scenario enum — labels are localized, ru/en).
  Since contract v1.3 this includes action params whose choice set lives on a
  same-named read-side field (the HVAC `set_mode(mode)` / `set_fan(fan)` family):
  the param mirrors the field's table, so «кондиционер на охлаждение» validates
  against the same triplets the state reads back. The canonical param name always
  equals the field name — that correspondence is the rule, not a coincidence.
- **`options_from`** marks an **intentionally open set**: the choices are
  runtime-dynamic (installed apps change with every install) and enumerable via
  `GET /devices/{id}/options/<options_from>`. A param carries *either* `values` *or*
  `options_from`, never both — an open set frozen into the golden would drift.
- **Selection capabilities advertise `set`** (since contract v1.2): a capability
  that switches between options (`input` on TVs, amps, streamers) carries a `set`
  action with one required `value` param. Devices with a **closed** option set (one
  native command per input) embed it as static `values` — the consumer can validate
  «переключи на CD» without a round-trip; devices with a **runtime** set carry
  `options_from: "inputs"` instead. Same rule as above: either/or, never both.
- **No empty capability husks:** a capability with neither invocable actions nor
  readable fields is suppressed from the catalog. (The TVs' `input` was the one case
  — it carries a real `set` since contract v1.2 and is back in the catalog.)

## Localization (since contract v1.11)

Which catalog surfaces carry human language, in which locales, and what a consumer may
assume about them. Who contributes which words across the product — the nouns here, the
verbs in the voice assistant's own vocabulary — is the organisation's language-data
convention; this section is the machine rule that convention points at.

- **Locales.** Every localized surface carries `ru` and `en`. `de` is optional and other
  locales may appear; a consumer that wants a locale an entry lacks falls back to `ru`.
  Locale keys are lowercase two-letter language codes.
- **Localized surfaces.** Device `names` and room `names`; `aliases` — per locale, a list
  of spoken alternatives, authored `ru` first, never required and never complete; field
  `labels`; and the `labels` of every entry in a `values` table, on a field and on an
  action param alike — including the `set(value)` table of a selection capability.
- **The one exemption.** The `on` / `off` entries of a `power` field carry no labels:
  the words for power are a consumer's verbs, not catalog nouns.
- **Not localized, by design.** `canonical` and `wire` are identifiers. Capability names,
  action names, param names and `group` are identifiers too — a consumer never shows
  them as words or matches speech against them. A param's `description` is
  developer-facing English. A `unit` is a symbol (`°C`, `%`, `dB`, `min`), the same in
  every locale.
- **The guard.** The committed golden sample is checked against these rules on every
  change to the bridge: a configuration that breaks them cannot be regenerated into the
  sample. A consumer may therefore treat a missing `ru` or `en` on a localized surface as
  a defect to report, not a case to handle.

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

## Jobs (since contract v1.12)

A scenario switch is a chain of device steps that can take most of a minute. A consumer
that does not want to hold a request open for that long starts it as a job and follows it.

- **Starting a job.** `scenario.set(value)` or `scenario.off` on a room's scenario manager
  with `wait: false` returns `202` and, in `state`, a `job_id` and the `max_duration_ms`
  of the target — the `202` means the job was accepted, not that it is done. With
  `wait: true` (the default) the same request returns when the chain has finished, as
  before, and `state` also names the `job_id`. A request that finds the room already at
  the target returns `200` with `no_op: true` and starts nothing.
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

## Versioning

The contract is versioned as a whole, `MAJOR.MINOR.PATCH`. Two things carry a version
and they always move together: the git tag **`catalog-vMAJOR.MINOR.PATCH`** in the
bridge repository and the `version` / `tag` fields of `STAMP.json`. The stamp and the
tag are the machine-readable authority — no contract version exists that is not in a
stamp, and a tag's bytes are exactly the files the stamp enumerates.

**The three levels:**

| Level | Meaning | Typical cause |
|---|---|---|
| **major** | Breaking — a consumer built against the previous major can fail. | A field removed or re-typed, a canonical name changing meaning, an incompatible change to a documented behaviour. |
| **minor** | The surface changed, compatibly. | A new field, schema, capability rule or endpoint; a documented behaviour refined; the artifact set gaining a file. |
| **patch** | Bytes of an artifact moved; the surface did not. | The golden sample refreshed after a house-configuration change; an editorial fix to this guide or to a schema description. |

Every level is a new stamp and a new tag. What a trailing pin means follows from the
level: behind by a **patch** it is still right about the surface; behind by a **minor**
it is missing additions; behind by a **major** it is no longer safe to build against.

**The content hash is not a version.** `CatalogResponse.version` — stamped as
`catalog_version`, and published retained on the MQTT topic `bridge/catalog/version` —
is a deterministic hash of the catalog *content*. It moves whenever the house
configuration changes and tells a running consumer when to re-fetch. It says nothing
about the contract version, and the contract version is not served at runtime. When a
configuration change moves the committed golden sample, the hash inside it moves and
the contract cuts a **patch** — never a minor: the sample changed, the surface did not.

**Tag forms.** Tags are three-part from `catalog-v1.10.0` on. The earlier tags
(`catalog-v1.5` … `catalog-v1.9`) keep their two-part form and are never re-cut; mixed
forms order naturally (`v1.9` < `v1.10.0`). The lineage v1.1–v1.4 predates tagging: it
survives as the "since contract vX" notes in this guide, which name the version a rule
first appeared in.

## The stamp

`STAMP.json` carries the contract core — `contract`, `version`, `tag`, `date`,
`owner_repo` — the `artifacts` list (the files that make up the pinned set; the stamp
itself always travels with them), and a build record:

- **`bridge_commit`** / **`bridge_version`** — the bridge build the artifacts were
  generated *from*: the code a consumer was built against.
- **`catalog_version`** — the content hash of the golden sample beside it.

The two answer different questions — which code, which configuration — and neither
substitutes for the other.

## Pinning

Consumers pin one way, and pin the whole set: every file the stamp enumerates plus the
stamp, byte-identical, at a tag. A pinned copy is never edited by hand and never
refreshed automatically; it moves by a deliberate re-pin, after which the consumer's
conformance test proves its code still honors the surface. The bridge never writes into
a consumer's repository.
