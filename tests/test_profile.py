# -*- coding: utf-8 -*-
"""T1 画像存取层：写入 / 读取 / 旧行失效 / 渲染 / 截断 / 缓存。"""

from __future__ import annotations

import json

import pytest

import profile_store as ps
import store

ACCOUNT = "10001"
UID = "20002"
SCOPE = f"qq:{ACCOUNT}:private:{UID}"
WINDOW = {"from": "2026-11-17T05:00:00+08:00", "to": "2026-11-18T05:20:00+08:00"}


def body(n: int) -> str:
    """恰好 n 个字符的占位正文（绕开字数区间时用）。"""
    return "话" * n


def item(fact_key: str, content: str, basis: str = "stated", **extra) -> dict:
    payload = {"fact_key": fact_key, "content": content, "basis": basis}
    payload.update(extra)
    return payload


def reasons(report: dict) -> list[str]:
    return [one["reason"] for one in report["skipped"]]


def active_rows(conn, scope_key: str = SCOPE, account: str = ACCOUNT) -> list:
    return conn.execute(
        "SELECT id, fact_key, content, is_active, state, superseded_by, source, embedding, meta "
        "FROM memories WHERE account=? AND scope_key=? AND category='profile' AND is_active=1",
        (account, scope_key),
    ).fetchall()


def seed_turns(conn, texts: list[str], created_at: str = "2026-11-17T20:00:00+08:00") -> None:
    for index, text in enumerate(texts):
        conn.execute(
            "INSERT INTO conversation_turns "
            "(account, scope_key, scope_type, seq, role, speaker_name, content, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ACCOUNT, SCOPE, "private", index + 1, "user", "他", text, created_at),
        )


def state(conn, key: str) -> str:
    return ps.get_state(conn, ACCOUNT, SCOPE, key, "")


@pytest.fixture
def conn(settings):
    connection = store.open_conn(settings)
    ps.clear_cache()
    try:
        yield connection
    finally:
        connection.close()
        ps.clear_cache()


# ---------------------------------------------------------------------------
# 写入 → 读取
# ---------------------------------------------------------------------------

def test_return_shape_matches_spec(conn):
    report = ps.upsert(
        conn, ACCOUNT, SCOPE, [item("user.schedule", body(20), basis="inferred")], "model"
    )
    assert set(report) == {"written", "skipped", "downgraded", "advanced", "last_profile_day"}
    assert report["written"] == 1
    assert report["skipped"] == []
    assert report["downgraded"] == 0
    assert report["advanced"] is True
    assert report["last_profile_day"] == "2026-11-18"
    json.dumps(report)  # 必须能直接进 HTTP 响应


def test_write_then_read(conn):
    items = [
        item("user.identity", body(20), evidence="我是个后端"),
        item("user.schedule", body(20), basis="inferred"),
        item("user.topics", body(30)),
        item("self.speech", body(20)),
        item("self.observations", body(25)),
    ]
    report = ps.upsert(conn, ACCOUNT, SCOPE, items, "model", window=WINDOW)
    assert report["written"] == 5

    rows = active_rows(conn)
    assert len(rows) == 5
    assert all(int(row["is_active"]) == 1 for row in rows)
    assert all(row["embedding"] is None for row in rows)  # 画像不参与召回
    loaded = ps.load(conn, ACCOUNT, SCOPE)
    assert [one["fact_key"] for one in loaded] == sorted(one["fact_key"] for one in loaded)
    assert state(conn, ps.KEY_LAST_AT)  # 水位有值
    # window.to=11-18T05:20 已经落在 11-18 这个逻辑日（05:00 切）
    assert state(conn, ps.KEY_LAST_DAY) == "2026-11-18"
    assert report["last_profile_day"] == "2026-11-18"


def test_source_is_mapped_to_column(conn):
    ps.upsert(conn, ACCOUNT, SCOPE, [item("user.schedule", body(20))], "model")
    ps.upsert(conn, ACCOUNT, SCOPE, [item("self.speech", body(20))], "user")
    sources = {row["fact_key"]: row["source"] for row in active_rows(conn)}
    assert sources["user.schedule"] == "llm"
    assert sources["self.speech"] == "console"
    with pytest.raises(ValueError):
        ps.upsert(conn, ACCOUNT, SCOPE, [item("user.schedule", body(20))], "whatever")


def test_old_row_superseded(conn):
    assert ps.upsert(conn, ACCOUNT, SCOPE, [item("user.identity", body(20))], "model")["written"] == 1
    old_id = int(active_rows(conn)[0]["id"])
    assert ps.upsert(conn, ACCOUNT, SCOPE, [item("user.identity", body(21))], "model")["written"] == 1

    rows = active_rows(conn)
    assert len(rows) == 1  # 同一 fact_key 只有一条生效
    new_id = int(rows[0]["id"])
    assert new_id != old_id
    dead = conn.execute(
        "SELECT is_active, state, superseded_by FROM memories WHERE id=?", (old_id,)
    ).fetchone()
    assert int(dead["is_active"]) == 0
    assert dead["state"] == "inactive"
    assert int(dead["superseded_by"]) == new_id  # 版本链指向新行


def test_multiple_rows_are_filtered_by_account_and_scope(conn):
    ps.upsert(conn, ACCOUNT, SCOPE, [item("user.schedule", body(20))], "model")
    assert ps.load(conn, ACCOUNT, SCOPE)
    assert ps.load(conn, "88888", "qq:88888:private:20002") == []


# ---------------------------------------------------------------------------
# 校验规则
# ---------------------------------------------------------------------------

def test_locked_field_never_stored(conn):
    report = ps.upsert(conn, ACCOUNT, SCOPE, [item("self.traits", "温柔体贴")], "model")
    assert report["written"] == 0
    assert report["skipped"][0]["fact_key"] == "self.traits"
    assert "不落库" in report["skipped"][0]["reason"]
    assert active_rows(conn) == []


def test_unknown_and_length(conn):
    report = ps.upsert(
        conn,
        ACCOUNT,
        SCOPE,
        [
            item("user.nope", body(20)),
            item("user.identity", body(10)),   # 下限 15
            item("user.identity", body(130)),  # 写入上限 120
            item("user.schedule", ""),
        ],
        "model",
    )
    assert report["written"] == 0
    assert [one["fact_key"] for one in report["skipped"]] == [
        "user.nope", "user.identity", "user.identity", "user.schedule",
    ]
    assert "白名单" in reasons(report)[0]
    assert "少于下限" in reasons(report)[1]
    assert "超过上限" in reasons(report)[2]
    assert "内容为空" in reasons(report)[3]
    assert report["advanced"] is False
    assert state(conn, ps.KEY_LAST_AT) == ""  # 全丢弃不推进水位
    assert report["last_profile_day"] is None


def test_user_name_needs_stated_and_evidence_in_window(conn):
    seed_turns(conn, ["今天好累", "以后叫我小七吧"])
    ok = ps.upsert(
        conn,
        ACCOUNT,
        SCOPE,
        [item("user.name", "小七", evidence="叫我小七")],
        "model",
        window=WINDOW,
    )
    assert ok["written"] == 1

    # evidence 不在原文里 → 直接丢弃（宁可空着也不瞎喊）
    bad = ps.upsert(
        conn,
        ACCOUNT,
        SCOPE,
        [item("user.name", "小白", evidence="叫我小白")],
        "model",
        window=WINDOW,
    )
    assert bad["written"] == 0
    assert "原文的子串" in bad["skipped"][0]["reason"]
    assert [row["content"] for row in active_rows(conn)] == ["小七"]

    # 没有 window → user.name 一律丢弃
    no_window = ps.upsert(conn, ACCOUNT, SCOPE, [item("user.name", "老白")], "model")
    assert "没带 window" in no_window["skipped"][0]["reason"]

    # inferred 的称呼也不收
    inferred = ps.upsert(
        conn,
        ACCOUNT,
        SCOPE,
        [item("user.name", "阿七", basis="inferred")],
        "model",
        window=WINDOW,
    )
    assert "只接受 stated" in inferred["skipped"][0]["reason"]


def test_other_fields_downgrade_when_evidence_missing(conn):
    seed_turns(conn, ["随便聊聊"])
    report = ps.upsert(
        conn,
        ACCOUNT,
        SCOPE,
        [item("user.identity", body(20), evidence="这句话原文里没有")],
        "model",
        window=WINDOW,
    )
    assert report["written"] == 1
    assert report["downgraded"] == 1
    assert json.loads(active_rows(conn)[0]["meta"])["basis"] == "inferred"


def test_no_window_downgrades_stated(conn):
    report = ps.upsert(conn, ACCOUNT, SCOPE, [item("user.schedule", body(20))], "model")
    assert report["written"] == 1
    assert report["downgraded"] == 1
    assert json.loads(active_rows(conn)[0]["meta"])["basis"] == "inferred"


def test_inferred_never_overrides_stated(conn):
    seed_turns(conn, ["我最近一直在折腾部署"])
    ps.upsert(
        conn,
        ACCOUNT,
        SCOPE,
        [item("user.topics", body(20), basis="stated", evidence="折腾部署")],
        "model",
        window=WINDOW,
    )
    assert json.loads(active_rows(conn)[0]["meta"])["basis"] == "stated"
    report = ps.upsert(
        conn, ACCOUNT, SCOPE, [item("user.topics", body(30), basis="inferred")], "model"
    )
    assert report["written"] == 0
    assert "推断不能覆盖" in report["skipped"][0]["reason"]
    assert active_rows(conn)[0]["content"] == body(20)


def test_inferred_can_replace_inferred(conn):
    ps.upsert(conn, ACCOUNT, SCOPE, [item("user.topics", body(20), basis="inferred")], "model")
    report = ps.upsert(
        conn, ACCOUNT, SCOPE, [item("user.topics", body(25), basis="inferred")], "model"
    )
    assert report["written"] == 1
    assert active_rows(conn)[0]["content"] == body(25)


def test_unchanged_is_skipped_but_advances_watermark(conn):
    ps.upsert(conn, ACCOUNT, SCOPE, [item("user.schedule", body(20))], "model")
    before = len(conn.execute("SELECT id FROM memories").fetchall())
    report = ps.upsert(conn, ACCOUNT, SCOPE, [item("user.schedule", body(20))], "model")
    assert report["written"] == 0
    assert "完全相同" in report["skipped"][0]["reason"]
    assert len(conn.execute("SELECT id FROM memories").fetchall()) == before  # 版本链没被撑爆
    assert report["advanced"] is True  # 7：无变化但输出合法，水位照推


def test_duplicate_in_one_request(conn):
    report = ps.upsert(
        conn,
        ACCOUNT,
        SCOPE,
        [item("user.schedule", body(20)), item("user.schedule", body(21))],
        "model",
    )
    assert report["written"] == 1
    assert "重复出现" in report["skipped"][0]["reason"]


def test_bad_items_do_not_block_good_ones(conn):
    report = ps.upsert(
        conn,
        ACCOUNT,
        SCOPE,
        [item("self.name", "鲸鱼娘"), item("user.schedule", body(20)), "not-a-dict"],
        "model",
    )
    assert report["written"] == 1
    assert len(report["skipped"]) == 2
    assert len(active_rows(conn)) == 1


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def test_render_block_empty_scope(conn):
    assert ps.render_block(conn, ACCOUNT, SCOPE) == ""


def test_render_block_order_and_labels(conn):
    seed_turns(conn, ["以后叫我小七吧"])
    ps.upsert(
        conn,
        ACCOUNT,
        SCOPE,
        [
            item("self.observations", "他喜欢自己摸索，卡住了才会开口问", basis="inferred"),
            item("user.identity", "后端工程师，正在做QQ机器人项目"),
            item("user.name", "小七", evidence="叫我小七"),
            item("self.speech", "平时说话很短，偶尔会发一两个颜文字", basis="inferred"),
        ],
        "model",
        window=WINDOW,
    )
    block = ps.render_block(conn, ACCOUNT, SCOPE)
    assert block.startswith("【关于他】")
    assert "【关于我】" in block
    labels = [line.split("：")[0] for line in block.splitlines() if "：" in line]
    assert labels == ["称呼", "身份", "说话习惯", "对用户的观察"]  # 4.4 顺序
    assert ps.render_block(conn, ACCOUNT, SCOPE, max_chars=500) == block


def test_render_block_truncation_keeps_priority(conn):
    ps.upsert(
        conn,
        ACCOUNT,
        SCOPE,
        [
            item("user.identity", body(20)),
            item("user.schedule", body(20)),
            item("self.growth", body(20)),
        ],
        "model",
    )
    full = ps.render_block(conn, ACCOUNT, SCOPE, max_chars=400)
    assert len(full) > 50
    tight = ps.render_block(conn, ACCOUNT, SCOPE, max_chars=50)
    assert len(tight) <= 50
    assert "相处中的变化" in tight   # 最高优先级保住了
    assert "身份" not in tight      # 先丢的是它
    assert ps.render_block(conn, ACCOUNT, SCOPE, max_chars=0) == ""


def test_render_block_cache_and_invalidate(conn):
    ps.upsert(conn, ACCOUNT, SCOPE, [item("user.schedule", body(20))], "model")
    first = ps.render_block(conn, ACCOUNT, SCOPE)
    assert first

    # 绕过存取层直接改库：60 秒内应该还是缓存里的旧块
    conn.execute(
        "UPDATE memories SET content = ? WHERE category = 'profile' AND is_active = 1",
        (body(25),),
    )
    assert ps.render_block(conn, ACCOUNT, SCOPE) == first

    ps.invalidate(ACCOUNT, SCOPE)
    assert body(25) in ps.render_block(conn, ACCOUNT, SCOPE)


def test_upsert_invalidates_cache(conn):
    ps.upsert(conn, ACCOUNT, SCOPE, [item("user.schedule", body(20))], "model")
    first = ps.render_block(conn, ACCOUNT, SCOPE)
    ps.upsert(conn, ACCOUNT, SCOPE, [item("user.schedule", body(28))], "model")
    second = ps.render_block(conn, ACCOUNT, SCOPE)
    assert first != second
    assert body(28) in second


def test_render_block_never_raises(conn):
    """坏 scope / 空 scope 都返回空串，不能把召回带崩。"""
    assert ps.render_block(conn, ACCOUNT, "garbage") == ""
    assert ps.render_block(conn, "", "") == ""
    assert ps.render_block(conn, ACCOUNT, f"qq:{ACCOUNT}:group:30003") == ""


# ---------------------------------------------------------------------------
# 串号 / 群聊防护
# ---------------------------------------------------------------------------

def test_scope_must_match_account_and_be_private(conn):
    with pytest.raises(ValueError):
        ps.upsert(conn, "77777", SCOPE, [item("user.schedule", body(20))], "model")
    with pytest.raises(ValueError):
        ps.upsert(conn, ACCOUNT, f"qq:{ACCOUNT}:group:30003", [item("user.schedule", body(20))], "model")
    with pytest.raises(ValueError):
        ps.load(conn, ACCOUNT, "garbage")


def test_profile_rows_identified_by_category_only(conn):
    """判断画像行只认 category='profile'，不看 source。"""
    ps.upsert(conn, ACCOUNT, SCOPE, [item("user.schedule", body(20))], "model")
    count = conn.execute(
        "SELECT count(*) FROM memories WHERE category='profile' AND is_active=1 AND source='llm'"
    ).fetchone()[0]
    assert int(count) == 1
    assert len(ps.load(conn, ACCOUNT, SCOPE)) == 1

def test_upsert_without_row_factory(tmp_path):
    """连接没设 row_factory 时也不能炸：写第二条会查现存行。"""
    import sqlite3

    import db_init

    db = tmp_path / "raw.db"
    db_init.init_db(db)
    conn = sqlite3.connect(db)  # 故意不设 sqlite3.Row
    try:
        first = ps.upsert(
            conn, ACCOUNT, SCOPE,
            [item("user.identity", body(20), basis="inferred")],
            "model", window=WINDOW, day_cut_hour=5,
        )
        second = ps.upsert(
            conn, ACCOUNT, SCOPE,
            [item("user.schedule", body(20), basis="inferred")],
            "model", window=WINDOW, day_cut_hour=5,
        )
    finally:
        conn.close()
    assert first["written"] == 1
    assert second["written"] == 1

def test_long_content_is_saved_but_clipped_for_render(settings):
    """写入只挡跑飞（>120），显示按 4.4 预算截断 —— 46 字不能再被整条丢掉。"""
    ps.clear_cache()
    conn = store.open_conn(settings)
    try:
        long_body = "正在开发并反复调试一个对话项目的开发者，靠对话跑测试来验证效果"  # 33 字
        report = ps.upsert(conn, ACCOUNT, SCOPE, [
            item("user.identity", long_body, basis="inferred"),
        ], "model", window=WINDOW, day_cut_hour=5)
        assert report["written"] == 1, report

        clip = ps._clip_for_render("一" * 60, 40)
        assert len(clip) <= 41 and clip.endswith("…")
        styled = ps._clip_for_render("先做接口，再做脚本，最后补文档，剩下的明天再说", 20)
        assert styled.endswith("…"), styled
        assert styled[:-1][-1] in "。，；、！？", styled  # 切在句读之后
    finally:
        conn.close()


def test_volatile_field_overrides_stated(settings):
    """user.current 是状态：现存 stated 也要被新值覆盖，旧行走 supersede 链。"""
    ps.clear_cache()
    conn = store.open_conn(settings)
    try:
        first = ps.upsert(conn, ACCOUNT, SCOPE, [
            item("user.current", "正在给机器人加画像功能，天天在调提示词", basis="stated"),
        ], "model", window=WINDOW, day_cut_hour=5)
        assert first["written"] == 1, first
        second = ps.upsert(conn, ACCOUNT, SCOPE, [
            item("user.current", "画像跑通了，这两天在调按日回放的脚本", basis="inferred"),
        ], "model", window=WINDOW, day_cut_hour=5)
        assert second["written"] == 1, second
        rows = conn.execute(
            "SELECT content, is_active FROM memories WHERE category='profile' AND fact_key='user.current'",
        ).fetchall()
        active = [r[0] for r in rows if r[1] == 1]
        assert active == ["画像跑通了，这两天在调按日回放的脚本"], rows
    finally:
        conn.close()


def test_stable_field_still_protected_from_inferred(settings):
    """稳定事实照旧：stated 的身份不能被推断覆盖。"""
    ps.clear_cache()
    conn = store.open_conn(settings)
    try:
        seed_turns(conn, ["我在一家做企业服务的公司负责接口，就是个后端"])
        ps.upsert(conn, ACCOUNT, SCOPE, [
            item("user.identity", "后端工程师，在一家做企业服务的公司负责接口", basis="stated",
                 evidence="一家做企业服务的公司负责接口"),
        ], "model", window=WINDOW, day_cut_hour=5)
        report = ps.upsert(conn, ACCOUNT, SCOPE, [
            item("user.identity", "可能是个前端，看他聊界面的样子像", basis="inferred"),
        ], "model", window=WINDOW, day_cut_hour=5)
        assert report["written"] == 0, report
        assert "stated" in report["skipped"][0]["reason"]
    finally:
        conn.close()

def test_watermark_is_last_consumed_turn(settings):
    """水位 = window.to（最后消费的那一轮），不是服务器时钟。

    否则取数被 turnLimit 截断时，窗口末尾到 now 之间的对话会被永久跳过。
    """
    ps.clear_cache()
    conn = store.open_conn(settings)
    try:
        cut = {"from": "2026-11-16T04:00:00+08:00", "to": "2026-11-16T12:24:22+08:00"}
        ps.upsert(conn, ACCOUNT, SCOPE, [
            item("user.identity", "在做一个QQ机器人项目，天天在调接口", basis="inferred"),
        ], "model", window=cut, day_cut_hour=5)
        at = ps.get_state(conn, ACCOUNT, SCOPE, ps.KEY_LAST_AT)
        day = ps.get_state(conn, ACCOUNT, SCOPE, ps.KEY_LAST_DAY)
        assert at == "2026-11-16T12:24:22+08:00", at
        assert day == "2026-11-16", day
    finally:
        conn.close()
