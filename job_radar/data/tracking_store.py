from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any


_STATE_PATHS = {
    "applications": Path("tracking") / "applications.json",
    "events": Path("tracking") / "events.json",
    "lifecycle": Path("tracking") / "lifecycle.json",
    "rejected": Path("tracking") / "rejected.json",
    "review_cache": Path("review") / "cache.json",
    "company_facts": Path("review") / "company-facts.json",
}
_EXPORT_KINDS = ("applications", "events", "lifecycle", "rejected")
_EMPTY_DOCUMENT = {"contract_version": 1, "items": []}
_APPLICATION_EVENT_JOURNAL = (
    Path("tracking") / ".application-event.journal.json"
)


class StateRecoveryRequired(ValueError):
    """Raised when state cannot be parsed and must be recovered by its owner."""

    def __init__(self, path: Path):
        self.path = path
        super().__init__(f"state recovery required for malformed JSON: {path}")


def _default_replace(source: Path, destination: Path) -> None:
    os.replace(source, destination)


class LocalTrackingStore:
    """Atomic JSON persistence inside an explicitly configured user-data root."""

    def __init__(
        self,
        root: Path | str,
        *,
        replace_file: Callable[[Path, Path], None] = _default_replace,
    ):
        supplied = Path(root)
        if not supplied.is_absolute():
            raise ValueError("user-data root must be an absolute path without traversal")
        self.root = supplied.resolve(strict=False)
        self._replace_file = replace_file

    def path_for(self, kind: str) -> Path:
        if kind not in _STATE_PATHS:
            raise ValueError(f"unsupported state kind: {kind}")
        path = (self.root / _STATE_PATHS[kind]).resolve(strict=False)
        if not self._is_within_root(path):
            raise ValueError(f"state kind escapes user-data root: {kind}")
        return path

    def initialize(self) -> dict[str, Path]:
        self._recover_pending_application_event()
        paths = {kind: self.path_for(kind) for kind in _STATE_PATHS}
        for path in paths.values():
            if path.exists():
                self._read_path(path)
                continue
            self._atomic_write(path, _EMPTY_DOCUMENT, replace_file=_default_replace)
        return paths

    def read(self, kind: str) -> Any:
        self._recover_pending_application_event()
        path = self.path_for(kind)
        if not path.exists():
            return json.loads(json.dumps(_EMPTY_DOCUMENT))
        return self._read_path(path)

    def write(self, kind: str, document: Any) -> Path:
        self._recover_pending_application_event()
        path = self.path_for(kind)
        if path.exists():
            self._read_path(path)
        self._atomic_write(path, document)
        return path

    def commit_application_event(
        self,
        applications: dict[str, Any],
        events: dict[str, Any],
    ) -> None:
        """Commit application state and its audit event as one recoverable unit."""

        self._recover_pending_application_event()
        self._validate_versioned_items("applications", applications)
        self._validate_versioned_items("events", events)
        before = {
            "applications": self.read("applications"),
            "events": self.read("events"),
        }
        self._validate_versioned_items("applications", before["applications"])
        self._validate_versioned_items("events", before["events"])

        # Serialize both documents before creating the journal or replacing state.
        json.dumps(applications, ensure_ascii=False, sort_keys=True)
        json.dumps(events, ensure_ascii=False, sort_keys=True)
        journal = {
            "contract_version": 1,
            "transaction": "application_event",
            "before": before,
        }
        journal_path = self._journal_path()
        self._atomic_write(
            journal_path,
            journal,
            replace_file=_default_replace,
        )
        try:
            self._atomic_write(self.path_for("applications"), applications)
            self._atomic_write(self.path_for("events"), events)
        except Exception:
            self._recover_pending_application_event()
            raise
        journal_path.unlink()

    def export_to(self, destination: Path | str) -> Path:
        target = Path(destination).resolve(strict=False)
        if self._is_within_root(target):
            raise ValueError("export destination must be outside managed user-data")
        payload = {
            "contract_version": 1,
            "state": {kind: self.read(kind) for kind in _EXPORT_KINDS},
        }
        self._atomic_write(target, payload)
        return target

    def delete_tracking_data(self) -> dict[str, Path]:
        self._recover_pending_application_event()
        removed: dict[str, Path] = {}
        for kind in _STATE_PATHS:
            path = self.path_for(kind)
            if not path.exists():
                continue
            path.unlink()
            removed[kind] = path
        self._remove_empty_managed_directories()
        return removed

    def _atomic_write(
        self,
        destination: Path,
        document: Any,
        *,
        replace_file: Callable[[Path, Path], None] | None = None,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.stem}.",
                suffix=".tmp",
                dir=destination.parent,
            )
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    document,
                    handle,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            (replace_file or self._replace_file)(temporary, destination)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    @staticmethod
    def _read_path(path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StateRecoveryRequired(path) from error

    def _is_within_root(self, path: Path) -> bool:
        try:
            path.relative_to(self.root)
        except ValueError:
            return False
        return True

    def _journal_path(self) -> Path:
        path = (self.root / _APPLICATION_EVENT_JOURNAL).resolve(strict=False)
        if not self._is_within_root(path):
            raise ValueError("application event journal escapes user-data root")
        return path

    def _recover_pending_application_event(self) -> None:
        journal_path = self._journal_path()
        if not journal_path.exists():
            return
        journal = self._read_path(journal_path)
        if (
            not isinstance(journal, dict)
            or set(journal) != {"contract_version", "transaction", "before"}
            or journal.get("contract_version") != 1
            or journal.get("transaction") != "application_event"
            or not isinstance(journal.get("before"), dict)
            or set(journal["before"]) != {"applications", "events"}
        ):
            raise StateRecoveryRequired(journal_path)
        before = journal["before"]
        self._validate_versioned_items("applications", before["applications"])
        self._validate_versioned_items("events", before["events"])
        self._atomic_write(
            self.path_for("applications"),
            before["applications"],
            replace_file=_default_replace,
        )
        self._atomic_write(
            self.path_for("events"),
            before["events"],
            replace_file=_default_replace,
        )
        journal_path.unlink()

    @staticmethod
    def _validate_versioned_items(kind: str, document: Any) -> None:
        if (
            not isinstance(document, dict)
            or set(document) != {"contract_version", "items"}
            or document.get("contract_version") != 1
            or not isinstance(document.get("items"), list)
            or not all(isinstance(item, dict) for item in document["items"])
        ):
            raise ValueError(f"{kind} state must be a versioned items document")

    def _remove_empty_managed_directories(self) -> None:
        managed = sorted(
            {path.parent for path in _STATE_PATHS.values()},
            key=lambda path: len(path.parts),
            reverse=True,
        )
        for relative in managed:
            directory = self.root / relative
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
