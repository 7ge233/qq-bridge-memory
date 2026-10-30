# -*- coding: utf-8 -*-
"""6.6：VACUUM INTO 备份与保留策略。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import store
from backup import _prune, backup_now


def test_backup_and_integrity(settings):
    conn = store.open_conn(settings)
    try:
        store.insert_memory(
            conn,
            {
                "account": "10001",
                "scope_key": store.scope_key("10001", "private", "20002", ""),
                "scope_type": "private",
                "user_id": "20002",
                "role": "user",
                "content": "备份测试",
                "tokens": "备份 测试",
            },
        )
        conn.commit()
    finally:
        conn.close()

    target = backup_now(settings, keep=7)
    assert target is not None and target.exists()
    check = sqlite3.connect(str(target))
    try:
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        count = check.execute("SELECT count(*) FROM memories").fetchone()[0]
        assert count >= 1
    finally:
        check.close()


def test_prune_keeps_latest(settings):
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    for day in range(1, 10):
        (settings.backup_dir / f"qq-memory-2026010{day}.db").write_bytes(b"x")
    removed = _prune(settings.backup_dir, keep=7)
    remaining = sorted(p.name for p in settings.backup_dir.glob("qq-memory-*.db"))
    assert len(remaining) == 7
    assert len(removed) == 2
    assert remaining[-1] == "qq-memory-20260109.db"
