"""UI-22 (PROD-28, council HK-13): the Workbench plugin contract pin — conformance.

The bridge's Workbench plugin (`workbench-plugin/`) is a consumer of the commons-owned
plugin contract, family `workbench` (pinned at `contracts/pins/workbench/` by the
vendored repin tool: the contract types `contract.ts` + the two machine schemas). This
test proves the consuming side honors the pinned surface, HERMETICALLY — no Node, no
built `dist/`, no sibling checkout:

- the manifest fragment the plugin build emits validates against the pinned
  `manifest-fragment.schema.json`. The build is a pure merge of two committed files —
  `workbench-plugin/manifest.fragment.json` (id, entry, styles, peers) and the
  `version` in `workbench-plugin/package.json` — so this test performs the same merge;
- the Vite config really is that pure merge (it reads the data file and carries no
  inline fragment of its own), so the thing validated here is the thing emitted. CI
  closes the last link in the plugin job: after `npm run build` it checks that
  `dist/manifest.json` equals this merge;
- the fragment's peers are exactly the shell singletons the plugin leaves external —
  a peer the bundle inlines, or an external it declares no major for, is a mismatch
  the shell could only discover at load time;
- the pin holds the three files the owner's stamp enumerates.
"""

import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
PIN = REPO / "contracts" / "pins" / "workbench"
PLUGIN = REPO / "workbench-plugin"

SCHEMA = json.loads((PIN / "manifest-fragment.schema.json").read_text(encoding="utf-8"))
SOURCE = json.loads((PLUGIN / "manifest.fragment.json").read_text(encoding="utf-8"))
PACKAGE = json.loads((PLUGIN / "package.json").read_text(encoding="utf-8"))
VITE_CONFIG = (PLUGIN / "vite.config.ts").read_text(encoding="utf-8")


def emitted_fragment() -> dict:
    """The merge `emitManifestFragment()` performs in vite.config.ts."""
    rest = {k: v for k, v in SOURCE.items() if k != "id"}
    return {"id": SOURCE["id"], "version": PACKAGE["version"], **rest}


def test_pinned_schema_is_valid_draft_2020_12():
    Draft202012Validator.check_schema(SCHEMA)


def test_emitted_manifest_fragment_validates_against_the_pinned_schema():
    Draft202012Validator(SCHEMA).validate(emitted_fragment())


def test_schema_actually_constrains_the_fragment():
    """Guard against a vacuous pass: the pinned schema must reject a fragment missing
    a required field and one with a non-string peer."""
    validator = Draft202012Validator(SCHEMA)
    for field in SCHEMA["required"]:
        broken = emitted_fragment()
        del broken[field]
        with pytest.raises(ValidationError):
            validator.validate(broken)
    broken = emitted_fragment()
    broken["peers"] = {"react": 18}
    with pytest.raises(ValidationError):
        validator.validate(broken)


def test_source_file_carries_no_version_and_the_package_does():
    """`version` has ONE source — package.json; a second copy in the data file would be
    silently overridden by the merge order and drift."""
    assert "version" not in SOURCE
    assert isinstance(PACKAGE["version"], str) and PACKAGE["version"]


def test_vite_config_emits_exactly_the_validated_source():
    assert "from './manifest.fragment.json'" in VITE_CONFIG
    assert "from './package.json'" in VITE_CONFIG
    assert "version: pkg.version" in VITE_CONFIG
    # no inline fragment left behind: the id and the peer table live in the data file
    assert not re.search(r"\bid:\s*['\"]", VITE_CONFIG)
    assert not re.search(r"\bpeers:", VITE_CONFIG)


def test_peers_are_the_singletons_the_bundle_leaves_external():
    block = re.search(r"const SINGLETONS = \[(.*?)\]", VITE_CONFIG, flags=re.DOTALL)
    assert block, "vite.config.ts lost its SINGLETONS list"
    externals = set(re.findall(r"'([^']+)'", block.group(1)))
    packages = {e.split("/")[0] for e in externals}  # react-dom/client -> react-dom
    assert set(emitted_fragment()["peers"]) == packages


def test_entry_and_styles_match_the_configured_lib_file_names():
    fragment = emitted_fragment()
    assert fragment["entry"] == "./index.js" and "fileName: () => 'index.js'" in VITE_CONFIG
    assert fragment["styles"] == ["./style.css"] and "cssFileName: 'style'" in VITE_CONFIG


def test_pin_holds_the_owner_enumerated_set():
    pin = json.loads((PIN / "PIN.json").read_text(encoding="utf-8"))
    stamp = json.loads((PIN / "STAMP.json").read_text(encoding="utf-8"))
    assert pin["contract"] == stamp["contract"] == "workbench"
    assert pin["tag"] == stamp["tag"]
    enumerated = {Path(a).name for a in stamp["artifacts"]}
    assert enumerated == {
        "contract.ts", "manifest-fragment.schema.json", "runtime-config.schema.json",
    }
    assert enumerated | {"STAMP.json"} == set(pin["files"])
    for name in enumerated:
        assert (PIN / name).is_file()


def test_pinned_schema_matches_the_pinned_contract_type():
    """The two halves of the pin agree with each other: every field the pinned
    `ManifestFragment` type declares is a schema property, with the same required set —
    so validating against the schema is validating against the type the plugin compiles
    against."""
    contract = (PIN / "contract.ts").read_text(encoding="utf-8")
    body = re.search(r"interface ManifestFragment \{(.*?)\n\}", contract, flags=re.DOTALL)
    assert body, "pinned contract.ts has no ManifestFragment interface"
    text = re.sub(r"/\*.*?\*/", "", body.group(1), flags=re.DOTALL)
    fields = {m.group(1): m.group(2) != "?"
              for m in re.finditer(r"^\s+(\w+)(\??):", text, flags=re.MULTILINE)}
    assert set(SCHEMA["properties"]) == set(fields)
    assert set(SCHEMA["required"]) == {f for f, required in fields.items() if required}
