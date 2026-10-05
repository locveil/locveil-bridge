# Bridge UI surfaces under the Workbench split (UI-17)

**Status: design, 2026-07-14 (UI-17); §2.1 amended 2026-07-15 per council HK-11
(runtime assembly).** The bridge-side rendering of the PROD-24 shell
council. Normative upstream, in precedence order: the **PROD-24 entry** in
`../locveil-commons/board/BOARD.md` (the decision record) → commons
**`docs/design/workbench.md`** (the deploy-split design) → this document (bridge
specifics only — the Bridge plugin, the staged-write API shape, the operations
consequences). On disagreement the upstream wins. Design only: implementation is
gated per §7; **no write API ships before PROD-4's auth decision lands** (binding
condition, on record).

---

## 1. The classification, consumed

The council classified every bridge surface (workbench.md §2). This repo's rendering:

| bridge surface | class | consequence here |
|---|---|---|
| `ui/` (remote, runtime layouts) + appliance/room pages (`docs/planned/appliance-pages.md`) | **operations** | Stays controller-served exactly as today. The "admin route / auth shell" scope all four planned pages declared is **deleted from operations** — `ui/` never grows an admin route or auth; the Workbench answers it once. |
| device-setup + IR learning (`docs/planned/device-setup.md`) | **workbench** | Page of the **Bridge plugin**; IR learning is a sidebar entry under it (§2.2). |
| topology-setup (`docs/planned/topology-setup.md`) | **workbench** | Page of the Bridge plugin. Its "live vs file edit mode" open question is **answered: staging** (§3). |
| voice-setup (`docs/planned/voice-setup.md`) | **workbench** | Page of the Bridge plugin — it is a bridge-backend surface despite the name; it stays under the Bridge tab, not Voice. |

The four planned pages remain the page-level designs (panes, flows, validators,
importer rules). This document changes only their *chassis* (where they run) and
their *write path* (§3); it does not re-litigate their content.

## 2. The Bridge plugin

### 2.1 Package home and build

- **New top-level `workbench-plugin/`** — a sibling of `ui/`, not inside it. `ui/`
  stays the operations SPA with its own deploy lifecycle (nginx container on the WB7);
  the plugin is workstation-side code with a different consumer (the commons shell)
  and a different release cadence. Working package name:
  `@locveil/bridge-workbench-plugin`.
- **Build: vite 6 library mode** → a **single-file ESM entry** in `dist/` (one
  `entry` for the shell's dynamic import) with the **HK-11 singleton set external**:
  `react`, `react-dom(/client)`, `react/jsx-runtime`, `react-router-dom` (**pinned
  major 6** — the bridge ships 6.30.4 today) and `locveil-ui-kit` resolve through
  the shell's import map; everything else bundles into the plugin (i18n is a
  plugin-local instance driven by the shell's locale signal). CSS is emitted as
  files listed in the fragment's `styles[]` — the shell owns inject/remove and
  loads the single Tailwind preflight, so the plugin build disables preflight.
  The build also **emits the manifest fragment** `dist/manifest.json`
  (`{id, version, entry, styles[], peers{}, backendCompat?}`, schema versioned
  `workbench-vX`) — build-emitted by this repo, never hand-written into commons.
  Reference shape: the shell's in-tree demo plugin
  (`../locveil-commons/packages/workbench/demo-plugin` + `scripts/build-demo.mjs`).
  The toolchain is the one OPS-13 just landed (eslint-9 flat config as the shared
  target; vite majors are per-consumer — bridge is on 6, and per HK-11 the boundary
  is runtime ESM, bundler-blind: tools are never contractual). The flat-config file
  is shared or mirrored from `ui/eslint.config.js`; drift between the two is a
  lint-config bug.
- **Generated API types are embedded.** The plugin runs its own
  `gen:api-types` against `backend/openapi.json` and ships the generated types inside
  `dist/` — the openapi generator stays repo-side and the shell never sees a schema
  (`cross-repo-source-of-truth`; workbench.md §4 rules).
- **Consumption — RUNTIME ASSEMBLY (amended by council HK-11, 2026-07-15; supersedes
  the build-time `file:` dependency this bullet originally specified).** The shell
  dynamically imports the plugin's **built** `dist/` at runtime (native ESM behind
  the shell-served import map — never TS sources); the shell's owner-edited config
  lists the **location only** (dev-phase: the sibling `dist/` path). The descriptor
  compiles against **`locveil-workbench/contract`** (the shipped shell's types-only
  export — since UI-23 resolved by the plugin's tsconfig to the pinned copy at
  `contracts/pins/workbench/contract.ts`, not to an installed package). **Peer mismatch = strict refuse-and-surface**: a plugin whose `peers`
  disagree with the shell's singleton majors does not load, and the shell names the
  disagreement — never warn-and-load. Final distribution (published URLs, pinning)
  stays deferred to the productization step (workbench.md §4).
- **Components**: built on ui-kit tokens/components once `ui-kit-v1` exists (PROD-10).
  Until then the plugin uses minimal local primitives; the restyle-on-ui-kit debt is
  accepted and tracked by the adoption plan (§7).
- **No imports from `ui/`.** Anything genuinely shared (the generated API types, small
  pure helpers) is duplicated or generated per-package until ui-kit provides a home;
  a bridge-local shared package is explicitly NOT created for two consumers.

### 2.2 The plugin descriptor (contract v1 mapping)

Against the workbench.md §4 shape:

| contract surface | bridge v1 |
|---|---|
| `id` / `title` | `"bridge"` / localized (ru/en). |
| `pages()` | Static list (runtime-registrable is legal per contract; the bridge doesn't need it in v1): `device-setup` (sidebar children: *Import*, *IR learning*), `topology`, `voice`. |
| `i18n` | Plugin ships its own **RU/EN** bundles; the shell provides the active locale. Device-sourced strings (localized names in configs and the catalog) keep their own locale set — `de` data survives untouched; the *chrome* contract is RU/EN. |
| `status()` | The per-plugin status slot: reachability + health of the bridge backend, fed from `GET /system` (MQTT connection state, device counts) + the current `GET /system/catalog` version hash. Light poll in v1; an SSE upgrade is an implementation option, not a contract need. |
| `reportHook` | Delegates the chrome bug button to the bridge's live pin-validated `POST /reports`, with the active plugin/page carried in the app-context field the report schema already has. |
| `verbs` (per page) | Every config-writing verb is **dormant with the named gate `PROD-4-auth`** — rendered disabled with its gate, never hidden (contract honesty rule). Everything else is live from day one (§2.3). |
| `gate` (plugin-level) | None — the Bridge plugin itself is not dormant. |
| `backendTarget` | One target class for all pages: the **bridge controller API** (the FastAPI backend on the WB7). The target address is workbench-level configuration; heterogeneous per-page targets are allowed by the contract but unused here. |

### 2.3 The v1 cut: read-only pages before PROD-4

The binding condition gates **config-writing endpoints** — not reads, not pure
computation, not the action surfaces the bridge already serves. That yields a useful
v1 with zero new attack surface:

- **device-setup**: the importer's *propose* step is a compute endpoint (parse
  `wb-webui.conf` content → typed proposals + lint results; writes nothing) — allowed.
  The conf source question from the planned page is answered for the dev phase:
  **(c) UI-side paste/upload** of the file content — no new secret, no new trust
  boundary. The controller-side helper (option b) stays a later option; SSH from the
  backend (option a) is rejected — it would put a controller credential inside the
  bridge process ahead of PROD-4's secrets posture.
- **topology-setup**: palette and graph read from existing surfaces plus a new
  *read* endpoint for `config/topology.json`; the path preview is a dry
  `resolve_targets` compute endpoint — all allowed. Only *save* stages (§3).
- **voice-setup**: read-only by design (`/voice/status` family is a read surface);
  the test-utterance pane fires the **existing** `POST /devices/{id}/canonical` — an
  action API that already ships, so the page is just a new client of it.
- **IR learning**: *capture* drives existing device-action surfaces (Broadlink /
  WB IR) — live hardware actions, already served; **saving** captured codes into a
  device config is a staged write and waits with the rest. The planned page's
  overwrite-protection concern is answered structurally: staging never merges — a
  proposal for an existing target starts from the live file's content and the page
  diffs before staging (§3.1).

## 3. The staged-write API — shape only; shipping is PROD-4-gated

The dev-phase write model (workbench.md §5; normative home PROD-4 item 4): the bridge
config tree is repo-owned and canonical (`config-master-tree`), so **no page ever
writes it**. Pages write staged proposals; promotion is a human commit.

### 3.1 Model

- **Store**: `data/staged-config/` — inside the data mount, which is the only writable
  volume the container has (`config/` is mounted `:ro`, so the no-live-write rule is
  enforced by the mount, not by convention).
- **One proposal per target path.** An envelope file per target:
  `{target, base_sha256, content, staged_at, note}` — `target` is the repo-relative
  config path (`config/devices/wb-devices/<room>/<id>.json`, `config/topology.json`);
  `base_sha256` is the live file's hash at stage time (`null` for a new file).
  Re-staging the same target replaces the envelope.
- **Lifecycle**: stage → (re-stage replaces) → **promote = human**: apply the proposal
  to a repo checkout, review the diff, commit; `update.sh`'s one-way sync ships it to
  the runtime tree unchanged. The bridge **self-cleans**: a proposal whose `content`
  hash equals the live file's current hash is cleared on next listing (promotion
  landed). A proposal whose `base_sha256` no longer matches the live file is surfaced
  as **stale/conflict** — never merged, never silently overwritten; the operator
  re-stages from current.
- `"Apply"` in every page means *stage*. Running config is immutable through this API;
  `POST /reload` reads `config/` only and is unchanged.

### 3.2 Endpoints (sketch — implementation refines names, not semantics)

| endpoint | semantics |
|---|---|
| `GET /staged` | List proposals with target, staleness flag, timestamps. |
| `GET /staged/{target}` | The proposal + a diff against the live file + staleness. |
| `PUT /staged/{target}` | Validate, then stage. Validation = overlay the proposal on the live config tree and run the **existing load-time validation** (the Pydantic config models plus the topology/scenario structural checks) so cross-file breakage is caught at stage time, not at the next reload. Schema/cross-ref failures → 422 with the model errors. |
| `DELETE /staged/{target}` | Discard the proposal. |

### 3.3 Hexagonal placement

Thin presentation router → an **app-layer staging service** (wired in `bootstrap.py`
like every other composition) → a new **infrastructure staged-store adapter** over
`data/staged-config/`; validation reuses the existing config-load machinery. No new
cross-layer imports; the import-linter contract set stays 6/6 with **zero new
exceptions** (the `/reload` residual remains the only documented one). Staged
proposals are an operations concern, not domain entities — no new domain port unless
implementation genuinely finds the need, and that would be a design amendment, not a
drive-by.

### 3.4 The auth slot

The router ships only after PROD-4's auth decision, mounted behind whatever guard it
prescribes. Code may land earlier behind a disabled feature flag, but the endpoints
must not be reachable until then — that is the binding condition, verbatim.

## 4. Operations consequences

- `ui/` is untouched by this design: no admin route, no auth shell, no new pages. The
  appliance/room pages roadmap stays operations scope as written.
- The four planned pages are updated in the same change as this design: the shared
  "Admin route / auth shell" rows re-point to the Workbench; device-setup and
  topology-setup carry the staging write path ("Apply stages via the controller API;
  promotion is a commit"); topology-setup's live-vs-file open question is closed as
  *staging*; the three flow diagrams gain the staged hop.

## 5. Non-goals (bridge rendering of workbench.md §9)

The final write/distribution conventions (productization step); the shell-level
reporter fallback (PROD-19); the auth posture itself (PROD-4); anything satellite
(DES-5 / first light); ui-kit component boundaries (PROD-10 consumes this design, not
the reverse); re-litigating the planned pages' pane-level content.

## 6. Follow-up tasks (filed at UI-17 completion, `design-then-implement`)

- **UI-18** — the Bridge Workbench plugin package: `workbench-plugin/` skeleton +
  the §2.3 read-only v1 cut. Gated on the commons workbench shell existing to consume
  it (the shell and the first two plugins co-develop; PROD-10's "no framework before
  two real plugins" rule); ui-kit restyle rides `ui-kit-v1` when it lands.
- **CORE-12** — the staged-write API (§3). Design may be refined during
  implementation; **endpoints unreachable until PROD-4's auth decision lands**.

## 7. References

Board `PROD-24` (decision record) · commons `docs/design/workbench.md` (the split) ·
`docs/planned/{device-setup,topology-setup,voice-setup,appliance-pages}.md` (page
designs) · `docs/wb_device_authoring_log.md` (importer rules source of truth) ·
`docs/design/scenarios/scenario_system_redesign.md` §4 (topology schema) ·
`docs/design/problem_reports_bridge.md` (the reportHook backend) · PROD-4 item (4)
(write convention + auth posture) · PROD-10 (ui-kit) · PROD-19 (reporter fallback).
