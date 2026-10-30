#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qq-bridge-memory · 数据库初始化：建表、索引、FTS5、schema 版本

用法：
    python db_init.py                    # 初始化/升级 data/qq-memory.db
    python db_init.py --json             # 输出机器可读报告（供脚本/CI 用）
    python db_init.py --db X:\\tmp\\a.db   # 指定数据库路径

设计要点：
  * 幂等：可重复执行；已存在的表/索引/触发器不会被改动，新版本按 MIGRATIONS 顺序升级。
  * pragma 逐条执行后**断言实际生效**（journal_mode=WAL / synchronous=NORMAL /
    busy_timeout=3000 / foreign_keys=ON）；任一项不生效立即报错，绝不静默降级。
  * 表：memories（35 列 = 设计稿 34 列 + suspicious 列）、
    conversation_turns（v2.2 流水表）、memories_archive、conversation_turns_archive、
    schema_version（表 + PRAGMA user_version 双保险）。
  * 索引：5 个 memories 索引 + 2 个 conversation_turns 索引（与文档完全同名）。
  * FTS5：memories_fts 为 external content 表，索引 jieba 切好的 tokens 列（content/tags
    仅用于回查），由 3 个触发器自动同步，write 事务里无需手工维护索引。

数据库里的时间统一为 ISO8601 带 +08:00 偏移；created_at 的默认值只作兜底，
正常写入路径必须显式带时间。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import APP_VERSION

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 2           # 本文件能升到的最高 schema 版本

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "qq-memory.db"

TZ = timezone(timedelta(hours=8))
ISO_NOW = "(strftime('%Y-%m-%dT%H:%M:%f','now','+8 hours') || '+08:00')"

PRAGMA_STATEMENTS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA busy_timeout = 3000",
    "PRAGMA foreign_keys = ON",
)

# v1 基线列（35 列）。迁移 1 必须继续用这份历史定义，不能在旧迁移里加新列；
# 新列统一走迁移 2，保证老库和新库都能升到同一形态。
MEMORY_COLUMNS_V1: tuple[tuple[str, str], ...] = (
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("account", "TEXT NOT NULL DEFAULT ''"),
    ("scope_key", "TEXT NOT NULL"),
    ("scope_type", "TEXT NOT NULL CHECK (scope_type IN ('private','group'))"),
    ("user_id", "TEXT"),
    ("group_id", "TEXT DEFAULT ''"),
    ("speaker_id", "TEXT"),
    ("speaker_name", "TEXT"),
    ("role", "TEXT NOT NULL DEFAULT 'user'"),
    ("message_id", "TEXT"),
    ("session_id", "TEXT"),
    ("generation", "INTEGER DEFAULT 0"),
    ("content", "TEXT NOT NULL"),
    ("attachment", "TEXT"),
    ("embedding", "BLOB"),
    ("embedding_model", "TEXT"),
    ("embedding_dim", "INTEGER"),
    ("visibility", "TEXT NOT NULL DEFAULT 'private'"),
    ("fact_key", "TEXT"),
    ("fact_scope", "TEXT"),
    ("category", "TEXT DEFAULT 'event'"),
    ("importance", "REAL DEFAULT 0.5"),
    ("emotion", "REAL DEFAULT 0"),
    ("is_active", "INTEGER NOT NULL DEFAULT 1"),
    ("superseded_by", "INTEGER"),
    ("tokens", "TEXT"),
    ("tags", "TEXT"),
    ("source", "TEXT DEFAULT 'auto'"),
    ("created_at", f"TEXT NOT NULL DEFAULT {ISO_NOW}"),
    ("happened_at", "TEXT"),
    ("last_recalled_at", "TEXT"),
    ("recall_count", "INTEGER DEFAULT 0"),
    ("deleted_at", "TEXT"),
    ("schema_version", "INTEGER DEFAULT 1"),
    ("suspicious", "INTEGER NOT NULL DEFAULT 0"),
)

# v2：事件时效（event_at / valid_until / event_status）
EVENT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("event_at", "TEXT"),
    ("valid_until", "TEXT"),
    ("event_status", "TEXT"),
)

# 当前完整列（35 + 3 = 38），供 store / writer / 报告使用。
MEMORY_COLUMNS: tuple[tuple[str, str], ...] = MEMORY_COLUMNS_V1 + EVENT_COLUMNS

# conversation_turns 列定义（设计稿 3.x 流水表）
TURN_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("account", "TEXT NOT NULL"),
    ("scope_key", "TEXT NOT NULL"),
    ("scope_type", "TEXT NOT NULL"),
    ("session_id", "TEXT"),
    ("generation", "INTEGER DEFAULT 0"),
    ("seq", "INTEGER NOT NULL"),
    ("role", "TEXT NOT NULL"),
    ("speaker_id", "TEXT"),
    ("speaker_name", "TEXT"),
    ("message_id", "TEXT"),
    ("content", "TEXT NOT NULL"),
    ("happened_at", "TEXT"),
    ("created_at", f"TEXT NOT NULL DEFAULT {ISO_NOW}"),
)

INDEXES: tuple[tuple[str, str], ...] = (
    (
        "ux_mem_msg",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_mem_msg ON memories(account, message_id, role) "
        "WHERE message_id IS NOT NULL",
    ),
    (
        "ix_mem_scope",
        "CREATE INDEX IF NOT EXISTS ix_mem_scope "
        "ON memories(account, scope_type, user_id, group_id, is_active)",
    ),
    (
        "ix_mem_fact",
        "CREATE INDEX IF NOT EXISTS ix_mem_fact "
        "ON memories(account, fact_scope, fact_key, is_active)",
    ),
    (
        "ix_mem_time",
        "CREATE INDEX IF NOT EXISTS ix_mem_time ON memories(happened_at, created_at)",
    ),
    (
        "ix_mem_active",
        "CREATE INDEX IF NOT EXISTS ix_mem_active ON memories(is_active, visibility)",
    ),
    (
        "ix_turns_scope",
        "CREATE INDEX IF NOT EXISTS ix_turns_scope ON conversation_turns(scope_key, id DESC)",
    ),
    (
        "ux_turns_msg",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_turns_msg "
        "ON conversation_turns(account, message_id, role) WHERE message_id IS NOT NULL",
    ),
)

TRIGGER_NAMES = ("memories_ai", "memories_au", "memories_ad")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def iso_now() -> str:
    """当前时间，ISO8601 带 +08:00（与库内时间格式一致）。"""
    return datetime.now(TZ).isoformat(timespec="milliseconds")


def _columns_sql(columns: tuple[tuple[str, str], ...], *, indent: str = "    ") -> str:
    return ",\n".join(f"{indent}{name} {ddl}" for name, ddl in columns)


def _create_table_sql(name: str, columns: tuple[tuple[str, str], ...]) -> str:
    return f"CREATE TABLE IF NOT EXISTS {name} (\n{_columns_sql(columns)}\n)"


def _archive_columns(columns: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    """归档表：主键去掉 AUTOINCREMENT（保持原 id），追加归档元数据列。"""
    out: list[tuple[str, str]] = []
    for name, ddl in columns:
        if name == "id":
            out.append((name, "INTEGER PRIMARY KEY"))
        else:
            out.append((name, ddl))
    out.append(("archived_at", "TEXT NOT NULL"))
    out.append(("archive_reason", "TEXT DEFAULT ''"))
    return tuple(out)


DDL_SCHEMA_VERSION = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    app_version TEXT NOT NULL DEFAULT ''
)"""

DDL_MEMORIES_V1 = _create_table_sql("memories", MEMORY_COLUMNS_V1)
DDL_MEMORIES_ARCHIVE_V1 = _create_table_sql(
    "memories_archive", _archive_columns(MEMORY_COLUMNS_V1)
)
DDL_CONVERSATION_TURNS = _create_table_sql("conversation_turns", TURN_COLUMNS)
DDL_TURNS_ARCHIVE = _create_table_sql(
    "conversation_turns_archive", _archive_columns(TURN_COLUMNS)
)

INDEX_EVENT = (
    "ix_mem_event",
    "CREATE INDEX IF NOT EXISTS ix_mem_event ON memories(event_status, valid_until)",
)

DDL_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    tokens, content, tags,
    content='memories', content_rowid='id',
    tokenize='unicode61'
)"""

# external content FTS5 的同步触发器：delete 必须带旧值，update 先 delete 再 insert。
# UPDATE 只在索引列被改动时触发，避免 recall_count/last_recalled_at 的更新白白重建索引。
# 注意：sqlite3.execute() 一次只能执行一条语句，所以触发器必须逐条存放。
TRIGGER_STATEMENTS: tuple[str, ...] = (
    """
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, tokens, content, tags)
    VALUES (new.id, coalesce(new.tokens, ''), new.content, coalesce(new.tags, ''));
END""",
    """
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF tokens, content, tags ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, tokens, content, tags)
    VALUES ('delete', old.id, coalesce(old.tokens, ''), old.content, coalesce(old.tags, ''));
    INSERT INTO memories_fts(rowid, tokens, content, tags)
    VALUES (new.id, coalesce(new.tokens, ''), new.content, coalesce(new.tags, ''));
END""",
    """
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, tokens, content, tags)
    VALUES ('delete', old.id, coalesce(old.tokens, ''), old.content, coalesce(old.tags, ''));
END""",
)

# 版本 → 语句。已发布的迁移禁止修改；新改动一律追加新版本并抬高 SCHEMA_VERSION。
MIGRATIONS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (
        1,
        "v2.2 基线：memories(35 列) + conversation_turns + 归档表 + 7 索引 + FTS5 + 触发器",
        (
            DDL_MEMORIES_V1,
            DDL_MEMORIES_ARCHIVE_V1,
            DDL_CONVERSATION_TURNS,
            DDL_TURNS_ARCHIVE,
            *(sql for _, sql in INDEXES),
            DDL_FTS,
            *TRIGGER_STATEMENTS,
        ),
    ),
    (
        2,
        "v2.3 事件时效：memories/归档表新增 event_at/valid_until/event_status + ix_mem_event",
        (
            "ALTER TABLE memories ADD COLUMN event_at TEXT",
            "ALTER TABLE memories ADD COLUMN valid_until TEXT",
            "ALTER TABLE memories ADD COLUMN event_status TEXT",
            "ALTER TABLE memories_archive ADD COLUMN event_at TEXT",
            "ALTER TABLE memories_archive ADD COLUMN valid_until TEXT",
            "ALTER TABLE memories_archive ADD COLUMN event_status TEXT",
            INDEX_EVENT[1],
        ),
    ),
)


# ---------------------------------------------------------------------------
# 连接 / pragma
# ---------------------------------------------------------------------------

def connect(db_path: str | Path, *, timeout: float = 3.0) -> sqlite3.Connection:
    """打开连接：autocommit 模式（isolation_level=None），后续显式 BEGIN。

    每个线程必须各自 connect；不要把连接跨线程共享。
    """
    conn = sqlite3.connect(str(db_path), timeout=timeout, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def read_pragmas(conn: sqlite3.Connection) -> dict[str, int | str]:
    return {
        "journal_mode": str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
        "synchronous": int(conn.execute("PRAGMA synchronous").fetchone()[0]),
        "busy_timeout": int(conn.execute("PRAGMA busy_timeout").fetchone()[0]),
        "foreign_keys": int(conn.execute("PRAGMA foreign_keys").fetchone()[0]),
    }


def apply_pragmas(conn: sqlite3.Connection) -> dict[str, int | str]:
    """逐条执行 pragma 并断言生效，返回实际值。"""
    for stmt in PRAGMA_STATEMENTS:
        conn.execute(stmt)
    actual = read_pragmas(conn)
    problems: list[str] = []
    if actual["journal_mode"] != "wal":
        problems.append(f"journal_mode={actual['journal_mode']}（期望 wal）")
    if actual["synchronous"] != 1:
        problems.append(f"synchronous={actual['synchronous']}（期望 1=NORMAL）")
    if actual["busy_timeout"] != 3000:
        problems.append(f"busy_timeout={actual['busy_timeout']}（期望 3000）")
    if actual["foreign_keys"] != 1:
        problems.append(f"foreign_keys={actual['foreign_keys']}（期望 1）")
    if problems:
        raise RuntimeError("pragma 未生效：" + "；".join(problems))
    return actual


def open_db(db_path: str | Path) -> sqlite3.Connection:
    """打开并配置好 pragma 的连接（供其它模块复用）。"""
    conn = connect(db_path)
    apply_pragmas(conn)
    return conn


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------

def migrate(conn: sqlite3.Connection) -> list[int]:
    """幂等迁移：schema_version 表 + PRAGMA user_version 双记录，返回本次应用的版本号。"""
    conn.execute(DDL_SCHEMA_VERSION)
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"数据库 schema_version={current} 高于本程序支持的 {SCHEMA_VERSION}，"
            "拒绝降级；请升级代码或改用备份库。"
        )

    applied: list[int] = []
    for version, description, statements in MIGRATIONS:
        if version <= current:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in statements:
                conn.execute(stmt)
            conn.execute(
                "INSERT OR REPLACE INTO schema_version"
                "(version, applied_at, description, app_version) VALUES (?,?,?,?)",
                (version, iso_now(), description, APP_VERSION),
            )
            conn.execute(f"PRAGMA user_version = {int(version)}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        applied.append(version)
    return applied


def ensure_fts(conn: sqlite3.Connection) -> bool:
    """FTS 与 content 表一致性自愈；返回是否执行了 rebuild。"""
    try:
        conn.execute(
            "INSERT INTO memories_fts(memories_fts, rank) VALUES('integrity-check', 1)"
        )
        return False
    except sqlite3.DatabaseError:
        pass
    n = int(conn.execute("SELECT count(*) FROM memories").fetchone()[0])
    if n == 0:
        return False
    conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
    return True


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def _rows(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


def _table_info(conn: sqlite3.Connection, table: str) -> list[dict[str, object]]:
    return [dict(row) for row in conn.execute(f"PRAGMA table_info({table})")]


def collect_report(conn: sqlite3.Connection, db_path: Path, migrated: list[int]) -> dict:
    tables = (
        "memories",
        "conversation_turns",
        "memories_archive",
        "conversation_turns_archive",
        "schema_version",
    )
    table_report = {
        name: {"columns": len(_table_info(conn, name)), "rows": _rows(conn, name)}
        for name in tables
    }
    indexes = [
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
    ]
    triggers = [
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name"
        )
    ]
    fts_exists = (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories_fts'"
        ).fetchone()
        is not None
    )
    db_paths = {
        "main": db_path,
        "wal": db_path.with_name(db_path.name + "-wal"),
        "shm": db_path.with_name(db_path.name + "-shm"),
    }
    sizes = {
        key: (p.stat().st_size if p.exists() else 0) for key, p in db_paths.items()
    }
    return {
        "ok": True,
        "app_version": APP_VERSION,
        "db_path": str(db_path),
        "size_bytes": sizes["main"],
        "wal_bytes": sizes["wal"],
        "user_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
        "schema_version": max(
            [int(r[0]) for r in conn.execute("SELECT version FROM schema_version")] or [0]
        ),
        "migrated": migrated,
        "pragmas": read_pragmas(conn),
        "tables": table_report,
        "indexes": indexes,
        "triggers": triggers,
        "fts5": {"name": "memories_fts", "exists": fts_exists},
    }


def render_text(report: dict) -> str:
    p = report["pragmas"]
    lines: list[str] = []
    lines.append("=== qq-bridge-memory 数据库初始化 ===")
    lines.append(f"数据库      : {report['db_path']}")
    lines.append(
        f"文件大小    : {report['size_bytes']:,} 字节"
        f"（-wal {report['wal_bytes']:,} 字节）"
    )
    lines.append(
        f"schema 版本 : {report['schema_version']}（PRAGMA user_version="
        f"{report['user_version']}，程序支持 {SCHEMA_VERSION}）"
    )
    lines.append(f"本次迁移    : {report['migrated'] or '无（已是目标版本，幂等）'}")
    lines.append("")
    lines.append("--- PRAGMA 实际值（逐条断言通过）---")
    lines.append(f"journal_mode  = {p['journal_mode']}")
    lines.append(f"synchronous   = {p['synchronous']}  (NORMAL)")
    lines.append(f"busy_timeout  = {p['busy_timeout']} ms")
    lines.append(f"foreign_keys  = {p['foreign_keys']}  (ON)")
    lines.append("")
    lines.append("--- 表 ---")
    for name, info in report["tables"].items():
        lines.append(f"{name:<26} {info['columns']:>3} 列 {info['rows']:>8} 行")
    lines.append("")
    lines.append("--- 索引 ---")
    lines.append(", ".join(report["indexes"]) or "(无)")
    lines.append("")
    lines.append("--- FTS5 / 触发器 ---")
    lines.append(
        f"memories_fts: {'存在' if report['fts5']['exists'] else '缺失'}"
        f"（tokenize='unicode61'，索引 jieba tokens 列）"
    )
    lines.append("触发器: " + ", ".join(report["triggers"]))
    lines.append("")
    lines.append("[OK] 初始化完成；本脚本幂等，可重复执行。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="qq-bridge-memory 数据库初始化（幂等建表 + WAL + FTS5）"
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("QQ_MEMORY_DB", str(DEFAULT_DB_PATH)),
        help="数据库路径（默认 data/qq-memory.db，或环境变量 QQ_MEMORY_DB）",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告")
    return parser.parse_args(argv)


def init_db(db_path: str | Path) -> dict:
    db_path = Path(db_path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(db_path)
    try:
        migrated = migrate(conn)
        ensure_fts(conn)
        # 关闭前把 WAL 合回主文件，避免留下超长 -wal（下次启动会自动 checkpoint）
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return collect_report(conn, db_path, migrated)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    args = parse_args(argv)
    try:
        report = init_db(args.db)
    except Exception as exc:  # 初始化失败必须显式失败，不能带病启动
        print(f"[FAIL] 数据库初始化失败：{exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
