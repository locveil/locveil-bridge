#!/usr/bin/env python3
"""repin — consumed-contract re-pin + staleness check (HK-12/PROD-26; v2 HK-13/PROD-28).

Each consumed contract family declared in the repo's `.repin.toml` is fetched from its
OWNER repo's committed artifacts at a family-named tag (`<family>-vX.Y.Z`), copied
verbatim into the pin folder(s), and stamped with a strict `PIN.json` (core fields +
`files` sha256 map + conformance pointer) that the vendored contract-guard verifies on
every commit.

    python3 repin.py <family> [--tag TAG]      re-pin (default: the owner's newest tag)
    python3 repin.py tool <name> [--tag TAG]   re-vendor a shared tool at its owner's tag
    python3 repin.py --check [--fail-on X] [--touched BASE]   staleness report

v2 (HK-13) — the owner's STAMP is the single source of the pin file set: the files
pinned are the `artifacts` the owner's STAMP enumerates AT THE TAG, plus the STAMP
itself, flat (file names, not paths). A consumer-side `files` list survives only as the
fallback for owner tags cut before the owner enumerated. Reserved names (README.md,
PIN.json — the consumer's own files in a pin folder) can never be pinned.

Severity (process/contracts.md §5) is the caller's choice of --fail-on:
    none   pre-commit warn stage — always exit 0
    major  ordinary CI — a MAJOR-version family gap or a never-pinned family
    minor  release / image-dispatch gates — a minor-or-major family gap (HK-13 q6)
    any    everything, including patch gaps and vendored-tool gaps
Vendored-tool (`[[tool]]`) version gaps fail only under `any` — a commons tool tag must
never block a hotfix image; a LOCALLY EDITED vendored tool (sha256 mismatch) fails at
every level but `none`. The config's `default_fail_on` applies when the flag is omitted.

--touched BASE implements touch-the-family once, here: a family whose pin folder or
conformance test changed since BASE (`git diff --name-only BASE HEAD`) fails on ANY
staleness, whatever --fail-on says — working against a stale pin is an error now.

Tag lookup is REMOTE-FIRST via tokenless `git ls-remote --tags <owner_url>` (the org repos
are public — recorded HK-12 assumption); on network failure it falls back to the on-disk
sibling's tags with a WARN carrying fetch age. Never network-required-to-commit. A family
with no tag yet: re-pin pins at the owner's `main` (tag/version null); `--check` does a
byte-drift check against the sibling when one is on disk, else skips with a warning.

Re-pinning WRITES and therefore requires the owner sibling on disk. Cross-repo dest
writes are legal ONLY into ../locveil-commons (co-owned ground — HK-12 ruling); a dest
whose sibling repo is not on disk (CI) is skipped by --check, never counted never-pinned.
A family marked `check_only` (a pin another repo's re-pin flow stamps) is checked, never
written.

`[[tool]]` entries are the vendored-tools manifest: `pinned_tag` is checked against the
owner's newest family tag, and `path` + `sha256` pin the vendored bytes themselves.

Distribution: locveil-repin, single stdlib file, tags repin-vX.Y.Z, vendored per consumer
at a pinned tag (the scope-guard consumption model) + a repo-local `.repin.toml`.
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import tomllib

__version__ = "2.0.0"

LS_REMOTE_TIMEOUT = 10  # seconds; a hook must stay fast even on a flaky network
RESERVED_NAMES = {"README.md", "PIN.json"}  # the consumer's own files in a pin folder


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _git_bytes(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, check=True).stdout


def _sha256(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _version_of(family: str, tag: str) -> tuple[int, ...] | None:
    m = re.fullmatch(rf"{re.escape(family)}-v(\d+(?:\.\d+)*)", tag)
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def _newest(tags, family: str) -> str | None:
    versioned = [t for t in tags if _version_of(family, t)]
    return max(versioned, key=lambda t: _version_of(family, t)) if versioned else None


def _remote_tags(url: str) -> set[str] | None:
    try:
        out = subprocess.run(["git", "ls-remote", "--tags", url],
                             capture_output=True, text=True, check=True,
                             timeout=LS_REMOTE_TIMEOUT).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    tags = set()
    for line in out.splitlines():
        ref = line.split("\t")[-1]
        if ref.startswith("refs/tags/"):
            tags.add(ref[len("refs/tags/"):].removesuffix("^{}"))
    return tags


def _fetch_age(repo: Path) -> str:
    fetch_head = repo / ".git" / "FETCH_HEAD"
    try:
        days = (time.time() - fetch_head.stat().st_mtime) / 86400
        return f"fetched ~{days:.0f}d ago"
    except OSError:
        return "fetch age unknown"


def _newest_tag(spec: dict, root: Path, family: str) -> tuple[str | None, str]:
    """Resolve the owner's newest family tag, remote-first.

    Returns (tag_or_None, source_note); source_note is "" for a clean remote answer,
    a WARN string when degraded (stale-clone fallback / no source at all).
    """
    url = spec.get("owner_url")
    if url:
        tags = _remote_tags(url)
        if tags is not None:
            return _newest(tags, family), ""
    owner_dir = spec.get("owner_dir")
    if owner_dir:
        owner = (root / owner_dir).resolve()
        if owner.is_dir():
            try:
                tags = set(_git(owner, "tag", "-l", f"{family}-v*").splitlines())
            except subprocess.CalledProcessError:
                return None, f"WARN: {family}: sibling {owner} is not a git repo"
            note = ("" if not url else
                    f"WARN: {family}: remote unreachable — using on-disk tags of {owner} "
                    f"({_fetch_age(owner)}); a stale clone under-reports")
            return _newest(tags, family), note
    return None, f"WARN: {family}: no tag source (remote unreachable, no sibling on disk)"


def _pointer_path(value: str) -> str:
    """The file part of a `path` / `path::node` pointer."""
    return value.split("::", 1)[0].strip()


def _dest_repo_root(root: Path, dest_path: str) -> Path:
    """The repo a dest lives in: this repo, or the sibling a `../<repo>/…` path names."""
    parts = Path(dest_path).parts
    ups = 0
    while ups < len(parts) and parts[ups] == "..":
        ups += 1
    if ups == 0:
        return root
    base = root
    for _ in range(ups):
        base = base.parent
    return base / parts[ups] if ups < len(parts) else base


# ---------------------------------------------------------------- config


def load_config(path: Path) -> dict:
    cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    root = path.resolve().parent
    for fam in cfg.get("family", []):
        for key in ("name", "owner_repo"):
            if key not in fam:
                raise SystemExit(f"config error: [[family]] missing '{key}'")
        for dest in fam.get("dest", []):
            p = dest.get("path", "")
            if p.startswith(".."):
                target = (root / p).resolve()
                commons = (root / "../locveil-commons").resolve()
                if not str(target).startswith(str(commons)):
                    raise SystemExit(
                        f"config error: family '{fam['name']}' dest '{p}' writes outside "
                        "this repo — cross-repo dest writes are legal ONLY into "
                        "../locveil-commons (HK-12)")
    for tool in cfg.get("tool", []):
        for key in ("name", "family", "pinned_tag"):
            if key not in tool:
                raise SystemExit(f"config error: [[tool]] missing '{key}'")
    return cfg


def _stamp_path(spec: dict, family: str) -> str:
    return spec.get("stamp") or f"contracts/{family}/STAMP.json"


def _owner_file_set(spec: dict, owner: Path, ref: str, family: str) -> tuple[list[str], str]:
    """The owner paths to pin at `ref`, and where the list came from.

    Single source (HK-13): the owner STAMP's `artifacts` at the ref, plus the STAMP.
    Fallback: the consumer's `files` list, for refs whose STAMP does not enumerate.
    """
    stamp_rel = _stamp_path(spec, family)
    try:
        stamp = json.loads(_git_bytes(owner, "show", f"{ref}:{stamp_rel}"))
    except (subprocess.CalledProcessError, ValueError):
        stamp = None
    arts = stamp.get("artifacts") if isinstance(stamp, dict) else None
    if isinstance(arts, list):
        return [str(a) for a in arts] + [stamp_rel], "owner STAMP"
    if spec.get("files"):
        return list(spec["files"]), "config files (fallback — owner STAMP enumerates nothing)"
    raise SystemExit(
        f"error: {family}: the owner's {stamp_rel} at {ref} enumerates no `artifacts` and "
        "the config has no `files` fallback — nothing defines the pin set")


# ---------------------------------------------------------------- re-pin


def repin(cfg: dict, root: Path, family: str, tag: str | None) -> int:
    spec = next((f for f in cfg.get("family", []) if f["name"] == family), None)
    if spec is None:
        print(f"error: family '{family}' not in config")
        return 2
    if spec.get("check_only"):
        print(f"error: family '{family}' is check_only — its pin is stamped by "
              f"{spec.get('managed_by', 'another repo')}'s re-pin flow, never here")
        return 2
    owner_dir = spec.get("owner_dir")
    owner = (root / owner_dir).resolve() if owner_dir else None
    if owner is None or not owner.is_dir():
        print(f"error: re-pinning needs the owner sibling on disk ({owner_dir}) — "
              "repin writes bytes, ls-remote cannot")
        return 2

    tag = tag or _newest(set(_git(owner, "tag", "-l", f"{family}-v*").splitlines()), family)
    ref = tag or "main"
    if tag:
        version = ".".join(str(x) for x in _version_of(family, tag))
    else:
        version = None
        print(f"note: {spec['owner_repo']} has no {family}-v* tag yet — pinning at main "
              "(version/tag stay null until the owner stamps)")
    owner_commit = _git(owner, "rev-parse", f"{ref}^{{commit}}")

    paths, source = _owner_file_set(spec, owner, ref, family)
    print(f"pin set from {source}:")
    blobs: dict[str, bytes] = {}
    files: dict[str, str] = {}
    mirrored: dict[str, object] = {}
    for path in paths:
        name = path.rsplit("/", 1)[-1]
        if name in RESERVED_NAMES:
            print(f"error: {family}: the owner enumerates '{path}', but {name} is reserved "
                  "for the consumer in a pin folder (HK-13) — re-pin at an owner cut that "
                  "names its guide file")
            return 2
        if name in blobs:
            print(f"error: {family}: two owner files share the name {name} — pins are "
                  "flat (HK-13)")
            return 2
        blob = _git_bytes(owner, "show", f"{ref}:{path}")
        blobs[name] = blob
        files[name] = _sha256(blob)
        if name == "STAMP.json" and spec.get("mirror"):
            stamp = json.loads(blob)
            mirrored = {k: stamp[k] for k in spec["mirror"] if k in stamp}
        print(f"  {name}  {files[name][:12]}…  ({spec['owner_repo']} @ {ref})")

    # conformance pointers must name real tests in the repo that holds the pin
    for dest_spec in spec.get("dest", []):
        conf = dest_spec.get("conformance")
        if conf and not (_dest_repo_root(root, dest_spec["path"]) / _pointer_path(conf)).is_file():
            print(f"error: {family}: conformance '{conf}' for dest '{dest_spec['path']}' "
                  "does not resolve to a file in that repo (repo-root-relative path, "
                  "optionally path::node; omit it when no test exists yet)")
            return 2

    for dest_spec in spec.get("dest", []):
        dest = (root / dest_spec["path"]).resolve()
        dest.mkdir(parents=True, exist_ok=True)
        # files the previous pin carried and the new set no longer has
        old_pin = dest / "PIN.json"
        if old_pin.is_file():
            try:
                old_files = json.loads(old_pin.read_text(encoding="utf-8")).get("files") or {}
            except ValueError:
                old_files = {}
            for stale in sorted(set(old_files) - set(blobs)):
                if stale not in RESERVED_NAMES and (dest / stale).is_file():
                    (dest / stale).unlink()
                    print(f"  removed {stale} (no longer in the owner's set)")
        for name, blob in blobs.items():
            (dest / name).write_bytes(blob)
        pin = {"contract": family, "version": version, "tag": tag,
               "owner_repo": spec["owner_repo"], "owner_commit": owner_commit,
               "pinned_by": cfg.get("repin", {}).get("pinned_by", "repin.py"),
               "pin_date": date.today().isoformat(),
               **mirrored,
               "files": files, "conformance": dest_spec.get("conformance")}
        (dest / "PIN.json").write_text(json.dumps(pin, indent=2, ensure_ascii=False) + "\n",
                                       encoding="utf-8")
        print(f"pinned {family} @ {ref} → {dest}")
        if dest_spec.get("conformance"):
            print(f"  conformance: {dest_spec['conformance']}")
    return 0


# ---------------------------------------------------------------- re-vendor a tool


def vendor_tool(cfg: dict, config_path: Path, name: str, tag: str | None) -> int:
    root = config_path.resolve().parent
    spec = next((t for t in cfg.get("tool", []) if t["name"] == name), None)
    if spec is None:
        print(f"error: tool '{name}' not in config")
        return 2
    if not spec.get("path"):
        print(f"error: tool '{name}' has no `path` — say where the vendored file lives")
        return 2
    owner_dir = spec.get("owner_dir")
    owner = (root / owner_dir).resolve() if owner_dir else None
    if owner is None or not owner.is_dir():
        print(f"error: re-vendoring needs the owner sibling on disk ({owner_dir})")
        return 2
    family = spec["family"]
    tag = tag or _newest(set(_git(owner, "tag", "-l", f"{family}-v*").splitlines()), family)
    if not tag:
        print(f"error: {spec.get('owner_repo', owner_dir)} has no {family}-v* tag")
        return 2
    source = spec.get("source")
    if not source:
        try:
            stamp = json.loads(_git_bytes(owner, "show", f"{tag}:{_stamp_path(spec, family)}"))
            arts = stamp.get("artifacts")
        except (subprocess.CalledProcessError, ValueError):
            arts = None
        if not isinstance(arts, list) or len(arts) != 1:
            print(f"error: tool '{name}': the owner STAMP at {tag} does not enumerate "
                  "exactly one artifact — set `source` in the [[tool]] entry")
            return 2
        source = arts[0]
    blob = _git_bytes(owner, "show", f"{tag}:{source}")
    digest = _sha256(blob)
    (root / spec["path"]).write_bytes(blob)

    # rewrite pinned_tag + sha256 inside this tool's [[tool]] block, nothing else
    text = config_path.read_text(encoding="utf-8")
    blocks = re.split(r"(?m)^(?=\[\[)", text)
    done = False
    for i, block in enumerate(blocks):
        if block.startswith("[[tool]]") and re.search(
                rf'(?m)^name\s*=\s*"{re.escape(name)}"\s*$', block):
            block = re.sub(r'(?m)^pinned_tag\s*=.*$', f'pinned_tag = "{tag}"', block)
            if re.search(r"(?m)^sha256\s*=", block):
                block = re.sub(r'(?m)^sha256\s*=.*$', f'sha256 = "{digest}"', block)
            else:
                block = re.sub(r'(?m)^(pinned_tag\s*=.*)$', rf'\1\nsha256 = "{digest}"',
                               block, count=1)
            blocks[i] = block
            done = True
            break
    if not done:
        print(f"error: could not locate the [[tool]] block for '{name}' in {config_path}")
        return 2
    config_path.write_text("".join(blocks), encoding="utf-8")
    print(f"vendored {name} @ {tag} → {spec['path']}  sha256 {digest[:12]}…")
    return 0


# ---------------------------------------------------------------- check

# family classes that fail at each --fail-on level (tool version gaps fail only under any)
_MAJOR_CLASSES = ("stale-major", "never-pinned", "tool-drift")
_MINOR_CLASSES = _MAJOR_CLASSES + ("stale-minor",)
_ANY_CLASSES = _MINOR_CLASSES + ("stale-patch", "drifted", "unknown",
                                 "tool-stale-major", "tool-stale-minor", "tool-stale-patch")
_TOUCH_CLASSES = ("stale-major", "stale-minor", "stale-patch", "never-pinned", "drifted")


def _classify(family: str, pinned_tag: str | None, newest: str | None) -> str:
    if newest is None:
        return "ok"  # untagged family freshness handled separately (drift path)
    if pinned_tag is None:
        return "stale-minor"  # pinned-at-main while the owner has since tagged
    if pinned_tag == newest:
        return "ok"
    old, new = _version_of(family, pinned_tag), _version_of(family, newest)
    if old is None or new is None:
        return "stale-major"
    pad = lambda v: (tuple(v) + (0, 0, 0))[:3]  # noqa: E731 - v1 == v1.0.0
    old3, new3 = pad(old), pad(new)
    if old3[0] != new3[0]:
        return "stale-major"
    if old3[1] != new3[1]:
        return "stale-minor"
    if old3 == new3:
        return "ok"
    return "stale-patch"


def _touched_paths(root: Path, base: str) -> set[str] | None:
    try:
        out = _git(root, "diff", "--name-only", base, "HEAD")
    except (subprocess.CalledProcessError, OSError):
        return None
    return {line.strip() for line in out.splitlines() if line.strip()}


def check(cfg: dict, root: Path, fail_on: str, only: str | None,
          touched_base: str | None = None) -> int:
    findings: list[tuple[str, str, str]] = []  # (class, message, family-or-"")

    def note(cls: str, msg: str, family: str = "") -> None:
        findings.append((cls, msg, family))
        prefix = {"ok": "  ok   ", "unknown": "  WARN ", "tool-unverified": "  WARN ",
                  "skip": "  skip "}.get(cls, "  STALE")
        print(f"{prefix} {msg}")

    for spec in cfg.get("family", []):
        family = spec["name"]
        if only and family != only:
            continue
        newest, warn = _newest_tag(spec, root, family)
        if warn:
            print(f"  {warn}")
        for dest_spec in spec.get("dest", []):
            dest = (root / dest_spec["path"]).resolve()
            where = f"{family} ({dest_spec['path']})"
            if dest_spec["path"].startswith("..") and \
                    not _dest_repo_root(root, dest_spec["path"]).is_dir():
                # a cross-repo dest whose sibling is not checked out (CI): not ours to judge
                note("skip", f"{where}: sibling repo not on disk — skipped", family)
                continue
            pin_path = dest / "PIN.json"
            if not pin_path.is_file():
                note("never-pinned", f"{where}: no PIN.json — never pinned", family)
                continue
            pin = json.loads(pin_path.read_text(encoding="utf-8"))
            if newest is None and warn:
                note("unknown", f"{where}: freshness unknown — no tag source", family)
                continue
            if newest is None:
                # untagged family: byte-drift check against the sibling when present
                owner_dir = spec.get("owner_dir")
                owner = (root / owner_dir).resolve() if owner_dir else None
                if owner is None or not owner.is_dir():
                    note("unknown", f"{where}: untagged family, no sibling — skipped", family)
                    continue
                paths, _ = _owner_file_set(spec, owner, "main", family)
                drifted = [p.rsplit("/", 1)[-1] for p in paths
                           if _sha256(_git_bytes(owner, "show", f"main:{p}"))
                           != pin.get("files", {}).get(p.rsplit("/", 1)[-1])]
                if drifted:
                    note("drifted", f"{where}: owner's committed {', '.join(drifted)} no "
                                    f"longer matches the pin — run repin.py {family}", family)
                else:
                    note("ok", f"{where}: untagged, bytes match owner main", family)
                continue
            cls = _classify(family, pin.get("tag"), newest)
            if cls == "ok":
                note("ok", f"{where}: {newest}", family)
            else:
                note(cls, f"{where}: pinned {pin.get('tag') or 'untagged'}, owner's newest "
                          f"is {newest} — run repin.py {family}", family)

    for tool in cfg.get("tool", []):
        family = tool["family"]
        if only and tool["name"] != only:
            continue
        where = f"tool {tool['name']} (vendored @ {tool['pinned_tag']})"
        # the vendored bytes themselves: hermetic, no network
        if tool.get("path") and tool.get("sha256"):
            local = root / tool["path"]
            if not local.is_file() or _sha256(local.read_bytes()) != tool["sha256"]:
                note("tool-drift", f"{where}: {tool['path']} does not match its recorded "
                                   "sha256 — never edit a vendored tool; re-vendor "
                                   f"(repin.py tool {tool['name']})")
                continue
        else:
            note("tool-unverified", f"{where}: no path/sha256 recorded — the vendored "
                                    f"bytes are unverified (repin.py tool {tool['name']})")
        newest, warn = _newest_tag(tool, root, family)
        if warn:
            print(f"  {warn}")
        if newest is None:
            note("unknown", f"{where}: freshness unknown — no tag source")
            continue
        cls = _classify(family, tool["pinned_tag"], newest)
        if cls == "ok":
            note("ok", f"{where}: current")
        else:
            note("tool-" + cls, f"{where}: owner's newest is {newest} — re-vendor via a "
                                "ledger task")

    bad_classes = {"none": (), "major": _MAJOR_CLASSES, "minor": _MINOR_CLASSES,
                   "any": _ANY_CLASSES}[fail_on]
    bad = [m for cls, m, _ in findings if cls in bad_classes]

    # touch-the-family (§5 case 1): a commit that touches a family's pin or its
    # conformance test while the pin trails fails NOW, whatever the ambient severity.
    if touched_base:
        changed = _touched_paths(root, touched_base)
        if changed is None:
            print(f"  WARN: --touched: cannot diff against '{touched_base}' — "
                  "touch-the-family skipped")
        else:
            for spec in cfg.get("family", []):
                family = spec["name"]
                watch = []
                for dest_spec in spec.get("dest", []):
                    if not dest_spec["path"].startswith(".."):
                        watch.append(dest_spec["path"].rstrip("/") + "/")
                        if dest_spec.get("conformance"):
                            watch.append(_pointer_path(dest_spec["conformance"]))
                hit = sorted(c for c in changed
                             if any(c == w or (w.endswith("/") and c.startswith(w))
                                    for w in watch))
                if not hit:
                    continue
                for cls, msg, fam in findings:
                    if fam == family and cls in _TOUCH_CLASSES and msg not in bad:
                        bad.append(msg)
                        print(f"  TOUCHED {family}: this change touches {hit[0]} while "
                              "the pin trails its owner — re-pin first (§5 "
                              "touch-the-family)")

    stale = [m for cls, m, _ in findings if cls not in ("ok", "skip")]
    if bad:
        print(f"\nSTALE (fail-on={fail_on}): {len(bad)} finding(s). The fix is a "
              "deliberate re-pin ledger task, never an auto-fetch.")
        return 1
    if stale:
        print(f"\nOK under fail-on={fail_on} — {len(stale)} warning(s) above.")
        return 0
    print("\nOK: every pin and vendored tool is at its owner's newest version.")
    return 0


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("family", nargs="?",
                        help="consumed contract family to re-pin, or the word 'tool'")
    parser.add_argument("name", nargs="?", help="with 'tool': the tool to re-vendor")
    parser.add_argument("--tag", help="owner tag to pin (default: newest family tag)")
    parser.add_argument("--check", action="store_true",
                        help="staleness report; exit per --fail-on")
    parser.add_argument("--fail-on", choices=["none", "major", "minor", "any"],
                        help="severity gate (default: config default_fail_on, else 'any')")
    parser.add_argument("--touched", metavar="BASE",
                        help="with --check: touch-the-family against this git base ref")
    parser.add_argument("--family", dest="only", metavar="NAME",
                        help="with --check: restrict to one family/tool")
    parser.add_argument("--config", default=".repin.toml", type=Path,
                        help="config path (default: ./.repin.toml)")
    parser.add_argument("--version", action="version", version=f"repin {__version__}")
    args = parser.parse_args(argv)

    if not args.config.is_file():
        print(f"error: no config at {args.config}")
        return 2
    cfg = load_config(args.config)
    root = args.config.resolve().parent

    if args.check:
        if args.family or args.tag:
            parser.error("--check takes --family/--fail-on/--touched only")
        fail_on = args.fail_on or cfg.get("repin", {}).get("default_fail_on", "any")
        return check(cfg, root, fail_on, args.only, args.touched)
    if args.family == "tool":
        if not args.name:
            parser.error("give the tool to re-vendor: repin.py tool <name>")
        return vendor_tool(cfg, args.config, args.name, args.tag)
    if not args.family:
        parser.error("give a family to re-pin, 'tool <name>', or --check")
    return repin(cfg, root, args.family, args.tag)


if __name__ == "__main__":
    sys.exit(main())
