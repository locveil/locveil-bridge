# Action Plan — Journal
> Older sections: docs/archive/journal/2026-07-05_2026-07-08.md
> Older sections: docs/archive/journal/2026-06-09_2026-07-04.md

**Status:** Living dated record of work done on the wb-mqtt-bridge monorepo. Extracted from
`docs/action_plan.md` §6 on 2026-06-06 — the plan was growing too quickly and the dated
history is intrinsically append-only, so it lives on its own from here. **Newest entries on
top.** References elsewhere in the plan ("see §6 (2026-05-25)" etc.) remain valid and now
point at the dated entry below.

`docs/action_plan.md` stays the master driving document (forward work + an index of recent
journal entries in §6). This file is the long tail.

**Archive pointer:** entries older than 2026-07-09 are frozen in
[`docs/archive/journal/`](archive/journal/) — newest archive
[`2026-07-05_2026-07-08.md`](archive/journal/2026-07-05_2026-07-08.md) (third rotation
2026-07-15 via `scope_guard.py --rotate`), then
[`2026-06-09_2026-07-04.md`](archive/journal/2026-06-09_2026-07-04.md) (second rotation
2026-07-11, the first via `scope_guard.py --rotate`), oldest
[`2026-05-23_2026-06-08.md`](archive/journal/2026-05-23_2026-06-08.md) (first rotation
2026-07-06, manual). Per the `one-active-journal` high-water rule; append-only, grep when a
`task-start-reconciliation` trail points there.

---

**Note on IDs (2026-06-30):** the ledger was re-IDed to the `PREFIX-N` workstream scheme (DOC-9). This
journal's **earlier dated entries keep their original positional refs** (`§P3.7 #19`, `§5.1 #7`, `P4 #7`,
etc.) — they are historical and resolve via [`action_plan_aliases.md`](action_plan_aliases.md). New
entries use the new IDs.

- **2026-08-04 (later) — CORE-1's `/reload` rack verify RAN: mechanics pass, and the verify
  earned its keep — two P1 findings filed (CORE-15/CORE-16).** `POST /reload` on the deployed
  image drove the full ReloadService sequence clean (reconnect, all 14 WB cards + the
  `scenario_manager_living_room` card rebuilt, catalog republished, no reload-path errors) —
  but the catalog came back `95e24c…`, not golden, and the diff told the real story: ALL 79
  devices' `capabilities` were EMPTY post-reload, canonical answered `capability_not_supported`
  fleet-wide (voice actuation dead until restart). Root cause **CORE-15**: `/reload` never
  re-runs `attach_capability_maps` after re-initializing the fleet — a pre-existing omission
  the CORE-1 verbatim extraction faithfully preserved (the OLD image produced the identical
  hash on 08-02; the earlier journal attribution of `95e24c…` to "old-image catalog code" was
  wrong — superseded here). Heal verified: container restart → golden + 157 capabilities.
  **CORE-16** fell out of the simulation prep: the boot-time connect loop gives up PERMANENTLY
  after 5 attempts (~50 s) — the 08-01 boot survived on attempt 5/5 by luck; 20 more seconds
  of network delay = MQTT dead with the healthcheck green (HTTP-only probe). The CORE-14
  lost-race simulation itself is PENDING: the harness declined to stop the production broker
  (correctly — whole-house disturbance is the owner's hand), choreography handed to the owner.
  CORE-1's close = owner's call; its one failing gate item re-verifies with CORE-15's fix.

- **2026-08-04 — the power-outage incident CLOSED: main deployed on the WB7, kitchen-hood
  rule verified live.** The owner deployed current main 2026-08-02 (~10 min after the
  dispatched image build went green) and confirmed the «Kitchen Light Switch Control» rule
  working; verified from the dev box 2026-08-04: `kitchen_hood/set_light` retained with full
  meta, clean `Rule triggered` firings with zero `unexisting control` errors since the deploy
  (surviving wb-rules' own midnight restart), and `bridge/catalog/version` back to the golden
  `5622ba7a1a78102a` — the `95e24c…` value seen after the 2026-08-02 old-image reload was that
  image's catalog code, not config drift. CORE-14's gate narrows to the race-fix verify (next
  cold boot, or the mosquitto-stop simulation at the owner's convenience); CORE-1's `/reload`
  rack verify is now unblocked (the ReloadService code is live — the 08-02 reload attempt ran
  on the old image and does not count). Side observation, not filed: wb-rules warns `Unknown
  metadata for device kitchen_hood: 'online'/'available'` at its restarts — cosmetic.

- **2026-08-02 — CORE-14 code half: WB cards publish on first connect (task stays open,
  HW-gated).** Same-day turnaround on the power-outage incident: the 2026-08-01 cold boot
  lost the race to the host network — bootstrap's 30 s MQTT wait timed out and the old
  connect-or-skip gate dropped WB emulation + scenario cards permanently, while connect
  attempt 5/5 landed 20 s later (house symptom: the kitchen-hood wb-rule writing into an
  unexisting `kitchen_hood/set_light` control; the persistence-less broker had wiped every
  retained card at boot). Card publishing now lives in a one-shot on-connect callback
  (`make_wb_cards_publisher`): registered on the client AND called directly when the boot
  connect already happened — gated on the LIVE connected flag, not the wait's snapshot,
  so a connect landing between the timeout and registration is covered; the latch makes
  later invocations no-ops (setup publishes config-derived initial values — re-running on
  reconnects would clobber live state, so a mid-run broker restart still wipes cards
  by design; only the catalog version self-heals per VWB-32 — recorded as an explicit
  non-goal). Decision recorded on the task's secondary: failed driver init at boot (the
  Broadlink `Errno 101`) gets no retry trigger — stays OPS-18 offline-marking + `/reload`.
  The same investigation caught the deployed 07-15 image's `/reload` restoring no cards
  (`set_runtime_services(mqtt_client=...)` nulls `wb_service` — CORE-1's gap (4), already
  fixed on main). 5 new tests incl. a lost-race end-to-end through the reconnect-loop
  harness; suite 752, pyright 0, contracts 6/6, openapi golden byte-identical; the
  architecture overview's startup sequence re-truthed. Close waits on deploying main to
  the WB7 (the op that also rack-verifies CORE-1) + a cold-boot verify.

- **2026-07-20 — UI-21 filed: the main-UI wireframe/layout session (the PROD-10 deferred
  filing discharged).** The owner asked where the bridge's UI-redesign task was — the answer:
  nowhere, by design. PROD-10 stage ② recorded "bridge files it at that session's intake
  (owner-announced)", and the session had not been announced; the workbench arc's bridge
  write-backs (UI-17/18) covered only the split + plugin. Today's ask IS the announcement —
  UI-21 now carries the session: wireframes over the operations surfaces, the `ui/`
  stylebook/ui-kit adoption plan, the D10 fluid island rebuild, the divergence-list verdicts.

- **2026-07-19 (follow-up) — CORE-1: the ScenarioWBAdapter residual retired pre-bench.** The
  fifth stale-composition gap closed on owner directive before the rack visit: the reload
  service gained a post-connect `rebuild_scenario_cards` hook — bootstrap builds a fresh
  adapter over the new client + WB service and re-runs `setup()` (cards republished,
  command topics re-subscribed, `on_active_changed` re-pointed). The rack checklist now
  includes the «Сценарии» cards. Suite 747.

- **2026-07-19 — CORE-1 code half: the hexagon's last exception removed (task stays open,
  HW-gated).** The `/reload` background task moved verbatim into `app/reload_service.py`;
  the router schedules it and imports zero infrastructure — the import-linter
  `ignore_imports` list is now EMPTY. Reconciliation against live code found the inline
  reload had quietly diverged from boot composition: the replacement client lost the
  maintenance guard + traffic observer, three routers kept publishing via the stopped old
  client, the VWB-32 on-connect publish was never re-registered, and the WB service stayed
  bound to the dead client — the extraction routes all of that through a bootstrap factory
  + rewire hook. `ScenarioWBAdapter` left as a known residual for the bench. Suite 747.
  Close waits on the owner's `POST /reload` rack verify.

- **2026-07-19 — OPS-37: scope-guard re-vendored @ v7.2.** The staleness nag the sweep kept
  surfacing, discharged: a one-regex rotation fix (commons IMPL-9 — `##`-style journals parsed
  as zero sections and `--rotate` refused; bridge's bullet-style journal was never affected).
  Byte-identical vendor, first run green at 1.4.1, `[[tool]]` manifest bumped, `repin --check
  --fail-on any` fully green across all pins and tools.

- **2026-07-19 — OPS-18: partial-startup failure marks WB cards offline.** Third of the
  sweep — the OPS-8 symmetry closed: `_release_partial_startup` now runs the same
  per-device WB offline pass the normal shutdown does (guarded, MQTT-still-connected,
  skipped when no client was ever acquired). Suite 742. The sweep's third commit; the
  repin warn lane kept flagging scope-guard v7.2 throughout — filed as OPS-37.

- **2026-07-19 — VWB-31: availability failures speak 503; catalog-v1.9 cut.** Second of the
  sweep. The classifier now checks reachability wording before the param sniff, so a handler
  that already knows the device is gone gets the same `device_unreachable`/503 the
  echo-timeout path emits — deliberately excluding bare "available"/"unavailable" (protocol
  limitations are real 500s; keyword set built from a sweep of actual driver error strings).
  The endpoint's OpenAPI description documented the old mapping, and that description lives
  in the stamped catalog openapi — owner ruled: cut the minor now (voice+commons still owe
  the v1.8 re-pin, so one re-pin covers both). Golden byte-identical. Suite 740. Side
  catch: a wrong-cwd `locveil-catalog` run scaffolds a stray `backend/config/` skeleton
  that shadows the root tree for 17 cwd-fallback tests — run it from the repo root
  (the README already says so).

- **2026-07-19 — DRV-22: IR driver's dead `last_command` enrichment deleted.** First of the
  low-hanging-fruit sweep (DRV-22 → VWB-31 → OPS-18, one commit each). The REL-5 #10 finding
  held exactly as written: the base chokepoint re-records `last_command` right after every
  handler return, so the IR driver's enriched record (topic/payload, hardcoded
  `source="mqtt"`) was overwritten on every single call — removal is pure dead code, zero
  behavior change. Suite 738.

- **2026-07-18 (evening) — OPS-36: contract-guard re-vendored @ v3.1.** IMPL-8's ARTIFACTS-PATH
  rule (bare artifact names now fail loudly — the VWB-43 trap promoted to an org-wide guard)
  taken the day it was tagged; first strict run 0-warning because VWB-43 had already normalized
  the catalog STAMP — the sequencing-free rollout the VWB-43-first ordering was chosen to buy.
  `[[tool]]` manifest bumped v3→v3.1.

- **2026-07-18 (evening) — CORE-7: core-py adopted for the driver axis; CORE-13 + OPS-35 filed.**
  PROD-8's bridge half executed the day its gate opened (voice ARCH-58 closed, core-py real).
  The pin was taken by the vendored repin tool itself — `.repin.toml` grew the `core-py` family
  and one command wrote the strict pin at `core-py-v1.1` (v1 refused by design: its tree
  predates the STAMP). The swap honored every §5 condition: loader + singleton in `utils/`
  (never `domain/`), zero new import-linter exceptions, registry re-keyed by class name (the
  configs' vocabulary), fresh-discovery semantics kept for `/reload`, dead
  `validate_class_references` folded out. Verified by driving it: 9/9 drivers discovered with
  the failure ledger empty, golden byte-identical at 79 devices. Suite 738. The findings
  analysis (config tree = complete demand declaration; driver→lib map 1:1) became CORE-13
  (config-driven activation, decided WITH UI-20) and OPS-35 (per-driver extras / slim images,
  the pip-extras dialect over voice's metadata quartet).

- **2026-07-18 (later) — VWB-43: catalog-v1.8 minor cut — the root-README false-drift trap
  defused.** Owner go-ahead after the commons IMPL-8 sequencing note (VWB-43-first makes the
  contract-guard v3.1 rollout sequencing-free). The real fix site was the GENERATOR:
  `dump_catalog.py` hardcoded the bare artifact names, so hand-editing the STAMP would have
  been reverted at the next regen — it now emits repo-root-relative paths, `CONTRACT_VERSION`
  1.8, STAMP regenerated, golden byte-identical, lightweight `catalog-v1.8` tag pushed with the
  commit. contract-guard now runs 0-warning with CONTENT-DRIFT genuinely armed for the catalog
  family. Re-pin owed: voice, commons.

- **2026-07-18 — OPS-34: workbench-plugin CI coverage (the round-2 zero-coverage finding).** New
  gated `workbench-plugin-validate` job: sibling checkouts of bridge + commons inside the
  workspace (the `file:` deps two levels up), ui-kit built first, then plugin `npm ci` +
  `gen:api-types` + typecheck + vite build; `workbench_plugin` path filter includes
  `backend/openapi.json` since the plugin consumes the OpenAPI contract like `ui/` does. All
  steps verified green locally before wiring. This closes the PROD-26 bridge slate: OPS-32/33/34
  + VWB-42 all landed today.

- **2026-07-18 — VWB-42: `device-integration-v1.1` cut (satellite's repo-to-repo ask).** The
  VWB-41 STAMP normalization finally got its version: STAMP 1 → 1.1 + annotated tag in the same
  change, so the tag's bytes are the clean core shape the satellite's DES-4 pins. The v1.1 STAMP
  enumerates its three artifacts with repo-root-relative paths — CONTENT-DRIFT is armed for this
  family from here on (and the VWB-43 path trap was dodged at birth). Registry row + convention
  README versioning prose re-truthed. Re-pin owed: satellite (first pin).

- **2026-07-18 — OPS-33: repin adopted @ repin-v1.** `scripts/repin.py` vendored; `.repin.toml`
  born as the family registry — report-protocol (the one consumed pin) + the `[[tool]]` manifest
  rows for all three vendored guards. Pre-commit warns, ordinary CI gates on major-gap, release
  flows run `--fail-on any` by hand. First full-strictness run all green: the pin and all three
  tools are at their owners' newest tags. The workbench family joins at the next commons workbench
  bump (machine schemas owed there first). Registry README now teaches the staleness lane.

- **2026-07-18 — OPS-32: the HK-12 guard + block sweep landed (one commit).** scope-guard
  re-vendored @ scope-v7.1 (contracts-verdict + unknown-prefix), contract-guard @ contract-guard-v3
  (ORPHAN-TAG / CONTENT-DRIFT / VENDORABLE-UNREGISTERED / --relax-tags), `.contract-guard.toml`
  born with explicitly empty `vendorable_roots`, `contracts_verdict_since = 2026-07-18`, and the
  `contract-triad` block pinned into CLAUDE.md (hash matches the commons source and the voice pin
  bit-for-bit). Hook relaxes tag rules mid-bump; CI stays strict and gained a belt tag-fetch step.
  From this entry on, every completion in this ledger carries a `contracts:` verdict. The first v3
  run also earned its keep: the catalog STAMP's pre-v3 `artifacts` shape (bare names vs v3's
  repo-root-relative reads) is a latent false-drift trap on the root README — filed as VWB-43 at
  the same sitting; the fix rides the next catalog cut because a STAMP is tag bytes.

- **2026-07-18 — PROD-26 delegation consumed: OPS-32/33/34 filed at intake; VWB-42 confirmed
  in-scope.** Pulled the board's PROD-26 entry (HK-12 execution: convention-enforcement hardening,
  decided 2026-07-18; bridge = guard sweep + repin adoption + workbench-plugin CI +  the satellite's
  device-integration-v1.1 request). Every claim verified against live repo state per
  `task-start-reconciliation` — **all four classify (a) valid, no narrowing needed**: scope-guard is
  vendored at scope-v6 (v7.1 current in commons), contract-guard at v1 (v3 current), no repin
  tool/`.repin.toml` exists here, `workbench-plugin/` has zero CI coverage, tag
  `device-integration-v1.1` does not exist and the STAMP carries no `artifacts` list. The two
  existing CLAUDE.md pinned blocks are byte-current against their commons sources — only the new
  `contract-triad` block (scope-v7.1) needs adopting. VWB-42 was already filed (`4cbf667`,
  repo-to-repo) and its claims re-verified at this intake; it executes after OPS-32 so
  contract-guard v3's ORPHAN-TAG/CONTENT-DRIFT enforce the cut it describes. Voice's BUILD-41/42/43
  sweep (closed 2026-07-18) used as the reference shape; deliberate bridge deviations: an explicit
  empty-`vendorable_roots` `.contract-guard.toml` (voice ships no config) and an ordinary-CI repin
  step at `--fail-on major` (voice gates at release time only).

- **2026-07-16 — PROD-8 delegation consumed: CORE-7 reconciled + narrowed, UI-20 filed at intake.**
  Pulled the board's PROD-8 entry (core-py bootstrap + first two extractions, COUNCIL-DECIDED
  2026-07-16, 2 rounds). Both bridge delegations executed; every claim verified against live code
  first per `task-start-reconciliation`.
  **CORE-7 — verdict (d) scope drifted → redefined** (owner consulted + accepted the narrowing
  before the edit, per the dialect's STOP rule). Confirmed stale: the entry gated on **"voice
  BUILD-21"**, which the council replaced with **"PROD-8 / core-py exists"** (skeleton cut only after
  voice's ARCH-50 + ARCH-42 land). Scope narrowed to council decision 1 — the shared loader is the
  **entry-point-group registry only**; the by-name config resolver (`utils/class_loader.py`) stays
  bridge-side; voice's `EntryPointMetadata` quartet stays voice-side. Council decision 2 recorded as
  a rejection with its reason: no config→entry-point unification, because runtime config-path
  discovery breaks the offline `dump_catalog` generator that builds the voice-pinned golden without
  loading a driver — so CORE-7 stays a self-contained infra swap, **no catalog-contract bump**.
  Verified live: the driver axis is the `locveil_bridge.devices` group, 9 drivers
  (`backend/pyproject.toml:98-107`), consumed at `domain/devices/service.py:51-52`.
  **Dead-code fold verified, not taken on faith:** `infrastructure/config/validation.py:67-97`
  (`validate_class_references`, pre-HK-8 `"devices."`/`"app.schemas."` prefixes at the board's cited
  `:89`/`:94`) has **zero call sites** — `validate_device_configs` never calls it, so `device_class`
  is never validated at startup at all. Folded into CORE-7 for removal with the swap.
  **UI-20 filed `[P1]` `[release]`** (council decision 3 — a SEPARATE task, never a rider on CORE-7).
  Intake verification found the board's "split-brained" wording **understated**: the surface is
  **three**-brained, not two. Raw-config surfaces show a configured-but-unloaded device
  (`/config/devices`, the nav dropdown with no room selected, workbench Device Setup lists it as
  healthy); loaded-driver surfaces deny it (catalog, layout/state/options/canonical all 404) — and
  **room membership is a loaded-driver surface too** since the 2026-06-08 refactor
  (`domain/rooms/service.py:203-240` derives `room.devices` by walking `DeviceManager.devices`), a
  nuance the board's wording missed, so the device vanishes the moment a room is selected. The third
  brain: `GET /devices/{id}/persisted_state` (`routers/state.py:75-92`) reads the store directly and
  answers **200 with a stale snapshot** for a device the rest of the runtime denies exists. Root
  cause: `service.py:116-120` logs and skips, retaining no status — the deliberate opposite of the
  setup()-failure policy at `service.py:154-183`, which keeps the device registered. User-visible:
  dead link from the unfiltered nav dropdown ("Layout unavailable"), HomePage total-vs-filtered
  counts disagreeing unexplained. UI-20 owns the suppress-vs-surface verdict the council left open.
  Board write-back: lead ID **CORE-7** + **UI-20**. docs: none — ledger-only intake, no behavior
  change (UI-20's own docs verdict lands with its fix).

- **2026-07-15 — DOC-18 DONE: the four unreferenced evidence docs reconciled — two anchored,
  two retired.** Scope-v6's UNREFERENCED rule (OPS-31) flagged four docs no ledger entry named.
  Verified each against the tree: **kept + anchored** `docs/design/conditioner/mitsubishi.md`
  (CN105 brown-out hardware note; MitsubishiHvac live) and `docs/design/ui/deployment-network-config.md`
  (consumer-UI container networking — nginx template / entrypoint / runtime.ts all present).
  **Retired to `docs/archive/`** (owner: "archive retired documents", "alise should be retired"):
  `remote_layout.md` → `docs/archive/ui-docs/` (described the removed build-time codegen renderer,
  superseded by the runtime Layout Manifest) and `wb-alice-bridge.md` → `docs/archive/review/` (the
  shipped voice path is the Irene catalog contract, not the WB-native Alisa bridge). Both got a dated
  retirement banner before the move. Tree clean → `[evidence] unreferenced` flipped `warn`→`error` to
  match commons and lock HK-10's fourth direction. Guard bare: green.
- **2026-07-15 — DOC-17 DONE: planned-pages prose staleness + the diagram title rename.**
  Two-part docs task, scope narrowed twice at owner direction. (1) `device-setup.md` +
  `topology-setup.md` left as-is — owner classes them dummy placeholder pages for now, not content
  to reconcile. (2) DOC-14 (2026-07-12) had already corrected the diagram *content* (voice-setup
  78 configs/11 rooms; appliance-pages HVAC-shipped×3), so DOC-17's diagram half was purely the
  rename-era title fix. **voice-setup.md:** §P3.7 tail reconciled — #22/#25/#26 all shipped
  (VWB-10/13/14), only #24 (wb-msw sensor side) remains deferred; counts corrected (12 profiles,
  62 WB configs, 11 rooms, 9 drivers, 78 device configs). **appliance-pages.md:** HvacPanel.tsx
  flipped Not-built→Built (3 instances, device-id-keyed via `DevicePage`), with an honest note
  that the `/appliance/:id` + `AppliancePage` container is still the planned end-state. **Diagrams:**
  all 15 `*.dot` titles `wb-mqtt-bridge`→`locveil-bridge` (33 occurrences) + the one `wb-mqtt-voice`
  sister-label→`locveil-voice`; PNGs regenerated via `dot`; `wb-mqtt-serial` (real WB daemon) left
  alone. Manifest coherence 8/8, no manifest edit. docs verdict = the 15 diagram nodes.
- **2026-07-15 — OPS-31 DONE: scope-guard re-pinned @ `scope-v6` (1.3.0) — the UNREFERENCED
  rule.** Commons published scope-v6 (IMPL-2 / HK-10): the guard now flags any evidence doc on
  disk that no active/DONE entry names — the fourth anchoring direction. Script-only re-pin:
  `scripts/scope_guard.py` vendored byte-identical to the tag (1.2.0 → 1.3.0); the CLAUDE.md
  pinned blocks are untouched because their commons *sources* (`process/claude-blocks/`) didn't
  change between scope-v5 and scope-v6, so the `scope-v5`/`scope-v4` markers and `[claude.blocks]`
  hashes stand. `.scope-guard.toml` gains `[evidence] unreferenced = "warn"` (the consumer
  default; commons runs `error`) — `dirs` already had `docs/review` since OPS-24. First run
  surfaced four pre-existing unreferenced docs (mitsubishi design, two ui/ design pages, the
  wb-alice-bridge review) → filed **DOC-18** to reconcile/anchor them (naming them there anchors
  them, so the guard is green now; flipping to `error` waits on DOC-18). Guard run bare: green. CI
  `ledger-guard` comment re-truthed to scope-v6. No board delegation — consumers adopt at re-pin.
- **2026-07-15 — UI-19 DONE: backend address via `PageProps.backends.api` (IMPL-6 intake).**
  Commons landed deployment-facts injection (workbench-v1.2): per-plugin `backends` in the
  owner-edited shell config reach pages through the loader. The plugin's pre-IMPL-6 invention —
  an ad-hoc `window.__LOCVEIL_BRIDGE_API__` global nothing set, plus a hostname fallback that
  hit the shell origin — replaced (~10 lines): the page wrapper now feeds `backends.api` into
  the module client synchronously (child effects run before parent effects — the voice plugin's
  reasoning), precedence localStorage escape hatch → shell config → `hostname:8000`. Plugin
  0.1.1; check+build green; runtime-config verified handing the bridge
  `http://192.168.1.50:8000`.
- **2026-07-15 — UI-18 DONE: the Bridge Workbench plugin ships and loads in the shell.** New
  top-level `workbench-plugin/` built to the HK-11 shape: single-file ESM entry (17 kB) with the
  singleton set external, plugin-run Tailwind css (preflight off), build-emitted
  `dist/manifest.json`, embedded generated API types, eslint-9 mirror of `ui/`. Descriptor: id
  `bridge`, RU/EN typed message table, status slot (`/system` + catalog version), reportHook →
  live `POST /reports`. Pages: voice-readiness (canonical test pane over the catalog vocabulary +
  the controller-address override), device-setup (inventory), topology (rooms) — all writes
  dormant behind `PROD-4-auth` chips. Registered in commons `workbench.config.json` (the reserved
  mount); verified over the shell's serve: runtime-config lists it, fragment/entry/styles 200.
  Notable reconciliations: ui-kit consumed from day one (the "local primitives until ui-kit-v1"
  fallback expired unused); dormant verbs render in-page (contract v1 has no verb surface);
  browser-render E2E deliberately left to the owner's first `npm run serve` against the WB7.
- **2026-07-15 — HK-11 intake: UI-18 confirmed + refined (runtime assembly), shell gate discharged.**
  Board delegation pulled from council HK-11 (Workbench runtime assembly = native ESM + import
  map; `BOARD_DONE.md`). As the entry predicted, no new ID — the refinement is absorbed into the
  already-filed UI-18: the plugin build gains the HK-11 shape (single-file ESM entry; singleton
  set external — react/react-dom(/client)/jsx-runtime/react-router-dom pinned major 6 +
  locveil-ui-kit — via the shell's import map; build-emitted `dist/manifest.json` fragment with
  styles the shell injects; descriptor compiles against `locveil-workbench/contract`; reference =
  the shell's in-tree demo plugin). `workbench_split.md` §2.1 amended same change: the build-time
  `file:` consumption bullet superseded by runtime assembly + strict refuse-and-surface on peer
  mismatch. Reconciliation finding beyond the delegation text: commons IMPL-1 (shell v1) is DONE
  2026-07-15 — UI-18's "shell exists" gate is discharged, the task is startable (sprint-02
  candidate). UI-18 written back to the HK-11 board entry as confirmed.
- **2026-07-15 — OPS-30 DONE: contract-guard CI job fetches tags.** The first manual image-build
  dispatch since OPS-27 forced the contract-guard gate to run and it failed with three
  `TAG-MISSING` violations — a CI-environment false alarm, not a contracts problem: all three
  STAMP tags (`catalog-v1.7`, `device-integration-v1`, `docs-manifest-v1`) exist locally and on
  origin, but the job's bare `actions/checkout@v6` clone carries no tags at all, so the v2 rule's
  `git tag -l` could never see them. Latent since the OPS-27 push itself (its own CI run failed
  the same way, unnoticed; every push since skipped the path-gated job). Fix: `fetch-tags: true`
  on that job's checkout, workflow-only — the vendored guard stays byte-identical to
  contract-guard-v2. The fix commit re-runs the gate (the `contracts` filter includes the
  workflow file).
- **2026-07-15 — DRV-39 DONE (DRV-40 folded in): eMotiva silence-while-busy — the terminal wedge fix.**
  `busy` is now a first-class driver state (`_busy_since`): armed by any commanded transition
  (power/input) and by the uncommanded `arc` grab, cleared on 2 s notification quiescence. The
  readiness gate returns a verdict and **fails closed** — at the cap it refuses the command
  (`success=False`/"still settling") instead of releasing it into a live CEC/ARC window (reverses
  DRV-38a's release-on-cap; `force` is the operator override). The power-on handler's defensive
  re-subscribe + status batch — the 2026-07-14 wedge itself — is deleted (we're already subscribed;
  the device pushes its burst unbidden). Subscription-breadth audit: the 9 monitored properties all
  surface in state, so the set stays; the load win was dropping the re-subscribe. DRV-40 folded in:
  the watchdog's recovery probe now backs off (interval→×2→90 s cap, reset on recovery) — the
  07-14 outage's 2 455 probe cycles become a handful; detection latency unchanged. 6 new + 5
  reshaped tests (fail-closed through real dispatch, force bypass, arc-arm, backoff); suite 734,
  pyright 0, import-linter 6/6, no contract drift. HW verification rides the DRV-38 rack replay
  (CEC-state pin first, DRV-32). DRV-38a's entry annotated with the fail-closed supersession.
- **2026-07-15 — SCN-18 DONE: boot restore is tracking-only — a restart never touches hardware.**
  Owner decision (same day, after the restart-vs-deploy analysis: detectable via a baked build sha,
  but it misclassifies the power-outage case, and reconcile-at-boot is a no-op exactly when it's
  safe): restore marks the scenario active (state, persistence, WB card, SSE) and dispatches zero
  device commands — `_restore_state` → new `_restore_tracking`, replacing the full
  `switch_scenario`. Kills the wedge-#3 exposure (the 07-14 boot-restore cold-start) and the
  3am-outage cold-start. Drift heals on demand via the SCN-11 dialog. 2 new tests (zero dispatch,
  no manual steps); suite 728; docs: arch/key-concepts restart passage now says tracking-only
  explicitly.
- **2026-07-15 — OPS-29 DONE: the load-bearing eMotiva transitions survive INFO.** Device-reported
  transitions of power / zone2_power / input_source now log at INFO in the driver (actual value
  changes only — a handful of lines per scenario switch; keepAlive stays silent), so the
  `source → arc` grab that keys the readiness gate is visible in a production log — OPS-25's
  hygiene had blinded exactly that evidence at wedge #3. `ops/INSTALL.md` gains the "Full protocol
  forensics" temporary flip-on procedure (root DEBUG + unpin pymotivaxmc2, revert after). Caplog
  test locks the contract. docs: install.
- **2026-07-15 — LIB-1 + LIB-2 + LIB-3 DONE: pymotivaxmc2 0.8.0 shipped and repinned (one unattended
  sweep, owner-directed).** Three commits in `../pymotivaxmc2` (`08073b3` serialization + stale-frame
  hygiene · `0f2d111` per-call retries/`ack="no"`/pacing/missing-only batch retry · `f7ff11a`
  keepAlive accessor/real unsubscribe/`SO_REUSEADDR`/sequence surfacing), each with tests + guide
  updates; release `927ae18` bumped 0.7.0 → **0.8.0** (minor — new public surface), tag `v0.8.0`
  pushed, the library's gated CI published to **PyPI** (295 tests, import-linter 4/4,
  no-TYPE_CHECKING, pyright 0). Bridge repin in the same change: `pymotivaxmc2==0.8.0` + lockfile,
  and the driver's `getattr(client, "_info")` replaced with the new public
  `keepalive_interval_ms` (the LIB-3 completion item). Bridge gates on 0.8.0: pytest **725**,
  import-linter 6/6, pyright 0. Semantics now under the driver: control-port transactions serialize
  and drain stale frames (the wedge-#3 cross-talk class dies in the transport), and DRV-39 has its
  library-side toolkit ready (`retries=0`, `ack="no"`, `min_send_interval`, notification gaps).
- **2026-07-15 — eMotiva fix set re-scoped on the community research: the library/driver boundary
  drawn, LIB-1 reshaped to serialization.** Owner-approved. Principle recorded in the LIB section:
  the **library** owns protocol-transport safety (packets-in-flight, replies, retries, pacing,
  socket hygiene, reliable notification delivery); the **driver** owns device-state semantics
  (busy/readiness, subscription breadth, when to send, completion-awaiting). Changes: **LIB-1**
  reshaped from "correlate replies" to **serialize the control port** (`Semaphore(5)→1`) — the
  openHAB "limited processing power / subscribing to all channels grinds it to a halt" finding
  proves the device can't use concurrency, and serialization subsumes reply-correlation, respects
  the load ceiling, and removes the concurrent-flood path in one change; **LIB-2** reframed as the
  second line behind LIB-1 (pacing is now a device *requirement*, vendor-confirmed); **LIB-3**
  prioritizes notification sequence numbers (they enable the readiness redesign); **DRV-39** gains
  the explicit driver-side subscription-breadth lever (trim `PROPERTIES_TO_MONITOR`, stop the
  power-on tail re-subscribing to all). No new tasks — the research sharpened the existing set. See
  [`emotiva_arc_community_research_2026-07-15.md`](review/emotiva_arc_community_research_2026-07-15.md).
- **2026-07-15 — eMotiva wedge #3 investigated: evidence frozen, seven tasks filed, LIB workstream
  born.** The 2026-07-14 08:07 wedge (startup restore of `movie_appletv` after the redeploy) is NOT
  the keep-alive/watchdog work (cleared on three counts — wedge #1 predates DRV-30; the watchdog
  fired 25 s AFTER the silence began; interval math correct) and NOT a gated-command escape: the
  device died ~3 s after its own gate-exempt main-zone `power_on`, whose handler tail (defensive
  re-subscribe + a 9-property Update batch at +1 s, silently retried whole 3× by the library) floods
  the fw-3.1 fatal window from inside the exempt command. The library's shared-queue cross-talk was
  observed directly in production (`emotivaUpdate` consumed by a subscribe probe). Protocol-doc
  analysis: subscribe/update are legal "at any time" but the spec's own transaction model names the
  *notification*, not the ack, as the resume point — and observation shows subscriptions survive
  standby→on, so the defensive re-subscribe guards a non-case. Evidence frozen:
  `docs/review/emotiva_wedge_20260714.md`. Filed: **DRV-39** (quiet the power-on tail), **DRV-40**
  (watchdog probe backoff — 2 455 cycles/12.5 h on record), **SCN-18** (boot-restore policy — a
  redeploy cold-started the rack unattended), **LIB-1/2/3** (pymotivaxmc2: reply correlation, retry
  damping + pacing, API/hygiene batch — new **LIB** workstream: tracked here, executed in
  `../pymotivaxmc2`, landing as pin bumps; scope-guard prefixes updated), **OPS-29** (forensic
  middle ground — the ARC claim must survive INFO). DRV-31/32 (fw 3.2 flash) and the DRV-38 rack
  replay stand unchanged; device recovered 20:46 via external power-cycle, the DRV-30 recovery path
  worked first try.
- **2026-07-14 — UI-17 DONE: the Workbench split designed on the bridge side; UI-18 + CORE-12 +
  DOC-17 filed.** The design (`docs/design/ui/workbench_split.md`) renders PROD-24 for this repo:
  the Bridge plugin (new top-level `workbench-plugin/`, built artifact consumed by the commons shell
  via `file:` dep; RU/EN bundles; status slot from `GET /system` + catalog version; `reportHook` →
  `POST /reports`), the staged-write API shape (envelope-per-target in `data/staged-config/`,
  stage-time full-tree validation, stale-base conflicts never merge, self-cleaning after promotion;
  promotion stays a human commit), and the v1 read-only cut that ships useful pages before PROD-4 —
  every config-writing verb dormant under the named gate `PROD-4-auth`. The four planned pages were
  re-pointed in the same change (admin-shell rows superseded/deleted, staging language on
  device-setup/topology-setup, live-vs-file answered = staging) and the three flow diagrams
  regenerated with the staged hop / Workbench chassis. Discovered (not caused) staleness filed as
  DOC-17: planned-pages status tables predate recent landings (HvacPanel, value-labels) and every
  diagram title still says "wb-mqtt-bridge". Implementation follow-ups: UI-18 (plugin skeleton +
  read-only cut, gated on the commons shell), CORE-12 (staged-write API, hard-gated on PROD-4's
  auth decision).
- **2026-07-14 — OPS-13 DONE (UI-8 absorbed): the UI dev-toolchain is on eslint 9 flat config +
  @typescript-eslint 8 + vite 6.** Sprint-01 decision 1 discharged — ui-kit (PROD-10) now targets the
  migrated toolchain and the migration ran once. `.eslintrc.cjs` → `eslint.config.js` as a faithful
  translation (same ts/tsx-only scope, same type-aware tuning); vite landed at 6.4.3, the exact
  first-patched version the filing predicted. The 26 new-rule errors were fixed in code, not config:
  17 custom-icon empty interfaces → type aliases, 7 redundant `as 'material'` assertions removed,
  `fmtValue` got honest positive typeof-narrowing (negative `typeof` doesn't narrow `unknown` — the
  rule was right), one documented inline disable for the axios re-reject idiom. Gates green (check +
  build), the vite-6 SSE dev-proxy smoke passed against a mock backend (both `http-proxy` hooks
  fired, stream unbuffered), `npm audit` 0, minimatch 9.0.3 out of the lockfile — the five
  OPS-7-dismissed Dependabot alerts resolve on the post-push scan. docs: ui-readme (Vite 5 → 6).
- **2026-07-14 — PROD-19 intake: the locveil-reports intake-consolidation twin filed as OPS-28.**
  The board's PROD-19 ("one door, locveil-reports") delegated the bridge twin of voice BUILD-14 —
  the write-back slot had been empty since HK-7 because BUILD-14's "the bridge repo has the same
  question" claim never got a bridge task. Reconciled at intake: the bridge carries none of voice's
  pre-board machinery (no issue templates, no triage workflow, no docs pointers, zero issues ever
  filed), but the public repo's Issues tab is enabled bare — an unwatched side door, and the only
  intake channel a public visitor can see (locveil-reports is private). Filed `[P2][deferred]` to
  travel with voice BUILD-14's posture decision (forward / redirect-templates / disable). ID written
  back into board PROD-19 (`Bridge ID: **OPS-28**`).
- **2026-07-14 — PROD-24 intake: the Workbench bridge delegation filed as UI-17.** The board's
  Workbench shell council (PROD-24, decided 2026-07-14; commons `docs/design/workbench.md`) delegated
  one bridge item — the sprint-01 "(files at intake)" UI-surfaces row, grown by the council:
  Bridge-plugin design + the staged-write API shape (`data/staged-config/` proposals; promotion = an
  explicit human commit; **no write API before PROD-4's auth decision**) + the planned-docs follow-up.
  Reconciled clean against the repo: the four ID-less `docs/planned/` pages exist and all four carry
  the shared "Admin route / auth shell — Not built" row (the scope the council deleted from operations
  — the Workbench answers it once); topology-setup's "Live vs file edit mode" open question is the one
  the council answered = staging; the pages are not `docs/manifest.json` nodes (only their flow
  diagrams are). Cross-repo ID coincidence flagged in the entry: voice filed its *own* UI-17
  (config-ui → Workbench plugin) at the same council — references must be repo-qualified. ID written
  back into board PROD-24 (`Bridge ID: **UI-17**`).
- **2026-07-14 — DOC-16: VWB-39's stale dependency line re-anchored (commons PROD-23 delegation).** The
  HK-9 dependency audit's side-find executed: the line still framed done-VWB-38 (`device-integration-v1`,
  2026-07-12) as pending and named "DRV-36's implementation" (DRV-36 was design-only — the implementation
  is DRV-37). Rewritten to the real trigger: activates alongside DRV-37 at the satellite's first
  conforming descriptor (PROD-20 chain). Executed by the commons session on owner instruction, filed and
  completed in one change per the quick-task precedent.
- **2026-07-14 — OPS-27: contract-guard re-vendored @ v2 (PROD-22) — and the new rule fired on us immediately.** The TAG-MISSING check the bridge requested after the catalog-v1.7 false green found a second instance in this very repo: the docs-manifest STAMP named a tag that didn't exist. Tag created at the STAMP's landing commit (3592282); check green. Executed by the commons session on owner instruction.
- **2026-07-13 — OPS-26 DONE: `meta/driver` wire-identity cutover `wb_mqtt_bridge` → `locveil-bridge`
  (the last PROD-21 bridge item).** Owner-gate lifted by the user. Separate, separately-revertible
  commit — the only task allowed to touch the retained `meta/driver` string. Flipped the two default
  literals (`wb_device/service.py:77`, `devices/base.py:122` — the production caller) that CORE-10
  deliberately preserved. Reconciliation confirmed `driver_name` is never a persisted-state key (only
  the in-memory `_active_devices` dict + the `meta.driver` publish, `retain=True qos=1`) and no
  doc/config states the value → pure republish-in-place, no broker migration. Functionally verified:
  the real publish path now emits `meta.driver == "locveil-bridge"`. Live retained topic flips on the
  next WB7 image deploy (`git pull` + `update.sh` + restart). Gates: import-linter 6/6, pyright 0,
  pytest 725. **PROD-21 bridge share fully consumed (CORE-10 + CORE-11 + OPS-26 all DONE).**

- **2026-07-13 — CORE-11 DONE: config tree → repo root + Dockerfiles → `docker/` (root context).**
  Two commits, NO contract cut (golden byte-identical). `git mv backend/config → config` (pure
  renames). The `ConfigManager`/CLI default `"config"` stays CWD-relative — the container resolves it
  via WORKDIR `/app` + the `/app/config` mount, so nothing repo-relative gets baked in. **Reconciliation
  surfaced the cert-path coupling** the delegation's "loader/CLI defaults" hinted at: LG TV configs store
  cert paths (`config/devices/certs/*.pem`) validated relative to CWD = the deployment root, which is now
  the repo root (was `backend/`), matching the container's `/app`. So the offline catalog build + the real
  `locveil-catalog`/`locveil-openapi` regen now run from the repo root (`uv run --project backend …`);
  updated `dump_catalog --output` default, the golden test (`monkeypatch.chdir(REPO)`), and the regen
  docs — verified the repo-root regen reproduces the byte-identical golden with the LG TVs present. ~15
  test config anchors retargeted; walk-up tests auto-adapt. ops/update.sh, compose comments, CI filters
  (backend + ui), the `config-master-tree` invariant, manifest globs, and two `.dot`+`.png` diagrams
  followed. Dockerfiles moved to `docker/` with root context: backend COPYs re-prefixed, **CORE-10's
  `CMD ["wb-api"]` miss fixed → `locveil-bridge`**, the UI's vestigial `COPY backend/config`/`openapi.json`
  removed (build reads nothing outside `ui/`), CI repointed, the two `.dockerignore`s merged to one root
  file. Both images build clean locally (amd64). Gates: import-linter 6/6, pyright 0, pytest 725,
  contract-guard 0, docs-manifest 8/8, UI check+build green. Remaining PROD-21 bridge work: OPS-26
  (owner-gated wire cutover).

- **2026-07-13 — CORE-10 DONE: `wb_mqtt_bridge` → `locveil_bridge` import rename + console scripts +
  the deliberate `catalog-v1.7` cut (one tree churn).** Bridge was already src-layout so no layout
  move was owed. `git mv` (98 files, history preserved) + identifier sweep across 104 `.py`, pyproject
  (entry-point group + the 6 import-linter contract refs + root_packages), device-state-mapping, CI,
  manifest globs, live docs. Scripts `wb-openapi`/`wb-catalog` → `locveil-*`; `wb-api` retired. **The
  two `meta/driver` wire literals (`driver_name="wb_mqtt_bridge"`) were deliberately preserved — that
  cutover is OPS-26, owner-gated.** catalog-v1.7: schema-name-only change (the two `ManualInstructions`
  variants), golden byte-identical; regenerated openapi + UI types + STAMP. Removed the stale
  gitignored `src/*.egg-info` (polluted `importlib.metadata` on the src-on-path install) + reinstalled
  editable. sys.path shims: delegation estimated 4, actual inert count was 9 — removed all 9
  (user-confirmed) + orphaned imports. Gates: import-linter 6/6, pyright 0, pytest 725, UI check+build
  green. Remaining PROD-21 bridge work: CORE-11 (config→root + Dockerfiles→root), OPS-26 (wire
  cutover, owner-gated).

- **2026-07-13 — PROD-21 intake: HK-8 Python-layout convention delegated to the bridge → filed
  CORE-10 + CORE-11 + OPS-26.** Pulled the board delegation (council HK-8, normative
  `../locveil-commons/process/python-layout.md`), reconciled the keeper checklist against live repo:
  bridge is **already src-layout** (`backend/src/wb_mqtt_bridge/`, tests outside the package) so no
  layout move is owed — confirmed the delegation correctly omits one. Verified 122 `.py` files
  reference `wb_mqtt_bridge`; entry points `wb-api`/`wb-openapi`/`wb-catalog` present; config at
  `backend/config/`; Dockerfiles at `backend/Dockerfile` + `ui/Dockerfile`; `locveil_bridge.egg-info`
  is a gitignored stale build artifact, not a partial rename. Filed three tasks (rename+catalog-v1.7
  cut; config→root + Dockerfiles→root/`docker/`; owner-gated `meta/driver` wire cutover). Execution
  not started — awaiting go. Board write-back pending.

- **2026-07-12 — OPS-24: scope-guard re-pinned @ `scope-v5` — the docs-verdict rule is armed
  (PROD-17 / HK-6, bridge delegation (4); closes the bridge's PROD-17 arc).** Vendored script →
  1.2.0 (tag-verified); the `shared-invariants` block re-pinned with the org-wide
  `user-facing-docs-are-done` invariant (the local bullet is now formally its dialect — the DOC-13
  rewrite); `docs_verdict_since = 2026-07-13`, so every completion from tomorrow must carry a
  `docs:` verdict (today's PROD-17 entries carry them voluntarily). With DOC-13/14/15 this completes
  all four PROD-17 bridge delegation items in one session; board write-back rode the intake
  (commons `5142a64`).

- **2026-07-12 — DOC-15: ADR dissolution — the class is retired (PROD-17 / HK-6 q3, bridge
  delegation (3)).** ADR 0006's four dependency-pinning rules now live as `CONTRIBUTING.md` →
  "Dependency policy" (OPS-19 re-pointed at it); 0001–0005 verified in force (0005's voice half
  recorded as overtaken — Irene catalog contract, not the Alisa bridge) and all seven files archived
  to `docs/archive/adr/` with dated supersession banners; every live `docs/adr` link re-pointed.
  The manifest never carried ADR nodes, so no coverage change.

- **2026-07-12 — DOC-14: the docs staleness pass — CONTRIBUTING re-truthed, OpenAPI descriptions
  scrubbed (⇒ `catalog-v1.6`), NINE diagrams fixed (PROD-17 / HK-6, bridge delegation (1)).**
  CONTRIBUTING's pre-VWB-29 regen paths / 3-vs-6 import contracts / missing contract-guard job fixed;
  every task-ID and §-ref scrubbed from the schema-surfacing descriptions (regex-verified CLEAN on the
  regenerated schema), UI types regen'd — artifact bytes moved, so a deliberate minor cut:
  `CONTRACT_VERSION` 1.6 + tag `catalog-v1.6` + the STAMP `artifacts` pin-completeness list (golden
  hash unchanged; voice picks it up at their next re-pin). The REL-4 "5 flagged diagrams" (list never
  recorded) resolved by verifying all 13 via three parallel agents: 9 stale — dead scenario endpoints,
  a missing port and driver, planned-vs-shipped drift (HVAC panels, global aggregates), the dead
  role-routing atom — all fixed + re-rendered; 4 accurate. Suite 717.

- **2026-07-12 — DOC-13: the docs manifest lands — the org's first (PROD-17 / HK-6, bridge
  delegation (2)).** `docs/manifest.json` (36 nodes / 14 roots / 10 surfaces, floor fully staffed,
  canonical-reference triples on the contract READMEs, 15 diagram pairs as nodes) +
  `contracts/docs-manifest/` (STAMP @ docs-manifest-v1, INTERNAL, verbatim commons-schema copy for
  hermetic CI) + the 8-test coherence suite (bijection passed on the first sweep) + the CLAUDE.md
  dialect rewrite (manifest = scope authority, verdict-line rule). Falsifiability check documented
  as the rule-of-two deferred slice. Upstream note: the schema rejects top-level `$comment`, which
  the commons template skeleton carries. Suite 717. PROD-17 siblings DOC-14/DOC-15/OPS-24 follow.

- **2026-07-12 — OPS-23: contract-guard vendored @ `contract-guard-v1` — hook + CI, zero warnings
  (PROD-16 / HK-5, bridge delegation (4); closes the bridge's PROD-16 arc).** `scripts/contract_guard.py`
  byte-identical to the commons tag; `hooks/pre-commit` chains both vendored guards; `build-arm.yml`
  gains the path-gated `contract-guard` job. First run on the finished layout: 0 failures, 0 warnings —
  the bridge's contract surfaces (catalog owned, device-integration owned, report-protocol strict pin)
  are fully convention-shaped. With VWB-29/40/41 this completes all four PROD-16 bridge delegation
  items in one session; board write-back rode the intake (`commons ef751bf`).

- **2026-07-13 — OPS-25: production log hygiene — the 20 MB/day is over.** Root INFO +
  `pymotivaxmc2: WARNING` pinned in the existing `loggers` map (no model change — the mechanism
  was there all along, the library just never joined pyatv/upnp in it); keepAlive beats now log
  NOTHING even at DEBUG (driver-side early-exit, watchdog untouched, caplog-locked); the 34
  forensic tags stay as flip-on instrumentation; the boot-time `print("DEBUG: …")` leftover gone;
  retention 30→1 day (owner decision: today + yesterday — cross-midnight analysis, nothing else).
  2026-07-12 corpus at the new levels: ~1.5 k lines (≈300 KB) instead of 138 k / 20 MB. Lands on
  the WB7 at the next update.sh cycle. Suite 725.

- **2026-07-13 — SCN-16 + SCN-17: the DRV-38 review remediations land in one shot.** SCN-16:
  zone-aware power planning — `ZonePower.port` (planner metadata, zero contract drift) +
  `resolve_targets` now returns `used_ports` (both endpoints per link — `source_targets`
  structurally loses the mid-chain src port, exactly the eMotiva's `zone2`); planner, forced plan,
  and SCN-11 preview all filter on it, so `movie_ld`/`movie_vhs` no longer fire the spurious
  `zone2_power_on` while the three zone2-using scenarios keep it. SCN-17: `execute_plan` bounds
  each dispatch with `wait_for(60 s)` — a hung driver costs one failed step, not the switch.
  Suite 724, golden byte-identical. Still owed on DRV-38: the rack replay (HW-GATED).

- **2026-07-13 — DRV-38 (a)+(b): the eMotiva readiness hold moved to the dispatch seam; the
  topology-layer review lands — SCN-16/SCN-17 filed.** The 2026-07-12 wedge trigger
  (`power_on {zone:2}` acked mid-ARC-handshake, then silence) is closed at the only layer that
  covers emergent plan shapes: `execute_action` itself, zone-aware (main-zone power exempt as the
  window starter, zone 2 gated — same action name, the zone decides), fresh-`arc` = full hold for
  ANY command, `force` no bypass. Review (three parallel lanes, frozen in
  `docs/review/topology_readiness_review_2026-07-13.md`): eMotiva is the fleet's only
  hardware-wedge risk; the executor honors driver-side holds without eating the gate budget
  (validating the seam as the pattern — base-class hook waits for rule-of-two); zone2 fired
  ungated in all five AV scenarios and spuriously in ld/vhs → SCN-16 (zone-aware power planning)
  + SCN-17 (bound dispatch) filed. DRV-38 stays `[~]` HW-GATED on the rack replay + the DRV-32
  CEC re-check.

- **2026-07-12 — VWB-41: device-integration owner-side guard — committed example fixture + CI check +
  STAMP core (PROD-16 / HK-5, bridge delegation (3)).** The convention README's canonical descriptor
  example is now committed as `contracts/device-integration/example.descriptor.json` with byte-parity
  enforced by a new 5-test backend suite (schema self-check, fixture validates, README==fixture,
  en-only names rejected, full-surface in-test descriptor for the shapes the example doesn't reach).
  STAMP normalized to the org core (tag `device-integration-v1` untouched, commons sidecar precedent).
  Suite 708.

- **2026-07-12 — VWB-40: report-protocol pin → `contracts/pins/report-protocol/` (PROD-16 / HK-5 q3,
  bridge delegation (2)).** The VWB-37 root pin moved into the org pin shape — verbatim artifact
  (tag-verified `report-protocol-v1` bytes), the owner's STAMP sidecar verbatim, and a strict PIN.json
  (files hash map + owner_commit + conformance pointer). Consuming paths re-taught: the conformance
  test, the `REPORT_*` comment, the design doc, the registry, and the reports-repo `lens-bridge.md`
  (which also learned VWB-29's `contracts/catalog/` paths). Wire surface untouched.

- **2026-07-12 — VWB-29: the catalog contract's owner-side cut — `contracts/catalog/`, `CONTRACT_VERSION`,
  STAMP core, first family tag `catalog-v1.5` (PROD-16 / council HK-5, bridge delegation (1); rescoped at
  intake `17734d8` from the superseded GitHub-Release + `contract-vN` shape).** The catalog family now
  lives on the org's uniform owned surface: artifacts + contract README under `contracts/catalog/`, a
  direction-labeled registry at `contracts/README.md`, the version carried in code
  (`presentation/api/catalog.py::CONTRACT_VERSION`) and stamped into the STAMP core alongside the build
  extras. Golden byte-identical (`5622ba7a1a78102a`) — a versioning/layout cut, no surface change, no
  voice re-pin owed beyond their own BUILD-24. En-passant find: `test_contracts_golden.py` had its whole
  module pasted twice (shadowing hid it); deduplicated. Same-session siblings: VWB-40 (pin relocation),
  VWB-41 (device-integration guard), OPS-23 (vendor contract-guard).

- **2026-07-12 — `cross-repo-board` block re-pinned @ scope-v4 (PROD-15 close follow-through).** The
  shared block now names `../locveil-satellite` as the fourth sibling; block text between the markers +
  the `.scope-guard.toml` hash updated from the commons source per the `process/claude-md.md` §3 flow
  (mechanical re-pin, no other content change — journal-line only, no ledger task). PROD-15 closed on
  the board the same day.

- **2026-07-12 — DRV-36: EspManagedDevice designed (design-only, owner decision) — DRV-37 filed
  BLOCKED on the satellite's first descriptor.** Same-day continuation of the PROD-15 arc:
  `docs/design/esp_managed_device.md` consumes the VWB-38 convention. One descriptor-native driver
  class for every Locveil-built satellite device — new device = descriptor pin
  (`config/descriptors/<id>.json`, byte-identical mirror, fail-fast load validation against the
  convention pin) + thin device config; the descriptor's capability block translates mechanically
  into the class-map dialect as the loader's third per-instance source, so catalog projection and
  the DRV-29 gate honoring are unchanged code. Reachability finally rides an honest LWT
  (`meta/online`) — no DRV-27-style heartbeat; the `meta/locveil` stamp surfaces `stale_pin`
  monitor-only; broker-wipe self-heals firmware-side (announce-on-reconnect). `requires_arm` stays
  firmware-enforced single-point; the deck transport vocabulary remains a deferred contract cut
  batched with implementation. **DRV-37 filed:** implement per design, BLOCKED on satellite DES-4's
  first conforming descriptor (the fixture); VWB-39 activates alongside; per-deck configs still wait
  for first-light. Design-only — no code, no contract, golden untouched.

- **2026-07-12 — VWB-38: device-integration convention v1 designed + shipped — the satellite
  boundary has its contract.** Interactive design session (PROD-15 item 2, HK-4's "convention down,
  descriptors up"). Deliverables: `docs/design/device_integration_convention.md` +
  `contracts/device-integration/` (guide, `device-descriptor.schema.json`, `STAMP.json`), tagged
  **`device-integration-v1`**. wb-mqtt-v1 promotes the deck FR-text exactly as the satellite's
  DES-1 truth pass dispositioned it (announce / LWT `meta/online` / `<control>/on` / echo-on-success
  / `requires_arm` / STATIC `confirm_latency_ms`), cross-checked against the bridge's own WB
  emulation and passthrough consumption — one dialect, both ends of the wire; plus the retained
  `meta/locveil` stamp as the monitor-only stale-pin tripwire. Three owner decisions: descriptors
  CARRY the canonical capability mapping (class-map dialect, `control` for `command`, gate derived
  from the static latency — DRV-36 becomes truly descriptor-native, zero bridge authoring per new
  device); REST leg = asset-plane URLs normative + `GET /api/status`/`POST /api/control` reserved
  (full profile waits for the first real consumer); descriptor i18n ru+en required / de optional.
  **Standing owner constraint recorded in the design §2: `MitsubishiHvac` stays untouched —
  external firmware never retrofits; the only door is an owner-decided firmware rewrite** (the
  HVAC design's §8 transport-swap horizon). Schema machine-verified (draft 2020-12 valid; the
  guide's example validates; en-only names rejected). No code, no catalog/openapi change, no
  re-pin owed. DRV-36 + VWB-39 (pre-filed) are the follow-ups; the satellite's DES-4 can now pin.

- **2026-07-12 — DRV-35: `ESP32/` deleted, DRV-7 retired — the deck-bridge scaffold lives in
  locveil-satellite now.** The satellite confirmed the import repo-to-repo (`c592733` into this
  ledger): their DES-6 (`0d950a9`) copied the tree verbatim @ `a80322f` into `imports/bridge-esp32/`;
  their DES-1 harmonization then absorbed the docs into `locveil-satellite/docs/devices/`
  per-device dossiers + their DES-1 truth-pass review record and deleted the staging copy — owner verdict: the code was
  reference-only, the docs were the value; single-image FR-1 stays retired (GPIO14 triple-booking,
  recorded in their deck-common §5). Bridge side, one change: the 35-file tree removed (`git rm`),
  **DRV-7 moved to DONE as RETIRED-exported** (never completed here — the bench fill-ins and
  first-light verification belong to the satellite's FW phase now; everything resolvable in history
  at `a80322f` and earlier). Pre-delete sweep found zero external references to the tree; VWB-38's
  promotion source had already been re-pointed to the satellite's truth pass (`be774a8`), so the
  delete strands nothing. PROD-15's bridge item 1 is fully discharged (1a rode DRV-34). Remaining on
  this arc: **VWB-38** (convention design — the separate session, next), then DRV-36, then VWB-39.

- **2026-07-12 — DRV-34: HK-4 supersession doc pass done — the chip discrepancy resolves AGAINST the
  plan narrative.** Same session as the PROD-15 intake below. The parked question closed with hard
  evidence: the HVAC modules are **ESP8266** (Wemos D1 Mini) — recorded at DRV-27 decision D1
  ("renamed, since the modules are ESP8266 and the contract is the firmware's, not a chip's"),
  `docs/design/mitsubishi_hvac_driver.md` + `docs/wb_device_authoring_log.md` concur — so the VWB
  context narrative's "run on ESP32" was wrong and the board's HK-4 charter wording ("HVAC ESP8266
  firmware rewrite" as the escalation trigger) is right. Executed: dated SUPERSEDED block under the
  2026-06-08 ESP32ManagedDevice paragraph (the HVACs graduated to `MitsubishiHvac` via DRV-27/28
  instead; the ESP-managed class returns descriptor-native as `EspManagedDevice`, DRV-36); the stale
  "§5" positional pointer fixed to DRV-7; DRV-7 annotated (frozen import source, FR-1 single-image
  retired satellite-side, retirement rides DRV-35, no reactivation here); `productization_bridge.md`
  §3 amended in the doc's own annotation style (regime 1 vindicated — the satellite grew a
  bridge-facing surface and the bridge generates both layers). Frozen history untouched; user-facing
  docs + code grep-clean of the old name. Docs-only change.

- **2026-07-12 — PROD-15 bridge delegation pulled: satellite-boundary tasks filed (DRV-34/35/36 + VWB-38/39).**
  Board-as-outbox intake (council HK-4, locveil-satellite bootstrap; board entry PROD-15 in
  `../locveil-commons/board/BOARD.md`). All five delegation items verified against the repo at intake
  (`task-start-reconciliation`) — all valid: the `ESP32/` tree + DRV-7 + the ESP32ManagedDevice
  narrative + the `productization_bridge.md` §3 "no bridge work filed" note + the VWB-37 pin pattern
  are exactly as the delegation claims. Two reconciliation notes: the delegation's "§5.1 area" pointer
  is old positional numbering — the superseded text actually lives in the VWB context narrative (plus
  a stale "§5" back-pointer inside DRV-7); and the board says the HVACs run **ESP8266** while the plan
  says **ESP32** — discrepancy parked into DRV-34 to resolve at execution. Filed 1:1 with the
  delegation: **DRV-34** (HK-4 supersession doc pass — items 1a+5 folded, both dated-annotation work),
  **DRV-35** (DELETE `ESP32/` + retire DRV-7; BLOCKED on satellite import confirmation — item 1b),
  **DRV-36** (EspManagedDevice driver design — item 3), **VWB-38** (device-integration-convention
  design — item 2; owner: separate session), **VWB-39** (descriptor-pin conformance test, VWB-37
  pattern — item 4). Per-deck cutover tasks stay unfiled until satellite first-light, per the board.
  IDs written back into the PROD-15 board entry (commons commit).

- **2026-07-11 — VWB-35: reports-repo re-point complete — spool drained (trivially), the task closes.**
  Board-as-outbox delegation (PROD-14 phase 2 (1), council HK-3); implementation had landed
  (`0279ade` + `7f37dae`: slug sweep, explicit `reports.repo`, schema default dropped with the
  fail-fast validator, regen chain, guide re-truthed) with the completion held on the one owner
  check — the agent's SSH to the controller is permission-blocked by design. Owner verified:
  `/mnt/data/locveil-bridge-config/data/` has **no `reports/` subdirectory** — the spool dir is
  lazily created on first spool (`_spool` mkdir; `retry_spooled` handles absence), so nothing was
  ever spooled = trivially drained, consistent with the phase-1 live smoke delivering directly.
  All four scope items verified → completion triad. Suite 704, golden unchanged.

- **2026-07-11 — VWB-36: lens-bridge.md re-reviewed — triage now knows the protocol pin.**
  Board-as-outbox delegation (PROD-14 phase 2 (2); the VWB-26 co-ownership pattern; the board's
  pre-named VWB-30 was stale — serial already consumed — re-serialed at intake). All existing
  lens claims verified true against the live repo (test paths, gates, `$CROSS_REPO_TOKEN`,
  ping-pong guard; slugs already clean from the phase-0 sweep); one addition landed on the
  reports repo (`locveil-reports@6a5dc62`): §2 teaches the protocol-pinned filing surface
  (VWB-37's pin + conformance test — wire-surface changes start with a commons re-pin, never
  bridge-side) and the explicit no-default `reports.repo` (VWB-35). No bridge code touched.

- **2026-07-11 — VWB-37: report-protocol-v1 consumed — the filing surface is pin-validated now.**
  Board-as-outbox delegation (PROD-14 phase 2 (3) / PROD-6, council HK-3), filed + executed same
  session alongside VWB-35/36. The commons machine core pinned byte-identical at the repo root
  (`report-protocol.pin.json`, tag verified before copy — the reports repo's pin convention);
  the `service.py` filing hardcodes (labels, `[bridge-ui]` prefix, bundle name, id source token)
  retired into `REPORT_*` constants; `test_report_protocol_pin.py` (5 tests) locks constants → pin,
  including `system.json` `reports.repo` == `repos.reports` (chains the VWB-35 cutover value to the
  protocol; the emitted-value half was already locked by the envelope test). Suite 704, pyright 0,
  import-linter 6/6, no contract change. Protocol bumps: re-pin first, adjust until conformance
  passes.

- **2026-07-11 — OPS-16: CLAUDE.md harmonization — the shared process layer is pinned blocks now
  (scope-v3).** Third board-as-outbox delegation (PROD-5 / HK-2), pre-assigned this ID with a REDEFINE
  flag — reconciliation confirmed the old text stale on three counts (`check_scope.py` gone with
  OPS-22; the separate-drift-guard-script plan dead — HK-2 put a `claudemd` hash rule INSIDE
  scope-guard; the split-in-two rename superseded by rename-apart) — owner approved the redefine
  before execution. Landed: both digest blocks (`shared-invariants`, `cross-repo-board`) inserted
  byte-identical between `locveil:begin/end` markers; the ~55 lines of duplicated long-form
  ledger/rotation/guard mechanics they replace came out, shared invariants now carry dialect-only
  bullets — CLAUDE.md 175 → 164 lines (the HK-2 hard criterion: adoption must not grow the file).
  Preamble de-lied (the "single source of truth = this file" claim was false since HK-1); the retired
  uncommitted-intake clause in `cross-repo-source-of-truth` replaced with board-as-outbox vs
  direct-operational-filing (and commons acknowledged as co-owned ground — the old "never write into
  `locveil-commons`" wording contradicted the board convention). **`config-master-canonical` →
  `config-master-tree`** (HK-2 rename-apart; frozen design-doc mention annotated, not rewritten).
  Guard re-pinned at **scope-v3** (1.1.0): `[claude]` hashes match commons' pins exactly; tamper test
  fails correctly (CLAUDE-BLOCK drift, exit 1); CLAUDE.md added to the CI `ledger` paths-filter. The
  board block closes the "sessions search for the board process" gap HK-2 named. Consumption noted on
  the board journal; PROD-5 stays `[>]` until voice's BUILD-23 lands.

- **2026-07-11 — OPS-22: scope-guard cutover — the commons ledger guard is the law here now; found +
  fixed a v1 rotation bug upstream (scope-v2).** Second board-as-outbox delegation consumed (PROD-13 /
  HK-1, verified at intake: tool + starter config green on this tree, tag matched HEAD, retirement
  targets where claimed). Cutover: `scripts/scope_guard.py` vendored + `.scope-guard.toml` (aliases +
  tombstones ON), old `check_scope.py` deleted only after the vendored tool proved green, CI
  `ledger-guard` + paths-filter re-pointed, committed pre-commit hook live (`core.hooksPath hooks` —
  every commit in this session passed through it), CLAUDE.md invariants updated (scope-guard as
  enforcer; DONE-ledger rotation rule new: 3000/2000/4000). **The first real `--rotate journal` run
  caught a scope-v1 defect**: archives written character-per-line AND the kept journal silently
  truncated (`rotate_journal` double-indexed the day-lines list after tuple unpacking — `s[1]` on an
  already-unpacked list, so `join` iterated a string). No history lost (git restore). Fixed in commons
  per the convention (never patch the vendored copy), validated on a copy of this tree with a
  line-by-line diff (zero loss), tagged **scope-v2** = 1.0.1 — also adds the explicit `--check` flag
  the docs promised but v1 lacked. Re-pinned here at v2; rotation then executed for real as its own
  commit: 1625 → 990 lines, 6 day-sections (2026-06-09…07-04) frozen to `docs/archive/journal/`.
  Board: OPS-22 written back into PROD-13; both delegations re-pointed at scope-v2 — **voice must pin
  v2**, its own overdue rotation would have hit the identical bug. Guard fully green post-rotation.

- **2026-07-11 — OPS-21 controller cutover CONFIRMED on hardware.** Owner flipped the new GHCR packages
  public (org-policy fix: allow public package creation) and ran `ops/migrate-to-locveil.sh` on the WB7:
  migration successful — `locveil-bridge` + `locveil-bridge-ui` running from
  `/mnt/data/locveil-bridge-config` under `locveil-bridge.service`. No wb-mqtt naming left on the box
  (the WB system services `wb-mqtt-serial`/`wb-rules` are Wirenboard's own, not ours).

- **2026-07-11 — OPS-21: deployment identity renamed — after the migration script runs, nothing on the
  controller says wb-mqtt.** Second act of the rename day, coordinated with voice BUILD-29 (owner call:
  finish the re-pointing down to the metal). Images → `locveil-bridge` + `locveil-bridge-ui`, containers,
  unit (`locveil-bridge.service`), runtime tree (`/mnt/data/locveil-bridge-config`), clone path, INSTALL
  flow, and the Python distribution (`locveil-bridge`, alias script renamed, `wb-api` + import package
  kept). New `ops/migrate-to-locveil.sh` performs the one-time controller cutover (old unit out → tree mv
  with state/.env intact → update.sh under the new identity → new unit in → old images dropped).
  Sequencing for the owner: dispatch one CI publish, flip the two new GHCR packages public, THEN run the
  migration script on the WB7 (both repos' scripts, either order). Backend suite 698 on the renamed
  distribution; eval cli 4/4; check_scope green.

- **2026-07-11 — OPS-20: the repo is `locveil-bridge` now — first board-as-outbox delegation consumed;
  OPS-21 filed.** The Locveil productization arc reached this repo: name locked (**Locveil**), org
  `locveil`, all three repos + local dirs renamed, commons restructured (`locveil-commons@52126da`,
  board live). The delegation was pulled from `locveil-commons/board/BOARD.md` PROD-2 (the mechanism
  D-5 designed — this retires the uncommitted-filing pattern that VWB-29/CORE-7/OPS-14/15/16 used as
  its deliberate last case), verified against live code, filed as OPS-20, executed: eval re-point to
  `../../locveil-commons/eval` (cli 4/4), `backend/.venv` rebuilt (the dir rename had bricked every
  console-script shebang — the voice session hit the identical failure), operative name sweep,
  `domovoy`→`locveil` container user (inert until the next image rebuild), GHCR pull refs →
  `ghcr.io/locveil/*` (**owner: run one CI publish before the next controller `update.sh`**, or the
  compose pull 404s). Deployment identity (image basenames, container/unit names, controller paths,
  the `wb-mqtt-bridge` distribution name) deliberately unchanged → OPS-21, to be coordinated with
  voice BUILD-29. Backend suite 698 passed post-rebuild; `check_scope.py` green; OPS-20 written back
  to the board (PROD-2 closes).

- **2026-07-10 — RELEASE 1 CUT: v0.6.0. VWB-16 → `[deferred]`; every `[release]` task now closed.** —
  the owner's call at the tag: VWB-16 (the voice-crossover consumer contract test) waits on the voice
  repo's TEST-18 fixtures, so it moved `[release]` → `[deferred]` — release 1 doesn't hang on a sibling
  repo (it lands whenever the fixtures do; golden `5622ba7a` is stable). With that, **every `[release]`
  task is `[x]`** and the scope gate is clean. Version bumped **0.5.0 → 0.6.0** across all nine
  touch-points (pyproject, `__version__.py`, uv.lock, ui/package.json, README, openapi ×2, STAMP,
  ui types); golden catalog unchanged (version-independent → no voice re-pin). Annotated tag **`v0.6.0`
  — "the house runs on the controller"** cut on the release commit. Structure mirrors the voice repo
  (0.x pre-1.0, annotated descriptive tag). The bridge's first real release since the year-old,
  pre-transformation `v0.5.0` — 651 commits of hexagonal rebuild, canonical-first, scenarios, the
  HVAC driver, and WB7 production deployment.

- **2026-07-10 — REL-4 DONE (the release docs pass — the last critical-path gate before the tag)** —
  user-facing docs made true at the release version, in the voice project's spirit, and scrubbed of
  ALL internal tracking language. Scope reconciled with the owner mid-task: the master-doc/governance
  half descoped to the board (with VWB-33/34); `project.md` archived (not user-facing); `docs/planned/*`
  ruled not-user-facing (excluded, and user-facing docs no longer link into them). Method: a four-way
  parallel doc audit against reality (architecture/guides/READMEs/ADRs) — 8 accurate, 11 fixed. Fixed
  the driver count (8→9), canonical-first framing (`/canonical` public, `/action` internal), DOC-11
  (ui.md scenario-manifest → render-projection-over-Scenario-Manager), the deployed/HVAC/reports status
  in the READMEs, contracts realism-check; added `docs/QUICKSTART.md` (safe-by-construction tester
  guide); amended ADR 0006 + filed OPS-19 (unmirrored pyatv git source); regenerated the 2 stale
  diagrams (canonical write path). A hard lesson mid-pass, owner-flagged twice: **user-facing docs must
  NEVER carry task IDs / `§P3.7` refs / plan pointers** — I'd leaked three into ui.md; the final sweep
  verifies the whole set clean. No code, no contract change. Board: **only VWB-16 (voice fixtures,
  off critical path) now stands between here and the tag.**

- **2026-07-10 — VWB-33 re-tagged `[release]` → `[deferred]` (owner: doesn't belong in release 1)** —
  at the start of the VWB-33 design session (scope/plan explained; reconciliation confirmed the
  analysis — labels are en/ru fleet-wide except HVAC's de/en/ru, aliases ru-only 35/78, names carry
  de on 65/78), the owner stopped it: the language-data convention is **half the voice side's** (the
  verbs-are-donations rule binds their repo), so it's a **board-level cross-repo design session** —
  one of the first tasks after the Domovoy board is established, sibling to VWB-34. Not a release-1
  gate. Effect on the board: **REL-4 is now the SOLE remaining `[release]` gate before the tag**
  (VWB-16 waits on voice TEST-18 fixtures, off the critical path). Critical path collapses to
  **REL-4 → tag**.

- **2026-07-10 — REL-3 DONE (the converged rack pass — two sittings, one critical bug found and
  killed, house verified)** — closed on owner confirmation that the SCN-11 dialog reads in sync after
  the SCN-15 redeploy. Two sittings the same day: #1 flagged the eMotiva ARC-window wedge (the
  headline finding the whole mock-tested suite structurally couldn't reach) → DRV-30 + SCN-14 shipped;
  #2 proved the wedge gesture now passes and closed its lone flag with DRV-33 + SCN-15. Verified live:
  bridge healthy + non-root + golden `5622ba7a1a78102a` byte-match, the full scenario lifecycle on
  hardware, the HVAC live pass, force/reconcile, mf_amplifier mute, end-to-end after cleanup, CI green.
  Deferred-not-gating items enumerated in the DONE entry; `tv_on_speakers` expected-fail until DRV-32.
  Five fixes were born and shipped from this one pass (DRV-30, SCN-14, DRV-33, SCN-15 + the DRV-31/32
  filings) — the rack paid for itself many times over. Only REL-4 (+ VWB-33) now stands before the tag.

- **2026-07-10 — SCN-15 DONE (all comparison sites unified on `_satisfies` — the DRV-33 flag's tail)** —
  post-DRV-33 the SCN-11 dialog still called the TV "out of sync" (`'HDMI_2'` vs `'hdmi2'`, owner
  screenshot): SCN-14 had canonicalized only the execution gate; the build_plan diffs and the
  preview's `in_sync` still compared raw. One shared `_satisfies()` (exact → value table →
  normalized) now serves the gate, all five diff sites, and both preview comparisons — the dialog
  reads honest wire-form state as in-sync and plans stop emitting phantom TV-input steps at the
  source. Fresh logs confirmed zero gate timeouts since the DRV-33 redeploy (the execution side was
  already clean). +2 tests (the screenshot case; diff already-satisfied across vocabularies). Full
  tree 698, pyright 0, 6/6.

- **2026-07-10 — REL-3 sitting #2: 21 ok · 1 flag · 11 not run — THE WEDGE GESTURE PASSED; the one
  flag root-caused and fixed same hour (DRV-33)** — START `movie_zappiti` with the TV already ON
  (yesterday's rack-killer) ran clean end-to-end; both switches executed against a live eMotiva for
  the first time; END + restart-survival green; force/reconcile station mostly green. The single
  flag (SCN-11 force on the TV showing believed input `Emotiva XMC` + a gate timeout) decoded from
  logs: the LG driver's optimistic input write stored the user-assigned input LABEL while the webOS
  event path writes the ID — with the TV already on target there's no correcting event, so the label
  stuck, every switch re-dispatched the TV input as a phantom diff, and SCN-14's honest gate
  faithfully reported the pre-existing lie (actuation itself always worked). One-line fix
  (`input_id` in the optimistic write) + pinned-behavior test corrected + an already-on-target
  regression. Full tree 696, pyright 0, 6/6. Remaining not-run items ride sitting #3 / owner triage.

- **2026-07-10 — DRV-30 DONE (eMotiva hardening: readiness gate + keepAlive watchdog + re-subscribe
  on recovery)** — the wedge class is closed at the driver, notification-driven throughout:
  `set_input` holds inside the post-power-on window (2 s quiescence normally; the **ARC-exit case
  holds the full 15 s** — design sharpened mid-implementation: the incident wedged 3.3 s of silence
  AFTER the `'arc'` claim, so quiescence alone is provably insufficient); `Property.KEEPALIVE`
  subscribed with the device-advertised 7500 ms interval — 3 missed beats → `connected=False` +
  speakable fail-fast on all six handlers (`force` bypasses) instead of blind 9 s retry burns; the
  recovery probe IS a re-subscribe (device-side subscriptions die with the device — the F4 deafness),
  ack re-seeds state. No new state field — contracts byte-identical. +9 tests; suite 595, pyright 0,
  6/6. Pre-release mandate complete (with SCN-14): scenario startup is safe as-if-ARC-worked; the
  ARC bench itself rides DRV-32 post-release.

- **2026-07-10 — SCN-14 DONE (reconciler gates: canonical comparison + timeout = failed step)** — the
  gate now translates the device's reported state through the capability's value table (carried on
  `PlannedAction`) with a normalized-comparison fallback for tableless drivers — the LG's
  `'HDMI_2'`/`'HDMI2'` finally satisfy a `'hdmi2'` target instead of burning 3 s per activation
  (F2, the gate had NEVER confirmed); and a `feedback:true` gate timeout is a **failed step** in the
  result (dispatched-and-acked stays in `executed`, unconfirmed lands in `failures`) — no more
  `tv_on_speakers` false successes (F3). Feedback-less steps stay optimistic. +4 gate tests; the
  force-endpoint fakes now reflect commands into state (honest gates correctly fail static fakes).
  Suite 586, pyright 0, 6/6; contracts untouched. `tv_on_speakers` now honestly fails until DRV-32.

- **2026-07-10 — REL-3 sitting #1: the eMotiva ARC-window wedge — investigated live, findings frozen,
  DRV-30/SCN-14 (P0) + DRV-31/DRV-32 filed** — the first rack pass (13 ok · 4 flagged · 14 not run)
  flagged a critical: START `movie_zappiti` with the TV already on wedged the XMC-2 hard
  (wall-unplug). Same-day forensics (owner-copied container logs + five controlled power-ups + a
  remote OSD probe over the protocol's menu system, `scripts/emotiva_menu_probe.py`): the bridge
  fired `set_input` **3.3 s into a CEC/ARC handshake its own state already showed**
  (`input_source='arc'` at power-on +0.4 s); the safety spacing between `processor.power` and
  `processor.input` was only ever accidental (edges collapse on a warm TV + the TV gate burns a
  dead 3 s — the gate compares canonical `'hdmi2'` to wire `'HDMI_2'` and has NEVER confirmed);
  `tv_on_speakers` reported success twice while ARC never engaged (gate timeout is advisory);
  the wedge+unplug also wiped the device-side subscriptions (bridge deaf until restart) AND
  **reset the XMC-2's CEC config to disabled** (panel read: Enable + Audio to TV off) — the
  afternoon's "ARC is dead" mystery. Evidence: `docs/review/rel3_rack_findings_2026-07-10.md`.
  Filed: **DRV-30** (readiness gate + keepAlive 7500 ms watchdog + re-subscribe on recovery,
  P0 release), **SCN-14** (gate canonicalization + timeout→failed-step, P0 release), DRV-31
  (Zappiti IR toggle miss, deferred), DRV-32 (CEC restoration + ARC bench, deferred HW-GATED —
  owner decision: post-release). REL-3 sitting #2 after the P0s land.

- **2026-07-10 — SCN-13 FILED; the two-room drill leaves REL-3 (user catch on the checklist)** —
  the REL-3 checklist still carried the SCN-6-owed two-room concurrency drill, but second-room
  scenarios went post-release at the 2026-07-04 SCN-4/SCN-6 amendment ("a future round") and the
  move never propagated into REL-3's entry or the release-definition item 2 — and every configured
  scenario is `living_room`, so the drill has nothing to run against. Fixed all three places:
  REL-3 entry + definition item 2 now point at SCN-13 (`[P2]` `[deferred]`: author the
  children-room set, then the concurrency drill SCN-6 recorded as owed); the rack-checklist
  artifact dropped the station (same URL). REL-3's HVAC line also refreshed — the DRV-26/28/29
  driver arc superseded the old VWB-14/24 read-path watch.

- **2026-07-10 — VWB-34 FILED (publish confirmation-timing in the contract — design, post-release)** —
  off the DRV-29 post-mortem: "your HTTP timeout must exceed 15 s" is contract information delivered
  out-of-band in a handover note — retune a gate and voice breaks silently again. Filed `[P2]`
  `[deferred]`, cross-repo, intended for board delegation once board-as-outbox lands (voice co-owns
  consumption; scenario startup as a durable action on the voice side is on the table). Three tiers
  sketched: per-capability `confirm_timeout_ms` (the latency promise, not the internal gate), scenario
  `max_duration_ms` as a derivable upper bound (diff-dependent duration means an estimate would lie;
  progress belongs to SSE), and the async-job pattern for composites as the design's open call.

- **2026-07-10 — DRV-29 DONE (voice-filed; canonical echo window honors the capability gate)** — the
  first DRV-28 live smoke («выключи кондиционер в детской») worked but 503'd: the flat 500 ms
  `CANONICAL_ECHO_TIMEOUT_S` vs the firmware's multi-second confirm rotation (~7 s observed). Fix:
  the endpoint now waits `cap.gate.poll_timeout_ms` when set (the per-capability timing the
  reconciler always honored; LgTv 8000 benefits too), else the 500 ms relay default. The 15 s HVAC
  window is DERIVED from the firmware source (1 s packet gate + 6×2 s info rotation ≈ 13 s worst
  case), not guessed — user requirement. Gates set on all six MitsubishiHvac capabilities. NO
  contract change (gates aren't catalog surface — no re-pin). +2 endpoint tests; suite 682, pyright
  0, 6/6. Voice's wait:true now speaks truth ~7 s late (their client timeout must exceed 15 s) or
  they opt into wait:false — their call. Backend-only rebuild owed.

- **2026-07-10 — VWB-32 DONE (retained catalog-version published at startup + on reconnect)** — the
  topic was `/reload`-only, so the persistence-less broker's restarts left it missing (voice's
  staleness gate blind). New generic `on_connect_callbacks` seam on `MQTTClient` (fires after each
  (re)connect's subscriptions, failure-isolated — the reusable home for wipe-surviving retained
  state); bootstrap registers the catalog-version publish + calls it once at boot; `/reload` publish
  kept. +2 tests (fires on connect + reconnect; raising callback isolated). Suite 680, pyright 0,
  6/6. Rides the pending image rebuild.

- **2026-07-10 — UI-16 DONE (enum-value icons via the shared IconResolver) — closed through a
  three-iteration ARTIFACT review** — a live review page (per-icon approve/alternate/comment + a
  general-instructions field; the user's exported review drove each next iteration, same URL
  republished). All 27 HVAC values settled: Material 1:1s (AcUnit/WaterDrop/WbSunny/Nightlight/
  arrows/SyncAlt), the custom number ladder for speeds+positions, the AV remotes' power pair, and
  FOUR new custom SVGs from the review: `auto-recycle` (♻-faithful chasing arrows), `swing-v`/`swing-h`
  (DETACHED-ray fans — the user rejected the joined pivot, matching ⚟'s implied convergence;
  orientation-aware pair), `center-bar` (the "keyboard |"). Color rule pinned: dry/cool/heat carry
  fixed colors (#42A5F5/#4FC3F7/#FFB300), everything else theme ink (currentColor). Code:
  `IconResolver.valueIconMappings` + `resolveValueIcon()` (scoped `capability.value` → bare fallback),
  4 custom components registered, `HvacPanel` glyph maps deleted → `ValueIcon` component. UI-only;
  check+build green; rides the pending DRV-28 image rebuild.

- **2026-07-10 — DRV-28 DONE (the `MitsubishiHvac` driver) — the ACs are bespoke devices** — the
  DRV-27 rev. 2 design implemented in one sitting: typed state + VWB-18 restore-at-boot (the
  broker-wipe fix), heartbeat reachability off the firmware's 45 s room_temperature publish (no LWT
  exists), canonical→numeric-wire commands via the attached class map through `idempotence_skip`,
  and — the user's hard requirement, twice-enforced and test-pinned — **never creates a WB virtual
  device** (the firmware owns its card). Translation stack extracted to the shared
  `value_translation` module (passthrough delegates; the `independence` contract stays clean, the new
  package added to it). Configs moved to `config/devices/` root (explicit commands, bare
  state_topics, `temperature`→`setpoint`); `profiles/hvac.json` deleted; `classes/MitsubishiHvac.json`
  = six capabilities. Catalog derivation extended: `set {value}` params inherit the state-field table
  (VWB-24's zero-round-trip property preserved under the new convention). HvacPanel reworked;
  device-state-mapping + OPENAPI_EXTRA_MODELS registered (user catch). **Golden → `5622ba7a1a78102a`,
  openapi +1 schema — voice re-pins ONCE (DRV-25+26+28), and their intent mapping must follow the new
  six-capability vocabulary, not just the hash.** Suite 678, pyright 0, 6/6, UI green, eval cli 4/4.
  Docs: devices-and-scenarios (nine drivers), README, howto-new-device. Owed: image rebuild + WB7
  redeploy; HW check rides REL-3.

- **2026-07-10 — DRV-27 DONE again (design rev. 2: six capabilities + explicit topics)** — the
  reopened discussion settled all three forks (user-pinned): **six per-domain capabilities**
  (power/mode/fan/vane/widevane/temperature — the AV action-group shape, 1:1 with the firmware's
  controls; `reconcile:false` explicit); **`{value}` param convention** on every enum/float `set`
  (mirrors VWB-19 select-form — one shape for voice); **today's config schema kept** (explicit
  per-action `commands` + bare `state_topics`, tables in the class map via the DRV-25 enrichment),
  the 3 configs moving out of `wb-devices/` to `config/devices/` root, driver validates completeness
  at load. Owned consequences: canonical HVAC vocabulary changes (golden bump, single voice re-pin
  after DRV-28) + `HvacPanel.tsx` rework. Design doc rewritten (rev. 2); DRV-28 amended to match.

- **2026-07-10 — DRV-27 REOPENED (design review: decomposition + explicit topics)** — the user read the
  design doc and rejected (1) the single `climate` capability — must decompose into per-domain action
  groups like LgTv/kitchen-hood/eMotiva, and (2) topic derivation from one `mqtt_device` field — device
  configs must carry explicit MQTT topics per action, and the 3 HVAC configs move out of `wb-devices/`
  to `config/devices/` root (bespoke home). Settled decisions stand (name, class-map-only tables, no WB
  card, UI-16 icons, heartbeat, restore). Discussion continues; DRV-28 amended on re-close.

- **2026-07-10 — DRV-27 DONE (design: `MitsubishiHvac` dedicated driver) + DRV-28/UI-16 filed** —
  interactive design session (`docs/design/mitsubishi_hvac_driver.md`). User-pinned decisions: name
  **MitsubishiHvac** (not "ESP32ManagedDevice" — the modules are ESP8266 and the contract is the
  mitsubishi2wb firmware dialect); capability map `profiles/hvac.json` → `classes/MitsubishiHvac.json`
  (profile dies, `reconcile:false` explicit); **tables in the class map ONLY** (driver translates via
  its attached capability map — amends the tables-in-code premise); device configs shrink to identity +
  `mqtt_device`; typed state → VWB-18 restore-at-boot + **heartbeat reachability** (the firmware's
  unconditional 45 s room_temperature publish; no LWT exists); NO bridge-owned WB card (the ESP32's raw
  card "is fine like it is"); enum-value icons = the AV IconResolver mechanism → **UI-16**
  `[P2][deferred]`. **DRV-28** filed `[P1][release]` (user pulled the implementation into the release
  run; the voice golden re-pin consolidates on its landing). REST-firmware horizon recorded in §8
  (bench-twin-gated, unfiled). No code.

- **2026-07-10 — DRV-26 DONE (HVAC value tables → firmware numeric wire; control revived)** — the
  VWB-14 tables carried label strings as `wire`, but mitsubishi2wb speaks numeric indices and silently
  drops anything else — HVAC mode/fan/vane/widevane was dead in both directions. Fix: `wire =
  str(position)` (declaration order already equals the firmware index) in the `hvac` profile + the 3
  device configs; canonical vocab + labels unchanged. Drift-guard tests updated to the firmware truth
  (+ a generalized every-table-is-indices guard); 2 both-direction tests on the real children config
  (`'2'→'cool'` in, `cool→'2'` out). **Golden → `a4a2b1aed5f86447`** (openapi byte-identical) — voice
  re-pins ONCE to this (covers DRV-25 + DRV-26). Suite 664, pyright 0, 6/6. DRV-27's firmware premise
  amended (user reversed: REST-endpoints rewrite under research, bench-twin-gated, ESP8266 D1 Mini
  confirmed). Live set_mode check rides REL-3.

- **2026-07-09, night — the HVAC-cards incident: root cause pinned (bridge exonerated); DRV-26 + DRV-27
  + VWB-32 filed** — all three WB-UI «Кондиционер» cards showed valueless controls (sliders pegged at
  max, blank setpoint, red ⊗); initial suspicion fell on the day's DRV-23/25 deploys. Investigation
  (live broker forensics + container logs + the `../mitsubishi2wb` firmware source): **the controller
  rebooted at 14:37 MSK and mosquitto runs WITHOUT persistence → every retained message was wiped.**
  Everything that actively republishes recovered (wb-mqtt-serial poll, wb-rules, ESP32 `meta` + periodic
  `room_temperature`); the mitsubishi2wb ESP32s republish interactive control values **only on change**
  (`mqttConnect()` re-sends meta only — confirmed in source), so their cards stayed empty until poked
  (living room self-healed after voice's command; children via panel power-cycle; bedroom = the still-
  broken witness that proved it: meta intact, values absent). The bridge's total output to HVAC topics
  all day: ONE non-retained `power/on: 0`. **Decisions:** mosquitto persistence stays OFF (user, per WB
  community advice) → robustness moves above the broker. **Found en route, filed:** **DRV-26** `[P1]
  [release]` — the VWB-14 HVAC value tables have label-string wire values but the firmware speaks
  numeric indices AND silently drops non-numeric commands ⇒ HVAC mode/fan/vane/widevane control is
  currently DEAD both directions (definitive numeric tables extracted from `hpSettingsChanged()`);
  **DRV-27** `[P2] [deferred]` — dedicated ESP32-HVAC driver design (typed declared state → restore-at-
  boot covers the wipe; firmware tables in code; optional bridge-owned labeled WB card; the VWB-11-
  anticipated migration, user-directed); **VWB-32** `[P1] [release]` — `bridge/catalog/version` is only
  published from `/reload`, so the wipe left it MISSING on the live broker (voice's staleness gate reads
  it) — publish retained at startup + on reconnect. No code in this entry — investigation + filings.

- **2026-07-09 — DRV-25 DONE (WB-passthrough state → top-level fields; switch `power` readable)** —
  pulled forward from `[deferred]` (maintainer: a nasty bug that must not ship in release 1). Retired
  the `mirrored` bucket: `WbPassthroughState` is `extra="allow"`, `_on_value_message` sets each coerced
  value as a top-level field, `BaseDeviceState.model_dump` merges `__pydantic_extra__`, idempotence reads
  `getattr(state, field)`, + a collision guard. `light_switch`/`dimmable_light`/`power_switch` `power` →
  stateful+readable+`reconcile:false`+`'1'↔'on'` value table; a loader step enriches bare
  `state_topics[field]` from the profile field. `HvacPanel` migrated off `state.mirrored.*`. **Contract:
  golden `8159b4b0068d1c63` → `16eee0f2f7832995`** (power readable); openapi byte-identical (the `/state`
  endpoints aren't openapi-typed, so `WbPassthroughState` isn't in the schema) — only golden+stamp moved,
  **voice re-pins to `16eee0f2f7832995`.** Suite
  662, pyright 0, import-linter 6/6, UI check+build green. Doc: `devices-and-scenarios.md` (state
  surfaces top-level). Supersedes DRV-23's projection. Live re-verify owed to the redeploy.

- **2026-07-09 — DRV-24 design REVISED (converged: retire `mirrored`)** — maintainer review of the first
  cut ("why mirror stateful values — isn't it duplication?") landed: the `mirrored` bucket is the generic
  driver's substitute for typed fields, and the driver never holds a logical value apart from the mirror,
  so for passthrough the mirror *is* the state and the projection was a workaround. Revised D3 to **retire
  `mirrored`** — passthrough state moves to top-level dynamic fields (`extra="allow"`, the AV-driver shape),
  subsuming DRV-23's `model_dump` projection and dropping the `power` duplication. Now an openapi + golden
  change + a UI `HvacPanel` migration. `docs/design/wb_passthrough_readable_power.md` + the DRV-25 entry
  updated to the converged scope. No code (design only).

- **2026-07-09 — DRV-24 DONE (design) + DRV-25 filed — WB-passthrough readable/authoritative power** —
  follow-on to DRV-23 from the voice side's live re-test. Design (`docs/design/wb_passthrough_readable_power.md`)
  for making `state.power` authoritative + readable on the 39 momentary-power WB-passthrough switch devices
  (`light_switch`×24, `dimmable_light`×13, `power_switch`×2): (D1) profiles' power → stateful readable with a
  `'1'↔'on'` value table; (D2) load-time enrichment so a device's bare `state_topics[field]` inherits the
  profile field's type/values (declare once; also fixes the DRV-23 `mode` raw-`'0'` sibling); (D3)
  `_on_value_message` sets the declared top-level `power` from the coerced canonical value. Scope pinned to the
  WB-passthrough power capability only. Golden re-pin on implementation (cross-repo). Implementation = **DRV-25**
  (`[P2][deferred]`, picks up with the voice features that need it — no release-1 gate).

- **2026-07-09 — DRV-23 DONE (voice-filed; WB-passthrough state projected to top-level)** — filed by the
  voice repo per `cross-repo-source-of-truth`; verified against live code + the running WB7, reframed, and
  fixed same session. **Read path was genuinely broken:** WbPassthrough wrote feedback only into
  `state.mirrored`, so voice reading top-level `state.<field>` (ARCH-8 contract) got `None` — «какая
  температура в кабинете» failed though `room_temperature=24.125` sat in `mirrored`. **Write-path claim
  disproven** by a live on→off→off repro (`power.off` fired, `no_op:false`; idempotence already uses
  `mirrored`). **Scope: WbPassthrough-only, 39/65 devices** (live sweep; `mirrored` exists only on that
  state class; all other classes clean). **Fix:** `model_dump` override on `WbPassthroughState` lifts each
  mirrored value to a top-level key (serialized-view only — mirrored stays the source of truth, declared
  fields like `power` not shadowed). +2 tests, suite 662, pyright 0, contracts 6/6, OpenAPI byte-identical.
  Live re-verify owed to the redeploy.

- **2026-07-09 — VWB-30 DONE (REL-5 reports hardening #13/#14/#15/#16, pulled forward)** — reports is
  live on the WB7, so four gaps fixed: redaction now masks the whole value under a credential-shaped key
  (was leaking non-secret-keyed leaves), `redact_text` masks URL-embedded creds (`scheme://user:pass@`),
  `report_id` gained a uuid suffix (spool-filename collisions), and the browser `actionLog` takes the
  newest N not the oldest (`slice(0,N)` — the store unshifts). +3 backend tests; pyright 0, 6/6, UI green.

- **2026-07-09 — UI-13 DONE (REL-5 #7 P1 fix: SSE never gives up)** — `useEventSource` now falls back to a
  60 s keepalive reconnect after the fast back-off budget is spent (was dead-until-reload after ~10
  attempts, freezing wall-panel device state). UI check + build green.

- **2026-07-09 — UI-14 DONE (REL-5 #17/#19, pulled forward)** — removed the shipped TEMPORARY-DEBUG branch
  that surfaced every unknown SSE event into the progress feed; `ForceReconcileDialog.close()` now clears
  `pending` so a quick reopen during an in-flight force isn't stranded. UI check + build green.

- **2026-07-09 — SCN-12 DONE (REL-5 #6 P1 fix: teardown honours reconcile:false)** — `build_power_off_plan`
  gained the `reconcile` guard `build_plan` already had, so a graceful switch/shutdown no longer emits an
  IR `power_off` to a `reconcile:false` device (the upscaler). One-line fix + test (upscaler ON → empty
  power-off plan). pyright 0, import-linter 6/6.

- **2026-07-09 — DRV-21 DONE (REL-5 #3 P1 fix: select-form forwards force/assume_state)** — canonical
  select-form `set` dropped the reserved cross-cutting params (`CapabilitySelect.expand` takes only
  `value`), so the UI re-tap-to-force escape hatch was dead for AV inputs. New domain constant
  `RESERVED_PARAMS`; the dispatcher overlays force/assume_state onto each expanded select-form step
  (mirrors the actions-form path; step params win). 3 tests in `test_select_canonical.py`; pyright 0,
  import-linter 6/6 (presentation→domain only).

- **2026-07-09 — CORE-9 DONE (REL-5 #2 P0 fix: MQTT reconnect budget per-episode)** — `_run_mqtt_client`
  reset `retry_count = 0` on each successful connect, so `max_retries` bounds retries within one
  failed-to-connect episode instead of across the process lifetime (five lifetime blips no longer
  permanently kill MQTT). New `test_mqtt_reconnect.py` (connect-then-drop 8× > max_retries, asserts the
  loop keeps reconnecting). pyright 0, import-linter 6/6.

- **2026-07-09 — REL-5 DONE (pre-tag code review) + 11 remediation tasks filed** — the code-review
  half of the release gate, split out of REL-3 and run off the rack. A multi-agent review (7 subsystem
  reviewers × 4 dimensions, every finding adversarially re-verified — 27 agents, 0 errors) over the
  release-critical backend + UI. **20 raw → 19 confirmed (1 refuted): 2 P0, 5 P1, 12 P2.** Frozen
  evidence: `docs/review/rel5_pretag_review.md`. Two P0s stand out — the broker password committed in
  `system.json` and served unauthenticated on the LAN (#1), and the MQTT reconnect counter that never
  resets so the bridge permanently loses MQTT after 5 lifetime disconnects (#2). Findings triaged with
  the user and filed as fresh tasks: tag-blocking **CORE-9, DRV-21, SCN-12, UI-13** + pulled-forward
  `[release]` **VWB-30** (reports hardening), **UI-14** (UI nits); deferred **CORE-8** (broker/device
  secret handling — owner scoped it to productization, house on a trusted LAN; password rotation is a
  separate near-term op), **VWB-31, DRV-22, OPS-18, UI-15**. P0/P1 remediation lands before the tag.
  Board now: REL-3 (rack) ∥ {CORE-9/DRV-21/SCN-12/UI-13/VWB-30/UI-14 fixes} → REL-4 → tag.

- **2026-07-09 — OPS-17 DONE (both containers run non-root as uid 1000 `domovoy`)** — user-flagged
  from the voice deployment work ("we run the containers as root, which is wrong"); mirrors the voice
  repo's BUILD-15 uid fix. Backend `Dockerfile`: `useradd -m -u 1000 domovoy` + chown /app + `USER`
  (clean — deps already in `/opt/venv`). UI `Dockerfile` (`nginx:alpine`): `adduser -D -u 1000`
  (BusyBox, not Debian `useradd`) + chown the paths nginx writes at start/run + `USER`; pid redirected
  to `/tmp/nginx.pid` in `nginx.conf.template` (non-root can't write `/run/nginx.pid`). `update.sh`
  chowns the writable mounts (`data/`,`logs/`) to 1000 before `up` (it runs as root; container would
  else EACCES). Name is provisional (tracks the Domovoy decision); the UID is the real identity.
  Verified: a stage-2-only UI build booted nginx master+worker as `domovoy`, pid in `/tmp`, HTTP 200.
  Activation owed to the rack — inert until images rebuilt + `git pull && ./ops/update.sh` (chown and
  the new non-root images land together in that one command). `sh -n` + `compose config` clean; no
  Python change. Docs: `ops/INSTALL.md` non-root note.

- **2026-07-09 — VWB-13 DONE (catalog completeness sweep + bulk end-to-end, on the deployed WB7)**
  — closed on the user's spot-check evidence after reconciling the sweep's two halves against live
  controller reality (`task-start-reconciliation`; user-accepted "close as done now"). Half one
  (**catalog completeness**) was already proven at the REL-2 cutover 2026-07-08: the controller's
  live `/system/catalog` byte-matched golden `8159b4b0068d1c63` (79 devices / 11 rooms, incl. all 8
  `global` aggregates + `scenario_manager_living_room`) — zero deployment drift, every canonical
  action resolvable. Half two (**bulk end-to-end across rooms**) rode the user's controller
  spot-checks 2026-07-08–09: room devices exercised through the deployed bridge, behaving identically
  to the dev-box run (same binary + config tree — REL-2 removed the only variable). The 8 `global`
  aggregates accepted as covered-enough (bar = the canonical call *publishes to the broker*; the wired
  ones actuate the house and several still owe wb-rules backing, so an exhaustive fire-all was
  deliberately not run). Verification task — no code touched. **REL-3 is now unblocked** (its last
  non-done prerequisite). Board: REL-3 → REL-4 → tag; VWB-16 x-repo.

