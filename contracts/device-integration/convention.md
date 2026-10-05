# The Locveil device-integration convention — v1

How a **Locveil-built device** (firmware we own — today the ESP32 satellite family)
integrates with the bridge. Two layers, one direction each:

- **Convention down** — this document, the descriptor schema beside it, and the capability
  vocabulary are owned and versioned in the **bridge repository**. Device repos pin a copy
  and build against it.
- **Descriptors up** — each device ships one JSON **descriptor** (validated against
  [`device-descriptor.schema.json`](device-descriptor.schema.json)) declaring its wire
  surface and how it maps onto the bridge's canonical capabilities. The bridge consumes the
  descriptor as-is: a conforming device needs no bridge-side authoring.

Everything is settled at **design time**. There is no runtime negotiation: a device and the
bridge agree because both were built against the same pinned convention version.

## Who must conform — and who must not

The convention binds **Locveil-owned firmware only**. Third-party and pre-existing gear —
stock Wirenboard devices, externally-flashed modules (e.g. the Mitsubishi HVAC dongles),
controller-side rule scripts — integrate the way they always have and are **never**
retrofitted. If an external device's firmware is ever deliberately rewritten under Locveil
ownership, it conforms from that rewrite on; until then it is out of scope.

## The `wb-mqtt-v1` profile

A conforming device speaks the Wirenboard MQTT convention on the wire — the same dialect the
bridge itself publishes and consumes — with the following normative rules.

**Topic tree** (all under `/devices/<device_id>/`):

| Topic | Direction | Retained | Payload |
|---|---|---|---|
| `meta/name` | device → | yes | human-readable device name |
| `meta/online` | device → | yes | `"1"` while connected; **last will sets `"0"`** |
| `meta/locveil` | device → | yes | the version stamp (below) |
| `controls/<control>` | device → | yes (stateful) / no (pushbutton ack) | the control's current value |
| `controls/<control>/meta` | device → | yes | control meta: `type`, `readonly`, units/range where applicable |
| `controls/<control>/on` | → device | no | a command: the value to apply, or `"1"` to fire a pushbutton |

**Lifecycle:**

1. **Announce** — on MQTT connect the device publishes (retained) its `meta/name`, every
   control's `meta`, every stateful control's current value, `meta/online = "1"`, and the
   `meta/locveil` stamp. The MQTT session carries the last will `meta/online = "0"` — the
   broker itself reports the device unreachable; no heartbeat protocol is needed.
2. **Command** — the device subscribes to `controls/<control>/on` for every writable control.
3. **Echo** — on a *successful* command the device republishes the resulting value on the
   control's value topic. This echo is the confirmation signal consumers gate on. A
   pushbutton's ack is transient (not retained); a stateful control's echo is its new
   retained value. A failed or refused command publishes **no** echo.
4. **Safety interlocks are firmware-enforced.** A device that exposes a hazardous action
   (e.g. `record` on a tape deck) requires its declared arming action first
   (`arm_record`, within the descriptor's window, consumed on use) — a stray message must
   never trigger the hazard. The descriptor declares the interlock; the firmware enforces it.

**The version stamp** — `meta/locveil`, retained JSON:

```json
{"app": "revox-a77", "fw": "1.2.0", "descriptor": 3, "convention": 1}
```

It states which firmware, descriptor revision, and convention version the running device was
built against. `convention` carries the **major** version only (see
[Versioning & conformance](#versioning--conformance)). Consumers may surface a staleness
warning when the stamp trails the pinned versions; behavior never branches on it.

## REST URL conventions

Normative since v1:

- **Asset plane** (served by the satellite-owned nginx on the controller, mTLS):
  firmware images at `GET /esp32/firmware/{ref}`, model files at `GET /esp32/models/{ref}`.
  Devices pull; nothing is pushed to a device.

Reserved since v1 (shapes fixed, full profile specified when the first REST-transport device
exists):

- **Device-hosted REST**: `GET /api/status` (full state read-back), `POST /api/control`
  (command). A future device that commands over HTTP uses these paths with MQTT retained for
  value sync — no convention redesign required to add it.

## The descriptor

One JSON file per device, owned by the device's repo, valid against
[`device-descriptor.schema.json`](device-descriptor.schema.json). The example below is
also committed beside the schema as
[`example.descriptor.json`](example.descriptor.json) and machine-checked on every
change: the schema must accept it, and this document and the fixture are held to the
same content — the document can never teach a shape the schema rejects. In outline:

```json
{
  "convention": 1,
  "descriptor_version": 1,
  "profile": "wb-mqtt-v1",
  "device_id": "revox_a77",
  "names": {"ru": "Revox A77", "en": "Revox A77"},
  "firmware": {"app": "revox-a77"},
  "timing": {"confirm_latency_ms": 1000},
  "controls": {
    "play":       {"type": "pushbutton"},
    "stop":       {"type": "pushbutton"},
    "arm_record": {"type": "pushbutton"},
    "record":     {"type": "pushbutton"}
  },
  "capabilities": {
    "transport": {
      "kind": "momentary",
      "feedback": false,
      "actions": {
        "play": {"control": "play"},
        "stop": {"control": "stop"}
      }
    }
  },
  "interlocks": [
    {"type": "requires_arm", "action": "record", "arm_action": "arm_record", "window_ms": 8000}
  ]
}
```

Rules that matter:

- **`timing.confirm_latency_ms` is required and static** — the worst-case time from command
  to echo. Consumers size their confirmation windows from it. It is a promise, not a
  measurement channel.
- **Capability and action names must already exist in the bridge's canonical vocabulary**
  (exhibited by the catalog contract's golden sample,
  [`../catalog/catalog.golden.json`](../catalog/catalog.golden.json), and explained in its
  guide, [`../catalog/catalog-contract.md`](../catalog/catalog-contract.md) — both in the
  bridge repository). A device that needs new vocabulary requests it from the bridge side
  first; descriptors never invent canonical tokens.
- **Names and labels ship in Russian and English; German is optional.**
- Value tables use the `{wire, canonical, labels}` triplet form — the same convention the
  rest of the catalog speaks.

## Versioning & conformance

- **The convention is versioned as a whole, `MAJOR.MINOR.PATCH`.** [`STAMP.json`](STAMP.json)
  names the current version; the bridge-repository tag
  `device-integration-vMAJOR.MINOR.PATCH` is the pinnable reference. The stamp and the tag
  always move together, and the stamp enumerates the files that make up the convention —
  this document, the schema, the example fixture. An edit to any of them without a version
  move fails the bridge repository's contract checks, so a tag's bytes always match the
  stamp that names it.
- **Three levels.** **Major** — breaking: a device built against the previous major may be
  rejected. **Minor** — the surface changed compatibly: a new optional descriptor field, a
  new profile, a reserved shape becoming specified, the file set gaining a file. **Patch** —
  bytes moved and the surface did not: an editorial fix to this document, a reformatted
  example. Tags are three-part from `device-integration-v1.2.0` on; the two earlier tags
  (`device-integration-v1`, `device-integration-v1.1`) keep their form and are never re-cut.
- **What travels on the wire is the major only.** The descriptor's `convention` field and
  the `convention` value in a device's `meta/locveil` stamp name the major version. A minor
  or patch cut never changes what a fielded device reports or what a descriptor declares.
- **A descriptor's own revision is not a convention version.** `descriptor_version` belongs
  to the device: a bench-confirmed timing value or a new control bumps the descriptor, never
  the convention.
- **Consumers pin, one way, and pin the whole set.** A device repo mirrors every file the
  stamp enumerates, plus the stamp, byte-identical at a tagged version; records the pin; and
  validates its descriptors against the pinned schema in CI. Pins are never hand-edited; a
  bump means re-pin, then reconcile.
- The bridge validates the other direction: its consuming code is locked to the same
  artifacts by a conformance test, so neither side can drift silently.
- **The owner's side is guarded too:** the committed example descriptor is validated against
  the schema (and against this document's example) in the bridge's normal test suite — the
  schema never changes without a conforming exhibit changing with it.
