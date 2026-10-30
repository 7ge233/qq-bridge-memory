# -*- coding: utf-8 -*-
"""8.2 可自动化部分：模型不可用、DB 排他锁、空库。"""

from __future__ import annotations

import sqlite3

import store
from embedder import Embedder
from retrieval import Retriever


def test_model_unavailable_returns_empty_degraded(settings, writer, make_payload, seed_filler):
    seed_filler()
    writer.write(make_payload("她说想拼团买那款加湿器", "-d1"))
    dead = Embedder(settings, enabled=False)
    retriever = Retriever(settings, dead)
    result = retriever.recall(
        {
            "account": "10001",
            "scope": {"type": "private", "user_id": "20002"},
            "query": "那个拼团加湿器",
        }
    )
    assert result.degraded is True
    assert result.items == []


def test_db_exclusive_lock_write_degrades(settings, writer, make_payload):
    lock = sqlite3.connect(str(settings.db_path), timeout=1)
    try:
        lock.execute("BEGIN EXCLUSIVE")
        result = writer.write(make_payload("锁冲突时写入应降级", "-d2"))
        assert result["ok"] is False
        assert result.get("error") == "db_error"
    finally:
        lock.rollback()
        lock.close()


def test_empty_db_recall_does_not_crash(settings, retriever):
    result = retriever.recall(
        {
            "account": "10001",
            "scope": {"type": "private", "user_id": "20002"},
            "query": "随便问问有什么记忆",
        }
    )
    assert result.items == []
    assert result.reason in ("no_data", "below_gate", "no_hit")
