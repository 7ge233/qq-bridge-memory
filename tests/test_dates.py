# -*- coding: utf-8 -*-
"""中文日期解析 + 话题噪声（名词过滤）。"""

from datetime import date, datetime, timedelta

import diary
import store
import temporal
from timeutil import TZ

ACCOUNT = "10001"
PRIVATE = {"type": "private", "user_id": "20002", "group_id": ""}


def _base(y=2026, m=11, d=20, h=10):
    return datetime(y, m, d, h, 0, tzinfo=TZ)


def _day(text: str, base: datetime) -> date | None:
    parsed = temporal.parse_event_time(text, base)
    return None if parsed is None else date.fromisoformat(parsed.event_at[:10])


def test_chinese_month_day():
    assert _day("上个月八号的事", _base()) == date(2026, 10, 8)
    assert _day("十一月五号", _base()) == date(2026, 11, 5)
    assert _day("下个月三号", _base()) == date(2026, 12, 3)
    assert _day("这个月十五号", _base()) == date(2026, 11, 15)


def test_bare_day_falls_back_to_previous_month():
    assert _day("三号那天", _base(d=5)) == date(2026, 11, 3)
    # 15 号比 5 号晚太多，说明说的是上个月
    assert _day("十五号", _base(d=5)) == date(2026, 10, 15)


def test_relative_days():
    assert _day("大前天", _base()) == date(2026, 11, 17)
    assert _day("前天的事还记得吗", _base()) == date(2026, 11, 18)


def test_weekday_prefix_with_ge():
    # 2026-11-20 是周五：上周（11-09~11-15）的周三是 11-11
    assert _day("上个礼拜三", _base()) == date(2026, 11, 11)
    assert _day("下周三", _base()) == date(2026, 11, 25)


def test_line_numbers_are_not_dates():
    assert _day("我坐3号线回家", _base()) is None
    assert _day("住5号楼", _base()) is None


def test_topics_keep_nouns_only(settings):
    label = date(2026, 11, 11)
    conn = store.open_conn(settings)
    try:
        key = store.scope_key(ACCOUNT, "private", "20002", "")
        # 先灌 30 条「平时就这么说话」的语料：说好/开心/终于都是口头禅
        for i in range(30):
            store.insert_turn(conn, {
                "account": ACCOUNT, "scope_key": key, "scope_type": "private",
                "session_id": "old", "seq": i + 1, "role": "user",
                "content": "说好，开心，终于，一起，吃饭，晚安",
                "created_at": datetime(2026, 11, 1, 12, tzinfo=TZ).isoformat(timespec="milliseconds"),
            })
        for i, (role, text) in enumerate([
            ("user", "今天面试终于过了，太开心了"),
            ("assistant", "太好了，那晚上去吃火锅庆祝一下"),
            ("user", "行，说好了啊，周六我请你"),
        ]):
            store.insert_turn(conn, {
                "account": ACCOUNT, "scope_key": key, "scope_type": "private",
                "session_id": "s1", "seq": i + 1, "role": role, "content": text,
                "created_at": datetime(2026, 11, 11, 20 + i, tzinfo=TZ).isoformat(timespec="milliseconds"),
            })
        conn.commit()
        digest = diary.build_digest(conn, key, label, settings)
    finally:
        conn.close()
    topics = digest["topics"]
    assert any("面试" in t for t in topics)
    assert any("火锅" in t for t in topics)
    assert not any(t in ("终于", "说好", "开心") for t in topics), f"口头禅不该当话题：{topics}"
