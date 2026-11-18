# -*- coding: utf-8 -*-
"""/v1/turns：按区间取对话原文（给 bridge 画像生成用）。"""

from __future__ import annotations

import dataclasses
import sqlite3

import config
import db_init

ACCOUNT = "10001"
SCOPE = "private:20002"
TOKEN = "tok-turns-123456"
HEADERS = {"x-memory-token": TOKEN}


def _client(tmp_path, monkeypatch):
    import app as app_mod
    from fastapi.testclient import TestClient

    db = tmp_path / "turns.db"
    db_init.init_db(db)
    conn = sqlite3.connect(db)
    rows = [
        ("2026-11-17T03:00:00+08:00", "凌晨那条，属于上一个逻辑日"),
        ("2026-11-17T20:00:00+08:00", "以后叫我小七吧"),
        ("2026-11-18T06:00:00+08:00", "今天好累"),
    ]
    for index, (created_at, text) in enumerate(rows):
        conn.execute(
            "INSERT INTO conversation_turns "
            "(account, scope_key, scope_type, seq, role, speaker_name, content, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ACCOUNT, f"qq:{ACCOUNT}:private:20002", "private", index + 1, "user", "他",
             text, created_at),
        )
    conn.commit()
    conn.close()

    real = config.get_settings
    monkeypatch.setattr(config, "get_settings", lambda: dataclasses.replace(real(), db_path=db))
    monkeypatch.setattr(app_mod, "get_settings", config.get_settings)
    monkeypatch.setattr(app_mod.logging_setup, "setup_logging", lambda *a, **k: None)
    monkeypatch.setenv("QQ_MEMORY_TOKEN", TOKEN)
    return TestClient(app_mod.app)


def _get(client, **params):
    return client.get("/v1/turns", params={"account": ACCOUNT, "scope": SCOPE, **params}, headers=HEADERS)


def test_returns_window_turns_in_order(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        resp = _get(client, **{"from": "2026-11-17T05:00:00+08:00", "to": "2026-11-18T05:00:00+08:00"})
    data = resp.json()
    assert resp.status_code == 200
    assert data["ok"] is True
    assert data["count"] == 1
    assert data["turns"][0]["content"] == "以后叫我小七吧"
    assert data["turns"][0]["created_at"] == "2026-11-17T20:00:00+08:00"


def test_without_window_returns_all_in_seq_order(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        data = _get(client).json()
    assert data["count"] == 3
    assert [one["seq"] for one in data["turns"]] == [1, 2, 3]


def test_left_closed_right_open(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        data = _get(client, **{"from": "2026-11-18T06:00:00+08:00", "to": "2026-11-18T06:00:00+08:00"}).json()
    assert data["count"] == 0  # 左闭右开，同一点不命中


def test_invalid_scope(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        bad = client.get("/v1/turns", params={"account": ACCOUNT, "scope": "nope"}, headers=HEADERS)
        no_token = client.get("/v1/turns", params={"account": ACCOUNT, "scope": SCOPE})
    assert bad.status_code == 200 and bad.json()["ok"] is False
    assert no_token.status_code == 401

def test_utc_bounds_are_normalized(tmp_path, monkeypatch):
    """调用方传 Z（UTC），库里是 +08:00 —— 不能按字符串比大小。"""
    client = _client(tmp_path, monkeypatch)
    with client:
        # 11-17T20:00+08:00 = 11-17T12:00Z，落在 [11-17T11:00Z, 11-17T13:00Z) 内
        hit = _get(client, **{"from": "2026-11-17T11:00:00.000Z", "to": "2026-11-17T13:00:00.000Z"}).json()
        miss = _get(client, **{"from": "2026-11-17T13:00:00.000Z", "to": "2026-11-17T15:00:00.000Z"}).json()
    assert hit["count"] == 1, hit["turns"]
    assert hit["turns"][0]["content"] == "以后叫我小七吧"
    assert miss["count"] == 0, miss["turns"]
