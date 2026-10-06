# Language-data contribution convention — design (VWB-33)

**Status: COUNCIL-DECIDED 2026-10-06, implementation pending** (board PROD-18, round 1 —
the owner's "Round 1 DECIDED" paragraph in `../../../locveil-commons/board/BOARD.md` is the
decision of record; this document designs to it and reopens nothing). Implementation:
**VWB-46** (the `catalog-v1.11.0` cut, shared with the timing design
[`confirmation_timing.md`](confirmation_timing.md)). Sibling prose: the ownership split
(nouns in the catalog, verbs in voice donations, group tokens unlocalized, donations may quote
value labels) lives in commons [`process/language-data.md`](../../../locveil-commons/process/language-data.md)
(IMPL-27) and is referenced here, never restated. **Where the machine rule will live:** the
pinned guide [`contracts/catalog/catalog-contract.md`](../../contracts/catalog/catalog-contract.md),
new section "Localization (since contract v1.11)" — §7 of this document is its text, verbatim.

## 1. Purpose and scope

The catalog is the only place the voice assistant learns what a device, a field or an enum
value is *called*. Until now what it carried per locale was whatever each author happened to
write: HVAC with three locales, the AV fleet with two, aliases Russian-only on fewer than half
the devices, and two IR inputs whose selectable values reached the catalog as bare canonical
tokens with no words at all. This design fixes the rule — which surfaces carry language, in
which locales, with which exemptions — and the guard that keeps it true, so that a consumer
can build against the floor instead of probing for it.

**In scope:** the machine rule and its guide text; the audit of the committed golden
(`catalog-v1.10.0`, content hash `5622ba7a1a78102a`); the guard; the label slot that closes the
nine real gaps; the louver label rename; the authoring-checklist text for the how-to.
**Out of scope** (decided): German cosmetics on the thirteen devices and the AV fields that lack
`de`; localizing `description` or `unit`; any voice-side matching change (the resolver already
matches labels in the active locale); the vane/widevane crossover fixtures (a separate task).

## 2. Decisions this design executes (from the council, verbatim in substance)

| # | Decision | Where it lands |
|---|---|---|
| 1 | Label floor = `ru` + `en` REQUIRED, `de` optional; a consumer may fall back to `ru`. | Guide §7; already enforced for `names`/`labels` by `LocalizedName` (`domain/devices/config.py:22`) and for descriptors by device-integration D3. |
| 2 | Every enum value carries labels EXCEPT the bare `power` on/off pair. The 9 real gaps (two IR by-value inputs) close via a label slot on by-value selects. A check-style test over the committed golden is the guard. | §4 (slot), §5 (guard), guide §7. |
| 3 | The machine rule lives in the pinned `catalog-contract.md`; the ownership split in a short pointer-only commons `process/` file; voice's how-to cites both. | Guide §7; commons `process/language-data.md` (landed). |
| 4 | The louver noun is «заслонка» — the donation's spoken noun AND the renamed HVAC `vane` field label; the cabinet rollers keep «жалюзи». | §6. |
| 7 riders | `description` stays developer English; `unit` is a symbol; aliases are an authoring-checklist item, ru-first, no minimum; no German cosmetics. | Guide §7 (documented as such); §8 (checklist). |

## 3. The audit — the committed golden at `catalog-v1.10.0`

Counted by script over `contracts/catalog/catalog.golden.json` on 2026-10-06 (79 devices
incl. the living-room scenario manager, 11 rooms). These are the numbers the convention is
measured against; the July text's "some fleet fields carry no labels at all" does not survive
them.

| Surface | Total | ru+en+de | ru+en only | No labels |
|---|---|---|---|---|
| Device `names` | 79 | 66 | 13 | 0 |
| Room `names` | 11 | 11 | 0 | 0 |
| Field `labels` | 122 | 21 (the 3 HVACs × 7) | 101 | **0** |
| Field `values` entries | 171 | 81 | 12 | 78 — all of them the `power` field's `on`/`off` pair on 39 WB devices (the exempted pair) |
| Action-param `values` entries | 93 | — | 84 (scenario + the HVAC `set(value)` mirrors) | **9** — `mf_amplifier.input.set(value)` × 7 (`cd`, `aux2`, `usb`, `phono`, `tuner`, `aux1`, `balanced`) and `upscaler.input.set(value)` × 2 (`video`, `s_vhs`) |
| Device `aliases` | 35 of 78 config devices carry any; every alias list is `ru`-only | | | |
| Room `aliases` | 3 of 11 | | | |

Other facts the rule documents rather than changes: `unit` appears on 75 params/fields with
four distinct symbols (`°C` 37, `%` 35, `min` 2, `dB` 1); `description` appears on params in 14
distinct English strings, none localized. One label/alias collision exists in Russian:
«жалюзи» is the `vane` field label on all three HVACs **and** the only alias of
`cabinet_roller_left` / `cabinet_roller_right` — the collision §6 removes. No other field
label equals any alias.

**Why the nine are unlabelled** (verified in code): `CapabilitySelect.by_value` maps a value to
a bare `CapabilityAction` (`domain/capabilities/models.py:89-136`); the catalog builder emits
the select's `set(value)` table as `CatalogValueLabel(wire=v, canonical=v)` with no labels
(`presentation/api/catalog.py:126-138`). There is no slot in the model to author a label, so no
authoring effort could have closed this — it is a model gap, not a config gap.

## 4. The by-value label slot

**Model.** `CapabilitySelect.by_value` stays a `Dict[str, CapabilityAction]`. The value side
gains one optional field on `CapabilityAction`:

```python
labels: Optional[LocalizedName] = Field(
    None,
    description="Localized human strings for this option when the action is the value "
                "of a by-value select (the catalog's `set(value)` table). Meaningless on "
                "sequence steps and ordinary actions; the catalog ignores it there.",
)
```

Alternatives weighed: a parallel `by_value_labels: Dict[str, LocalizedName]` keeps
`CapabilityAction` untouched but splits one option across two maps and invites drift between
the key sets; a new `SelectOption {command|sequence, labels}` value type is the cleanest but
is a breaking config change for every by-value map. The one optional field is the smallest
change that keeps one map, one key, one place to author — and `extra="forbid"` on the model
still rejects misspellings.

**Config shape** (what an author writes; `config/capabilities/devices/mf_amplifier.json`):

```json
"select": {
  "by_value": {
    "cd":       { "command": "input_cd",       "labels": { "ru": "CD",        "en": "CD" } },
    "aux2":     { "command": "input_aux2",     "labels": { "ru": "AUX 2",     "en": "AUX 2" } },
    "usb":      { "command": "input_usb",      "labels": { "ru": "USB",       "en": "USB" } },
    "phono":    { "command": "input_phono",    "labels": { "ru": "фоно",      "en": "phono" } },
    "tuner":    { "command": "input_tuner",    "labels": { "ru": "тюнер",     "en": "tuner" } },
    "aux1":     { "command": "input_aux1",     "labels": { "ru": "AUX 1",     "en": "AUX 1" } },
    "balanced": { "command": "input_balanced", "labels": { "ru": "балансный", "en": "balanced" } }
  }
}
```

and `upscaler.json`:

```json
"by_value": {
  "video": { "command": "input_video", "labels": { "ru": "видео", "en": "video" } },
  "s_vhs": { "command": "input_s_vhs", "labels": { "ru": "S-VHS", "en": "S-VHS" } }
}
```

**Proposed strings — the migration of the nine values** (the owner may edit any of these at the
cut; the rule only requires that `ru` and `en` exist):

| Device | canonical | ru | en | Note |
|---|---|---|---|---|
| mf_amplifier | `cd` | CD | CD | Latin as spoken («переключи на си-ди» is the resolver's business; the label is the display/match string). |
| mf_amplifier | `aux2` | AUX 2 | AUX 2 | The zone-2 feed from the eMotiva (topology `processor:zone2 → mf_amplifier:aux2`). |
| mf_amplifier | `usb` | USB | USB | |
| mf_amplifier | `phono` | фоно | phono | The Dodocus/Sugden path is `cd` today; `phono` is the amp's own input. |
| mf_amplifier | `tuner` | тюнер | tuner | |
| mf_amplifier | `aux1` | AUX 1 | AUX 1 | |
| mf_amplifier | `balanced` | балансный | balanced | The streamer's feed (`streamer:out → mf_amplifier:balanced`). |
| upscaler | `video` | видео | video | Composite in — the LD player. |
| upscaler | `s_vhs` | S-VHS | S-VHS | The VHS deck. |

**Projection.** In `_project_capability_actions`, the synthesized `set` action's table becomes
`CatalogValueLabel(wire=v, canonical=v, labels=act.labels.model_dump() if act.labels else None)`
for `v, act in cap.select.by_value.items()`. No new catalog type; `CatalogValueLabel.labels`
already exists. Parametric selects (`options_from`) are untouched — their option set is
runtime-dynamic and unlabelled by design (guide, "Param semantics").

**Reconciler / dispatch.** None. `CapabilitySelect.expand` and `option_values` ignore the new
field; `by_value` keys remain the canonical values the topology's `dst_port` names.

**Contract impact.** The golden gains `labels` on nine `set(value)` entries; `openapi.json`
is unchanged by this item (the schema already allows `labels`); the content hash moves. A
MINOR cut because the guide gains a section (surface changed), batched into `catalog-v1.11.0`.

## 5. The guard — what fails the commit

Home: `backend/tests/unit/test_contracts_golden.py` (the drift-guard file — it already pins
every "since contract vN" property on the committed golden, so a config that breaks the rule
fails CI in the same place a stale golden does). The tests read the committed
`contracts/catalog/catalog.golden.json`; together with `test_golden_catalog_matches_configs`
this means a config change that would break the rule cannot be regenerated into a passing
golden. Assertions, one test each, named `test_contract_v111_*`:

1. **`names_carry_the_locale_floor`** — every device and every room has `names.ru` and
   `names.en`, non-empty strings.
2. **`field_labels_carry_the_locale_floor`** — every field on every capability has `labels`
   with non-empty `ru` and `en`.
3. **`every_value_is_labelled_except_the_power_pair`** — every entry of every `values` table
   (fields AND action params) has `labels` with non-empty `ru` and `en`, **except** an entry
   that satisfies all of: it sits on a FIELD (not a param), the capability is named `power`,
   the field is named `power`, and `canonical ∈ {on, off}`. The exemption list is exactly that
   one shape — the failure message names `device.capability.field/param: canonical` so the
   fix is one authoring edit.
4. **`field_labels_never_collide_with_aliases`** — for the `ru` locale, the set of field
   labels and the set of all device + room aliases are disjoint (lower-cased). This is the
   mechanical form of decision 4: it fails today on «жалюзи» and passes after §6, and it keeps
   the next author from re-creating the collision with a different word.
5. **`aliases_are_ru_first`** — an `aliases` object, where present, has a `ru` key with a
   non-empty list; every list element is a non-empty string; keys are two-letter lowercase
   codes. (No minimum count, no requirement to be present.)
6. **`units_are_symbols`** — every `unit` on a field or param is a short symbol: no
   whitespace, at most five characters.
7. **`guide_holds_the_localization_section`** — extend the existing
   `test_guide_holds_the_normative_text_and_the_readme_points_at_it` heading list with
   `"## Localization"` (and the timing design adds `"## Timing"`).

Not guarded, deliberately: `de` presence (optional by decision); `description` wording
(developer-facing); the DESCRIPTOR side (device-integration's own schema requires `ru`+`en` on
every label and every value — stricter than the catalog, so a conforming descriptor never
fails these tests). Load-time validation of value labels on an arbitrary house's config is NOT
added: the decision names the golden test as the guard, and `LocalizedName` already refuses a
label that lacks `ru`/`en` wherever a label is present.

## 6. The louver label rename

| Field (all three `MitsubishiHvac` devices) | ru today | ru after | en | de |
|---|---|---|---|---|
| `vane` (vertical airflow louver — up/down positions 1–5, swing, auto) | жалюзи | **заслонка** | vane | Lamelle (unchanged) |
| `widevane` (horizontal airflow louver — left/center/right, split, swing) | горизонтальные жалюзи | **заслонка по горизонтали** | wide vane | Horizontallamelle (unchanged) |

Authored once in `config/capabilities/classes/MitsubishiHvac.json` (`vane.fields[0].labels`,
`widevane.fields[0].labels`); the per-value tables («авто», «качание», «положение 1»…,
«крайне влево»…) are unchanged. The proposed `widevane` string follows the owner's noun and
reads as the same object oriented the other way — «заслонка по горизонтали» — rather than an
adjective pile («горизонтальная заслонка»), which would compete with the unqualified
«заслонка» at match time; voice's donation for QUAL-82 (`hvac_vane`, `hvac_widevane`) owns the
spoken phrases either way and may quote these labels. `cabinet_roller_left` /
`cabinet_roller_right` keep `aliases.ru = ["жалюзи"]`. After the rename guard §5.4 is green.

## 7. The guide section — verbatim text for `catalog-contract.md`

To be inserted after "Param semantics (since contract v1.1)" and before "Versioning", at the
`catalog-v1.11.0` cut. The text names no task, no file outside the pin folder, and no current
version, so it never needs an edit because a later cut happened.

```markdown
## Localization (since contract v1.11)

Which catalog surfaces carry human language, in which locales, and what a consumer may
assume about them. Who contributes which words across the product — the nouns here, the
verbs in the voice assistant's own vocabulary — is the organisation's language-data
convention; this section is the machine rule that convention points at.

- **Locales.** Every localized surface carries `ru` and `en`. `de` is optional and other
  locales may appear; a consumer that wants a locale an entry lacks falls back to `ru`.
  Locale keys are lowercase two-letter language codes.
- **Localized surfaces.** Device `names` and room `names`; `aliases` — per locale, a list
  of spoken alternatives, authored `ru` first, never required and never complete; field
  `labels`; and the `labels` of every entry in a `values` table, on a field and on an
  action param alike — including the `set(value)` table of a selection capability.
- **The one exemption.** The `on` / `off` entries of a `power` field carry no labels:
  the words for power are a consumer's verbs, not catalog nouns.
- **Not localized, by design.** `canonical` and `wire` are identifiers. Capability names,
  action names, param names and `group` are identifiers too — a consumer never shows
  them as words or matches speech against them. A param's `description` is
  developer-facing English. A `unit` is a symbol (`°C`, `%`, `dB`, `min`), the same in
  every locale.
- **The guard.** The committed golden sample is checked against these rules on every
  change to the bridge: a configuration that breaks them cannot be regenerated into the
  sample. A consumer may therefore treat a missing `ru` or `en` on a localized surface as
  a defect to report, not a case to handle.
```

## 8. The authoring checklist — text for the user-facing how-to

For `docs/guides/howto-new-device.md` (manifest node `howto/new-device`), a new short
subsection after "Three things you decide before writing the file", landed with VWB-46 and
recorded in its `docs:` verdict. Reader-first, no internal references:

```markdown
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
```

## 9. What the voice side sees after the cut

- `CatalogValueLabel.labels` becomes reliably present on every table entry except the power
  pair — the resolver's "no label → skip value" branch (if any) becomes dead for the by-value
  inputs; «переключи усилитель на тюнер» gains a surface.
- `vane.labels.ru` changes from «жалюзи» to «заслонка» on three devices; QUAL-82's donations
  are gated on the noun ruling, not on this re-pin, and need no catalog change.
- A single re-pin (`repin.py catalog`) at `catalog-v1.11.0` moves voice's local pin and the
  commons crossover copy together (VWB-46 records `re-pin owed: voice, commons`).

## 10. Open items (none reopen a decision)

- The exact ru/en strings in §4 are proposals; the owner edits them at the cut if a house
  word is better («балансный» vs «XLR», «фоно» vs «винил»).
- Guard §5.4 (label/alias disjointness) and §5.5 (`ru` present when aliases exist) are the
  design's mechanical readings of decisions 4 and the aliases rider; strike either at the
  cut if too strict.
