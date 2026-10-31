from __future__ import annotations

import json
from pathlib import Path

import pytest

from job_radar.data.tracking_store import LocalTrackingStore


def test_sheet_outbox_and_review_receipts_are_not_managed_state(tmp_path: Path):
    store = LocalTrackingStore(tmp_path / "user-data")
    paths = store.initialize()
    assert set(paths) == {
        "applications",
        "events",
        "lifecycle",
        "rejected",
        "review_cache",
        "company_facts",
    }
    for kind in ("sheet_outbox", "review_receipts"):
        with pytest.raises(ValueError, match="unsupported state kind"):
            store.path_for(kind)
    output = tmp_path / "export.json"
    store.export_to(output)
    exported = json.loads(output.read_text(encoding="utf8"))
    assert set(exported["state"]) == {"applications", "events", "lifecycle", "rejected"}
    removed = store.delete_tracking_data()
    assert "sheet_outbox" not in removed and "review_receipts" not in removed
    assert set(removed) == set(paths)
