# Device-integration convention v1 — design (VWB-38)

**Status: DESIGN AGREED 2026-07-12** (interactive session; the three owner decisions in §5,
the standing HVAC constraint in §2). Executes the HK-4 "convention down, descriptors up"
two-layer contract (board PROD-15, bridge delegation item 2). **Artifacts shipped with this
design:** [`contracts/device-integration/`](../../contracts/device-integration/) — the guide
(`README.md`), `device-descriptor.schema.json`, `STAMP.json`; tagged **`device-integration-v1`**.
Consumers: `locveil-satellite` (pins the artifacts one-way, authors conforming descriptors —
their DES-4), the `EspManagedDevice` driver (DRV-36), the descriptor-pin conformance test
(VWB-39). *(Layout note, 2026-10-05 — VWB-45, `device-integration-v1.2.0`: the normative text
now lives in `contracts/device-integration/convention.md`, a pinned artifact; the folder
`README.md` is an unlocked index and history, outside the pinned set.)*

## 1. Purpose — the two-layer contract

One versioned convention **down** (bridge-owned: the MQTT profile, the REST URL conventions,
the capability vocabulary, the descriptor schema), per-device descriptors **up**
(satellite-owned: one JSON per device, conforming to the schema, carrying the device's wire
surface AND its canonical capability mapping). The bridge is the *generator* on this boundary
(`cross-repo-source-of-truth`): reference artifacts are committed in this repo's
`contracts/device-integration/` and never pushed outward; the satellite pins its own copy and
CI-checks its descriptors against the pinned schema.

**Fully design-time** (HK-4 round 3): no runtime negotiation of any kind. Vocabulary
reconciliation happens at the satellite's design gate (§6); latency is a static descriptor
fact (§4); the single runtime artifact is a retained firmware-stamp topic used as a
**stale-pin tripwire** — monitor-only, never behavior-changing (§3).

## 2. Ownership & applicability

- **Locveil-owned firmware** (the satellite's deck bridges and every future satellite device)
  **MUST** conform: speak wb-mqtt-v1 on the wire, ship a conforming descriptor.
- **External firmware is out of scope by construction** — the mitsubishi2wb ESP8266 HVAC
  modules, stock Wirenboard devices, wb-rules-driven gear. They are integrated the way they
  are today (bespoke drivers, passthrough profiles) and are never retrofitted.
  **Owner constraint, recorded 2026-07-12: the `MitsubishiHvac` driver is untouched by this
  convention.** The only path that ever moves the HVACs onto it is a deliberate, owner-decided
  firmware rewrite — the same event that is the satellite charter's escalation trigger (HK-4).
  If that day comes, the rewritten firmware conforms (most likely via the reserved REST leg,
  §5 D2, with MQTT staying for value sync) and the driver follows as the transport swap the
  HVAC design's horizon section (`mitsubishi_hvac_driver.md` §8) already sketches. Until then:
  nothing changes.
- Pin discipline both ways: the satellite never hand-edits its pinned copy; the bridge never
  writes artifacts into the satellite repo (operational filings — a vocabulary request, a
  conformance bug report — stay repo-to-repo as normal intake).

## 3. The wb-mqtt-v1 profile — promotion record

Promoted from the deck firmware's requirement text as truth-passed by the satellite
(`locveil-satellite/docs/review/des1-truth-pass.md` §2; the original FR-text lives in this
repo's history at `a80322f` and earlier — `ESP32/REQUIREMENTS.md`):

| Source | Verdict there | Lands in v1 as |
|---|---|---|
| FR-5 (announce: `meta/name`, control `meta` with type, initial values, `meta/online` — all retained) | promotion material | the **announce sequence** |
| FR-6 (last will `meta/online = "0"`) | promotion material | the **availability contract** (LWT) |
| FR-7 (subscribe `<control>/on`) | promotion material | the **command surface** |
| FR-8 (republish value on successful command) | promotion material | the **echo/confirmation semantics** |
| C-5 (wire protocol = the WB topic convention, no abstraction) | true in effect | the **topic tree** |
| FR-12 (record arming: `arm_record` within a window, consumed on use) | convention (deck-common §6) | the **`requires_arm` interlock** declared in descriptors |
| NFR-2 (≤ 1 s command latency) | becomes descriptor timing data | **`timing.confirm_latency_ms`** — STATIC per HK-4 |

Cross-checked against what this repo already speaks on the same wire: the WB virtual-device
emulation (`infrastructure/wb_device/service.py` — `/devices/<id>/meta`, `/controls/<c>`,
`/controls/<c>/meta`, `/controls/<c>/on`) and the passthrough driver's consumption side
(subscribe value topics, publish `/on`). wb-mqtt-v1 is the same dialect written down, from
the device side — so `EspManagedDevice` (DRV-36) consumes exactly what `WbPassthroughDevice`
already proved out, plus honest availability (a firmware **with** an LWT, unlike
mitsubishi2wb — DRV-27's heartbeat workaround is not needed here).

**The firmware-stamp tripwire:** one retained topic per device,
`/devices/<device_id>/meta/locveil`, JSON
`{"app": …, "fw": …, "descriptor": <int>, "convention": <int>}`. It answers "which
convention/descriptor version is this device actually running?" — the bridge may surface a
staleness warning; it must never branch behavior on it (design-time contract, HK-4).
Normative topic table and lifecycle rules: the guide.

## 4. The descriptor

One JSON per device, satellite-authored, validated against
`device-descriptor.schema.json`. Shape decisions:

- **Structurally mirrors the class-map dialect** this repo's drivers already use
  (`config/capabilities/classes/*.json`): per-capability `kind` / `feedback` / `reconcile` /
  `state_field` / `actions` / `fields` with `{wire, canonical, labels}` value tables — with
  two deliberate differences: actions bind to a **`control`** (the MQTT control name) instead
  of a driver `command`, and there is **no `gate` object** — the driver derives its gate from
  the static `timing.confirm_latency_ms` (the gate stays bridge-internal and re-tunable,
  the latency promise is contract — the same split VWB-34 records for the catalog).
- **`timing.confirm_latency_ms` is required and static** (HK-4: latency conceded static, no
  runtime negotiation). It is also the natural derivation source if VWB-34's
  per-capability `confirm_timeout_ms` ever lands in the catalog — recorded as linkage, not
  scope.
- **Interlocks are declared, firmware-enforced.** `requires_arm` (FR-12) is machine-readable
  in the descriptor so the bridge UI can render the arm flow; purely internal interlocks
  (A77 reel-motion, B215 never-drive-high) remain firmware truth, descriptor `notes` at most.
- **`device_id`** is the wire-level MQTT id (`^[a-z][a-z0-9_]*$`). The bridge-era ids
  (`revox_a77`, …) remain the likely values — the satellite's descriptor design decides
  (their DES-4), not this convention.

## 5. Owner decisions (2026-07-12, interactive)

- **D1 — the descriptor carries the canonical capability mapping** (not a bridge-side class
  map per device). This is what makes DRV-36 *descriptor-native*: a new satellite device
  means a new descriptor and zero bridge-side authoring — the driver validates the mapping
  against the pinned vocabulary and projects it into the catalog. The class-map mechanism
  stays untouched for every existing driver; `EspManagedDevice` is the one class whose
  "map" arrives from the descriptor. Rejected alternative: wire-only descriptors + bridge
  class maps — consistent with existing drivers but re-creates a bridge authoring task per
  device, weakening the "descriptors up" half.
- **D2 — REST leg: URL shapes reserved, profile deferred.** v1 records as normative the WB7
  asset-plane conventions (`GET /esp32/firmware/{ref}`, `GET /esp32/models/{ref}` — mTLS at
  the satellite-owned nginx plane; publishing owned by satellite ops) and **reserves** the
  device-hosted REST shapes `GET /api/status`, `POST /api/control` so a REST-transport
  device slots in without a convention redesign. The full rest profile is specified when the
  first real consumer exists — the known candidate being exactly the HVAC firmware rewrite
  (§2), which is not green-lit.
- **D3 — descriptor i18n: `ru` + `en` required, `de` optional** — for device `names` and all
  `labels` tables. Matches current fleet reality (hood ru/en, HVAC ru/en/de). VWB-33 (the
  fleet-wide label-policy design) will bind descriptors too when it lands; v1 deliberately
  doesn't pre-empt it beyond this floor.

## 6. Vocabulary reconciliation — the satellite DES gate

The canonical capability vocabulary (capability ids, action names, canonical value tokens) is
**bridge-owned**; the catalog contract (`contracts/catalog/catalog.golden.json` + its README) is its
exhibit of record. A descriptor may only use vocabulary the bridge recognizes. When a new
device needs vocabulary that doesn't exist yet — the decks will: a transport surface in the
`stop/play/ff/rewind` family plus `record`/`arm_record` — the flow is: the satellite's design
session files the vocabulary request **repo-to-repo**; the addition lands bridge-side first
(capability vocabulary + catalog projection, a deliberate contract cut); only then is the
descriptor approved at their DES gate. No descriptor invents canonical tokens. The concrete
deck vocabulary is resolved at DRV-36 + the first deck descriptor — per HK-4, the golden
catalog waits for the first deck config, so no vocabulary is added speculatively now.

## 7. Conformance & versioning

- **Version = single integer on the artifact set** (`STAMP.json`), tagged
  **`device-integration-vN`** (annotated tag, this repo). Any breaking change bumps N;
  additive schema evolution within a major is allowed and dated in the stamp. The tag is the
  pinnable ref (the same discipline VWB-29 establishes for the catalog contract tags).
- **Satellite side:** mirrors the schema into their `contracts/`, validates every descriptor
  under `boards/` against it in CI (their DES-4 + their `consumer-pins` invariant).
- **Bridge side:** VWB-39 pins the artifact byte-identical (the report-protocol pin
  recipe) and locks the DRV-36 consuming surface to the pin with a unit test. On a bump:
  re-pin first, then adjust until conformance passes.
- **Runtime:** the `meta/locveil` stamp topic (§3) is the tripwire that a deployed device
  was built against a stale pin — surfaced, never acted on.

## 8. Follow-ups (filed)

- **DRV-36** — `EspManagedDevice` design (descriptor-native driven adapter consuming this
  convention; one-time openapi bump).
- **VWB-39** — descriptor-pin conformance test (activates with DRV-36's implementation).
- Per-deck bridge configs and the deck vocabulary cut stay **unfiled until satellite
  first-light** (HK-4 board instruction).
- No HVAC task exists or is anticipated on this arc (§2 — owner constraint).
