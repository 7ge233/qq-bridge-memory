# -*- coding: utf-8 -*-
"""召回日志：把「这一次给模型看了什么」落库，供控制台 /console/recall 核对。

为什么需要它：记忆系统坏掉时不报错，只是「这次没想起你」。没有这份流水，
排查只能靠猜——是没检索到、检索到了没注入、还是注入了模型没用。

三条硬约束：
1. 失败绝不影响回复 —— 所有异常吞掉，只留一行 warning。
2. 不写空结果 —— 没召回到东西看 metrics 就够，不必刷屏。
3. 滚动保留 —— 这是排查窗口，不是审计档案，默认留最近 500 条。
"""

from __future__ import annotations

import json
import logging

from db_init import iso_now

log = logging.getLogger(__name__)

KEEP = 500            # 滚动保留条数
BLOCK_MAX = 8000      # 注入块最多存多少字符（够看出实际长相）
ITEM_CONTENT_MAX = 300  # 单条记忆正文截断长度

_prune_tick = 0
_PRUNE_EVERY = 25     # 每 25 次写入清一次旧记录，避免每次召回都跑 DELETE


def record(
    settings,
    *,
    account: str,
    scope_type: str,
    scope_key: str,
    query: str,
    result,
    keep: int = KEEP,
) -> None:
    """记一条召回。任何异常都被吞掉——日志坏了不能拖累聊天。"""
    global _prune_tick
    if not getattr(result, "items", None):
        return
    try:
        from store import open_conn

        conn = open_conn(settings)
        try:
            rows = []
            for item in result.items:
                d = item.as_dict()
                d["content"] = (d.get("content") or "")[:ITEM_CONTENT_MAX]
                rows.append(d)
            block = result.block or ""
            conn.execute(
                "INSERT INTO recall_log (account, scope_key, scope_type, query,"
                " created_at, item_count, gate, reason, degraded, latency_ms,"
                " block_chars, block, items)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    account,
                    scope_key,
                    scope_type,
                    (query or "")[:500],
                    iso_now(),
                    len(rows),
                    float(result.gate or 0),
                    result.reason or "",
                    1 if result.degraded else 0,
                    int(result.latency_ms or 0),
                    len(block),
                    block[:BLOCK_MAX],
                    json.dumps(rows, ensure_ascii=False),
                ),
            )
            _prune_tick += 1
            if _prune_tick % _PRUNE_EVERY == 0:
                conn.execute(
                    "DELETE FROM recall_log WHERE id NOT IN "
                    "(SELECT id FROM recall_log ORDER BY id DESC LIMIT ?)",
                    (keep,),
                )
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - 日志失败不能影响主链路
        log.warning("召回日志写入失败（不影响回复）：%s", exc)


def recent(settings, limit: int = 50) -> list[dict]:
    """读最近若干条，供控制台展示。读失败返回空列表，页面不至于白屏。"""
    try:
        from store import open_conn

        conn = open_conn(settings)
        try:
            cur = conn.execute(
                "SELECT * FROM recall_log ORDER BY id DESC LIMIT ?", (int(limit),)
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("读取召回日志失败：%s", exc)
        return []
