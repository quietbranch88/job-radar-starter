"""Per-attempt local stage evidence; not a deployment or review approval."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from job_radar.local_state import write_state, writer_guard


class RunReceipt:
    def __init__(self, output: Path, *, path: Path | None = None):
        run_id = uuid4().hex
        self.path = (path or output.parent / 'run-manifests' / f'{run_id}.json').resolve()
        self.document: dict[str, Any] = {
            'contract_version': 1, 'run_id': run_id, 'state': 'running',
            'started_at': datetime.now(timezone.utc).isoformat(), 'finished_at': None,
            'stages': dict(scan='pending', workflow='pending', review='pending', build='pending',
                           sync='not_configured', deployment='not_requested'),
            'scan_progress': None, 'error_type': None,
        }
        with writer_guard(self.path.with_name(self.path.name + '.guard')):
            if self.path.exists():
                raise ValueError('run receipt already exists; choose a new path')
            self._save()

    def stage(self, name: str, state: str) -> None:
        if name not in ('scan', 'workflow', 'review', 'build') or state not in (
            'running', 'complete', 'partial', 'failed', 'not_configured', 'interrupted',
        ):
            raise ValueError('unsupported run stage transition')
        self.document['stages'][name] = state
        self._save()

    def scan_progress(self, progress: dict[str, Any]) -> None:
        self.document['scan_progress'] = progress
        self._save()

    def finish(self, error: BaseException | None = None) -> None:
        self.document['state'] = 'finished' if error is None else 'failed'
        if error is not None:
            self.document['error_type'] = type(error).__name__
            for stage, state in self.document['stages'].items():
                if state == 'running':
                    self.document['stages'][stage] = 'interrupted' if isinstance(error, (KeyboardInterrupt, SystemExit)) else 'failed'
        self.document['finished_at'] = datetime.now(timezone.utc).isoformat()
        self._save()

    def _save(self) -> None:
        write_state(self.path, self.document)
