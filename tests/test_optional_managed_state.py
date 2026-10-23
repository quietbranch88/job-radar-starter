from __future__ import annotations

import json
from pathlib import Path

import pytest

from job_radar.data.tracking_store import LocalTrackingStore
from job_radar.local_state import writer_guard
from job_radar.sheet_sync import SheetOutbox


def test_optional_receipts_and_outbox_are_not_created_by_default(tmp_path: Path):
    store = LocalTrackingStore(tmp_path / 'user-data')
    paths = store.initialize()
    assert len(paths) == 6
    assert not store.path_for('sheet_outbox').exists()
    assert not store.path_for('review_receipts').exists()


def test_explicit_export_and_delete_cover_new_managed_state(tmp_path: Path):
    store = LocalTrackingStore(tmp_path / 'user-data')
    store.initialize()
    for kind in ('sheet_outbox', 'review_receipts'):
        store.write(kind, {'contract_version': 1, 'items': [{'id': kind}]})
    output = tmp_path / 'export.json'
    store.export_to(output)
    exported = json.loads(output.read_text(encoding='utf8'))
    assert exported['state']['sheet_outbox']['items'] == [{'id': 'sheet_outbox'}]
    assert exported['state']['review_receipts']['items'] == [{'id': 'review_receipts'}]
    removed = store.delete_tracking_data()
    assert 'sheet_outbox' in removed and 'review_receipts' in removed
    assert all(not path.exists() for path in removed.values())
    assert output.exists()


@pytest.mark.parametrize('kind', ['sheet_outbox', 'review_receipts'])
def test_delete_refuses_active_optional_writer_before_removing_any_state(tmp_path: Path, kind):
    store = LocalTrackingStore(tmp_path / 'user-data')
    store.initialize()
    path = store.path_for(kind)
    guard = SheetOutbox(path).guard_path if kind == 'sheet_outbox' else path.with_name(path.name + '.guard')
    with writer_guard(guard):
        with pytest.raises(ValueError, match='writer'):
            store.delete_tracking_data()
    assert store.path_for('applications').exists()
