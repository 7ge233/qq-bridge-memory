# -*- coding: utf-8 -*-
"""跨会话续接包（4.5）：画像 + 最近原文 + 未完成事项 + 话题 + 偏好，总预算 1500 字。

关键点：
  * ①②③④ 现场从 conversation_turns / memories 组装，⑤ 走 memories；
  * 注入措辞与 4.3 的「相关历史记忆」完全不同——这是"你就是刚才那个你"；
  * 超预算按 ②→③→①→④→⑤ 裁剪（先砍最旧原文，最后砍摘要）；
  * 组包失败降级：完整包 → 仅最近 20 条原文 → 仅一句"上一段对话未能保存"，
    绝不因为失败而卡住会话轮换（4.3 安全阀 b）。
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field

import store
import textproc
from config import Settings, get_settings
from timeutil import date_only, iso_now, short_date

logger = logging.getLogger("qq_memory.handoff")

BLOCK_HEADER = (
    "【上一段对话（接着聊，不是新对话）】\n"
    "下面是你和对方刚才的对话。你是同一个人，语气和话题都要接上。\n"
    '不要向对方提起"上下文/记忆/会话"这些词，也不要复述这段摘要。\n'
)
BLOCK_FOOTER = "\n【上一段对话结束】"

_LOOP_PATTERNS = ("答应", "约定", "说好", "讲好", "待办", "记得", "别忘", "提醒我", "等你")
_PREF_PATTERNS = ("喜欢", "讨厌", "偏好", "最爱", "爱吃", "不爱")


@dataclass
class HandoffPacket:
    profile: str = ""
    recent: list[dict] = field(default_factory=list)
    open_loops: list[str] = field(default_factory=list)
    topic: str = ""
    preferences: list[str] = field(default_factory=list)
    generation: int = 0

    def as_dict(self) -> dict:
        return {
            "profile": self.profile,
            "recent": self.recent,
            "open_loops": self.open_loops,
            "topic": self.topic,
            "preferences": self.preferences,
            "generation": self.generation,
        }


@dataclass
class HandoffResult:
    packet: HandoffPacket | None = None
    chars: int = 0
    degraded: bool = False
    reason: str = ""
    block: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": True,
            "packet": self.packet.as_dict() if self.packet else None,
            "chars": self.chars,
            "degraded": self.degraded,
            "reason": self.reason,
            "block": self.block,
        }


class HandoffBuilder:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    # ------------------------------------------------------------------
    def build(self, payload: Mapping) -> HandoffResult:
        account = str(payload.get("account") or "").strip()
        if not account:
            raise ValueError("缺少 account")
        scope_type, user_id, group_id = store.parse_scope(payload.get("scope"))
        key = store.scope_key(account, scope_type, user_id, group_id)
        limit = _clamp(
            _int(payload.get("limit"), self.settings.handoff_recent),
            1,
            self.settings.handoff_recent_max,
        )

        conn = store.open_conn(self.settings)
        try:
            turns = store.recent_turns(conn, key, limit)
            if not turns:
                return HandoffResult(reason="no_data")
            try:
                packet = self._assemble(conn, account, key, scope_type, group_id, turns)
                return self._render(packet)
            except Exception as exc:  # 组包失败 → 降级链（4.3 安全阀 b）
                logger.warning("续接包组装失败，走降级：%s", type(exc).__name__)
                return self._fallback(turns)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    def _assemble(
        self,
        conn: sqlite3.Connection,
        account: str,
        key: str,
        scope_type: str,
        group_id: str,
        turns: list[sqlite3.Row],
    ) -> HandoffPacket:
        packet = HandoffPacket(generation=store.latest_generation(conn, key))
        packet.profile = self._profile(conn, account, key)
        packet.open_loops = self._loops(conn, account, key)
        packet.preferences = self._preferences(conn, account, key)
        packet.topic = self._topic(turns)
        event_map = self._event_map(conn, key, turns)
        packet.recent = [
            {
                "role": row["role"],
                "speaker": self._speaker(row),
                "content": str(row["content"] or "")[:120],
                "at": row["happened_at"] or row["created_at"],
                "event_at": event_map.get(str(row["message_id"] or "")),
            }
            for row in turns
        ]
        return packet

    @staticmethod
    def _event_map(
        conn: sqlite3.Connection, key: str, turns: list[sqlite3.Row]
    ) -> dict[str, str]:
        ids = [str(row["message_id"]) for row in turns if row["message_id"]]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT message_id, event_at FROM memories WHERE scope_key=? "
            f"AND message_id IN ({placeholders}) AND event_at IS NOT NULL",
            [key, *ids],
        ).fetchall()
        return {str(row["message_id"]): str(row["event_at"]) for row in rows}

    def _profile(self, conn: sqlite3.Connection, account: str, key: str) -> str:
        row = conn.execute(
            "SELECT content FROM memories WHERE account=? AND scope_key=? "
            "AND is_active=1 AND deleted_at IS NULL "
            "AND (category='fact' OR fact_key IS NOT NULL) "
            "ORDER BY importance DESC, id DESC LIMIT 1",
            (account, key),
        ).fetchone()
        if not row:
            return ""
        return textproc.truncate(str(row["content"] or ""), 120)

    def _loops(self, conn: sqlite3.Connection, account: str, key: str) -> list[str]:
        like = " OR ".join("content LIKE ?" for _ in _LOOP_PATTERNS)
        params = [f"%{p}%" for p in _LOOP_PATTERNS]
        rows = conn.execute(
            f"SELECT content, event_at FROM memories WHERE account=? AND scope_key=? "
            f"AND is_active=1 AND deleted_at IS NULL AND ({like}) "
            f"AND (event_status IS NULL OR event_status NOT IN ('cancelled','done')) "
            f"AND (valid_until IS NULL OR valid_until >= ?) "
            f"ORDER BY importance DESC, id DESC LIMIT 3",
            (account, key, *params, iso_now()),
        ).fetchall()
        out: list[str] = []
        for row in rows:
            text = textproc.truncate(str(row["content"] or ""), 50)
            event_date = date_only(row["event_at"]) if row["event_at"] else ""
            if event_date:
                text = f"{text}（事件 {event_date}）"
            out.append(text)
        return out

    def _preferences(self, conn: sqlite3.Connection, account: str, key: str) -> list[str]:
        like = " OR ".join("fact_key LIKE ?" for _ in _PREF_PATTERNS)
        params = [f"%{p}%" for p in _PREF_PATTERNS]
        rows = conn.execute(
            f"SELECT content FROM memories WHERE account=? AND scope_key=? "
            f"AND is_active=1 AND deleted_at IS NULL "
            f"AND (category='preference' OR {like}) "
            f"ORDER BY importance DESC, id DESC LIMIT 3",
            (account, key, *params),
        ).fetchall()
        return [textproc.truncate(str(r["content"] or ""), 50) for r in rows]

    def _topic(self, turns: list[sqlite3.Row]) -> str:
        counter: Counter[str] = Counter()
        for row in turns[-8:]:
            if row["role"] == "user":
                counter.update(textproc.tokenize(row["content"], max_tokens=8))
        top = [word for word, _ in counter.most_common(3)]
        topic = f"在聊：{'、'.join(top)}" if top else ""
        emotion = ""
        for row in turns[-4:]:
            if textproc.has_emotion(row["content"]):
                emotion = "，情绪有起伏"
                break
        return textproc.truncate(topic + emotion, self.settings.handoff_topic_chars)

    @staticmethod
    def _speaker(row: sqlite3.Row) -> str:
        name = str(row["speaker_name"] or "").strip()
        if name:
            return name
        return "我" if row["role"] == "assistant" else "对方"

    # ------------------------------------------------------------------
    def _render(self, packet: HandoffPacket) -> HandoffResult:
        profile = textproc.truncate(packet.profile, self.settings.handoff_profile_chars)
        loops = [textproc.truncate(x, 50) for x in packet.open_loops][:3]
        prefs = [textproc.truncate(x, 50) for x in packet.preferences][:3]
        topic = textproc.truncate(packet.topic, self.settings.handoff_topic_chars)

        recent_lines = [self._recent_line(item) for item in packet.recent]
        # 单节预算：② 900 字（先砍最旧）
        recent_lines = _cap_lines(recent_lines, self.settings.handoff_recent_chars)
        loops = _cap_lines(loops, self.settings.handoff_loops_chars)
        prefs = _cap_lines(prefs, self.settings.handoff_prefs_chars)

        text = _compose(profile, recent_lines, loops, topic, prefs)
        # 全局预算：②→③→①→④→⑤
        while len(text) > self.settings.handoff_max_chars and recent_lines:
            recent_lines.pop(0)
            text = _compose(profile, recent_lines, loops, topic, prefs)
        while len(text) > self.settings.handoff_max_chars and loops:
            loops.pop()
            text = _compose(profile, recent_lines, loops, topic, prefs)
        while len(text) > self.settings.handoff_max_chars and profile:
            profile = profile[:-20]
            text = _compose(profile, recent_lines, loops, topic, prefs)
        while len(text) > self.settings.handoff_max_chars and topic:
            topic = topic[:-10]
            text = _compose(profile, recent_lines, loops, topic, prefs)
        while len(text) > self.settings.handoff_max_chars and prefs:
            prefs.pop()
            text = _compose(profile, recent_lines, loops, topic, prefs)
        if len(text) > self.settings.handoff_max_chars:
            text = (
                text[: self.settings.handoff_max_chars - len(BLOCK_FOOTER)]
                + BLOCK_FOOTER
            )

        packet.profile = profile
        packet.open_loops = loops
        packet.preferences = prefs
        packet.topic = topic
        kept = len(recent_lines)
        packet.recent = packet.recent[len(packet.recent) - kept :] if kept else []
        return HandoffResult(
            packet=packet, chars=len(text), degraded=False, reason="ok", block=text
        )

    def _fallback(self, turns: list[sqlite3.Row]) -> HandoffResult:
        latest = turns[-20:]
        items = [
            {
                "role": r["role"],
                "speaker": self._speaker(r),
                "content": str(r["content"] or "")[:120],
                "at": r["happened_at"] or r["created_at"],
            }
            for r in latest
        ]
        lines = [self._recent_line(item) for item in items]
        if not lines:
            return self._note()
        text = _compose("", lines, [], "", [])
        if len(text) > self.settings.handoff_max_chars:
            text = text[: self.settings.handoff_max_chars - len(BLOCK_FOOTER)] + BLOCK_FOOTER
        if not text.strip():
            return self._note()
        return HandoffResult(
            packet=HandoffPacket(recent=items),
            chars=len(text),
            degraded=True,
            reason="fallback_recent",
            block=text,
        )

    def _note(self) -> HandoffResult:
        text = BLOCK_HEADER + "\n（上一段对话未能保存，请自然接住对方的话。）" + BLOCK_FOOTER
        return HandoffResult(
            packet=HandoffPacket(), chars=len(text), degraded=True,
            reason="fallback_note", block=text,
        )

    @staticmethod
    def _recent_line(item: Mapping) -> str:
        stamp = short_date(item.get("at")) or "--"
        speaker = str(item.get("speaker") or "对方")
        content = textproc.truncate(str(item.get("content") or ""), 120)
        event_date = date_only(item.get("event_at")) if item.get("event_at") else ""
        if event_date:
            content = f"{content}（事件 {event_date}）"
        return f"[{stamp}] {speaker}：{content}"


# ---------------------------------------------------------------------------
# 渲染小工具
# ---------------------------------------------------------------------------

def _compose(
    profile: str, recent: list[str], loops: list[str], topic: str, prefs: list[str]
) -> str:
    parts = [BLOCK_HEADER]
    if profile:
        parts.append(f"\n【关系与身份】\n{profile}")
    if recent:
        parts.append("\n【最近对话】\n" + "\n".join(recent))
    if loops:
        parts.append("\n【未完成事项】\n" + "\n".join(f"- {x}" for x in loops))
    if topic:
        parts.append(f"\n【当前话题】\n{topic}")
    if prefs:
        parts.append("\n【关键偏好】\n" + "\n".join(f"- {x}" for x in prefs))
    parts.append(BLOCK_FOOTER)
    return "".join(parts)


def _cap_lines(lines: list[str], cap: int) -> list[str]:
    """从最旧开始丢弃，直到该节总长 ≤ cap（保留最新）。"""
    out = list(lines)
    while out and sum(len(x) + 1 for x in out) > cap:
        out.pop(0)
    return out


def _int(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))
