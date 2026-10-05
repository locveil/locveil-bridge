# report-protocol — consumed pin

The Locveil problem-report protocol's machine core, owned by `locveil-commons`
(`contracts/report-protocol/`, tags `report-protocol-vX.Y.Z`). This folder is the pin:
the owner's enumerated artifact byte-identical, the owner's `STAMP.json` verbatim, and
the strict `PIN.json` recording the tag and the hashes.

The bridge's filing surface (labels, title prefix, report-id/bundle shape, the target
reports repo) is locked to this pin by `backend/tests/unit/test_report_protocol_pin.py`.

Never hand-edit any of this. A new owner version means a deliberate re-pin with the
vendored tool (`python3 scripts/repin.py report-protocol`); if the protocol itself
changed, adjust the `REPORT_*` constants in `domain/reports/service.py` until the
conformance test passes, in the same change.
