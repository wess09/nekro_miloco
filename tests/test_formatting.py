from __future__ import annotations

import importlib


formatting_mod = importlib.import_module("nekro_miloco_plugin.formatting")


def test_format_event_mentions_clip_on_demand() -> None:
    text = formatting_mod.format_event(
        {
            "event_id": "evt1",
            "text": "客厅有人经过",
            "device_ids": ["cam1"],
            "snapshot_count": 1,
            "clip_kind": "mp4",
        }
    )

    assert "客厅有人经过" in text
    assert "按需获取证据" in text
    assert "cam1" in text


def test_summarize_home_compacts_devices() -> None:
    text = formatting_mod.summarize_home(
        {
            "home_name": "家",
            "devices": [{"did": "1", "name": "灯", "room": "客厅", "online": True, "category": "light"}],
            "scenes": [{"scene_id": "s1", "scene_name": "回家"}],
            "persons": [{"name": "Alice"}],
        }
    )

    assert "家" in text
    assert "灯" in text
    assert "回家" in text
    assert "Alice" in text

