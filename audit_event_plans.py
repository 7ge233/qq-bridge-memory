# -*- coding: utf-8 -*-
"""事件计划只读审计（v2.3 规则审计）。

只读：不修改数据库，只报告“同 scope 多个 planned 事件 / 已过期仍 planned”。
用于发现规则没收敛干净的残留；人工看报告后，再去控制台或回填工具处理。

用法：
  python audit_event_plans.py
  python audit_event_plans.py --minutes 60 --json
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import db_init
from config import Settings, get_settings
from timeutil import date_only, hours_ago, iso_now

_PREVIEW = 40


def run_audit(settings: Settings | None = None, *, minutes: int = 60) -> dict:
    settings = settings or get_settings()
    conn = db_init.open_db(settings.db_path)
    try:
        now = iso_now()
        cutoff = hours_ago(minutes / 60)
        rows = conn.execute(
            "SELECT id, scope_key, content, event_at, valid_until, event_status, created_at "
            "FROM memories WHERE category='event' AND event_status='planned' "
            "AND event_at IS NOT NULL AND deleted_at IS NULL "
            "AND created_at >= ? "
            "ORDER BY scope_key, event_at ASC, id ASC",
            (cutoff,),
        ).fetchall()
        stale = conn.execute(
            "SELECT id, scope_key, content, event_at, valid_until "
            "FROM memories WHERE category='event' AND event_status='planned' "
            "AND valid_until IS NOT NULL AND valid_until < ? AND deleted_at IS NULL "
            "ORDER BY id ASC",
            (now,),
        ).fetchall()
    finally:
        conn.close()

    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[str(row["scope_key"])].append(
            {
                "id": int(row["id"]),
                "event_at": row["event_at"],
                "date": date_only(row["event_at"]),
                "preview": str(row["content"] or "")[:_PREVIEW],
            }
        )

    findings: list[dict] = []
    for scope, items in groups.items():
        dates = {item["date"] for item in items}
        if len(items) > 1 and len(dates) > 1:
            findings.append(
                {
                    "kind": "multiple_plans",
                    "scope_key": scope,
                    "count": len(items),
                    "dates": sorted(d for d in dates if d),
                    "items": items,
                }
            )
    for row in stale:
        findings.append(
            {
                "kind": "stale_planned",
                "scope_key": str(row["scope_key"]),
                "count": 1,
                "items": [
                    {
                        "id": int(row["id"]),
                        "event_at": row["event_at"],
                        "valid_until": row["valid_until"],
                        "preview": str(row["content"] or "")[:_PREVIEW],
                    }
                ],
            }
        )

    return {
        "ok": True,
        "read_only": True,
        "db": str(settings.db_path),
        "window_minutes": minutes,
        "scanned_planned": len(rows),
        "scopes": len(groups),
        "multiple_plans": sum(1 for f in findings if f["kind"] == "multiple_plans"),
        "stale_planned": sum(1 for f in findings if f["kind"] == "stale_planned"),
        "findings": findings,
        "generated_at": now,
    }


def _render(report: dict) -> str:
    lines = [
        "=== 事件计划审计（只读）===",
        f"数据库        : {report['db']}",
        f"窗口          : 最近 {report['window_minutes']} 分钟",
        f"planned 条数  : {report['scanned_planned']}",
        f"多计划 scope  : {report['multiple_plans']}",
        f"过期仍 planned: {report['stale_planned']}",
    ]
    if not report["findings"]:
        lines.append("未发现异常。")
        return "\n".join(lines)
    for finding in report["findings"]:
        lines.append("")
        lines.append(f"[{finding['kind']}] scope={finding['scope_key']} count={finding['count']}")
        for item in finding.get("items", []):
            lines.append(
                f"  #{item['id']} {item.get('event_at') or ''} | {item.get('preview', '')}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="qq-bridge-memory 事件计划只读审计")
    parser.add_argument("--db", default=str(get_settings().db_path))
    parser.add_argument("--minutes", type=int, default=60)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    settings = Settings(db_path=Path(args.db))
    try:
        report = run_audit(settings, minutes=max(1, args.minutes))
    except Exception as exc:
        print(f"[FAIL] 审计失败：{exc}")
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else _render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
