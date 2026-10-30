# -*- coding: utf-8 -*-
"""8.1：权限过滤（私聊不进群、群聊可取 public、私聊不串群）。"""

from __future__ import annotations

import pytest

from conftest import ACCOUNT, GROUP, PRIVATE


@pytest.fixture(autouse=True)
def _filler(seed_filler):
    seed_filler()


def _recall(retriever, scope, query):
    return retriever.recall(
        {"account": ACCOUNT, "scope": scope, "query": query, "limit": 6, "max_chars": 800}
    )


def test_private_memory_never_in_group(settings, writer, retriever, make_payload):
    writer.write(make_payload("她喜欢原木风装修，预算十五万", "-p1"))
    writer.write(make_payload("群里讨论去爬山", "-g1", scope=GROUP))

    result = _recall(retriever, GROUP, "她喜欢原木风装修，预算十五万")
    assert all(item.visibility != "private" for item in result.items)
    assert all("原木风" not in item.content for item in result.items)


def test_group_recall_finds_group_memory(settings, writer, retriever, make_payload):
    writer.write(make_payload("群里讨论去爬山", "-g2", scope=GROUP))
    result = _recall(retriever, GROUP, "群里讨论去爬山")
    assert any("爬山" in item.content for item in result.items)


def test_public_knowledge_visible_in_group(settings, writer, retriever, make_payload):
    writer.write(make_payload("科普：加湿器的原理是什么", "-g3", scope=GROUP))
    result = _recall(retriever, GROUP, "科普：加湿器的原理是什么")
    assert any(item.visibility == "public" and "加湿器" in item.content for item in result.items)


def test_private_recall_excludes_group(settings, writer, retriever, make_payload):
    writer.write(make_payload("群里讨论去爬山", "-g4", scope=GROUP))
    result = _recall(retriever, PRIVATE, "群里讨论去爬山")
    assert all("爬山" not in item.content for item in result.items)


def test_exclude_ids(settings, writer, retriever, make_payload):
    written = writer.write(make_payload("她说想拼团买那款加湿器", "-p2"))
    result = retriever.recall(
        {
            "account": ACCOUNT,
            "scope": PRIVATE,
            "query": "她说想拼团买那款加湿器",
            "exclude_ids": [written["id"]],
        }
    )
    assert all(item.id != written["id"] for item in result.items)
