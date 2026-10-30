# -*- coding: utf-8 -*-
"""8.1：freshness / score / 截断。"""

from __future__ import annotations

from datetime import datetime

import pytest

from timeutil import TZ
from retrieval import RecallItem, final_score, freshness_score, render_block


def _now() -> datetime:
    return datetime(2026, 10, 25, 12, 0, 0, tzinfo=TZ)


def test_freshness_normal_half_life(settings):
    value = freshness_score(
        "2026-09-25T12:00:00+08:00", None, None, settings=settings, now=_now()
    )
    assert value == pytest.approx(0.5, abs=0.01)


def test_freshness_fact_half_life(settings):
    value = freshness_score(
        "2026-09-25T12:00:00+08:00", None, "她.身高", settings=settings, now=_now()
    )
    assert value == pytest.approx(0.5 ** (30 / 180), abs=0.01)


def test_freshness_without_timestamp(settings):
    assert freshness_score(None, None, None, settings=settings, now=_now()) == 0.5


def test_freshness_prefers_happened_at(settings):
    value = freshness_score(
        "2026-10-24T12:00:00+08:00",
        "2020-01-01T00:00:00+08:00",
        None,
        settings=settings,
        now=_now(),
    )
    assert value == pytest.approx(0.5 ** (1 / 30), abs=0.01)


def test_score_weights():
    assert final_score(1.0, 1.0, 1.0) == pytest.approx(1.0)
    assert final_score(0.5, 0.5, 0.5) == pytest.approx(0.5)
    assert final_score(1.0, 0.0, 0.0) == pytest.approx(0.7)
    assert final_score(0.0, 1.0, 0.0) == pytest.approx(0.2)
    assert final_score(0.0, 0.0, 1.0) == pytest.approx(0.1)


def _item(index: int, content: str, score: float) -> RecallItem:
    return RecallItem(
        id=index,
        content=content,
        sim=0.8,
        freshness=0.5,
        importance=0.5,
        score=score,
        fact_key=None,
        visibility="private",
        happened_at="2026-10-20T12:00:00+08:00",
        speaker="她",
        scope_label="私聊",
        created_at="2026-10-20T12:00:00+08:00",
    )


def test_render_block_budget():
    items = [_item(i, "很长的内容" * 100, 1.0 - i * 0.01) for i in range(1, 11)]
    block = render_block(items, 800)
    assert len(block) <= 800
    assert block.startswith("【相关历史记忆（背景参考）】")
    assert block.endswith("【记忆结束】")
    assert block.count("- [") >= 1


def test_select_top6_and_char_limit(settings, retriever):
    items = [_item(i, "内容" * 50, 1.0 - i * 0.01) for i in range(1, 11)]
    selected = retriever._select(items, 6, 800)
    assert len(selected) <= 6
    block = render_block(selected, 800)
    assert len(block) <= 800
