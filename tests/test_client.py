from __future__ import annotations

import importlib
import sys

import pytest


client_mod = importlib.import_module("nekro_miloco_plugin.client")


def test_annotate_miot_control_response_marks_inner_failures() -> None:
    payload = {
        "code": 0,
        "message": "Device control executed successfully",
        "data": {"results": [{"code": -704042011}]},
    }

    client_mod.annotate_miot_control_response(payload)

    assert payload["code"] == -704042011
    assert "设备离线" in payload["message"]
    assert payload["data"]["results"][0]["code_msg"] == "设备离线"


@pytest.mark.asyncio
async def test_request_sends_bearer_and_unwraps_data() -> None:
    import httpx

    seen_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"code": 0, "message": "ok", "data": {"ok": True}})

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    httpx.AsyncClient = fake_client  # type: ignore[assignment]
    try:
        result = await client_mod.MilocoClient("http://miloco.local", "secret").get("/api/miot/status")
    finally:
        httpx.AsyncClient = original_client  # type: ignore[assignment]

    assert result == {"ok": True}
    assert seen_headers["authorization"] == "Bearer secret"


@pytest.mark.asyncio
async def test_request_raises_on_business_error() -> None:
    import httpx

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"code": 123, "message": "bad", "data": None})
    )
    original_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    httpx.AsyncClient = fake_client  # type: ignore[assignment]
    try:
        with pytest.raises(client_mod.MilocoBusinessError):
            await client_mod.MilocoClient("http://miloco.local").get("/api/miot/status")
    finally:
        httpx.AsyncClient = original_client  # type: ignore[assignment]

