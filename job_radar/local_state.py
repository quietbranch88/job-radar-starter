"""Bounded local JSON writes and single-host OS writer guards."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterator


MAX_STATE_BYTES = 128 * 1024 * 1024


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('local state contains duplicate fields')
        result[key] = value
    return result


def read_state(path: Path) -> dict[str, Any]:
    with path.open('rb') as stream:
        raw = stream.read(MAX_STATE_BYTES + 1)
    if len(raw) > MAX_STATE_BYTES:
        raise ValueError('local state exceeds 128 MiB')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise ValueError('local state must be an object')
    return value


def write_state(path: Path, value: dict[str, Any]) -> None:
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')
    if len(raw) > MAX_STATE_BYTES:
        raise ValueError('local state exceeds 128 MiB')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


@contextmanager
def writer_guard(path: Path) -> Iterator[None]:
    """Hold a persistent guard inode; never unlink it, including after release."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b'\0')
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError('local state is already in use by another writer') from None
        yield
    finally:
        os.close(descriptor)
