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
- **The contract types, by compilation.** The plugin imports its types from
  `locveil-workbench/contract`, and `workbench-plugin/tsconfig.json` maps that
  specifier to this folder's `contract.ts` — the pinned bytes are the ones the
  type-check reads. The plugin has no `locveil-workbench` package among its
  dependencies: the imports are type-only and erased by the build, so nothing is
  needed at runtime, and a commons checkout cannot supply the types by accident. A
  change to the contract in commons reaches the plugin only through a re-pin. The
  same test file holds the arrangement in place (the mapping points here, the
  dependency stays absent, every import of the contract is type-only), and the
  plugin's CI job asks the compiler which files it read: this `contract.ts` must be
  among them, and nothing from the commons checkout except the UI kit.

`contract.ts` imports React's types. It sits outside any `node_modules`, so the same
tsconfig maps `react` to the plugin's own `@types/react`.

The UI kit (`locveil-ui-kit`) is a different kind of surface and is not part of this
pin: a package the shell provides at runtime, linked from a commons checkout at build
time. The plugin README describes it.

`runtime-config.schema.json` is pinned because a pin is always the owner's whole set;
this repo emits no runtime config — the shell's deployment does.

Never hand-edit any of this. A new owner version means a deliberate re-pin with the
vendored tool (`python3 scripts/repin.py workbench`), then make the plugin and the
conformance test pass in the same change — the plugin type-checks against the new
bytes at once, so a breaking contract change shows up there as type errors.
