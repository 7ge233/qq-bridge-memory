# -*- coding: utf-8 -*-
"""日记层：把「一天」压成可召回的条目，供「昨天/前天/上周三」这类问题命中。

三个分工：
  * 侧车**不生成**正文，只生成「事实骨架」(digest)：从 conversation_turns 里抽话题、
    约定、偏好、情绪、未完成事项——零 LLM、永不失败；
  * 正文由 bridge 的模型写好 POST 回来（同一天重复提交 = 覆盖）；
  * 骨架兜底：正文没来时，骨架自己就是一条日记（source='digest'）。

日记日（逻辑日）按 day_cut_hour 切，默认 05:00：23:40 聊到 01:20 属于同一个日记日，
不会被劈成两天。所有时间统一 +08:00（timeutil.TZ）。
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from datetime import date, datetime, time, timedelta
from typing import Mapping

import store
import textproc
from config import Settings
from timeutil import TZ, date_only, iso_now, parse_iso

CATEGORY_DIARY = "diary"
DEFAULT_CUT_HOUR = 5

_LOOP_WORDS = ("答应", "约定", "说好", "讲好", "待办", "记得", "别忘", "提醒我", "等你", "回头")
_DATE_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]|(20\d{2})-(\d{2})-(\d{2})")


# ---------------------------------------------------------------------------
# 日记日
# ---------------------------------------------------------------------------

def logical_day(ts: datetime, cut_hour: int = DEFAULT_CUT_HOUR) -> date:
    """逻辑日：cut_hour 之前算前一天（默认 05:00 前算前一天）。"""
    cut = max(0, min(23, int(cut_hour)))
    return (ts - timedelta(hours=cut)).date()


def day_window(label: date, cut_hour: int = DEFAULT_CUT_HOUR) -> tuple[datetime, datetime]:
    """日记日 label 覆盖的时间窗：[label cut_hour, label+1 cut_hour)。"""
    cut = max(0, min(23, int(cut_hour)))
    start = datetime.combine(label, time(cut, 0), tzinfo=TZ)
    return start, start + timedelta(days=1)


def parse_label(value: object) -> date | None:
    """把 '2026-11-11' / '2026-11-11T…' 解析成日期。"""
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        try:
            return date(int(text[:4]), int(text[5:7]), int(text[8:10]))
        except ValueError:
            return None
    parsed = parse_iso(text)
    return parsed.date() if parsed is not None else None


def query_day(query: str, now: datetime, cut_hour: int = DEFAULT_CUT_HOUR) -> date | None:
    """问句里如果有相对日期（昨天/前天/上周三/10月31日），换算成要查的日记日。"""
    probe = str(query or "").strip().rstrip("呢吗啊呀吧？?！!。，, ")
    if not probe:
        return None
    try:
        import temporal

        parsed = temporal.parse_event_time(probe, base=now)
    except Exception:  # 解析失败绝不影响召回
        return None
    if parsed is None:
        return None
    dt = parse_iso(parsed.event_at)
    if dt is None:
        return None
    # 用户说的「昨天」是自然日；日记按自然日打标签，主要时段（05:00–24:00）落在同一天
    return dt.date()


# ---------------------------------------------------------------------------
# 事实骨架
# ---------------------------------------------------------------------------

def _turn_rows(
    conn: sqlite3.Connection, scope_key_value: str, start: datetime, end: datetime
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT role, speaker_name, content, created_at FROM conversation_turns "
        "WHERE scope_key=? AND created_at >= ? AND created_at < ? ORDER BY id",
        (
            scope_key_value,
            start.isoformat(timespec="milliseconds"),
            end.isoformat(timespec="milliseconds"),
        ),
    ).fetchall()


def _corpus_df(conn: sqlite3.Connection, scope_key_value: str, limit: int = 3000) -> tuple[dict[str, float], int]:
    """统计「平时说话里有多常见」（文档频率），用来把口头禅从话题里剔掉。

    思路：话题 = 今天出现、但平时少见。终于/说好/开心这种天天出现的词，
    在语料里的 DF 很高，直接过滤；面试/火锅这种只在特定日子出现的词留下来。
    """
    rows = conn.execute(
        "SELECT content FROM conversation_turns WHERE scope_key=? ORDER BY id DESC LIMIT ?",
        (scope_key_value, max(1, int(limit))),
    ).fetchall()
    total = len(rows)
    df: Counter[str] = Counter()
    for row in rows:
        for token in set(textproc.tokenize(str(row["content"] or ""), max_tokens=40)):
            df[token] += 1
    return {w: c / total for w, c in df.items()}, total


def _top_words(
    texts: list[str],
    df: Mapping[str, float] | None = None,
    corpus_turns: int = 0,
    limit: int = 8,
    min_count: int = 1,
    df_cap: float = 0.30,
) -> list[str]:
    """话题词：今天反复出现、且平时不常见的词（语料够多时按 DF 过滤口头禅）。"""
    counter: Counter[str] = Counter()
    for text in texts:
        for token in textproc.tokenize(text, max_tokens=40):
            if len(token) >= 2:
                counter[token] += 1
    counter = Counter({w: c for w, c in counter.items() if c >= min_count})
    picked: list[str] = []
    for word, _count in counter.most_common(limit * 4):
        if corpus_turns >= 20 and df is not None:
            if df.get(word, 0.0) >= df_cap:
                continue  # 口头禅、常用词
        if any(word in old or old in word for old in picked):
            continue
        picked.append(word)
        if len(picked) >= limit:
            break
    return picked


def _pick_lines(texts: list[str], words: tuple[str, ...], limit: int = 4) -> list[str]:
    out: list[str] = []
    for text in texts:
        if any(word in text for word in words):
            line = text.strip()
            if line and not any(line[:12] == old[:12] for old in out):
                out.append(line[:60])
        if len(out) >= limit:
            break
    return out


def build_digest(
    conn: sqlite3.Connection,
    scope_key_value: str,
    label: date,
    settings: Settings,
) -> dict:
    """从当天的原始对话生成事实骨架（零 LLM）。"""
    start, end = day_window(label, settings.day_cut_hour)
    rows = _turn_rows(conn, scope_key_value, start, end)
    user_texts = [str(r["content"] or "") for r in rows if str(r["role"]) == "user"]
    all_texts = [str(r["content"] or "") for r in rows]
    try:
        df, corpus_turns = _corpus_df(conn, scope_key_value)
    except sqlite3.Error:
        df, corpus_turns = {}, 0
    emotions: Counter[str] = Counter()
    for text in all_texts:
        for word in textproc.EMOTION_WORDS:
            if word in text:
                emotions[word] += 1
    commitments = _pick_lines(all_texts, textproc.COMMITMENT_WORDS)
    loops = [line for line in _pick_lines(all_texts, _LOOP_WORDS) if line not in commitments]
    digest = {
        "date": label.isoformat(),
        "window": [start.isoformat(), end.isoformat()],
        "turns": len(rows),
        "user_turns": len(user_texts),
        "assistant_turns": len(rows) - len(user_texts),
            "topics": _top_words(
            all_texts or user_texts,
            df=df,
            corpus_turns=corpus_turns,
            min_count=1,
        ),
        "commitments": commitments,
        "open_loops": loops,
        "preferences": _pick_lines(all_texts, textproc.PREFERENCE_WORDS),
        "emotions": dict(emotions.most_common(5)),
    }
    digest["text"] = render_digest(digest)
    return digest


def render_digest(digest: Mapping[str, object]) -> str:
    """把骨架渲染成给模型看的一段话（也是骨架兜底日记的正文）。"""
    label = str(digest.get("date") or "")
    lines = [f"【{label} 当天小结（机器整理）】"]
    turns = int(digest.get("turns") or 0)
    if turns <= 0:
        return "\n".join(lines + ["这天没有对话记录。"])
    lines.append(
        f"对话 {turns} 轮（对方 {digest.get('user_turns')} / 我 {digest.get('assistant_turns')}）。"
    )
    topics = digest.get("topics") or []
    if topics:
        lines.append("话题：" + "、".join(str(t) for t in topics))
    for key, title in (("commitments", "约定"), ("open_loops", "未完成"), ("preferences", "偏好")):
        items = digest.get(key) or []
        if items:
            lines.append(f"{title}：" + "；".join(str(i) for i in items))
    emotions = digest.get("emotions") or {}
    if emotions:
        lines.append(
            "情绪：" + "、".join(f"{k}×{v}" for k, v in emotions.items())
        )
    return "\n".join(lines)


def validate_prose(text: str, digest: Mapping[str, object]) -> list[str]:
    """粗校验：正文和当天对话差太远、或提了别的日期，就记一条问题。"""
    issues: list[str] = []
    prose = textproc.tokenize(text, max_tokens=200)
    day_text = " ".join(
        str(x)
        for key in ("topics", "commitments", "open_loops", "preferences")
        for x in (digest.get(key) or [])
    ) + " " + str(digest.get("text") or "")
    day_tokens = set(textproc.tokenize(day_text, max_tokens=200))
    long_tokens = [t for t in prose if len(t) >= 2]
    if long_tokens:
        hit = sum(1 for t in long_tokens if t in day_tokens)
        ratio = hit / len(long_tokens)
        if ratio < 0.30:
            issues.append(f"与当天对话重合度低（{ratio:.0%}）")
    label = parse_label(digest.get("date"))
    if label is not None:
        for m in _DATE_RE.finditer(text):
            if m.group(1):
                month, day_num = int(m.group(1)), int(m.group(2))
                if not (month == label.month and day_num == label.day):
                    issues.append(f"提到别的日期 {month}月{day_num}日")
            else:
                other = parse_label(f"{m.group(3)}-{m.group(4)}-{m.group(5)}")
                if other is not None and other != label:
                    issues.append(f"提到别的日期 {other.isoformat()}")
            if len(issues) >= 3:
                break
    return issues


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------

def _chunks(text: str, limit: int = 300) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    buf = ""
    for piece in re.split(r"(?<=[。！？\n])", text):
        if not piece:
            continue
        if len(buf) + len(piece) > limit and buf:
            parts.append(buf.strip())
            buf = ""
        buf += piece
        while len(buf) > limit:
            parts.append(buf[:limit].strip())
            buf = buf[limit:]
    if buf.strip():
        parts.append(buf.strip())
    return [p for p in parts if p]


def write_diary(
    conn: sqlite3.Connection,
    settings: Settings,
    embedder,
    account: str,
    scope: Mapping | None,
    label: date,
    text: str,
    source: str = "llm",
    importance: float = 0.85,
    suspicious: int = 0,
) -> dict:
    """写一天的小结；同一天重复提交 = 覆盖（幂等）。"""
    account = str(account or "").strip()
    if not account:
        raise ValueError("缺少 account")
    scope_type, user_id, group_id = store.parse_scope(scope)
    scope_key_value = store.scope_key(account, scope_type, user_id, group_id)
    label = parse_label(label.isoformat())
    if label is None:
        raise ValueError("date 非法")
    pieces = _chunks(text)
    if not pieces:
        raise ValueError("text 为空")

    conn.execute(
        "DELETE FROM memories WHERE account=? AND scope_key=? AND category=? "
        "AND substr(happened_at,1,10)=?",
        (account, scope_key_value, CATEGORY_DIARY, label.isoformat()),
    )
    start, end = day_window(label, settings.day_cut_hour)
    happened = start + timedelta(hours=7)  # 日记日 12:00 左右，用于显示
    visibility = "private" if scope_type == "private" else "group"
    ids: list[int] = []
    if embedder is None:
        vectors: list = [None] * len(pieces)
    elif hasattr(embedder, "embed_passages"):
        vectors = embedder.embed_passages(pieces)
    else:
        vectors = [embedder.embed_passage(piece) for piece in pieces]
    for idx, piece in enumerate(pieces):
        vec = vectors[idx] if idx < len(vectors) else None
        tokens = textproc.tokenize(piece)
        record = {
            "account": account,
            "scope_key": scope_key_value,
            "scope_type": scope_type,
            "user_id": user_id,
            "group_id": group_id,
            "speaker_name": "当天小结",
            "role": "assistant",
            "content": piece,
            "embedding": None,
            "embedding_model": None,
            "embedding_dim": None,
            "visibility": visibility,
            "category": CATEGORY_DIARY,
            "importance": float(importance),
            "is_active": 1,
            "tokens": textproc.tokens_text(tokens),
            "tags": json.dumps(
                {"diary": label.isoformat(), "source": source, "part": idx + 1},
                ensure_ascii=False,
            ),
            "source": f"diary-{source}",
            "created_at": iso_now(),
            "happened_at": happened.isoformat(timespec="milliseconds"),
            "event_at": start.isoformat(timespec="milliseconds"),
            "valid_until": end.isoformat(timespec="milliseconds"),
            "event_status": "done",
            "recall_count": 0,
            "schema_version": 2,
            "suspicious": int(suspicious),
            "state": store.STATE_ACTIVE,
        }
        if vec is not None:
            from embedder import to_blob  # 局部导入，避免无模型环境报错

            record["embedding"] = to_blob(vec)
            record["embedding_model"] = settings.model_name
            record["embedding_dim"] = int(vec.size)
        ids.append(store.insert_memory(conn, record))
    conn.commit()
    return {"ok": True, "date": label.isoformat(), "chunks": len(ids), "ids": ids}


def read_diary(
    conn: sqlite3.Connection, account: str, scope: Mapping | None, label: date
) -> list[dict]:
    scope_type, user_id, group_id = store.parse_scope(scope)
    scope_key_value = store.scope_key(account, scope_type, user_id, group_id)
    rows = conn.execute(
        "SELECT id, content, source, created_at, suspicious FROM memories "
        "WHERE account=? AND scope_key=? AND category=? AND substr(happened_at,1,10)=? "
        "AND deleted_at IS NULL ORDER BY id",
        (account, scope_key_value, CATEGORY_DIARY, label.isoformat()),
    ).fetchall()
    return [
        {
            "id": int(r["id"]),
            "content": str(r["content"] or ""),
            "source": str(r["source"] or ""),
            "created_at": r["created_at"],
            "suspicious": bool(r["suspicious"]),
        }
        for r in rows
    ]


def latest_diary_day(conn: sqlite3.Connection, account: str, scope_key_value: str) -> date | None:
    row = conn.execute(
        "SELECT MAX(substr(happened_at,1,10)) FROM memories "
        "WHERE account=? AND scope_key=? AND category=? AND deleted_at IS NULL",
        (account, scope_key_value, CATEGORY_DIARY),
    ).fetchone()
    return parse_label(row[0]) if row and row[0] else None
