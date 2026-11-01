# -*- coding: utf-8 -*-
"""写入管线（4.1 / 4.1.2 / 4.1.3 / 4.1.4 / 4.1.5）。

一条消息的写入顺序：
  1. 规范化 + 清洗（CQ 码）→ 脱敏 → 注入话术打标；
  2. message_id 幂等去重；
  3. 写 conversation_turns 流水（私聊全量；群聊非噪声全量，至少覆盖唤醒上下文）；
  4. 按 4.1 规则决定是否进 memories（必记 / 选记时间锁 / 不记）；
  5. 近似重复（24h 内余弦 ≥0.95 且长度差 <20%）→ 丢弃新条、旧条 recall_count+1；
  6. 同事务内做 fact_key 取代；
  7. 任何异常都只计数 + 降级，绝不让桥接卡住。

/v1/write 默认走异步队列（4.1.5）：submit() 只入队立即返回，单消费者线程落库。
"""

from __future__ import annotations

import json
import logging
import queue
import re
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Mapping
from datetime import datetime

import numpy as np

import store
import temporal
import textproc
from config import Settings, get_settings
from embedder import Embedder, from_blob, to_blob
from timeutil import TZ, date_only, hours_ago, iso_now, parse_iso

logger = logging.getLogger("qq_memory.writer")

_SOURCE_VALUES = {"auto", "llm", "import", "console"}
_ROLE_VALUES = {"user", "assistant"}
_EVENT_STATUS_VALUES = {"planned", "done", "cancelled"}
_EVENT_CANCEL_WORDS = (
    "不去了", "不出去了", "不出去", "不出门", "没去成", "去不了", "去不成",
    "取消了", "取消吧", "不约了", "计划有变", "临时有事", "改天吧", "又不去了",
)
_EVENT_RESCHEDULE_WORDS = (
    "改到", "改期", "改在", "推迟", "延后", "提前到", "挪到",
)
# assistant 的最终拍板：“那就后天 / 选好了就周末 / 确定下周三”。
_EVENT_FINAL_RE = re.compile(r"(那就|就|选好了|不改了|确定|决定)")


PLACEHOLDER_NAMES = {"私聊", "群聊", "私", "群", "private", "group", "unknown", "未知"}


def _clean_speaker_name(value: object) -> str | None:
    """桥接在私聊里常把 speaker.name 填成「私聊」这类占位词，别把它当人名存下来。"""
    text = _as_str(value)
    if not text or text.strip().lower() in PLACEHOLDER_NAMES:
        return None
    return text.strip()


def _as_str(value: object) -> str:
    return "" if value is None else str(value).strip()


def normalize_payload(payload: Mapping | None) -> dict:
    """把 API JSON 规范成内部写请求；非法输入抛 ValueError。"""
    if not isinstance(payload, Mapping):
        raise ValueError("payload 必须是对象")
    account = _as_str(payload.get("account"))
    if not account:
        raise ValueError("缺少 account")
    scope_type, user_id, group_id = store.parse_scope(payload.get("scope"))

    role = _as_str(payload.get("role") or "user").lower()
    if role not in _ROLE_VALUES:
        raise ValueError("role 必须是 user 或 assistant")

    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content 不能为空")

    meta_raw = payload.get("meta")
    meta = meta_raw if isinstance(meta_raw, Mapping) else {}
    speaker_raw = payload.get("speaker")
    speaker = speaker_raw if isinstance(speaker_raw, Mapping) else {}

    happened_at = _as_str(payload.get("happened_at")) or None
    if happened_at and parse_iso(happened_at) is None:
        raise ValueError("happened_at 不是合法 ISO8601")

    message_id = payload.get("message_id")
    message_id = None if message_id in (None, "") else str(message_id)

    source = _as_str(payload.get("source") or "auto").lower()
    if source not in _SOURCE_VALUES:
        source = "auto"
    trigger = _as_str(meta.get("trigger") or payload.get("trigger") or "passive").lower()
    forced = bool(
        meta.get("forced")
        or meta.get("mentions_bot")
        or trigger in {"wake", "command"}
    )

    session_id = _as_str(payload.get("session_id") or meta.get("session_id")) or None
    generation = payload.get("generation", meta.get("generation"))
    try:
        generation = int(generation) if generation is not None else 0
    except (TypeError, ValueError):
        generation = 0

    fact_key = _as_str(payload.get("fact_key")) or None
    fact_scope = _as_str(payload.get("fact_scope")) or None
    attachment = payload.get("attachment")
    if attachment is not None and not isinstance(attachment, (list, tuple)):
        raise ValueError("attachment 必须是数组")

    event_at = _as_str(payload.get("event_at")) or None
    valid_until = _as_str(payload.get("valid_until")) or None
    for label, value in (("event_at", event_at), ("valid_until", valid_until)):
        if value and parse_iso(value) is None:
            raise ValueError(f"{label} 不是合法 ISO8601")
    event_status = _as_str(payload.get("event_status")) or None
    if event_status and event_status not in _EVENT_STATUS_VALUES:
        raise ValueError("event_status 必须是 planned/done/cancelled")

    return {
        "account": account,
        "scope_type": scope_type,
        "user_id": user_id,
        "group_id": group_id,
        "scope_key": store.scope_key(account, scope_type, user_id, group_id),
        "role": role,
        "content": content,
        "message_id": message_id,
        "speaker_id": _as_str(speaker.get("id")) or user_id or None,
        "speaker_name": _clean_speaker_name(speaker.get("name")),
        "happened_at": happened_at,
        "source": source,
        "trigger": trigger,
        "forced": forced,
        "session_id": session_id,
        "generation": generation,
        "fact_key": fact_key,
        "fact_scope": fact_scope,
        "attachment": list(attachment) if attachment else None,
        "event_at": event_at,
        "valid_until": valid_until,
        "event_status": event_status,
    }


class MemoryWriter:
    """单消费者写队列；一个进程一个实例。"""

    def __init__(
        self,
        settings: Settings | None = None,
        embedder: Embedder | None = None,
        *,
        queue_max: int | None = None,
        autostart: bool = False,
    ):
        self.settings = settings or get_settings()
        self.embedder = embedder  # None = 不计算向量（测试/降级）
        self._queue: queue.Queue[dict] = queue.Queue(
            maxsize=queue_max or self.settings.queue_max
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._sampling: dict[str, deque[float]] = {}
        self._stats = {
            "write_total": 0,
            "write_dedup": 0,
            "write_dropped": 0,
            "write_fail": 0,
            "write_queued": 0,
            "write_recorded": 0,
            "write_turns": 0,
            "write_skipped": 0,
        }
        if autostart:
            self.start()

    # ------------------------------------------------------------------
    # 统计 / 生命周期
    # ------------------------------------------------------------------
    def _bump(self, key: str, n: int = 1) -> None:
        with self._lock:
            self._stats[key] = self._stats.get(key, 0) + n

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="memory-writer", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                payload = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.write(payload)
            except Exception as exc:  # 绝不让消费者线程死掉
                self._bump("write_fail")
                logger.warning("写队列处理失败：%s", type(exc).__name__)
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    def submit(self, payload: Mapping) -> dict:
        """异步入口：预检幂等后入队，立即返回。"""
        try:
            req = normalize_payload(payload)
        except ValueError as exc:
            self._bump("write_fail")
            return {"ok": False, "id": None, "deduped": False, "queued": False,
                    "error": str(exc)}

        # 幂等预检（只查索引，毫秒级）——让桥接立刻知道"这条已经有了"
        if req["message_id"]:
            try:
                conn = store.open_conn(self.settings)
                try:
                    row = store.find_memory_by_message(
                        conn, req["account"], req["message_id"], req["role"]
                    )
                finally:
                    conn.close()
                if row is not None:
                    self._bump("write_dedup")
                    return {"ok": True, "id": int(row["id"]), "deduped": True,
                            "queued": False}
            except sqlite3.Error:
                pass  # 查不到就照常入队，由消费者兜底

        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                pass
            self._bump("write_dropped")
            logger.warning("写队列已满，丢弃最旧一条（上限 %s）", self._queue.maxsize)
            try:
                self._queue.put_nowait(payload)
            except queue.Full:
                self._bump("write_dropped")
                return {"ok": False, "id": None, "deduped": False, "queued": False,
                        "error": "queue_full"}
        self._bump("write_queued")
        return {"ok": True, "id": None, "deduped": False, "queued": True}

    def write(self, payload: Mapping) -> dict:
        """同步落库（队列消费者与测试走这里）。"""
        self._bump("write_total")
        try:
            req = normalize_payload(payload)
        except ValueError as exc:
            self._bump("write_fail")
            return {"ok": False, "id": None, "deduped": False, "error": str(exc)}

        clean = textproc.clean_text(req["content"])
        if len(clean) > self.settings.max_content_chars:
            clean = clean[: self.settings.max_content_chars]
        redacted, redact_hits = textproc.redact(clean)
        suspicious = textproc.is_suspicious(clean)

        record_memory, reason = self._decide_memory(req, redacted)
        record_turn = not textproc.is_command(clean) and not textproc.is_noise(redacted)

        if textproc.is_noise(redacted):
            self._bump("write_skipped")  # 纯表情/CQ/链接：不写任何表
            return {"ok": True, "id": None, "deduped": True, "recorded": False,
                    "reason": "noise"}

        vec = None
        if record_memory and self.embedder is not None:
            vec = self.embedder.embed_passage(redacted)

        conn = store.open_conn(self.settings)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = store.find_memory_by_message(
                    conn, req["account"], req["message_id"], req["role"]
                )
                if existing is not None:
                    conn.execute("COMMIT")
                    self._bump("write_dedup")
                    return {"ok": True, "id": int(existing["id"]), "deduped": True,
                            "recorded": False, "reason": "message_id"}

                turn_id = None
                if record_turn:
                    turn_id = store.insert_turn(
                        conn,
                        {
                            "account": req["account"],
                            "scope_key": req["scope_key"],
                            "scope_type": req["scope_type"],
                            "session_id": req["session_id"],
                            "generation": req["generation"],
                            "seq": store.next_turn_seq(
                                conn, req["scope_key"], req["session_id"]
                            ),
                            "role": req["role"],
                            "speaker_id": req["speaker_id"],
                            "speaker_name": req["speaker_name"],
                            "message_id": req["message_id"],
                            "content": redacted,
                            "happened_at": req["happened_at"],
                            "created_at": iso_now(),
                        },
                    )
                    if turn_id is not None:
                        self._bump("write_turns")

                memory_id = None
                superseded: list[int] = []
                event_update = None
                if self.settings.temporal_enabled:
                    event_update = self._apply_event_update(conn, req, redacted)
                if record_memory:
                    # fact_key 记录必须先走取代：否则「身高 165 → 168」这种高相似更新
                    # 会被近似重复去重吞掉，事实永远不更新（8.1 验收会暴露）。
                    near = None
                    if not req["fact_key"]:
                        near = self._find_near_duplicate(conn, req, vec, redacted)
                    if near is not None:
                        conn.execute(
                            "UPDATE memories SET recall_count=COALESCE(recall_count,0)+1,"
                            " last_recalled_at=? WHERE id=?",
                            (iso_now(), int(near["id"])),
                        )
                        conn.execute("COMMIT")
                        self._bump("write_dedup")
                        return {"ok": True, "id": int(near["id"]), "deduped": True,
                                "recorded": False, "reason": "near_duplicate",
                                "event_update": event_update}

                    memory_id = store.insert_memory(
                        conn,
                        self._build_memory(
                            req, redacted, vec, suspicious, event_update=event_update
                        ),
                    )
                    fact_key = req["fact_key"]
                    if fact_key:
                        superseded = store.mark_superseded(
                            conn,
                            account=req["account"],
                            fact_scope=req["fact_scope"] or req["scope_key"],
                            fact_key=fact_key,
                            new_id=memory_id,
                        )
                    self._bump("write_recorded")

                conn.execute("COMMIT")
                return {
                    "ok": True,
                    "id": memory_id,
                    "deduped": False,
                    "recorded": bool(memory_id),
                    "reason": reason,
                    "superseded": superseded,
                    "turn_id": turn_id,
                    "redacted": redact_hits,
                    "event_update": event_update,
                }
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                # 并发下唯一索引兜底：视为重复
                self._bump("write_dedup")
                return {"ok": True, "id": None, "deduped": True, "recorded": False,
                        "reason": "unique_index"}
            except Exception:
                conn.execute("ROLLBACK")
                raise
        except sqlite3.Error as exc:
            self._bump("write_fail")
            logger.warning("写库失败（已丢弃）：%s", type(exc).__name__)
            return {"ok": False, "id": None, "deduped": False, "error": "db_error"}
        except Exception as exc:  # 兜底：任何内部错误都不能冒泡到 HTTP/队列
            self._bump("write_fail")
            logger.warning("写入内部错误（已丢弃）：%s", type(exc).__name__)
            return {"ok": False, "id": None, "deduped": False, "error": "internal_error"}
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 规则
    # ------------------------------------------------------------------
    def _decide_memory(self, req: dict, content: str) -> tuple[bool, str]:
        """4.1 必记/选记/不记。返回 (是否入 memories, 原因)。"""
        if req["role"] == "assistant":
            return (len(content) >= 8, "assistant")
        if textproc.is_noise(content) or textproc.is_command(content):
            return False, "noise"
        if req["scope_type"] == "private":
            return (len(content) >= 2, "private_always")
        if req["forced"]:
            return True, "wake"
        if textproc.has_commitment(content) or textproc.has_preference(content):
            return True, "commitment"
        if len(content) >= 8 and (
            textproc.is_question(content) or textproc.has_concrete_info(content)
        ):
            allowed = self._sampling_allowed(req["scope_key"])
            return allowed, "sampled" if allowed else "sampled_out"
        return False, "group_low_signal"

    def _sampling_allowed(self, scope_key_value: str) -> bool:
        """群聊选记时间锁：3 分钟 ≤2 条、10 分钟 ≤5 条（4.1）。"""
        now = time.monotonic()
        with self._lock:
            dq = self._sampling.setdefault(scope_key_value, deque())
            while dq and now - dq[0] > 600:
                dq.popleft()
            in_3min = sum(1 for ts in dq if now - ts <= 180)
            if in_3min >= self.settings.sampling_3min:
                return False
            if len(dq) >= self.settings.sampling_10min:
                return False
            dq.append(now)
            return True

    @staticmethod
    def _detect_event_action(content: str) -> str | None:
        if any(word in content for word in _EVENT_CANCEL_WORDS):
            return "cancel"
        if any(word in content for word in _EVENT_RESCHEDULE_WORDS):
            return "reschedule"
        return None

    @staticmethod
    def _nearest_event(conn: sqlite3.Connection, req: dict) -> sqlite3.Row | None:
        rows = conn.execute(
            "SELECT id, event_at, valid_until FROM memories "
            "WHERE account=? AND scope_key=? AND role='user' AND category='event' "
            "AND event_status='planned' AND event_at IS NOT NULL AND deleted_at IS NULL "
            "ORDER BY id DESC LIMIT 20",
            (req["account"], req["scope_key"]),
        ).fetchall()
        if not rows:
            return None
        now = datetime.now(TZ)
        best = None
        best_delta = None
        for row in rows:
            ts = parse_iso(row["event_at"])
            if ts is None:
                continue
            delta = abs((ts - now).total_seconds())
            if best_delta is None or delta < best_delta:
                best, best_delta = row, delta
        return best

    def _apply_final_choice(
        self, conn: sqlite3.Connection, req: dict, content: str
    ) -> dict | None:
        """assistant 最终拍板：保留选中日期，取消同窗口内其他 planned 计划。

        只处理明确句式 + 能解析出日期的消息；问句/解析失败不写。
        如果选中日期已有计划，就保留它、不重复建事件；否则本条消息会建一条计划。
        """
        if not _EVENT_FINAL_RE.search(content):
            return None
        if textproc.is_question(content):
            return {"action": "final_choice_question", "create": False}
        parsed = temporal.parse_event_time(
            content, parse_iso(req["happened_at"]) or datetime.now(TZ)
        )
        if parsed is None:
            return {"action": "final_choice_unparsed", "create": False}

        rows = conn.execute(
            "SELECT id, event_at FROM memories "
            "WHERE account=? AND scope_key=? AND category='event' "
            "AND event_status='planned' AND event_at IS NOT NULL AND deleted_at IS NULL "
            "AND created_at>=? ORDER BY id DESC",
            (req["account"], req["scope_key"], hours_ago(0.5)),
        ).fetchall()
        chosen_date = date_only(parsed.event_at)
        keep_id = None
        cancelled: list[int] = []
        for row in rows:
            memory_id = int(row["id"])
            if date_only(row["event_at"]) == chosen_date and keep_id is None:
                keep_id = memory_id
                continue
            conn.execute(
                "UPDATE memories SET event_status='cancelled' "
                "WHERE id=? AND event_status='planned'",
                (memory_id,),
            )
            cancelled.append(memory_id)
        return {
            "action": "final_choice",
            "id": keep_id,
            "event_at": parsed.event_at,
            "valid_until": parsed.valid_until,
            "create": keep_id is None,
            "cancelled": cancelled,
        }

    def _apply_event_update(
        self, conn: sqlite3.Connection, req: dict, content: str
    ) -> dict | None:
        """Phase 2：用户说“不去了/改到周末”时，更新最近一条 planned 事件。

        返回的 action 同时用于抑制本条消息自身的事件解析，避免“明天不去了”
        被写成一条新的“明天”计划。找不到候选时也返回 action，但仍不新建事件。
        """
        if req["role"] == "assistant":
            return self._apply_final_choice(conn, req, content)
        if req["role"] != "user":
            return None
        action = self._detect_event_action(content)
        if action is None:
            return None
        # 问句是提议/确认，不是已经发生或已定的计划；不更新旧事件，也不新建事件。
        if textproc.is_question(content):
            return {"action": f"{action}_question", "id": None}
        candidate = self._nearest_event(conn, req)
        if candidate is None:
            return {"action": action, "id": None}
        memory_id = int(candidate["id"])
        if action == "cancel":
            conn.execute(
                "UPDATE memories SET event_status='cancelled' "
                "WHERE id=? AND event_status='planned'",
                (memory_id,),
            )
            return {"action": "cancel", "id": memory_id}
        parsed = temporal.parse_event_time(
            content, parse_iso(req["happened_at"]) or datetime.now(TZ)
        )
        if parsed is None:
            return {"action": "reschedule_unparsed", "id": memory_id}
        conn.execute(
            "UPDATE memories SET event_at=?, valid_until=?, event_status='planned' "
            "WHERE id=? AND event_status='planned'",
            (parsed.event_at, parsed.valid_until, memory_id),
        )
        return {
            "action": "reschedule",
            "id": memory_id,
            "event_at": parsed.event_at,
            "valid_until": parsed.valid_until,
        }

    def _build_memory(
        self,
        req: dict,
        content: str,
        vec: np.ndarray | None,
        suspicious: bool,
        *,
        event_update: dict | None = None,
    ) -> dict:
        tokens = textproc.tokenize(content)
        tags = textproc.extract_tags(content) if req["role"] == "user" else []
        fact_key = req["fact_key"]
        category = textproc.detect_category(content, fact_key=fact_key)
        dim = self.embedder.dim if (vec is not None and self.embedder) else None

        # 事件时效（v2.3）：只对 user + event + 非 fact 生效；
        # 解析失败/关闭开关时三列为空，行为与 v2.2 完全一致。
        event_at = req.get("event_at") if self.settings.temporal_enabled else None
        valid_until = req.get("valid_until") if self.settings.temporal_enabled else None
        event_status = req.get("event_status") if self.settings.temporal_enabled else None
        suppress_event = bool(
            event_update
            and str(event_update.get("action", "")).startswith(("cancel", "reschedule"))
        )
        final_choice_create = bool(
            event_update
            and event_update.get("action") == "final_choice"
            and event_update.get("create")
        )
        if event_update and str(event_update.get("action", "")).startswith("final_choice"):
            suppress_event = not final_choice_create
        if final_choice_create:
            event_at = event_update.get("event_at")
            valid_until = event_update.get("valid_until")
            event_status = "planned"
        if (
            self.settings.temporal_enabled
            and not suppress_event
            and not final_choice_create
            and req["role"] == "user"
            and not fact_key
            and category == "event"
            and not textproc.is_question(content)
        ):
            if not event_at:
                base = parse_iso(req["happened_at"]) or datetime.now(TZ)
                try:
                    parsed = temporal.parse_event_time(content, base)
                except Exception:
                    parsed = None
                if parsed is not None:
                    event_at = parsed.event_at
                    valid_until = valid_until or parsed.valid_until
                    event_status = event_status or "planned"
                    logger.debug("事件时间解析命中：precision=%s", parsed.precision)
            elif not event_status:
                event_status = "planned"

        return {
            "account": req["account"],
            "scope_key": req["scope_key"],
            "scope_type": req["scope_type"],
            "user_id": req["user_id"],
            "group_id": req["group_id"],
            "speaker_id": req["speaker_id"],
            "speaker_name": req["speaker_name"],
            "role": req["role"],
            "message_id": req["message_id"],
            "session_id": req["session_id"],
            "generation": req["generation"],
            "content": content,
            "attachment": json.dumps(req["attachment"], ensure_ascii=False)
            if req["attachment"]
            else None,
            "embedding": to_blob(vec) if vec is not None else None,
            "embedding_model": self.settings.model_name if vec is not None else None,
            "embedding_dim": dim,
            "visibility": textproc.detect_visibility(req["scope_type"], content),
            "fact_key": fact_key,
            "fact_scope": (req["fact_scope"] or req["scope_key"]) if fact_key else None,
            "category": category,
            "importance": textproc.detect_importance(content),
            "emotion": textproc.detect_emotion(content),
            "is_active": 1,
            "tokens": textproc.tokens_text(tokens),
            "tags": json.dumps(tags, ensure_ascii=False),
            "source": req["source"],
            "created_at": iso_now(),
            "happened_at": req["happened_at"],
            "event_at": event_at,
            "valid_until": valid_until,
            "event_status": event_status,
            "recall_count": 0,
            "schema_version": 2,
            "suspicious": 1 if suspicious else 0,
        }

    def _find_near_duplicate(
        self, conn: sqlite3.Connection, req: dict, vec: np.ndarray | None, content: str
    ) -> sqlite3.Row | None:
        """4.1.2 近似重复：同 scope 24h 内、余弦 ≥0.95、长度差 <20%。"""
        if vec is None:
            return None
        rows = conn.execute(
            "SELECT id, content, embedding FROM memories "
            "WHERE scope_key=? AND is_active=1 AND deleted_at IS NULL "
            "AND embedding IS NOT NULL AND embedding_model=? AND embedding_dim=? "
            "AND created_at>=? ORDER BY id DESC LIMIT 50",
            (
                req["scope_key"],
                self.settings.model_name,
                self.embedder.dim if self.embedder else self.settings.embedding_dim,
                hours_ago(self.settings.dedupe_window_hours),
            ),
        ).fetchall()
        best: sqlite3.Row | None = None
        best_sim = -1.0
        for row in rows:
            other = from_blob(row["embedding"])
            if other is None or other.size != vec.size:
                continue
            denom = float(np.linalg.norm(other))
            if denom <= 0:
                continue
            sim = float(np.dot(vec, other / denom))
            if sim < self.settings.dedupe_sim:
                continue
            a, b = content, str(row["content"] or "")
            longer = max(len(a), len(b), 1)
            if abs(len(a) - len(b)) / longer >= self.settings.dedupe_len_ratio:
                continue
            if sim > best_sim:
                best, best_sim = row, sim
        return best


_default_writer: MemoryWriter | None = None
_default_lock = threading.Lock()


def get_writer(settings: Settings | None = None, embedder: Embedder | None = None) -> MemoryWriter:
    """进程级单例（供 FastAPI 使用）。"""
    global _default_writer
    with _default_lock:
        if _default_writer is None:
            _default_writer = MemoryWriter(settings, embedder, autostart=True)
            _default_writer.start()
        return _default_writer
