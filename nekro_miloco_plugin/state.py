from __future__ import annotations

import json
import secrets
import time
from typing import Any, Iterable

from .models import CatalogCache, PendingOperation


CHAT_BINDINGS_KEY = "chat_bindings"
PUSH_ENABLED_KEY = "event_push_enabled"


catalog_cache = CatalogCache()
pending_operations: dict[str, PendingOperation] = {}


async def get_bound_chats(store: Any) -> list[str]:
    raw = await store.get(store_key=CHAT_BINDINGS_KEY)
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


async def set_bound_chats(store: Any, chats: Iterable[str]) -> None:
    unique = sorted(dict.fromkeys(chat for chat in chats if chat))
    await store.set(store_key=CHAT_BINDINGS_KEY, value=json.dumps(unique, ensure_ascii=False))


async def add_bound_chat(store: Any, chat_key: str) -> list[str]:
    chats = await get_bound_chats(store)
    if chat_key not in chats:
        chats.append(chat_key)
        await set_bound_chats(store, chats)
    return chats


async def remove_bound_chat(store: Any, chat_key: str) -> list[str]:
    chats = [item for item in await get_bound_chats(store) if item != chat_key]
    await set_bound_chats(store, chats)
    return chats


async def get_push_enabled(store: Any, default: bool) -> bool:
    raw = await store.get(store_key=PUSH_ENABLED_KEY)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


async def set_push_enabled(store: Any, enabled: bool) -> None:
    await store.set(store_key=PUSH_ENABLED_KEY, value="true" if enabled else "false")


def create_pending_operation(
    *,
    operation: str,
    summary: str,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    ttl_seconds: int,
) -> PendingOperation:
    cleanup_pending_operations()
    token = secrets.token_urlsafe(6)
    now = time.time()
    item = PendingOperation(
        token=token,
        created_at=now,
        expires_at=now + ttl_seconds,
        operation=operation,
        summary=summary,
        method=method,
        path=path,
        body=body,
    )
    pending_operations[token] = item
    return item


def pop_pending_operation(token: str) -> PendingOperation | None:
    cleanup_pending_operations()
    return pending_operations.pop(token, None)


def cleanup_pending_operations() -> None:
    expired = [token for token, item in pending_operations.items() if item.expired]
    for token in expired:
        pending_operations.pop(token, None)

