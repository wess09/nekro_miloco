from __future__ import annotations

import asyncio
import importlib

import pytest


state_mod = importlib.import_module("nekro_miloco_plugin.state")


class MemoryStore:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, chat_key: str = "", user_key: str = "", store_key: str = ""):
        return self.data.get(store_key)

    async def set(self, chat_key: str = "", user_key: str = "", store_key: str = "", value: str = ""):
        self.data[store_key] = value
        return 0


@pytest.mark.asyncio
async def test_chat_binding_roundtrip() -> None:
    store = MemoryStore()

    await state_mod.add_bound_chat(store, "onebot_v11-group_1")
    await state_mod.add_bound_chat(store, "onebot_v11-group_1")
    await state_mod.add_bound_chat(store, "sse-room")

    assert await state_mod.get_bound_chats(store) == ["onebot_v11-group_1", "sse-room"]

    await state_mod.remove_bound_chat(store, "onebot_v11-group_1")
    assert await state_mod.get_bound_chats(store) == ["sse-room"]


def test_pending_operation_expires() -> None:
    pending = state_mod.create_pending_operation(
        operation="set_property",
        summary="test",
        method="POST",
        path="/api/miot/devices/did/control",
        body={},
        ttl_seconds=1,
    )

    assert state_mod.pop_pending_operation(pending.token) is pending
    assert state_mod.pop_pending_operation(pending.token) is None

    expired = state_mod.create_pending_operation(
        operation="set_property",
        summary="test",
        method="POST",
        path="/api/miot/devices/did/control",
        body={},
        ttl_seconds=0,
    )
    asyncio.run(asyncio.sleep(0))
    assert state_mod.pop_pending_operation(expired.token) is None

