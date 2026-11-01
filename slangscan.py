# -*- coding: utf-8 -*-
"""L3 黑话自动学习：从群聊里挖候选词，四道校验后写进本地词表。

为什么要校验：jieba 是最长匹配，随手加词会把已有词切碎
（加「记忆系」会把「记忆系统」拆成「记忆系 / 统」）。所以候选必须过：

  1. 频次 / 人数 / 天数 门槛
  2. 最大短语优先：被更长候选覆盖、且频次相当 → 丢掉短的
  3. 冲突检测：候选是已知词的前缀/子串 → 丢掉
  4. dry-run：真的加进去再切一遍原文，发现别的词被拆坏就拒绝

扫描只在 role='user' 的行上做，机器人自己说的话不能算。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import jieba

import migrate
import slang
import textproc
import tuning

TZ = timezone(timedelta(hours=8))
META_LAST_SCAN = "slang.last_scan"
MAX_REJECT_SAMPLES = 40
MAX_ACCEPT_PER_SCAN = 30

_PUNCT = re.compile(r"[\s\u3000，。！？、,.!?;:;:\"'“”‘’（）()\[\]【】<>《》<>~@#￥$%…—\-_=+*/\\|]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _now() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _today() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def load_turns(conn: sqlite3.Connection, cfg: dict) -> list[tuple[str, str, str]]:
    """(content, speaker, happened_at)；只看历史窗口内的用户发言。"""
    since = (datetime.now(TZ) - timedelta(days=int(cfg["min_days"]))).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    rows = conn.execute(
        "SELECT content, COALESCE(speaker_id, speaker_name, ''), COALESCE(happened_at, created_at) "
        "FROM conversation_turns WHERE role='user' AND length(content) BETWEEN 4 AND 200 "
        "AND COALESCE(happened_at, created_at) >= ? ORDER BY id",
        (since,),
    ).fetchall()
    return [(textproc.clean_text(r[0]), r[1] or "", r[2] or "") for r in rows]


def _valid_word(word: str, max_len: int) -> bool:
    if not (2 <= len(word) <= max_len):
        return False
    if _PUNCT.search(word):
        return False
    if word.isdigit():
        return False
    if word in textproc.STOPWORDS:
        return False
    return bool(_CJK.search(word)) or len(word) >= 3


def _boundary_ok(word: str) -> bool:
    """去掉跨在句子边界的碎片：以虚词/停用词开头或结尾、含数字的都不可靠。"""
    if word[0] in textproc.STOPWORDS or word[-1] in textproc.STOPWORDS:
        return False
    if any(ch.isdigit() for ch in word):
        return False
    return True


def _count_ngrams(texts: list[str], max_len: int) -> Counter:
    counter: Counter = Counter()
    for text in texts:
        size = len(text)
        for n in range(2, max_len + 1):
            for i in range(size - n + 1):
                counter[text[i:i + n]] += 1
    return counter


def _token_set(texts: list[str]) -> set[str]:
    out: set[str] = set()
    for text in texts:
        out.update(textproc.tokenize(text))
    return out


def _known_words() -> set[str]:
    """jieba 词典里已有的词 + 黑话词表，用来做子串冲突检测。"""
    known = set(slang.words())
    try:
        known.update(jieba.dt.FREQ.keys())
    except Exception:  # noqa: BLE001
        pass
    return {w for w in known if 2 <= len(w) <= 12}


def _has_longer_word(word: str, known: set[str]) -> str | None:
    for candidate in known:
        if len(candidate) > len(word) and word in candidate:
            return candidate
    return None


def _dry_run(word: str, samples: list[str], lock) -> tuple[bool, str]:
    """真加进去切一遍，看有没有把别的词拆坏。"""
    if not samples:
        return False, "没有可验证的例句"
    with lock:
        before = [set(textproc.tokenize(s)) for s in samples]
        try:
            jieba.add_word(word)
        except Exception:  # noqa: BLE001
            return False, "加词失败"
        try:
            after = [set(textproc.tokenize(s)) for s in samples]
        finally:
            jieba.del_word(word)
    became = False
    for old, new in zip(before, after):
        if word in new:
            became = True
        for token in old:
            if len(token) < 2 or token in new or token == word:
                continue
            if token in word or word in token:
                continue
            return False, f"会拆坏「{token}」"
    if not became:
        return False, "加词后仍不成词（可能是更长短语的一部分）"
    return True, ""


def _samples_for(word: str, texts: list[str], limit: int = 3) -> list[str]:
    return [t for t in texts if word in t][:limit]


def scan(conn: sqlite3.Connection, *, cfg: dict | None = None, apply: bool | None = None) -> dict:
    """跑一次扫描；apply=False 时只出候选不写词表。"""
    conf = cfg or tuning.slang()
    migrate.ensure_schema(conn)
    started = _now()
    t0 = time.perf_counter()
    if apply is None:
        apply = bool(conf.get("auto_accept", True))

    turns = load_turns(conn, conf)
    texts = [t[0] for t in turns if t[0]]
    report = {
        "started_at": started,
        "finished_at": None,
        "turns": len(texts),
        "candidates": 0,
        "accepted": [],
        "rejected": [],
        "applied": False,
        "seconds": 0.0,
        "config": {k: conf.get(k) for k in ("min_count", "min_speakers", "min_days", "max_word_len")},
    }
    if not texts:
        report["finished_at"] = _now()
        conn.execute(
            "INSERT INTO app_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (META_LAST_SCAN, json.dumps(report, ensure_ascii=False)),
        )
        conn.commit()
        return report

    counter = _count_ngrams(texts, int(conf["max_word_len"]))
    already = _token_set(texts)
    known = _known_words()
    min_count = int(conf["min_count"])

    raw = [
        w for w, c in counter.items()
        if c >= min_count and _valid_word(w, int(conf["max_word_len"])) and _boundary_ok(w)
    ]
    report["candidates"] = len(raw)

    # 第二遍：只对通过频次的候选统计人数与天数
    speakers: dict[str, set[str]] = {w: set() for w in raw}
    days: dict[str, set[str]] = {w: set() for w in raw}
    raw_set = set(raw)
    for content, speaker, happened in turns:
        if not content:
            continue
        size = len(content)
        for n in range(2, int(conf["max_word_len"]) + 1):
            for i in range(size - n + 1):
                piece = content[i:i + n]
                if piece in raw_set:
                    speakers[piece].add(speaker)
                    days[piece].add((happened or "")[:10])

    picked = [
        w for w in raw
        if len(speakers[w]) >= int(conf["min_speakers"]) and len(days[w]) >= int(conf["min_days"])
    ]

    # 门槛过了但语义上不该收的，记进 rejected 便于调参
    for word in raw:
        if word in picked:
            continue
        if len(speakers[word]) < int(conf["min_speakers"]):
            report["rejected"].append({"word": word, "reason": "说话人数不够"})
        else:
            report["rejected"].append({"word": word, "reason": "时间跨度不够"})

    # 最大短语优先：被更长候选覆盖、且频次相当（≥90%）的短片段丢掉
    picked_set = set(picked)
    covered: list[dict] = []
    survivors: list[str] = []
    for word in picked:
        longer = next(
            (o for o in picked_set
             if len(o) > len(word) and word in o and counter[o] >= counter[word] * 0.9),
            None,
        )
        if longer:
            covered.append({"word": word, "reason": f"被更长候选「{longer}」覆盖"})
        else:
            survivors.append(word)
    picked = survivors
    picked.sort(key=lambda w: (-counter[w], -len(w)))
    lock = textproc._JIEBA_LOCK
    accepted: list[dict] = []
    report["rejected"].extend(covered)
    for word in picked:
        if len(accepted) >= MAX_ACCEPT_PER_SCAN:
            break
        if word in already:
            report["rejected"].append({"word": word, "reason": "已经是独立词"})
            continue
        longer = _has_longer_word(word, known)
        if longer:
            report["rejected"].append({"word": word, "reason": f"是已有词「{longer}」的一部分"})
            continue
        ok, why = _dry_run(word, _samples_for(word, texts), lock)
        if not ok:
            report["rejected"].append({"word": word, "reason": why})
            continue
        accepted.append({
            "word": word,
            "count": counter[word],
            "speakers": len(speakers[word]),
            "days": len(days[word]),
        })

    if apply:
        for item in accepted:
            try:
                if slang.add_word(item["word"])["added"]:
                    item["applied"] = True
            except ValueError:
                item["applied"] = False
    report["accepted"] = accepted
    report["applied"] = bool(apply)
    report["finished_at"] = _now()
    report["seconds"] = round(time.perf_counter() - t0, 2)
    report["rejected"] = report["rejected"][:MAX_REJECT_SAMPLES]

    conn.execute(
        "INSERT INTO app_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (META_LAST_SCAN, json.dumps(report, ensure_ascii=False)),
    )
    conn.commit()
    return report


def last_scan(conn: sqlite3.Connection) -> dict | None:
    try:
        row = conn.execute("SELECT value FROM app_meta WHERE key = ?", (META_LAST_SCAN,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    try:
        return json.loads(row[0])
    except ValueError:
        return None


def due_now(conn: sqlite3.Connection, cfg: dict | None = None) -> bool:
    """定时模式：到点且今天还没跑过。"""
    conf = cfg or tuning.slang()
    if not conf.get("enabled") or conf.get("scan_mode") != "daily":
        return False
    now = datetime.now(TZ)
    if now.hour < int(conf.get("scan_hour", 4)):
        return False
    last = last_scan(conn) or {}
    return str(last.get("started_at") or "")[:10] != _today()
