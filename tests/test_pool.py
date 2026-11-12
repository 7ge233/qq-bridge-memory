# -*- coding: utf-8 -*-
"""候选池分层抽样：老记忆不靠 FTS 也能进候选（v2.5.1）。"""

import dataclasses

import pytest

import store
from retrieval import Retriever

ACCOUNT = "10001"
USER = "20002"


def _seed(settings, count=600):
    conn = store.open_conn(settings)
    try:
        key = store.scope_key(ACCOUNT, "private", USER, "")
        for i in range(count):
            store.insert_memory(
                conn,
                {
                    "account": ACCOUNT,
                    "scope_key": key,
                    "scope_type": "private",
                    "user_id": USER,
                    "role": "user",
                    "content": f"第{i}条无关内容",
                    "tokens": "无关 内容",
                    "created_at": "2026-10-01T10:00:00+08:00",
                    "happened_at": "2026-10-01T10:00:00+08:00",
                    "visibility": "private",
                    "state": "active",
                },
            )
        conn.commit()
        first = int(conn.execute("SELECT MIN(id) FROM memories").fetchone()[0])
    finally:
        conn.close()
    return first


def _ids(retriever_obj, settings):
    conn = store.open_conn(settings)
    try:
        where, params = retriever_obj._permission_sql(ACCOUNT, "private", USER, "")
        return retriever_obj._candidate_ids(conn, where, params, ["完全不匹配的词"])
    finally:
        conn.close()


def test_old_memory_reaches_pool_without_fts(settings, retriever):
    first = _seed(settings)
    ids = _ids(retriever, settings)
    assert min(ids) <= first + 10, "最老的一批记忆应该被分层抽样带进候选池"
    assert len(ids) <= 60 + 200 + 100


def test_strata_zero_falls_back_to_newest_only(settings, retriever):
    first = _seed(settings)
    off = Retriever(dataclasses.replace(settings, pool_strata=0), retriever.embedder)
    assert min(_ids(off, settings)) > first + 10, "关掉分层后，老记忆不该再进池"


def test_strata_still_respects_scope(settings, retriever):
    _seed(settings, count=300)
    conn = store.open_conn(settings)
    try:
        key = store.scope_key("99999", "private", "other", "")
        store.insert_memory(conn, {
            "account": "99999", "scope_key": key, "scope_type": "private",
            "user_id": "other", "role": "user", "content": "别人的记忆",
            "tokens": "别人 记忆", "visibility": "private", "state": "active",
        })
        conn.commit()
        other_id = int(conn.execute("SELECT MAX(id) FROM memories").fetchone()[0])
    finally:
        conn.close()
    assert other_id not in _ids(retriever, settings)
