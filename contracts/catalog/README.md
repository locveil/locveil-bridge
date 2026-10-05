# The catalog contract — the Irene ↔ bridge artifacts

This directory is the **contract of record** between the bridge and its non-UI
consumers — first among them the `locveil-voice` assistant (Irene). The bridge is the
*generator and source of truth*: artifacts are committed **here** and never pushed
into a sibling repository. The voice side pins its own copies — one inside its own
repository, one in the shared crossover location
(`locveil-commons/contracts/pins/catalog/`) — a one-way, outward, version-stamped sync.
The [registry one level up](../README.md) indexes every contract this repo owns or
consumes; the org-wide rules live in the Locveil contract convention
(`locveil-commons/process/contracts.md`).

**What a consumer may rely on is written in [`catalog-contract.md`](catalog-contract.md)**
— param semantics, the versioning rule, the stamp's fields, how to pin. That guide is
part of the versioned artifact set and travels with every pinned copy. This README is
not: it is the owner-side index — history, the file list, and how to regenerate — and
may change without a contract version.

## Files

| File | Pinned | What it is |
|---|---|---|
| `catalog.golden.json` | yes | The golden catalog sample — the full house as `GET /system/catalog` serves it: rooms, devices (including the `global` aggregates and the per-room `scenario_manager_*` entities), capabilities with action **param descriptors** (typed `CatalogParam`: name/type/required/default/min/max/`unit`/`values`/`options_from` — the schema of record for param parsing) and `{wire, canonical, labels}` enum value tables. Generated offline and deterministically from `config/` — same projection code path as the live endpoint. |
| `openapi.json` | yes | The pinned API schema of record — carries `CatalogResponse`, the canonical action request/response shapes, and (since contract v1.4) the problem-report surface: `EvidenceEnvelope`, the shape `GET /reports/evidence` returns — the bridge-side evidence a voice-filed problem report embeds when the smart home is involved. Byte-identical to `backend/openapi.json` (the UI-consumed copy). |
| `catalog-contract.md` | yes | The normative guide (since contract v1.10.0) — the semantics the machine artifacts cannot state. Hand-written; an edit is a contract cut. |
| `STAMP.json` | yes (always travels with the set) | The version stamp: the contract core (`contract`, `version`, `tag`, `date`, `owner_repo` — since contract v1.5), the `artifacts` list naming the pinned files above, plus the build record — which bridge commit + version last generated these artifacts, and the golden's content-hash. The commit named is the build the artifacts were generated **from** (i.e. the parent of the commit that lands them). |
| `README.md` | no | This file. Deliberately outside the pinned set: inside a consumer's pin folder the name `README.md` belongs to the consumer. |

## Version history

The versioning *rule* — three levels, what each means for a pinned copy — is in the
[guide](catalog-contract.md#versioning). This is the narrative record of the cuts; the
stamp and the tag are the machine-readable authority.

- **v1.1–v1.4** predate tagging. They live on as the "since contract vX" notes in the
  guide — frozen history, not retro-tagged.
- **v1.5** — the first tag. No contract surface changed: the convention cut that gave
  the family its layout, stamp core, and tag.
- **v1.6** — the OpenAPI field descriptions rewritten reader-first; no structural
  change, no golden change. The stamp began enumerating the artifact set (`artifacts`)
  so a consumer's pin can be checked for completeness.
- **v1.7** — the backend import package renamed to `locveil_bridge`: the
  module-qualified names of the two `ManualInstructions` schema variants in
  `openapi.json` changed prefix accordingly — a schema-name rename, no field or
  structural change; golden byte-identical.
- **v1.8** — administrative: the stamp's `artifacts` enumeration moved to
  repo-root-relative paths. No schema, field, or golden change.
- **v1.9** — the canonical endpoint's error mapping refined: a reachability failure
  reported by the device handler itself now surfaces as `device_unreachable` (503),
  consistent with the echo-timeout path — previously such failures fell through to
  `internal_error` (500). The endpoint description documents the mapping; golden
  byte-identical.
- **v1.10.0** — the normative text moved out of this README into the new pinned guide
  `catalog-contract.md`, and this README left the pinned set (a minor: the set gained
  a file). Versions gained a third level and tags became three-part from this cut on;
  the versioning rule was rewritten to match. Golden and OpenAPI schema byte-identical.

## Regeneration

From the **repo root** — device cert paths in `config/` resolve relative to it (the same
way the container resolves them from its `/app` workdir):

```bash
uv run --project backend locveil-catalog --stamp contracts/catalog/STAMP.json
uv run --project backend locveil-openapi -o backend/openapi.json && cp backend/openapi.json contracts/catalog/openapi.json
```

`locveil-catalog` builds the catalog **offline** — typed configs + capability maps +
rooms + scenario definitions, no drivers, no network, no broker — so the dump is
deterministic (devices sorted by id; identical bytes across runs).

**Every regeneration that moves a pinned file is a contract cut.** The version lives
in code as the catalog projection's `CONTRACT_VERSION` constant and flows into the
stamp at regeneration, so the order is:

1. Bump `CONTRACT_VERSION` at the right level — see the guide's table. A house-config
   change that only refreshes the golden (its content hash moves, nothing else) is a
   **patch**; so is an editorial fix to the guide.
2. Regenerate with the commands above (the stamp picks up the new version and tag).
3. Add a line to the version history here for a minor or major cut.
4. Commit artifacts and stamp together, tag that commit `catalog-v<version>`, and push
   the commit and the tag together.

The repo's contract checks enforce this: a pinned file whose bytes differ from the
stamp's tag, with no version move, fails at commit.

## Drift guard

`backend/tests/unit/test_contracts_golden.py` regenerates both generated artifacts
inside the normal backend test job and fails if the committed copies are stale — any
config, capability-map, or API change that alters the contract without a re-dump
breaks CI with the one-command fix above. The same test holds the version triple
together (code constant, stamp, tag string) and the shape of the pinned set.

## Realism check

The bridge runs on the WB7 controller, and its live catalog has been verified a
byte-for-byte match against `catalog.golden.json` — so the *deployed* bridge serves
exactly what the repo says (no deployment drift). To re-check at any time, dump the
live catalog and diff it against the golden:

```bash
curl -s http://<wb7>:8000/system/catalog | diff - catalog.golden.json
```

An empty diff means the deployed bridge and the committed contract agree.
