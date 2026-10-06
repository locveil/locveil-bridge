# How-to — add a device with an existing driver

This is the **config-only path**: a new physical device whose driver class
already ships. The vast majority of devices land here — the nine shipped
driver classes between them cover most cases:

- `WbPassthroughDevice` (one class, many configs) — every native Wirenboard
  control: relays (`wb-mr6c`), dimmers (`wb-mdm3`), RGBW (`wb-mrgbw-d`),
  covers (`dooya`), heating valves (`wb-gpio`), HVAC, multi-sensors.
- `WirenboardIRDevice` — anything controlled via the WB IR blaster.
- `BroadlinkKitchenHood` — adapt the broadlink pattern for another
  Broadlink-controlled appliance.
- `LgTv`, `AppleTVDevice`, `AuralicDevice`, `EMotivaXMC2`,
  `RevoxA77ReelToReel` — one-off configs per physical unit.
- `MitsubishiHvac` — the Mitsubishi air conditioners (a bespoke driver that
  speaks the AC firmware's MQTT dialect directly); one config per unit.

If the device needs a *new* driver class, read **[How-to: add a new driver
with a native library](howto-new-driver.md)** instead.

## Three things you decide before writing the file

1. **Which driver class** — see the list above. For a new WB control of a
   shape already covered (a light, a dimmer, a cover, a heating loop),
   that's `WbPassthroughDevice`.
2. **Which capability profile** (only for `WbPassthroughDevice`) — pick from
   `config/capabilities/profiles/`: `light_switch` (relay),
   `dimmable_light` (relay + brightness), `rgb_light`, `cover` (dooya),
   `heating_loop`, `sensor_room`, and the whole-house utility profiles
   (`power_switch`, `water_supply`, `heating_control`, and more — the
   directory is the full list). (The Mitsubishi air conditioners are NOT
   passthrough devices — they have their own `MitsubishiHvac` driver and class
   capability map.) If none fit, profile-extension is
   a small change covered in [the new-driver guide](howto-new-driver.md).
3. **Which room** — must match a `room_id` in `config/rooms.json`
   (see [Architecture: rooms](../architecture/rooms.md)).

## Words the voice assistant will use

The catalog carries the *nouns* for your device; the voice assistant brings the verbs. Before
you commit, check:

- **`names`** — Russian and English are required; add German if you have it. The assistant
  falls back to Russian for a locale you leave out.
- **`aliases`** — optional, but the single best thing you can do for voice. Write the words
  people in the house actually say, Russian first (`"aliases": {"ru": ["люстра", "большой
  свет"]}`). Don't reuse a word that is already a field label somewhere — «жалюзи» belongs
  to the cabinet rollers, so the air conditioner's louver is «заслонка».
- **Field labels and value labels** — if your capability map (or profile) declares a field,
  it needs `labels` in Russian and English; every entry of an enum value table needs them
  too. The only exception is the plain `on`/`off` of a `power` field. A selection input with
  one command per option (`by_value`) labels each option in place.
- **`unit`** is a symbol (`°C`, `%`, `dB`), **`description`** is a short English note for
  developers — neither is spoken, neither is translated.

The contract tests check all of this on the committed catalog, so a missing label fails the
build with the exact device, field and value named.

## Worked example — a WB relay light

File: `config/devices/wb-devices/cabinet/cabinet_spots.json`. The
template every WB-passthrough light follows:

```json
{
  "device_id": "cabinet_spots",
  "device_class": "WbPassthroughDevice",
  "config_class": "WbPassthroughDeviceConfig",
  "names": {"ru": "Споты", "en": "Spots", "de": "Spots"},
  "capability_profile": "light_switch",
  "room": "cabinet",
  "commands": {
    "power_on":  {"topic": "/devices/wb-mr6c_51/controls/K4/on", "value": "1"},
    "power_off": {"topic": "/devices/wb-mr6c_51/controls/K4/on", "value": "0"}
  },
  "state_topics": {"power": "/devices/wb-mr6c_51/controls/K4"}
}
```

What each line does:

| Field | Meaning |
|---|---|
| `device_id` | The unique id. **Must match the filename** (the file is `cabinet_spots.json`). |
| `device_class` + `config_class` | The driver to instantiate and its typed Pydantic config — both **required**. |
| `names` | Localised display names (`ru` / `en` / `de`). The voice catalog reads all locales. |
| `capability_profile` | The shared profile this device's capability map deep-merges with. `light_switch` declares `power.on → power_on` / `power.off → power_off`. |
| `room` | Single source of truth for room membership; `RoomManager` derives the room's `devices` list from this. |
| `commands` | Named commands. For WB-passthrough, each one publishes `value` to `topic`. `power_on` and `power_off` are the names the `light_switch` profile expects. |
| `state_topics` | What the driver subscribes to for state mirroring. The key (`power`) matches the profile's `state_field`; the value is the WB control's value topic (without `/on`). |

## Where to put the file

By driver flavor:

- **WB-passthrough** → `config/devices/wb-devices/<room_id>/<device_id>.json`.
  The subfolder is the bridge `room_id` (matching `rooms.json`), not the WB
  dashboard id.
- **Everything else** (the eight non-passthrough driver classes) →
  `config/devices/<device_id>.json`.

## Per-flavor config shape — quick reference

| Flavor | Required fields beyond the common ones |
|---|---|
| `WbPassthroughDevice` | `capability_profile`, `commands` (topic + value), `state_topics`. |
| `WirenboardIRDevice` | `commands` with `topic`, `location` (the IR-code name on the blaster), `action`; capabilities go in `devices/<id>.json` under `config/capabilities/devices/`. |
| `BroadlinkKitchenHood` | `broadlink` block (host, mac, device_class), `rf_codes` map keyed by category. |
| `LgTv`, `EMotivaXMC2`, etc. | Device-specific config sub-blocks (IP, port, credentials, …) defined in `WbPassthroughDeviceConfig`'s sibling classes in `infrastructure/config/models.py`. |

Each driver class's typed config in `infrastructure/config/models.py` is the
authoritative field list for that flavor.

## Capability profiles + the resolution chain (when it matters)

For `WbPassthroughDevice` only. When `attach_capability_maps()` builds your
device's capability map at bootstrap, three files deep-merge (later wins):

1. `config/capabilities/classes/<device_class>.json` — driver-class default
   (used by the AV stack; usually empty for WB-passthrough).
2. `config/capabilities/profiles/<capability_profile>.json` — your profile,
   shared by the whole fixture family.
3. `config/capabilities/devices/<device_id>.json` — per-instance override
   (rare; use it only for one-off tweaks).

For the AV stack (`LgTv`, `EMotivaXMC2`, etc.), there is no profile —
class-level + per-device override only.

## After writing — verify

From `backend/`:

1. **Parse + load**: `pytest tests/unit/test_capabilities.py
   tests/unit/test_rooms_bootstrap.py` runs the typed loaders and the
   room-membership invariant against every config in `config/devices/`.
2. **Catalog projection**: `curl localhost:8000/system/catalog` (or the
   relevant test) should show the device under its room with the right
   capability surface.
3. **Hardware smoke**: `POST /devices/{id}/canonical
   {capability: "power", action: "on"}`. For WB-passthrough this publishes
   to the WB control's `/on` topic; the bridge subscribes to the value
   topic and the round-trip echoes back within ~500 ms.

## Commit

One commit per logical unit (one device, or one room's worth of devices
authored in the same session). Commit body lists the device id(s), the
profile picked, the WB control topic(s), and any non-obvious decision
(inversion, naming choice, missing fields, etc.). Include the trailer.

## When you hit a wall

- **Profile doesn't fit your fixture** → check whether a `state_topics`
  field with `type` + `encoding` (RGB, multi-cell HVAC) covers it; if not,
  a profile extension is the right move (small Pydantic change, see the
  new-driver guide).
- **Type mismatch between `state_topics` and the profile's `fields[]`** →
  declare the field once in the profile with its `type` and (for enums) a
  `{wire, canonical, labels}` value table — bare per-device `state_topics`
  inherit it automatically at load.
- **WB-UI dashboard id ≠ bridge `room_id`** → the subfolder is the bridge
  `room_id`. The `description` field in `rooms.json` documents the WB
  dashboard mapping for the importer.

## Where to go next

- **[Architecture: devices and scenarios](../architecture/devices-and-scenarios.md)**
  — driver flavors + the capability-map resolution chain.
- **[Architecture: key concepts](../architecture/key-concepts.md)** — the
  four declarative inputs (configs, capabilities, topology, scenarios).
- **[How-to: add a new driver with a native library](howto-new-driver.md)** —
  the Python-side path when no existing driver fits.
- **[How-to: define a new AV scenario](howto-new-scenario.md)** — wire your
  new device into a one-touch activity.
