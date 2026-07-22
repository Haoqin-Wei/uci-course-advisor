"""Replaceable persistence boundary for automatic-term state."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator, Optional, Protocol


TERM_STATE_SCHEMA_VERSION = 1


@dataclass
class TermStateSnapshot:
    schema_version: int = TERM_STATE_SCHEMA_VERSION
    calendar_records: list[dict] = field(default_factory=list)
    websoc_terms: list[str] = field(default_factory=list)
    availability: dict[str, dict] = field(default_factory=dict)
    automatic_term: Optional[str] = None
    source: str = "code_fallback"
    status: str = "fallback"
    source_urls: dict[str, str] = field(default_factory=dict)
    last_success_at: Optional[str] = None
    last_attempt_at: Optional[str] = None
    last_error: Optional[str] = None
    transition: Optional[dict] = None

    @classmethod
    def from_dict(cls, raw: dict) -> "TermStateSnapshot":
        if not isinstance(raw, dict):
            raise ValueError("term state must be an object")
        fields = cls.__dataclass_fields__
        values = {name: raw[name] for name in fields if name in raw}
        state = cls(**values)
        if state.schema_version != TERM_STATE_SCHEMA_VERSION:
            raise ValueError(f"unsupported term-state schema: {state.schema_version!r}")
        if not isinstance(state.calendar_records, list):
            raise ValueError("calendar_records must be a list")
        if not isinstance(state.websoc_terms, list):
            raise ValueError("websoc_terms must be a list")
        if not isinstance(state.availability, dict):
            raise ValueError("availability must be an object")
        return state

    def to_dict(self) -> dict:
        return asdict(self)


class TermStateStore(Protocol):
    def load(self) -> Optional[TermStateSnapshot]: ...

    def save(self, state: TermStateSnapshot) -> None: ...

    @contextmanager
    def sync_lock(self) -> Iterator[bool]: ...


_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.Lock] = {}


def _lock_for_path(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.Lock())


class JsonFileTermStateStore:
    """Atomic JSON cache with a process-local, non-blocking sync lock."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = _lock_for_path(self.path)

    def load(self) -> Optional[TermStateSnapshot]:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return TermStateSnapshot.from_dict(raw)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def save(self, state: TermStateSnapshot) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    @contextmanager
    def sync_lock(self) -> Iterator[bool]:
        acquired = self._lock.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                self._lock.release()


class InMemoryTermStateStore:
    """Deterministic store used by tests and replaceable deployments."""

    def __init__(self, state: Optional[TermStateSnapshot] = None):
        self.state = state
        self._lock = threading.Lock()

    def load(self) -> Optional[TermStateSnapshot]:
        if self.state is None:
            return None
        return TermStateSnapshot.from_dict(self.state.to_dict())

    def save(self, state: TermStateSnapshot) -> None:
        self.state = TermStateSnapshot.from_dict(state.to_dict())

    @contextmanager
    def sync_lock(self) -> Iterator[bool]:
        acquired = self._lock.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                self._lock.release()

