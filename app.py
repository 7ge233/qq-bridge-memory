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
import config
import dataclasses
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time
import traceback
from contextlib import asynccontextmanager

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import ValidationError

import console
import db_init
import migrate
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

def _log_startup_failure() -> None:
    """启动失败时把完整堆栈写进 logs/startup-error.log，窗口一闪而过也能查。"""
    try:
        log_dir = Path(__file__).resolve().parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "startup-error.log").open("a", encoding="utf-8") as fh:
            fh.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动失败 ====\n")
            fh.write(traceback.format_exc())
    except Exception:  # noqa: BLE001 - 记日志失败不能再抛
        pass


def _slang_scheduler(settings, stop_event) -> None:
    """每日定时扫描：到点且当天没跑过就扫一次。"""
    import slangscan
    import tuning

    while not stop_event.wait(300):
        try:
            cfg = tuning.slang()
            if not cfg.get("enabled") or cfg.get("scan_mode") != "daily":
                continue
            conn = store.open_conn(settings)
            try:
                if not slangscan.due_now(conn, cfg):
                    continue
                logger.info("到点，开始黑话自动扫描")
                report = slangscan.scan(conn, cfg=cfg)
                logger.info("黑话扫描完成：候选 %s，收词 %s",
                            report.get("candidates"), len(report.get("accepted") or []))
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - 定时任务不能把进程搞挂
            logger.warning("黑话定时扫描失败：%s", exc)


def _check_slang_fingerprint(conn) -> None:
    """黑话词表变了就把 tokens 标记为过期，提示去控制台重算。"""
    import slang

    current = slang.fingerprint()
    stored = migrate.meta_get(conn, "tokens.slang_fp")
    if not stored:
        migrate.meta_set(conn, "tokens.slang_fp", current)
        conn.commit()
        return
    if stored != current:
        migrate.meta_set(conn, "tokens.stale", "1")
        migrate.meta_set(conn, "tokens.slang_fp", current)
        conn.commit()
        logger.warning(
            "黑话词表已变化（%s → %s）：已有记忆的分词可能过期，建议在控制台点「重算分词」",
            stored, current,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        settings = get_settings()
        settings.ensure_dirs()
        logging_setup.setup_logging(settings)
    except Exception:
        _log_startup_failure()
        raise
    boot_error = ""
    try:
        db_init.init_db(settings.db_path)
    except Exception as exc:  # noqa: BLE001 - 选中库坏了也要能进控制台改回来
        fallback = Path(config.DEFAULT_DB_PATH)
        boot_error = f"选中的库打不开（{settings.db_path}）：{exc}"
        logger.error("%s；尝试回退到默认库 %s", boot_error, fallback)
        if fallback != Path(settings.db_path):
            try:
                db_init.init_db(fallback)
                settings = dataclasses.replace(settings, db_path=fallback)
                boot_error += f"；已临时回退到 {fallback}，请在控制台改回或修好原库"
            except Exception as exc2:  # noqa: BLE001
                logger.error("默认库也打不开：%s", exc2)
                raise exc from exc2
        else:
            raise

    # 词典档位：把档案里的档位翻译成 QQ_MEMORY_JIEBA_DICT，必须在 jieba 初始化之前。
    # 若上一次迁移已完成但还没重启，这里顺手把 tokens_next 换成 tokens。
    try:
        active_tier = migrate.apply_tier_env()
        boot_conn = store.open_conn(settings)
        try:
            swap_info = migrate.on_startup(boot_conn, active_tier=active_tier)
            _check_slang_fingerprint(boot_conn)
            fixed = store.resync_state(boot_conn)
            if fixed:
                boot_conn.commit()
                logger.info("状态字段对齐：%d 条（is_active/deleted_at → state）", fixed)
        finally:
            boot_conn.close()
        if swap_info.get("swapped"):
            logger.info("词典档位切换完成：%s → %s（%.2fs）", swap_info.get("from_tier"),
                        swap_info.get("to_tier"), swap_info.get("seconds") or 0)
        elif swap_info.get("needs_restart"):
            logger.warning("词典档位迁移已完成但未切换：%s", swap_info.get("reason") or "等待重启")
    except Exception as exc:  # noqa: BLE001 - 迁移问题绝不能挡住启动
        logger.warning("词典档位初始化失败，按默认词典启动：%s", exc)

    embedder = Embedder(settings)
    retriever = Retriever(settings, embedder)
    writer = MemoryWriter(settings, embedder, autostart=True)
    builder = HandoffBuilder(settings)

    retriever.warmup()  # jieba + DF 快照，毫秒级
    threading.Thread(target=embedder.warmup, name="embedder-warmup", daemon=True).start()

    app.state.settings = settings
    app.state.boot_error = boot_error
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
    stop_event = threading.Event()
    threading.Thread(target=_slang_scheduler, args=(settings, stop_event),
                     name="slang-scheduler", daemon=True).start()

    try:
        yield
    finally:
        stop_event.set()
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
