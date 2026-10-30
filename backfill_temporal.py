# -*- coding: utf-8 -*-
"""事件时效回填（v2.3 Phase 1.5，一次性维护工具）。

用途：把 v2.3 之前写入、`event_at IS NULL` 的历史事件补上绝对时间，
让“旧的明天/下周X”也能按有效期过滤。

安全约束：
  * 默认 dry-run，不写库；`--apply` 才写；
  * 只处理 `role='user'` + `category='event'` + `event_at IS NULL`；
  * 只回填能确定解析的记录，模糊表达保持 NULL；
  * `--apply` 默认先 VACUUM INTO 备份（可用 --no-backup 关闭）；
  * 不改 fact/preference/assistant，不删除任何记录；
  * 供 CLI 与控制台复用：`run_backfill(settings, apply=...)`。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import db_init
from config import Settings, get_settings
from temporal import parse_event_time
from timeutil import iso_now, parse_iso


def _has_event_columns(conn: sqlite3.Connection) -> bool:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
    return {"event_at", "valid_until", "event_status"} <= cols


def _candidate_rows(conn: sqlite3.Connection, limit: int | None) -> list[sqlite3.Row]:
    sql = (
        "SELECT id, account, scope_key, content, happened_at, created_at, event_at "
        "FROM memories WHERE role='user' AND category='event' AND event_at IS NULL "
        "AND event_status IS NULL AND deleted_at IS NULL ORDER BY id ASC"
    )
    params: list[object] = []
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return conn.execute(sql, params).fetchall()


def run_backfill(
    settings: Settings | None = None,
    *,
    apply: bool = False,
    limit: int | None = None,
    backup: bool = True,
) -> dict:
    """扫描并（可选）回填旧事件。返回可 JSON 序列化的报告。"""
    settings = settings or get_settings()
    db_path = settings.db_path
    if not db_path.exists():
        raise FileNotFoundError(f"数据库不存在：{db_path}")

    conn = db_init.open_db(db_path)
    try:
        if not _has_event_columns(conn):
            raise RuntimeError(
                "数据库还是 schema v1，缺少 event_at/valid_until/event_status；"
                "请先运行 db_init.py 或启动 sidecar 完成 v2 迁移。"
            )
        rows = _candidate_rows(conn, limit)
        planned: list[tuple[str, str, int]] = []
        ambiguous = 0
        missing_base = 0
        samples: list[dict] = []

        for row in rows:
            content = str(row["content"] or "").strip()
            base = parse_iso(row["happened_at"]) or parse_iso(row["created_at"])
            if base is None:
                missing_base += 1
                continue
            try:
                parsed = parse_event_time(content, base)
            except Exception:
                parsed = None
            if parsed is None:
                ambiguous += 1
                continue
            planned.append((parsed.event_at, parsed.valid_until, int(row["id"])))
            if len(samples) < 10:
                samples.append(
                    {
                        "id": int(row["id"]),
                        "event_at": parsed.event_at,
                        "valid_until": parsed.valid_until,
                        "precision": parsed.precision,
                        "preview": content[:40],
                    }
                )

        backup_path = None
        updated = 0
        if apply and planned:
            if backup:
                from backup import backup_now

                backup_path = backup_now(settings, keep=7)
            conn.execute("BEGIN IMMEDIATE")
            try:
                for event_at, valid_until, memory_id in planned:
                    cur = conn.execute(
                        "UPDATE memories SET event_at=?, valid_until=?, event_status='planned' "
                        "WHERE id=? AND event_at IS NULL",
                        (event_at, valid_until, memory_id),
                    )
                    updated += cur.rowcount
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        return {
            "ok": True,
            "db": str(db_path),
            "dry_run": not apply,
            "candidates": len(rows),
            "parsed": len(planned),
            "would_update": len(planned) if not apply else updated,
            "updated": updated,
            "ambiguous": ambiguous,
            "missing_base": missing_base,
            "backup": str(backup_path) if backup_path else None,
            "samples": samples,
            "generated_at": iso_now(),
        }
    finally:
        conn.close()


def _render_text(report: dict) -> str:
    lines = [
        "=== 事件时效回填 ===",
        f"数据库        : {report['db']}",
        f"模式          : {'dry-run（不写库）' if report['dry_run'] else 'apply（已写入）'}",
        f"候选记录      : {report['candidates']}",
        f"可解析        : {report['parsed']}",
        f"已更新        : {report['updated']}",
        f"模糊跳过      : {report['ambiguous']}",
        f"缺少时间基准  : {report['missing_base']}",
    ]
    if report.get("backup"):
        lines.append(f"备份          : {report['backup']}")
    if report["samples"]:
        lines.append("--- 样例（前 10 条，仅预览 40 字）---")
        for item in report["samples"]:
            lines.append(
                f"#{item['id']} {item['event_at'][:10]} "
                f"{item['precision']} | {item['preview']}"
            )
    if report["dry_run"]:
        lines.append("")
        lines.append("确认无误后加 --apply 落库；落库前默认自动备份。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="qq-bridge-memory 事件时效回填")
    parser.add_argument("--db", default=str(get_settings().db_path))
    parser.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    parser.add_argument("--limit", type=int, default=None, help="最多处理多少条候选")
    parser.add_argument("--no-backup", action="store_true", help="apply 时不自动备份（不推荐）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告")
    args = parser.parse_args(argv)

    settings = Settings(db_path=Path(args.db))
    try:
        report = run_backfill(
            settings,
            apply=args.apply,
            limit=args.limit,
            backup=not args.no_backup,
        )
    except Exception as exc:
        print(f"[FAIL] 回填失败：{exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(_render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
