# -*- coding: utf-8 -*-
"""T3：召回返回体里叠加画像块（只私聊、不占召回额度、合计不超预算）。"""

from __future__ import annotations

import profile_store
import store

ACCOUNT = "10001"
UID = "20002"
SCOPE = f"qq:{ACCOUNT}:private:{UID}"
PRIVATE = {"type": "private", "user_id": UID, "group_id": ""}
GROUP = {"type": "group", "user_id": UID, "group_id": "30003"}


def _seed_profile(settings) -> None:
    profile_store.clear_cache()
    conn = store.open_conn(settings)
    try:
        report = profile_store.upsert(
            conn,
            ACCOUNT,
            SCOPE,
            [
                {"fact_key": "user.identity", "content": "后端工程师，正在做QQ机器人项目", "basis": "inferred"},
                {"fact_key": "self.observations", "content": "他喜欢自己摸索，卡住了才会开口问", "basis": "inferred"},
                {"fact_key": "self.traits", "content": "温柔体贴", "basis": "stated"},
            ],
            "model",
        )
    finally:
        conn.close()
    assert report["written"] == 2
    assert report["skipped"][0]["fact_key"] == "self.traits"


def _recall(retriever, scope, query: str = "我喜欢下雨天"):
    return retriever.recall(
        {"account": ACCOUNT, "scope": scope, "query": query, "max_chars": 800}
    )


def test_private_recall_carries_profile_block(settings, writer, retriever, make_payload, seed_filler):
    seed_filler(12)
    writer.write(make_payload("我喜欢下雨天", "p-inject-1"))
    _seed_profile(settings)

    result = _recall(retriever, PRIVATE)

    assert result.reason == "ok"
    assert result.block
    assert result.block.startswith("【关于他】"), result.block[:80]
    assert "【关于我】" in result.block          # 画像块在前
    assert "身份：后端工程师" in result.block
    assert len(result.block) <= 800 + settings.profile_max_chars


def test_group_recall_has_no_profile_block(settings, writer, retriever, make_payload, seed_filler):
    seed_filler(12)
    writer.write(make_payload("我喜欢下雨天", "p-inject-2", scope=GROUP))
    _seed_profile(settings)

    result = _recall(retriever, GROUP)

    assert result.reason == "ok"
    assert "【关于他】" not in result.block   # 群聊没有画像（决策 7）


def test_profile_budget_is_configurable(settings, writer, retriever, make_payload, seed_filler):
    seed_filler(12)
    writer.write(make_payload("我喜欢下雨天", "p-inject-3"))
    _seed_profile(settings)

    import dataclasses
    from retrieval import Retriever

    tight = Retriever(dataclasses.replace(settings, profile_max_chars=20), retriever.embedder)
    result = _recall(tight, PRIVATE)
    assert result.block
    assert "【关于他】" not in result.block or len(result.block) <= 800 + 20
