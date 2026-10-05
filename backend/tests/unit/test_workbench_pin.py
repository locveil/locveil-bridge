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

UI-23: the pinned `contract.ts` is also the file the plugin COMPILES against. The plugin
imports its contract types from `locveil-workbench/contract`; TypeScript resolves that
specifier through a `paths` mapping in `workbench-plugin/tsconfig.json` to the pinned
file, and no `locveil-workbench` package is installed at all. The tests at the bottom
hold that arrangement in place from the configuration side — the mapping exists and
points at the pin, the dependency is gone from `package.json` and the lockfile, the
type-check runs on the mapped config, and every plugin import of the contract is
type-only (erased at build, so nothing needs the package at runtime either). The
plugin's CI job proves the same from the compiler's side: after the build it asks
`tsc --listFilesOnly` which files were compiled.
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

CONTRACT_PACKAGE = "locveil-workbench"
CONTRACT_SPECIFIER = "locveil-workbench/contract"


def load_jsonc(path: Path) -> dict:
    """tsconfig.json is JSON with comments and optional trailing commas. Strings are
    matched first so that a `/*` inside one (the `"@/*"` path key) is left alone."""
    token = re.compile(r'''"(?:\\.|[^"\\])*"|/\*.*?\*/|//[^\n]*|,(?=\s*[}\]])''', re.DOTALL)
    text = token.sub(lambda m: m.group(0) if m.group(0).startswith('"') else "",
                     path.read_text(encoding="utf-8"))
    return json.loads(text)


TSCONFIG = load_jsonc(PLUGIN / "tsconfig.json")


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


# --- UI-23: the pinned contract.ts is what the plugin compiles against -----------------


def tsconfig_path_target(specifier: str) -> Path:
    """Where the plugin's tsconfig sends `specifier` — exactly one target, no fallback."""
    options = TSCONFIG["compilerOptions"]
    targets = options.get("paths", {}).get(specifier)
    assert targets, f"workbench-plugin/tsconfig.json has no `paths` entry for {specifier!r}"
    assert len(targets) == 1, f"{specifier!r} must map to ONE file, got {targets}"
    return (PLUGIN / options.get("baseUrl", ".") / targets[0]).resolve()


def test_jsonc_loader_keeps_strings_and_drops_comments(tmp_path):
    sample = tmp_path / "tsconfig.json"
    sample.write_text(
        '{ /* block */ "paths": { "@/*": ["./src/*"], // line\n "a": ["b"], }, }',
        encoding="utf-8",
    )
    assert load_jsonc(sample) == {"paths": {"@/*": ["./src/*"], "a": ["b"]}}


def test_contract_import_resolves_to_the_pinned_file():
    assert tsconfig_path_target(CONTRACT_SPECIFIER) == (PIN / "contract.ts").resolve()
    # no second route: a wildcard or sibling key for the package would be a way around
    routes = [k for k in TSCONFIG["compilerOptions"]["paths"] if CONTRACT_PACKAGE in k]
    assert routes == [CONTRACT_SPECIFIER]
    # one tsconfig, nothing inherited — the mapping read here is the whole story
    assert "extends" not in TSCONFIG


def test_pinned_file_gets_react_types_from_the_plugin_itself():
    """The pinned contract.ts imports React types and lives outside any node_modules.
    Without this entry the import would resolve from whatever sits above the repo on the
    machine at hand (or not at all); with it, from the plugin's own declared types."""
    assert "import type * as React from \"react\"" in (PIN / "contract.ts").read_text(
        encoding="utf-8")
    assert tsconfig_path_target("react") == (PLUGIN / "node_modules" / "@types" / "react").resolve()
    assert "@types/react" in PACKAGE["devDependencies"]


def test_typecheck_runs_on_the_mapped_tsconfig():
    """`tsc` with no `-p` reads workbench-plugin/tsconfig.json — the file checked above.
    A script pointing the compiler at another project file would bypass the mapping."""
    assert PACKAGE["scripts"]["typecheck"] == "tsc"
    assert PACKAGE["scripts"]["build"].startswith("tsc && ")
    assert PACKAGE["scripts"]["check"].startswith("npm run typecheck")


def test_no_dependency_on_the_live_contract_package():
    """The contract reaches the plugin as pinned bytes only. A `locveil-workbench`
    dependency (historically a `file:` link into the commons checkout) would put a
    second, unpinned copy of the types within the compiler's reach."""
    for section in ("dependencies", "devDependencies", "peerDependencies",
                    "optionalDependencies"):
        for name, spec in PACKAGE.get(section, {}).items():
            assert name != CONTRACT_PACKAGE, f"{CONTRACT_PACKAGE} is back in {section}"
            assert "packages/workbench" not in spec, f"{name} in {section} links to {spec}"
    lock = (PLUGIN / "package-lock.json").read_text(encoding="utf-8")
    assert CONTRACT_PACKAGE not in lock
    assert "packages/workbench" not in lock
    # nor a bundler alias that would resolve the specifier at build time
    assert CONTRACT_PACKAGE not in VITE_CONFIG


def test_plugin_sources_import_only_types_and_only_from_the_contract_entry():
    """Every mention of the package in the plugin sources is
    `import type … from 'locveil-workbench/contract'`. Type-only imports are erased by
    the build, which is why no installed package is needed; a value import, a deeper
    path, or a dynamic import would need one."""
    statement = re.compile(
        r"""\bimport\s+type\b[^;'"`]*?\bfrom\s*(['"])(?P<spec>[^'"]+)\1""", re.DOTALL)
    mention = re.compile(r"""(['"`])[^'"`\n]*locveil-workbench[^'"`\n]*\1""")
    total = 0
    for source in sorted((PLUGIN / "src").rglob("*.ts*")):
        text = source.read_text(encoding="utf-8")
        typed = [m for m in statement.finditer(text) if CONTRACT_PACKAGE in m.group("spec")]
        for m in typed:
            assert m.group("spec") == CONTRACT_SPECIFIER, f"{source.name}: {m.group('spec')}"
        mentions = mention.findall(text)
        assert len(mentions) == len(typed), (
            f"{source.relative_to(REPO)}: a reference to {CONTRACT_PACKAGE} that is not "
            f"`import type … from '{CONTRACT_SPECIFIER}'`")
        assert "locveil-commons" not in text, f"{source.relative_to(REPO)} reaches into commons"
        total += len(typed)
    assert total > 0, "the plugin no longer imports the contract — this guard is vacuous"
