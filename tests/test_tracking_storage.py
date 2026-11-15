from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import ModuleType

import pytest


STATE_KINDS = {
    "applications",
    "events",
    "lifecycle",
    "rejected",
    "review_cache",
    "company_facts",
}
EXPORT_KINDS = {"applications", "events", "lifecycle", "rejected"}


@pytest.fixture()
def storage() -> ModuleType:
    """Load the production adapter without making a missing module a collection error."""

    try:
        return importlib.import_module("job_radar.data.tracking_store")
    except ModuleNotFoundError as error:
        if error.name not in {"job_radar.data", "job_radar.data.tracking_store"}:
            raise
        pytest.fail(
            "production API missing: implement job_radar.data.tracking_store",
            pytrace=False,
        )


@pytest.fixture()
def store(storage: ModuleType, tmp_path: Path):
    return storage.LocalTrackingStore(tmp_path / "user-data")


def _document(marker: str) -> dict[str, object]:
    return {
        "contract_version": 1,
        "items": [{"id": marker}],
    }


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def test_initialization_is_idempotent_and_does_not_replace_existing_state(
    store,
):
    first = store.initialize()
    application_path = store.path_for("applications")
    application_path.write_text(
        json.dumps(_document("application_01")),
        encoding="utf-8",
    )

    second = store.initialize()

    assert first == second
    assert set(first) == STATE_KINDS
    assert json.loads(application_path.read_text(encoding="utf-8")) == _document(
        "application_01"
    )


def test_every_mutable_state_file_stays_under_configured_user_data_root(store):
    paths = store.initialize()

    assert set(paths) == STATE_KINDS
    assert all(_is_within(path, store.root) for path in paths.values())
    assert all(path.is_file() for path in paths.values())
    assert {path.suffix for path in paths.values()} == {".json"}

    actual_files = {
        path.resolve()
        for path in store.root.rglob("*")
        if path.is_file()
    }
    assert actual_files == {path.resolve() for path in paths.values()}


@pytest.mark.parametrize("kind", sorted(STATE_KINDS))
def test_atomic_write_replaces_the_complete_document_without_temp_leftovers(
    store,
    kind: str,
):
    store.initialize()

    store.write(kind, _document(f"{kind}_01"))

    assert store.read(kind) == _document(f"{kind}_01")
    assert not list(store.root.rglob("*.tmp"))
    assert not list(store.root.rglob(".*.tmp"))


def test_failed_atomic_replace_preserves_previous_document_and_cleans_temp_file(
    storage: ModuleType,
    tmp_path: Path,
):
    root = tmp_path / "user-data"
    normal = storage.LocalTrackingStore(root)
    normal.initialize()
    normal.write("applications", _document("before"))

    def fail_replace(source: Path, destination: Path) -> None:
        assert _is_within(source, root)
        assert destination == normal.path_for("applications")
        raise OSError("simulated replace failure")

    failing = storage.LocalTrackingStore(root, replace_file=fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        failing.write("applications", _document("after"))

    assert normal.read("applications") == _document("before")
    assert not list(root.rglob("*.tmp"))
    assert not list(root.rglob(".*.tmp"))


def test_second_event_replace_failure_rolls_back_application_and_event_state(
    storage: ModuleType,
    tmp_path: Path,
):
    root = tmp_path / "user-data"
    normal = storage.LocalTrackingStore(root)
    normal.initialize()
    before_applications = _document("application-before")
    before_events = _document("event-before")
    normal.write("applications", before_applications)
    normal.write("events", before_events)
    failed_once = False

    def fail_second_replace(source: Path, destination: Path) -> None:
        nonlocal failed_once
        if destination == normal.path_for("events") and not failed_once:
            failed_once = True
            raise OSError("simulated second replace failure")
        source.replace(destination)

    failing = storage.LocalTrackingStore(root, replace_file=fail_second_replace)

    with pytest.raises(OSError, match="simulated second replace failure"):
        failing.commit_application_event(
            _document("application-after"),
            _document("event-after"),
        )

    assert normal.read("applications") == before_applications
    assert normal.read("events") == before_events
    assert not list(root.rglob("*.journal.json"))
    assert not list(root.rglob("*.tmp"))
    assert not list(root.rglob(".*.tmp"))


def test_pending_event_transaction_is_recovered_before_state_is_read(
    storage: ModuleType,
    tmp_path: Path,
):
    root = tmp_path / "user-data"
    store = storage.LocalTrackingStore(root)
    store.initialize()
    before_applications = _document("application-before")
    before_events = _document("event-before")
    store.write("applications", before_applications)
    store.write("events", before_events)
    journal = root / "tracking" / ".application-event.journal.json"
    journal.write_text(
        json.dumps(
            {
                "contract_version": 1,
                "transaction": "application_event",
                "before": {
                    "applications": before_applications,
                    "events": before_events,
                },
            }
        ),
        encoding="utf-8",
    )
    store.path_for("applications").write_text(
        json.dumps(_document("application-partial")),
        encoding="utf-8",
    )

    assert store.read("applications") == before_applications
    assert store.read("events") == before_events
    assert not journal.exists()


def test_finished_event_transaction_is_kept_when_the_journal_remains(
    storage: ModuleType,
    tmp_path: Path,
):
    root = tmp_path / "user-data"
    store = storage.LocalTrackingStore(root)
    store.initialize()
    before_applications = _document("application-before")
    before_events = _document("event-before")
    after_applications = _document("application-after")
    after_events = _document("event-after")
    store.write("applications", before_applications)
    store.write("events", before_events)
    store.write("applications", after_applications)
    store.write("events", after_events)
    journal = root / "tracking" / ".application-event.journal.json"
    journal.write_text(
        json.dumps(
            {
                "contract_version": 1,
                "transaction": "application_event",
                "before": {
                    "applications": before_applications,
                    "events": before_events,
                },
                "after": {
                    "applications": after_applications,
                    "events": after_events,
                },
            }
        ),
        encoding="utf-8",
    )

    assert store.read("applications") == after_applications
    assert store.read("events") == after_events
    assert not journal.exists()


def test_partial_event_transaction_with_an_after_image_rolls_back(
    storage: ModuleType,
    tmp_path: Path,
):
    root = tmp_path / "user-data"
    store = storage.LocalTrackingStore(root)
    store.initialize()
    before_applications = _document("application-before")
    before_events = _document("event-before")
    store.write("applications", before_applications)
    store.write("events", before_events)
    journal = root / "tracking" / ".application-event.journal.json"
    journal.write_text(
        json.dumps(
            {
                "contract_version": 1,
                "transaction": "application_event",
                "before": {
                    "applications": before_applications,
                    "events": before_events,
                },
                "after": {
                    "applications": _document("application-after"),
                    "events": _document("event-after"),
                },
            }
        ),
        encoding="utf-8",
    )
    store.path_for("applications").write_text(
        json.dumps(_document("application-after")),
        encoding="utf-8",
    )

    assert store.read("applications") == before_applications
    assert store.read("events") == before_events
    assert not journal.exists()


def test_malformed_event_journal_is_preserved_and_does_not_overwrite_state(
    storage: ModuleType,
    tmp_path: Path,
):
    root = tmp_path / "user-data"
    store = storage.LocalTrackingStore(root)
    store.initialize()
    applications = _document("application-current")
    events = _document("event-current")
    store.write("applications", applications)
    store.write("events", events)
    journal = root / "tracking" / ".application-event.journal.json"
    malformed = '{"contract_version":1,"transaction":"application_event","before":'
    journal.write_text(malformed, encoding="utf-8")

    with pytest.raises(storage.StateRecoveryRequired) as error:
        store.read("applications")

    assert error.value.path == journal
    assert journal.read_text(encoding="utf-8") == malformed
    assert json.loads(store.path_for("applications").read_text(encoding="utf-8")) == (
        applications
    )
    assert json.loads(store.path_for("events").read_text(encoding="utf-8")) == events


def test_temp_directory_store_commits_application_and_event_together(
    storage: ModuleType,
    tmp_path: Path,
):
    store = storage.LocalTrackingStore(tmp_path / "user-data")
    store.initialize()

    store.commit_application_event(
        _document("application-after"),
        _document("event-after"),
    )

    assert store.read("applications") == _document("application-after")
    assert store.read("events") == _document("event-after")
    assert not list(store.root.rglob("*.journal.json"))


def test_malformed_existing_json_is_preserved_for_recovery_and_not_overwritten(
    storage: ModuleType,
    tmp_path: Path,
):
    root = tmp_path / "user-data"
    malformed_path = root / "tracking" / "applications.json"
    malformed_path.parent.mkdir(parents=True)
    malformed = '{"contract_version": 1, "items": ['
    malformed_path.write_text(malformed, encoding="utf-8")
    store = storage.LocalTrackingStore(root)

    with pytest.raises(storage.StateRecoveryRequired) as error:
        store.initialize()

    assert error.value.path == malformed_path
    assert malformed_path.read_text(encoding="utf-8") == malformed
    assert not list(root.rglob("*.tmp"))


def test_malformed_json_is_never_replaced_by_a_write_until_user_recovers_it(
    storage: ModuleType,
    tmp_path: Path,
):
    root = tmp_path / "user-data"
    store = storage.LocalTrackingStore(root)
    path = store.path_for("events")
    path.parent.mkdir(parents=True)
    malformed = "{broken"
    path.write_text(malformed, encoding="utf-8")

    with pytest.raises(storage.StateRecoveryRequired):
        store.write("events", _document("event_01"))

    assert path.read_text(encoding="utf-8") == malformed


@pytest.mark.parametrize(
    "unsafe_root",
    [
        Path("..") / "outside",
        Path(".") / ".." / "outside",
    ],
)
def test_relative_user_data_root_with_traversal_is_rejected(
    storage: ModuleType,
    unsafe_root: Path,
):
    with pytest.raises(ValueError, match="root|traversal|absolute"):
        storage.LocalTrackingStore(unsafe_root)


@pytest.mark.parametrize(
    "kind",
    [
        "../applications",
        r"..\applications",
        "/absolute",
        r"C:\absolute",
        "tracking/applications",
        "",
        "unknown",
    ],
)
def test_state_kind_is_allowlisted_and_cannot_escape_root(store, kind: str):
    with pytest.raises(ValueError, match="kind|state|unsupported"):
        store.path_for(kind)


def test_export_is_explicit_and_contains_only_portable_tracking_state(
    store,
    tmp_path: Path,
):
    store.initialize()
    for kind in STATE_KINDS:
        store.write(kind, _document(kind))
    destination = tmp_path / "exports" / "tracking-export.json"

    assert not destination.exists()

    exported = store.export_to(destination)

    assert exported == destination
    assert destination.is_file()
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["contract_version"] == 1
    assert set(payload["state"]) == EXPORT_KINDS
    assert payload["state"] == {
        kind: _document(kind)
        for kind in sorted(EXPORT_KINDS)
    }
    serialized = destination.read_text(encoding="utf-8")
    assert "review_cache" not in serialized
    assert "company_facts" not in serialized


def test_export_rejects_destination_inside_managed_user_data_root(
    store,
):
    store.initialize()

    with pytest.raises(ValueError, match="export|destination|user-data"):
        store.export_to(store.root / "export.json")


def test_export_failure_does_not_leave_partial_destination_or_temp_file(
    storage: ModuleType,
    tmp_path: Path,
):
    root = tmp_path / "user-data"
    destination = tmp_path / "exports" / "tracking-export.json"

    def fail_replace(source: Path, target: Path) -> None:
        assert target == destination
        raise OSError("simulated export failure")

    store = storage.LocalTrackingStore(root, replace_file=fail_replace)
    store.initialize()

    with pytest.raises(OSError, match="simulated export failure"):
        store.export_to(destination)

    assert not destination.exists()
    assert not list(tmp_path.rglob("*.tmp"))
    assert not list(tmp_path.rglob(".*.tmp"))


def test_delete_removes_only_scoped_tracking_state_and_preserves_unrelated_data(
    store,
):
    paths = store.initialize()
    unrelated = store.root / "preferences.local.json"
    unrelated.write_text('{"keep": true}', encoding="utf-8")
    nested_unrelated = store.root / "notes" / "keep.txt"
    nested_unrelated.parent.mkdir()
    nested_unrelated.write_text("keep", encoding="utf-8")

    removed = store.delete_tracking_data()

    assert set(removed) == STATE_KINDS
    assert all(not path.exists() for path in paths.values())
    assert unrelated.read_text(encoding="utf-8") == '{"keep": true}'
    assert nested_unrelated.read_text(encoding="utf-8") == "keep"
    assert store.root.is_dir()


def test_delete_is_retry_safe_when_tracking_state_is_already_absent(store):
    store.initialize()

    first = store.delete_tracking_data()
    second = store.delete_tracking_data()

    assert set(first) == STATE_KINDS
    assert second == {}


def test_gitignore_protects_the_complete_user_data_tree():
    gitignore = (
        Path(__file__).resolve().parents[1] / ".gitignore"
    ).read_text(encoding="utf-8").splitlines()
    rules = {
        line.strip()
        for line in gitignore
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "user-data/" in rules or "/user-data/" in rules


def test_blank_tracking_examples_exist_and_contain_no_private_or_personal_data():
    repository = Path(__file__).resolve().parents[1]
    expected = {
        repository / "examples" / "tracking-state.example.json",
        repository / "examples" / "aliases.example.json",
    }

    assert all(path.is_file() for path in expected)
    combined = "\n".join(path.read_text(encoding="utf-8") for path in expected)
    lowered = combined.casefold()
    synthetic_owner = "private-" + "owner"
    forbidden = {
        synthetic_owner,
        synthetic_owner + "-handle",
        synthetic_owner + "-site.invalid",
        "104.com.tw",
        "linkedin.com",
        "indeed.com",
        "cloudflare",
        "account_id",
        "namespace_id",
        "api_key",
        "password",
        "resume",
        r"c:\users",
    }

    assert not {term for term in forbidden if term in lowered}
    tracking = json.loads(
        (repository / "examples" / "tracking-state.example.json").read_text(
            encoding="utf-8"
        )
    )
    aliases = json.loads(
        (repository / "examples" / "aliases.example.json").read_text(
            encoding="utf-8"
        )
    )
    assert tracking == {
        "contract_version": 1,
        "applications": [],
        "events": [],
        "lifecycle": [],
        "rejected": [],
    }
    assert aliases == {
        "contract_version": 1,
        "company_aliases": {},
        "title_aliases": {},
    }
