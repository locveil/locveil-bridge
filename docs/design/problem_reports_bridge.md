# Problem reporting — the bridge side («Report a problem» button)

**Status: AGREED 2026-07-06 (interactive design session, VWB-27). B-1..B-10 user-approved
(B-1..B-3 decided interactively; B-4..B-10 accepted as proposed). B-11 added the same day —
a voice-side amendment (their ARCH-34), verified and accepted at intake. B-12 (button
placement + look) decided interactively the same day, pre-VWB-28.**

The bridge's half of the cross-repo problem-reporting loop designed in
`locveil-voice/docs/design/problem_reports.md` (ARCH-30, AGREED 2026-07-06). The shared pieces —
the private `locveil/locveil-reports` triage home (named `droman42/wb-user-reports` until the
2026-07-11 org transfer, PROD-14/HK-3), the one-Claude-two-lenses choreography, the envelope —
are defined THERE and consumed here unchanged; since HK-3 the wire-visible surface is the
versioned machine core `locveil-commons/contracts/report-protocol/` (pinned at `contracts/pins/report-protocol/` — VWB-37, relocated to the org pin shape by VWB-40). This document decides only what is the bridge's
own: what a UI-originated report collects, how it is assembled and delivered, and what new
evidence infrastructure v1 builds.

## 1. Agreed decisions

- **B-1 — Scope model: scoped details + whole-house live states + today's logs.** A report is
  anchored to the **page context** the button was pressed on (the UI equivalent of the voice
  side's verbatim utterance): the current entity + its topology neighbors (the reconciler's
  `involved` set) contribute their configs, capability maps, and persisted-vs-live state diffs.
  On top of that, **ALL devices' live states** ride along (cheap, and "the problem was actually
  elsewhere" is common), plus the **whole-system backend log** (today's `service.log` + the
  previous rotated file, gzipped).
- **B-2 — Three evidence rings in v1** (all always-on, in-memory, dumped only into bundles):
  the backend **dispatch ring** and **MQTT window**, and the **browser-side rings** (§4).
- **B-3 — One trigger: the UI button.** Collection is fully automatic behind a single
  `POST /reports` endpoint — the user types free text and presses send; everything else is
  assembled server-side. The owner-invoked CLI trigger (attach bridge evidence to an existing
  handed-over ticket) is explicitly **out of v1** — recorded in §8 as the known gap it leaves.
- **B-4 — Browser-side evidence set** (§4 table): the existing `useLogStore` action log, a
  console/crash buffer, an API-call ring, SSE health, app context. **Screenshots are excluded**
  (privacy + weight; the state data says more), as are React-Query cache dumps (redundant with
  the API ring) and any audio/media.
- **B-5 — Redaction** (mirror of the voice design's §4): values of keys matching
  `token|password|secret|key|credential` in configs and log excerpts (the MQTT broker password
  in `system.json` is the known hot item) and `Authorization` headers. Room/device names stay —
  the repo is private; the leak fence guards the public boundary.
- **B-6 — Rate limit, server-side** (mirror of the voice D-7): max 3 reports/hour, 10/day; the
  endpoint answers 429 with a friendly message the UI surfaces.
- **B-7 — Offline path: directory spool.** On GitHub failure the packaged bundle spools to
  `backend/data/reports/`; a retry runs at startup and hourly. No durable-job substrate needed
  at household volume.
- **B-8 — Endpoint + token.** `POST /reports` takes `{free_text, context, ui_evidence}`; the
  backend assembles Tiers A+B (§3), merges the UI's Tier C, redacts, packages, and files the
  shared envelope (one issue + one bundle commit in `locveil/locveil-reports`; title
  `[bridge-ui] <first 60 chars>`; labels `problem-report`, `lens:bridge`, `new`). The
  fine-grained PAT (issues + contents on `locveil/locveil-reports` ONLY) lives in the
  controller's environment — never in the browser. The target repo is an explicit
  `system.json` `reports.repo` value — the schema carries no default (HK-3 q4).
- **B-9 — Ring parameters** (tunable defaults): dispatch ring depth 50; MQTT window filtered to
  `/devices/#` with per-topic last-value dedup, capped at ~60 s / 500 messages (tames sensor
  churn); browser rings ~200 entries each.
- **B-10 — Hexagonal placement.** `ReportSinkPort` in `domain/ports.py`; `GitHubReportSink` and
  the bundle collector as infrastructure/app services; the router a thin presentation adapter.
  The dispatch ring hooks the existing `execute_action` chokepoint; the MQTT window hooks the
  MQTT client adapter.
- **B-11 — The collector is also a READ seam: `GET /reports/evidence`** (voice-side amendment,
  their ARCH-34, accepted 2026-07-06). The same collector that backs `POST /reports` is exposed
  as a read endpoint returning the bundle-shaped evidence (Tier A snapshots + Tier B rings,
  redacted per B-5) WITHOUT filing a ticket. Consumer: the voice collector calls it at
  report-filing time when a smart-home intent was involved and folds the response into the
  VOICE bundle under a `bridge/` subtree — closing the §8 handover-evidence gap automatically,
  at filing time, for the common case. The **evidence envelope shape is the bridge's to own**
  and rides `openapi.json` → the `contracts/` pin (the voice side pins its expectation, one-way
  sync as always). Endpoint details: no caching; a light rate guard (the payload gzips logs —
  heavier than a normal GET); redaction happens before return, exactly as in the filing path.
  Note: the amendment's second claimed consumer — an evidence *preview* in the report dialog —
  contradicts §2's agreed "no draft state" and is NOT adopted; preview stays a possible later
  UX refinement, and B-11 stands on the voice consumer alone.
- **B-12 — Button placement + look** (decided interactively 2026-07-06, pre-implementation).
  The affordance is an **icon button pinned to the navbar's far right** (the centered picker
  row leaves that edge free; part of the app chrome on every page, never overlapping remote
  layouts — a floating button was considered and rejected for shadowing dense control corners).
  Icon: **Material `BugReport`** — the top-down beetle; genuinely an insect (user requirement)
  AND the universal bug-report pictogram; the UI's one icon library (`@mui/icons-material`)
  ships it, so no new dependency (rejected: `PestControl` — more literally a roach but
  extermination semantics; `EmojiNature` — a bee, nature semantics). Resting state: **quiet**
  — `muted-foreground` like the pickers, turning **amber on hover/press** (the manual-notes
  accent — "attention, not alarm"), tooltip «Сообщить о проблеме». No permanent accent (a
  standing amber icon would read like an active alert) and no text label (navbar width).

- **B-12 — Public intake posture: one door, no public issue tracker (PROD-19 / OPS-28,
  owner decision 2026-10-06).** The public repo's GitHub Issues tab is DISABLED — it was an
  unwatched side door (zero issues ever filed) and, locveil-reports being private, the only
  intake channel a visitor could see. Every problem report, from a user or from a developer,
  enters through the report button (§3) into locveil-reports; a public visitor has no filing
  path by design. The same posture holds for voice (its pre-ARCH-30 issue triage is retired,
  BUILD-14), satellite and commons; the private reports repo keeps its Issues — it IS the
  door. `pyproject.toml` carries no `Issues` URL.

## 2. The dialog (UI side)

The "Report a problem" affordance — the B-12 navbar bug button (`BugReport` icon, far right,
quiet with amber hover, tooltip «Сообщить о проблеме») — opens a minimal dialog: one free-text
field («Опишите проблему своими словами» — same wording family as the voice flow), send/cancel.
Send → `POST /reports` → confirmation toast carrying the report id (or the spooled-offline
variant, B-7; or the rate-limit message, B-6). No draft state, no attachments UI.

## 3. What the bundle contains (backend-assembled)

### Tier A — snapshots (existing data, zero new infrastructure)

| Item | Source | Notes |
|---|---|---|
| today's backend log (+ previous rotated file) | `logs/service.log*` | gzipped; driver exceptions, scenario step failures, gate timeouts |
| live device states — **all devices** | `get_current_state()` | includes `last_command` + `error` per device (B-1) |
| persisted-vs-live state diff — scoped set | `state_store.sqlite` vs live | the optimistic-desync detector |
| active scenario per room + manual steps | scenario state | |
| configs — scoped set | `config/` | device configs + capability maps + topology slice + redacted `system.json` |
| catalog version hash, bridge version, platform/arch | retained version topic, system info | the stale-contract detector — same role as in the voice bundle |

### Tier B — backend rings (new, B-2/B-9)

| Ring | Contents |
|---|---|
| dispatch ring | last 50 executed actions: timestamp, source (`ui`/`voice`/`wb`/`scenario`/`system`), device, capability/action, params, result or error — "what the system actually did just before the complaint" |
| MQTT window | recent `/devices/#` traffic, deduped + capped — "what the broker saw" |

### Tier C — browser evidence (posted by the UI, §4)

## 4. Browser-side evidence (B-4)

| # | What | How |
|---|---|---|
| 1 | the in-app action log | `useLogStore` (exists — every dispatch site already writes it); capped ~200 |
| 2 | console + crash buffer | shim: `console.error`/`warn` tap + `window.onerror` + `unhandledrejection` |
| 3 | API ring | `apiClient` interceptor: last ~50 requests — method, path, status, duration, error body on non-2xx |
| 4 | SSE health | connection state per channel + last-event timestamps (the silent-SSE-death detector) |
| 5 | app context | route + entity id, manifest/catalog version, UI build hash, browser UA + viewport, language |

## 5. The issue body (distilled summary)

Triage should usually not need the tarball: free text verbatim (language preserved), the page
context (entity, room), versions (bridge / catalog hash / UI build), a one-glance
persisted-vs-live diff summary for the scoped devices, the last few dispatch-ring entries,
bundle link, report id.

## 6. Config & secrets

| Where | What |
|---|---|
| controller env (`ops/` compose) | fine-grained PAT → `locveil/locveil-reports` only |
| `system.json` | `reports` section: `enabled`, `repo` (explicit — no schema default; required when enabled), rate limits (B-6), ring tunables (B-9) |
| browser | nothing — the UI only ever talks to `POST /reports` |

## 7. Implementation

Filed as **VWB-28** (unblocked 2026-07-06 — the voice side's BUILD-12 provisioned
`wb-user-reports` and live-smoked the full loop): rings (backend ×2, browser ×3 — one is a cap
on the existing store) → collector + redaction + envelope builder → `ReportSinkPort`/
`GitHubReportSink` + spool/retry + rate limit → `POST /reports` router **+ the B-11
`GET /reports/evidence` read seam** (same collector, second thin router; its response schema
is a contract surface — regen `openapi.json` + `contracts/` pin) → the UI dialog + toast →
tests (collector unit tests with a mock sink; redaction cases; an e2e that files against a
temp-dir sink; an evidence-endpoint schema test).

## 8. Later (explicitly out of v1)

- **The owner-invoked CLI trigger** (`attach bridge evidence to ticket N`) — **demoted to the
  residual case only (B-11, 2026-07-06):** the voice side's ARCH-34 calls the B-11 evidence
  endpoint at filing time and attaches bridge evidence automatically whenever the report looks
  smart-home-related, so a voice→bridge handover normally arrives WITH bridge evidence. The
  residual gap — a report not flagged at filing, handed over later — is small enough that the
  owner gathers evidence manually (or curls the B-11 endpoint) if it ever bites; a CLI stays
  unfiled until the volume justifies it.
- Screenshots / raw media (B-4 exclusion), a user-identity channel (the voice design's later
  registry), auto-attach on `lens:bridge` label events.
