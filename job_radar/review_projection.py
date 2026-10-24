"""Read-only operator decisions, separate from provider assessment."""

from collections.abc import Mapping, Sequence
from datetime import datetime
import re
from typing import Any, Protocol


class ReceiptChecker(Protocol):
    def check(self, job_id: str, jd_sha256: str, resume_sha256: str, *,
              asof: str | None = None) -> dict[str, Any]: ...


def validate_resume_selection(resume_sha256: str | None) -> None:
    if resume_sha256 is not None and (
        not isinstance(resume_sha256, str)
        or re.fullmatch(r'[0-9a-f]{64}', resume_sha256) is None
    ):
        raise ValueError('selected resume must be a lowercase SHA256 digest')


def project_review_receipts(jobs: Sequence[Mapping[str, Any]], *,
                            checker: ReceiptChecker | None = None,
                            resume_sha256: str | None = None,
                            asof: str | None = None) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Project one selected resume without opening it or inferring a submission.

    The caller supplies current time separately from the scan observation date.
    Private reviewer/evidence references remain in local receipt storage.
    """
    validate_resume_selection(resume_sha256)
    if resume_sha256 is not None:
        if checker is None or asof is None:
            raise ValueError('selected resume review requires a receipt checker and explicit time')
        try:
            stamp = datetime.fromisoformat(asof)
        except (TypeError, ValueError):
            raise ValueError('review projection time must have an explicit timezone') from None
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError('review projection time must have an explicit timezone')
    counts = {'approved': 0, 'rejected': 0, 'pending': 0}
    projected = []
    for job in jobs:
        status = {'applicable': False, 'approved': False, 'decision': None,
                  'reason': 'resume_not_selected', 'receipt_id': None}
        if resume_sha256 is not None:
            if not job.get('jd_hash'):
                status['reason'] = 'jd_unavailable'
            else:
                result = checker.check(job['stable_id'], job['jd_hash'], resume_sha256, asof=asof)
                applicable, approved = result['applicable'], result['approved']
                decision, reason, receipt = result['decision'], result['reason'], result['receipt']
                if (type(applicable) is not bool or type(approved) is not bool
                        or approved != (applicable and decision == 'approve')
                        or applicable != (decision in ('approve', 'reject'))
                        or reason not in ('matched', 'no_receipt', 'version_mismatch', 'not_yet_reviewed')
                        or (reason == 'matched') != applicable):
                    raise ValueError('receipt checker returned an invalid decision')
                receipt_id = receipt['receipt_id'] if applicable else None
                if applicable and (not isinstance(receipt_id, str)
                                   or re.fullmatch(r'[0-9a-f]{64}', receipt_id) is None):
                    raise ValueError('receipt checker returned an invalid receipt identity')
                status.update(applicable=applicable, approved=approved, decision=decision,
                              reason=reason, receipt_id=receipt_id)
        bucket = 'approved' if status['approved'] else 'rejected' if status['applicable'] else 'pending'
        counts[bucket] += 1
        projected.append({**job, 'operator_review': status})
    return projected, counts
