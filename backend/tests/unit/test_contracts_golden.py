"""VWB-15: the committed contract artifacts must never go stale (the drift guard).

Regenerates the golden catalog (offline, deterministic — the same builder the
`locveil-catalog` CLI uses) and the OpenAPI schema, and fails if the committed copies in
`contracts/catalog/` (and the UI-consumed `backend/openapi.json`) differ. Runs inside
the normal backend test job, so the check is self-contained in CI: any config /
capability-map / API change that alters the contract without a re-dump fails here
with a one-command fix.

Fix on failure (from the REPO ROOT — a regeneration that moves a pinned file is a
contract cut, so bump `CONTRACT_VERSION` first: patch for a config-driven golden
refresh, minor/major when the surface changed; then tag the landing commit):
    uv run --project backend locveil-catalog --stamp contracts/catalog/STAMP.json
    uv run --project backend locveil-openapi -o backend/openapi.json && cp backend/openapi.json contracts/catalog/openapi.json

The same file holds the version triple together (code constant, STAMP, tag string) and
the shape of the pinned set (`STAMP_ARTIFACTS` — the guide in, the folder README out).
"""

import json
import re
from pathlib import Path

import pytest

from locveil_bridge.cli.dump_catalog import STAMP_ARTIFACTS, build_offline_catalog
from locveil_bridge.cli.dump_openapi import generate_openapi
from locveil_bridge.presentation.api.catalog import CONTRACT_VERSION

pytestmark = pytest.mark.unit

BACKEND = Path(__file__).resolve().parents[2]
REPO = BACKEND.parent
CONTRACTS = REPO / "contracts" / "catalog"

_REGEN_HINT = (
    "contract artifact stale — regenerate FROM THE REPO ROOT: "
    "`uv run --project backend locveil-catalog --stamp contracts/catalog/STAMP.json` "
    "(and `uv run --project backend locveil-openapi -o backend/openapi.json && "
    "cp backend/openapi.json contracts/catalog/openapi.json` for the API schema). "
    "A regeneration that moves a pinned file is a contract cut: bump CONTRACT_VERSION "
    "first (patch for a config-driven golden refresh), then tag the landing commit."
)


def test_golden_catalog_matches_configs(monkeypatch):
    # The offline build resolves device cert paths (config/devices/certs/*.pem) relative to
    # CWD = the deployment root, which since CORE-11 is the repo root (config/ lives there,
    # mirroring the container's /app + the /app/config mount). Build from there, exactly like
    # the real regeneration command.
    monkeypatch.chdir(REPO)
    committed = json.loads((CONTRACTS / "catalog.golden.json").read_text(encoding="utf-8"))
    regenerated = build_offline_catalog("config").model_dump()
    assert regenerated == committed, _REGEN_HINT


def test_openapi_pin_matches_app():
    regenerated = generate_openapi()
    committed_backend = json.loads((BACKEND / "openapi.json").read_text(encoding="utf-8"))
    assert regenerated == committed_backend, _REGEN_HINT
    committed_contract = json.loads((CONTRACTS / "openapi.json").read_text(encoding="utf-8"))
    assert regenerated == committed_contract, _REGEN_HINT


def _golden():
    return json.loads((CONTRACTS / "catalog.golden.json").read_text(encoding="utf-8"))


def _device(golden, device_id):
    return next(d for d in golden["devices"] if d["id"] == device_id)


def test_contract_v11_params_are_typed_with_units():
    """VWB-20/G1+G4: every action param descriptor is CatalogParam-shaped and
    semantic units ride the descriptor (voice parses «двадцать два градуса»
    against a °C-shaped target)."""
    golden = _golden()
    hvac = _device(golden, "living_room_hvac")
    temperature = next(c for c in hvac["capabilities"] if c["name"] == "temperature")
    setpoint = next(a for a in temperature["actions"] if a["name"] == "set")
    (value,) = setpoint["params"]
    assert value["name"] == "value"  # DRV-28: the canonical `set {value}` convention
    assert value["unit"] == "°C" and value["min"] == 16.0 and value["max"] == 31.0

    processor = _device(golden, "processor")
    volume = next(c for c in processor["capabilities"] if c["name"] == "volume")
    vol_set = next(a for a in volume["actions"] if a["name"] == "set")
    level = next(p for p in vol_set["params"] if p["name"] == "level")
    assert level["unit"] == "dB"


def test_contract_v11_scenario_labels_are_localized():
    """VWB-20/G3: the scenario enum carries ru labels — «включи кино» has a surface."""
    golden = _golden()
    sm = _device(golden, "scenario_manager_living_room")
    scenario = next(c for c in sm["capabilities"] if c["name"] == "scenario")
    set_action = next(a for a in scenario["actions"] if a["name"] == "set")
    for v in set_action["params"][0]["values"]:
        assert v["labels"].get("ru"), f"scenario {v['canonical']} lacks a ru label"
        assert v["labels"].get("en")


def test_contract_v11_dynamic_sets_carry_options_from():
    """VWB-20/G5 (corrected): app launching is an intentionally OPEN set — the param
    points at the runtime options endpoint instead of lying with a static enum."""
    golden = _golden()
    tv = _device(golden, "living_room_tv")
    apps = next(c for c in tv["capabilities"] if c["name"] == "apps")
    launch = next(a for a in apps["actions"] if a["params"])
    app_param = launch["params"][0]
    assert app_param["options_from"] == "apps"
    assert app_param["values"] is None


def test_contract_v11_no_empty_capability_husks():
    """VWB-20 minor flag: a capability with neither actions nor fields is suppressed
    (the TVs' select-form `input` — reappears when VWB-19 makes select routable)."""
    golden = _golden()
    for d in golden["devices"]:
        for c in d["capabilities"]:
            assert c["actions"] or c["fields"], f"{d['id']}.{c['name']} is an empty husk"


def test_stamp_carries_contract_core_and_names_a_bridge_build():
    """Since catalog-v1.5 (the PROD-16 convention cut) the STAMP carries the org core
    {contract, version, tag, date, owner_repo} sourced from CONTRACT_VERSION, plus the
    build extras. The version triple — code constant, STAMP, tag string — must agree."""
    stamp = json.loads((CONTRACTS / "STAMP.json").read_text(encoding="utf-8"))
    assert set(stamp) == {
        "contract", "version", "tag", "date", "owner_repo", "artifacts",
        "bridge_commit", "bridge_version", "catalog_version",
    }
    # The pinned set: what a consumer's pin copies and what the drift rule byte-locks.
    # Repo-root-relative since catalog-v1.8; since catalog-v1.10.0 the normative prose
    # is the named guide and the README is OUT of the set (see the shape test below).
    assert stamp["artifacts"] == list(STAMP_ARTIFACTS), _REGEN_HINT
    assert set(stamp["artifacts"]) == {
        "contracts/catalog/catalog.golden.json",
        "contracts/catalog/openapi.json",
        "contracts/catalog/catalog-contract.md",
    }
    assert stamp["contract"] == "catalog"
    assert stamp["version"] == CONTRACT_VERSION, _REGEN_HINT
    assert stamp["tag"] == f"catalog-v{CONTRACT_VERSION}", _REGEN_HINT
    assert stamp["owner_repo"] == "locveil-bridge"
    committed = json.loads((CONTRACTS / "catalog.golden.json").read_text(encoding="utf-8"))
    # the stamp's catalog hash must match the committed golden (they travel together)
    assert stamp["catalog_version"] == committed["version"], _REGEN_HINT


def test_stamp_version_is_three_part():
    """Since catalog-v1.10.0 versions are MAJOR.MINOR.PATCH and tags are three-part
    (major = breaking, minor = surface changed, patch = enumerated bytes moved with no
    surface change — e.g. a config-driven golden refresh)."""
    assert re.fullmatch(r"\d+\.\d+\.\d+", CONTRACT_VERSION), (
        f"CONTRACT_VERSION {CONTRACT_VERSION!r} must be three-part (X.Y.Z)"
    )


def test_pinned_set_shape_survives_a_flat_pin_folder():
    """A consumer's pin folder is FLAT and two names in it belong to the consumer
    (README.md, PIN.json); STAMP.json travels implicitly. So the owner never enumerates
    a file with a reserved name, never two files sharing a name — and every enumerated
    path exists. The normative guide is in the set; the folder README is not."""
    names = [Path(a).name for a in STAMP_ARTIFACTS]
    assert not {"README.md", "PIN.json", "STAMP.json"} & set(names)
    assert len(names) == len(set(names)), f"duplicate file names in the pinned set: {names}"
    for artifact in STAMP_ARTIFACTS:
        assert (REPO / artifact).is_file(), f"enumerated artifact missing: {artifact}"
    assert "contracts/catalog/catalog-contract.md" in STAMP_ARTIFACTS


def test_guide_holds_the_normative_text_and_the_readme_points_at_it():
    """The split's two halves stay put: param semantics + the versioning rule live in
    the pinned guide; the unlocked README links to it instead of restating it."""
    guide = (CONTRACTS / "catalog-contract.md").read_text(encoding="utf-8")
    for heading in ("## Param semantics", "## Versioning"):
        assert heading in guide, f"catalog-contract.md lost its {heading!r} section"
    readme = (CONTRACTS / "README.md").read_text(encoding="utf-8")
    assert "(catalog-contract.md)" in readme
    assert "## Param semantics" not in readme


def test_contract_v13_hvac_action_params_carry_field_value_tables():
    """VWB-24 property, DRV-28 shape: each enum capability's `set {value}` param is
    typed with the same {wire, canonical, labels} table its state field carries — a
    closed set a voice consumer validates against with zero round-trips. The
    derivation rule is now `value` → the capability's state_field table (the VWB-19
    set-{value} convention); the table stays authored ONCE, on the field."""
    golden = _golden()
    for device_id in ("bedroom_hvac", "children_room_hvac", "living_room_hvac"):
        hvac = _device(golden, device_id)
        caps = {c["name"]: c for c in hvac["capabilities"]}
        assert set(caps) == {"power", "mode", "fan", "vane", "widevane", "temperature"}
        for cap_name in ("mode", "fan", "vane", "widevane"):
            cap = caps[cap_name]
            field = next(f for f in cap["fields"] if f["name"] == cap_name)
            action = next(a for a in cap["actions"] if a["name"] == "set")
            (param,) = action["params"]
            assert param["name"] == "value", f"{device_id}.{cap_name}"
            assert param["values"] == field["values"], (
                f"{device_id}.{cap_name}.set(value) must mirror the field table"
            )
        mode_values = {v["canonical"]: v for v in caps["mode"]["fields"][0]["values"]}
        assert mode_values["cool"]["labels"]["ru"] == "охлаждение"
