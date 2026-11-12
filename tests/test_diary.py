# -*- coding: utf-8 -*-
"""日记层：逻辑日、骨架、幂等写入、按日期召回。"""

from datetime import date, datetime, timedelta

import diary
import store
from timeutil import TZ, iso_now

ACCOUNT = "10001"
PRIVATE = {"type": "private", "user_id": "20002", "group_id": ""}


def _key() -> str:
    return store.scope_key(ACCOUNT, "private", "20002", "")


def _turn(conn, content: str, when: datetime, role: str = "user", seq: int = 1) -> None:
    store.insert_turn(
        conn,
        {
            "account": ACCOUNT,
            "scope_key": _key(),
            "scope_type": "private",
            "session_id": "s1",
            "seq": seq,
            "role": role,
            "speaker_name": "对方" if role == "user" else "我",
            "content": content,
            "created_at": when.isoformat(timespec="milliseconds"),
        },
    )


def test_logical_day_cuts_at_five():
    assert diary.logical_day(datetime(2026, 11, 12, 3, 0, tzinfo=TZ), 5) == date(2026, 11, 11)
    assert diary.logical_day(datetime(2026, 11, 12, 6, 0, tzinfo=TZ), 5) == date(2026, 11, 12)
    # 跨 0 点的夜聊属于同一个日记日
    assert diary.logical_day(datetime(2026, 11, 11, 23, 40, tzinfo=TZ), 5) == date(2026, 11, 11)
    assert diary.logical_day(datetime(2026, 11, 12, 1, 20, tzinfo=TZ), 5) == date(2026, 11, 11)


def test_day_window_spans_to_next_cut():
    start, end = diary.day_window(date(2026, 11, 11), 5)
    assert start.date() == date(2026, 11, 11) and start.hour == 5
    assert end.date() == date(2026, 11, 12) and end.hour == 5


def test_build_digest_only_counts_that_day(settings):
    label = date(2026, 11, 11)
    conn = store.open_conn(settings)
    try:
        _turn(conn, "明天一起去看电影吧，说好了", datetime(2026, 11, 11, 20, 0, tzinfo=TZ), seq=1)
        _turn(conn, "好呀，记得给我发地址", datetime(2026, 11, 11, 20, 1, tzinfo=TZ), "assistant", seq=2)
        _turn(conn, "今天好累", datetime(2026, 11, 10, 20, 0, tzinfo=TZ), seq=3)
        conn.commit()
        digest = diary.build_digest(conn, _key(), label, settings)
    finally:
        conn.close()
    assert digest["date"] == "2026-11-11"
    assert digest["turns"] == 2, "只统计当天窗口内的对话"
    assert any("电影" in item for item in digest["commitments"])
    assert "2026-11-11 当天小结" in digest["text"]


def test_write_diary_is_idempotent(settings, fake):
    conn = store.open_conn(settings)
    try:
        first = diary.write_diary(conn, settings, fake, ACCOUNT, PRIVATE, date(2026, 11, 11), "今天聊了电影。")
        second = diary.write_diary(conn, settings, fake, ACCOUNT, PRIVATE, date(2026, 11, 11), "改写后的小结。")
        rows = diary.read_diary(conn, ACCOUNT, PRIVATE, date(2026, 11, 11))
    finally:
        conn.close()
    assert first["chunks"] == 1 and second["chunks"] == 1
    assert len(rows) == 1, "同一天重复写入应覆盖而不是追加"
    assert "改写后" in rows[0]["content"]


def test_long_diary_is_chunked(settings, fake):
    conn = store.open_conn(settings)
    try:
        text = "。".join(f"第{i}件事" + "啊" * 40 for i in range(8)) + "。"
        result = diary.write_diary(conn, settings, fake, ACCOUNT, PRIVATE, date(2026, 11, 11), text)
        rows = diary.read_diary(conn, ACCOUNT, PRIVATE, date(2026, 11, 11))
    finally:
        conn.close()
    assert result["chunks"] >= 2
    assert len(rows) == result["chunks"]


def test_recall_puts_yesterday_diary_first(retriever, settings, fake, seed_filler):
    seed_filler(12)
    yesterday = (datetime.now(TZ) - timedelta(days=1)).date()
    conn = store.open_conn(settings)
    try:
        diary.write_diary(
            conn, settings, fake, ACCOUNT, PRIVATE, yesterday,
            "昨天她说面试通过了，晚上一起吃了火锅。", source="digest",
        )
    finally:
        conn.close()
    result = retriever.recall(
        {"account": ACCOUNT, "scope": PRIVATE, "query": "昨天的事还记得吗"}
    )
    assert result.reason == "ok"
    assert result.items, "应该召回到东西"
    assert result.items[0].category == "diary", "当天小结应该排在最前"
    assert "当天小结" in result.block


def test_recall_says_missing_when_no_diary(retriever, settings, seed_filler):
    seed_filler(12)
    result = retriever.recall(
        {"account": ACCOUNT, "scope": PRIVATE, "query": "前天的事还记得吗"}
    )
    assert "没有留下记忆" in result.block, "没有当天记录时要明说，而不是假装有"


def test_validate_prose_flags_wrong_date(settings):
    digest = {"date": "2026-11-11", "turns": 3, "text": "聊了电影 火锅",
              "topics": ["电影", "火锅"], "commitments": [], "open_loops": [], "preferences": []}
    issues = diary.validate_prose("11月9日我们去看了电影，还吃了火锅。", digest)
    assert any("11月9日" in item for item in issues)
    assert diary.validate_prose("今天聊了电影和火锅。", digest) == []
