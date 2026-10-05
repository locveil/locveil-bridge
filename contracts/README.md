# locveil-bridge — contract registry

The direction-labeled index required by the Locveil contract convention
(`locveil-commons/process/contracts.md` §2). Every contract this repo OWNS and every
pin it CONSUMES, one line each; details live in the per-contract READMEs. Layout is
the uniform org shape: `contracts/<name>/` owned, `contracts/pins/<name>/` consumed.
Version strings here always name the **current** stamp or pin tag — a contract's
history lives in its own README.

## Owned

| Contract | Consumers | Version authority |
|---|---|---|
| [`catalog`](catalog/README.md) — the Irene ↔ bridge read contract: golden catalog + pinned OpenAPI schema + the normative guide ([`catalog-contract.md`](catalog/catalog-contract.md)) | locveil-voice (pins a copy in its own repo and the crossover copy in `locveil-commons/contracts/pins/catalog/`) | `catalog/STAMP.json` + tags `catalog-vX.Y.Z` (history: the folder README) |
| [`device-integration`](device-integration/README.md) — how Locveil-built devices integrate with the bridge: the normative convention ([`convention.md`](device-integration/convention.md)) + descriptor schema + example descriptor | locveil-satellite (pins the convention, authors conforming descriptors) | `device-integration/STAMP.json` + tags `device-integration-vX.Y.Z` (current: `device-integration-v1.2.0`; history: the folder README) |

Cross-reference (a consumed process contract on the **block-pin lane**, not
relocated): the **scope kit** (`scope-vN`) — the pinned CLAUDE.md blocks and the
vendored `scripts/scope_guard.py`, enforced by the sha256 block rules in
`.scope-guard.toml`.

## Consumed (pins)

| Pin | Owner | Conformance guard |
|---|---|---|
| [`report-protocol`](pins/report-protocol/README.md) — the problem-report filing surface (labels, title prefix, report-id/bundle shape) | locveil-commons (tag `report-protocol-v1.0.1`) | `backend/tests/unit/test_report_protocol_pin.py` |
| [`docs-manifest-schema`](pins/docs-manifest-schema/README.md) — the org-wide schema this repo's docs manifest (`docs/manifest.json`, instance data) validates against | locveil-commons (tag `docs-manifest-schema-v1.0.0`) | `backend/tests/unit/test_docs_manifest.py` |
| [`workbench`](pins/workbench/README.md) — the Workbench plugin contract (contract types + manifest-fragment and runtime-config schemas) the Workbench plugin in `workbench-plugin/` is built for | locveil-commons (tag `workbench-v1.3.0`) | `backend/tests/unit/test_workbench_pin.py` |
| [`core-py`](pins/core-py/README.md) — the shared entry-point-group discovery engine (`DynamicLoader`), vendored as runtime code: the pinned artifact plus a byte-identical importable copy in `backend/` | locveil-commons (tag `core-py-v1.1`) | `backend/tests/unit/test_core_py_pin_identity.py` |

Layer-1 coherence (layout, stamps, pin hashes) is checked by the vendored
contract-guard; layer-2 conformance lives in the named tests above, inside the
normal backend suite. Pin staleness is watched by the vendored repin tool
(config: `.repin.toml`, which also records each vendored guard script's tag and
hash) — a warning at commit time; in CI a failure on a major gap, or on any gap when
the push touches that pin or its test; a minor-or-major gate before an image build.
A pin moves only by a deliberate re-pin with that tool, never by hand-edit or
auto-fetch.
