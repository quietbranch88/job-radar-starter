"""Optional company-board diagnostics, separate from the public scan contract."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import time

from job_radar.local_state import write_state, writer_guard


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SourceHealth:
    def __init__(self, path: Path, companies, *, checkpoint=None):
        self.path = path
        self.guard = writer_guard(path.with_name(path.name + '.guard'))
        self.guard.__enter__()
        self.started = {}
        self.document = {
            'contract_version': 1, 'scope': 'configured_company_boards',
            'state': 'running', 'started_at': _now(), 'finished_at': None,
            'entries': [dict(company=company, source=source, board=board, country=country,
                             state='pending', jobs=None, attempted_at=None, fetched_at=None,
                             elapsed_seconds=None, failure_category=None, http_status=None)
                        for company, source, board, country in companies],
        }
        self.entries = {tuple(scope): entry for scope, entry in zip(companies, self.document['entries'])}
        if checkpoint is not None:
            for completed in checkpoint.document['companies']:
                if completed['state'] == 'complete':
                    self.entries[tuple(completed['scope'])].update(
                        state='cached', jobs=len(completed['jobs']), fetched_at=completed['fetched_at'],
                    )
        try:
            self._save()
        except BaseException:
            self.guard.__exit__(None, None, None)
            raise

    def begin(self, scope) -> None:
        self.started[scope] = time.monotonic()
        self.entries[scope].update(state='running', attempted_at=_now())
        self._save()

    def complete(self, scope, count: int, *, fetched_at: str | None = None) -> None:
        self.entries[scope].update(state='complete', jobs=count, fetched_at=fetched_at or _now(),
                                   elapsed_seconds=self._elapsed(scope))
        self._save()

    def fail(self, scope, category: str, *, http_status: int | None = None) -> None:
        self.entries[scope].update(state='failed', jobs=None, failure_category=category,
                                   http_status=http_status, elapsed_seconds=self._elapsed(scope))
        self._save()

    def finish(self, *, interrupted: bool = False) -> None:
        entries = self.document['entries']
        complete = sum(entry['state'] in ('complete', 'cached') for entry in entries)
        failed = any(entry['state'] == 'failed' for entry in entries)
        self.document['state'] = (
            'interrupted' if interrupted else 'partial' if failed and complete
            else 'failed' if failed else 'complete'
        )
        if interrupted:
            for scope, entry in self.entries.items():
                if entry['state'] == 'running':
                    entry.update(state='interrupted', elapsed_seconds=self._elapsed(scope))
        self.document['finished_at'] = _now()
        try:
            self._save()
        finally:
            self.guard.__exit__(None, None, None)

    def _elapsed(self, scope) -> float:
        return round(max(0.0, time.monotonic() - self.started[scope]), 6)

    def _save(self) -> None:
        write_state(self.path, self.document)
