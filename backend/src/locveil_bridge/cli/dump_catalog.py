"""locveil-catalog — dump the golden catalog contract artifact (VWB-15).

Builds the catalog OFFLINE — typed device configs + capability maps + rooms +
scenario definitions straight from ``config/``, no drivers instantiated, no network,
no broker — so the dump is deterministic and CI-runnable. This is the same projection
`GET /system/catalog` serves at runtime (one code path: ``build_catalog``); devices
are sorted by id so the emitted JSON and its content-hash are stable across runs.

The committed artifact set (``contracts/catalog/``) is the Irene↔bridge contract
of record: the voice side pins its own copy (one-way, outward — the bridge never
writes into a sibling repo). ``--stamp`` additionally records the STAMP core
(contract/version/tag/date/owner_repo, from ``CONTRACT_VERSION``), the ``artifacts``
list (``STAMP_ARTIFACTS`` — the pinned set) plus which bridge
build generated the artifacts (the content-hash tracks config drift; the commit
stamp tracks the code build; neither substitutes for the other).
"""

import argparse
import asyncio
import datetime
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from locveil_bridge.__version__ import __version__
from locveil_bridge.domain.ports import StateRepositoryPort
from locveil_bridge.domain.rooms.service import RoomManager
from locveil_bridge.domain.scenarios.proxy import ScenarioProxy
from locveil_bridge.domain.scenarios.service import ScenarioManager
from locveil_bridge.domain.topology.loader import load_topology
from locveil_bridge.infrastructure.capabilities.loader import attach_capability_maps
from locveil_bridge.infrastructure.config.manager import ConfigManager
from locveil_bridge.presentation.api.catalog import CONTRACT_VERSION, build_catalog


# The pinned set: what a consumer's pin copies and what the contract checks byte-lock
# against the tag (STAMP.json itself always travels implicitly). Repo-root-relative —
# contract-guard resolves each entry via `git show <tag>:<path>` from the repo root.
# Never a file named README.md / PIN.json / STAMP.json (inside a pin folder those names
# are reserved), never two entries with the same file name (pins are flat): the
# normative prose lives in the named guide, and contracts/catalog/README.md stays an
# unlocked owner-side index outside this list.
STAMP_ARTIFACTS = (
    "contracts/catalog/catalog.golden.json",
    "contracts/catalog/openapi.json",
    "contracts/catalog/catalog-contract.md",
)


class _NullStore(StateRepositoryPort):
    """StateRepositoryPort no-op implementation — the offline world restores nothing."""

    async def load(self, entity_id):
        return None

    async def save(self, entity_id, state):
        return None

    async def bulk_save(self, states):
        return None

    async def delete(self, entity_id):
        return None

    async def list_entities(self):
        return []

    async def initialize(self):
        return None

    async def close(self):
        return None


def _standin(config):
    """A device stand-in carrying exactly what the catalog projection reads:
    ``.config``, ``.capabilities`` (attached below), ``get_room()``. No driver,
    no network."""
    d = SimpleNamespace(config=config, capabilities=None, room=getattr(config, "room", None))
    d.get_room = lambda _d=d: _d.room
    return d


def build_offline_catalog(config_dir: str = "config"):
    """The offline world: typed configs -> stand-ins -> capability maps -> rooms ->
    scenario proxy -> ``build_catalog``. Deterministic (devices sorted by id)."""
    config_manager = ConfigManager(config_dir=config_dir)
    typed = config_manager.get_all_typed_configs()

    devices = {device_id: _standin(cfg) for device_id, cfg in sorted(typed.items())}
    attach_capability_maps(devices, Path(config_dir) / "capabilities")

    device_manager = SimpleNamespace(
        devices=devices,
        get_device=lambda device_id: devices.get(device_id),
    )

    room_manager = RoomManager(Path(config_dir), device_manager)  # type: ignore[arg-type]
    room_manager.reload()

    scenario_manager = ScenarioManager(
        device_manager=device_manager,  # type: ignore[arg-type]
        room_manager=room_manager,
        state_repository=_NullStore(),
        scenario_dir=Path(config_dir) / "scenarios",
    )
    asyncio.run(scenario_manager.load_scenarios())
    # VWB-46: the scenario values' `max_duration_ms` is derived by the planners over the
    # signal topology (Layer 0) — the runtime loads it in `initialize()`, which the
    # offline world skips (no state restore, no validation side effects), so load it here
    # the same way. Without it every scenario resolves to an empty path.
    scenario_manager.topology = load_topology(Path(config_dir) / "topology.json")
    proxy = ScenarioProxy(scenario_manager, device_manager)

    return build_catalog(device_manager, room_manager, proxy)


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dump the golden catalog contract artifact (offline, deterministic)."
    )
    parser.add_argument("-o", "--output", default="contracts/catalog/catalog.golden.json",
                        help="Golden catalog output path (default: contracts/catalog/catalog.golden.json). "
                             "Run from the repo root — device cert paths in config/ resolve relative to it.")
    parser.add_argument("--config-dir", default="config",
                        help="Config directory to project (default: config, relative to the repo root)")
    parser.add_argument("--stamp", default=None,
                        help="Also write the STAMP.json (contract core + bridge build + catalog hash)")
    parser.add_argument("--stdout", action="store_true", help="Print the catalog instead of writing")
    args = parser.parse_args()

    catalog = build_offline_catalog(args.config_dir)
    payload = json.dumps(catalog.model_dump(), indent=2, ensure_ascii=False) + "\n"

    if args.stdout:
        sys.stdout.write(payload)
        return 0

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(payload, encoding="utf-8")
    print(f"Wrote {out} ({len(catalog.devices)} devices, {len(catalog.rooms)} rooms, "
          f"version {catalog.version})")

    if args.stamp:
        stamp = {
            "contract": "catalog",
            "version": CONTRACT_VERSION,
            "tag": f"catalog-v{CONTRACT_VERSION}",
            "date": datetime.date.today().isoformat(),
            "owner_repo": "locveil-bridge",
            "artifacts": list(STAMP_ARTIFACTS),
            "bridge_commit": _git_commit(),
            "bridge_version": __version__,
            "catalog_version": catalog.version,
        }
        Path(args.stamp).write_text(
            json.dumps(stamp, indent=2) + "\n", encoding="utf-8"
        )
        print(f"Wrote {args.stamp} ({stamp['bridge_commit'][:9]}, v{stamp['bridge_version']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
