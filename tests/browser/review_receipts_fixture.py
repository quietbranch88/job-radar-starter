"""Build synthetic review cards through the actual CLI and local receipt store."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys

from job_radar.domain.tracking import stable_job_id
from job_radar.review_receipts import ReviewReceiptStore


def build(output: Path) -> None:
    root = output.parent / 'operator-review-fixture'
    root.mkdir(parents=True, exist_ok=True)
    receipts = ReviewReceiptStore(root / 'user-data' / 'review' / 'receipts.json')
    jobs = []
    reviewed_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    for name, decision, resume in [('approved', 'approve', 'b' * 64),
                                    ('rejected', 'reject', 'b' * 64),
                                    ('mismatch', 'approve', 'c' * 64)]:
        job_id = stable_job_id(source='greenhouse', external_id=name,
                               company='Synthetic Systems', title=name + ' Backend Engineer')
        jobs.append(dict(source='greenhouse', external_id=name, company='Synthetic Systems',
                         title=name + ' Backend Engineer', location='Toronto',
                         url='https://example.test/jobs/' + name, published_at='2026-10-24T00:00:00Z',
                         jd_hash='a' * 64, score=90, country='CA', category='backend'))
        receipts.record(job_id, 'a' * 64, resume, 'Private Reviewer', 'private-review-evidence',
                        decision, reviewed_at, confirmed=True)
    snapshot = root / 'jobs.json'
    snapshot.write_text(json.dumps({'contract_version': 1, 'mode': 'atomic', 'incomplete': False,
                                    'jobs': jobs, 'failures': []}), encoding='utf-8')
    subprocess.run([sys.executable, '-B', '-m', 'job_radar', '--json', 'build-dashboard',
                    '--jobs', str(snapshot), '--output', str(output),
                    '--user-data', str(root / 'user-data'), '--resume-sha256', 'b' * 64], check=True)


if __name__ == '__main__':
    build(Path(sys.argv[1]).resolve())
