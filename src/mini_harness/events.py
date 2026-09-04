from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class HarnessEvent:
    timestamp: str
    event: str
    session_id: str | None
    details: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "event": self.event,
            "session_id": self.session_id,
            "details": dict(self.details),
        }


class EventSink(Protocol):
    def emit(
        self,
        event: str,
        *,
        session_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None: ...


class JsonlEventSink:
    """Best-effort structured diagnostics with deliberately metadata-only events."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def emit(
        self,
        event: str,
        *,
        session_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        record = _build_event(event, session_id=session_id, details=details)
        encoded = json.dumps(
            record.as_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.write("\n")
                handle.flush()


class MemoryEventSink:
    """Test sink that preserves the exact structured events in memory."""

    def __init__(self) -> None:
        self.events: list[HarnessEvent] = []

    def emit(
        self,
        event: str,
        *,
        session_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.events.append(
            _build_event(event, session_id=session_id, details=details)
        )


def _build_event(
    event: str,
    *,
    session_id: str | None,
    details: Mapping[str, Any] | None,
) -> HarnessEvent:
    if not isinstance(event, str) or not event.strip():
        raise ValueError("event must be a non-empty string")
    safe_details = dict(details or {})
    json.dumps(safe_details, ensure_ascii=False)
    timestamp = (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    return HarnessEvent(
        timestamp=timestamp,
        event=event.strip(),
        session_id=session_id,
        details=safe_details,
    )
