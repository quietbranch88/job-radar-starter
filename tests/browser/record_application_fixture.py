"""Exercise the local CLI bridge using only the synthetic browser fixture."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import mkdtemp

from build_dashboard_fixture import MODEL
from job_radar.cli import main
from job_radar.data.tracking_store import LocalTrackingStore


if __name__ == "__main__":
    handoff, output = map(Path, sys.argv[1:])
    store = LocalTrackingStore(Path(mkdtemp(prefix="confirmed-application-", dir=output.parent)).resolve())
    store.initialize()
    store.write("lifecycle", {"contract_version": 1, "items": MODEL["jobs"]})
    assert main([
        "tracking", "record-application", "--user-data", str(store.root),
        "--from-dashboard", str(handoff), "--job-id", "job_active",
        "--applied-at", "unknown", "--resume-version", "example-v2",
        "--channel", "official-site", "--confirmation-evidence", "Synthetic local receipt",
        "--confirmed-at", "2026-10-23T00:05:00Z", "--confirm-submitted",
    ]) == 0
    applications = store.read("applications")["items"]
    assert len(applications) == 1
    assert applications[0]["applied_at"] == "unknown"
    lifecycle = output.with_suffix(".jobs.json")
    lifecycle.write_text(json.dumps({
        "contract_version": 1, "jobs": MODEL["jobs"], "statuses": {},
    }), encoding="utf-8")
    assert main([
        "build-dashboard", "--jobs", str(lifecycle), "--output", str(output),
        "--user-data", str(store.root),
    ]) == 0
