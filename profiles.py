# -*- coding: utf-8 -*-
"""数据库档案：登记多个 SQLite 库，选出当前使用哪一个。

选择结果写进 data/db-profiles.json，**下次启动生效**。运行期不换库，避免写队列、
词频缓存、控制台口令停在一个对不上号的状态里。

优先级：环境变量 QQ_MEMORY_DB > 档案里的 current > 默认 data/qq-memory.db
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
PROFILES_PATH = DATA_DIR / "db-profiles.json"
DEFAULT_DB_PATH = DATA_DIR / "qq-memory.db"

NAME_MAX = 40
_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff.\- ]{1,%d}$" % NAME_MAX)


def validate_name(raw: str) -> str:
    """档案名：中文、字母、数字、下划线、点、横线、空格；不能带路径分隔符。"""
    name = (raw or "").strip()
    if not name:
        raise ValueError("档案名不能为空")
    if len(name) > NAME_MAX:
        raise ValueError(f"档案名最长 {NAME_MAX} 个字符")
    if not _NAME_RE.match(name):
        raise ValueError("档案名只能含中文、字母、数字、下划线、点、横线")
    if ".." in name:
        raise ValueError("档案名不能包含两个连续的点")
    if not re.search(r"[\w\u4e00-\u9fff]", name):
        raise ValueError("档案名至少包含一个字母、数字或汉字")
    return name


def load() -> dict:
    """读档案文件；不存在或坏掉时返回空结构，不抛错。"""
    try:
        raw = json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"current": None, "entries": []}
    if not isinstance(raw, dict):
        return {"current": None, "entries": []}
    entries = []
    for item in raw.get("entries") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        path = str(item.get("path") or "").strip()
        if not name or not path:
            continue
        entries.append({"name": name, "path": path})
    current = raw.get("current")
    current = str(current).strip() if current else None
    if current and not any(e["name"] == current for e in entries):
        current = None
    return {"current": current, "entries": entries}


def save(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"current": state.get("current"), "entries": state.get("entries") or []},
        ensure_ascii=False,
        indent=2,
    )
    tmp = PROFILES_PATH.with_suffix(".json.tmp")
    tmp.write_text(payload + "\n", encoding="utf-8")
    tmp.replace(PROFILES_PATH)


def normalize_path(raw: str) -> Path:
    """把用户填的路径变成绝对路径；指向目录时自动补 qq-memory.db。"""
    text = (raw or "").strip()
    text = text.strip(chr(34)).strip(chr(39))
    if not text:
        raise ValueError("数据库路径不能为空")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if path.is_dir():
        path = path / "qq-memory.db"
    return path.resolve()


def register(name: str, raw_path: str) -> dict:
    """新增或覆盖一条档案（同名即改路径）。"""
    clean = validate_name(name)
    path = normalize_path(raw_path)
    state = load()
    entry = {"name": clean, "path": str(path)}
    replaced = False
    for index, item in enumerate(state["entries"]):
        if item["name"] == clean:
            state["entries"][index] = entry
            replaced = True
            break
    if not replaced:
        state["entries"].append(entry)
    if not state.get("current"):
        state["current"] = clean
    save(state)
    return entry


def select(name: str) -> dict:
    """把某个档案设为当前；名字不存在时报错，不静默新建。"""
    clean = validate_name(name)
    state = load()
    for item in state["entries"]:
        if item["name"] == clean:
            state["current"] = clean
            save(state)
            return item
    raise ValueError(f"没有这个档案：{clean}")


def remove(name: str) -> dict:
    """从档案里移除一条（不动文件）。"""
    clean = validate_name(name)
    raw = _read_raw()
    entries = [e for e in raw.get("entries") or [] if isinstance(e, dict)]
    target = next((e for e in entries if e.get("name") == clean), None)
    if target is None:
        raise ValueError(f"没有这个档案：{clean}")
    raw["entries"] = [e for e in entries if e.get("name") != clean]
    if raw.get("current") == clean:
        raw["current"] = raw["entries"][0]["name"] if raw["entries"] else None
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PROFILES_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(PROFILES_PATH)
    return target


def current_db_path() -> Path | None:
    state = load()
    name = state.get("current")
    if not name:
        return None
    for item in state["entries"]:
        if item["name"] == name:
            return Path(item["path"])
    return None


def resolve_db_path(env_value: str | None = None) -> Path:
    """决定这次进程用哪个库。"""
    env = (env_value or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    chosen = current_db_path()
    if chosen is not None:
        return chosen
    return DEFAULT_DB_PATH


def _counts(path: Path) -> tuple[int | None, int | None]:
    """只读统计；库不存在或没建表时返回 (None, None)。"""
    if not path.exists():
        return None, None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return None, None
    try:
        memories = conn.execute("SELECT count(*) FROM memories").fetchone()[0]
        turns = conn.execute("SELECT count(*) FROM conversation_turns").fetchone()[0]
        return int(memories), int(turns)
    except sqlite3.Error:
        return None, None
    finally:
        conn.close()


def describe(in_use: Path) -> list[dict]:
    """控制台用：列出所有档案及其状态。"""
    state = load()
    rows = []
    tiers = {e.get("name"): e.get("jieba_dict", "full") for e in _read_raw().get("entries") or []}
    for item in state["entries"]:
        path = Path(item["path"])
        memories, turns = _counts(path)
        rows.append(
            {
                "name": item["name"],
                "path": str(path),
                "exists": path.exists(),
                "size_bytes": path.stat().st_size if path.exists() else 0,
                "memories": memories,
                "turns": turns,
                "is_current": item["name"] == state.get("current"),
                "in_use": path.resolve() == Path(in_use).resolve(),
                "tier": str(tiers.get(item["name"]) or "full"),
            }
        )
    return rows


def ensure_default() -> None:
    """档案为空时，把默认库登记成「默认」，让控制台有东西可看。"""
    if load()["entries"]:
        return
    save({"current": "默认", "entries": [{"name": "默认", "path": str(DEFAULT_DB_PATH), "jieba_dict": "full"}]})


def _read_raw() -> dict:
    try:
        raw = json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"current": None, "entries": []}
    return raw if isinstance(raw, dict) else {"current": None, "entries": []}


def _find_raw_entry(raw: dict, name: str | None) -> dict | None:
    target = (name or raw.get("current") or "").strip()
    for item in raw.get("entries") or []:
        if isinstance(item, dict) and item.get("name") == target:
            return item
    return None


def entry_tier(name: str | None = None) -> str:
    """某个档案用的词典档位；没写过就是 full。"""
    item = _find_raw_entry(_read_raw(), name)
    return str((item or {}).get("jieba_dict") or "full")


def set_tier_for_path(db_path, tier: str) -> dict | None:
    """只给路径匹配的那个档案写档位；没登记过就不动（别误改别的库）。"""
    raw = _read_raw()
    target = str(Path(db_path).resolve()).lower()
    for item in raw.get("entries") or []:
        if not isinstance(item, dict):
            continue
        try:
            if str(Path(item.get("path", "")).resolve()).lower() != target:
                continue
        except (OSError, ValueError):
            continue
        item["jieba_dict"] = str(tier or "full").strip() or "full"
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = PROFILES_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(PROFILES_PATH)
        return item
    return None


def set_tier(tier: str, name: str | None = None) -> dict:
    """只改 jieba_dict 一个字段，其他字段原样保留。"""
    raw = _read_raw()
    item = _find_raw_entry(raw, name)
    if item is None:
        raise ValueError("找不到要设置档位的档案")
    item["jieba_dict"] = str(tier or "full").strip() or "full"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PROFILES_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(PROFILES_PATH)
    return item

