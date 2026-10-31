# -*- coding: utf-8 -*-
"""管理控制台（第五章）：/console 同进程同端口（8787），仅 127.0.0.1。

安全（5.1）：
  * 首次启动生成随机 token → data/console-token；也可用 QQ_MEMORY_CONSOLE_TOKEN 覆盖；
  * token 换 HttpOnly + SameSite=Strict 的 session cookie（HMAC 签名，12h）；
  * 写操作（停用/软删/撤销取代/导入）额外校验 CSRF；
  * 登录连续失败 5 次按 peer 锁 10 分钟；
  * 所有写动作追加 logs/console-audit.jsonl（不记录内容原文）。
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

import profiles
import store
import textproc
from audit_event_plans import run_audit
from backfill_temporal import run_backfill
from config import Settings
from retrieval import freshness_score, fts_match_expr
from timeutil import iso_now

logger = logging.getLogger("qq_memory.console")

router = APIRouter()

SESSION_COOKIE = "qqm_session"
SESSION_TTL = 12 * 3600
LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SECONDS = 600

_fail_lock = threading.Lock()
_failures: dict[str, tuple[int, float]] = {}


# ---------------------------------------------------------------------------
# token / session / csrf
# ---------------------------------------------------------------------------

def token_path(settings: Settings) -> Path:
    return settings.db_path.parent / "console-token"


def load_console_token(settings: Settings) -> str:
    env = os.environ.get("QQ_MEMORY_CONSOLE_TOKEN", "").strip()
    if env:
        return env
    path = token_path(settings)
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return token


def _secret(token: str) -> bytes:
    return hashlib.sha256(f"qqm|{token}".encode("utf-8")).digest()


def make_session(token: str, ttl: int = SESSION_TTL) -> tuple[str, int]:
    exp = int(time.time()) + ttl
    sig = hmac.new(_secret(token), f"session:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{exp}.{sig}", exp


def verify_session(token: str, raw: str | None) -> int | None:
    if not raw or "." not in raw:
        return None
    exp_raw, sig = raw.split(".", 1)
    try:
        exp = int(exp_raw)
    except ValueError:
        return None
    expected = hmac.new(
        _secret(token), f"session:{exp}".encode(), hashlib.sha256
    ).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        return None
    if exp < int(time.time()):
        return None
    return exp


def csrf_for(token: str, exp: int) -> str:
    return hmac.new(_secret(token), f"csrf:{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def _session_exp(request: Request) -> int:
    return verify_session(request.app.state.console_token, request.cookies.get(SESSION_COOKIE)) or 0


def _ensure_csrf(request: Request, supplied: str) -> int:
    exp = _session_exp(request)
    if not exp:
        raise HTTPException(status_code=401, detail="session required")
    if not supplied or not hmac.compare_digest(supplied, csrf_for(request.app.state.console_token, exp)):
        raise HTTPException(status_code=403, detail="csrf token invalid")
    return exp


def _peer(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _locked(peer: str) -> bool:
    with _fail_lock:
        entry = _failures.get(peer)
        if not entry:
            return False
        fails, until = entry
        return fails >= LOGIN_MAX_FAILS and time.time() < until


def _record_failure(peer: str) -> None:
    with _fail_lock:
        fails, until = _failures.get(peer, (0, 0.0))
        fails += 1
        if fails >= LOGIN_MAX_FAILS:
            until = time.time() + LOGIN_LOCK_SECONDS
        _failures[peer] = (fails, until)


def _reset_failures(peer: str) -> None:
    with _fail_lock:
        _failures.pop(peer, None)


def _token_fp(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:8]


def audit(settings: Settings, action: str, **fields: object) -> None:
    record = {"ts": iso_now(), "action": action, **fields}
    try:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        with (settings.log_dir / "console-audit.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        logger.warning("审计日志写入失败：%s", action)


# ---------------------------------------------------------------------------
# HTML 外壳
# ---------------------------------------------------------------------------

CSS = """
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
background:#0f172a;color:#e2e8f0;font-size:14px;line-height:1.5}
header{background:#1e293b;border-bottom:1px solid #334155;padding:12px 22px;
display:flex;gap:18px;align-items:center;flex-wrap:wrap}
header .brand{font-weight:700;letter-spacing:.5px}
header .spacer{flex:1}
a{color:#93c5fd;text-decoration:none}a:hover{text-decoration:underline}
main{max-width:1240px;margin:0 auto;padding:20px}
.card{background:#1e293b;border:1px solid #273449;border-radius:10px;padding:16px;margin-bottom:16px}
.card h2{margin:0 0 12px;font-size:15px;color:#cbd5e1;font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.k{color:#94a3b8;font-size:12px}.v{font-size:22px;font-weight:700;margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{border-bottom:1px solid #2b3a52;padding:7px 8px;text-align:left;vertical-align:top}
th{color:#94a3b8;font-weight:600;white-space:nowrap}
tr:hover td{background:#17233a}
input,select,textarea,button{background:#0f172a;color:#e2e8f0;border:1px solid #334155;
border-radius:6px;padding:6px 8px;font:inherit}
button{cursor:pointer;background:#1d4ed8;border-color:#1d4ed8}
button.danger{background:#b91c1c;border-color:#b91c1c}
button.ghost{background:transparent}
form.inline{display:inline-flex;gap:6px;align-items:center;margin:2px 0}
.muted{color:#94a3b8}.bad{color:#fca5a5}.ok{color:#86efac}.warn{color:#fcd34d}
.tag{display:inline-block;padding:1px 7px;border-radius:999px;background:#17233a;
border:1px solid #334155;font-size:12px;color:#cbd5e1}
.pager{display:flex;gap:10px;align-items:center;margin-top:10px}
pre{white-space:pre-wrap;word-break:break-all;margin:0;font-size:12px;color:#cbd5e1}
.login{max-width:420px;margin:12vh auto}
"""


def _shell(title: str, body: str, *, nav: bool = True, status_code: int = 200) -> HTMLResponse:
    nav_html = (
        '<a href="/console">总览</a>'
        '<a href="/console/memories">记忆</a>'
        '<a href="/console/maintenance">维护</a>'
        '<a href="/console/db">数据库</a>'
        '<a href="/console/export.jsonl">导出 JSONL</a>'
        '<span class="spacer"></span>'
        '<a href="/console/logout">退出</a>'
        if nav
        else ""
    )
    return HTMLResponse(
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)} · qq_memory</title><style>{CSS}</style></head><body>"
        f'<header><span class="brand">qq-bridge-memory</span>{nav_html}</header>'
        f"<main>{body}</main></body></html>",
        status_code=status_code,
    )


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------

@router.get("/console/login")
def login_page(request: Request) -> HTMLResponse:
    if _session_exp(request):
        return RedirectResponse("/console", status_code=303)
    body = """
<div class="card login">
  <h2>控制台登录</h2>
  <p class="muted">令牌在本机 data/console-token（或环境变量 QQ_MEMORY_CONSOLE_TOKEN）。</p>
  <form method="post" action="/console/login">
    <input type="password" name="token" placeholder="console token" autofocus
           style="width:100%;margin:8px 0 12px">
    <button type="submit" style="width:100%">登录</button>
  </form>
</div>"""
    return _shell("登录", body, nav=False)


@router.post("/console/login")
def login(request: Request, token: str = Form("")) -> Response:
    peer = _peer(request)
    if _locked(peer):
        return _shell(
            "登录",
            '<div class="card login"><p class="bad">失败次数过多，已锁定 10 分钟。</p></div>',
            nav=False,
            status_code=429,
        )
    expected = request.app.state.console_token
    if not hmac.compare_digest(token.strip(), expected):
        _record_failure(peer)
        return _shell(
            "登录",
            '<div class="card login"><p class="bad">令牌不正确。</p>'
            '<p><a href="/console/login">重试</a></p></div>',
            nav=False,
            status_code=401,
        )
    _reset_failures(peer)
    cookie, exp = make_session(expected)
    response = RedirectResponse("/console", status_code=303)
    response.set_cookie(
        SESSION_COOKIE, cookie, max_age=SESSION_TTL, httponly=True,
        samesite="strict", path="/",
    )
    audit(request.app.state.settings, "login", peer=peer)
    return response


@router.get("/console/logout")
def logout(request: Request) -> RedirectResponse:
    response = RedirectResponse("/console/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    audit(request.app.state.settings, "logout", peer=_peer(request))
    return response


# ---------------------------------------------------------------------------
# 总览
# ---------------------------------------------------------------------------

@router.get("/console")
def dashboard(request: Request) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    conn = store.open_conn(settings)
    try:
        total = conn.execute("SELECT count(*) FROM memories").fetchone()[0]
        active = conn.execute(
            "SELECT count(*) FROM memories WHERE is_active=1 AND deleted_at IS NULL"
        ).fetchone()[0]
        deleted = conn.execute(
            "SELECT count(*) FROM memories WHERE deleted_at IS NOT NULL"
        ).fetchone()[0]
        turns = conn.execute("SELECT count(*) FROM conversation_turns").fetchone()[0]
        today = iso_now()[:10]
        today_mem = conn.execute(
            "SELECT count(*) FROM memories WHERE created_at >= ?", (today,)
        ).fetchone()[0]
        by_vis = conn.execute(
            "SELECT visibility, count(*) n FROM memories GROUP BY visibility ORDER BY n DESC"
        ).fetchall()
        by_account = conn.execute(
            "SELECT account, count(*) n FROM memories GROUP BY account ORDER BY n DESC LIMIT 5"
        ).fetchall()
        by_scope = conn.execute(
            "SELECT scope_type, count(*) n FROM memories GROUP BY scope_type ORDER BY n DESC"
        ).fetchall()
        recent = conn.execute(
            "SELECT id, content, visibility, scope_type, group_id, created_at "
            "FROM memories ORDER BY id DESC LIMIT 5"
        ).fetchall()
    finally:
        conn.close()

    from metrics import metrics

    snap = metrics.snapshot()
    wstats = request.app.state.writer.stats()
    model = request.app.state.embedder.health()
    db_size = settings.db_path.stat().st_size if settings.db_path.exists() else 0

    cards = [
        ("记忆总数", total),
        ("生效中", active),
        ("软删", deleted),
        ("流水条数", turns),
        ("今日写入", today_mem),
        ("模型状态", model["state"] + (" / 降级" if model["degraded"] else "")),
        ("recall p50", f"{snap.get('recall_latency_ms_p50', 0)} ms"),
        ("recall p95", f"{snap.get('recall_latency_ms_p95', 0)} ms"),
    ]
    cards_html = "".join(
        f'<div class="card"><div class="k">{html.escape(str(k))}</div>'
        f'<div class="v">{html.escape(str(v))}</div></div>'
        for k, v in cards
    )

    def _dist(rows, col):
        return " ".join(
            f'<span class="tag">{html.escape(str(r[col]))} · {r["n"]}</span>' for r in rows
        ) or '<span class="muted">无</span>'

    recent_html = "".join(
        f'<tr><td>{r["id"]}</td><td>{html.escape(r["created_at"] or "")[:19]}</td>'
        f'<td>{html.escape(r["scope_type"] + ("/" + (r["group_id"] or "") if r["scope_type"] == "group" else ""))}</td>'
        f'<td>{html.escape(r["visibility"] or "")}</td>'
        f'<td>{html.escape((r["content"] or "")[:80])}</td></tr>'
        for r in recent
    )

    body = f"""
<div class="grid">{cards_html}</div>
<div class="card"><h2>分布</h2>
  <p><span class="muted">visibility：</span>{_dist(by_vis, "visibility")}</p>
  <p><span class="muted">scope：</span>{_dist(by_scope, "scope_type")}</p>
  <p><span class="muted">account：</span>{_dist(by_account, "account")}</p>
  <p class="muted">数据库 {html.escape(str(settings.db_path))} · {db_size:,} 字节</p>
</div>
<div class="card"><h2>召回 / 写入</h2>
  <p>recall_total={snap.get('recall_total',0)} · hit={snap.get('recall_hit',0)}
     · gate_skip={snap.get('recall_gate_skip',0)} · empty={snap.get('recall_empty',0)}
     · 最近注入 {snap.get('inject_items_last',0)} 条 / {snap.get('inject_chars_last',0)} 字</p>
  <p>write_total={wstats.get('write_total',0)} · recorded={wstats.get('write_recorded',0)}
     · dedup={wstats.get('write_dedup',0)} · dropped={wstats.get('write_dropped',0)}
     · fail={wstats.get('write_fail',0)}</p>
  <p class="muted">model: {html.escape(model['model'])} dim={model['dim']} state={model['state']}
     {html.escape(model['reason'] or '')}</p>
</div>
<div class="card"><h2>最近写入</h2>
  <table><thead><tr><th>ID</th><th>时间</th><th>scope</th><th>visibility</th><th>内容</th></tr></thead>
  <tbody>{recent_html or '<tr><td colspan="5" class="muted">暂无</td></tr>'}</tbody></table>
  <p><a href="/console/memories">查看全部记忆 →</a></p>
</div>
<div class="card"><h2>批量导入（JSON 数组）</h2>
  <form method="post" action="/console/actions/import">
    <input type="hidden" name="csrf" value="{csrf_for(request.app.state.console_token, _session_exp(request))}">
    <textarea name="payload" rows="6" style="width:100%" placeholder='[{{"account":"1234567890","scope":{{"type":"private","user_id":"10001"}},"content":"..."}}]'></textarea>
    <p><button type="submit">导入（source=import）</button></p>
  </form>
</div>"""
    return _shell("总览", body)


# ---------------------------------------------------------------------------
# 记忆列表
# ---------------------------------------------------------------------------

def _fetch_memories(
    conn: sqlite3.Connection,
    *,
    q: str,
    visibility: str,
    is_active: str,
    account: str,
    scope_type: str,
    event_state: str = "",
    page: int,
    page_size: int = 20,
) -> tuple[list[sqlite3.Row], int]:
    where: list[str] = []
    params: list[object] = []
    if q:
        tokens = textproc.tokenize(q)
        if tokens:
            where.append(
                "id IN (SELECT rowid FROM memories_fts WHERE memories_fts MATCH ? LIMIT 1000)"
            )
            params.append(fts_match_expr(tokens))
        else:
            where.append("content LIKE ?")
            params.append(f"%{q}%")
    if visibility in ("private", "group", "public"):
        where.append("visibility=?")
        params.append(visibility)
    if is_active in ("1", "0"):
        where.append("is_active=?")
        params.append(int(is_active))
    if account:
        where.append("account=?")
        params.append(account)
    if scope_type in ("private", "group"):
        where.append("scope_type=?")
        params.append(scope_type)
    now_iso = iso_now()
    if event_state == "planned":
        where.append("event_status='planned' AND (valid_until IS NULL OR valid_until >= ?)")
        params.append(now_iso)
    elif event_state == "expired":
        where.append("event_status='planned' AND valid_until IS NOT NULL AND valid_until < ?")
        params.append(now_iso)
    elif event_state in ("cancelled", "done"):
        where.append("event_status=?")
        params.append(event_state)
    elif event_state == "none":
        where.append("event_at IS NULL AND event_status IS NULL")
    where_sql = " AND ".join(where) or "1=1"
    total = int(
        conn.execute(
            f"SELECT count(*) FROM memories WHERE {where_sql}", params
        ).fetchone()[0]
    )
    rows = conn.execute(
        "SELECT id, account, scope_type, user_id, group_id, speaker_name, role, content,"
        " visibility, fact_key, importance, created_at, happened_at, last_recalled_at,"
        " recall_count, is_active, deleted_at, superseded_by, category,"
        " event_at, valid_until, event_status "
        f"FROM memories WHERE {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
        [*params, page_size, (page - 1) * page_size],
    ).fetchall()
    return list(rows), total


def _href(path: str, **params: object) -> str:
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    return path + ("?" + urlencode(clean) if clean else "")


@router.get("/console/memories")
def memories_page(request: Request) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    qp = request.query_params
    q = (qp.get("q") or "").strip()
    visibility = (qp.get("visibility") or "").strip()
    is_active = (qp.get("is_active") or "all").strip()
    account = (qp.get("account") or "").strip()
    scope_type = (qp.get("scope_type") or "").strip()
    event_state = (qp.get("event_state") or "").strip()
    msg = (qp.get("msg") or "").strip()
    try:
        page = max(1, int(qp.get("page") or 1))
    except ValueError:
        page = 1
    page_size = 20

    conn = store.open_conn(settings)
    try:
        rows, total = _fetch_memories(
            conn, q=q, visibility=visibility, is_active=is_active,
            account=account, scope_type=scope_type, event_state=event_state,
            page=page, page_size=page_size,
        )
    finally:
        conn.close()

    csrf = csrf_for(request.app.state.console_token, _session_exp(request))
    options = "".join(
        f'<option value="{v}"{" selected" if visibility == v else ""}>{label}</option>'
        for v, label in (("", "全部"), ("private", "private"), ("group", "group"), ("public", "public"))
    )
    active_options = "".join(
        f'<option value="{v}"{" selected" if is_active == v else ""}>{label}</option>'
        for v, label in (("all", "全部状态"), ("1", "生效中"), ("0", "已失效"))
    )
    scope_options = "".join(
        f'<option value="{v}"{" selected" if scope_type == v else ""}>{label}</option>'
        for v, label in (("", "全部 scope"), ("private", "私聊"), ("group", "群聊"))
    )
    event_options = "".join(
        f'<option value="{v}"{" selected" if event_state == v else ""}>{label}</option>'
        for v, label in (
            ("", "全部事件"), ("planned", "计划中"), ("expired", "已过期"),
            ("cancelled", "已取消"), ("done", "已完成"), ("none", "无事件"),
        )
    )

    def _status(row: sqlite3.Row) -> str:
        if row["deleted_at"]:
            return '<span class="bad">已软删</span>'
        if not row["is_active"]:
            return '<span class="warn">已失效</span>'
        return '<span class="ok">生效</span>'

    trs: list[str] = []
    for row in rows:
        fresh = freshness_score(
            row["happened_at"], row["created_at"], row["fact_key"]
        )
        scope = "私聊" if row["scope_type"] == "private" else f"群{row['group_id'] or ''}"
        actions = ""
        event_status = (row["event_status"] or "").strip()
        if row["event_at"] or event_status:
            if event_status != "cancelled":
                actions += (
                    f'<form class="inline" method="post" action="/console/actions/event-status">'
                    f'<input type="hidden" name="csrf" value="{csrf}">'
                    f'<input type="hidden" name="memory_id" value="{row["id"]}">'
                    f'<input type="hidden" name="status" value="cancelled">'
                    f'<button type="submit" class="ghost">取消</button></form>'
                )
            if event_status != "done":
                actions += (
                    f'<form class="inline" method="post" action="/console/actions/event-status">'
                    f'<input type="hidden" name="csrf" value="{csrf}">'
                    f'<input type="hidden" name="memory_id" value="{row["id"]}">'
                    f'<input type="hidden" name="status" value="done">'
                    f'<button type="submit" class="ghost">完成</button></form>'
                )
            if event_status in ("cancelled", "done"):
                actions += (
                    f'<form class="inline" method="post" action="/console/actions/event-status">'
                    f'<input type="hidden" name="csrf" value="{csrf}">'
                    f'<input type="hidden" name="memory_id" value="{row["id"]}">'
                    f'<input type="hidden" name="status" value="planned">'
                    f'<button type="submit" class="ghost">恢复</button></form>'
                )
        if row["is_active"]:
            actions += (
                f'<form class="inline" method="post" action="/console/actions/deactivate">'
                f'<input type="hidden" name="csrf" value="{csrf}">'
                f'<input type="hidden" name="memory_id" value="{row["id"]}">'
                f'<input name="reason" placeholder="原因" size="8">'
                f'<button type="submit" class="ghost">停用</button></form>'
            )
        if not row["deleted_at"]:
            actions += (
                f'<form class="inline" method="post" action="/console/actions/soft-delete">'
                f'<input type="hidden" name="csrf" value="{csrf}">'
                f'<input type="hidden" name="memory_id" value="{row["id"]}">'
                f'<input name="reason" placeholder="原因" size="8">'
                f'<button type="submit" class="danger">软删</button></form>'
            )
        if row["superseded_by"]:
            actions += (
                f'<form class="inline" method="post" action="/console/actions/undo-supersede">'
                f'<input type="hidden" name="csrf" value="{csrf}">'
                f'<input type="hidden" name="memory_id" value="{row["id"]}">'
                f'<button type="submit" class="ghost">撤销取代</button></form>'
            )
        event_label = {
            "planned": "计划中", "done": "已完成", "cancelled": "已取消",
        }.get(event_status, event_status)
        if row["event_at"]:
            event_cell = (
                f'{html.escape(str(row["event_at"])[:16])}<br>'
                f'<span class="tag">{html.escape(event_label)}</span>'
            )
        else:
            event_cell = '<span class="muted">—</span>'
        trs.append(
            f'<tr><td><input type="checkbox" name="memory_ids" form="bulk-form" value="{row["id"]}"></td>'
            f'<td>{row["id"]}</td>'
            f'<td>{html.escape((row["created_at"] or "")[:19])}</td>'
            f'<td>{html.escape(row["account"] or "")}</td>'
            f'<td>{html.escape(scope)}</td>'
            f'<td>{html.escape(row["speaker_name"] or ("我" if row["role"] == "assistant" else ""))}</td>'
            f'<td><span class="tag">{html.escape(row["visibility"] or "")}</span></td>'
            f'<td><details><summary>{html.escape((row["content"] or "")[:60])}</summary>'
            f'<pre>{html.escape(row["content"] or "")}</pre></details></td>'
            f'<td>{html.escape(row["fact_key"] or "")}</td>'
            f'<td>{fresh:.2f}</td>'
            f'<td>{event_cell}</td>'
            f'<td>{_status(row)}</td>'
            f'<td>{actions or "<span class=muted>—</span>"}</td></tr>'
        )

    pages = max(1, (total + page_size - 1) // page_size)
    base = dict(q=q, visibility=visibility, is_active=is_active, account=account,
                scope_type=scope_type, event_state=event_state)
    return_to = _href("/console/memories", **base)
    message_html = (
        f'<div class="card"><p class="ok">{html.escape(msg)}</p></div>' if msg else ""
    )
    pager = (
        f'<div class="pager"><span class="muted">共 {total} 条 · 第 {page}/{pages} 页</span>'
        + (f'<a href="{_href("/console/memories", **base, page=page - 1)}">上一页</a>' if page > 1 else "")
        + (f'<a href="{_href("/console/memories", **base, page=page + 1)}">下一页</a>' if page < pages else "")
        + "</div>"
    )

    body = f"""
<div class="card"><h2>筛选</h2>
<form method="get" action="/console/memories" class="inline" style="flex-wrap:wrap">
  <input name="q" value="{html.escape(q)}" placeholder="关键词（FTS）" size="18">
  <select name="visibility">{options}</select>
  <select name="is_active">{active_options}</select>
  <select name="scope_type">{scope_options}</select>
  <select name="event_state">{event_options}</select>
  <input name="account" value="{html.escape(account)}" placeholder="account" size="12">
  <button type="submit">筛选</button>
  <a href="/console/memories">重置</a>
</form></div>
{message_html}
<div class="card"><h2>批量操作</h2>
  <p class="muted">勾选下方列表左侧的复选框，然后在这里统一处理。批量写操作需要输入 <b>BULK</b> 确认。</p>
  <form id="bulk-form" method="post" action="/console/actions/bulk">
    <input type="hidden" name="csrf" value="{csrf}">
    <input type="hidden" name="next" value="{html.escape(return_to)}">
    <label><input type="checkbox" onclick="for(const c of document.querySelectorAll('input[name=memory_ids]')) c.checked=this.checked"> 全选本页</label>
    <select name="action">
      <option value="event_status">修改事件状态</option>
      <option value="activate">启用</option>
      <option value="deactivate">停用</option>
      <option value="soft_delete">软删</option>
    </select>
    <select name="status">
      <option value="planned">事件 → 计划中</option>
      <option value="done">事件 → 已完成</option>
      <option value="cancelled">事件 → 已取消</option>
    </select>
    <input name="reason" placeholder="原因（可选）" size="16">
    <input name="confirm" placeholder="输入 BULK" size="10">
    <button type="submit" class="danger">执行批量操作</button>
  </form>
  <p class="muted">事件状态只作用于 category=event 的记录；启用/停用/软删作用于选中的所有记录。</p>
</div>
<div class="card"><h2>记忆列表</h2>
<table><thead><tr><th>选</th><th>ID</th><th>时间</th><th>account</th><th>scope</th><th>说话人</th>
<th>visibility</th><th>内容</th><th>fact_key</th><th>鲜活度</th><th>事件</th><th>状态</th><th>操作</th></tr></thead>
<tbody>{''.join(trs) or '<tr><td colspan="13" class="muted">没有匹配的记忆</td></tr>'}</tbody></table>
{pager}</div>
<div class="card"><h2>导出</h2>
<p><a href="{_href("/console/export.jsonl", q=q, visibility=visibility, is_active=is_active, account=account, scope_type=scope_type, event_state=event_state)}">按当前筛选导出 JSONL</a></p>
</div>"""
    return _shell("记忆", body)


# ---------------------------------------------------------------------------
# 写操作
# ---------------------------------------------------------------------------

def _back_to_list(message: str = "") -> RedirectResponse:
    target = "/console/memories"
    if message:
        target += "?" + urlencode({"msg": message})
    return RedirectResponse(target, status_code=303)


@router.post("/console/actions/deactivate")
def action_deactivate(
    request: Request,
    memory_id: int = Form(...),
    csrf: str = Form(""),
    reason: str = Form(""),
) -> RedirectResponse:
    _ensure_csrf(request, csrf)
    settings: Settings = request.app.state.settings
    conn = store.open_conn(settings)
    try:
        conn.execute("UPDATE memories SET is_active=0 WHERE id=?", (memory_id,))
        conn.commit()
    finally:
        conn.close()
    audit(settings, "deactivate", memory_id=memory_id, reason=reason[:200],
          peer=_peer(request), token_fp=_token_fp(request.app.state.console_token))
    return _back_to_list("deactivated")


@router.post("/console/actions/event-status")
def action_event_status(
    request: Request,
    memory_id: int = Form(...),
    status: str = Form(""),
    csrf: str = Form(""),
) -> RedirectResponse:
    _ensure_csrf(request, csrf)
    if status not in ("planned", "done", "cancelled"):
        raise HTTPException(status_code=400, detail="invalid event status")
    settings: Settings = request.app.state.settings
    conn = store.open_conn(settings)
    try:
        row = conn.execute(
            "SELECT category, event_at, event_status FROM memories WHERE id=?",
            (memory_id,),
        ).fetchone()
        if row is None or (row["event_at"] is None and row["event_status"] is None):
            raise HTTPException(status_code=404, detail="event memory not found")
        conn.execute(
            "UPDATE memories SET event_status=? WHERE id=?", (status, memory_id)
        )
        conn.commit()
    finally:
        conn.close()
    audit(settings, "event_status", memory_id=memory_id, status=status,
          peer=_peer(request), token_fp=_token_fp(request.app.state.console_token))
    return _back_to_list(f"event-{status}")


@router.post("/console/actions/bulk")
def action_bulk(
    request: Request,
    memory_ids: list[int] = Form(default=[]),
    action: str = Form(""),
    status: str = Form(""),
    reason: str = Form(""),
    confirm: str = Form(""),
    next: str = Form("/console/memories"),
    csrf: str = Form(""),
) -> RedirectResponse:
    _ensure_csrf(request, csrf)
    ids = sorted({int(value) for value in memory_ids if int(value) > 0})
    target = next if next.startswith("/console/memories") else "/console/memories"

    def _back(msg: str) -> RedirectResponse:
        sep = "&" if "?" in target else "?"
        return RedirectResponse(
            f"{target}{sep}{urlencode({'msg': msg})}", status_code=303
        )

    if not ids:
        return _back("bulk: 没有选中记录")
    if len(ids) > 200:
        return _back("bulk: 一次最多 200 条")
    if confirm.strip().upper() != "BULK":
        return _back("bulk: 需要在确认框输入 BULK")
    if action not in ("event_status", "activate", "deactivate", "soft_delete"):
        raise HTTPException(status_code=400, detail="invalid bulk action")
    if action == "event_status" and status not in ("planned", "done", "cancelled"):
        raise HTTPException(status_code=400, detail="invalid event status")
    if action in ("activate", "deactivate") and not ids:
        return _back("bulk: 没有选中记录")

    settings: Settings = request.app.state.settings
    placeholders = ",".join("?" for _ in ids)
    count = 0
    conn = store.open_conn(settings)
    try:
        if action == "event_status":
            cur = conn.execute(
                f"UPDATE memories SET event_status=? WHERE id IN ({placeholders}) "
                f"AND category='event' AND (event_at IS NOT NULL OR event_status IS NOT NULL)",
                (status, *ids),
            )
        elif action == "activate":
            cur = conn.execute(
                f"UPDATE memories SET is_active=1 WHERE id IN ({placeholders})", tuple(ids)
            )
        elif action == "deactivate":
            cur = conn.execute(
                f"UPDATE memories SET is_active=0 WHERE id IN ({placeholders})", tuple(ids)
            )
        else:
            cur = conn.execute(
                f"UPDATE memories SET deleted_at=?, is_active=0 "
                f"WHERE id IN ({placeholders}) AND deleted_at IS NULL",
                (iso_now(), *ids),
            )
        count = int(cur.rowcount or 0)
        conn.commit()
    finally:
        conn.close()

    audit(
        settings,
        "bulk_action",
        operation=action,
        status=status or None,
        count=count,
        ids=len(ids),
        reason=reason[:200],
        peer=_peer(request),
        token_fp=_token_fp(request.app.state.console_token),
    )
    return _back(f"bulk: {action} 完成 {count} 条")


@router.post("/console/actions/soft-delete")
def action_soft_delete(
    request: Request,
    memory_id: int = Form(...),
    csrf: str = Form(""),
    reason: str = Form(""),
) -> RedirectResponse:
    _ensure_csrf(request, csrf)
    settings: Settings = request.app.state.settings
    conn = store.open_conn(settings)
    try:
        conn.execute(
            "UPDATE memories SET deleted_at=?, is_active=0 WHERE id=? AND deleted_at IS NULL",
            (iso_now(), memory_id),
        )
        conn.commit()
    finally:
        conn.close()
    audit(settings, "soft_delete", memory_id=memory_id, reason=reason[:200],
          peer=_peer(request), token_fp=_token_fp(request.app.state.console_token))
    return _back_to_list("deleted")


@router.post("/console/actions/undo-supersede")
def action_undo_supersede(
    request: Request, memory_id: int = Form(...), csrf: str = Form("")
) -> RedirectResponse:
    _ensure_csrf(request, csrf)
    settings: Settings = request.app.state.settings
    conn = store.open_conn(settings)
    try:
        restored = store.revoke_supersede(conn, memory_id)
        conn.commit()
    finally:
        conn.close()
    audit(settings, "undo_supersede", memory_id=memory_id, restored=restored,
          peer=_peer(request), token_fp=_token_fp(request.app.state.console_token))
    return _back_to_list("restored")


@router.post("/console/actions/import")
def action_import(
    request: Request, payload: str = Form(""), csrf: str = Form("")
) -> JSONResponse:
    _ensure_csrf(request, csrf)
    settings: Settings = request.app.state.settings
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        return JSONResponse({"ok": False, "error": f"JSON 解析失败：{exc}"}, status_code=400)
    if isinstance(data, dict) and "items" in data:
        data = data["items"]
    if not isinstance(data, list):
        return JSONResponse({"ok": False, "error": "需要 JSON 数组"}, status_code=400)

    writer = request.app.state.writer
    results = []
    ok_count = 0
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            results.append({"index": index, "ok": False, "error": "非对象"})
            continue
        body = dict(item)
        body.setdefault("source", "import")
        body.setdefault("meta", {"trigger": "command"})
        outcome = writer.write(body)
        if outcome.get("ok") and outcome.get("recorded"):
            ok_count += 1
        results.append({"index": index, "outcome": outcome})
    audit(settings, "import", items=len(data), recorded=ok_count,
          peer=_peer(request), token_fp=_token_fp(request.app.state.console_token))
    return JSONResponse({"ok": True, "total": len(data), "recorded": ok_count, "results": results})


# ---------------------------------------------------------------------------
# 维护 / 数据修复：事件时效回填
# ---------------------------------------------------------------------------

def _backfill_page(
    request: Request,
    *,
    report: dict | None = None,
    error: str = "",
    message: str = "",
    audit_report: dict | None = None,
    audit_error: str = "",
) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    conn = store.open_conn(settings)
    try:
        total = conn.execute(
            "SELECT count(*) FROM memories WHERE role='user' AND category='event' "
            "AND deleted_at IS NULL"
        ).fetchone()[0]
        filled = conn.execute(
            "SELECT count(*) FROM memories WHERE role='user' AND category='event' "
            "AND deleted_at IS NULL AND event_at IS NOT NULL"
        ).fetchone()[0]
        pending = conn.execute(
            "SELECT count(*) FROM memories WHERE role='user' AND category='event' "
            "AND deleted_at IS NULL AND event_at IS NULL AND event_status IS NULL"
        ).fetchone()[0]
        cancelled = conn.execute(
            "SELECT count(*) FROM memories WHERE event_status='cancelled'"
        ).fetchone()[0]
        expired = conn.execute(
            "SELECT count(*) FROM memories WHERE event_status='planned' "
            "AND valid_until IS NOT NULL AND valid_until < ?",
            (iso_now(),),
        ).fetchone()[0]
    finally:
        conn.close()

    csrf = csrf_for(request.app.state.console_token, _session_exp(request))
    cards = [
        ("用户事件总数", total),
        ("已有事件时间", filled),
        ("待回填（空）", pending),
        ("已取消", cancelled),
        ("已过期", expired),
    ]
    cards_html = "".join(
        f'<div class="card"><div class="k">{html.escape(str(k))}</div>'
        f'<div class="v">{html.escape(str(v))}</div></div>'
        for k, v in cards
    )

    error_html = f'<div class="card"><p class="bad">{html.escape(error)}</p></div>' if error else ""
    message_html = f'<div class="card"><p class="ok">{html.escape(message)}</p></div>' if message else ""

    report_html = ""
    if report is not None:
        samples = report.get("samples") or []
        rows_html = "".join(
            f"<tr><td>{int(item['id'])}</td>"
            f"<td>{html.escape(str(item.get('event_at') or '')[:19])}</td>"
            f"<td>{html.escape(str(item.get('precision') or ''))}</td>"
            f"<td>{html.escape(str(item.get('preview') or ''))}</td></tr>"
            for item in samples
        ) or '<tr><td colspan="4" class="muted">没有可解析样例</td></tr>'
        mode = "dry-run（未写库）" if report.get("dry_run") else "apply（已写入）"
        backup_html = (
            f'<p class="muted">备份：{html.escape(str(report["backup"]))}</p>'
            if report.get("backup") else ""
        )
        report_html = f"""
<div class="card"><h2>扫描结果</h2>
  <p>模式：<span class="tag">{html.escape(mode)}</span>
     候选 {int(report.get('candidates', 0))} ·
     可解析 {int(report.get('parsed', 0))} ·
     已更新 {int(report.get('updated', 0))} ·
     模糊跳过 {int(report.get('ambiguous', 0))} ·
     缺少基准 {int(report.get('missing_base', 0))}</p>
  {backup_html}
  <table><thead><tr><th>ID</th><th>事件时间</th><th>精度</th><th>内容预览</th></tr></thead>
  <tbody>{rows_html}</tbody></table>
</div>"""

    audit_html = ""
    if audit_error:
        audit_html = f'<div class="card"><h2>事件计划审计</h2><p class="bad">{html.escape(audit_error)}</p></div>'
    elif audit_report is not None:
        findings = audit_report.get("findings") or []
        audit_rows = []
        for finding in findings:
            for item in finding.get("items", []):
                audit_rows.append(
                    "<tr>"
                    f"<td>{html.escape(str(finding.get('kind')))}</td>"
                    f"<td>{html.escape(str(finding.get('scope_key')))}</td>"
                    f"<td>{int(item.get('id', 0))}</td>"
                    f"<td>{html.escape(str(item.get('event_at') or ''))}</td>"
                    f"<td>{html.escape(str(item.get('preview') or ''))}</td>"
                    "</tr>"
                )
        audit_table = (
            "<table><thead><tr><th>类型</th><th>scope</th><th>ID</th><th>事件时间</th><th>内容预览</th></tr></thead>"
            f"<tbody>{''.join(audit_rows) or '<tr><td colspan=5 class=muted>未发现异常</td></tr>'}</tbody></table>"
        )
        audit_html = f"""
<div class="card"><h2>事件计划审计（只读）</h2>
  <p class="muted">最近 {int(audit_report.get('window_minutes', 0))} 分钟 ·
     planned {int(audit_report.get('scanned_planned', 0))} 条 ·
     多计划 {int(audit_report.get('multiple_plans', 0))} ·
     过期仍 planned {int(audit_report.get('stale_planned', 0))}</p>
  {audit_table}
  <p class="muted">审计不会改库；需要处理时去记忆页单条或批量操作。</p>
</div>"""

    body = f"""
<div class="card"><h2>事件时效回填 · 数据修复</h2>
  <p class="warn">这是会写入历史数据的维护操作。建议先「扫描」，确认样例后再「应用」；
     应用前会自动备份数据库，并写入审计日志。</p>
  <div class="grid">{cards_html}</div>
  <p class="muted">只处理 role=user + category=event + event_at 为空；
     模糊表达（过两天/改天/下次）保持 NULL；不碰 fact / assistant。</p>
  <p><a href="/console/maintenance?audit=1">运行只读计划审计 →</a></p>
</div>
<div class="card"><h2>扫描 / 应用</h2>
  <form method="post" action="/console/maintenance/backfill">
    <input type="hidden" name="csrf" value="{csrf}">
    <p>最多处理 <input type="number" name="limit" min="1" placeholder="全部" style="width:110px">
       （留空=全部候选）</p>
    <p><button type="submit" name="apply" value="0">扫描（dry-run）</button></p>
    <p class="muted">应用前在右侧输入 <b>BACKFILL</b> 确认：
       <input name="confirm" placeholder="BACKFILL" size="14">
       <button class="danger" type="submit" name="apply" value="1"
               onclick="return confirm('确认写入历史数据？')">应用回填</button>
    </p>
  </form>
  <p class="muted">应用按钮不会自动执行；必须输入 BACKFILL 并再次确认。</p>
</div>
{error_html}{message_html}{report_html}{audit_html}"""
    return _shell("维护 · 事件时效回填", body)


@router.get("/console/maintenance")
def maintenance_page(request: Request) -> HTMLResponse:
    audit_report = None
    audit_error = ""
    if request.query_params.get("audit") == "1":
        try:
            audit_report = run_audit(request.app.state.settings, minutes=120)
        except Exception as exc:
            audit_error = f"审计失败：{exc}"
    return _backfill_page(
        request, audit_report=audit_report, audit_error=audit_error
    )


@router.post("/console/maintenance/backfill")
def maintenance_backfill(
    request: Request,
    csrf: str = Form(""),
    apply: str = Form("0"),
    limit: str = Form(""),
    confirm: str = Form(""),
) -> HTMLResponse:
    _ensure_csrf(request, csrf)
    settings: Settings = request.app.state.settings
    do_apply = apply.strip().lower() in {"1", "true", "yes", "on"}
    if do_apply and confirm.strip().upper() != "BACKFILL":
        return _backfill_page(request, error="应用回填需要在确认框输入 BACKFILL。")

    limit_value: int | None = None
    if limit.strip():
        try:
            limit_value = int(limit)
        except ValueError:
            return _backfill_page(request, error="limit 必须是整数。")
        if limit_value <= 0:
            return _backfill_page(request, error="limit 必须大于 0。")

    try:
        report = run_backfill(
            settings,
            apply=do_apply,
            limit=limit_value,
            backup=True,
        )
    except Exception as exc:
        audit(settings, "temporal_backfill_failed", peer=_peer(request),
              error=type(exc).__name__)
        return _backfill_page(request, error=f"回填失败：{exc}")

    audit(
        settings,
        "temporal_backfill",
        peer=_peer(request),
        dry_run=not do_apply,
        candidates=report.get("candidates"),
        parsed=report.get("parsed"),
        updated=report.get("updated"),
        backup=bool(report.get("backup")),
    )
    if do_apply:
        if report.get("backup"):
            message = f"已写入 {report.get('updated', 0)} 条；备份已生成。"
        else:
            message = f"已写入 0 条；没有可回填项，未生成备份。"
    else:
        message = f"扫描完成：可写入 {report.get('parsed', 0)} 条，未改库。"
    return _backfill_page(request, report=report, message=message)


@router.get("/console/export.jsonl")
def export_jsonl(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    qp = request.query_params
    conn = store.open_conn(settings)
    try:
        rows, _ = _fetch_memories(
            conn,
            q=(qp.get("q") or "").strip(),
            visibility=(qp.get("visibility") or "").strip(),
            is_active=(qp.get("is_active") or "all").strip(),
            account=(qp.get("account") or "").strip(),
            scope_type=(qp.get("scope_type") or "").strip(),
            event_state=(qp.get("event_state") or "").strip(),
            page=1,
            page_size=5000,
        )
    finally:
        conn.close()
    lines = []
    for row in rows:
        record = {
            "id": row["id"],
            "account": row["account"],
            "scope": {
                "type": row["scope_type"],
                "user_id": row["user_id"],
                "group_id": row["group_id"],
            },
            "role": row["role"],
            "speaker_name": row["speaker_name"],
            "content": row["content"],
            "visibility": row["visibility"],
            "fact_key": row["fact_key"],
            "category": row["category"],
            "importance": row["importance"],
            "created_at": row["created_at"],
            "happened_at": row["happened_at"],
            "event_at": row["event_at"],
            "valid_until": row["valid_until"],
            "event_status": row["event_status"],
            "is_active": row["is_active"],
            "deleted_at": row["deleted_at"],
        }
        lines.append(json.dumps(record, ensure_ascii=False))
    return Response(
        "\n".join(lines) + ("\n" if lines else ""),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="qq-memory.jsonl"'},
    )


# ---------------------------------------------------------------------------
# 数据库档案
# ---------------------------------------------------------------------------

def _fmt_size(size: int) -> str:
    if size <= 0:
        return "-"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / 1024 / 1024:.2f} MB"


def _db_page(request: Request, message: str = "", error: str = "") -> HTMLResponse:
    settings: Settings = request.app.state.settings
    profiles.ensure_default()
    rows = profiles.describe(settings.db_path)
    csrf = csrf_for(request.app.state.console_token, _session_exp(request))
    env_db = os.environ.get("QQ_MEMORY_DB", "").strip()

    banners = []
    if message:
        banners.append(f'<p class="ok">{html.escape(message)}</p>')
    if error:
        banners.append(f'<p class="bad">{html.escape(error)}</p>')
    if env_db:
        banners.append(
            '<p class="warn">环境变量 QQ_MEMORY_DB 已设置（' + html.escape(env_db) +
            '），它的优先级高于这里的选择，切换不会生效。要在这里切换，先从启动脚本里去掉这一行。</p>'
        )

    body_rows = []
    for row in rows:
        mark = "●" if row["in_use"] else ("○" if row["is_current"] else "")
        state = []
        if row["in_use"]:
            state.append('<span class="ok">运行中</span>')
        if row["is_current"]:
            state.append('<span class="tag">已选中（重启生效）</span>')
        if not row["exists"]:
            state.append('<span class="muted">尚未创建</span>')
        counts = (
            f'{row["memories"]} 条 / 流水 {row["turns"]} 条'
            if row["memories"] is not None
            else '<span class="muted">读不到（未建表或路径不可访问）</span>'
        )
        action = ''
        if row["is_current"]:
            action = '<span class="muted">当前选项</span>'
        else:
            action = (
                '<form class="inline" method="post" action="/console/db/select">'
                f'<input type="hidden" name="csrf" value="{csrf}">'
                f'<input type="hidden" name="name" value="{html.escape(row["name"])}">'
                '<button type="submit">切换</button></form>'
            )
        body_rows.append(
            "<tr>"
            f'<td>{html.escape(mark)} {html.escape(row["name"])}</td>'
            f'<td><code>{html.escape(row["path"])}</code></td>'
            f"<td>{counts}</td>"
            f'<td>{_fmt_size(row["size_bytes"])}</td>'
            f'<td>{" ".join(state) or "-"}</td>'
            f"<td>{action}</td>"
            "</tr>"
        )
    table = (
        "<table><tr><th></th><th>名称</th><th>库文件</th><th>记忆 / 流水</th><th>大小</th>"
        "<th>状态</th><th>操作</th></tr>" + "".join(body_rows) + "</table>"
        if body_rows
        else '<p class="muted">还没有登记任何库。</p>'
    )

    body = (
        '<div class="card"><h2>切换数据库</h2>'
        + "".join(banners)
        + '<p class="muted">选择结果写进 <code>data/db-profiles.json</code>，<b>下一次启动生效</b>。'
          "运行中的进程仍在使用上面标着「运行中」的那个库。切换后关掉侧车窗口、"
          '重新运行 <code>run.cmd</code> 即可。</p>'
        + table
        + "</div>"
        + '<div class="card"><h2>登记一个库</h2>'
        + '<p class="muted">填名称和路径；路径不存在时，下次启动会自动建库并建表。'
          "填目录会补上 qq-memory.db。同名会覆盖路径。</p>"
        + '<form class="inline" method="post" action="/console/db/register">'
        + f'<input type="hidden" name="csrf" value="{csrf}">'
        + '<input name="name" placeholder="名称，例如 工作" size="12">'
        + '<input name="path" placeholder="路径，例如 D:\\qq-memory\\work.db" size="42">'
        + '<button type="submit">登记</button></form></div>'
    )
    return _shell("数据库", body)


@router.get("/console/db")
def db_page(request: Request) -> HTMLResponse:
    return _db_page(request)


@router.post("/console/db/select")
def db_select(
    request: Request,
    name: str = Form(""),
    csrf: str = Form(""),
) -> HTMLResponse:
    _ensure_csrf(request, csrf)
    settings: Settings = request.app.state.settings
    try:
        entry = profiles.select(name)
    except ValueError as exc:
        return _db_page(request, error=str(exc))
    audit(
        settings,
        "db_select",
        name=entry["name"],
        path=entry["path"],
        running=str(settings.db_path),
        peer=_peer(request),
        token_fp=_token_fp(request.app.state.console_token),
    )
    return _db_page(
        request,
        message=f"已选中「{entry['name']}」，下次启动生效。",
    )


@router.post("/console/db/register")
def db_register(
    request: Request,
    name: str = Form(""),
    path: str = Form(""),
    csrf: str = Form(""),
) -> HTMLResponse:
    _ensure_csrf(request, csrf)
    settings: Settings = request.app.state.settings
    try:
        entry = profiles.register(name, path)
    except ValueError as exc:
        return _db_page(request, error=str(exc))
    audit(
        settings,
        "db_register",
        name=entry["name"],
        path=entry["path"],
        peer=_peer(request),
        token_fp=_token_fp(request.app.state.console_token),
    )
    return _db_page(request, message=f"已登记「{entry['name']}」→ {entry['path']}。")
