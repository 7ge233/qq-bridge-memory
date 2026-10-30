# -*- coding: utf-8 -*-
"""8.1：门控（寒暄跳过、我胃疼不跳过、低信息量跳过）。"""

from __future__ import annotations

import store
from timeutil import iso_now


def _seed(settings, count: int = 30) -> None:
    conn = store.open_conn(settings)
    key = store.scope_key("10001", "private", "20002", "")
    try:
        for index in range(count):
            store.insert_memory(
                conn,
                {
                    "account": "10001",
                    "scope_key": key,
                    "scope_type": "private",
                    "user_id": "20002",
                    "role": "user",
                    "content": f"日常闲聊第{index}条",
                    "tokens": "天气 不错",
                    "created_at": iso_now(),
                },
            )
        conn.commit()
    finally:
        conn.close()


def test_greeting_skipped(settings, retriever):
    _seed(settings)
    conn = store.open_conn(settings)
    try:
        _, reason, _ = retriever.gate(conn, "在吗")
        assert reason == "greeting"
        _, reason2, _ = retriever.gate(conn, "？")
        assert reason2 == "greeting"
    finally:
        conn.close()


def test_short_pain_message_not_skipped(settings, retriever):
    _seed(settings)
    conn = store.open_conn(settings)
    try:
        info, reason, tokens = retriever.gate(conn, "我胃疼")
        assert reason == "ok", (reason, tokens, info)
        assert info >= settings.gate_min_info
    finally:
        conn.close()


def test_low_information_skipped(settings, retriever):
    _seed(settings)
    conn = store.open_conn(settings)
    try:
        _, reason, _ = retriever.gate(conn, "天气 不错")
        assert reason == "low_information"
    finally:
        conn.close()
