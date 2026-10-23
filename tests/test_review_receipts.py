from __future__ import annotations

import json
from pathlib import Path
import traceback

import pytest

from job_radar import local_state, review_receipts
from job_radar.domain.tracking import stable_job_id
from job_radar.review_receipts import ReviewReceiptStore


JOB = stable_job_id(source='greenhouse', external_id='fixture-1', company='Fixture', title='Engineer')
OTHER_JOB = stable_job_id(source='greenhouse', external_id='fixture-2', company='Fixture', title='Engineer')
JD = 'a' * 64
RESUME = 'b' * 64
EARLY = '2026-10-24T01:00:00+00:00'
LATER = '2026-10-24T02:00:00+00:00'
ASOF = '2026-10-24T03:00:00+00:00'


def arguments(**changes):
    return dict(job_id=JOB, jd_sha256=JD, resume_sha256=RESUME,
                reviewer='Fixture Reviewer', evidence='review:independent-1',
                decision='approve', reviewed_at=EARLY, confirmed=True, asof=ASOF) | changes


def version(**changes):
    return dict(job_id=JOB, jd_sha256=JD, resume_sha256=RESUME, asof=ASOF) | changes


@pytest.fixture()
def path(tmp_path: Path) -> Path:
    return tmp_path / 'user-data' / 'review' / 'receipts.json'


@pytest.fixture()
def store(path: Path) -> ReviewReceiptStore:
    return ReviewReceiptStore(path)


def document(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def test_absent_receipts_fail_closed_without_creating_any_state(store, path):
    assert store.check(**version()) == {
        'applicable': False, 'approved': False, 'decision': None,
        'reason': 'no_receipt', 'receipt': None,
    }
    assert not path.parent.exists()


def test_explicit_receipt_persists_only_versioned_minimal_attestation(store, path):
    recorded = store.record(**arguments())
    assert set(recorded) == {
        'receipt_id', 'job_id', 'jd_sha256', 'resume_sha256', 'reviewer',
        'evidence', 'decision', 'reviewed_at',
    }
    assert recorded['job_id'] == JOB
    assert JOB.startswith('job_') and len(JOB) == 36
    assert recorded['jd_sha256'] == JD and recorded['resume_sha256'] == RESUME
    assert recorded['reviewer'] == 'Fixture Reviewer'
    assert recorded['evidence'] == 'review:independent-1'
    assert recorded['decision'] == 'approve'
    assert recorded['reviewed_at'] == '2026-10-24T01:00:00.000000+00:00'
    assert len(recorded['receipt_id']) == 64
    assert document(path) == {'contract_version': 1, 'items': [recorded]}
    assert ReviewReceiptStore(path).check(**version()) == {
        'applicable': True, 'approved': True, 'decision': 'approve',
        'reason': 'matched', 'receipt': recorded,
    }


@pytest.mark.parametrize('changes,reason', [
    ({'job_id': OTHER_JOB}, 'no_receipt'),
    ({'jd_sha256': 'c' * 64}, 'version_mismatch'),
    ({'resume_sha256': 'd' * 64}, 'version_mismatch'),
    ({'jd_sha256': 'c' * 64, 'resume_sha256': 'd' * 64}, 'version_mismatch'),
])
def test_every_version_identity_must_match(store, changes, reason):
    store.record(**arguments())
    result = store.check(**version(**changes))
    assert result == dict(applicable=False, approved=False, decision=None,
                          reason=reason, receipt=None)


@pytest.mark.parametrize('insert_rejection_first', [False, True])
def test_latest_rejection_wins_even_when_older_approval_is_inserted_last(store, path, insert_rejection_first):
    approval = arguments(reviewed_at=EARLY)
    rejection = arguments(decision='reject', reviewed_at=LATER, evidence='review:independent-2')
    for entry in ([rejection, approval] if insert_rejection_first else [approval, rejection]):
        store.record(**entry)
    result = store.check(**version())
    assert result['applicable'] is True
    assert result['approved'] is False
    assert result['decision'] == 'reject'
    assert result['receipt']['evidence'] == 'review:independent-2'
    assert len(document(path)['items']) == 2


@pytest.mark.parametrize('insert_rejection_first', [False, True])
def test_equal_time_conflict_rejects_in_both_insertion_orders(store, insert_rejection_first):
    approval = arguments(reviewed_at='2026-10-24T09:00:00+08:00')
    rejection = arguments(decision='reject', reviewed_at=EARLY)
    for entry in ([rejection, approval] if insert_rejection_first else [approval, rejection]):
        store.record(**entry)
    result = store.check(**version())
    assert result['decision'] == 'reject'
    assert result['approved'] is False


def test_chronology_uses_absolute_time_not_lexicographic_offsets(store):
    store.record(**arguments(reviewed_at='2026-10-24T10:00:00+09:00'))
    store.record(**arguments(decision='reject', reviewed_at='2026-10-24T00:30:00-01:00'))
    assert store.check(**version())['decision'] == 'reject'


def test_newer_explicit_same_version_approval_can_supersede_rejection(store, path):
    store.record(**arguments(decision='reject'))
    new_approval = store.record(**arguments(reviewed_at=LATER, evidence='review:reconsidered-2'))
    assert store.check(**version())['receipt'] == new_approval
    assert store.check(**version())['approved'] is True
    assert [item['decision'] for item in document(path)['items']] == ['reject', 'approve']


def test_other_version_approval_cannot_erase_rejection_of_original_version(store, path):
    rejected = store.record(**arguments(decision='reject'))
    approved = store.record(**arguments(jd_sha256='c' * 64, resume_sha256='d' * 64,
                                      reviewed_at=LATER))
    original = store.check(**version())
    assert original['decision'] == 'reject' and original['receipt'] == rejected
    changed = store.check(**version(jd_sha256='c' * 64, resume_sha256='d' * 64))
    assert changed['approved'] is True and changed['receipt'] == approved
    assert len(document(path)['items']) == 2


def test_rejection_for_another_job_cannot_replace_this_job_decision(store):
    approved = store.record(**arguments())
    store.record(**arguments(job_id=OTHER_JOB, decision='reject', reviewed_at=LATER))
    assert store.check(**version())['receipt'] == approved


def test_exact_repeat_is_idempotent_without_rewriting_or_reordering(store, path, monkeypatch):
    approved = store.record(**arguments())
    store.record(**arguments(decision='reject', reviewed_at=LATER))
    before = path.read_bytes()

    def no_rewrite(*args, **kwargs):
        raise AssertionError('exact repeats must not rewrite or change history order')

    monkeypatch.setattr(review_receipts, 'write_state', no_rewrite)
    assert store.record(**arguments()) == approved
    assert path.read_bytes() == before
    assert store.check(**version())['decision'] == 'reject'


def test_equivalent_timezone_spelling_has_one_canonical_receipt(store, path):
    original = store.record(**arguments())
    assert store.record(**arguments(reviewed_at='2026-10-24T09:00:00+08:00')) == original
    assert store.record(**arguments(reviewed_at='2026-10-24T01:00:00.000000Z')) == original
    assert document(path)['items'] == [original]


@pytest.mark.parametrize('changes', [
    {'job_id': OTHER_JOB}, {'jd_sha256': 'c' * 64}, {'resume_sha256': 'd' * 64},
    {'reviewer': 'Other Reviewer'}, {'evidence': 'review:independent-2'},
    {'decision': 'reject'}, {'reviewed_at': LATER},
])
def test_distinct_content_appends_instead_of_overwriting(store, path, changes):
    first = store.record(**arguments())
    second = store.record(**arguments(**changes))
    assert first['receipt_id'] != second['receipt_id']
    assert document(path)['items'] == [first, second]


def test_instances_reload_before_writing_and_returns_are_detached(store, path):
    other = ReviewReceiptStore(path)
    first = store.record(**arguments())
    second = other.record(**arguments(decision='reject', reviewed_at=LATER))
    first['decision'] = 'reject'
    second['decision'] = 'approve'
    result = store.check(**version())
    result['receipt']['decision'] = 'approve'
    assert [item['decision'] for item in document(path)['items']] == ['approve', 'reject']
    assert other.check(**version())['decision'] == 'reject'


@pytest.mark.parametrize('confirmed', [False, None, 0, 1, 'true', [], {}])
def test_operator_confirmation_requires_literal_true(store, path, confirmed):
    with pytest.raises(ValueError, match='confirmation'):
        store.record(**arguments(confirmed=confirmed))
    assert not path.parent.exists()


def test_omitted_confirmation_and_unknown_provider_fields_never_create_receipts(store, path):
    missing = arguments()
    del missing['confirmed']
    with pytest.raises(TypeError):
        store.record(**missing)
    with pytest.raises(TypeError):
        store.record(**arguments(provider_complete=True))
    with pytest.raises(ValueError):
        store.record(**arguments(decision='complete'))
    assert not path.parent.exists()


@pytest.mark.parametrize('field', ['jd_sha256', 'resume_sha256'])
@pytest.mark.parametrize('invalid', [None, 1, {}, '', 'a' * 63, 'a' * 65, 'A' * 64,
                                     'g' * 64, 'a' * 64 + '\n', ' ' + 'a' * 64])
def test_hashes_are_strict_lowercase_sha256_for_record_and_check(store, path, field, invalid):
    with pytest.raises(ValueError, match='SHA256'):
        store.record(**arguments(**{field: invalid}))
    with pytest.raises(ValueError, match='SHA256'):
        store.check(**version(**{field: invalid}))
    assert not path.parent.exists()


@pytest.mark.parametrize('field,invalid', [
    ('job_id', ''), ('job_id', 'job with spaces'), ('job_id', 'x' * 129),
    ('job_id', '../job'), ('job_id', {'raw': 'private body'}),
    ('reviewer', ''), ('reviewer', ' Fixture Reviewer'), ('reviewer', 'x' * 201),
    ('reviewer', '測' * 67), ('reviewer', 'Reviewer\nOther'),
    ('reviewer', {'raw': 'private body'}), ('reviewer', '{"private":"body"}'),
    ('evidence', ''), ('evidence', 'x' * 1025), ('evidence', ['private body']),
    ('evidence', '{"private":"body"}'), ('evidence', 'private body\nmore'),
    ('evidence', 'https://user:pass@example.test/review/1'),
    ('evidence', 'https://example.test/review?credential=private'),
    ('evidence', 'https:///missing-host'), ('evidence', 'http://example.test/review'),
])
def test_identity_and_reference_fields_are_bounded_metadata_not_arbitrary_private_json(store, path, field, invalid):
    with pytest.raises(ValueError):
        store.record(**arguments(**{field: invalid}))
    assert not path.parent.exists()


def test_reference_and_identity_are_retained_without_reading_or_fetching_content(store):
    recorded = store.record(**arguments(reviewer='測試 Reviewer', evidence='https://example.test/review/fixture-1#decision'))
    assert recorded['reviewer'] == '測試 Reviewer'
    assert recorded['evidence'] == 'https://example.test/review/fixture-1#decision'


@pytest.mark.parametrize('invalid', [None, 0, '', '2026-10-24', '2026-10-24T01:00:00',
                                     '2026-02-30T01:00:00Z', '2026-10-24 01:00:00Z',
                                     '2026-10-24T01:00:00+24:00', '2026-10-24T01:00:00-00:00',
                                     '2026-10-24T01:00:00+01:60', '2026-10-24T01:00:00-01:99',
                                     '2026-10-24T01:00:00Z\n', 'x' * 1000])
def test_malformed_review_and_cutoff_timestamps_fail_before_persistence(store, path, invalid):
    with pytest.raises(ValueError):
        store.record(**arguments(reviewed_at=invalid))
    if invalid is not None:
        with pytest.raises(ValueError):
            store.record(**arguments(asof=invalid))
        with pytest.raises(ValueError):
            store.check(**version(asof=invalid))
    assert not path.parent.exists()


def test_explicit_asof_rejects_future_records_without_using_a_wall_clock(store, path):
    with pytest.raises(ValueError, match='later than explicit asof'):
        store.record(**arguments(reviewed_at='2026-10-24T03:00:00.000001Z'))
    assert not path.exists()
    recorded = store.record(**arguments(reviewed_at=ASOF))
    assert store.check(**version())['receipt'] == recorded


def test_cutoff_excludes_future_decisions_and_can_inspect_historical_state(store):
    approval = store.record(**arguments())
    rejection = store.record(**arguments(decision='reject', reviewed_at=LATER))
    before = store.check(**version(asof='2026-10-24T00:59:59.999999Z'))
    assert before == dict(applicable=False, approved=False, decision=None,
                          reason='not_yet_reviewed', receipt=None)
    assert store.check(**version(asof=EARLY))['receipt'] == approval
    assert store.check(**version(asof=LATER))['receipt'] == rejection


def test_omitting_asof_uses_attested_times_and_does_not_invent_now(store):
    attested = store.record(**arguments(reviewed_at='2099-01-01T00:00:00Z', asof=None))
    assert store.check(**version(asof=None))['receipt'] == attested
    assert store.check(**version())['reason'] == 'not_yet_reviewed'


@pytest.mark.parametrize('raw', [
    b'not-json private body', b'{"contract_version":1,"items":[', b'\xff\xfe',
    b'[]', b'{"contract_version":1,"items":[],"items":[]}',
    b'{"contract_version":1,"items":[],"provider_complete":true}',
    b'{"contract_version":true,"items":[]}',
    b'{"contract_version":2,"items":[]}', b'{"contract_version":1,"items":{}}',
    b'{"contract_version":1,"items":[null]}',
    b'{"contract_version":1,"items":[{"provider_complete":true}]}',
])
def test_corrupt_or_unrecognized_state_is_preserved_byte_for_byte(store, path, raw):
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    for operation in (lambda: store.check(**version()), lambda: store.record(**arguments())):
        with pytest.raises(ValueError) as caught:
            operation()
        assert 'private body' not in str(caught.value)
        assert path.read_bytes() == raw
    assert list(path.parent.glob('*.tmp')) == []


@pytest.mark.parametrize('change', ['extra-field', 'missing-field', 'invalid-hash',
                                    'naive-time', 'tampered-decision', 'invalid-id',
                                    'duplicate-id', 'foreign-review-data'])
def test_corrupt_history_cannot_be_silently_skipped_to_find_an_approval(store, path, change):
    receipt = store.record(**arguments())
    state = document(path)
    item = state['items'][0]
    if change == 'extra-field':
        item['private_notes'] = 'private body'
    elif change == 'missing-field':
        del item['evidence']
    elif change == 'invalid-hash':
        item['jd_sha256'] = 'A' * 64
    elif change == 'naive-time':
        item['reviewed_at'] = '2026-10-24T01:00:00'
    elif change == 'tampered-decision':
        item['decision'] = 'reject'
    elif change == 'invalid-id':
        item['receipt_id'] = '0' * 64
    elif change == 'duplicate-id':
        state['items'].append(dict(receipt))
    else:
        state['items'].append({'provider': 'configured', 'state': 'complete'})
    raw = json.dumps(state).encode('utf-8')
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        store.check(**version())
    with pytest.raises(ValueError):
        store.record(**arguments(reviewed_at=LATER))
    assert path.read_bytes() == raw


def test_existing_local_state_read_bound_is_enforced_without_replacing_bytes(store, path, monkeypatch):
    store.record(**arguments())
    raw = path.read_bytes()
    assert local_state.MAX_STATE_BYTES == 128 * 1024 * 1024
    monkeypatch.setattr(local_state, 'MAX_STATE_BYTES', len(raw) - 1)
    with pytest.raises(ValueError):
        store.check(**version())
    with pytest.raises(ValueError):
        store.record(**arguments(reviewed_at=LATER))
    assert path.read_bytes() == raw


def test_existing_local_state_write_bound_preserves_previous_history(store, path, monkeypatch):
    store.record(**arguments())
    raw = path.read_bytes()
    monkeypatch.setattr(local_state, 'MAX_STATE_BYTES', len(raw) + 1)
    with pytest.raises(ValueError):
        store.record(**arguments(reviewed_at=LATER))
    assert path.read_bytes() == raw
    assert list(path.parent.glob('*.tmp')) == []


def test_history_limit_does_not_prune_or_block_idempotent_repeats(store, path, monkeypatch):
    monkeypatch.setattr(review_receipts, 'MAX_RECEIPTS', 1)
    receipt = store.record(**arguments())
    assert store.record(**arguments()) == receipt
    raw = path.read_bytes()
    with pytest.raises(ValueError):
        store.record(**arguments(reviewed_at=LATER))
    assert path.read_bytes() == raw
    monkeypatch.setattr(review_receipts, 'MAX_RECEIPTS', 0)
    with pytest.raises(ValueError):
        store.check(**version())


def test_atomic_replace_failure_keeps_prior_receipt_and_removes_temporary_file(store, path, monkeypatch):
    first = store.record(**arguments())
    raw = path.read_bytes()

    def failed_replace(*args):
        raise OSError('private filesystem diagnostic body')

    with monkeypatch.context() as patch:
        patch.setattr(local_state.os, 'replace', failed_replace)
        with pytest.raises(ValueError) as caught:
            store.record(**arguments(decision='reject', reviewed_at=LATER))
    assert 'private filesystem diagnostic body' not in ''.join(traceback.format_exception(caught.value))
    assert path.read_bytes() == raw
    assert list(path.parent.glob('*.tmp')) == []
    assert ReviewReceiptStore(path).check(**version())['receipt'] == first


def test_read_failures_have_safe_errors_and_cannot_be_treated_as_absent_history(store, path, monkeypatch):
    store.record(**arguments())
    raw = path.read_bytes()

    def failed_read(*args):
        raise PermissionError('private filesystem diagnostic body')

    monkeypatch.setattr(review_receipts, 'read_state', failed_read)
    for operation in (lambda: store.check(**version()), lambda: store.record(**arguments())):
        with pytest.raises(ValueError) as caught:
            operation()
        assert 'private filesystem diagnostic body' not in ''.join(traceback.format_exception(caught.value))
    assert path.read_bytes() == raw


def test_os_writer_guard_blocks_competing_record_and_retains_history(store, path):
    first = store.record(**arguments())
    guard = path.with_name(path.name + '.guard')
    raw = path.read_bytes()
    with local_state.writer_guard(guard):
        with pytest.raises(ValueError):
            ReviewReceiptStore(path).record(**arguments(decision='reject', reviewed_at=LATER))
        assert path.read_bytes() == raw
    assert guard.is_file()
    second = ReviewReceiptStore(path).record(**arguments(decision='reject', reviewed_at=LATER))
    assert document(path)['items'] == [first, second]


@pytest.mark.parametrize('invalid', ['string-path', Path('relative.json'), Path('../escape.json')])
def test_store_requires_explicit_absolute_path(invalid):
    with pytest.raises(ValueError, match='path'):
        ReviewReceiptStore(invalid)


@pytest.mark.parametrize('kind', ['non-json', 'directory', 'traversal', 'parent-file'])
def test_invalid_path_targets_fail_without_replacing_any_files(tmp_path, kind):
    if kind == 'non-json':
        target = tmp_path / 'resume.pdf'
    elif kind == 'directory':
        target = tmp_path / 'directory.json'
        target.mkdir()
    elif kind == 'traversal':
        target = tmp_path / '..' / 'escape.json'
    else:
        parent = tmp_path / 'not-a-directory'
        parent.write_bytes(b'keep parent bytes')
        target = parent / 'receipts.json'
    with pytest.raises(ValueError, match='path|file|parent'):
        ReviewReceiptStore(target)


@pytest.mark.parametrize('alias', ['state', 'guard'])
def test_hard_link_aliases_are_rejected_before_private_file_is_modified(tmp_path, alias):
    original = tmp_path / 'original.json'
    original.write_bytes(b'keep original private bytes')
    path = tmp_path / 'receipts.json'
    target = path if alias == 'state' else path.with_name(path.name + '.guard')
    target.hardlink_to(original)
    with pytest.raises(ValueError, match='unaliased'):
        ReviewReceiptStore(path)
    assert original.read_bytes() == b'keep original private bytes'
    assert target.read_bytes() == b'keep original private bytes'


@pytest.mark.parametrize('alias', ['state', 'guard', 'parent'])
def test_symlink_aliases_are_rejected_without_following_the_target(tmp_path, alias):
    private = tmp_path / 'private'
    private.mkdir()
    original = private / 'original.json'
    original.write_bytes(b'keep original private bytes')
    path = tmp_path / 'receipts.json'
    target = path if alias == 'state' else path.with_name(path.name + '.guard')
    if alias == 'parent':
        target = tmp_path / 'linked'
        path = target / 'receipts.json'
    try:
        target.symlink_to(private if alias == 'parent' else original, target_is_directory=alias == 'parent')
    except OSError:
        pytest.skip('host cannot create symlinks; alias test requires that filesystem capability')
    with pytest.raises(ValueError, match='links|junctions'):
        ReviewReceiptStore(path)
    assert original.read_bytes() == b'keep original private bytes'
    assert not (private / 'receipts.json').exists()
