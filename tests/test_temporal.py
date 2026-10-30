# -*- coding: utf-8 -*-
"""v2.3 事件时效：解析、写入、召回过滤、schema 迁移。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import db_init
import store
from conftest import ACCOUNT, PRIVATE
from handoff import HandoffBuilder
from temporal import parse_event_time
from timeutil import TZ


def _base() -> datetime:
    return datetime(2026, 10, 26, 21, 0, 0, tzinfo=TZ)


def test_parse_relative_days():
    assert parse_event_time("明天我要出去玩", _base()).event_at == "2026-10-27T00:00:00+08:00"
    assert parse_event_time("后天", _base()).event_at == "2026-10-28T00:00:00+08:00"
    assert parse_event_time("大后天", _base()).event_at == "2026-10-29T00:00:00+08:00"


def test_parse_week_and_weekend():
    next_wed = parse_event_time("下周三", _base())
    assert next_wed.event_at == "2026-11-04T00:00:00+08:00"
    weekend = parse_event_time("这周末", _base())
    assert weekend.event_at == "2026-10-31T00:00:00+08:00"
    assert weekend.valid_until == "2026-11-01T23:59:59+08:00"
    assert weekend.precision == "range"


def test_parse_month_and_time():
    assert parse_event_time("下个月 3 号", _base()).event_at == "2026-11-03T00:00:00+08:00"
    assert parse_event_time("10月底", _base()).event_at == "2026-10-31T00:00:00+08:00"
    assert parse_event_time("明天下午3点", _base()).event_at == "2026-10-27T15:00:00+08:00"
    assert parse_event_time("今晚八点", _base()).event_at == "2026-10-26T20:00:00+08:00"


def test_parse_ambiguous_returns_none():
    for text in ("过两天", "改天", "下次", "有空再说", "27号员工"):
        assert parse_event_time(text, _base()) is None


def test_parse_idiom_not_time():
    for text in ("一点也不可爱", "有点困", "一点点想睡", "三点开会", "两点意见", "差一点"):
        assert parse_event_time(text, _base()) is None


def test_parse_time_requires_evidence():
    # 有日期但没有时段前缀/时间后缀：只保留“日”精度，不猜钟点
    for text in (
        "明天八点",
        "明天一点小事",
        "明天早一点到",
        "明天快一点",
        "明天两点意见",
        "明天三点水",
    ):
        parsed = parse_event_time(text, _base())
        assert parsed is not None
        assert parsed.precision == "day"
        assert parsed.event_at.endswith("T00:00:00+08:00")

    assert parse_event_time("明天下午三点", _base()).event_at == "2026-10-27T15:00:00+08:00"
    assert parse_event_time("明天三点半", _base()).event_at == "2026-10-27T03:30:00+08:00"
    assert parse_event_time("明天三点钟", _base()).event_at == "2026-10-27T03:00:00+08:00"
    assert parse_event_time("明天3:30", _base()).event_at == "2026-10-27T03:30:00+08:00"


def test_schema_v2_columns_and_idempotent(settings):
    conn = store.open_conn(settings)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
        assert {"event_at", "valid_until", "event_status"} <= columns
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 2
    finally:
        conn.close()
    db_init.init_db(settings.db_path)
    conn = store.open_conn(settings)
    try:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 2
    finally:
        conn.close()


def test_event_written_and_expired_filtered(settings, writer, retriever, make_payload, seed_filler):
    seed_filler()
    happened = (datetime.now(TZ) - timedelta(days=3)).isoformat(timespec="seconds")
    result = writer.write(make_payload("明天我要出去玩", "-e1", happened_at=happened))
    assert result["recorded"] is True

    conn = store.open_conn(settings)
    try:
        row = conn.execute(
            "SELECT category, event_at, valid_until, event_status FROM memories WHERE id=?",
            (result["id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row["category"] == "event"
    assert row["event_status"] == "planned"
    assert row["event_at"] is not None and row["valid_until"] is not None

    default = retriever.recall(
        {"account": ACCOUNT, "scope": PRIVATE, "query": "明天我要出去玩"}
    )
    assert all("出去玩" not in item.content for item in default.items)
    assert default.expired_skipped >= 1

    with_past = retriever.recall(
        {
            "account": ACCOUNT,
            "scope": PRIVATE,
            "query": "明天我要出去玩",
            "include_past": True,
        }
    )
    assert any(item.temporal_state == "past" and "出去玩" in item.content for item in with_past.items)
    assert "已过去" in with_past.block


def test_future_event_is_planned(settings, writer, retriever, make_payload, seed_filler):
    seed_filler()
    happened = datetime.now(TZ).isoformat(timespec="seconds")
    writer.write(make_payload("明天我要出去玩", "-e2", happened_at=happened))
    result = retriever.recall(
        {"account": ACCOUNT, "scope": PRIVATE, "query": "明天我要出去玩"}
    )
    assert any(item.temporal_state == "planned" for item in result.items)
    assert "计划中" in result.block


def test_fact_has_no_event_fields(settings, writer, make_payload):
    result = writer.write(make_payload("她身高 165", "-f1", fact_key="她.身高"))
    conn = store.open_conn(settings)
    try:
        row = conn.execute(
            "SELECT event_at, valid_until, event_status FROM memories WHERE id=?",
            (result["id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row["event_at"] is None and row["valid_until"] is None and row["event_status"] is None


def test_cancel_updates_recent_planned_event(settings, writer, make_payload):
    happened = datetime.now(TZ).isoformat(timespec="seconds")
    first = writer.write(make_payload("明天我要出去玩", "-c1", happened_at=happened))
    second = writer.write(make_payload("不去了", "-c2", happened_at=happened))
    assert second["event_update"]["action"] == "cancel"
    assert second["event_update"]["id"] == first["id"]

    conn = store.open_conn(settings)
    try:
        old = conn.execute(
            "SELECT event_status FROM memories WHERE id=?", (first["id"],)
        ).fetchone()
        new = conn.execute(
            "SELECT event_at, event_status FROM memories WHERE id=?", (second["id"],)
        ).fetchone()
    finally:
        conn.close()
    assert old["event_status"] == "cancelled"
    # 取消消息本身不能被写成一条新的“明天”计划
    assert new["event_at"] is None


def test_reschedule_updates_recent_planned_event(settings, writer, make_payload):
    # 固定一个周一：若用 now()，周五跑这条时会因为「明天」= 周六 =「周末」而解析成同一天，
    # 断言必然失败（2026-10-30 实测踩到）。时间相关的用例一律钉死基准时间。
    happened = "2026-10-26T10:00:00+08:00"
    first = writer.write(make_payload("明天我要出去玩", "-r1", happened_at=happened))
    conn = store.open_conn(settings)
    try:
        original_at = conn.execute(
            "SELECT event_at FROM memories WHERE id=?", (first["id"],)
        ).fetchone()["event_at"]
    finally:
        conn.close()

    second = writer.write(make_payload("改到周末了", "-r2", happened_at=happened))
    assert second["event_update"]["action"] == "reschedule"
    assert second["event_update"]["id"] == first["id"]

    conn = store.open_conn(settings)
    try:
        old = conn.execute(
            "SELECT event_at, event_status FROM memories WHERE id=?", (first["id"],)
        ).fetchone()
        new = conn.execute(
            "SELECT event_at FROM memories WHERE id=?", (second["id"],)
        ).fetchone()
    finally:
        conn.close()
    assert old["event_status"] == "planned"
    assert old["event_at"] != original_at
    assert new["event_at"] is None


def test_cancel_without_candidate_does_not_create_event(settings, writer, make_payload):
    happened = datetime.now(TZ).isoformat(timespec="seconds")
    result = writer.write(make_payload("明天不去了", "-n1", happened_at=happened))
    assert result["event_update"]["action"] == "cancel"
    assert result["event_update"]["id"] is None
    conn = store.open_conn(settings)
    try:
        row = conn.execute(
            "SELECT event_at FROM memories WHERE id=?", (result["id"],)
        ).fetchone()
    finally:
        conn.close()
    assert row["event_at"] is None


def test_cancel_phrase_variant(settings, writer, make_payload):
    happened = datetime.now(TZ).isoformat(timespec="seconds")
    first = writer.write(make_payload("明天我要出去", "-cv1", happened_at=happened))
    second = writer.write(make_payload("又不出去了", "-cv2", happened_at=happened))
    assert second["event_update"]["action"] == "cancel"
    assert second["event_update"]["id"] == first["id"]
    conn = store.open_conn(settings)
    try:
        row = conn.execute(
            "SELECT event_status FROM memories WHERE id=?", (first["id"],)
        ).fetchone()
    finally:
        conn.close()
    assert row["event_status"] == "cancelled"


def test_question_does_not_create_event(settings, writer, make_payload):
    for mid, content in (("-q1", "后天怎么样"), ("-q2", "那大后天呢？")):
        result = writer.write(make_payload(content, mid))
        conn = store.open_conn(settings)
        try:
            row = conn.execute(
                "SELECT event_at FROM memories WHERE id=?", (result["id"],)
            ).fetchone()
        finally:
            conn.close()
        assert row["event_at"] is None


def test_question_cancel_does_not_update_event(settings, writer, make_payload):
    happened = datetime.now(TZ).isoformat(timespec="seconds")
    first = writer.write(make_payload("明天我要出去", "-qq1", happened_at=happened))
    second = writer.write(make_payload("不去了吗？", "-qq2", happened_at=happened))
    assert second["event_update"]["action"] == "cancel_question"
    conn = store.open_conn(settings)
    try:
        row = conn.execute(
            "SELECT event_status FROM memories WHERE id=?", (first["id"],)
        ).fetchone()
    finally:
        conn.close()
    assert row["event_status"] == "planned"


def test_assistant_final_choice_keeps_chosen_and_cancels_other(settings, writer, make_payload):
    happened = datetime.now(TZ).isoformat(timespec="seconds")
    first = writer.write(make_payload("后天我要出去", "-f1", happened_at=happened))
    second = writer.write(make_payload("大后天我要出去", "-f2", happened_at=happened))
    result = writer.write(
        make_payload("那就后天，拖久了你又要忘", "-f3", happened_at=happened, role="assistant")
    )
    assert result["event_update"]["action"] == "final_choice"
    assert result["event_update"]["create"] is False
    assert result["event_update"]["id"] == first["id"]
    assert second["id"] in result["event_update"]["cancelled"]

    conn = store.open_conn(settings)
    try:
        a = conn.execute(
            "SELECT event_status FROM memories WHERE id=?", (first["id"],)
        ).fetchone()
        b = conn.execute(
            "SELECT event_status FROM memories WHERE id=?", (second["id"],)
        ).fetchone()
        c = conn.execute(
            "SELECT event_at FROM memories WHERE id=?", (result["id"],)
        ).fetchone()
    finally:
        conn.close()
    assert a["event_status"] == "planned"
    assert b["event_status"] == "cancelled"
    # 已有同日期计划时，不再建重复事件
    assert c["event_at"] is None


def test_assistant_final_choice_creates_when_none(settings, writer, make_payload):
    happened = datetime.now(TZ).isoformat(timespec="seconds")
    result = writer.write(
        make_payload("那就后天，拖久了你又要忘", "-f4", happened_at=happened, role="assistant")
    )
    assert result["event_update"]["action"] == "final_choice"
    assert result["event_update"]["create"] is True
    conn = store.open_conn(settings)
    try:
        row = conn.execute(
            "SELECT event_at, event_status FROM memories WHERE id=?", (result["id"],)
        ).fetchone()
    finally:
        conn.close()
    assert row["event_at"] is not None
    assert row["event_status"] == "planned"


def test_assistant_final_choice_question_does_not_create(settings, writer, make_payload):
    happened = datetime.now(TZ).isoformat(timespec="seconds")
    result = writer.write(
        make_payload("那就后天吗？你确定吗？", "-f5", happened_at=happened, role="assistant")
    )
    assert result["event_update"]["action"] == "final_choice_question"
    conn = store.open_conn(settings)
    try:
        row = conn.execute(
            "SELECT event_at FROM memories WHERE id=?", (result["id"],)
        ).fetchone()
    finally:
        conn.close()
    assert row["event_at"] is None


def test_handoff_annotates_event_date(settings, writer, make_payload):
    happened = datetime.now(TZ).isoformat(timespec="seconds")
    writer.write(make_payload("明天我要出去玩", "-h1", happened_at=happened))
    result = HandoffBuilder(settings).build({"account": ACCOUNT, "scope": PRIVATE})
    assert result.packet is not None
    assert any(item.get("event_at") for item in result.packet.recent)
    assert "（事件" in result.block
