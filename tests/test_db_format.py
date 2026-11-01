# -*- coding: utf-8 -*-
"""登记前的格式检查：只有本程序的库才允许登记。"""

from __future__ import annotations

import sqlite3

import db_init


def test_missing_file(tmp_path):
    info = db_init.inspect_db(tmp_path / "nope.db")
    assert info["ok"] is False
    assert "不存在" in info["reason"]


def test_empty_file(tmp_path):
    p = tmp_path / "empty.db"
    p.write_bytes(b"")
    info = db_init.inspect_db(p)
    assert info["ok"] is False
    assert "空文件" in info["reason"]


def test_not_sqlite(tmp_path):
    p = tmp_path / "note.db"
    p.write_text("hello", encoding="utf-8")
    info = db_init.inspect_db(p)
    assert info["ok"] is False
    assert "不是 SQLite" in info["reason"]


def test_missing_tables(tmp_path):
    p = tmp_path / "other.db"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE something(x)")
    conn.commit()
    conn.close()
    info = db_init.inspect_db(p)
    assert info["ok"] is False
    assert "缺少表" in info["reason"]


def test_valid_db(tmp_path):
    p = tmp_path / "good.db"
    db_init.init_db(p)
    info = db_init.inspect_db(p)
    assert info["ok"] is True
    assert "memories" in info["tables"]
    assert info["reason"] == ""


def test_too_new_version(tmp_path):
    p = tmp_path / "future.db"
    db_init.init_db(p)
    conn = sqlite3.connect(p)
    conn.execute(f"PRAGMA user_version = {db_init.SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    info = db_init.inspect_db(p)
    assert info["ok"] is False
    assert "版本过新" in info["reason"]


def test_inspect_does_not_modify(tmp_path):
    p = tmp_path / "other2.db"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE something(x)")
    conn.commit()
    conn.close()
    before = p.read_bytes()
    db_init.inspect_db(p)
    assert p.read_bytes() == before
