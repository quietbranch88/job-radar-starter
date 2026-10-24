"""Verify the small copied operator core without reading operational data.

The versioned manifest is an intentional compatibility baseline, not a proof of
behavior or a substitute for integration tests. Updating a digest requires
reviewing both implementations and their tests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re


MODULES = ("local_state.py", "review_receipts.py", "sheet_sync.py", "sheets_transport.py")


def _core(root: Path) -> Path:
    choices = [root / "job_radar", root / "job-scan" / "operator_state"]
    present = [path for path in choices if all((path / name).is_file() for name in MODULES)]
    if len(present) != 1:
        raise ValueError("root must contain exactly one complete operator core layout")
    return present[0]


def normalized_digest(path: Path) -> str:
    source = path.read_text(encoding="utf-8")
    # Only this adapter-local import differs between the two layouts.
    if path.name in ("review_receipts.py", "sheet_sync.py"):
        source = re.sub(r"(?m)^from operator_state\.local_state import ",
                        "from job_radar.local_state import ", source)
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def check(root: Path, peer_root: Path | None = None) -> dict:
    manifest = json.loads((root / "contracts" / "operator-core.json").read_text(encoding="utf-8"))
    if (set(manifest) != {"contract_version", "normalization", "modules"}
            or manifest["contract_version"] != 1
            or manifest["normalization"] != "lf-and-local-state-import-v1"
            or not isinstance(manifest["modules"], dict)
            or set(manifest["modules"]) != set(MODULES)
            or any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
                   for value in manifest["modules"].values())):
        raise ValueError("unsupported operator core manifest")
    local, peer = _core(root), _core(peer_root) if peer_root is not None else None
    results = {}
    if peer_root is not None:
        peer_manifest = json.loads((peer_root / "contracts" / "operator-core.json").read_text(encoding="utf-8"))
        if peer_manifest != manifest:
            raise ValueError("operator core manifests differ; reconcile both intentionally")
    for name in MODULES:
        actual = normalized_digest(local / name)
        peer_digest = normalized_digest(peer / name) if peer is not None else None
        results[name] = {
            "matches_contract": actual == manifest["modules"][name],
            "matches_peer": peer_digest == actual if peer is not None else None,
        }
    return {
        "ok": all(item["matches_contract"] and item["matches_peer"] is not False
                  for item in results.values()),
        "peer_checked": peer is not None,
        "modules": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--peer-root", type=Path)
    args = parser.parse_args()
    try:
        result = check(args.root, args.peer_root)
    except (OSError, ValueError):
        print(json.dumps({"ok": False, "error": "operator core verification failed"}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

