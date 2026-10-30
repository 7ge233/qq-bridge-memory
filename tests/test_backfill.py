# -*- coding: utf-8 -*-
"""v2.3 历史事件回填：dry-run / apply / 幂等 / 不碰 fact 与 assistant。"""

from __future__ import annotations

import store
from backfill_temporal import run_backfill


def _insert(settings, *, content, role="user", category="event", happened_at=None, fact_key=None):
    conn = store.open_conn(settings)
    try:
        return store.insert_memory(
            conn,
            {
                "account": "10001",
                "scope_key": store.scope_key("10001", "private", "20002", ""),
                "scope_type": "private",
                "user_id": "20002",
                "role": role,
                "content": content,
                "category": category,
                "happened_at": happened_at or "2026-10-20T21:00:00+08:00",
                "fact_key": fact_key,
            },
        )
    finally:
        conn.close()


def _event_fields(settings, ids):
    conn = store.open_conn(settings)
    try:
        rows = {}
        for memory_id in ids:
            row = conn.execute(
                "SELECT event_at, valid_until, event_status FROM memories WHERE id=?",
                (memory_id,),
            ).fetchone()
            rows[memory_id] = dict(row)
        return rows
    finally:
        conn.close()


def test_dry_run_does_not_write(settings):
    event_id = _insert(settings, content="明天我要出去玩")
    ambiguous_id = _insert(settings, content="过两天再说")
    fact_id = _insert(settings, content="她身高 165", category="fact", fact_key="她.身高")
    assistant_id = _insert(settings, content="明天见呀", role="assistant")

    report = run_backfill(settings, apply=False)
    assert report["dry_run"] is True
    assert report["candidates"] == 2  # 只算 user + event + event_at null
    assert report["parsed"] == 1
    assert report["ambiguous"] == 1
    assert report["updated"] == 0

    fields = _event_fields(settings, [event_id, ambiguous_id, fact_id, assistant_id])
    assert all(row["event_at"] is None for row in fields.values())


def test_apply_fills_only_confident_events(settings):
    event_id = _insert(settings, content="明天我要出去玩")
    ambiguous_id = _insert(settings, content="过两天再说")
    fact_id = _insert(settings, content="她身高 165", category="fact", fact_key="她.身高")
    assistant_id = _insert(settings, content="明天见呀", role="assistant")

    report = run_backfill(settings, apply=True, backup=False)
    assert report["updated"] == 1

    fields = _event_fields(settings, [event_id, ambiguous_id, fact_id, assistant_id])
    assert fields[event_id]["event_at"] is not None
    assert fields[event_id]["event_status"] == "planned"
    assert fields[ambiguous_id]["event_at"] is None
    assert fields[fact_id]["event_at"] is None
    assert fields[assistant_id]["event_at"] is None

    second = run_backfill(settings, apply=True, backup=False)
    # 模糊记录会继续留在候选里（没有可写的状态位），但不会产生重复更新
    assert second["parsed"] == 0 and second["updated"] == 0


def test_limit(settings):
    for index in range(3):
        _insert(settings, content=f"明天第{index}次出去玩")
    report = run_backfill(settings, apply=False, limit=1)
    assert report["candidates"] == 1
    assert report["parsed"] == 1
