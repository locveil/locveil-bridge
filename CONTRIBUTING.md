# Contributing

The repo is a monorepo (`backend/` + `ui/` + `wb-rules/` + `ops/` + `docs/`)
maintained solo. The conventions below are what every commit clears; treat
them as the rules of the road, not aspirations.

## Workflow

- **Push directly to `main`** — solo workflow, no PR ceremony.
- **Small, focused commits** — one logical change each. Don't bundle unrelated
  work into a single push.
- **Detailed commit bodies explain the *why*** — what changed, what it
  fixes/removes, with file/line refs where useful. The body is the audit trail
  in lieu of PR descriptions.
- AI-co-authored commits add a `Co-Authored-By: Claude <model>
  <noreply@anthropic.com>` trailer naming the model that helped; pass
  multi-line messages via a HEREDOC.
- **Decisions land in `docs/action_plan.md`** (revision log) and, for anything
  design-shaped, in `docs/design/`. **Superseded docs move to `docs/archive/`**
  (or `docs/design/` / `docs/review/` if they remain authoritative or
  informational), never deleted. (Historical decision records live frozen in
  `docs/archive/adr/`; the class is retired — living policy belongs here or in
  the design docs.)

## Backend — layering (hexagonal, enforced)

Dependencies point inward. When adding code, place it by layer:

- **`domain/`** — pure business logic, **no I/O**; reaches the outside only
  through the ports in `domain/ports.py`. (`DeviceManager`, `ScenarioManager`,
  `RoomManager`, the topology and capability models, the reconciler.)
- **`infrastructure/`** — adapters that implement the ports: device drivers,
  MQTT client, SQLite store, config + capability loaders, WB virtual-device
  emulation.
- **`presentation/`** — FastAPI routers, SSE, request/response schemas.
- **`app/`** — wiring only (`create_app()` + `lifespan`). **`cli/`** — console
  tools.
- A new external dependency goes behind a port + adapter; do not import it
  from `domain/`.

A presentation→infrastructure back-edge currently exists in
`presentation/api/routers/system.py` (`POST /reload` constructs an
`MQTTClient` directly for live reconnect). It is consciously accepted,
documented, and **codified as an `ignore_imports` exception** in the
import-linter config (see below); do not add new ones.

See **[Architecture overview](docs/architecture/overview.md)** for the full
picture.

### Three health gates — CI-enforced via `droman42/py-dev-gates`

The inward rule, no-TYPE_CHECKING discipline, and 0-error type-check are
not conventions you have to remember. **CI hard-fails on all three** via
the shared composite action `droman42/py-dev-gates/.github/actions/python-health`.

1. **`import-linter`** — six contracts in
   `backend/pyproject.toml [tool.importlinter]`:
   1. Domain depends on nothing outward (no `infrastructure`,
      `presentation`, `app`, `cli`).
   2. Infrastructure does not import presentation, the composition
      root, or the CLI.
   3. Presentation does not import the composition root or the CLI.
   4. Utils (the foundation layer) depends on nothing upward.
   5. Device drivers are independent of each other.
   6. Presentation does not reach into infrastructure adapters — one
      exception, the `POST /reload` MQTTClient construction, is codified
      by path in `ignore_imports`.
2. **`check-no-type-checking`** — AST-based gate forbidding
   `from typing import TYPE_CHECKING` and `if TYPE_CHECKING:` guards.
   Such guards are band-aids for import cycles; the right fix is to
   break the cycle (move the shared type inward / use a port), not hide
   the import from the runtime.
3. **`pyright`** — pinned `1.1.410`, **0 errors, empty suppression list**.

Run all three locally before pushing:

```bash
cd backend
lint-imports
check-no-type-checking src/locveil_bridge
pyright
```

If you genuinely need a new infrastructure→presentation or
presentation→infrastructure edge, add it to `ignore_imports` in the
contract AND document the why in the commit body — the suppression list
is the project's audit trail.

## Backend — typed configs + typed state (a hard rule)

- Every device config JSON declares **`device_class`** (the driver) and
  **`config_class`** (the Pydantic config model) — both required, both
  validated non-empty.
- Config models live in `infrastructure/config/models.py` (subclass
  `BaseDeviceConfig`).
- State models live in `domain/devices/models.py` (subclass `BaseDeviceState`);
  every driver is `BaseDevice[StateT]`.
- **No dict-shaped configs or state.** Type everything.

## Backend — formatting + typing

- **black** + **isort** (black profile), line length 88, target py311.
- **pyright** is the type-check gate (see "Three health gates" below).
  Pinned `1.1.410`, config `backend/pyrightconfig.json`, scope
  `src/locveil_bridge/`. **0 errors, empty suppression list.** Type
  hints expected on new code.
- *(Legacy: `mypy` is still installed via `[dev]` but is no longer the
  type-check gate. Removing it is tracked.)*

## Backend — tests (the recipe)

- `pytest`, `asyncio_mode = auto`, `testpaths = ["tests"]`. Markers:
  `unit`, `integration`, `requires_mqtt`, `requires_device`, `slow`.
- **CI runs `pytest -m "not requires_device"`** on amd64. Keep CI-run tests
  free of real hardware + brokers.
- **Device-driver test recipe** (the pattern every driver follows):
  1. Build a typed Pydantic config.
  2. Inject the driver's external client as an `AsyncMock`.
  3. **Bypass `setup()`** (it connects to real hardware).
  4. Prime the connectivity gate (`state.connected = True`, etc.).
  5. Stub network helpers.
  6. Drive `handle_<action>` directly.
  7. Assert the external client was called as expected, `device.state`
     mutations are right, the `CommandResult` shape matches.
- Don't name non-test helpers `test_*` — pytest will collect them. Rename
  CLI helpers to `_check_*` / `_run_*`.

## Dependency policy (backend)

Four rules keep the build reproducible; they govern every dependency decision:

1. **Personal libraries: PyPI exact-pin.** Libraries the project owner authors
   and publishes (`pymotivaxmc2`, `asyncwebostv`) come from PyPI with an exact
   `==` specifier.
2. **Third-party git sources: immutable ref only.** A git dependency must pin a
   full commit SHA (`rev = "..."` in `[tool.uv.sources]`) — never a branch. If
   the upstream repo is not under the owner's control, mirror it under the
   owner's account so the build survives an upstream force-push or deletion;
   when an acceptable PyPI release appears, migrate to it and drop the git
   source. (The owner's `openhomedevice` fork exists because upstream's
   mandatory `lxml` does not build on the ARMv7 deploy target.)
3. **Direct PyPI dependencies: lower bound + next-major upper bound**
   (`>=2.11.0,<3`). Minors and patches flow through a lockfile refresh; a
   breaking major cannot arrive silently.
4. **The committed `uv.lock` is the pin-of-record.** Deterministic restore is
   `uv sync --frozen`; regenerate with `uv lock` only after an intentional
   `pyproject.toml` change, then `uv sync` + a full test run before committing.

Recovery runbook for the git-sourced deps:
`docs/design/maintenance/dependency-recovery.md`.

## The OpenAPI contract (build-time discipline)

The committed `backend/openapi.json` — *not* a running server — is what the
UI build consumes.

- **Regenerate + commit `openapi.json`** (`locveil-openapi -o openapi.json` from
  inside `backend/`) whenever the REST surface or any device-state model
  changes. A test (`tests/unit/test_openapi_schema.py`) guards that every
  `OPENAPI_EXTRA_MODELS` entry actually lands in the schema.
- When the REST surface changes, also regenerate the UI's
  `src/types/api.gen.ts` (`npm run gen:api-types` from inside `ui/`) and
  commit it.
- Full invariants: **[Architecture: UI](docs/architecture/ui.md)** and
  **[Architecture: interfaces](docs/architecture/interfaces.md)**.

## UI conventions

- **No Python in the build.** Device-state types come from the backend's
  `openapi.json`; the UI never imports the Python package.
- **Generated artifacts are gitignored** (`*.gen.tsx`, `*.hooks.ts`,
  `src/types/generated/*.state.ts`, `index.gen.ts`) — built fresh in CI.
  `src/types/api.gen.ts` is committed.
- Before committing UI changes, from `ui/`: `npm run check && npm run build`.
  `check` runs typecheck + lint (`--max-warnings 0`) + the orphan-module
  guard (`scripts/find-orphans.mjs`); `build` (= `tsc && vite build`)
  catches bundle-level regressions the noEmit typecheck misses.
- **Tests deferred**: the jest preset is misconfigured + zero test files
  exist today. CI does not run `npm test` to avoid honest-red noise;
  re-wire once the framework is on its feet (likely a vitest migration).
- **No hardcoded backend IPs or baked URLs.** Backend proxy target is
  runtime (`BACKEND_HOST` / `BACKEND_PORT` via `docker-entrypoint.sh`);
  MQTT URL via `window.RUNTIME_CONFIG` (`/runtime-config.js`). `VITE_*`
  is local-dev fallback only.
- UI layout for a device is chosen by `device_category` (`device` →
  runtime layout manifest via `RemoteControlLayout`; `appliance` →
  bespoke per-class page).

## CI gates each commit must clear

One workflow, path-filtered: a `changes` job detects which areas the push
touched and gates everything downstream, so a commit only pays for the checks
its files can break.

- **`ledger-guard`** (docs/** or the guard script/config changed) —
  `scripts/scope_guard.py --config .scope-guard.toml`, the ledger-discipline
  check (a vendored copy of the shared Locveil scope-guard). The same check
  runs pre-commit via the committed hook — enable it once per clone with
  `git config core.hooksPath hooks`.
- **`backend-test`** (backend/** or contracts/** changed) — the three Python
  health gates (import-linter / no-TYPE_CHECKING / pyright) + `pytest -m "not
  requires_device"` on amd64. The suite includes the **contracts drift guard**
  (`test_contracts_golden.py`): the committed `contracts/catalog/` artifacts
  (golden catalog + pinned openapi) are regenerated in-test and compared — if
  a config or API change alters the contract, regenerate **from the repo root**
  (device cert paths in `config/` resolve relative to it) with
  `uv run --project backend locveil-catalog --stamp contracts/catalog/STAMP.json`
  (and `locveil-openapi` for the schema). A regeneration that moves a pinned
  file is a **contract cut** — bump the contract version first, tag the landing
  commit; the steps are in `contracts/catalog/README.md` → Regeneration, the
  version levels in `contracts/catalog/catalog-contract.md`.
- **`contract-guard`** (contracts/** or its vendored script changed) —
  `scripts/contract_guard.py --check`, the contract-coherence check (layout,
  stamps, pinned-copy hashes; a vendored copy of the shared Locveil
  contract-guard). It runs pre-commit too, via the same committed hook as the
  ledger guard.
- **`ui-validate`** (ui/** changed, **or** the backend contract the UI
  consumes: `backend/openapi.json`, `config/**`) — `gen:api-types` +
  `check` (typecheck, strict lint, orphans) + `build`.
- **Slow (manual)** — QEMU `arm/v7` Docker image builds for backend + UI.
  Triggered via `gh workflow run "Build ARM Docker Images (backend + ui)"`
  or the Actions UI, with per-image toggles (`build_backend` / `build_ui`);
  they don't run on every push because they're ~14 min for the UI. A
  dispatch also runs the matching fast checks — each image build needs its
  gate green.

A docs-only commit runs just the ledger guard; a backend contract change
re-validates the UI too. If you change a Dockerfile or anything in `ops/`,
dispatch the slow workflow before relying on `:latest`.

## How-to references

- **[Add a device with an existing driver](docs/guides/howto-new-device.md)**
  — config-only: the WB-passthrough or one of the shipped driver classes.
- **[Add a new device driver with a native library](docs/guides/howto-new-driver.md)**
  — Python-side: typed config + state, driver subclass, entry-point,
  capability map, tests.
- **[Define a new AV scenario](docs/guides/howto-new-scenario.md)** — thin
  scenario config; the reconciler does the rest.

## Source of truth pointers

- **[Architecture overview](docs/architecture/overview.md)** — the hexagon
  and its ports.
- **[Architecture: key concepts](docs/architecture/key-concepts.md)** —
  topology, capabilities, configs, reconciler.
- **`docs/action_plan.md`** *(internal)* — the living revision log and
  open work.
- **`docs/design/`** *(internal)* — design rationale; user docs derive from it.
  Frozen decision records: `docs/archive/adr/`.
