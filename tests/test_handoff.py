# -*- coding: utf-8 -*-
"""8.1：续接包预算、条数、降级链。"""

from __future__ import annotations

import store
from handoff import HandoffBuilder
from timeutil import iso_now


def _seed_turns(settings, count: int = 30) -> str:
    key = store.scope_key("10001", "private", "20002", "")
    conn = store.open_conn(settings)
    try:
        for index in range(1, count + 1):
            store.insert_turn(
                conn,
                {
                    "account": "10001",
                    "scope_key": key,
                    "scope_type": "private",
                    "session_id": "s1",
                    "generation": 0,
                    "seq": index,
                    "role": "user" if index % 2 else "assistant",
                    "speaker_name": "她" if index % 2 else "我",
                    "content": f"第{index}条消息：" + "聊点具体的事情" * 8,
                    "happened_at": "2026-10-24T21:00:00+08:00",
                    "created_at": iso_now(),
                },
            )
        conn.commit()
    finally:
        conn.close()
    return key


def _payload() -> dict:
    return {"account": "10001", "scope": {"type": "private", "user_id": "20002"}, "limit": 20}


def test_packet_budget_and_recent(settings):
    _seed_turns(settings)
    builder = HandoffBuilder(settings)
    result = builder.build(_payload())
    assert result.packet is not None
    assert result.chars <= settings.handoff_max_chars
    assert len(result.packet.recent) <= settings.handoff_recent
    assert "【上一段对话（接着聊，不是新对话）】" in result.block
    assert "【上一段对话结束】" in result.block


def test_no_data_returns_null(settings):
    builder = HandoffBuilder(settings)
    result = builder.build(
        {"account": "10001", "scope": {"type": "private", "user_id": "999"}, "limit": 20}
    )
    assert result.packet is None and result.degraded is False


def test_fallback_recent_on_assemble_error(settings):
    _seed_turns(settings)
    builder = HandoffBuilder(settings)

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    builder._assemble = boom
    result = builder.build(_payload())
    assert result.degraded is True
    assert result.reason == "fallback_recent"
    assert result.chars <= settings.handoff_max_chars
    assert "【上一段对话" in result.block
