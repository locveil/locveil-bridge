# Bridge Workbench plugin

The bridge's tab of the Locveil Workbench — the workstation-side admin shell that
hosts one plugin per product. This package builds a runtime-loadable ESM bundle the
shell discovers through `dist/manifest.json`; it is not served by the controller and
is not part of the deployed bridge.

## Build

```bash
npm install
npm run gen:api-types   # regenerate src/types/openapi.gen.ts from ../backend/openapi.json
npm run check           # typecheck + lint
npm run build           # → dist/index.js + dist/style.css + dist/manifest.json
```

The bundle externalizes the shared runtime set (react, react-dom, react-router-dom,
locveil-ui-kit) — the shell serves those through its import map. Everything else,
including the generated API types and the RU/EN strings, is bundled.

`dist/manifest.json` is not hand-written: the build merges
[`manifest.fragment.json`](manifest.fragment.json) (id, entry, styles, and the peer
majors the shell checks) with the `version` from `package.json`. Change the manifest by
editing that file. Its shape is held to the Workbench plugin contract this repo pins
at [`../contracts/pins/workbench/`](../contracts/pins/workbench/README.md) — the
backend test suite validates the merge against the pinned schema, and the build check
in CI confirms the emitted file is that same merge.

## Run it in the shell

From `../../locveil-commons/packages/workbench`: add this package's `dist/` path to
`workbench.config.json` and `npm run serve`. The Bridge tab appears with three pages:

- **Voice readiness** — catalog + bridge versions and a canonical-command test pane
  (fires `POST /devices/{id}/canonical`, the same path the voice assistant uses).
  Also hosts the controller address override (kept in browser storage) — an escape
  hatch over the normal source of the address: the shell's `workbench.config.json`
  `backends.api` entry. With neither set, the plugin falls back to
  `http://<page-host>:8000`.
- **Device setup** — the configured-device inventory (read-only).
- **Topology** — rooms and their devices (read-only).

Config-writing actions render disabled with their gate named (`PROD-4-auth`): writes
open up only after the auth decision lands, and "Apply" will stage a proposal rather
than touch the running config.
