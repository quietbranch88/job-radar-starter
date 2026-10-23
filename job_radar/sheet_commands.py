"""Explicit, minimal job/application projection into an opt-in Sheets outbox."""

from __future__ import annotations

from typing import Any, Protocol

from job_radar.domain.tracking import stable_job_id
from job_radar.sheet_sync import SheetOutbox


class ApplicationReader(Protocol):
    def read(self, kind: str) -> dict[str, Any]: ...


def enqueue_snapshot(outbox: SheetOutbox, repository: ApplicationReader, *,
                     jobs: list[dict[str, Any]], job_id: str, target: str,
                     from_application: bool = False,
                     expected_remote_hash: str | None = None) -> str:
    matches = [job for job in jobs if (job.get('stable_id') or stable_job_id(
        source=job['source'], external_id=job['external_id'], company=job['company'],
        title=job['title'])) == job_id]
    if len(matches) != 1:
        raise ValueError('sync requires one unambiguous job snapshot')
    job = matches[0]
    fields = {key: job[key] for key in ('company', 'title', 'url')}
    fields['status'] = 'interested'
    if from_application:
        document = repository.read('applications')
        if (not isinstance(document, dict) or document.get('contract_version') != 1
                or not isinstance(document.get('items'), list)):
            raise ValueError('sync requires valid local application state')
        applications = [item for item in document['items'] if isinstance(item, dict) and item.get('job_id') == job_id]
        if len(applications) != 1 or not applications[0].get('confirmed_at'):
            raise ValueError('sync requires one explicitly confirmed local application')
        application = applications[0]
        required = ('status', 'applied_at', 'resume_version', 'confirmation_evidence')
        if not all(application.get(key) for key in required):
            raise ValueError('sync requires submission time, resume version and confirmation evidence')
        fields.update({key: application[key] for key in required})
    return outbox.enqueue(job_id, fields, target, expected_remote_hash)
