# Device-integration — the convention for Locveil-built devices

This directory holds the contract between the bridge and **firmware Locveil owns** —
today the ESP32 satellite family. The bridge owns and versions the convention; device
repositories pin a copy and author conforming descriptors against it. The
[registry one level up](../README.md) indexes every contract this repo owns or consumes;
the org-wide rules live in the Locveil contract convention
(`locveil-commons/process/contracts.md`).

**The convention itself is [`convention.md`](convention.md)** — who must conform, the
`wb-mqtt-v1` wire profile, the REST URL conventions, the descriptor and its example, and
the versioning and pinning rules. It is part of the versioned artifact set and travels
with every pinned copy. This README is not: it is the owner-side index and history, and
may change without a contract version.

## Files

| File | Pinned | What it is |
|---|---|---|
| [`convention.md`](convention.md) | yes | The normative text. Hand-written; an edit is a contract cut. |
| [`device-descriptor.schema.json`](device-descriptor.schema.json) | yes | The machine schema every device descriptor validates against (JSON Schema, draft 2020-12). |
| [`example.descriptor.json`](example.descriptor.json) | yes | A committed conforming descriptor — the same document `convention.md` shows as its example. |
| [`STAMP.json`](STAMP.json) | yes (always travels with the set) | The version stamp: `contract`, `version`, `tag`, `date`, `owner_repo`, the `artifacts` list naming the pinned files above, plus the profile and reserved-REST summaries. Hand-written. |
| `README.md` | no | This file. Deliberately outside the pinned set: inside a consumer's pin folder the name `README.md` belongs to the consumer. |

## Version history

The versioning *rule* is in [`convention.md`](convention.md#versioning--conformance).
This is the narrative record; the stamp and the tag are the machine-readable authority.

- **v1** — the first cut: the `wb-mqtt-v1` profile, the REST URL conventions, the
  descriptor schema.
- **v1.1** — no surface change. The stamp took the org-wide core shape and began
  enumerating the artifact files, giving device repositories a clean tag to pin.
- **v1.2.0** — the normative text moved out of this README into the new pinned
  `convention.md`, and this README left the pinned set (a minor: the set gained a file).
  Versions gained a third level and tags became three-part from this cut on; the
  versioning section was rewritten to match, states that the wire carries the major
  version only, and says a pin copies the whole set. The vocabulary cross-reference now
  points at the catalog contract's guide. Schema and example fixture byte-identical.

## Cutting a version

The stamp is hand-written. In ONE commit: edit the artifact(s), set `version`, `tag` and
`date` in `STAMP.json` (and `artifacts` if the set changed), add a history line above for
a minor or major cut; then tag that commit `device-integration-v<version>` and push the
commit and the tag together. The repo's contract checks fail a pinned file whose bytes
differ from the stamp's tag with no version move.

## Guard

`backend/tests/unit/test_device_integration_schema.py`, in the normal backend test job:
the schema is valid, the committed example validates against it, the example shown in
`convention.md` is the committed fixture, the stamp's version triple agrees, and the
pinned set keeps the shape a consumer's flat pin folder needs.
