from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlencode

import httpx


MIOT_SPEC_CODE_MESSAGES = {
    -704042011: "设备离线",
    -704042001: "未找到设备",
    -704090001: "未找到设备",
    -704040003: "属性不存在",
    -704040004: "事件不存在",
    -704040005: "方法不存在",
    -704040999: "功能未上线",
    -704044006: "未找到功能定义",
    -704030013: "属性不可读",
    -704030023: "属性不可写",
    -704030033: "属性不可上报",
    -704030992: "请求过于频繁，本次被拒绝",
    -704220043: "属性值不正确",
    -704220035: "方法输入参数错误",
    -704220025: "方法输入参数数量不匹配",
    -704222035: "方法输出参数数量不匹配或参数错误",
    -704222034: "事件参数数量不匹配",
    -704220008: "非法的 ID",
    -704053100: "无法执行此操作",
    -704053101: "摄像机休眠中",
    -704013101: "红外设备不支持此操作",
    -704083036: "操作超时",
    -704012904: "设备未授权控制能力给小爱",
    -704012905: "设备未绑定",
    -704012906: "认证失败",
    -702022036: "操作正在处理中",
    -705201013: "读属性失败",
    -706012013: "读属性失败",
    -706012014: "读属性失败",
    -705201023: "写属性失败",
    -706012023: "写属性失败",
    -705201015: "方法执行失败",
    -706012015: "方法执行失败",
    -704002000: "设备错误",
}

MIOT_OK_CODES = {0, -702000000, -702010000}


class MilocoClientError(RuntimeError):
    pass


class MilocoBusinessError(MilocoClientError):
    def __init__(self, message: str, *, response: dict[str, Any] | None = None):
        super().__init__(message)
        self.response = response or {}


class MilocoClient:
    def __init__(
        self,
        base_url: str,
        token: str = "",
        *,
        timeout: float = 20.0,
        verify_tls: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.timeout = timeout
        self.verify_tls = verify_tls

    @property
    def headers(self) -> dict[str, str]:
        if not self.token:
            return {}
        return {"Authorization": f"Bearer {self.token}"}

    def _client(self, timeout: float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers=self.headers,
            timeout=timeout if timeout is not None else self.timeout,
            verify=self.verify_tls,
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float | None = None,
        unwrap: bool = True,
    ) -> Any:
        try:
            async with self._client(timeout) as client:
                response = await client.request(
                    method,
                    path,
                    params=params,
                    json=json_body,
                )
        except httpx.RequestError as exc:
            raise MilocoClientError(f"无法连接 miloco-backend: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            text = response.text[:300]
            raise MilocoClientError(f"miloco 返回非 JSON 响应: HTTP {response.status_code} {text}") from exc

        if not response.is_success:
            raise MilocoBusinessError(
                f"miloco HTTP {response.status_code}: {payload}",
                response=payload if isinstance(payload, dict) else {"payload": payload},
            )

        if isinstance(payload, dict) and payload.get("code", 0) != 0:
            raise MilocoBusinessError(
                str(payload.get("message") or payload),
                response=payload,
            )

        if unwrap and isinstance(payload, dict) and "data" in payload:
            return payload.get("data")
        return payload

    async def get(self, path: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        return await self.request("GET", path, params=params, **kwargs)

    async def post(self, path: str, body: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        return await self.request("POST", path, json_body=body or {}, **kwargs)

    async def control_device(self, did: str, body: dict[str, Any]) -> Any:
        response = await self.request(
            "POST",
            f"/api/miot/devices/{did}/control",
            json_body=body,
            unwrap=False,
        )
        annotate_miot_control_response(response)
        if isinstance(response, dict) and response.get("code", 0) != 0:
            raise MilocoBusinessError(str(response.get("message") or "设备执行失败"), response=response)
        return response.get("data") if isinstance(response, dict) else response

    async def download_event_clip(
        self,
        event_id: str,
        device_id: str,
        output_dir: Path,
    ) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        params = {"token": self.token} if self.token else None
        path = f"/api/events/{event_id}/clip/{device_id}"
        try:
            async with self._client(timeout=max(self.timeout, 60.0)) as client:
                response = await client.get(path, params=params)
        except httpx.RequestError as exc:
            raise MilocoClientError(f"下载事件片段失败: {exc}") from exc
        if not response.is_success:
            raise MilocoBusinessError(f"下载事件片段失败: HTTP {response.status_code} {response.text[:200]}")

        suffix = ".mp4"
        content_type = response.headers.get("content-type", "")
        if "audio" in content_type:
            suffix = ".m4a"
        filename = _filename_from_disposition(response.headers.get("content-disposition")) or f"miloco-{event_id}-{device_id}{suffix}"
        target = output_dir / _safe_filename(filename)
        target.write_bytes(response.content)
        return target

    async def iter_events(self) -> AsyncIterator[dict[str, Any]]:
        params = {"token": self.token} if self.token else None
        url = "/api/events/stream"
        if params:
            url = f"{url}?{urlencode(params)}"
        async with self._client(timeout=None) as client:
            async with client.stream("GET", url) as response:
                if not response.is_success:
                    text = (await response.aread()).decode("utf-8", errors="replace")[:300]
                    raise MilocoBusinessError(f"SSE 连接失败: HTTP {response.status_code} {text}")
                event_name = ""
                data_lines: list[str] = []
                async for raw_line in response.aiter_lines():
                    line = raw_line.strip("\r")
                    if not line:
                        if data_lines:
                            data = "\n".join(data_lines)
                            parsed = _parse_sse_data(event_name, data)
                            if parsed is not None:
                                yield parsed
                        event_name = ""
                        data_lines = []
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())


def annotate_miot_control_response(response: dict[str, Any]) -> None:
    inner = response.get("data")
    if not isinstance(inner, dict):
        return

    items: list[dict[str, Any]] = []
    for key in ("results", "properties"):
        value = inner.get(key)
        if isinstance(value, list):
            items.extend(item for item in value if isinstance(item, dict))
    single = inner.get("result")
    if isinstance(single, dict):
        items.append(single)

    failures: list[tuple[int, str]] = []
    total = 0
    for item in items:
        total += 1
        code = item.get("code")
        if isinstance(code, int) and code not in MIOT_OK_CODES:
            message = MIOT_SPEC_CODE_MESSAGES.get(code, "设备侧执行失败")
            item["code_msg"] = message
            failures.append((code, message))

    if not failures:
        return

    reasons = "；".join(dict.fromkeys(message for _, message in failures))
    response["code"] = failures[0][0]
    response["message"] = (
        f"失败：{reasons}" if len(failures) == total else f"部分失败（{len(failures)}/{total}）：{reasons}"
    )


def _parse_sse_data(event_name: str, data: str) -> dict[str, Any] | None:
    if event_name and event_name != "new_event":
        return None
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        payload = {"text": data}
    if isinstance(payload, dict):
        return payload
    return {"payload": payload}


def _filename_from_disposition(value: str | None) -> str | None:
    if not value:
        return None
    for part in value.split(";"):
        part = part.strip()
        if part.lower().startswith("filename="):
            return part.split("=", 1)[1].strip().strip('"')
    return None


def _safe_filename(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in value)
    return cleaned.strip(" .") or "miloco-clip"

