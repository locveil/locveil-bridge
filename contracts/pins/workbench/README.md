# workbench — consumed pin

The Locveil Workbench **plugin contract**, owned by `locveil-commons`
(`packages/workbench/`, tags `workbench-vX.Y.Z`). This folder is the pin: the owner's
enumerated set byte-identical — the contract types (`contract.ts`) and the two machine
schemas (`manifest-fragment.schema.json`, `runtime-config.schema.json`) — the owner's
`STAMP.json` verbatim, and the strict `PIN.json` recording the tag and the hashes.

The consumer is this repo's Workbench plugin, [`workbench-plugin/`](../../../workbench-plugin/).
What the pin locks:

- **The emitted manifest.** The plugin build writes `dist/manifest.json` as a pure
  merge of `workbench-plugin/manifest.fragment.json` and the `version` in its
  `package.json`. `backend/tests/unit/test_workbench_pin.py` validates that merge
  against the pinned `manifest-fragment.schema.json` without running the build; the
  plugin's CI job then checks the built file equals the same merge.
- **The contract types, by statement.** The plugin still compiles against the commons
  package through a `file:` link, so the types it builds with are whatever that
  checkout holds. The pin records which version the plugin was verified against; the
  plugin's CI job warns when the linked files differ from the pinned bytes.

`runtime-config.schema.json` is pinned because a pin is always the owner's whole set;
this repo emits no runtime config — the shell's deployment does.

Never hand-edit any of this. A new owner version means a deliberate re-pin with the
vendored tool (`python3 scripts/repin.py workbench`), then make the plugin and the
conformance test pass in the same change.
