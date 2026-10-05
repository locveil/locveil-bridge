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
