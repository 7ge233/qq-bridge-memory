# -*- coding: utf-8 -*-
"""画像存取层（T1）：写入 / 读取 / 渲染 / 缓存。

边界
----
* 只碰 memories(category='profile') 与 scope_state；不碰召回、不碰 HTTP、不碰模型。
* 判断「是不是画像行」只认 category='profile'，任何时候都不看 source（指令书 4.1）。
* locked 字段（self.name / self.traits）是人设常量：不落库、不注入、不播种（4.4 / 8.2）。
* 只做私聊画像：scope_key 必须是 qq:<account>:private:<uid>，且 account 必须与之一致
  （坑 #7：只按 scope_key 过滤，换 bot 后串号）。

对外接口（其它模块只准用这几个）
--------------------------------
    upsert(conn, account, scope_key, items, source, ...) -> dict  # 回执，键与 5.2 响应体一一对应
    load(conn, account, scope_key) -> list[dict]
    render_block(conn, account, scope_key, max_chars=400) -> str
    invalidate(account, scope_key) -> None

水位推进在 upsert 内部、与写行同一个事务：不会出现「行写了、水位没动」。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Iterable, Mapping

import diary
import store
from timeutil import TZ, iso_now, parse_iso

logger = logging.getLogger("qq_memory.profile")

CATEGORY_PROFILE = "profile"
SCHEMA_VERSION = 5
IMPORTANCE_PROFILE = 0.9

# 入参 source（调用方语义）→ 落库 source（侧车既有枚举 auto/llm/import/console）
SOURCE_TO_COLUMN = {
    "model": "llm",
    "user": "console",
    "llm": "llm",
    "console": "console",
}

PERMISSION_LOCKED = "locked"
PERMISSION_USER_ONLY = "user_only"
PERMISSION_EDITABLE = "editable"

BASIS_STATED = "stated"
BASIS_INFERRED = "inferred"
BASES = (BASIS_STATED, BASIS_INFERRED)

DEFAULT_RENDER_CHARS = 400
CACHE_TTL_S = 60.0
EVIDENCE_MAX_CHARS = 30

# scope_state 键（4.3）
KEY_LAST_AT = "last_profile_at"
KEY_LAST_DAY = "last_profile_day"
KEY_RUNS = "profile_runs"


# ---------------------------------------------------------------------------
# 字段白名单（4.4）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FieldSpec:
    fact_key: str
    label: str          # 渲染用的中文标签
    permission: str     # locked / user_only / editable
    min_chars: int
    max_chars: int
    section: str        # user → 【关于他】；self → 【关于我】


# 顺序 = 4.4 表顺序：渲染按它排，便于 diff
FIELD_SPECS: tuple[FieldSpec, ...] = (
    # —— 用户画像（8）——
    FieldSpec("user.name", "称呼", PERMISSION_EDITABLE, 2, 12, "user"),
    FieldSpec("user.identity", "身份", PERMISSION_EDITABLE, 15, 40, "user"),
    FieldSpec("user.schedule", "作息", PERMISSION_EDITABLE, 15, 40, "user"),
    FieldSpec("user.topics", "常聊", PERMISSION_EDITABLE, 15, 60, "user"),
    FieldSpec("user.style", "说话风格", PERMISSION_EDITABLE, 15, 40, "user"),
    FieldSpec("user.boundaries", "雷区", PERMISSION_EDITABLE, 15, 60, "user"),
    FieldSpec("user.current", "近况", PERMISSION_EDITABLE, 15, 60, "user"),
    FieldSpec("user.relationship", "相处方式", PERMISSION_EDITABLE, 15, 40, "user"),
    # —— 机器人自我画像（6），前两个是常量，不落库不注入 ——
    FieldSpec("self.name", "自我称呼", PERMISSION_LOCKED, 0, 0, "self"),
    FieldSpec("self.traits", "性格底色", PERMISSION_LOCKED, 0, 0, "self"),
    FieldSpec("self.speech", "说话习惯", PERMISSION_EDITABLE, 15, 40, "self"),
    FieldSpec("self.interests", "兴趣", PERMISSION_EDITABLE, 15, 60, "self"),
    FieldSpec("self.observations", "对用户的观察", PERMISSION_EDITABLE, 15, 60, "self"),
    FieldSpec("self.growth", "相处中的变化", PERMISSION_EDITABLE, 15, 60, "self"),
)

FIELD_SPECS_BY_KEY: dict[str, FieldSpec] = {spec.fact_key: spec for spec in FIELD_SPECS}

SECTION_TITLES = {"user": "【关于他】", "self": "【关于我】"}

# 截断优先级（8.1）：数字越大越先被丢。四档高优先级的数字小。
DROP_RANK: dict[str, int] = {
    "user.boundaries": 0,     # 最高，最后被丢
    "user.identity": 1,
    "self.observations": 2,
    "user.topics": 3,
}
DROP_RANK_DEFAULT = 4

# 这些 skip 原因不算「失败」，可以推进水位（7：模型判定无变化但输出合法）
NOOP_SKIP_CODES = frozenset({"unchanged", "duplicate_in_request"})


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _s(value: object) -> str:
    return "" if value is None else str(value)


def _loads(raw: object) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _clamp_confidence(value: object, default: float) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = default
    return max(0.0, min(1.0, num))


def parse_private_scope(account: object, scope_key: object) -> tuple[str, str]:
    """校验 scope_key，返回 (account, user_id)。只接受私聊（决策 7）。"""
    acc = _s(account).strip()
    key = _s(scope_key).strip()
    if not acc:
        raise ValueError("account 不能为空")
    parts = key.split(":")
    if len(parts) != 4 or parts[2] != "private":
        raise ValueError(f"scope_key 必须是 <prefix>:<account>:private:<uid>，收到 {key!r}")
    if parts[1] != acc:
        raise ValueError(f"scope_key 里的账号 {parts[1]!r} 与 account {acc!r} 不一致（禁止串号）")
    user_id = parts[3].strip()
    if not user_id:
        raise ValueError("scope_key 缺少用户 id")
    return acc, user_id


def _normalize_source(source: object) -> str:
    key = _s(source).strip().lower()
    if key not in SOURCE_TO_COLUMN:
        raise ValueError(f"source 只接受 model/user，收到 {source!r}")
    return SOURCE_TO_COLUMN[key]


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------

def load(conn, account: object, scope_key: object) -> list[dict]:
    """当前生效的画像条目，按 fact_key 排序。只认 category='profile' + is_active=1。"""
    acc, _ = parse_private_scope(account, scope_key)
    rows = conn.execute(
        "SELECT id, fact_key, content, meta, source, created_at FROM memories "
        "WHERE account = ? AND scope_key = ? AND category = ? AND is_active = 1 "
        "ORDER BY fact_key",
        (acc, _s(scope_key).strip(), CATEGORY_PROFILE),
    ).fetchall()
    items: list[dict] = []
    for row in rows:
        meta = _loads(row["meta"])
        items.append(
            {
                "id": int(row["id"]),
                "fact_key": _s(row["fact_key"]),
                "content": _s(row["content"]),
                "updated_at": _s(row["created_at"]),
                "source": _s(row["source"]),
                "basis": _s(meta.get("basis")),
                "confidence": meta.get("confidence"),
                "meta": meta,
            }
        )
    return items


def load_map(conn, account: object, scope_key: object) -> dict[str, dict]:
    return {item["fact_key"]: item for item in load(conn, account, scope_key)}


# ---------------------------------------------------------------------------
# 取数辅助
# ---------------------------------------------------------------------------

def _active_rows(conn, account: str, scope_key: str) -> dict[str, dict]:
    """现存生效画像：fact_key → {id, content, basis}。"""
    rows = conn.execute(
        "SELECT id, fact_key, content, meta FROM memories "
        "WHERE account = ? AND scope_key = ? AND category = ? AND is_active = 1",
        (account, scope_key, CATEGORY_PROFILE),
    ).fetchall()
    out: dict[str, dict] = {}
    for row in rows:
        fact_key = _s(row["fact_key"])
        if not fact_key:
            continue
        out[fact_key] = {
            "id": int(row["id"]),
            "content": _s(row["content"]),
            "basis": _s(_loads(row["meta"]).get("basis")),
        }
    return out


def _window_bounds(window: object) -> tuple[str, str] | None:
    if not isinstance(window, Mapping):
        return None
    start = _s(window.get("from")).strip()
    end = _s(window.get("to")).strip()
    if not start or not end:
        return None
    return start, end


def _window_text(conn, account: str, scope_key: str, bounds: tuple[str, str]) -> str:
    """窗口内本 scope 的私聊原文，拼成一段（evidence 子串校验用，5.2 规则 7）。"""
    start, end = bounds
    rows = conn.execute(
        "SELECT content FROM conversation_turns "
        "WHERE account = ? AND scope_key = ? AND scope_type = 'private' "
        "AND created_at >= ? AND created_at < ? ORDER BY seq ASC",
        (account, scope_key, start, end),
    ).fetchall()
    return "\n".join(_s(row["content"]) for row in rows)


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------

def _skip(out: list[dict], fact_key: str, code: str, reason: str) -> None:
    out.append({"fact_key": fact_key, "code": code, "reason": reason})


def _validate(
    raw: object,
    *,
    existing: Mapping[str, dict],
    has_window: bool,
    window_text_fn,
    seen: set[str],
    skipped: list[dict],
    downgraded: list[dict],
) -> dict | None:
    """按 5.2 的规则逐条校验；通过则返回待写条目，否则写进 skipped 并返回 None。"""
    if not isinstance(raw, Mapping):
        _skip(skipped, "", "invalid_item", "条目不是对象")
        return None

    fact_key = _s(raw.get("fact_key")).strip()
    spec = FIELD_SPECS_BY_KEY.get(fact_key)
    if spec is None:
        _skip(skipped, fact_key, "unknown_fact_key", "不在字段白名单内")
        return None

    # 规则 4：locked 任何 source 都 skip（常量不落库）
    if spec.permission == PERMISSION_LOCKED:
        _skip(
            skipped,
            fact_key,
            "locked",
            "locked 字段不落库，只能改代码里的常量",
        )
        return None

    content = _s(raw.get("content")).strip()
    if not content:
        _skip(skipped, fact_key, "empty", "内容为空")
        return None

    # 规则 3：字数
    length = len(content)
    if length < spec.min_chars:
        _skip(skipped, fact_key, "too_short", f"{length} 字少于下限 {spec.min_chars}")
        return None
    if length > spec.max_chars:
        _skip(skipped, fact_key, "too_long", f"{length} 字超过上限 {spec.max_chars}")
        return None

    # 规则 6：同一请求内重复
    if fact_key in seen:
        _skip(skipped, fact_key, "duplicate_in_request", "本次请求内重复出现")
        return None
    seen.add(fact_key)

    basis = _s(raw.get("basis")).strip().lower()
    # 5.2 / 6.4：basis 不合法或缺失一律按 inferred
    if basis not in BASES:
        basis = BASIS_INFERRED

    evidence = _s(raw.get("evidence")).strip()

    # 规则 7：evidence 原文校验
    if not has_window:
        # 没带 window：user.name 一律丢弃，其它按 inferred
        if fact_key == "user.name":
            _skip(
                skipped,
                fact_key,
                "name_needs_window",
                "没带 window，称呼无法做原文校验，直接丢弃",
            )
            return None
        if basis == BASIS_STATED:
            basis = BASIS_INFERRED
            downgraded.append({"fact_key": fact_key, "reason": "没有 window，降级为 inferred"})
    elif basis == BASIS_STATED:
        haystack = window_text_fn()
        ok = bool(evidence) and evidence in haystack
        if not ok:
            if fact_key == "user.name":
                _skip(
                    skipped,
                    fact_key,
                    "evidence_not_in_source",
                    "声称 stated，但 evidence 不是本区间原文的子串（宁可空着也不瞎喊）",
                )
                return None
            basis = BASIS_INFERRED
            downgraded.append({"fact_key": fact_key, "reason": "evidence 不是原文子串，降级为 inferred"})

    # user.name 只接受 stated（4.4）
    if fact_key == "user.name" and basis != BASIS_STATED:
        _skip(skipped, fact_key, "name_needs_stated", "称呼只接受 stated，推断不可用")
        return None

    current = existing.get(fact_key)

    # 规则 5：推断不覆盖陈述
    if basis == BASIS_INFERRED and current and current.get("basis") == BASIS_STATED:
        _skip(skipped, fact_key, "inferred_over_stated", "现存是 stated，推断不能覆盖")
        return None

    # 规则 6：内容完全相同
    if current and current.get("content") == content:
        _skip(skipped, fact_key, "unchanged", "与现存内容完全相同")
        return None

    return {
        "fact_key": fact_key,
        "content": content,
        "basis": basis,
        "evidence": evidence[:EVIDENCE_MAX_CHARS],
        "confidence": _clamp_confidence(
            raw.get("confidence"), 0.9 if basis == BASIS_STATED else 0.7
        ),
    }


def upsert(
    conn,
    account: object,
    scope_key: object,
    items: Iterable[object],
    source: object,
    *,
    window: object = None,
    meta_common: Mapping[str, object] | None = None,
    now: str | None = None,
    day_cut_hour: int = diary.DEFAULT_CUT_HOUR,
) -> dict:
    """写入画像条目并推进水位，返回回执（键与 5.2 响应体一一对应）。

        {"written": 4,
         "skipped": [{"fact_key": "self.traits", "reason": "locked 字段不落库，…"}],
         "downgraded": 1,
         "advanced": true,
         "last_profile_day": "2026-11-17"}

    写行与水位在同一个 BEGIN IMMEDIATE 事务里：要么都成，要么都不成（5.2 规则 9），
    不会留下「行写了、水位没动」的中间态（那会让下次重跑把版本链重复撑大）。
    """
    acc, user_id = parse_private_scope(account, scope_key)
    db_source = _normalize_source(source)
    stamp = now or iso_now()
    key = _s(scope_key).strip()

    existing = _active_rows(conn, acc, key)
    bounds = _window_bounds(window)
    memo: dict[str, str] = {}

    def window_text_fn() -> str:
        if "text" not in memo:
            memo["text"] = _window_text(conn, acc, key, bounds) if bounds else ""
        return memo["text"]

    skipped: list[dict] = []
    downgraded: list[dict] = []
    accepted: list[dict] = []
    seen: set[str] = set()

    for raw in items or ():
        entry = _validate(
            raw,
            existing=existing,
            has_window=bounds is not None,
            window_text_fn=window_text_fn,
            seen=seen,
            skipped=skipped,
            downgraded=downgraded,
        )
        if entry is not None:
            accepted.append(entry)

    common = dict(meta_common or {})
    written = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for entry in accepted:
            meta = {
                "basis": entry["basis"],
                "confidence": entry["confidence"],
                "evidence": entry["evidence"],
                "generatedAt": stamp,
            }
            for extra_key in ("model", "runId"):
                if common.get(extra_key):
                    meta[extra_key] = _s(common[extra_key])
            cursor = conn.execute(
                "INSERT INTO memories ("
                "account, scope_key, scope_type, user_id, content, category, fact_key,"
                " meta, importance, is_active, source, happened_at, schema_version,"
                " state, created_at) "
                "VALUES (?,?, 'private', ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 'active', ?)",
                (
                    acc,
                    key,
                    user_id,
                    entry["content"],
                    CATEGORY_PROFILE,
                    entry["fact_key"],
                    json.dumps(meta, ensure_ascii=False),
                    IMPORTANCE_PROFILE,
                    db_source,
                    stamp,
                    SCHEMA_VERSION,
                    stamp,
                ),
            )
            new_id = int(cursor.lastrowid or 0)
            # 5.2 规则 8：同 fact_key 的旧行失效，superseded_by 指向新行（版本链可回溯）
            old_ids = [
                int(row["id"])
                for row in conn.execute(
                    "SELECT id FROM memories WHERE account = ? AND scope_key = ? "
                    "AND fact_key = ? AND category = ? AND is_active = 1 AND id <> ?",
                    (acc, key, entry["fact_key"], CATEGORY_PROFILE, new_id),
                ).fetchall()
            ]
            if old_ids:
                conn.execute(
                    "UPDATE memories SET is_active = 0, superseded_by = ? "
                    "WHERE account = ? AND scope_key = ? AND fact_key = ? "
                    "AND category = ? AND is_active = 1 AND id <> ?",
                    (new_id, acc, key, entry["fact_key"], CATEGORY_PROFILE, new_id),
                )
                # state 是侧车的唯一真相，is_active 只是它的派生列
                store.set_state(conn, old_ids, store.STATE_INACTIVE)
            written += 1

        last_profile_day, advanced = _advance_watermark(
            conn,
            acc,
            key,
            stamp=stamp,
            window=bounds,
            written=written,
            skipped=skipped,
            cut_hour=day_cut_hour,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    if written:
        invalidate(acc, key)
    return {
        "written": written,
        "skipped": [
            {"fact_key": one["fact_key"], "reason": one["reason"]} for one in skipped
        ],
        "downgraded": len(downgraded),
        "advanced": advanced,
        "last_profile_day": last_profile_day,
    }


# ---------------------------------------------------------------------------
# scope_state
# ---------------------------------------------------------------------------

def get_state(conn, account: object, scope_key: object, key: str, default: str = "") -> str:
    row = conn.execute(
        "SELECT value FROM scope_state WHERE account = ? AND scope_key = ? AND key = ?",
        (_s(account).strip(), _s(scope_key).strip(), str(key)),
    ).fetchone()
    return _s(row["value"]) if row is not None else default


def set_state(conn, account: object, scope_key: object, key: str, value: object) -> None:
    conn.execute(
        "INSERT INTO scope_state (account, scope_key, key, value, updated_at) "
        "VALUES (?,?,?,?,?) "
        "ON CONFLICT(account, scope_key, key) DO UPDATE SET value = excluded.value,"
        " updated_at = excluded.updated_at",
        (_s(account).strip(), _s(scope_key).strip(), str(key), _s(value), iso_now()),
    )


def _logical_day_of(stamp: str | None, cut_hour: int = diary.DEFAULT_CUT_HOUR) -> str | None:
    """逻辑日（05:00 切，坑 #6）：绝不用 date(created_at)。"""
    parsed = parse_iso(stamp)
    if parsed is None:
        return None
    return diary.logical_day(parsed.astimezone(TZ), cut_hour).isoformat()


def _advance_watermark(
    conn,
    account: str,
    scope_key: str,
    *,
    stamp: str,
    window: tuple[str, str] | None,
    written: int,
    skipped: list[dict],
    cut_hour: int,
) -> tuple[str | None, bool]:
    """推进水位（4.3 / 7）：只有「至少写入 1 条」或「全部是 no-op」才推。

    失败或全部字段被丢弃时不推进，下次会重跑同一段（宁可重跑，不留空洞）。
    """
    noop_only = bool(skipped) and all(item.get("code") in NOOP_SKIP_CODES for item in skipped)
    if written <= 0 and not noop_only:
        return None, False

    covered = _logical_day_of(window[1] if window else stamp, cut_hour)
    set_state(conn, account, scope_key, KEY_LAST_AT, stamp)
    if covered:
        set_state(conn, account, scope_key, KEY_LAST_DAY, covered)
    try:
        runs = int(get_state(conn, account, scope_key, KEY_RUNS, "0") or 0)
    except ValueError:
        runs = 0
    set_state(conn, account, scope_key, KEY_RUNS, str(runs + 1))
    return covered, True


# ---------------------------------------------------------------------------
# 渲染（8.1）+ 缓存（8.2）
# ---------------------------------------------------------------------------

_CACHE: dict[tuple[str, str, int], tuple[float, str]] = {}
_CACHE_LOCK = threading.Lock()


def invalidate(account: object, scope_key: object) -> None:
    """某个 scope 的画像变了：立刻让渲染缓存失效。"""
    acc = _s(account).strip()
    key = _s(scope_key).strip()
    with _CACHE_LOCK:
        for cache_key in [k for k in _CACHE if k[0] == acc and k[1] == key]:
            _CACHE.pop(cache_key, None)


def clear_cache() -> None:
    """清空全部渲染缓存（测试用）。"""
    with _CACHE_LOCK:
        _CACHE.clear()


def _sections(fields: Mapping[str, str]) -> list[list]:
    """按 4.4 顺序分组；空字段整行省略，locked 字段永不出现。"""
    sections: list[list] = []
    for section in ("user", "self"):
        lines = []
        for spec in FIELD_SPECS:
            if spec.section != section or spec.permission == PERMISSION_LOCKED:
                continue
            content = _s(fields.get(spec.fact_key)).strip()
            if content:
                lines.append((spec, content))
        if lines:
            sections.append([section, lines])
    return sections


def _compose(sections: list[list]) -> str:
    chunks = []
    for section, lines in sections:
        if not lines:
            continue
        body = "\n".join(f"{spec.label}：{content}" for spec, content in lines)
        chunks.append(f"{SECTION_TITLES[section]}\n{body}")
    return "\n\n".join(chunks)


def _truncate(sections: list[list], max_chars: int) -> str:
    """超过上限时按 8.1 优先级丢行：boundaries > identity > observations > topics > 其余。"""
    while sections:
        text = _compose(sections)
        if len(text) <= max_chars:
            return text
        target = None
        for section_index, (_, lines) in enumerate(sections):
            for line_index, (spec, _content) in enumerate(lines):
                rank = DROP_RANK.get(spec.fact_key, DROP_RANK_DEFAULT)
                if target is None or rank >= target[0]:
                    target = (rank, section_index, line_index)
        if target is None:
            break
        _, section_index, line_index = target
        sections[section_index][1].pop(line_index)
        if not sections[section_index][1]:
            sections.pop(section_index)
    return _compose(sections)


def _render_uncached(conn, account: str, scope_key: str, max_chars: int) -> str:
    fields = {
        item["fact_key"]: item["content"]
        for item in load(conn, account, scope_key)
        if item["content"]
    }
    if not fields:
        return ""
    return _truncate(_sections(fields), max_chars)


def render_block(
    conn,
    account: object,
    scope_key: object,
    max_chars: int = DEFAULT_RENDER_CHARS,
) -> str:
    """渲染画像注入块（≤ max_chars 字）；无画像返回空串，不报错。60 秒 TTL 缓存。"""
    acc = _s(account).strip()
    key = _s(scope_key).strip()
    try:
        budget = int(max_chars)
    except (TypeError, ValueError):
        budget = DEFAULT_RENDER_CHARS
    if not acc or not key or budget <= 0:
        return ""

    cache_key = (acc, key, budget)
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(cache_key)
        if hit is not None and hit[0] > now:
            return hit[1]

    try:
        block = _render_uncached(conn, acc, key, budget)
    except Exception:  # noqa: BLE001 - 画像永远是可选增强，不能把召回带崩
        logger.warning("画像渲染失败，本次跳过", exc_info=True)
        return ""
    with _CACHE_LOCK:
        _CACHE[cache_key] = (now + CACHE_TTL_S, block)
    return block
