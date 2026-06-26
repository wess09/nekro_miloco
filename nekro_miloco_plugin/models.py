from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal


ControlConfirmMode = Literal["always", "dangerous", "never"]


@dataclass(frozen=True)
class PendingOperation:
    token: str
    created_at: float
    expires_at: float
    operation: str
    summary: str
    method: str
    path: str
    body: dict[str, Any] | None = None

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at


@dataclass
class CatalogCache:
    data: dict[str, Any] | None = None
    fetched_at: float = 0.0

    def fresh(self, ttl_seconds: int) -> bool:
        return self.data is not None and time.time() - self.fetched_at < ttl_seconds


@dataclass
class EventState:
    task: Any | None = None
    last_event_ids: set[str] = field(default_factory=set)
    last_error: str = ""
    running: bool = False

