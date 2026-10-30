# -*- coding: utf-8 -*-
"""事件计划只读审计：多计划 / 过期仍 planned，不写库。"""

from __future__ import annotations

import store
from audit_event_plans import run_audit
from conftest import ACCOUNT, PRIVATE

SCOPE_KEY = store.scope_key(ACCOUNT, "private", PRIVATE["user_id"], "")


def _insert(settings, *, content, event_at, valid_until, status="planned"):
    conn = store.open_conn(settings)
    try:
        return store.insert_memory(
            conn,
            {
                "account": ACCOUNT,
                "scope_key": SCOPE_KEY,
                "scope_type": "private",
                "user_id": PRIVATE["user_id"],
                "role": "user",
                "content": content,
                "category": "event",
                "event_at": event_at,
                "valid_until": valid_until,
                "event_status": status,
            },
        )
    finally:
        conn.close()


def test_audit_finds_multiple_plans_without_writing(settings):
    first = _insert(
        settings,
        content="后天我要出去",
        event_at="2026-10-30T00:00:00+08:00",
        valid_until="2026-10-30T23:59:59+08:00",
    )
    second = _insert(
        settings,
        content="大后天我要出去",
        event_at="2026-10-31T00:00:00+08:00",
        valid_until="2026-10-31T23:59:59+08:00",
    )
    report = run_audit(settings, minutes=60)
    assert report["read_only"] is True
    assert report["multiple_plans"] == 1
    finding = report["findings"][0]
    assert finding["kind"] == "multiple_plans"
    assert {item["id"] for item in finding["items"]} == {first, second}

    conn = store.open_conn(settings)
    try:
        statuses = {
            int(row["id"]): row["event_status"]
            for row in conn.execute(
                "SELECT id, event_status FROM memories WHERE id IN (?,?)", (first, second)
            )
        }
    finally:
        conn.close()
    assert statuses == {first: "planned", second: "planned"}


def test_audit_finds_stale_planned(settings):
    memory_id = _insert(
        settings,
        content="上个月说好的事",
        event_at="2026-09-01T00:00:00+08:00",
        valid_until="2026-09-01T23:59:59+08:00",
    )
    report = run_audit(settings, minutes=60)
    assert report["stale_planned"] >= 1
    assert any(
        finding["kind"] == "stale_planned"
        and any(item["id"] == memory_id for item in finding["items"])
        for finding in report["findings"]
    )
