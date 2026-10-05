"""DOC-13 (PROD-17, council HK-6): the docs-manifest coherence test — the per-repo
half of the documentation-is-part-of-done enforcement (the scope-guard checks only the
verdict line's presence; everything manifest-aware lives here, on the drift-guard
pattern, in the normal suite).

Checks:
- the manifest validates against the PINNED org schema — family `docs-manifest-schema`,
  owned by locveil-commons, pinned at contracts/pins/docs-manifest-schema/ by the
  vendored repin tool (hermetic: CI never reaches across repos). The manifest itself is
  instance data, not a contract; this file is the pin's named conformance test
  (DOC-19, council HK-13 — the hand-kept schema copy and the internal stamp retired);
- node <-> tree bijection over the declared roots: every file under a root has a node,
  every node's path exists (unless status=pending-gate);
- diagram nodes are .dot/.png PAIRS with the same basename — one unit;
- the required-class floor holds (this repo has every capability: front-door,
  quickstart, operator, end-user, canonical-reference, contributor);
- docs-verdict node-ids in DONE-ledger completion entries resolve to manifest nodes.

Deferred slice (documented, not silently dropped): the falsifiability check — a verdict
of `none` on a change that touched a mapped surface glob — needs per-completion diffs
the test tier cannot see; it arrives with the shared tooling at rule-of-two
(process/user-docs.md §3).
"""

import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

pytestmark = pytest.mark.unit

BACKEND = Path(__file__).resolve().parents[2]
REPO = BACKEND.parent

MANIFEST = json.loads((REPO / "docs" / "manifest.json").read_text(encoding="utf-8"))
PIN_DIR = REPO / "contracts" / "pins" / "docs-manifest-schema"
SCHEMA = json.loads((PIN_DIR / "manifest.schema.json").read_text(encoding="utf-8"))
NODES = MANIFEST["nodes"]
NODE_IDS = {n["id"] for n in NODES}

FLOOR_CLASSES = {"front-door", "quickstart", "operator", "end-user",
                 "canonical-reference", "contributor"}


def test_manifest_validates_against_pinned_schema():
    Draft202012Validator.check_schema(SCHEMA)
    Draft202012Validator(SCHEMA).validate(MANIFEST)


def test_schema_comes_from_the_pin_and_nowhere_else():
    """One schema copy in the repo: the pin (PIN.json names it, the owner STAMP beside
    it enumerates it). A second hand-kept copy is how the old one drifted unseen."""
    pin = json.loads((PIN_DIR / "PIN.json").read_text(encoding="utf-8"))
    assert pin["contract"] == "docs-manifest-schema"
    assert "manifest.schema.json" in pin["files"]
    stamp = json.loads((PIN_DIR / "STAMP.json").read_text(encoding="utf-8"))
    assert [Path(a).name for a in stamp["artifacts"]] == ["manifest.schema.json"]
    assert not (REPO / "contracts" / "docs-manifest").exists(), (
        "contracts/docs-manifest/ is retired — the manifest is instance data"
    )
    copies = [p for p in (REPO / "contracts").rglob("manifest.schema.json")]
    assert copies == [PIN_DIR / "manifest.schema.json"]


def test_node_ids_are_unique():
    assert len(NODE_IDS) == len(NODES)


def test_every_node_path_exists_unless_pending_gate():
    for n in NODES:
        if n["status"] == "pending-gate":
            continue
        assert (REPO / n["path"]).is_file(), f"node {n['id']}: {n['path']} missing on disk"


def test_diagram_nodes_are_dot_render_pairs():
    for n in NODES:
        if n["class"] != "diagram":
            continue
        dot = REPO / n["path"]
        assert dot.suffix == ".dot", f"diagram node {n['id']} must point at the .dot source"
        assert dot.with_suffix(".png").is_file(), (
            f"diagram {n['id']}: rendered {dot.with_suffix('.png').name} missing — "
            ".dot source and render are one unit"
        )


def test_tree_to_node_bijection_over_roots():
    """Every file under a declared root is registered. Registration IS the manifest
    edit — a doc committed without a node fails here (process/user-docs.md §4)."""
    registered = {n["path"] for n in NODES}
    # a diagram node registers its render implicitly
    registered |= {str(Path(p).with_suffix(".png")) for p in registered if p.endswith(".dot")}
    unregistered = []
    for root in MANIFEST["roots"]:
        target = REPO / root
        files = [target] if target.is_file() else sorted(target.rglob("*"))
        for f in files:
            if f.is_dir():
                continue
            rel = str(f.relative_to(REPO))
            if rel not in registered:
                unregistered.append(rel)
    assert not unregistered, (
        f"user-facing files without a manifest node: {unregistered} — add a node in "
        "docs/manifest.json in the same change (or tombstone per the node policy)"
    )


def test_required_class_floor():
    """Floor applies where the capability exists — this repo has them all
    (HK-6 round-2). Removing the last node of a floor class without a same-change
    replacement is a coverage loss and fails here."""
    present = {n["class"] for n in NODES}
    missing = FLOOR_CLASSES - present
    assert not missing, f"floor classes with no node: {missing}"


def test_surfaces_stay_small_and_globs_are_lists():
    surfaces = MANIFEST["surfaces"]
    assert len(surfaces) <= 10
    covered = {s for n in NODES for s in n.get("covers", [])}
    unknown = covered - set(surfaces)
    assert not unknown, f"nodes cover undeclared surfaces: {unknown}"


def test_done_ledger_docs_verdicts_resolve():
    """`docs: <node-ids>` lines in completion entries must name real nodes
    (`docs: none — <why>` passes syntactically; scope-guard owns presence)."""
    done = (REPO / "docs" / "action_plan_DONE.md").read_text(encoding="utf-8")
    for m in re.finditer(r"\bdocs:\s*([^\n]+)", done):
        verdict = m.group(1).strip()
        if verdict.startswith("none"):
            continue
        ids = [t.strip().rstrip(".") for t in verdict.split(",")]
        for i in ids:
            if not re.fullmatch(r"[a-z0-9][a-z0-9/_-]*", i):
                break  # prose after the id list — stop consuming
            assert i in NODE_IDS, f"docs verdict names unknown node id {i!r}"
