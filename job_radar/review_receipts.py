"""Local operator attestations bound to supplied job, JD and resume identities.

Receipts are not signatures or proof of an independent human review. This module
never reads resumes, dereferences evidence, calls a provider or infers approval.
History is retained until the caller explicitly deletes the managed state file.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import stat
from typing import Any
from urllib.parse import urlsplit

from job_radar.local_state import read_state, write_state, writer_guard


MAX_RECEIPTS = 100_000
_FIELDS = {
    'job_id', 'jd_sha256', 'resume_sha256', 'reviewer', 'evidence', 'decision',
    'reviewed_at',
}
_SHA256 = re.compile(r'[0-9a-f]{64}')
_JOB_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]*')
_REVIEWER = re.compile(r"[\w .@+'()-]+")
_REFERENCE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/#@+-]*')
_TIMESTAMP = re.compile(
    r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}'
    r'(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})'
)


def _text(value: Any, field: str, limit: int, pattern: re.Pattern[str]) -> str:
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > limit or pattern.fullmatch(value) is None):
        raise ValueError(f'{field} must be a bounded identity or evidence reference')
    if len(value.encode('utf-8')) > limit:
        raise ValueError(f'{field} exceeds its byte limit')
    return value


def _hash(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f'{field} must be a lowercase SHA256 digest')
    return value


def _timestamp(value: Any, field: str) -> datetime:
    if (not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None
            or value.endswith('-00:00')
            or (not value.endswith('Z') and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59))):
        raise ValueError(f'{field} must be an ISO timestamp with an explicit timezone')
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ValueError(f'{field} must be a valid timezone-aware timestamp') from None


def _version(job_id: Any, jd_sha256: Any, resume_sha256: Any) -> dict[str, str]:
    return {
        'job_id': _text(job_id, 'job_id', 128, _JOB_ID),
        'jd_sha256': _hash(jd_sha256, 'jd_sha256'),
        'resume_sha256': _hash(resume_sha256, 'resume_sha256'),
    }


def _content(job_id: Any, jd_sha256: Any, resume_sha256: Any, reviewer: Any,
             evidence: Any, decision: Any, reviewed_at: Any) -> dict[str, str]:
    content = _version(job_id, jd_sha256, resume_sha256)
    content['reviewer'] = _text(reviewer, 'reviewer', 200, _REVIEWER)
    content['evidence'] = _text(evidence, 'evidence', 1024, _REFERENCE)
    if '://' in evidence:
        try:
            reference = urlsplit(evidence)
            if (reference.scheme != 'https' or not reference.hostname
                    or reference.username is not None or reference.password is not None
                    or reference.query):
                raise ValueError('invalid reference')
        except ValueError:
            raise ValueError('evidence must be a reference without credentials or query data') from None
    if not isinstance(decision, str) or decision not in ('approve', 'reject'):
        raise ValueError('decision must be approve or reject')
    content['decision'] = decision
    content['reviewed_at'] = _timestamp(reviewed_at, 'reviewed_at').isoformat(timespec='microseconds')
    return content


def _receipt_id(content: dict[str, str]) -> str:
    raw = json.dumps({'contract_version': 1, **content}, ensure_ascii=False,
                     sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


class ReviewReceiptStore:
    """Append-only history at an explicit local path, with one OS-guarded writer.

    ``asof`` is an optional caller-supplied timezone-aware cutoff. Without it, all
    attested times participate; no current time is guessed. The caller owns the
    managed file's export/deletion policy and must supply hashes of the intended
    artifacts. A content ID detects inconsistent state, not a forged attestation.
    """

    def __init__(self, path: Path):
        if (not isinstance(path, Path) or not path.is_absolute()
                or '..' in path.parts or path.suffix.lower() != '.json'
                or len(str(path)) > 4096 or path.drive.startswith('\\\\')
                or any(':' in part for part in path.parts[1:])):
            raise ValueError('review receipt path must be an absolute local JSON file without traversal')
        self.path = path
        self._validate_path()

    def _validate_path(self) -> None:
        guard = self.path.with_name(self.path.name + '.guard')
        try:
            for candidate in (self.path, guard, *self.path.parents):
                try:
                    info = candidate.lstat()
                except FileNotFoundError:
                    continue
                if (stat.S_ISLNK(info.st_mode)
                        or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                    raise ValueError('review receipt path cannot contain links or junctions')
                if candidate in (self.path, guard):
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise ValueError('review receipt state and guard must be unaliased regular files')
                elif not stat.S_ISDIR(info.st_mode):
                    raise ValueError('review receipt parent must be a directory')
        except OSError:
            raise ValueError('review receipt path is unavailable') from None

    def _load(self) -> dict[str, Any]:
        try:
            document = read_state(self.path)
        except FileNotFoundError:
            return {'contract_version': 1, 'items': []}
        except (OSError, ValueError, RecursionError):
            raise ValueError('review receipt state is invalid or unreadable') from None
        try:
            if (set(document) != {'contract_version', 'items'}
                    or type(document['contract_version']) is not int
                    or document['contract_version'] != 1
                    or not isinstance(document['items'], list)
                    or len(document['items']) > MAX_RECEIPTS):
                raise ValueError('invalid document')
            seen: set[str] = set()
            for item in document['items']:
                if not isinstance(item, dict) or set(item) != _FIELDS | {'receipt_id'}:
                    raise ValueError('invalid receipt fields')
                content = _content(**{field: item[field] for field in _FIELDS})
                identifier = _receipt_id(content)
                if item != {**content, 'receipt_id': identifier} or identifier in seen:
                    raise ValueError('invalid or duplicate receipt identity')
                seen.add(identifier)
        except (ValueError, TypeError, OverflowError):
            raise ValueError('review receipt state is invalid; recover the original file') from None
        return document

    def record(self, job_id: str, jd_sha256: str, resume_sha256: str, reviewer: str,
               evidence: str, decision: str, reviewed_at: str, *, confirmed: bool,
               asof: str | None = None) -> dict[str, str]:
        """Record only an explicitly confirmed operator attestation, never provider state."""
        if confirmed is not True:
            raise ValueError('explicit operator confirmation is required')
        content = _content(job_id, jd_sha256, resume_sha256, reviewer, evidence, decision, reviewed_at)
        if asof is not None and _timestamp(content['reviewed_at'], 'reviewed_at') > _timestamp(asof, 'asof'):
            raise ValueError('reviewed_at cannot be later than explicit asof')
        receipt = {**content, 'receipt_id': _receipt_id(content)}
        try:
            self._validate_path()
            with writer_guard(self.path.with_name(self.path.name + '.guard')):
                self._validate_path()
                document = self._load()
                for previous in document['items']:
                    if previous['receipt_id'] == receipt['receipt_id']:
                        return dict(previous)
                if len(document['items']) >= MAX_RECEIPTS:
                    raise ValueError('review receipt history limit reached')
                document['items'].append(receipt)
                write_state(self.path, document)
        except (OSError, ValueError):
            raise ValueError('review receipt state is invalid, unavailable or in use') from None
        return dict(receipt)

    def check(self, job_id: str, jd_sha256: str, resume_sha256: str, *,
              asof: str | None = None) -> dict[str, Any]:
        """Return the newest applicable decision, resolving equal-time conflicts to reject."""
        version = _version(job_id, jd_sha256, resume_sha256)
        cutoff = None if asof is None else _timestamp(asof, 'asof')
        self._validate_path()
        items = self._load()['items']
        same_job = [item for item in items if item['job_id'] == job_id]
        matching = [item for item in same_job if all(item[field] == value for field, value in version.items())]
        eligible = [item for item in matching if cutoff is None
                    or _timestamp(item['reviewed_at'], 'reviewed_at') <= cutoff]
        result: dict[str, Any] = {
            'applicable': False, 'approved': False, 'decision': None,
            'reason': 'no_receipt', 'receipt': None,
        }
        if not same_job:
            return result
        if not matching:
            result['reason'] = 'version_mismatch'
            return result
        if not eligible:
            result['reason'] = 'not_yet_reviewed'
            return result
        latest = max(eligible, key=lambda item: (
            _timestamp(item['reviewed_at'], 'reviewed_at'), item['decision'] == 'reject', item['receipt_id'],
        ))
        result.update(applicable=True, approved=latest['decision'] == 'approve',
                      decision=latest['decision'], reason='matched', receipt=dict(latest))
        return result
