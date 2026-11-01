# -*- coding: utf-8 -*-
"""词典档位迁移：影子列 + 游标续跑 + 启动时原子切换。

为什么这么绕
------------
换词典意味着全库 tokens 要用**新词典**重新切一遍。若边切边覆盖 tokens，
迁移期间"查询用旧词典、存储用新词典"，检索质量会掉；中途断电还会留下
一半新一半旧的库。

所以拆成两步：

  1) 迁移（运行中，可随时中断）：结果写 tokens_next，tokens 不动。
     检索照旧，断电只丢当前这一批（最大 200 行）。
  2) 切换（启动时，一个事务）：tokens ⇐ tokens_next，然后 FTS rebuild。
     此时进程已加载目标词典，不存在错配窗口；事务原子，要么全换要么没换。

状态机（app_meta.migrate.state）

    idle --start--> running --run--> ready --重启并 swap--> done
                      |  ^
                   pause  resume
                      v  |
                    paused
                      |
                   cancel --> idle（清空 tokens_next）

命令行
------
    python migrate.py status
    python migrate.py start 3w        # 开始迁移到 3 万词档
    python migrate.py run             # 跑到完成（可中断，再跑接着来）
    python migrate.py cancel
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping

import jieba

import textproc

PROJECT_ROOT = Path(__file__).resolve().parent
DICT_DIR = PROJECT_ROOT / "resources" / "dict"
DICT_3W = DICT_DIR / "dict-30k.txt"

# 档位名 -> 词条数；None 表示 jieba 自带词典
TIER_WORDS: dict[str, int | None] = {"full": None, "3w": 30000}
DEFAULT_TIER = "full"

# 单行重算耗时：Windows 实测 0.145 ms/行（1.2 万行 1.74 s），
# 取 4 倍余量覆盖服务器上的慢磁盘与小核数。
SEC_PER_ROW = 0.0006
BACKUP_OVERHEAD_S = 3.0

META_STATE = "migrate.state"
META_FROM = "migrate.from_tier"
META_TO = "migrate.to_tier"
META_CURSOR = "migrate.cursor"
META_TOTAL = "migrate.total"
META_STARTED = "migrate.started_at"
META_FINISHED = "migrate.finished_at"
META_SWAPPED = "migrate.swapped_at"

STATES = ("idle", "running", "paused", "ready", "done", "failed")


class MigrationError(RuntimeError):
    """迁移前状态不允许（例如已经有未完成的迁移）。"""


# ---------------------------------------------------------------------------
# schema：app_meta + tokens_next
# ---------------------------------------------------------------------------

def ensure_schema(conn: sqlite3.Connection) -> None:
    """建 app_meta、补 tokens_next 列；幂等。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS app_meta ("
        " key TEXT PRIMARY KEY,"
        " value TEXT NOT NULL,"
        " updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now','+8 hours') || '+08:00')"
        ")"
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
    if "tokens_next" not in cols:
        conn.execute("ALTER TABLE memories ADD COLUMN tokens_next TEXT")
    conn.commit()


def meta_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
    return default if row is None else str(row[0])


def meta_set(conn: sqlite3.Connection, key: str, value: object) -> None:
    conn.execute(
        "INSERT INTO app_meta(key, value) VALUES(?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
        " updated_at = (strftime('%Y-%m-%dT%H:%M:%f','now','+8 hours') || '+08:00')",
        (key, "" if value is None else str(value)),
    )


def _delete_meta(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM app_meta WHERE key = ?", (key,))


@contextmanager
def _memory_temp(conn: sqlite3.Connection) -> Iterator[None]:
    """大事务（全表 UPDATE / FTS rebuild）期间把临时存储放内存。

    实测：12k 行的全表 UPDATE 在 temp_store=DEFAULT 时会报
    "unable to open database file"（SQLite 要落临时文件），改成 MEMORY 后正常。
    迁移行的 tokens 总量很小（12k × ~50B），放内存没有压力。
    """
    try:
        previous = int(conn.execute("PRAGMA temp_store").fetchone()[0])
    except (sqlite3.Error, TypeError):
        previous = 0
    if previous != 2:
        conn.execute("PRAGMA temp_store = MEMORY")
    try:
        yield
    finally:
        if previous != 2:
            try:
                conn.execute(f"PRAGMA temp_store = {previous}")
            except sqlite3.Error:
                pass


# ---------------------------------------------------------------------------
# 词典
# ---------------------------------------------------------------------------

def _jieba_default_dict() -> Path:
    return Path(jieba.__file__).resolve().parent / "dict.txt"


def build_trimmed_dict(words: int, dest: Path = DICT_3W) -> Path:
    """从 jieba 全量词典里取词频最高的 N 条，写成新词典（按词频降序）。

    注意必须先按词频排序再截断：jieba 的 dict.txt 本身不是按词频排的，
    直接取前 N 行会丢掉高频词（实测分词一致率只有 7%）。
    """
    source = _jieba_default_dict()
    if not source.exists():
        raise MigrationError(f"找不到 jieba 词典：{source}")
    rows: list[tuple[int, str]] = []
    with source.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                freq = int(parts[1])
            except ValueError:
                continue
            rows.append((freq, line))
    rows.sort(key=lambda item: (-item[0], item[1]))
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        for _, line in rows[:words]:
            fh.write(line + "\n")
    tmp.replace(dest)
    return dest


def tier_dict_path(tier: str, *, build: bool = True) -> Path | None:
    """档位 -> 词典路径；full 返回 None 表示用 jieba 自带词典。"""
    name = (tier or DEFAULT_TIER).strip().lower()
    if name in ("", "full", "base", "default"):
        return None
    if name in ("3w", "30k", "30k-w"):
        if not DICT_3W.exists():
            if not build:
                raise MigrationError("3 万词词典还没生成，先跑：python migrate.py build-3w")
            build_trimmed_dict(TIER_WORDS["3w"] or 30000)
        return DICT_3W
    path = Path(name)
    if path.exists():
        return path
    raise MigrationError(f"未知档位或词典路径不存在：{tier}")


def build_tokenizer(dict_path: Path | None) -> "jieba.Tokenizer":
    """独立 Tokenizer：不与主进程的全局 jieba 共享状态，但词表一致。"""
    tokenizer = jieba.Tokenizer() if dict_path is None else jieba.Tokenizer(dictionary=str(dict_path))
    try:
        import slang

        slang.apply(tokenizer)
    except Exception:  # noqa: BLE001 - 词表缺失时按基础词典切
        pass
    return tokenizer


def tokens_for(tokenizer: "jieba.Tokenizer", content: str | None) -> str:
    return textproc.tokens_text(textproc.tokenize_with(tokenizer.lcut, content))


# ---------------------------------------------------------------------------
# 档位来源
# ---------------------------------------------------------------------------

def configured_tier() -> str:
    """当前进程该用哪个档位：env > db-profiles.json 里的 jieba_dict > full。"""
    env = os.environ.get("QQ_MEMORY_JIEBA_DICT", "").strip()
    if env:
        return env
    try:
        import profiles

        return str(profiles.entry_tier() or DEFAULT_TIER)
    except Exception:  # noqa: BLE001 - 读不到就按默认档
        return DEFAULT_TIER


def apply_tier_env(tier: str | None = None) -> str:
    """把档位翻译成 QQ_MEMORY_JIEBA_DICT，供 textproc 初始化时使用。

    只在 env 未显式设置时生效（env 永远最高优先级）。
    """
    if os.environ.get("QQ_MEMORY_JIEBA_DICT", "").strip():
        return os.environ["QQ_MEMORY_JIEBA_DICT"].strip()
    name = tier or configured_tier()
    path = tier_dict_path(name) if name.lower() not in ("", "full") else None
    if path is not None:
        os.environ["QQ_MEMORY_JIEBA_DICT"] = str(path)
        return str(path)
    return "full"


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------

def _count_pending(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM memories WHERE tokens_next IS NULL").fetchone()[0])


def _count_total(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])


def status(conn: sqlite3.Connection) -> dict:
    state = meta_get(conn, META_STATE, "idle") or "idle"
    total = int(meta_get(conn, META_TOTAL, "0") or 0)
    pending = _count_pending(conn)
    done = max(0, total - pending)
    return {
        "state": state,
        "from_tier": meta_get(conn, META_FROM),
        "to_tier": meta_get(conn, META_TO),
        "cursor": int(meta_get(conn, META_CURSOR, "0") or 0),
        "total": total,
        "pending": pending,
        "done": done,
        "percent": round(done * 100.0 / total, 1) if total else (100.0 if state == "done" else 0.0),
        "started_at": meta_get(conn, META_STARTED),
        "finished_at": meta_get(conn, META_FINISHED),
        "swapped_at": meta_get(conn, META_SWAPPED),
        "resumable": state in ("running", "paused"),
        "needs_restart": state == "ready",
    }


def estimate(conn: sqlite3.Connection) -> dict:
    """切换档位的耗时预估，供控制台弹窗展示。"""
    total = _count_total(conn)
    return {
        "rows": total,
        "seconds": round(total * SEC_PER_ROW + BACKUP_OVERHEAD_S, 1),
        "backup_seconds": BACKUP_OVERHEAD_S,
    }


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------

def start(conn: sqlite3.Connection, to_tier: str, *, from_tier: str | None = None) -> dict:
    ensure_schema(conn)
    state = meta_get(conn, META_STATE, "idle")
    if state in ("running", "paused", "ready"):
        raise MigrationError(f"已有未完成的迁移（{state}），先完成或取消它")
    path = tier_dict_path(to_tier)  # 提前校验/生成，别等跑到一半才报错
    if from_tier is None:
        from_tier = configured_tier()
    with _memory_temp(conn):
        conn.execute("UPDATE memories SET tokens_next = NULL")
    total = _count_total(conn)
    meta_set(conn, META_STATE, "running")
    meta_set(conn, META_FROM, from_tier)
    meta_set(conn, META_TO, to_tier)
    meta_set(conn, META_CURSOR, "0")
    meta_set(conn, META_TOTAL, str(total))
    meta_set(conn, META_STARTED, time.strftime("%Y-%m-%dT%H:%M:%S+08:00"))
    meta_set(conn, META_FINISHED, None)
    conn.commit()
    return {"to_tier": to_tier, "from_tier": from_tier, "total": total, "dict": str(path) if path else "jieba-default"}


def step(conn: sqlite3.Connection, tokenizer: "jieba.Tokenizer", batch: int = 200) -> int:
    """处理一批：光标之后、tokens_next 还空着的行。返回本批行数。"""
    cursor = int(meta_get(conn, META_CURSOR, "0") or 0)
    rows = conn.execute(
        "SELECT id, content FROM memories WHERE id > ? AND tokens_next IS NULL ORDER BY id LIMIT ?",
        (cursor, batch),
    ).fetchall()
    if not rows:
        return 0
    payload = [(tokens_for(tokenizer, content), rid) for rid, content in rows]
    conn.executemany("UPDATE memories SET tokens_next = ? WHERE id = ?", payload)
    meta_set(conn, META_CURSOR, str(rows[-1][0]))
    conn.commit()
    return len(rows)


def run(
    conn: sqlite3.Connection,
    *,
    to_tier: str | None = None,
    batch: int = 200,
    tokenizer: "jieba.Tokenizer" | None = None,
    progress: Callable[[dict], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    max_batches: int | None = None,
) -> dict:
    """跑到完成（或被打断）。可反复调用，从游标接着来。"""
    ensure_schema(conn)
    state = meta_get(conn, META_STATE, "idle")
    if state in ("idle", "done", "failed"):
        if not to_tier:
            raise MigrationError("还没有进行中的迁移，需要指定 to_tier")
        start(conn, to_tier)
    elif state == "ready":
        return status(conn)
    elif state == "paused":
        meta_set(conn, META_STATE, "running")
        conn.commit()

    tier = meta_get(conn, META_TO) or to_tier or DEFAULT_TIER
    tok = tokenizer or build_tokenizer(tier_dict_path(tier))
    batches = 0
    while True:
        if should_stop is not None and should_stop():
            meta_set(conn, META_STATE, "paused")
            conn.commit()
            break
        processed = step(conn, tok, batch=batch)
        batches += 1
        if progress is not None:
            progress(status(conn))
        if processed == 0:
            if _count_pending(conn) == 0:
                meta_set(conn, META_STATE, "ready")  # 等重启切换
                meta_set(conn, META_FINISHED, time.strftime("%Y-%m-%dT%H:%M:%S+08:00"))
                conn.commit()
            break
        if max_batches is not None and batches >= max_batches:
            break
    return status(conn)


def pause(conn: sqlite3.Connection) -> dict:
    if meta_get(conn, META_STATE, "idle") in ("running",):
        meta_set(conn, META_STATE, "paused")
        conn.commit()
    return status(conn)


def cancel(conn: sqlite3.Connection) -> dict:
    """放弃迁移：清空影子列，恢复 idle。tokens 全程没动过，无需回滚。"""
    ensure_schema(conn)
    with _memory_temp(conn):
        conn.execute("UPDATE memories SET tokens_next = NULL")
    for key in (META_STATE, META_FROM, META_TO, META_CURSOR, META_TOTAL, META_STARTED, META_FINISHED):
        _delete_meta(conn, key)
    meta_set(conn, META_STATE, "idle")
    conn.commit()
    return status(conn)


# ---------------------------------------------------------------------------
# 启动时切换
# ---------------------------------------------------------------------------

def swap(conn: sqlite3.Connection, *, active_tier: str | None = None) -> dict:
    """把 tokens_next 一次性换成 tokens 并重建 FTS。仅在目标词典已加载时调用。"""
    ensure_schema(conn)
    state = meta_get(conn, META_STATE, "idle")
    if state != "ready":
        return {"swapped": False, "reason": f"state={state}"}

    target = meta_get(conn, META_TO) or ""
    active = (active_tier or configured_tier() or "").strip()
    if target and active and _canonical_tier(target) != _canonical_tier(active):
        return {"swapped": False, "reason": f"词典未切换：target={target} active={active}"}

    pending = _count_pending(conn)
    if pending:
        # 迁移完成后又写入了新记忆：继续补齐，别切出半截
        meta_set(conn, META_STATE, "running")
        conn.commit()
        return {"swapped": False, "reason": f"还有 {pending} 行待重算，已回到 running"}

    started = time.perf_counter()
    temp_guard = _memory_temp(conn)
    temp_guard.__enter__()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("UPDATE memories SET tokens = COALESCE(tokens_next, '')")
        conn.execute("UPDATE memories SET tokens_next = NULL")
        conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
        meta_set(conn, META_STATE, "done")
        meta_set(conn, META_SWAPPED, time.strftime("%Y-%m-%dT%H:%M:%S+08:00"))
        meta_set(conn, "tokens.stale", "0")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        temp_guard.__exit__(None, None, None)
    return {"swapped": True, "from_tier": meta_get(conn, META_FROM), "to_tier": target,
            "seconds": round(time.perf_counter() - started, 2)}


def _canonical_tier(tier: str) -> str:
    """档位名和词典路径要能对上：3w 与 resources/dict/dict-30k.txt 视为同一个。"""
    text = (tier or "").strip()
    if not text:
        return "full"
    try:
        path = tier_dict_path(text, build=False)
    except MigrationError:
        return text.lower()
    return "full" if path is None else str(path.resolve()).lower()


def on_startup(conn: sqlite3.Connection, *, active_tier: str | None = None) -> dict:
    """启动钩子：有 ready 状态的迁移就立刻切换；running/paused 只报告。"""
    ensure_schema(conn)
    state = meta_get(conn, META_STATE, "idle")
    if state == "ready":
        return swap(conn, active_tier=active_tier)
    result = status(conn)
    result["swapped"] = False
    if state in ("running", "paused"):
        result["resume_hint"] = "迁移未完成，可在控制台点“继续”或跑 python migrate.py run"
    return result


# ---------------------------------------------------------------------------
# 后台任务（控制台按钮用）
# ---------------------------------------------------------------------------

_JOB_LOCK = threading.Lock()
_JOB: dict = {"thread": None, "stop": False, "error": ""}


def job_error() -> str:
    """后台任务最近一次异常（空串表示没有）。"""
    return str(_JOB.get("error") or "")


def job_running() -> bool:
    thread = _JOB.get("thread")
    return bool(thread is not None and thread.is_alive())


def request_stop() -> None:
    _JOB["stop"] = True


def _job_main(db_path: Path, to_tier: str | None, batch: int) -> None:
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    try:
        conn.execute("PRAGMA busy_timeout = 3000")
        ensure_schema(conn)
        state = meta_get(conn, META_STATE, "idle")
        if state == "idle" and not to_tier:
            raise MigrationError("没有进行中的迁移，需要指定目标档位")
        tier = meta_get(conn, META_TO) or to_tier or DEFAULT_TIER
        run(
            conn,
            to_tier=to_tier,
            batch=batch,
            tokenizer=build_tokenizer(tier_dict_path(tier)),
            should_stop=lambda: bool(_JOB["stop"]),
        )
        _JOB["error"] = ""
    except Exception as exc:  # noqa: BLE001 - 后台线程不能把异常抛给调用方
        _JOB["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()


def start_async(db_path: Path, to_tier: str | None = None, *, batch: int = 200) -> dict:
    """在后台线程里跑（或续跑）迁移；同一时间只允许一个。"""
    with _JOB_LOCK:
        if job_running():
            return {"started": False, "reason": "已有迁移在运行"}
        _JOB["stop"] = False
        _JOB["error"] = ""
        thread = threading.Thread(
            target=_job_main, args=(Path(db_path), to_tier, batch),
            name="tier-migrate", daemon=True,
        )
        _JOB["thread"] = thread
        thread.start()
        return {"started": True, "to_tier": to_tier}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _open(db_path: str | None = None) -> sqlite3.Connection:
    import config
    import store

    settings = config.Settings() if not db_path else __import__("dataclasses").replace(
        config.get_settings(), db_path=Path(db_path)
    )
    conn = store.open_conn(settings)
    ensure_schema(conn)
    return conn


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="词典档位迁移")
    parser.add_argument("action", choices=["status", "start", "run", "pause", "cancel", "swap", "build-3w", "estimate"])
    parser.add_argument("tier", nargs="?", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--batch", type=int, default=200)
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.action == "build-3w":
        path = build_trimmed_dict(TIER_WORDS["3w"] or 30000)
        print(json.dumps({"dict": str(path), "words": TIER_WORDS["3w"]}, ensure_ascii=False))
        return 0

    conn = _open(args.db)
    try:
        if args.action == "status":
            print(json.dumps(status(conn), ensure_ascii=False))
        elif args.action == "estimate":
            print(json.dumps(estimate(conn), ensure_ascii=False))
        elif args.action == "start":
            print(json.dumps(start(conn, args.tier or "3w"), ensure_ascii=False))
        elif args.action == "run":
            def show(s: dict) -> None:
                print(f"  {s['done']}/{s['total']} ({s['percent']}%)")
            print(json.dumps(run(conn, to_tier=args.tier, batch=args.batch, progress=show), ensure_ascii=False))
        elif args.action == "pause":
            print(json.dumps(pause(conn), ensure_ascii=False))
        elif args.action == "cancel":
            print(json.dumps(cancel(conn), ensure_ascii=False))
        elif args.action == "swap":
            print(json.dumps(swap(conn), ensure_ascii=False))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
