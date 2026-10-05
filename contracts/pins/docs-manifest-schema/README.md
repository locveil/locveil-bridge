# docs-manifest-schema — consumed pin

The org-wide schema for a repository's docs manifest, owned by `locveil-commons`
(`process/user-docs/manifest.schema.json`, tags `docs-manifest-schema-vX.Y.Z`). This
folder is the pin: the owner's schema byte-identical, the owner's `STAMP.json`
verbatim, and the strict `PIN.json` recording the tag and the hashes.

This repo's manifest — [`docs/manifest.json`](../../../docs/manifest.json), the
machine-readable index of every user-facing document — is **instance data**: it is not
a contract and carries no stamp of its own. The contract is this pinned schema. The
coherence test `backend/tests/unit/test_docs_manifest.py` validates the manifest
against the pinned copy, so it runs without reaching across repositories, and keeps the
manifest in step with the documentation tree. The convention behind it:
`locveil-commons/process/user-docs.md`.

History: until October 2026 the manifest was stamped as a repo-internal contract of
its own (`contracts/docs-manifest/`, with a hand-copied schema beside it). That folder
is retired; the git tag `docs-manifest-v1` it produced stays in this repository as
frozen history and names nothing current.

Never hand-edit the pinned files. A new owner version means a deliberate re-pin with
the vendored tool (`python3 scripts/repin.py docs-manifest-schema`); if the schema
tightened, bring `docs/manifest.json` back into conformance in the same change.
