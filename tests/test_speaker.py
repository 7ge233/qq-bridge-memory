# -*- coding: utf-8 -*-
"""说话人字段：桥接填的「私聊」不是人名，渲染要退回 QQ 号。"""

from __future__ import annotations

import dataclasses
import re
import sqlite3

import config
import db_init
import writer


def test_placeholder_names_are_dropped():
    for bad in ("私聊", "群聊", "private", "GROUP", " unknown ", "未知"):
        assert writer._clean_speaker_name(bad) is None
    assert writer._clean_speaker_name("阿七") == "阿七"
    assert writer._clean_speaker_name("  ") is None
    assert writer._clean_speaker_name(None) is None


def _client(tmp_path, monkeypatch):
    import app as app_mod
    from fastapi.testclient import TestClient

    db = tmp_path / "sp.db"
    db_init.init_db(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO memories(account, scope_key, scope_type, user_id, role, content,"
        " speaker_id, speaker_name) VALUES('1','s','private','1401703272','user','在吗',"
        "'1401703272',NULL)"
    )
    conn.execute(
        "INSERT INTO memories(account, scope_key, scope_type, user_id, role, content,"
        " speaker_id, speaker_name) VALUES('1','s','private','1401703272','assistant','在',"
        "'3954318677',NULL)"
    )
    conn.commit()
    conn.close()
    real = config.get_settings
    monkeypatch.setattr(config, "get_settings", lambda: dataclasses.replace(real(), db_path=db))
    monkeypatch.setattr(app_mod, "get_settings", config.get_settings)
    monkeypatch.setattr(app_mod.logging_setup, "setup_logging", lambda *a, **k: None)
    monkeypatch.setenv("QQ_MEMORY_CONSOLE_TOKEN", "tok-sp-123456")
    return TestClient(app_mod.app)


def test_speaker_cell_falls_back_to_qq_id(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        client.post("/console/login", data={"token": "tok-sp-123456"}, follow_redirects=True)
        page = client.get("/console/memories?is_active=all").text
    assert ">1401703272<" in page          # 用户那条：显示 QQ 号
    assert "私聊 · 1401703272" in page      # scope 带上对方号
    assert "<td>私聊</td>" not in page      # 没人把「私聊」当人名（下拉框里的选项不算）
