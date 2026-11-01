# -*- coding: utf-8 -*-
"""导入判重：同一条记忆重复导入应被跳过。"""

from __future__ import annotations

import sqlite3

import db_init
from console import _fetch_import_keys, _import_key, _path_hint

SCOPE = {"type": "private", "user_id": "10001"}


def body(**over):
    base = {"account": "1", "scope": SCOPE, "role": "user", "content": "今天吃了面",
            "happened_at": "2026-11-01T12:00:00+08:00"}
    base.update(over)
    return base


def test_key_shape():
    key = _import_key(body())
    assert key == ("1", "qq:1:private:10001", "user", "今天吃了面",
                   "2026-11-01T12:00:00+08:00")


def test_key_differs_on_time():
    assert _import_key(body(happened_at="2026-11-02T12:00:00+08:00")) != _import_key(body())


def test_key_differs_on_content():
    assert _import_key(body(content="今天吃了饭")) != _import_key(body())


def test_key_none_when_scope_bad():
    assert _import_key(body(scope={"type": "private"})) is None
    assert _import_key(body(scope="private")) is None


def test_existing_keys_from_db(tmp_path):
    db = tmp_path / "k.db"
    db_init.init_db(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO memories(account, scope_key, scope_type, user_id, role, content, happened_at)"
        " VALUES('1','qq:1:private:10001','private','10001','user','今天吃了面',"
        "'2026-11-01T12:00:00+08:00')"
    )
    conn.commit()
    keys = _fetch_import_keys(conn)
    assert _import_key(body()) in keys
    assert _import_key(body(content="别的")) not in keys
    conn.close()


def test_path_hint_flags_download_dir():
    assert _path_hint(r"D:\at\download\test.db")
    assert _path_hint(r"D:\同步\OneDrive\a.db")
    assert _path_hint(r"D:\data\qq-memory.db") == ""
