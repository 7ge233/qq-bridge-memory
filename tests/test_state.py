# -*- coding: utf-8 -*-
"""状态合并：state 是唯一真相，is_active/deleted_at 由它派生并保持一致。"""

from __future__ import annotations

import dataclasses
import re
import sqlite3

import config
import db_init
import store


def _db(tmp_path):
    db = tmp_path / "state.db"
    db_init.init_db(db)
    return db


def _insert(db, content, *, is_active=1, deleted_at=None, state="active"):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO memories(account, scope_key, scope_type, user_id, role, content,"
        " is_active, deleted_at, state) VALUES('1','s','private','1','user',?,?,?,?)",
        (content, is_active, deleted_at, state),
    )
    conn.commit()
    conn.close()


def test_set_state_keeps_legacy_columns_in_sync(tmp_path):
    db = _db(tmp_path)
    _insert(db, "一条普通记忆")
    conn = store.open_conn(dataclasses.replace(config.get_settings(), db_path=db))
    try:
        store.set_state(conn, 1, store.STATE_INACTIVE)
        conn.commit()
        row = conn.execute("SELECT state, is_active, deleted_at FROM memories WHERE id=1").fetchone()
        assert tuple(row) == ("inactive", 0, None)

        store.set_state(conn, 1, store.STATE_DELETED)
        conn.commit()
        row = conn.execute("SELECT state, is_active, deleted_at FROM memories WHERE id=1").fetchone()
        assert row["state"] == "deleted"
        assert row["is_active"] == 0
        assert row["deleted_at"]          # 软删要留下时间

        store.set_state(conn, 1, store.STATE_ACTIVE)
        conn.commit()
        row = conn.execute("SELECT state, is_active, deleted_at FROM memories WHERE id=1").fetchone()
        assert tuple(row) == ("active", 1, None)
    finally:
        conn.close()


def test_resync_fixes_rows_changed_by_legacy_code(tmp_path):
    db = _db(tmp_path)
    _insert(db, "旧代码改过的行")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE memories SET is_active=0, state='active'")   # 绕过 set_state
    conn.commit()
    conn.close()

    conn = store.open_conn(dataclasses.replace(config.get_settings(), db_path=db))
    try:
        assert store.resync_state(conn) == 1
        conn.commit()
        assert conn.execute("SELECT state FROM memories").fetchone()[0] == "inactive"
        assert store.resync_state(conn) == 0      # 已经一致就不再动
    finally:
        conn.close()


def test_console_state_filter(tmp_path, monkeypatch):
    import app as app_mod
    from fastapi.testclient import TestClient

    db = _db(tmp_path)
    _insert(db, "活着的记忆")
    _insert(db, "停用的记忆", is_active=0, state="inactive")
    _insert(db, "删掉的记忆", is_active=0, deleted_at="2026-11-01T10:00:00+08:00", state="deleted")

    real = config.get_settings
    monkeypatch.setattr(config, "get_settings", lambda: dataclasses.replace(real(), db_path=db))
    monkeypatch.setattr(app_mod, "get_settings", config.get_settings)
    monkeypatch.setattr(app_mod.logging_setup, "setup_logging", lambda *a, **k: None)
    monkeypatch.setenv("QQ_MEMORY_CONSOLE_TOKEN", "tok-state-123456")

    with TestClient(app_mod.app) as client:
        client.post("/console/login", data={"token": "tok-state-123456"}, follow_redirects=True)
        page = client.get("/console/memories?is_active=deleted").text
        assert "删掉的记忆" in page
        assert "活着的记忆" not in page
        page_inactive = client.get("/console/memories?is_active=inactive").text
        assert "停用的记忆" in page_inactive
        assert "删掉的记忆" not in page_inactive
        page_all = client.get("/console/memories?is_active=all").text
        for text in ("活着的记忆", "停用的记忆", "删掉的记忆"):
            assert text in page_all
        # 状态下拉里有按 state 的选项
        for value in ("active", "inactive", "deleted"):
            assert f'value="{value}"' in page_all
