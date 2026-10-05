"""VWB-41 (PROD-16, council HK-5): the device-integration convention's owner-side
guard — an owned machine schema ships a committed schema-validating example fixture
plus a CI check from day one (no unguarded model layouts).

The bridge owns the convention (`contracts/device-integration/`, tags
`device-integration-vX.Y.Z`); the satellite repo pins it and authors conforming
descriptors. This test keeps the owner's side honest:

- the schema itself is a valid JSON Schema (draft 2020-12);
- the committed example descriptor validates against it;
- the example in the convention document (`convention.md` — the pinned normative text
  since v1.2.0; the folder README is an unlocked index outside the pinned set) is THE
  example — the same document as the committed fixture, so the convention can never
  teach a shape the schema rejects;
- the i18n floor holds (ru+en required — an en-only descriptor is rejected);
- the hand-written STAMP holds together: core fields, three-part version, tag string,
  and a pinned set a consumer's flat pin folder can hold (no reserved or duplicate
  file names).
"""

import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

pytestmark = pytest.mark.unit

CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "device-integration"

STAMP = json.loads((CONTRACT / "STAMP.json").read_text(encoding="utf-8"))
REPO = CONTRACT.parents[1]

SCHEMA = json.loads((CONTRACT / "device-descriptor.schema.json").read_text(encoding="utf-8"))
EXAMPLE = json.loads((CONTRACT / "example.descriptor.json").read_text(encoding="utf-8"))


def test_schema_is_valid_draft_2020_12():
    Draft202012Validator.check_schema(SCHEMA)


def test_example_descriptor_validates():
    Draft202012Validator(SCHEMA).validate(EXAMPLE)


def test_convention_example_is_the_committed_fixture():
    convention = (CONTRACT / "convention.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```json\n(.*?)```", convention, flags=re.DOTALL)
    descriptors = [b for b in blocks if '"descriptor_version"' in b]
    assert len(descriptors) == 1, "convention.md must show exactly one descriptor example"
    assert json.loads(descriptors[0]) == EXAMPLE, (
        "the convention's descriptor example and example.descriptor.json diverged — "
        "they are the same document; change both together (and cut a version)"
    )


def test_convention_holds_the_normative_text_and_the_readme_points_at_it():
    """The split's two halves stay put: the normative sections live in the pinned
    convention.md; the unlocked README links to it instead of restating them."""
    convention = (CONTRACT / "convention.md").read_text(encoding="utf-8")
    for heading in (
        "## Who must conform",
        "## The `wb-mqtt-v1` profile",
        "## REST URL conventions",
        "## The descriptor",
        "## Versioning & conformance",
    ):
        assert heading in convention, f"convention.md lost its {heading!r} section"
    readme = (CONTRACT / "README.md").read_text(encoding="utf-8")
    assert "(convention.md)" in readme
    assert "## The `wb-mqtt-v1` profile" not in readme


def test_stamp_core_and_three_part_version():
    """The hand-written STAMP carries the org core; since v1.2.0 the version is
    MAJOR.MINOR.PATCH and the tag three-part. The descriptor's `convention` value and
    the schema's const carry the MAJOR only — a minor/patch cut never moves them."""
    for key in ("contract", "version", "tag", "date", "owner_repo", "artifacts"):
        assert key in STAMP, f"STAMP.json lost {key!r}"
    assert STAMP["contract"] == "device-integration"
    assert STAMP["owner_repo"] == "locveil-bridge"
    assert re.fullmatch(r"\d+\.\d+\.\d+", STAMP["version"]), STAMP["version"]
    assert STAMP["tag"] == f"device-integration-v{STAMP['version']}"
    major = int(STAMP["version"].split(".")[0])
    assert SCHEMA["properties"]["convention"]["const"] == major
    assert EXAMPLE["convention"] == major
    assert SCHEMA["properties"]["profile"]["enum"] == STAMP["profiles"]


def test_pinned_set_shape_survives_a_flat_pin_folder():
    """A consumer's pin folder is FLAT and two names in it belong to the consumer
    (README.md, PIN.json); STAMP.json travels implicitly. So the owner never enumerates
    a file with a reserved name, never two files sharing a name — and every enumerated
    path exists. The convention document is in the set; the folder README is not."""
    artifacts = STAMP["artifacts"]
    assert set(artifacts) == {
        "contracts/device-integration/convention.md",
        "contracts/device-integration/device-descriptor.schema.json",
        "contracts/device-integration/example.descriptor.json",
    }
    names = [Path(a).name for a in artifacts]
    assert not {"README.md", "PIN.json", "STAMP.json"} & set(names)
    assert len(names) == len(set(names)), f"duplicate file names in the pinned set: {names}"
    for artifact in artifacts:
        assert (REPO / artifact).is_file(), f"enumerated artifact missing: {artifact}"


def test_en_only_names_are_rejected():
    broken = json.loads(json.dumps(EXAMPLE))
    broken["names"] = {"en": "Revox A77"}
    with pytest.raises(ValidationError):
        Draft202012Validator(SCHEMA).validate(broken)


def test_full_surface_descriptor_validates():
    """The surfaces the convention's example doesn't reach — stateful capability with
    feedback/reconcile/state_field, parametric action via param_map, enum field with a
    {wire, canonical, labels} triplet table, range/value control meta — all validate.
    Canonical tokens are real pinned vocabulary (the HVAC fan family)."""
    descriptor = {
        "convention": 1,
        "descriptor_version": 1,
        "profile": "wb-mqtt-v1",
        "device_id": "example_fan_unit",
        "names": {"ru": "Пример вентилятора", "en": "Example fan unit", "de": "Beispiel-Lüfter"},
        "firmware": {"app": "example-fan", "board": "esp32"},
        "timing": {"confirm_latency_ms": 800},
        "controls": {
            "power": {"type": "switch", "title": {"ru": "Питание", "en": "Power"}},
            "fan": {"type": "range", "min": 0, "max": 5},
            "temperature": {"type": "value", "readonly": True, "units": "°C"},
        },
        "capabilities": {
            "power": {
                "kind": "stateful",
                "feedback": True,
                "reconcile": True,
                "state_field": "power",
                "actions": {"on": {"control": "power", "payload": "1"},
                            "off": {"control": "power", "payload": "0"}},
                "fields": [{"name": "power", "type": "boolean",
                            "labels": {"ru": "Питание", "en": "Power"}}],
            },
            "fan": {
                "kind": "stateful",
                "state_field": "fan",
                "actions": {"set": {"control": "fan", "param_map": {"value": "{value}"}}},
                "fields": [{
                    "name": "fan", "type": "enum",
                    "labels": {"ru": "Скорость", "en": "Fan speed"},
                    "values": [
                        {"wire": "0", "canonical": "auto",
                         "labels": {"ru": "авто", "en": "auto"}},
                        {"wire": "2", "canonical": "speed_1",
                         "labels": {"ru": "скорость 1", "en": "speed 1"}},
                    ],
                }],
            },
        },
    }
    Draft202012Validator(SCHEMA).validate(descriptor)
