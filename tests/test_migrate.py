# -*- coding: utf-8 -*-
"""档位迁移：影子列、断点续跑、启动切换。"""

from __future__ import annotations

import sqlite3

import pytest

import db_init
import migrate

TEXTS = [
    "我昨天在群里说破防了",
    "他最喜欢吃白饭配鲸鱼罐头",
    "破防了破防了，真的破防了",
    "今天天气不错适合出去走走",
    "白饭要做得好吃也不容易啊",
]


def make_conn(tmp_path):
    db = tmp_path / "t.db"
    db_init.init_db(db)
    conn = sqlite3.connect(db, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    return conn


def seed(conn, n=40):
    for i in range(n):
        conn.execute(
            "INSERT INTO memories(scope_key, scope_type, content, tokens, role) VALUES(?,?,?,?,?)",
            ("private:1001", "private", TEXTS[i % len(TEXTS)] + f" #{i}", "旧 词 表", "user"),
        )
    conn.commit()


def small_dict(tmp_path):
    path = tmp_path / "dict-small.txt"
    path.write_text("破防 1000 n\n白饭 1000 n\n鲸鱼 1000 n\n", encoding="utf-8")
    return path


def test_ensure_schema_idempotent(tmp_path):
    conn = make_conn(tmp_path)
    migrate.ensure_schema(conn)
    migrate.ensure_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
    assert "tokens_next" in cols
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "app_meta" in tables


def test_meta_roundtrip(tmp_path):
    conn = make_conn(tmp_path)
    migrate.ensure_schema(conn)
    assert migrate.meta_get(conn, "x", "d") == "d"
    migrate.meta_set(conn, "x", 42)
    assert migrate.meta_get(conn, "x") == "42"
    migrate.meta_set(conn, "x", 7)
    assert migrate.meta_get(conn, "x") == "7"


def test_start_sets_state(tmp_path):
    conn = make_conn(tmp_path)
    seed(conn)
    info = migrate.start(conn, str(small_dict(tmp_path)))
    assert info["total"] == 40
    st = migrate.status(conn)
    assert st["state"] == "running"
    assert st["percent"] == 0.0


def test_start_refuses_when_running(tmp_path):
    conn = make_conn(tmp_path)
    seed(conn, 3)
    migrate.start(conn, str(small_dict(tmp_path)))
    with pytest.raises(migrate.MigrationError):
        migrate.start(conn, "full")


def test_run_fills_all_and_ready(tmp_path):
    conn = make_conn(tmp_path)
    seed(conn)
    tier = str(small_dict(tmp_path))
    migrate.start(conn, tier, from_tier="full")
    st = migrate.run(conn, tokenizer=migrate.build_tokenizer(small_dict(tmp_path)))
    assert st["state"] == "ready"
    assert st["pending"] == 0
    assert st["percent"] == 100.0
    # tokens 全程没动
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE tokens = '旧 词 表'").fetchone()[0] == 40
    # 影子列用新词典切出「破防」
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE tokens_next LIKE '%破防%'").fetchone()[0] > 0


def test_resume_after_interrupt(tmp_path):
    conn = make_conn(tmp_path)
    seed(conn, 40)
    tier = str(small_dict(tmp_path))
    tok = migrate.build_tokenizer(small_dict(tmp_path))
    migrate.start(conn, tier)
    half = migrate.run(conn, batch=10, tokenizer=tok, max_batches=2)
    assert half["state"] == "running"
    assert half["pending"] == 20
    conn.close()

    # 换一个连接模拟重启后续跑
    db = tmp_path / "t.db"
    conn2 = sqlite3.connect(db)
    conn2.row_factory = sqlite3.Row
    migrate.ensure_schema(conn2)
    st = migrate.run(conn2, tokenizer=tok)
    assert st["state"] == "ready"
    assert st["pending"] == 0
    assert st["done"] == 40


def test_swap_applies_and_rebuilds(tmp_path):
    conn = make_conn(tmp_path)
    seed(conn, 20)
    tier = str(small_dict(tmp_path))
    migrate.start(conn, tier)
    migrate.run(conn, tokenizer=migrate.build_tokenizer(small_dict(tmp_path)))
    result = migrate.swap(conn, active_tier=tier)
    assert result["swapped"] is True
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE tokens LIKE '%破防%'").fetchone()[0] > 0
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE tokens_next IS NOT NULL").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE tokens = '旧 词 表'").fetchone()[0] == 0
    assert migrate.status(conn)["state"] == "done"
    # FTS 跟着换掉：用新分词能搜到
    hit = conn.execute("SELECT COUNT(*) FROM memories_fts WHERE memories_fts MATCH '破防'").fetchone()[0]
    assert hit > 0


def test_swap_idempotent(tmp_path):
    conn = make_conn(tmp_path)
    seed(conn, 10)
    tier = str(small_dict(tmp_path))
    migrate.start(conn, tier)
    migrate.run(conn, tokenizer=migrate.build_tokenizer(small_dict(tmp_path)))
    assert migrate.swap(conn, active_tier=tier)["swapped"] is True
    again = migrate.swap(conn, active_tier=tier)
    assert again["swapped"] is False


def test_swap_refuses_on_tier_mismatch(tmp_path):
    conn = make_conn(tmp_path)
    seed(conn, 10)
    tier = str(small_dict(tmp_path))
    migrate.start(conn, tier)
    migrate.run(conn, tokenizer=migrate.build_tokenizer(small_dict(tmp_path)))
    result = migrate.swap(conn, active_tier="full")
    assert result["swapped"] is False
    assert migrate.status(conn)["state"] == "ready"
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE tokens = '旧 词 表'").fetchone()[0] == 10


def test_swap_waits_for_rows_written_later(tmp_path):
    conn = make_conn(tmp_path)
    seed(conn, 10)
    tier = str(small_dict(tmp_path))
    migrate.start(conn, tier)
    migrate.run(conn, tokenizer=migrate.build_tokenizer(small_dict(tmp_path)))
    # 迁移完成后又写了一条（运行期新消息）
    conn.execute(
        "INSERT INTO memories(scope_key, scope_type, content, tokens, role) VALUES(?,?,?,?,?)",
        ("private:1001", "private", "破防了", "旧", "user"),
    )
    conn.commit()
    result = migrate.swap(conn, active_tier=tier)
    assert result["swapped"] is False
    assert migrate.status(conn)["state"] == "running"
    # 补齐后即可切换
    migrate.run(conn, tokenizer=migrate.build_tokenizer(small_dict(tmp_path)))
    assert migrate.swap(conn, active_tier=tier)["swapped"] is True


def test_cancel_clears_shadow(tmp_path):
    conn = make_conn(tmp_path)
    seed(conn, 20)
    migrate.start(conn, str(small_dict(tmp_path)))
    migrate.run(conn, batch=5, tokenizer=migrate.build_tokenizer(small_dict(tmp_path)), max_batches=1)
    st = migrate.cancel(conn)
    assert st["state"] == "idle"
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE tokens_next IS NOT NULL").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE tokens = '旧 词 表'").fetchone()[0] == 20


def test_estimate(tmp_path):
    conn = make_conn(tmp_path)
    seed(conn, 100)
    est = migrate.estimate(conn)
    assert est["rows"] == 100
    assert est["seconds"] > 0


def test_tokenize_with_matches_tokenize(tmp_path):
    import textproc

    text = "我昨天在群里说破防了"
    assert textproc.tokenize(text) == textproc.tokenize_with(
        migrate.build_tokenizer(small_dict(tmp_path)).lcut, text
    )


def test_on_startup_swaps_when_ready(tmp_path):
    conn = make_conn(tmp_path)
    seed(conn, 10)
    tier = str(small_dict(tmp_path))
    migrate.start(conn, tier)
    migrate.run(conn, tokenizer=migrate.build_tokenizer(small_dict(tmp_path)))
    result = migrate.on_startup(conn, active_tier=tier)
    assert result["swapped"] is True
    assert migrate.status(conn)["state"] == "done"


def test_on_startup_reports_unfinished(tmp_path):
    conn = make_conn(tmp_path)
    seed(conn, 20)
    tier = str(small_dict(tmp_path))
    migrate.start(conn, tier)
    migrate.run(conn, batch=5, tokenizer=migrate.build_tokenizer(small_dict(tmp_path)), max_batches=1)
    result = migrate.on_startup(conn, active_tier=tier)
    assert result["swapped"] is False
    assert result["resumable"] is True
    assert "迁移未完成" in result["resume_hint"]
