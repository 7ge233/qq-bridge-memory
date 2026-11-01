# -*- coding: utf-8 -*-
"""黑话词表：L2 内置 + L3 本地。

分词是三层叠起来的：

  L1  基础词典：jieba 全量词典或 3 万词档（内存大头，见 migrate.py）
  L2  resources/slang/builtin.txt  随包发布的精选词（仓库里维护）
  L3  data/userdict.txt            本机累积的词（不进仓库，可随时增删）

L2/L3 不替换 L1，只是往词典里追词，让「栓Q」「破防」这类组合作为整体被切出来。
加词之后必须重算 tokens 才会影响检索——重算走 migrate.py 的迁移通道
（控制台「重算分词」按钮），加词本身不会立刻改变已有记忆的分词。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
BUILTIN_PATH = PROJECT_ROOT / "resources" / "slang" / "builtin.txt"
USER_PATH = PROJECT_ROOT / "data" / "userdict.txt"

MIN_LEN = 2
MAX_LEN = 12
MAX_USER_ENTRIES = 5000
_BAD_CHARS = re.compile(r"[\s\x00-\x1f]")


def _clean(raw: str) -> str:
    return (raw or "").strip()


def _valid(word: str) -> bool:
    if not (MIN_LEN <= len(word) <= MAX_LEN):
        return False
    if word.startswith("#"):
        return False
    return _BAD_CHARS.search(word) is None


def read_words(path: Path) -> list[str]:
    """每行一个词，# 开头是注释；去空行、去重、保序。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for line in text.splitlines():
        word = _clean(line)
        if not word or word.startswith("#") or not _valid(word):
            continue
        if word in seen:
            continue
        seen.add(word)
        out.append(word)
    return out


def builtin_words() -> list[str]:
    return read_words(BUILTIN_PATH)


def user_words() -> list[str]:
    return read_words(USER_PATH)


def words() -> list[str]:
    """内置 + 本地，内置在前（同名词以先出现的为准）。"""
    seen: set[str] = set()
    out: list[str] = []
    for word in builtin_words() + user_words():
        if word not in seen:
            seen.add(word)
            out.append(word)
    return out


def fingerprint() -> str:
    """词表指纹：词表一变就变，用来判断已有 tokens 是否过期。"""
    joined = "\n".join(sorted(set(words())))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def apply(target) -> int:
    """把词表灌进一个 jieba 分词器（全局 jieba 或独立 Tokenizer 都行）。"""
    count = 0
    for word in words():
        try:
            target.add_word(word)
            count += 1
        except Exception:  # noqa: BLE001 - 单个词失败不该拖垮启动
            continue
    return count


def status() -> dict:
    builtin = builtin_words()
    user = user_words()
    return {
        "builtin_path": str(BUILTIN_PATH),
        "user_path": str(USER_PATH),
        "builtin_count": len(builtin),
        "user_count": len(user),
        "total": len(words()),
        "fingerprint": fingerprint(),
    }


def _write_user(items: list[str]) -> None:
    USER_PATH.parent.mkdir(parents=True, exist_ok=True)
    header = "# 本地黑话词表（L3）：每行一个词，# 开头为注释。本文件不进仓库。\n"
    body = "\n".join(items)
    tmp = USER_PATH.with_suffix(".txt.tmp")
    tmp.write_text(header + body + ("\n" if items else ""), encoding="utf-8", newline="\n")
    tmp.replace(USER_PATH)


def add_word(word: str) -> dict:
    """加一个词到 L3；已存在（含内置）就直接返回现状。"""
    clean = _clean(word)
    if not _valid(clean):
        raise ValueError(f"词不合法：长度需在 {MIN_LEN}-{MAX_LEN} 之间且不含空白")
    builtin = builtin_words()
    user = user_words()
    if clean in builtin:
        return {"added": False, "reason": "内置词表里已有", "word": clean}
    if clean in user:
        return {"added": False, "reason": "本地词表里已有", "word": clean}
    if len(user) >= MAX_USER_ENTRIES:
        raise ValueError(f"本地词表已达上限 {MAX_USER_ENTRIES} 条")
    user.append(clean)
    _write_user(user)
    return {"added": True, "word": clean, "total": len(user)}


def remove_word(word: str) -> bool:
    """只删 L3；内置词删不掉（要删得改仓库里的文件）。"""
    clean = _clean(word)
    user = user_words()
    if clean not in user:
        return False
    user.remove(clean)
    _write_user(user)
    return True


def contains(word: str) -> bool:
    return _clean(word) in set(words())
