# -*- coding: utf-8 -*-
"""FastAPI sidecar：/v1/*（供 qq-bridge）+ /console（人用）+ /healthz。

启动：
    .venv\\Scripts\\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8787 --workers 1

边界（1.1）：只做「拼记忆文本」和「把消息写进库」，不改会话结构、不碰发送链路。
降级（2.5）：/v1/recall 永远 200（业务失败用 ok/items/meta 表达）；模型/DB 异常一律空结果。
"""

from __future__ import annotations

import hmac
import hashlib
import logging
import os
import sqlite3
import threading
import time
from contextlib import asynccontextmanager

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import ValidationError

import console
import db_init
import logging_setup
import store
from config import APP_VERSION, Settings, get_settings
from embedder import Embedder
from handoff import HandoffBuilder
from metrics import metrics
from retrieval import Retriever
from schemas import RecallRequest, WriteRequest
from writer import MemoryWriter

logger = logging.getLogger("qq_memory.app")


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------

def require_memory_token(request: Request) -> None:
    expected = os.environ.get("QQ_MEMORY_TOKEN", "").strip()
    if expected:
        supplied = request.headers.get("x-memory-token", "")
        if not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="invalid memory token")
        return
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="token not configured; loopback only")


def _s(value: object) -> str:
    return "" if value is None else str(value)


def _account_fp(account: object) -> str:
    """日志里只记 account 指纹，不记原始 QQ 号。"""
    raw = _s(account)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8] if raw else ""


def _empty_recall(reason: str, gate: float = 0.0) -> dict:
    return {
        "ok": True,
        "items": [],
        "block": "",
        "meta": {
            "gate": gate,
            "reason": reason,
            "candidates": 0,
            "latency_ms": 0,
            "degraded": False,
        },
    }


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    settings.ensure_dirs()
    logging_setup.setup_logging(settings)
    db_init.init_db(settings.db_path)

    embedder = Embedder(settings)
    retriever = Retriever(settings, embedder)
    writer = MemoryWriter(settings, embedder, autostart=True)
    builder = HandoffBuilder(settings)

    retriever.warmup()  # jieba + DF 快照，毫秒级
    threading.Thread(target=embedder.warmup, name="embedder-warmup", daemon=True).start()

    app.state.settings = settings
    app.state.embedder = embedder
    app.state.retriever = retriever
    app.state.writer = writer
    app.state.handoff = builder
    app.state.console_token = console.load_console_token(settings)
    app.state.started_at = time.time()

    if not os.environ.get("QQ_MEMORY_TOKEN", "").strip():
        logger.warning(
            "QQ_MEMORY_TOKEN 未配置：/v1/* 仅允许 127.0.0.1 且不做 token 校验（上线前必须配置）"
        )
    logger.info(
        "qq-bridge-memory %s 启动：db=%s model=%s", APP_VERSION, settings.db_path,
        settings.model_name,
    )
    try:
        yield
    finally:
        writer.stop()
        logger.info("qq-bridge-memory 已停止")


app = FastAPI(title="qq-bridge-memory", version=APP_VERSION, lifespan=lifespan)


@app.middleware("http")
async def console_session_guard(request: Request, call_next):
    """/console 除登录页外都要求有效 session；未登录 GET 跳登录、POST 返回 401。"""
    path = request.url.path
    if path.startswith("/console") and path != "/console/login":
        token = getattr(request.app.state, "console_token", "")
        if not console.verify_session(
            token, request.cookies.get(console.SESSION_COOKIE)
        ):
            if request.method in ("GET", "HEAD"):
                return RedirectResponse("/console/login", status_code=303)
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    return await call_next(request)


@app.middleware("http")
async def access_log(request: Request, call_next):
    """7.2：端点 / 状态 / 耗时；不含任何消息原文。"""
    started = time.perf_counter()
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/v1") or path.startswith("/console"):
        logging_setup.log_event(
            "http",
            endpoint=path,
            method=request.method,
            status=response.status_code,
            latency_ms=logging_setup.elapsed_ms(started),
            client=request.client.host if request.client else "",
        )
    return response


api = APIRouter(prefix="/v1", dependencies=[Depends(require_memory_token)])


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse("/console", status_code=303)


@app.get("/healthz")
def healthz(request: Request) -> dict:
    settings: Settings = request.app.state.settings
    count = 0
    db_ok = True
    try:
        conn = store.open_conn(settings)
        try:
            count = int(conn.execute("SELECT count(*) FROM memories").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error:
        db_ok = False
    model = request.app.state.embedder.health()
    return {
        "ok": db_ok,
        "db": "ok" if db_ok else "error",
        "model": settings.model_name,
        "dim": request.app.state.embedder.dim,
        "count": count,
        "degraded": request.app.state.embedder.degraded,
        "version": APP_VERSION,
        "model_state": model["state"],
        "uptime_s": int(time.time() - request.app.state.started_at),
    }


# ---------------------------------------------------------------------------
# /v1/recall
# ---------------------------------------------------------------------------

@api.post("/recall")
def v1_recall(request: Request, payload: dict | None = Body(default=None)) -> dict:
    if not isinstance(payload, dict):
        return _empty_recall("invalid_request")
    try:
        req = RecallRequest.model_validate(payload)
        data = {
            "account": _s(req.account),
            "scope": {
                "type": req.scope.type,
                "user_id": _s(req.scope.user_id),
                "group_id": _s(req.scope.group_id),
            },
            "query": req.query,
            "context": req.context,
            "limit": req.limit,
            "max_chars": req.max_chars,
            "exclude_ids": list(req.exclude_ids),
            "now": req.now,
            "include_past": req.include_past,
        }
    except ValidationError:
        return _empty_recall("invalid_request")
    try:
        result = request.app.state.retriever.recall(data)
    except ValueError:
        return _empty_recall("invalid_request")
    logging_setup.log_event(
        "recall",
        account_fp=_account_fp(data["account"]),
        scope=data["scope"]["type"],
        gate=round(result.gate, 3),
        items=len(result.items),
        degraded=result.degraded,
        latency_ms=result.latency_ms,
        reason=result.reason,
        expired_skipped=result.expired_skipped,
    )
    return result.as_dict()


# ---------------------------------------------------------------------------
# /v1/write
# ---------------------------------------------------------------------------

@api.post("/write")
def v1_write(request: Request, payload: dict | None = Body(default=None)) -> dict:
    if not isinstance(payload, dict):
        return {"ok": False, "id": None, "deduped": False, "error": "invalid_request"}
    try:
        req = WriteRequest.model_validate(payload)
        data = req.model_dump()
        data["account"] = _s(req.account)
        data["scope"] = {
            "type": req.scope.type,
            "user_id": _s(req.scope.user_id),
            "group_id": _s(req.scope.group_id),
        }
        data["speaker"] = req.speaker.model_dump() if req.speaker else None
        if req.message_id is not None:
            data["message_id"] = _s(req.message_id)
    except ValidationError:
        return {"ok": False, "id": None, "deduped": False, "error": "invalid_request"}

    settings: Settings = request.app.state.settings
    writer: MemoryWriter = request.app.state.writer
    started = time.perf_counter()
    try:
        result = writer.write(data) if settings.write_sync else writer.submit(data)
    except Exception as exc:  # 写入绝不影响主链路
        logger.warning("write 未预期错误：%s", type(exc).__name__)
        return {"ok": False, "id": None, "deduped": False, "error": "internal_error"}
    finally:
        metrics.observe("write", (time.perf_counter() - started) * 1000.0)
    logging_setup.log_event(
        "write",
        account_fp=_account_fp(data["account"]),
        scope=data["scope"]["type"],
        ok=bool(result.get("ok")),
        deduped=bool(result.get("deduped")),
        recorded=bool(result.get("recorded")),
        queued=bool(result.get("queued")),
        latency_ms=logging_setup.elapsed_ms(started),
    )
    return result


# ---------------------------------------------------------------------------
# /v1/handoff
# ---------------------------------------------------------------------------

@api.get("/handoff")
def v1_handoff(
    request: Request, account: str = "", scope: str = "", limit: int = 20
) -> dict:
    scope_type, _, sid = scope.partition(":")
    if not account or scope_type not in ("private", "group") or not sid:
        return {"ok": True, "packet": None, "chars": 0, "degraded": False,
                "reason": "invalid_request", "block": ""}
    data = {
        "account": account,
        "scope": {
            "type": scope_type,
            "user_id": sid if scope_type == "private" else "",
            "group_id": sid if scope_type == "group" else "",
        },
        "limit": limit,
    }
    try:
        result = request.app.state.handoff.build(data)
    except ValueError:
        return {"ok": True, "packet": None, "chars": 0, "degraded": False,
                "reason": "invalid_request", "block": ""}
    except Exception as exc:
        logger.warning("handoff 未预期错误：%s", type(exc).__name__)
        return {"ok": True, "packet": None, "chars": 0, "degraded": True,
                "reason": "internal_error", "block": ""}
    logging_setup.log_event(
        "handoff",
        account_fp=_account_fp(account),
        scope=scope_type,
        has_packet=result.packet is not None,
        chars=result.chars,
        degraded=result.degraded,
        reason=result.reason,
    )
    return result.as_dict()


# ---------------------------------------------------------------------------
# /v1/stats
# ---------------------------------------------------------------------------

@api.get("/stats")
def v1_stats(request: Request) -> dict:
    settings: Settings = request.app.state.settings
    try:
        conn = store.open_conn(settings)
        try:
            count = int(conn.execute("SELECT count(*) FROM memories").fetchone()[0])
            turns = int(conn.execute("SELECT count(*) FROM conversation_turns").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error:
        count, turns = 0, 0

    snap = metrics.snapshot()
    write_stats = request.app.state.writer.stats()
    recall_keys = (
        "recall_total", "recall_hit", "recall_gate_skip", "recall_empty",
        "inject_chars_last", "inject_items_last", "recall_latency_ms_p50",
        "recall_latency_ms_p95", "degraded_count",
    )
    payload: dict = {
        "ok": True,
        "version": APP_VERSION,
        "uptime_s": int(time.time() - request.app.state.started_at),
        "count": count,
        "turns": turns,
        "model": request.app.state.embedder.health(),
        "recall": {key: snap.get(key, 0) for key in recall_keys},
        "write": dict(write_stats),
        "write_latency_ms_p50": snap.get("write_latency_ms_p50", 0),
        "write_latency_ms_p95": snap.get("write_latency_ms_p95", 0),
    }
    payload.update({key: snap.get(key, 0) for key in recall_keys})
    payload.update(write_stats)
    return payload


app.include_router(api)
app.include_router(console.router)
