# -*- coding: utf-8 -*-
"""软删之后能不能恢复：恢复必须同时清掉 deleted_at 和取代链。"""

from __future__ import annotations

import dataclasses
import re
import sqlite3

import config
import db_init


def seed(db_path):
    db_init.init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO memories(account, scope_key, scope_type, user_id, role, content,"
        " is_active, deleted_at) VALUES('1','qq:1:private:1','private','1','user',"
        "'后天怎么样',0,'2026-11-01T16:48:58+08:00')"
    )
    memory_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return memory_id


def _client(tmp_path, monkeypatch):
    import app as app_mod
    from fastapi.testclient import TestClient

    db = tmp_path / "r.db"
    memory_id = seed(db)
    real = config.get_settings
    monkeypatch.setattr(config, "get_settings", lambda: dataclasses.replace(real(), db_path=db))
    monkeypatch.setattr(app_mod, "get_settings", config.get_settings)
    monkeypatch.setattr(app_mod.logging_setup, "setup_logging", lambda *a, **k: None)
    monkeypatch.setenv("QQ_MEMORY_CONSOLE_TOKEN", "tok-restore-123456")
    return TestClient(app_mod.app), db, memory_id


def test_restore_single_row(tmp_path, monkeypatch):
    client, db, memory_id = _client(tmp_path, monkeypatch)
    with client:
        client.post("/console/login", data={"token": "tok-restore-123456"}, follow_redirects=True)
        page = client.get("/console/memories")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        assert "恢复" in page.text or "后天怎么样" in page.text
        r = client.post(
            "/console/actions/restore",
            data={"csrf": csrf, "memory_id": str(memory_id)},
            follow_redirects=True,
        )
        assert r.status_code == 200
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT is_active, deleted_at, superseded_by FROM memories WHERE id=?", (memory_id,)
    ).fetchone()
    conn.close()
    assert row[0] == 1
    assert row[1] is None
    assert row[2] is None


def test_bulk_restore_clears_deleted_at(tmp_path, monkeypatch):
    client, db, memory_id = _client(tmp_path, monkeypatch)
    with client:
        client.post("/console/login", data={"token": "tok-restore-123456"}, follow_redirects=True)
        page = client.get("/console/memories")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        r = client.post(
            "/console/actions/bulk",
            data={"csrf": csrf, "action": "activate", "memory_ids": str(memory_id), "confirm": "yes"},
            follow_redirects=True,
        )
        assert r.status_code == 200
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT is_active, deleted_at FROM memories WHERE id=?", (memory_id,)).fetchone()
    conn.close()
    assert row[0] == 1
    assert row[1] is None
