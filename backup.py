# -*- coding: utf-8 -*-
"""备份脚本（6.6）：VACUUM INTO 每日备份，保留最近 N 份。

用法：
    .venv\\Scripts\\python.exe backup.py            # 备份到 backup/，保留 7 份
    .venv\\Scripts\\python.exe backup.py --keep 3
    .venv\\Scripts\\python.exe backup.py --list

计划任务（03:00）需要你自行在 Windows 任务计划程序里创建，本脚本不会安装任何系统级任务。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from config import Settings, get_settings
from timeutil import TZ


def backup_now(settings: Settings | None = None, *, keep: int = 7) -> Path | None:
    settings = settings or get_settings()
    source = settings.db_path
    if not source.exists():
        return None
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(TZ).strftime("%Y%m%d")
    target = settings.backup_dir / f"qq-memory-{stamp}.db"
    tmp = target.with_suffix(".db.tmp")
    for path in (tmp,):
        if path.exists():
            path.unlink()

    conn = sqlite3.connect(str(source), timeout=10)
    try:
        conn.execute("VACUUM INTO ?", (str(tmp),))
    finally:
        conn.close()

    check = sqlite3.connect(str(tmp))
    try:
        status = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if status != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"备份完整性校验失败：{status}")

    os.replace(tmp, target)
    _prune(settings.backup_dir, keep)
    return target


def _prune(backup_dir: Path, keep: int) -> list[Path]:
    files = sorted(backup_dir.glob("qq-memory-*.db"), key=lambda p: p.name, reverse=True)
    removed: list[Path] = []
    for path in files[max(1, keep):]:
        try:
            path.unlink()
            removed.append(path)
        except OSError:
            pass
    return removed


def list_backups(settings: Settings | None = None) -> list[Path]:
    settings = settings or get_settings()
    if not settings.backup_dir.exists():
        return []
    return sorted(settings.backup_dir.glob("qq-memory-*.db"), key=lambda p: p.name, reverse=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="qq-bridge-memory SQLite 备份（VACUUM INTO）")
    parser.add_argument("--db", default=str(get_settings().db_path), help="数据库路径")
    parser.add_argument("--backup-dir", default=str(get_settings().backup_dir), help="备份目录")
    parser.add_argument("--keep", type=int, default=7, help="保留份数（默认 7）")
    parser.add_argument("--list", action="store_true", help="只列出已有备份")
    args = parser.parse_args(argv)

    settings = Settings(db_path=Path(args.db), backup_dir=Path(args.backup_dir))
    if args.list:
        files = list_backups(settings)
        print("\n".join(str(path) for path in files) or "(无备份)")
        return 0
    try:
        target = backup_now(settings, keep=args.keep)
    except Exception as exc:
        print(f"[FAIL] 备份失败：{exc}", file=sys.stderr)
        return 1
    if target is None:
        print("[SKIP] 数据库不存在")
        return 0
    print(f"[OK] {target}（{target.stat().st_size:,} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
