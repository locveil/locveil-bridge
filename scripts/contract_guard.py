#!/usr/bin/env python3
"""contract-guard — the Locveil contract-coherence checker (HK-5 / PROD-16).

Layer-1 enforcement from process/contracts.md §4: verifies what is GENERIC and LOCAL
about a repo's contract surfaces — layout, registry, STAMP/PIN shape, hashes of local
pinned copies, version-string consistency. It never checks semantics (that is the
per-repo conformance tests' job, §4 layer 2) and never reaches across repos (pin==tag
bytes is checked at re-pin time by the re-pin flow, not here).

Layout it enforces (process/contracts.md §2, owner ruling: uniform, immediate):

    contracts/
      README.md            the registry: every owned surface + every consumed pin,
                           direction-labeled
      <name>/              OWNED:    README.md + STAMP.json (+ artifacts)
      pins/<name>/         CONSUMED: artifact copies + PIN.json (+ owner STAMP verbatim)

STAMP.json core: {contract, version, tag, date, owner_repo}; tag == "<contract>-v<version>".
PIN.json core:   {contract, version, tag, owner_repo, owner_commit, pinned_by, pin_date,
                  files: {<relpath>: <sha256>}, conformance}.

Legacy tolerance: a PIN.json without a "files" map (pre-convention pin) or a pin folder
without PIN.json degrades to WARNINGS — the strict shape becomes mandatory at the pin's
next re-pin. Everything structural (loose files, unregistered folders, missing owned
STAMP/README, hash mismatches on strict pins) FAILS.

v3 (HK-12/PROD-26) — the omission rules:
  ORPHAN-TAG      a newer <contract>-vN git tag exists than the STAMP records (the
                  reverse of TAG-MISSING; keyed to registered contracts, never
                  tag-pattern sniffing — release tags like v0.6.0 can't match).
  CONTENT-DRIFT   a STAMP-enumerated artifact's bytes at HEAD differ from its bytes at
                  the STAMP's tag with no version move (only STAMPs carrying the
                  `artifacts` list opt in — package-style contracts whose HEAD advances
                  between tags simply don't enumerate).
  VENDORABLE-UNREGISTERED   a directory matching `vendorable_roots` in the optional
                  `.contract-guard.toml` that carries a package manifest but no owned
                  STAMP (allowlist: `non_contract`; folder→contract renames:
                  `contract_names`). Roots are explicit config, empty by default.
  --relax-tags    the pre-commit hook's mid-bump tolerance: TAG-MISSING and ORPHAN-TAG
                  degrade to warnings locally (a bump commit cannot carry its own tag);
                  CI runs strict.

v3.1 (IMPL-8, owner ruling Option B) — one canonical path form:
  ARTIFACTS-PATH  every `artifacts` entry MUST be a repo-root-relative path that
                  resolves to a file at HEAD. Bare names FAIL: v3 resolved them from
                  the repo root, so a bare `README.md` silently compared the ROOT
                  readme on both sides of the drift check (the bridge VWB-43 trap).
                  The legacy singular `artifact` field stays informational — never
                  validated (pre-convention folder-relative values are frozen history).

v4 (HK-13/PROD-28, IMPL-10) — one declaration, checked from both ends:
  ARTIFACTS-UNDECLARED  every STAMP declares `artifacts` (the list is what consumers pin
                  AND what CONTENT-DRIFT locks). STAMPs dated before 2026-10-05 only
                  WARN (legacy — fix at the contract's next cut). An EMPTY list needs a
                  `guard` pointer that resolves (ARTIFACTS-EMPTY-NO-GUARD).
  VERSION-FORM    STAMPs dated from 2026-10-05 carry a three-part version (X.Y.Z).
  RESERVED-NAME / DUPLICATE-NAME   pins are flat and `README.md` / `PIN.json` belong to
                  the consumer, so an owner never enumerates a file with a reserved
                  basename (README.md, PIN.json, STAMP.json) nor two files sharing one.
  STAMP-DRIFT     STAMP.json is an implicit artifact: its bytes at HEAD must equal its
                  bytes at its own tag (a STAMP absent at the tag WARNs — pre-stamp tag).
  PIN-INCOMPLETE  a pin's files must cover the `artifacts` of the owner STAMP it carries
                  (plus the STAMP itself). Pins stamped before 2026-10-05 only WARN.
  UNLISTED-FILE   now FAILS on strict pins (anything but PIN.json / README.md must be in
                  the PIN.json files map).
  POINTER-UNRESOLVED   `guard` / `code_constant` in a STAMP, `conformance` in a PIN.json
                  and in `.repin.toml`, `path` of a `[[tool]]` entry must resolve to a
                  file (the part before `::`). Legacy pins WARN.
  REGISTRY-VERSION     any `<family>-v<digits>` string in contracts/README.md must equal
                  the family's STAMP tag (owned), PIN tag (pin) or `.repin.toml`
                  `[[tool]]` pinned_tag — history lives in per-contract READMEs.

Distribution: locveil-contract-guard, single stdlib file, tags contract-guard-vX.Y.Z,
vendored per consumer at a pinned tag (the scope-guard consumption model). --check only:
this tool never mutates the tree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

__version__ = "4.0.0"  # contract-guard-v4.0.0 — the HK-13 rule set (IMPL-10)

STAMP_CORE = ("contract", "version", "tag", "date", "owner_repo")
PIN_CORE = ("contract", "version", "tag", "owner_repo", "pin_date")
PIN_RECOMMENDED = ("owner_commit", "pinned_by", "conformance")
META_FILES = {"PIN.json", "README.md"}  # the consumer's own files in a pin folder
RESERVED_NAMES = META_FILES | {"STAMP.json"}  # never enumerable by an owner (HK-13)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
HK13_DATE = "2026-10-05"  # STAMPs / pins dated before this are legacy: new rules WARN
THREE_PART_RE = re.compile(r"^\d+\.\d+\.\d+$")
STAMP_POINTERS = ("guard", "code_constant")


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def fail(self, msg: str) -> None:
        self.failures.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def _load_json(path: Path, rep: Report) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - report, don't crash
        rep.fail(f"UNPARSEABLE: {path} — {exc}")
        return None
    if not isinstance(data, dict):
        rep.fail(f"UNPARSEABLE: {path} — top level must be an object")
        return None
    return data


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_version_tag(kind: str, where: str, meta: dict, rep: Report, strict: bool) -> None:
    contract, version, tag = meta.get("contract"), meta.get("version"), meta.get("tag")
    if contract is None or version is None or tag is None:
        return  # missing-core is reported separately
    expected = f"{contract}-v{version}"
    if str(tag) != expected:
        msg = f"VERSION-MISMATCH: {where} — {kind} tag {tag!r} != '{expected}' (contract+version)"
        rep.fail(msg) if strict else rep.warn(msg + " [legacy pin — fix at next re-pin]")


def _local_tag_exists(root: Path, tag: str) -> bool | None:
    """True/False if determinable; None when not a git repo / git unavailable."""
    try:
        out = subprocess.run(["git", "-C", str(root), "tag", "-l", tag],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return tag in out.stdout.split()


def _local_tags(root: Path, pattern: str) -> list[str] | None:
    try:
        out = subprocess.run(["git", "-C", str(root), "tag", "-l", pattern],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.split() if out.returncode == 0 else None


def _family_version(contract: str, tag: str) -> tuple[int, ...] | None:
    m = re.fullmatch(rf"{re.escape(contract)}-v(\d+(?:\.\d+)*)", tag)
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def _is_legacy(date_value: object) -> bool:
    """True when a STAMP `date` / PIN `pin_date` predates HK-13 (or is unreadable)."""
    s = str(date_value or "")
    return not DATE_RE.match(s) or s[:10] < HK13_DATE


def _pointer_resolves(root: Path, value: object) -> bool:
    """A pointer names a repo-root-relative file, optionally `path::symbol`."""
    if not isinstance(value, str) or not value.strip():
        return False
    return (root / value.split("::", 1)[0].strip()).is_file()


def load_repin_config(root: Path) -> dict | None:
    """The consumer's .repin.toml, when present and parseable (read-only use)."""
    p = root / ".repin.toml"
    if not p.is_file():
        return None
    try:
        return tomllib.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - repin itself reports a broken config
        return None


def load_guard_config(root: Path) -> dict:
    """Optional .contract-guard.toml — HK-12 rules that need explicit per-repo config."""
    cfg = {"vendorable_roots": [], "non_contract": [], "contract_names": {}}
    p = root / ".contract-guard.toml"
    if p.is_file():
        try:
            raw = tomllib.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            return {**cfg, "_error": f"UNPARSEABLE: .contract-guard.toml — {exc}"}
        for key in cfg:
            if key in raw:
                cfg[key] = raw[key]
    return cfg


def check_owned(folder: Path, registry_text: str, rep: Report, root: Path,
                relax: bool = False) -> str | None:
    """Check one owned surface; returns its STAMP tag (for the registry rule)."""
    name = folder.name
    if name not in registry_text:
        rep.fail(f"UNREGISTERED: owned contract '{name}' not mentioned in contracts/README.md")
    if not (folder / "README.md").is_file():
        rep.fail(f"OWNED-NO-README: contracts/{name}/README.md missing (the normative guide)")
    stamp_path = folder / "STAMP.json"
    if not stamp_path.is_file():
        rep.fail(f"OWNED-NO-STAMP: contracts/{name}/STAMP.json missing")
        return None
    stamp = _load_json(stamp_path, rep)
    if stamp is None:
        return None
    legacy = _is_legacy(stamp.get("date"))
    missing = [k for k in STAMP_CORE if k not in stamp]
    if missing:
        rep.fail(f"STAMP-CORE: contracts/{name}/STAMP.json missing {missing}")
    if stamp.get("contract") not in (None, name):
        rep.fail(f"STAMP-NAME: contracts/{name}/STAMP.json says contract={stamp['contract']!r}")
    if "date" in stamp and not DATE_RE.match(str(stamp["date"])):
        rep.fail(f"STAMP-DATE: contracts/{name}/STAMP.json date {stamp['date']!r} not ISO (YYYY-MM-DD…)")
    _check_version_tag("STAMP", f"contracts/{name}/STAMP.json", stamp, rep, strict=True)
    # VERSION-FORM (HK-13 q2): cuts from 2026-10-05 on are always three-part.
    if not legacy and "version" in stamp and not THREE_PART_RE.match(str(stamp["version"])):
        rep.fail(f"VERSION-FORM: contracts/{name}/STAMP.json version {stamp['version']!r} — "
                 "cuts dated from 2026-10-05 are three-part (X.Y.Z; tag <family>-vX.Y.Z)")
    # POINTER-UNRESOLVED (HK-13 rider): pointer fields name real files.
    for field in STAMP_POINTERS:
        if field in stamp and not _pointer_resolves(root, stamp[field]):
            rep.fail(f"POINTER-UNRESOLVED: contracts/{name}/STAMP.json {field} "
                     f"{stamp[field]!r} does not resolve to a file (repo-root-relative, "
                     "optionally path::symbol)")
    # PROD-22: a STAMP naming a tag that was never created is a false green — the
    # consumer re-pins AGAINST the tag. Local tag object is the bar (remote push is
    # out of scope; a guard can't see the remote).
    tag = stamp.get("tag")
    tag_ok = None
    if tag:
        tag_ok = _local_tag_exists(root, str(tag))
        if tag_ok is False:
            msg = (f"TAG-MISSING: contracts/{name}/STAMP.json names '{tag}' but no such "
                   "git tag exists — create the tag in the same change as the STAMP bump")
            rep.warn(msg + " [relaxed: mid-bump local state]") if relax else rep.fail(msg)
        elif tag_ok is None:
            rep.warn(f"TAG-UNCHECKED: could not resolve git tags for contracts/{name} "
                     "(not a git repo or git unavailable)")

    # ORPHAN-TAG (HK-12 v3): the reverse direction — a newer family tag than the STAMP
    # records means someone tagged without bumping the stamp (or fixed content one
    # commit after the tag). Keyed to THIS registered contract's family only.
    tags = _local_tags(root, f"{name}-v*")
    if tag and tags is not None:
        stamp_v = _family_version(name, str(tag))
        versioned = [( _family_version(name, t), t) for t in tags
                     if _family_version(name, t)]
        if stamp_v is not None and versioned:
            newest_v, newest_t = max(versioned)
            if newest_v > stamp_v:
                msg = (f"ORPHAN-TAG: tag '{newest_t}' exists but contracts/{name}/"
                       f"STAMP.json still says '{tag}' — bump the STAMP in the same "
                       "change as the tag (HK-12)")
                rep.warn(msg + " [relaxed: mid-bump local state]") if relax else rep.fail(msg)

    # CONTENT-DRIFT (HK-12 v3): STAMP-enumerated artifacts must be byte-identical to
    # the STAMP's tag at HEAD — an edit without a version move is the satellite scar
    # (a fix landing one commit after the tag). Only `artifacts`-carrying STAMPs opt in;
    # package-style contracts whose HEAD advances between tags don't enumerate.
    # ARTIFACTS-PATH (IMPL-8 v3.1, Option B): one canonical form — repo-root-relative,
    # resolving to a file at HEAD. Validated whenever the list exists (independent of
    # tag state); a bad entry is excluded from the drift check below.
    # STAMP-DRIFT (HK-13 v4): the STAMP is an implicit artifact — it travels with every
    # pin, so its bytes at HEAD must equal its bytes at its own tag.
    if tag and tag_ok:
        stamp_rel = f"contracts/{name}/STAMP.json"
        try:
            tag_stamp = subprocess.run(
                ["git", "-C", str(root), "show", f"{tag}:{stamp_rel}"],
                capture_output=True, timeout=10, check=True).stdout
        except (OSError, subprocess.SubprocessError):
            rep.warn(f"STAMP-NOT-IN-TAG: contracts/{name} — '{tag}' does not carry "
                     f"{stamp_rel} (pre-stamp tag); the next cut must tag STAMP and "
                     "artifact together")
        else:
            if stamp_path.read_bytes() != tag_stamp:
                rep.fail(f"STAMP-DRIFT: {stamp_rel} at HEAD differs from its bytes at "
                         f"'{tag}' — a STAMP moves only with a version+tag (HK-13)")

    # ARTIFACTS-UNDECLARED (HK-13 v4): one declaration, two readers — what consumers
    # pin and what CONTENT-DRIFT locks. Missing key = legacy STAMP (WARN before
    # 2026-10-05); an empty list is legal only with a resolving `guard` pointer.
    arts = stamp.get("artifacts")
    if "artifacts" not in stamp:
        msg = (f"ARTIFACTS-UNDECLARED: contracts/{name}/STAMP.json has no `artifacts` "
               "list (HK-13: every STAMP declares it; empty only with a `guard` pointer)")
        rep.warn(msg + " [legacy STAMP — fix at the next cut]") if legacy else rep.fail(msg)
    elif not isinstance(arts, list):
        rep.fail(f"ARTIFACTS-UNDECLARED: contracts/{name}/STAMP.json `artifacts` must be a list")
    elif not arts and not _pointer_resolves(root, stamp.get("guard")):
        rep.fail(f"ARTIFACTS-EMPTY-NO-GUARD: contracts/{name}/STAMP.json declares an empty "
                 "`artifacts` list without a `guard` pointer that resolves to a file")
    if isinstance(arts, list):
        # RESERVED-NAME / DUPLICATE-NAME (HK-13 q4): pins are flat; README.md and
        # PIN.json in a pin folder are the consumer's, STAMP.json travels implicitly.
        seen: dict[str, str] = {}
        for art in arts:
            if not isinstance(art, str):
                continue
            base = art.rsplit("/", 1)[-1]
            if base in RESERVED_NAMES:
                rep.fail(f"RESERVED-NAME: contracts/{name} — artifacts entry '{art}' uses "
                         f"the reserved file name {base} (README.md/PIN.json belong to "
                         "the consumer's pin folder, STAMP.json is implicit; normative "
                         "prose goes in a named guide file — HK-13)")
            elif base in seen:
                rep.fail(f"DUPLICATE-NAME: contracts/{name} — '{art}' and '{seen[base]}' "
                         "share a file name; pins are flat (HK-13)")
            else:
                seen[base] = art
        valid: list[str] = []
        for art in arts:
            if not isinstance(art, str) or "/" not in art:
                rep.fail(f"ARTIFACTS-PATH: contracts/{name} — artifacts entry {art!r} is "
                         "not a repo-root-relative path (bare names resolved against the "
                         "repo root and corrupted the drift comparison — IMPL-8); write "
                         "it as '<dir>/…/<file>' and bump version+tag together")
            elif not (root / art).is_file():
                rep.fail(f"ARTIFACTS-PATH: contracts/{name} — artifacts entry '{art}' "
                         "does not resolve to a file at HEAD (repo-root-relative "
                         "required — IMPL-8)")
            else:
                valid.append(art)
        arts = valid
    if tag and tag_ok and isinstance(arts, list):
        for art in arts:
            try:
                tag_bytes = subprocess.run(
                    ["git", "-C", str(root), "show", f"{tag}:{art}"],
                    capture_output=True, timeout=10, check=True).stdout
            except (OSError, subprocess.SubprocessError):
                rep.warn(f"CONTENT-UNVERIFIABLE: contracts/{name} — '{art}' not readable "
                         f"at tag '{tag}' (path moved since the tag?)")
                continue
            # existence at HEAD is guaranteed by ARTIFACTS-PATH above
            if (root / art).read_bytes() != tag_bytes:
                rep.fail(f"CONTENT-DRIFT: contracts/{name} — '{art}' at HEAD differs "
                         f"from its bytes at '{tag}' with no version move — bump "
                         "version+tag together or revert (HK-12)")
    return str(tag) if tag else None


def check_pin(folder: Path, registry_text: str, rep: Report, root: Path) -> str | None:
    """Check one consumed pin; returns its PIN tag (for the registry rule)."""
    name = folder.name
    where = f"contracts/pins/{name}"
    if name not in registry_text:
        rep.fail(f"UNREGISTERED: consumed pin '{name}' not mentioned in contracts/README.md")
    pin_path = folder / "PIN.json"
    if not pin_path.is_file():
        rep.warn(f"PIN-PENDING: {where}/PIN.json missing — legacy/co-owned pin; "
                 "strict PIN.json becomes mandatory at the next re-pin")
        return None
    pin = _load_json(pin_path, rep)
    if pin is None:
        return None
    pin_tag = str(pin["tag"]) if pin.get("tag") else None
    legacy = _is_legacy(pin.get("pin_date"))
    strict = isinstance(pin.get("files"), dict)
    missing = [k for k in PIN_CORE if k not in pin]
    if missing:
        msg = f"PIN-CORE: {where}/PIN.json missing {missing}"
        rep.fail(msg) if strict else rep.warn(msg + " [legacy pin — fix at next re-pin]")
    if pin.get("contract") not in (None, name):
        rep.fail(f"PIN-NAME: {where}/PIN.json says contract={pin['contract']!r}")
    _check_version_tag("PIN", f"{where}/PIN.json", pin, rep, strict=strict)
    if not strict:
        rep.warn(f"PIN-LEGACY: {where}/PIN.json has no 'files' hash map — upgrade at next re-pin")
        return pin_tag
    for rec in PIN_RECOMMENDED:
        if rec not in pin:
            rep.warn(f"PIN-RECOMMENDED: {where}/PIN.json lacks '{rec}'")
    listed = pin["files"]
    for rel, want in listed.items():
        target = folder / rel
        if not target.is_file():
            rep.fail(f"MISSING-PINNED-FILE: {where}/{rel} listed in PIN.json but absent")
            continue
        got = _sha256(target)
        if got != want:
            rep.fail(f"HASH-MISMATCH: {where}/{rel} — sha256 {got[:12]}… != PIN.json {str(want)[:12]}…")
    for child in sorted(folder.iterdir()):
        if child.is_file() and child.name not in META_FILES and child.name not in listed:
            rep.fail(f"UNLISTED-FILE: {where}/{child.name} is neither reserved (PIN.json, "
                     "README.md) nor covered by the PIN.json files map (HK-13)")

    def soft(msg: str) -> None:  # HK-13: pins stamped before the rule only warn
        rep.warn(msg + " [legacy pin — fix at next re-pin]") if legacy else rep.fail(msg)

    # PIN-INCOMPLETE (HK-13 v4): the pin covers the artifacts of the owner STAMP it
    # carries — the owner's declaration is the single source of the pin file set.
    stamp_path = folder / "STAMP.json"
    if "STAMP.json" not in listed or not stamp_path.is_file():
        if pin_tag:
            soft(f"PIN-INCOMPLETE: {where} does not carry the owner's STAMP.json "
                 "(a pin = artifacts + owner STAMP verbatim)")
    else:
        try:
            owner_stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            owner_stamp = None
        owner_arts = owner_stamp.get("artifacts") if isinstance(owner_stamp, dict) else None
        if not isinstance(owner_arts, list):
            rep.warn(f"PIN-COMPLETENESS-UNVERIFIABLE: {where}/STAMP.json enumerates no "
                     "`artifacts` (owner cut predates HK-13) — verified at the next re-pin")
        else:
            for art in owner_arts:
                base = str(art).rsplit("/", 1)[-1]
                if base in RESERVED_NAMES:
                    soft(f"PIN-INCOMPLETE: {where} — owner STAMP enumerates reserved name "
                         f"'{art}', which a flat pin cannot carry; re-pin at an owner cut "
                         "that names its guide file")
                elif base not in listed:
                    soft(f"PIN-INCOMPLETE: {where} — owner artifact '{art}' is not in the "
                         "pin (a pin is always COMPLETE — process/contracts.md §2)")

    # POINTER-UNRESOLVED (HK-13 rider): the conformance pointer names a real test.
    conf = pin.get("conformance")
    if conf is None:
        rep.warn(f"PIN-NO-CONFORMANCE: {where}/PIN.json names no conformance test")
    elif not _pointer_resolves(root, conf):
        soft(f"POINTER-UNRESOLVED: {where}/PIN.json conformance {conf!r} does not "
             "resolve to a file in this repo (repo-root-relative, optionally path::node)")
    return pin_tag


def check_vendorable(root: Path, gcfg: dict, rep: Report) -> None:
    """VENDORABLE-UNREGISTERED (HK-12 v3): a package-manifest-carrying dir under an
    explicit vendorable root must be a registered owned surface or allowlisted."""
    for pattern in gcfg["vendorable_roots"]:
        for d in sorted(root.glob(pattern)):
            if not d.is_dir():
                continue
            if not ((d / "pyproject.toml").is_file() or (d / "package.json").is_file()):
                continue
            base = d.name
            if base in gcfg["non_contract"]:
                continue
            cname = gcfg["contract_names"].get(base, base)
            if not (root / "contracts" / cname / "STAMP.json").is_file():
                rep.fail(f"VENDORABLE-UNREGISTERED: {d.relative_to(root).as_posix()} "
                         f"carries a package manifest but contracts/{cname}/STAMP.json "
                         "does not exist — cut the owned surface in the same change, or "
                         "list it under non_contract in .contract-guard.toml (HK-12)")


def check_registry_versions(registry_text: str, expected: dict[str, str | None],
                            rep: Report) -> None:
    """REGISTRY-VERSION (HK-13 rider): a `<family>-v<digits>` string in the registry
    must equal the family's current tag — history belongs in per-contract READMEs."""
    for family, tag in sorted(expected.items()):
        if not tag:
            continue
        pattern = rf"(?<![\w.-]){re.escape(family)}-v\d+(?:\.\d+)*"
        for found in sorted(set(re.findall(pattern, registry_text))):
            if found != tag:
                rep.fail(f"REGISTRY-VERSION: contracts/README.md says '{found}' but "
                         f"'{family}' is at '{tag}' — the registry carries only current "
                         "versions (HK-13)")


def check_repin_pointers(root: Path, rcfg: dict, rep: Report) -> dict[str, str | None]:
    """POINTER-UNRESOLVED over .repin.toml (in-repo dests and vendored tools); returns
    the `[[tool]]` family → pinned_tag map for the registry rule."""
    for fam in rcfg.get("family", []) or []:
        for dest in fam.get("dest", []) or []:
            path = str(dest.get("path", ""))
            conf = dest.get("conformance")
            if path.startswith("..") or conf is None:
                continue  # cross-repo dest: resolved in the repo that holds the pin
            if not _pointer_resolves(root, conf):
                rep.fail(f"POINTER-UNRESOLVED: .repin.toml family '{fam.get('name')}' "
                         f"conformance {conf!r} does not resolve to a file")
    tools: dict[str, str | None] = {}
    for tool in rcfg.get("tool", []) or []:
        if "path" in tool and not (root / str(tool["path"])).is_file():
            rep.fail(f"POINTER-UNRESOLVED: .repin.toml tool '{tool.get('name')}' path "
                     f"{tool['path']!r} does not exist")
        if tool.get("family"):
            tools[str(tool["family"])] = tool.get("pinned_tag")
    return tools


def run_check(root: Path, relax: bool = False) -> Report:
    rep = Report()
    gcfg = load_guard_config(root)
    if "_error" in gcfg:
        rep.fail(gcfg["_error"])
    contracts = root / "contracts"
    if not contracts.is_dir():
        check_vendorable(root, gcfg, rep)  # a vendorable root with NO contracts/ at all
        return rep
    registry = contracts / "README.md"
    if not registry.is_file():
        rep.fail("NO-REGISTRY: contracts/README.md missing (the direction-labeled index)")
        registry_text = ""
    else:
        registry_text = registry.read_text(encoding="utf-8")

    expected: dict[str, str | None] = {}  # family -> current tag, for REGISTRY-VERSION
    rcfg = load_repin_config(root)
    if rcfg is not None:
        expected.update(check_repin_pointers(root, rcfg, rep))
    for child in sorted(contracts.iterdir()):
        if child.is_file():
            if child.name != "README.md":
                rep.fail(f"LOOSE-FILE: contracts/{child.name} — everything lives in "
                         "contracts/<name>/ or contracts/pins/<name>/ (process/contracts.md §2)")
        elif child.name == "pins":
            for pin_child in sorted(child.iterdir()):
                if pin_child.is_file():
                    rep.fail(f"LOOSE-FILE: contracts/pins/{pin_child.name} — pins live in "
                             "contracts/pins/<name>/ subfolders")
                else:
                    pin_tag = check_pin(pin_child, registry_text, rep, root)
                    expected.setdefault(pin_child.name, pin_tag)
        else:
            # an owned surface wins over a same-named tool/pin entry
            expected[child.name] = check_owned(child, registry_text, rep, root, relax=relax)
    check_registry_versions(registry_text, expected, rep)
    check_vendorable(root, gcfg, rep)
    return rep


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path.cwd(),
                        help="repo root (default: cwd)")
    parser.add_argument("--check", action="store_true",
                        help="run the check (the default and only action)")
    parser.add_argument("--relax-tags", action="store_true",
                        help="mid-bump tolerance for the pre-commit hook: TAG-MISSING "
                             "and ORPHAN-TAG warn instead of failing; CI runs strict")
    parser.add_argument("--version", action="version",
                        version=f"contract-guard {__version__}")
    args = parser.parse_args(argv)

    rep = run_check(args.root.resolve(), relax=args.relax_tags)
    print(f"== contract-guard {__version__} · root {args.root.resolve()} ==")
    for w in rep.warnings:
        print(f"  WARN  {w}")
    for f in rep.failures:
        print(f"  FAIL  {f}")
    if rep.failures:
        print(f"\nFAIL: {len(rep.failures)} contract-coherence violation(s)"
              f" ({len(rep.warnings)} warning(s)).")
        return 1
    print(f"\nOK: contract coherence holds ({len(rep.warnings)} warning(s)).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
