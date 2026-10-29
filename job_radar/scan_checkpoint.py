"""Versioned continuation for complete company-board reads, not page cursors."""

from __future__ import annotations

from dataclasses import asdict, fields, replace
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

from job_radar.domain.jobs import Job
from job_radar.local_state import read_state, write_state


CompanyScope = tuple[str, str, str, str]


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError('checkpoint timestamp must be a string')
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('checkpoint timestamp must include a timezone')
    return result


def _job(raw: Any, company: CompanyScope) -> Job:
    if not isinstance(raw, dict) or set(raw) - {field.name for field in fields(Job)}:
        raise ValueError('checkpoint job has unsupported fields')
    if raw.get('company') != company[0] or raw.get('source') != company[1]:
        raise ValueError('checkpoint job does not match its company scope')
    item = dict(raw)
    for name in ('source', 'external_id', 'company', 'title', 'location', 'url', 'published_at',
                 'country', 'category', 'summary', 'risk', 'salary', 'first_seen', 'last_seen',
                 'jd_evidence_source', 'jd_hash'):
        if name in item and not isinstance(item[name], str):
            raise ValueError('checkpoint job text must be a string')
    for name in ('tracks', 'skills', 'jd_evidence'):
        value = item.get(name, [])
        if not isinstance(value, (list, tuple)) or not all(isinstance(term, str) for term in value):
            raise ValueError('checkpoint job lists must contain strings')
        item[name] = tuple(value)
    try:
        return Job(**item)
    except TypeError:
        raise ValueError('checkpoint job is incomplete') from None


class ScanCheckpoint:
    def __init__(self, path: Path, companies: Sequence[CompanyScope], *, mode: str,
                 max_jobs: int, observed_on: str, resume: bool):
        self.path = path
        self.companies = list(companies)
        self.max_jobs = max_jobs
        plan_hash = _digest({'adapter_contract': 1, 'companies': self.companies,
                             'mode': mode, 'max_jobs_per_source': max_jobs})
        if resume:
            if not path.exists():
                raise ValueError('resume checkpoint does not exist')
            self.document = read_state(path)
            self._validate(plan_hash)
        else:
            if path.exists():
                raise ValueError('checkpoint already exists; use --resume or a new path')
            if date.fromisoformat(observed_on).isoformat() != observed_on:
                raise ValueError('checkpoint observation date must be an ISO date')
            now = _now()
            self.document = {'contract_version': 2, 'plan_hash': plan_hash,
                             'observed_on': observed_on, 'started_at': now, 'updated_at': now,
                             'companies': [dict(scope=list(scope), state='pending', jobs=[],
                                                jobs_hash=None, fetched_at=None, failure=None)
                                           for scope in self.companies]}
            self._save()

    @property
    def observed_on(self) -> str:
        return self.document['observed_on']

    @property
    def latest_observed_on(self) -> str:
        """Include successful empty boards without refreshing cached observations."""
        return max(
            [self.observed_on]
            + [_timestamp(entry['fetched_at']).astimezone(timezone.utc).date().isoformat()
               for entry in self.document['companies'] if entry['state'] == 'complete']
        )

    def _validate(self, plan_hash: str) -> None:
        doc = self.document
        if (set(doc) != {'contract_version', 'plan_hash', 'observed_on', 'started_at', 'updated_at', 'companies'}
                or type(doc['contract_version']) is not int or doc['contract_version'] != 2):
            raise ValueError('checkpoint has unsupported contract version or fields')
        if doc['plan_hash'] != plan_hash:
            raise ValueError('checkpoint plan does not match the configured company scope')
        if not isinstance(doc['observed_on'], str) or date.fromisoformat(doc['observed_on']).isoformat() != doc['observed_on']:
            raise ValueError('checkpoint observation date is invalid')
        started, updated = _timestamp(doc['started_at']), _timestamp(doc['updated_at'])
        if started > updated:
            raise ValueError('checkpoint time range is invalid')
        entries = doc['companies']
        if not isinstance(entries, list) or len(entries) != len(self.companies):
            raise ValueError('checkpoint company scope is incomplete')
        for company, entry in zip(self.companies, entries):
            if (not isinstance(entry, dict)
                    or set(entry) != {'scope', 'state', 'jobs', 'jobs_hash', 'fetched_at', 'failure'}
                    or entry['scope'] != list(company)):
                raise ValueError('checkpoint company plan is invalid')
            jobs = entry['jobs']
            if not isinstance(jobs, list) or len(jobs) > self.max_jobs:
                raise ValueError('checkpoint company result exceeds its job limit')
            if entry['state'] == 'complete':
                if entry['failure'] is not None or entry['jobs_hash'] != _digest(jobs):
                    raise ValueError('checkpoint completion digest is invalid')
                fetched = _timestamp(entry['fetched_at'])
                if not started <= fetched <= updated:
                    raise ValueError('checkpoint completion timestamp is invalid')
                for raw in jobs:
                    job = _job(raw, company)
                    if job.last_seen != fetched.astimezone(timezone.utc).date().isoformat():
                        raise ValueError('checkpoint job observation date does not match its fetch timestamp')
            elif entry['state'] in ('pending', 'failed'):
                if jobs or entry['jobs_hash'] is not None or entry['fetched_at'] is not None:
                    raise ValueError('checkpoint unfinished result cannot prove completion')
                failure = entry['failure']
                if entry['state'] == 'pending' and failure is not None:
                    raise ValueError('checkpoint pending entry cannot have a failure')
                if entry['state'] == 'failed':
                    if (not isinstance(failure, dict) or set(failure) != {'source', 'company', 'category', 'message'}
                            or not all(isinstance(value, str) for value in failure.values())
                            or failure['source'] != company[1] or failure['company'] != company[0]):
                        raise ValueError('checkpoint failure scope is invalid')
            else:
                raise ValueError('checkpoint company state is invalid')

    def pending(self) -> list[CompanyScope]:
        return [scope for scope, entry in zip(self.companies, self.document['companies'])
                if entry['state'] != 'complete']

    def retained_jobs(self) -> list[Job]:
        return [_job(raw, scope) for scope, entry in zip(self.companies, self.document['companies'])
                if entry['state'] == 'complete' for raw in entry['jobs']]

    def complete(self, company: CompanyScope, jobs: list[Job]) -> list[Job]:
        fetched_at = _now()
        seen = _timestamp(fetched_at).astimezone(timezone.utc).date().isoformat()
        observed_jobs = [replace(job, last_seen=seen) for job in jobs]
        raw = json.loads(json.dumps([asdict(job) for job in observed_jobs], ensure_ascii=False))
        entry = self.document['companies'][self.companies.index(company)]
        entry.update(state='complete', jobs=raw, jobs_hash=_digest(raw), fetched_at=fetched_at, failure=None)
        self._save()
        return observed_jobs

    def fail(self, company: CompanyScope, failure: dict[str, str]) -> None:
        entry = self.document['companies'][self.companies.index(company)]
        entry.update(state='failed', jobs=[], jobs_hash=None, fetched_at=None, failure=failure)
        self._save()

    def _save(self) -> None:
        self.document['updated_at'] = _now()
        write_state(self.path, self.document)

    def progress(self) -> dict[str, Any]:
        completed = sum(entry['state'] == 'complete' for entry in self.document['companies'])
        return {'scope': 'configured_companies', 'total': len(self.companies),
                'completed': completed, 'remaining': len(self.companies) - completed,
                'observed_on': self.observed_on, 'started_at': self.document['started_at'],
                'updated_at': self.document['updated_at'], 'plan_hash': self.document['plan_hash']}
