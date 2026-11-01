# -*- coding: utf-8 -*-
"""批量操作：默认不预选操作、要勾确认框、命中 0 条要说明原因。"""

from __future__ import annotations

import dataclasses
import re
import sqlite3

import config
import db_init


def _client(tmp_path, monkeypatch):
    import app as app_mod
    from fastapi.testclient import TestClient

    db = tmp_path / "bulk.db"
    db_init.init_db(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO memories(account, scope_key, scope_type, user_id, role, content)"
        " VALUES('1','qq:1:private:1','private','1','user','批量测试')"
    )
    conn.commit()
    conn.close()
    real = config.get_settings
    monkeypatch.setattr(config, "get_settings", lambda: dataclasses.replace(real(), db_path=db))
    monkeypatch.setattr(app_mod, "get_settings", config.get_settings)
    monkeypatch.setattr(app_mod.logging_setup, "setup_logging", lambda *a, **k: None)
    monkeypatch.setenv("QQ_MEMORY_CONSOLE_TOKEN", "tok-bulk-123456")
    return TestClient(app_mod.app), db


def _flash(resp):
    m = re.search(r'<p class="(ok|bad)" id="flash">(.*?)</p>', resp.text, re.S)
    return (m.group(1), m.group(2)) if m else (None, "")


def test_form_defaults_to_placeholder_and_disables_submit(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    with client:
        client.post("/console/login", data={"token": "tok-bulk-123456"}, follow_redirects=True)
        page = client.get("/console/memories").text
    assert '<option value="">— 把勾选的记录改成… —</option>' in page
    assert 'id="bulk-submit" disabled' in page
    assert "var f = document.getElementById" in page


def test_bulk_without_action_is_rejected(tmp_path, monkeypatch):
    client, db = _client(tmp_path, monkeypatch)
    with client:
        client.post("/console/login", data={"token": "tok-bulk-123456"}, follow_redirects=True)
        csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/console/memories").text).group(1)
        r = client.post(
            "/console/actions/bulk",
            data={"csrf": csrf, "action": "", "confirm": "yes", "memory_ids": ["1"]},
            follow_redirects=True,
        )
    cls, text = _flash(r)
    assert cls == "bad"
    assert "先在下拉框里选一个操作" in text
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT is_active FROM memories").fetchone()[0] == 1


def test_event_status_zero_hit_explains_why(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    with client:
        client.post("/console/login", data={"token": "tok-bulk-123456"}, follow_redirects=True)
        csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/console/memories").text).group(1)
        r = client.post(
            "/console/actions/bulk",
            data={"csrf": csrf, "action": "event_status", "status": "planned",
                  "confirm": "yes", "memory_ids": ["1"]},
            follow_redirects=True,
        )
    cls, text = _flash(r)
    assert cls == "bad"
    assert "普通记忆请用" in text


def test_merged_dropdown_has_no_separate_status_select(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    with client:
        client.post("/console/login", data={"token": "tok-bulk-123456"}, follow_redirects=True)
        page = client.get("/console/memories").text
    assert page.count("<optgroup") == 2
    assert '<select name="status">' not in page
    for value in ("activate", "deactivate", "soft_delete",
                  "event_planned", "event_done", "event_cancelled"):
        assert f'value="{value}"' in page
