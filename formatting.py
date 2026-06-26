from __future__ import annotations

from typing import Any


def summarize_home(data: dict[str, Any] | None, *, limit: int = 40) -> str:
    if not isinstance(data, dict):
        return "miloco home 信息不可用。"
    home_name = data.get("home_name") or data.get("name") or "未命名家庭"
    rooms = _collect_rooms(data)
    devices = _as_list(data.get("devices"))
    scenes = _as_list(data.get("scenes"))
    persons = _as_list(data.get("persons"))

    lines = [f"miloco 当前家庭: {home_name}"]
    if rooms:
        lines.append("房间目录:")
        for item in rooms[:limit]:
            room_id = item.get("room_id") or item.get("id") or "-"
            room_name = item.get("room_name") or item.get("name") or "未命名房间"
            lines.append(f"- {room_name} | room_id={room_id}")
        if len(rooms) > limit:
            lines.append(f"- ... 还有 {len(rooms) - limit} 个房间")
    if devices:
        lines.append("设备目录:")
        for item in devices[:limit]:
            if not isinstance(item, dict):
                continue
            did = item.get("did") or item.get("id") or "-"
            name = item.get("name") or item.get("device_name") or "未命名设备"
            room = item.get("room") or item.get("room_name") or "-"
            online = "online" if item.get("online") else "offline"
            category = item.get("category") or item.get("model") or "-"
            lines.append(f"- {name} | did={did} | room={room} | {category} | {online}")
        if len(devices) > limit:
            lines.append(f"- ... 还有 {len(devices) - limit} 个设备")
    if scenes:
        lines.append("场景:")
        for item in scenes[:20]:
            if isinstance(item, dict):
                sid = item.get("scene_id") or item.get("id") or "-"
                name = item.get("scene_name") or item.get("name") or "未命名场景"
                lines.append(f"- {name} | scene_id={sid}")
    if persons:
        names = []
        for person in persons[:20]:
            if isinstance(person, dict):
                names.append(str(person.get("name") or person.get("id") or "未命名成员"))
        if names:
            lines.append("家庭成员: " + "、".join(names))
    return "\n".join(lines)


def format_event(event: dict[str, Any]) -> str:
    event_id = event.get("event_id") or event.get("id") or "-"
    text = str(event.get("text") or event.get("message") or "miloco 感知到新事件")
    device_ids = event.get("device_ids")
    if not isinstance(device_ids, list):
        device_ids = []
    flags = []
    if event.get("has_rule_hit"):
        flags.append("规则命中")
    if event.get("has_suggestion"):
        flags.append("主动建议")
    if event.get("has_asr"):
        flags.append("语音")
    if event.get("snapshot_count"):
        flags.append(f"证据片段 {event.get('snapshot_count')}")
    flag_text = f" [{' / '.join(flags)}]" if flags else ""
    devices = f"\n设备: {', '.join(map(str, device_ids))}" if device_ids else ""
    clip = ""
    if event.get("clip_kind") and device_ids:
        clip = f"\n可按需获取证据: event_id={event_id}, device_id={device_ids[0]}, kind={event.get('clip_kind')}"
    return f"[miloco 家庭事件{flag_text}]\n{text}\nevent_id: {event_id}{devices}{clip}"


def compact_json(data: Any, *, max_chars: int = 4000) -> str:
    import json

    text = json.dumps(data, ensure_ascii=False, indent=2)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... truncated ..."


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _collect_rooms(data: dict[str, Any]) -> list[dict[str, Any]]:
    rooms = _as_list(data.get("rooms"))
    if rooms:
        return [item for item in rooms if isinstance(item, dict)]

    homes = _as_list(data.get("homes"))
    collected: list[dict[str, Any]] = []
    for home in homes:
        if not isinstance(home, dict):
            continue
        room_list = home.get("room_list")
        if isinstance(room_list, dict):
            collected.extend(item for item in room_list.values() if isinstance(item, dict))
        else:
            collected.extend(item for item in _as_list(room_list) if isinstance(item, dict))
    return collected
