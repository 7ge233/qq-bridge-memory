# -*- coding: utf-8 -*-
"""/v1/profile 接口层：鉴权、读写、非法项 skip、预算截断（T2）。"""

from __future__ import annotations

import dataclasses
import sqlite3

import config
import db_init

ACCOUNT = "10001"
SCOPE = "qq:10001:private:20002"
TOKEN = "tok-profile-123456"
HEADERS = {"x-memory-token": TOKEN}
WINDOW = {"from": "2026-11-17T05:00:00+08:00", "to": "2026-11-18T05:20:00+08:00"}


def _client(tmp_path, monkeypatch, turns: tuple[str, ...] = ()):
    import app as app_mod
    from fastapi.testclient import TestClient

    db = tmp_path / "profile.db"
    db_init.init_db(db)
    if turns:
        conn = sqlite3.connect(db)
        for index, text in enumerate(turns):
            conn.execute(
                "INSERT INTO conversation_turns "
                "(account, scope_key, scope_type, seq, role, speaker_name, content, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (ACCOUNT, SCOPE, "private", index + 1, "user", "他", text,
                 "2026-11-17T20:00:00+08:00"),
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
    query = {"account": ACCOUNT, "scope_key": SCOPE, **params}
    return client.get("/v1/profile", params=query, headers=HEADERS)


def _post(client, payload: dict):
    return client.post("/v1/profile", json=payload, headers=HEADERS)


def _body(**extra) -> dict:
    return {"account": ACCOUNT, "scope_key": SCOPE, "source": "model", **extra}


def test_requires_memory_token(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        assert client.get("/v1/profile", params={"account": ACCOUNT, "scope_key": SCOPE}).status_code == 401
        assert client.get("/v1/profile", params={"account": ACCOUNT, "scope_key": SCOPE},
                          headers={"x-memory-token": "wrong"}).status_code == 401


def test_empty_scope_returns_empty_block(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        resp = _get(client)
    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True,
        "state": {"last_profile_at": "", "last_profile_day": "", "profile_runs": "0"},
        "items": [],
        "block": "",
        "blockChars": 0,
    }


def test_write_then_read(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, turns=("今天好累", "以后叫我小七吧"))
    with client:
        write = _post(client, _body(window=WINDOW, items=[
            {"fact_key": "user.name", "content": "小七", "basis": "stated", "evidence": "叫我小七"},
            {"fact_key": "user.identity", "content": "后端工程师，正在做QQ机器人项目",
             "basis": "stated", "evidence": "今天好累"},
            {"fact_key": "self.traits", "content": "温柔体贴", "basis": "stated"},
        ]))
        read = _get(client)

    assert write.status_code == 200
    written = write.json()
    assert set(written) == {"ok", "written", "skipped", "downgraded", "advanced", "last_profile_day"}
    assert written["written"] == 2
    assert written["skipped"][0]["fact_key"] == "self.traits"
    assert "不落库" in written["skipped"][0]["reason"]
    assert written["advanced"] is True

    data = read.json()
    assert data["ok"] is True
    assert [one["fact_key"] for one in data["items"]] == ["user.identity", "user.name"]
    assert data["block"] == "【关于他】\n称呼：小七\n身份：后端工程师，正在做QQ机器人项目"
    assert data["blockChars"] == len(data["block"])
    assert set(data["items"][0]) == {"fact_key", "content", "updated_at", "meta"}
    assert data["items"][1]["meta"]["basis"] == "stated"


def test_bad_source_is_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        resp = _post(client, _body(source="robot", items=[
            {"fact_key": "user.style", "content": "说话很短，经常一次只发几个字。", "basis": "inferred"},
        ]))
    assert resp.status_code == 400
    assert "model/user" in resp.json()["detail"]


def test_name_without_window_is_dropped(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        resp = _post(client, _body(items=[{"fact_key": "user.name", "content": "老白", "basis": "stated"}]))
    data = resp.json()
    assert resp.status_code == 200
    assert data["written"] == 0
    assert "没带 window" in data["skipped"][0]["reason"]


def test_evidence_miss_drops_name(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, turns=("随便聊聊",))
    with client:
        resp = _post(client, _body(window=WINDOW, items=[
            {"fact_key": "user.name", "content": "小白", "basis": "stated", "evidence": "叫我小白"},
        ]))
    assert resp.json()["written"] == 0
    assert "原文的子串" in resp.json()["skipped"][0]["reason"]


def test_scope_mismatch_is_invalid_request(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        resp = _get(client, account="99999")
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert resp.json()["items"] == []


def test_missing_items_is_invalid_request(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        resp = _post(client, {"account": ACCOUNT, "scope_key": SCOPE, "source": "model"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "error": "invalid_request"}


def test_max_chars_truncates_block(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        _post(client, _body(items=[
            {"fact_key": "user.identity", "content": "后端工程师，正在做QQ机器人项目", "basis": "inferred"},
            {"fact_key": "user.boundaries", "content": "不喜欢被反复追问同一件事，会生气", "basis": "inferred"},
            {"fact_key": "user.style", "content": "说话很短，经常只发几个字", "basis": "inferred"},
        ]))
        wide = _get(client).json()
        tight = _get(client, max_chars="40").json()
    assert wide["blockChars"] > 40
    assert tight["blockChars"] <= 40
    assert "雷区" in tight["block"]        # 最高优先级保住
    assert "说话风格" not in tight["block"]  # 先丢的是它
