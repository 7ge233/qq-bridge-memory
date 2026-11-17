# -*- coding: utf-8 -*-
"""召回可见（v5）：/v1/recall 之后 recall_log 能查到注入块与逐条分数。"""

from __future__ import annotations

import json

import recall_log
from retrieval import RecallItem, RecallResult


def _result() -> RecallResult:
    item = RecallItem(
        id=7,
        content="她喜欢下雨天",
        sim=0.81,
        freshness=0.9,
        importance=0.6,
        score=0.77,
        fact_key=None,
        visibility="private",
        happened_at=None,
        speaker="她",
        scope_label="",
        category="preference",
    )
    return RecallResult(
        items=[item],
        gate=0.42,
        reason="ok",
        candidates=9,
        latency_ms=12,
        block="<memory>\n- 她喜欢下雨天\n</memory>\n【记忆结束】",
    )


def test_record_then_read(settings):
    recall_log.record(
        settings,
        account="10001",
        scope_type="private",
        scope_key="qq:10001:private:20002",
        query="我喜欢什么天气",
        result=_result(),
    )
    rows = recall_log.recent(settings, limit=5)
    assert len(rows) == 1
    row = rows[0]
    assert row["item_count"] == 1
    assert "下雨天" in row["block"]
    assert row["reason"] == "ok"
    items = json.loads(row["items"])
    assert items[0]["id"] == 7
    assert items[0]["score"] == 0.77


def test_empty_result_not_logged(settings):
    recall_log.record(
        settings,
        account="10001",
        scope_type="private",
        scope_key="x",
        query="q",
        result=RecallResult(),
    )
    assert recall_log.recent(settings, limit=5) == []


def test_record_never_raises(settings):
    class Boom:
        items = [object()]

    recall_log.record(
        settings,
        account="a",
        scope_type="private",
        scope_key="k",
        query="q",
        result=Boom(),
    )
