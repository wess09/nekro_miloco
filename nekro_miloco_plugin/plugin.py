from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from pydantic import Field

from nekro_agent.api import message
from nekro_agent.api.plugin import (
    Arg,
    CmdCtl,
    CommandExecutionContext,
    CommandPermission,
    CommandResponse,
    ConfigBase,
    ExtraField,
    NekroPlugin,
    SandboxMethodType,
)
from nekro_agent.api.schemas import AgentCtx

from .client import MilocoBusinessError, MilocoClient, MilocoClientError
from .formatting import compact_json, format_event, summarize_home
from .models import ControlConfirmMode, EventState
from .state import (
    add_bound_chat,
    catalog_cache,
    create_pending_operation,
    get_bound_chats,
    get_push_enabled,
    pop_pending_operation,
    remove_bound_chat,
    set_push_enabled,
)


plugin = NekroPlugin(
    name="Nekro Miloco Bridge",
    module_name="miloco",
    description="通过已部署的 miloco-backend 桥接小米智能家居、感知事件和只读身份查询。",
    version="0.1.0",
    author="nekro_miloco",
    url="https://local/nekro_miloco_plugin",
    allow_sleep=False,
    sleep_brief="用于控制小米智能家居、查询 miloco 感知事件和获取按需证据。",
)


@plugin.mount_config()
class MilocoBridgeConfig(ConfigBase):
    MILOCO_BASE_URL: str = Field(
        default="http://127.0.0.1:1810",
        title="miloco-backend 地址",
        json_schema_extra=ExtraField(description="例如 http://127.0.0.1:8000").model_dump(),
    )
    MILOCO_TOKEN: str = Field(
        default="",
        title="miloco 服务 Token",
        json_schema_extra=ExtraField(description="对应 miloco HTTP Bearer Token").model_dump(),
    )
    REQUEST_TIMEOUT_SECONDS: float = Field(default=20.0, title="请求超时秒数")
    VERIFY_TLS: bool = Field(default=True, title="验证 HTTPS 证书")
    CATALOG_CACHE_TTL_SECONDS: int = Field(default=60, title="家庭目录缓存秒数")
    CATALOG_LIMIT: int = Field(default=40, title="提示注入设备上限")
    CONTROL_CONFIRM_MODE: ControlConfirmMode = Field(
        default="dangerous",
        title="控制确认策略",
        description="always=所有控制均确认；dangerous=高风险控制确认；never=不确认",
    )
    CONFIRM_TTL_SECONDS: int = Field(default=120, title="确认口令有效秒数")
    EVENT_PUSH_ENABLED: bool = Field(default=True, title="启用 miloco SSE 事件推送")
    CONFIGURED_TARGET_CHAT_KEYS: str = Field(
        default="",
        title="固定推送 chat_key 列表",
        description="多个 chat_key 用逗号或换行分隔；也可用命令绑定当前聊天。",
    )
    EVENT_TRIGGER_AGENT: bool = Field(default=False, title="事件推送时触发 Agent")
    EVENT_IMPORTANT_ONLY_TRIGGER: bool = Field(default=True, title="仅重要事件触发 Agent")
    EVENT_RECONNECT_SECONDS: float = Field(default=5.0, title="SSE 断线重连间隔")


config = plugin.get_config(MilocoBridgeConfig)
event_state = EventState()


def _client() -> MilocoClient:
    return MilocoClient(
        config.MILOCO_BASE_URL,
        config.MILOCO_TOKEN,
        timeout=config.REQUEST_TIMEOUT_SECONDS,
        verify_tls=config.VERIFY_TLS,
    )


async def _miloco_call(coro: Any) -> Any:
    try:
        return await coro
    except (MilocoClientError, MilocoBusinessError) as exc:
        raise RuntimeError(str(exc)) from exc


async def _get_home_cached(refresh: bool = False) -> dict[str, Any]:
    if not refresh and catalog_cache.fresh(config.CATALOG_CACHE_TTL_SECONDS):
        return catalog_cache.data or {}
    data = await _miloco_call(_client().get("/api/miot/home", params={"refresh": str(refresh).lower()}))
    catalog_cache.data = data if isinstance(data, dict) else {}
    import time

    catalog_cache.fetched_at = time.time()
    return catalog_cache.data


def _requires_confirmation(operation: str, summary: str) -> bool:
    if config.CONTROL_CONFIRM_MODE == "never":
        return False
    if config.CONTROL_CONFIRM_MODE == "always":
        return True
    text = f"{operation} {summary}".lower()
    dangerous_words = (
        "door",
        "lock",
        "unlock",
        "garage",
        "curtain",
        "heater",
        "oven",
        "gas",
        "scene",
        "门",
        "锁",
        "窗帘",
        "热水",
        "取暖",
        "燃气",
        "场景",
    )
    return any(word in text for word in dangerous_words)


async def _maybe_confirm(
    *,
    operation: str,
    summary: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> str | None:
    if not _requires_confirmation(operation, summary):
        return None
    pending = create_pending_operation(
        operation=operation,
        summary=summary,
        method=method,
        path=path,
        body=body,
        ttl_seconds=config.CONFIRM_TTL_SECONDS,
    )
    return (
        f"需要确认后执行: {summary}\n"
        f"确认口令: {pending.token}\n"
        f"请调用 confirm_miloco_control(token='{pending.token}') 或使用 /miloco.confirm {pending.token}。"
    )


async def _target_chats() -> list[str]:
    configured = []
    for part in config.CONFIGURED_TARGET_CHAT_KEYS.replace("\n", ",").split(","):
        part = part.strip()
        if part:
            configured.append(part)
    bound = await get_bound_chats(plugin.store)
    return sorted(dict.fromkeys(configured + bound))


def _event_should_trigger_agent(event: dict[str, Any]) -> bool:
    if not config.EVENT_TRIGGER_AGENT:
        return False
    if not config.EVENT_IMPORTANT_ONLY_TRIGGER:
        return True
    return bool(event.get("has_rule_hit") or event.get("has_suggestion") or event.get("has_asr"))


async def _send_event_to_targets(event: dict[str, Any]) -> None:
    event_id = str(event.get("event_id") or event.get("id") or "")
    if event_id:
        if event_id in event_state.last_event_ids:
            return
        event_state.last_event_ids.add(event_id)
        if len(event_state.last_event_ids) > 200:
            event_state.last_event_ids = set(list(event_state.last_event_ids)[-100:])

    text = format_event(event)
    trigger = _event_should_trigger_agent(event)
    for chat_key in await _target_chats():
        try:
            ctx = await AgentCtx.create_by_chat_key(chat_key)
            await message.push_system(chat_key=chat_key, message=text, ctx=ctx, trigger_agent=trigger)
        except Exception as exc:
            plugin.logger.warning("miloco 事件推送失败 chat_key=%s: %s", chat_key, exc)


async def _event_loop() -> None:
    event_state.running = True
    while True:
        enabled = config.EVENT_PUSH_ENABLED and await get_push_enabled(plugin.store, True)
        if not enabled:
            await asyncio.sleep(config.EVENT_RECONNECT_SECONDS)
            continue
        try:
            async for event in _client().iter_events():
                await _send_event_to_targets(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            event_state.last_error = str(exc)
            plugin.logger.warning("miloco SSE 订阅中断: %s", exc)
            await asyncio.sleep(config.EVENT_RECONNECT_SECONDS)


def _ensure_event_task() -> None:
    if event_state.task and not event_state.task.done():
        return
    event_state.task = asyncio.create_task(_event_loop())


@plugin.mount_init_method()
async def init_plugin() -> None:
    if config.EVENT_PUSH_ENABLED:
        _ensure_event_task()


@plugin.mount_cleanup_method()
async def cleanup_plugin() -> None:
    if event_state.task and not event_state.task.done():
        event_state.task.cancel()
        try:
            await event_state.task
        except asyncio.CancelledError:
            pass
    event_state.task = None
    event_state.running = False


@plugin.on_enabled()
async def on_enabled() -> None:
    if config.EVENT_PUSH_ENABLED:
        _ensure_event_task()


@plugin.on_disabled()
async def on_disabled() -> None:
    await cleanup_plugin()


@plugin.mount_prompt_inject_method("miloco_home_context", "注入小米家庭设备目录和安全边界")
async def inject_miloco_context(_ctx: AgentCtx) -> str:
    try:
        home = await _get_home_cached(refresh=False)
    except Exception as exc:
        return f"Miloco bridge: 当前无法连接 miloco-backend ({exc})。"

    push_enabled = await get_push_enabled(plugin.store, True)
    chats = await _target_chats()
    return (
        "Miloco bridge rules:\n"
        "- 通过 miloco-backend 控制小米智能家居；不要请求或转发实时摄像头直播。\n"
        "- 感知/身份识别由 miloco 完成，Nekro 只使用结构化结果；证据片段仅按需获取。\n"
        f"- 事件推送: {'enabled' if push_enabled else 'disabled'}, targets={len(chats)}。\n\n"
        + summarize_home(home, limit=config.CATALOG_LIMIT)
    )


@plugin.mount_sandbox_method(SandboxMethodType.TOOL, "get_miloco_status", "查询 miloco 和 MiOT 绑定状态")
async def get_miloco_status(_ctx: AgentCtx) -> str:
    data = await _miloco_call(_client().get("/api/miot/status"))
    return compact_json(data)


@plugin.mount_sandbox_method(SandboxMethodType.TOOL, "get_miloco_home", "查询 miloco 家庭、房间、设备、场景和成员概览")
async def get_miloco_home(_ctx: AgentCtx, refresh: bool = False) -> str:
    data = await _get_home_cached(refresh=refresh)
    return compact_json(data)


@plugin.mount_sandbox_method(SandboxMethodType.TOOL, "list_miloco_devices", "列出小米 IoT 设备")
async def list_miloco_devices(_ctx: AgentCtx) -> str:
    data = await _miloco_call(_client().get("/api/miot/device_list"))
    return compact_json(data)


@plugin.mount_sandbox_method(SandboxMethodType.TOOL, "get_miloco_device_spec", "查询设备 MiOT spec")
async def get_miloco_device_spec(_ctx: AgentCtx, did: str) -> str:
    data = await _miloco_call(_client().get(f"/api/miot/devices/{did}/spec"))
    return compact_json(data)


@plugin.mount_sandbox_method(SandboxMethodType.TOOL, "get_miloco_device_status", "查询设备属性状态")
async def get_miloco_device_status(_ctx: AgentCtx, did: str, iid: str = "") -> str:
    params = {"iid": iid} if iid else None
    data = await _miloco_call(_client().get(f"/api/miot/devices/{did}/status", params=params))
    return compact_json(data)


@plugin.mount_sandbox_method(SandboxMethodType.TOOL, "set_miloco_property", "设置单个设备属性，必要时返回确认口令")
async def set_miloco_property(_ctx: AgentCtx, did: str, iid: str, value: Any) -> str:
    body = {"type": "set_property", "iid": iid, "value": value}
    summary = f"set_property did={did} iid={iid} value={value!r}"
    confirm = await _maybe_confirm(
        operation="set_property",
        summary=summary,
        method="POST",
        path=f"/api/miot/devices/{did}/control",
        body=body,
    )
    if confirm:
        return confirm
    data = await _miloco_call(_client().control_device(did, body))
    return "已执行设备属性设置:\n" + compact_json(data)


@plugin.mount_sandbox_method(SandboxMethodType.TOOL, "set_miloco_properties", "批量设置设备属性，必要时返回确认口令")
async def set_miloco_properties(_ctx: AgentCtx, did: str, properties: list[dict[str, Any]]) -> str:
    body = {"type": "set_properties", "properties": properties}
    summary = f"set_properties did={did} count={len(properties)}"
    confirm = await _maybe_confirm(
        operation="set_properties",
        summary=summary,
        method="POST",
        path=f"/api/miot/devices/{did}/control",
        body=body,
    )
    if confirm:
        return confirm
    data = await _miloco_call(_client().control_device(did, body))
    return "已执行设备批量属性设置:\n" + compact_json(data)


@plugin.mount_sandbox_method(SandboxMethodType.TOOL, "call_miloco_action", "调用设备 action，必要时返回确认口令")
async def call_miloco_action(_ctx: AgentCtx, did: str, iid: str, params: list[Any] | None = None) -> str:
    body = {"type": "call_action", "iid": iid, "params": params or []}
    summary = f"call_action did={did} iid={iid} params={params or []!r}"
    confirm = await _maybe_confirm(
        operation="call_action",
        summary=summary,
        method="POST",
        path=f"/api/miot/devices/{did}/control",
        body=body,
    )
    if confirm:
        return confirm
    data = await _miloco_call(_client().control_device(did, body))
    return "已执行设备动作:\n" + compact_json(data)


@plugin.mount_sandbox_method(SandboxMethodType.TOOL, "trigger_miloco_scene", "触发米家手动场景，默认需要确认")
async def trigger_miloco_scene(_ctx: AgentCtx, scene_id: str) -> str:
    summary = f"trigger_scene scene_id={scene_id}"
    confirm = await _maybe_confirm(
        operation="trigger_scene",
        summary=summary,
        method="POST",
        path=f"/api/miot/scenes/{scene_id}/trigger",
        body={},
    )
    if confirm:
        return confirm
    data = await _miloco_call(_client().post(f"/api/miot/scenes/{scene_id}/trigger", {}))
    return "已触发场景:\n" + compact_json(data)


@plugin.mount_sandbox_method(SandboxMethodType.TOOL, "confirm_miloco_control", "确认并执行待确认的小米家居控制")
async def confirm_miloco_control(_ctx: AgentCtx, token: str) -> str:
    pending = pop_pending_operation(token)
    if pending is None:
        return "确认口令无效或已过期。"
    if pending.expired:
        return "确认口令已过期，未执行。"
    if pending.path.endswith("/control"):
        did = pending.path.split("/devices/", 1)[1].split("/control", 1)[0]
        data = await _miloco_call(_client().control_device(did, pending.body or {}))
    else:
        data = await _miloco_call(_client().request(pending.method, pending.path, json_body=pending.body or {}))
    return f"已确认并执行: {pending.summary}\n{compact_json(data)}"


@plugin.mount_sandbox_method(SandboxMethodType.TOOL, "get_miloco_perception_status", "查询 miloco 感知引擎状态")
async def get_miloco_perception_status(_ctx: AgentCtx) -> str:
    data = await _miloco_call(_client().get("/api/perception/engine/status"))
    return compact_json(data)


@plugin.mount_sandbox_method(SandboxMethodType.TOOL, "start_miloco_perception", "启动 miloco 实时感知引擎")
async def start_miloco_perception(_ctx: AgentCtx) -> str:
    data = await _miloco_call(_client().post("/api/perception/engine/start", {}))
    return "已请求启动感知引擎:\n" + compact_json(data)


@plugin.mount_sandbox_method(SandboxMethodType.TOOL, "stop_miloco_perception", "停止 miloco 实时感知引擎")
async def stop_miloco_perception(_ctx: AgentCtx) -> str:
    data = await _miloco_call(_client().post("/api/perception/engine/stop", {}))
    return "已请求停止感知引擎:\n" + compact_json(data)


@plugin.mount_sandbox_method(SandboxMethodType.TOOL, "ask_miloco_perception", "对指定感知设备发起一次主动感知提问")
async def ask_miloco_perception(_ctx: AgentCtx, sources: list[str], query: str) -> str:
    data = await _miloco_call(_client().post("/api/perception/perceive", {"sources": sources, "query": query}))
    return compact_json(data)


@plugin.mount_sandbox_method(SandboxMethodType.TOOL, "list_miloco_events", "查询 miloco 感知事件列表")
async def list_miloco_events(_ctx: AgentCtx, limit: int = 20, since: int = 0, offset: int = 0) -> str:
    data = await _miloco_call(
        _client().get("/api/events", params={"limit": limit, "since": since, "offset": offset})
    )
    return compact_json(data)


@plugin.mount_sandbox_method(SandboxMethodType.BEHAVIOR, "send_miloco_event_clip", "按需获取并发送 miloco 事件证据片段")
async def send_miloco_event_clip(_ctx: AgentCtx, event_id: str, device_id: str) -> str:
    output_dir = Path(plugin.get_plugin_data_dir()) / "clips"
    file_path = await _miloco_call(_client().download_event_clip(event_id, device_id, output_dir))
    sandbox_path = await _ctx.fs.mixed_forward_file(file_path)
    await _ctx.send_file(sandbox_path)
    return f"已发送事件证据片段: event_id={event_id}, device_id={device_id}"


@plugin.mount_sandbox_method(SandboxMethodType.TOOL, "list_miloco_persons", "只读查询 miloco 家庭成员列表")
async def list_miloco_persons(_ctx: AgentCtx) -> str:
    data = await _miloco_call(_client().get("/api/identity/persons"))
    return compact_json(data)


@plugin.mount_sandbox_method(SandboxMethodType.TOOL, "get_miloco_person", "从人员列表中按 person_id 查询成员详情")
async def get_miloco_person(_ctx: AgentCtx, person_id: str) -> str:
    persons = await _miloco_call(_client().get("/api/identity/persons"))
    if not isinstance(persons, list):
        return compact_json(persons)
    for person in persons:
        if isinstance(person, dict) and str(person.get("id")) == person_id:
            return compact_json(person)
    return f"未找到 person_id={person_id}"


@plugin.mount_sandbox_method(SandboxMethodType.TOOL, "list_miloco_rules", "只读查询 miloco 规则列表")
async def list_miloco_rules(_ctx: AgentCtx, enabled_only: bool = False) -> str:
    data = await _miloco_call(_client().get("/api/rules", params={"enabled_only": str(enabled_only).lower()}))
    return compact_json(data)


@plugin.mount_sandbox_method(SandboxMethodType.TOOL, "list_miloco_rule_logs", "只读查询 miloco 规则日志")
async def list_miloco_rule_logs(_ctx: AgentCtx, limit: int = 20, since: str = "") -> str:
    params: dict[str, Any] = {"limit": limit}
    if since:
        params["since"] = since
    data = await _miloco_call(_client().get("/api/rules/logs", params=params))
    return compact_json(data)


@plugin.mount_sandbox_method(SandboxMethodType.TOOL, "get_miloco_task_record", "只读查询 miloco 任务记录")
async def get_miloco_task_record(_ctx: AgentCtx, task_id: str) -> str:
    data = await _miloco_call(_client().get(f"/api/tasks/{task_id}/record"))
    return compact_json(data)


miloco_cmd = plugin.mount_command_group(
    name="miloco",
    description="Miloco 桥接插件管理命令",
    permission=CommandPermission.ADVANCED,
    category="智能家居",
    tags=["miloco", "iot", "xiaomi"],
)


@miloco_cmd.command(name="bind_chat", description="绑定当前聊天为 miloco 事件接收端")
async def cmd_bind_chat(context: CommandExecutionContext) -> CommandResponse:
    chats = await add_bound_chat(plugin.store, context.chat_key)
    return CmdCtl.success(f"已绑定当前聊天。当前绑定数: {len(chats)}")


@miloco_cmd.command(name="unbind_chat", description="取消当前聊天的 miloco 事件绑定")
async def cmd_unbind_chat(context: CommandExecutionContext) -> CommandResponse:
    chats = await remove_bound_chat(plugin.store, context.chat_key)
    return CmdCtl.success(f"已取消当前聊天绑定。剩余绑定数: {len(chats)}")


@miloco_cmd.command(name="event_push_on", description="开启 miloco 事件推送")
async def cmd_event_push_on(context: CommandExecutionContext) -> CommandResponse:
    await set_push_enabled(plugin.store, True)
    _ensure_event_task()
    return CmdCtl.success("miloco 事件推送已开启。")


@miloco_cmd.command(name="event_push_off", description="关闭 miloco 事件推送")
async def cmd_event_push_off(context: CommandExecutionContext) -> CommandResponse:
    await set_push_enabled(plugin.store, False)
    return CmdCtl.success("miloco 事件推送已关闭。")


@miloco_cmd.command(name="status", description="检查 miloco 桥接状态")
async def cmd_status(context: CommandExecutionContext) -> CommandResponse:
    try:
        status = await _client().get("/api/miot/status")
    except Exception as exc:
        return CmdCtl.failed(f"miloco 连接失败: {exc}")
    chats = await _target_chats()
    push_enabled = await get_push_enabled(plugin.store, True)
    running = bool(event_state.task and not event_state.task.done())
    return CmdCtl.success(
        f"miloco 可连接。事件推送={'on' if push_enabled else 'off'}，SSE任务={'running' if running else 'stopped'}，目标聊天={len(chats)}。",
        data={"miot_status": status, "event_error": event_state.last_error},
    )


@miloco_cmd.command(name="refresh", description="刷新 miloco 家庭目录缓存")
async def cmd_refresh(context: CommandExecutionContext) -> CommandResponse:
    try:
        home = await _get_home_cached(refresh=True)
    except Exception as exc:
        return CmdCtl.failed(f"刷新失败: {exc}")
    return CmdCtl.success("miloco 家庭目录已刷新。", data={"home": home})


@miloco_cmd.command(name="confirm", description="确认并执行待确认控制", usage="miloco.confirm <token>")
async def cmd_confirm(
    context: CommandExecutionContext,
    token: str = Arg("确认口令", positional=True),
) -> CommandResponse:
    pending = pop_pending_operation(token)
    if pending is None or pending.expired:
        return CmdCtl.failed("确认口令无效或已过期。")
    try:
        if pending.path.endswith("/control"):
            did = pending.path.split("/devices/", 1)[1].split("/control", 1)[0]
            data = await _client().control_device(did, pending.body or {})
        else:
            data = await _client().request(pending.method, pending.path, json_body=pending.body or {})
    except Exception as exc:
        return CmdCtl.failed(f"执行失败: {exc}")
    return CmdCtl.success(f"已确认并执行: {pending.summary}", data={"result": data})
