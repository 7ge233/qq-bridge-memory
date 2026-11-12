# -*- coding: utf-8 -*-
"""MMR 多样性 + 刚召回降权（治复读）。"""

import dataclasses

import numpy as np

from retrieval import RecallItem, Retriever


def _vec(slot: int, dim: int = 8, noise: float = 0.0) -> np.ndarray:
    v = np.zeros(dim, dtype=np.float32)
    v[slot] = 1.0
    if noise:
        v[(slot + 1) % dim] = noise
    return v / float(np.linalg.norm(v))


def _item(i: int, vec, score: float, content: str = "") -> RecallItem:
    return RecallItem(
        id=i, content=content or f"记忆{i}", sim=0.6, freshness=1.0, importance=0.5,
        score=score, fact_key=None, visibility="private", happened_at=None,
        speaker="她", scope_label="私聊", vector=vec,
    )


def test_mmr_spreads_similar_items(settings):
    items = [_item(i, _vec(0), 1.0 - i * 0.01) for i in range(4)]
    items += [_item(10 + i, _vec(1 + i), 0.80) for i in range(3)]

    plain = Retriever(dataclasses.replace(settings, mmr_lambda=1.0), None)
    picked = plain._select(list(items), 4, 2000)
    assert sum(1 for it in picked if it.id < 10) == 4, "关掉 MMR 时应该被同主题占满"

    mmr = Retriever(dataclasses.replace(settings, mmr_lambda=0.5), None)
    diverse = mmr._select(list(items), 4, 2000)
    assert sum(1 for it in diverse if it.id < 10) < 4, "开 MMR 后不该被同主题占满"


def test_recall_penalty_decays_over_window(settings):
    fixed = settings
    r = Retriever(dataclasses.replace(fixed, recall_penalty=0.2, recall_penalty_minutes=60.0), None)
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
    fresh = (now - timedelta(minutes=0)).isoformat()
    half = (now - timedelta(minutes=30)).isoformat()
    old = (now - timedelta(minutes=600)).isoformat()
    assert r._recall_penalty(fresh, now) == 1.0 - 0.2
    assert abs(r._recall_penalty(half, now) - 0.9) < 1e-6
    assert r._recall_penalty(old, now) == 1.0
    assert r._recall_penalty(None, now) == 1.0
