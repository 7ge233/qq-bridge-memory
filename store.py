# -*- coding: utf-8 -*-
"""数据访问层：scope_key 规范化、memories / conversation_turns 的读写小工具。

所有 SQL 一律参数化（6.1）；调用方负责事务边界（writer 用 BEGIN IMMEDIATE）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping

from config import Settings, get_settings
from db_init import MEMORY_COLUMNS, open_db
from timeutil import iso_now

_MEM_COLUMNS = {name for name, _ in MEMORY_COLUMNS}
_REQUIRED_MEM_FIELDS = ("account", "scope_key", "scope_type", "content", "role")
_VALID_SCOPE_TYPES = ("private", "group")
_VALID_ROLES = ("user", "assistant")


# ---------------------------------------------------------------------------
# scope
# ---------------------------------------------------------------------------

def parse_scope(scope: Mapping | None) -> tuple[str, str, str]:
    """校验 {type,user_id,group_id}，返回 (scope_type, user_id, group_id)。"""
    if not isinstance(scope, Mapping):
        raise ValueError("scope 必须是对象")
    scope_type = str(scope.get("type") or "").strip().lower()
    if scope_type not in _VALID_SCOPE_TYPES:
        raise ValueError("scope.type 必须是 private 或 group")
    user_id = str(scope.get("user_id") or "").strip()
    group_id = str(scope.get("group_id") or "").strip()
    if scope_type == "private" and not user_id:
        raise ValueError("private scope 缺少 user_id")
    if scope_type == "group" and not group_id:
        raise ValueError("group scope 缺少 group_id")
    return scope_type, user_id, group_id


def scope_key(account: str, scope_type: str, user_id: str = "", group_id: str = "") -> str:
    """qq:<account>:private:<uid> / qq:<account>:group:<gid>（1.3 术语表）。"""
    if scope_type == "private":
        return f"qq:{account}:private:{user_id}"
    if scope_type == "group":
        return f"qq:{account}:group:{group_id}"
    raise ValueError(f"未知 scope_type: {scope_type}")


def scope_label(scope_type: str, user_id: str = "", group_id: str = "") -> str:
    return "私聊" if scope_type == "private" else f"群{group_id}"


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------

def open_conn(settings: Settings | None = None) -> sqlite3.Connection:
    settings = settings or get_settings()
    return open_db(settings.db_path)


# ---------------------------------------------------------------------------
# memories
# ---------------------------------------------------------------------------

def insert_memory(conn: sqlite3.Connection, record: Mapping[str, object]) -> int:
    for field in _REQUIRED_MEM_FIELDS:
        if record.get(field) in (None, ""):
            raise ValueError(f"memories 缺少必填字段: {field}")
    values = {key: val for key, val in record.items() if key in _MEM_COLUMNS}
    values.setdefault("created_at", iso_now())
    columns = list(values)
    sql = (
        f"INSERT INTO memories ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})"
    )
    cur = conn.execute(sql, [values[col] for col in columns])
    return int(cur.lastrowid)


def find_memory_by_message(
    conn: sqlite3.Connection, account: str, message_id: str | None, role: str
) -> sqlite3.Row | None:
    if not message_id:
        return None
    return conn.execute(
        "SELECT id, content FROM memories WHERE account=? AND message_id=? AND role=?",
        (account, message_id, role),
    ).fetchone()


def mark_superseded(
    conn: sqlite3.Connection,
    *,
    account: str,
    fact_scope: str,
    fact_key: str,
    new_id: int,
) -> list[int]:
    """同事务内的 fact_key 取代（3.6）：旧记录 is_active=0、superseded_by=新 id。"""
    rows = conn.execute(
        "SELECT id FROM memories WHERE account=? AND fact_scope=? AND fact_key=? "
        "AND is_active=1 AND deleted_at IS NULL AND id<>?",
        (account, fact_scope, fact_key, new_id),
    ).fetchall()
    old_ids = [int(row[0]) for row in rows]
    for old_id in old_ids:
        conn.execute(
            "UPDATE memories SET is_active=0, superseded_by=? WHERE id=?",
            (new_id, old_id),
        )
    return old_ids


def revoke_supersede(conn: sqlite3.Connection, old_id: int) -> list[int]:
    """撤销取代（5.2）：传「被取代的旧条 id」。

    旧条恢复 is_active=1、清空 superseded_by；同时把取代它的新条置 is_active=0，
    保证同一 fact_key 仍然只有一条生效记录。
    """
    row = conn.execute(
        "SELECT superseded_by FROM memories WHERE id=?", (old_id,)
    ).fetchone()
    if row is None or row["superseded_by"] is None:
        return []
    new_id = int(row["superseded_by"])
    conn.execute(
        "UPDATE memories SET is_active=1, superseded_by=NULL WHERE id=?", (old_id,)
    )
    conn.execute("UPDATE memories SET is_active=0 WHERE id=?", (new_id,))
    return [int(old_id)]


def touch_recalled(conn: sqlite3.Connection, ids: Iterable[int]) -> None:
    ids = [int(i) for i in ids]
    if not ids:
        return
    now = iso_now()
    conn.executemany(
        "UPDATE memories SET last_recalled_at=?, "
        "recall_count=COALESCE(recall_count, 0)+1 WHERE id=?",
        [(now, i) for i in ids],
    )


def count_memories(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT count(*) FROM memories").fetchone()[0])


# ---------------------------------------------------------------------------
# conversation_turns
# ---------------------------------------------------------------------------

def next_turn_seq(conn: sqlite3.Connection, scope_key_value: str, session_id: str | None) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0)+1 FROM conversation_turns "
        "WHERE scope_key=? AND session_id IS ?",
        (scope_key_value, session_id),
    ).fetchone()
    return int(row[0])


def insert_turn(conn: sqlite3.Connection, record: Mapping[str, object]) -> int | None:
    fields = {
        key: record.get(key)
        for key in (
            "account", "scope_key", "scope_type", "session_id", "generation", "seq",
            "role", "speaker_id", "speaker_name", "message_id", "content",
            "happened_at", "created_at",
        )
    }
    fields.setdefault("created_at", iso_now())
    try:
        cur = conn.execute(
            "INSERT INTO conversation_turns "
            "(account, scope_key, scope_type, session_id, generation, seq, role,"
            " speaker_id, speaker_name, message_id, content, happened_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                fields["account"], fields["scope_key"], fields["scope_type"],
                fields["session_id"], fields.get("generation") or 0, fields["seq"],
                fields["role"], fields.get("speaker_id"), fields.get("speaker_name"),
                fields.get("message_id"), fields["content"], fields.get("happened_at"),
                fields["created_at"],
            ),
        )
        return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        # 唯一索引 (account,message_id,role) 命中 → 流水已存在，不重复写
        return None


def recent_turns(
    conn: sqlite3.Connection, scope_key_value: str, limit: int
) -> list[sqlite3.Row]:
    limit = max(0, int(limit))
    if limit == 0:
        return []
    rows = conn.execute(
        "SELECT id, role, speaker_name, content, happened_at, created_at, generation, "
        "message_id FROM conversation_turns WHERE scope_key=? ORDER BY id DESC LIMIT ?",
        (scope_key_value, limit),
    ).fetchall()
    return list(reversed(rows))


def latest_generation(conn: sqlite3.Connection, scope_key_value: str) -> int:
    row = conn.execute(
        "SELECT MAX(g) FROM ("
        "SELECT generation AS g FROM conversation_turns WHERE scope_key=? "
        "UNION ALL SELECT generation AS g FROM memories WHERE scope_key=?)",
        (scope_key_value, scope_key_value),
    ).fetchone()
    return int(row[0] or 0)
