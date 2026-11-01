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
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

import db_init
import profiles
import slang
import migrate
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
    """令牌固定放在项目 data 下，与用哪个库无关。

    以前放在库旁边：一换库令牌路径就变，浏览器里的登录态直接失效，
    看起来就像"切换数据库之后打不开了"。
    """
    return Path(__file__).resolve().parent / "data" / "console-token"


def load_console_token(settings: Settings) -> str:
    env = os.environ.get("QQ_MEMORY_CONSOLE_TOKEN", "").strip()
    if env:
        return env
    path = token_path(settings)
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    # 兼容旧版本：令牌原本放在库文件旁边，有就沿用，别让已登录的会话失效
    legacy = settings.db_path.parent / "console-token"
    if legacy != path and legacy.exists():
        token = legacy.read_text(encoding="utf-8").strip()
        if token:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(token + "\n", encoding="utf-8")
            except OSError:
                pass
            return token
    token = secrets.token_urlsafe(24)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token + "\n", encoding="utf-8")
    except OSError as exc:  # 目录不可写时用内存里的令牌，别把启动拖死
        logger.warning("控制台令牌无法写入 %s（%s），本次运行用临时令牌", path, exc)
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
.flash{position:sticky;top:0;z-index:9;margin:0 0 12px}
.tag{display:inline-block;padding:1px 7px;border-radius:999px;background:#17233a;
border:1px solid #334155;font-size:12px;color:#cbd5e1}
.pager{display:flex;gap:10px;align-items:center;margin-top:10px}
pre{white-space:pre-wrap;word-break:break-all;margin:0;font-size:12px;color:#cbd5e1}
.login{max-width:420px;margin:12vh auto}
"""


def boot_banner(request: Request) -> str:
    """启动时回退过库的话，每个页面顶部都提示一句。"""
    message = getattr(request.app.state, "boot_error", "")
    return f'<p class="bad">{html.escape(message)}</p>' if message else ""


def _shell(
    title: str, body: str, *, nav: bool = True, status_code: int = 200, refresh: int = 0
) -> HTMLResponse:
    nav_html = (
        '<a href="/console">总览</a>'
        '<a href="/console/memories">记忆</a>'
        '<a href="/console/maintenance">维护</a>'
        '<a href="/console/db">数据库</a>'
        '<a href="/console/dict">词典</a>'
        '<a href="/console/env">环境自检</a>'
        '<a href="/console/export.jsonl">导出 JSONL</a>'
        '<span class="spacer"></span>'
        '<a href="/console/logout">退出</a>'
        if nav
        else ""
    )
    return HTMLResponse(
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        + (f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else "")
        + f"<title>{html.escape(title)} · qq_memory</title><style>{CSS}</style></head><body>"
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
            "SELECT count(*) FROM memories WHERE state='active'"
        ).fetchone()[0]
        deleted = conn.execute(
            "SELECT count(*) FROM memories WHERE state='deleted'"
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
<div class="card"><h2>批量导入</h2>
  <p class="muted">支持三种格式：导出的 .jsonl（每行一个对象）、JSON 数组、{{"items":[...]}}。
     可以直接选择文件，也可以粘贴。重复的记录会跳过。</p>
  <form method="post" action="/console/actions/import" enctype="multipart/form-data">
    <input type="hidden" name="csrf" value="{csrf_for(request.app.state.console_token, _session_exp(request))}">
    <p>重复处理：<select name="mode">
      <option value="skip">跳过已存在的（默认，可安全重复导入）</option>
      <option value="force">不按内容判重（仍按 message_id 去重）</option>
    </select></p>
    <p><input type="file" name="file" accept=".jsonl,.json,.txt"></p>
    <textarea name="payload" rows="6" style="width:100%" placeholder='[{{"account":"1234567890","scope":{{"type":"private","user_id":"10001"}},"content":"..."}}]'></textarea>
    <p><button type="submit">导入（source=import）</button></p>
  </form>
</div>"""
    return _shell("总览", boot_banner(request) + body)


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
    if is_active in ("active", "inactive", "deleted"):
        where.append("state=?")
        params.append(is_active)
    elif is_active == "1":      # 旧链接
        where.append("state='active'")
    elif is_active == "0":
        where.append("state<>'active'")
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
        "SELECT id, account, scope_type, user_id, group_id, speaker_id, speaker_name, role, content,"
        " message_id, session_id, generation,"
        " visibility, fact_key, importance, created_at, happened_at, last_recalled_at,"
        " recall_count, is_active, deleted_at, superseded_by, category, state,"
        " event_at, valid_until, event_status "
        f"FROM memories WHERE {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
        [*params, page_size, (page - 1) * page_size],
    ).fetchall()
    return list(rows), total


def _href(path: str, **params: object) -> str:
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    return path + ("?" + urlencode(clean) if clean else "")


def speaker_cell(row: sqlite3.Row) -> str:
    """说话人：有昵称就「昵称 + QQ 号」，没有就只显示 QQ 号；侧车自己的消息显示「我」。"""
    name = (row["speaker_name"] or "").strip()
    sid = (row["speaker_id"] or row["user_id"] or "").strip()
    if row["role"] == "assistant":
        return html.escape(name or "我") + '<br><span class="muted">侧车</span>' if name else '<span class="ok">我</span><br><span class="muted">侧车</span>'
    if name and sid:
        return f'{html.escape(name)}<br><span class="muted">{html.escape(sid)}</span>'
    if name:
        return html.escape(name)
    return html.escape(sid or "—") if sid else '<span class="muted">—</span>'


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
        for v, label in (("all", "全部状态"), ("active", "生效中"),
                         ("inactive", "停用"), ("deleted", "已软删"))
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
        state = (row["state"] or "").strip()
        if state not in store.STATES:      # 旧行没回填时按老字段推
            state = ("deleted" if row["deleted_at"]
                     else ("inactive" if not row["is_active"] else "active"))
        css, label = {
            "active": ("ok", "生效"),
            "inactive": ("warn", "停用"),
            "deleted": ("bad", "已软删"),
        }[state]
        out = f'<span class="{css}">{label}</span>'
        ev = (row["event_status"] or "").strip()
        if ev:
            ev_label = {"planned": "计划中", "done": "已完成", "cancelled": "已取消"}.get(ev, ev)
            out += f'<br><span class="muted">事件：{ev_label}</span>'
        return out

    trs: list[str] = []
    for row in rows:
        fresh = freshness_score(
            row["happened_at"], row["created_at"], row["fact_key"]
        )
        scope = ("私聊" if row["scope_type"] == "private" else f"群{row['group_id'] or ''}")
        peer = row["user_id"] if row["scope_type"] == "private" else row["group_id"]
        if peer:
            scope = f'{scope} · {peer}'
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
        if (row["state"] or "") != store.STATE_ACTIVE or not row["is_active"] or row["deleted_at"]:
            actions += (
                f'<form class="inline" method="post" action="/console/actions/restore">'
                f'<input type="hidden" name="csrf" value="{csrf}">'
                f'<input type="hidden" name="memory_id" value="{row["id"]}">'
                f'<button type="submit" class="ghost">恢复</button></form>'
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
            f'<td>{speaker_cell(row)}</td>'
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
    msg_cls = "bad" if (qp.get("err") or "") == "1" else "ok"
    message_html = (
        f'<div class="card flash"><p class="{msg_cls}" id="flash">{html.escape(msg)}</p></div>'
        if msg else ""
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
  <p class="muted">勾选下方列表左侧的复选框，选好操作、确认无误后勾上确认框再提交。</p>
  <form id="bulk-form" method="post" action="/console/actions/bulk">
    <input type="hidden" name="csrf" value="{csrf}">
    <input type="hidden" name="next" value="{html.escape(return_to)}">
    <label><input type="checkbox" onclick="for(const c of document.querySelectorAll('input[name=memory_ids]')) c.checked=this.checked"> 全选本页</label>
    <select name="action" id="bulk-action" required>
      <option value="">— 把勾选的记录改成… —</option>
      <optgroup label="记录状态">
        <option value="activate">启用 / 恢复（参与召回）</option>
        <option value="deactivate">停用（保留，但不参与召回）</option>
        <option value="soft_delete">软删（标记删除）</option>
      </optgroup>
      <optgroup label="事件进度（只对事件记忆生效）">
        <option value="event_planned">计划中</option>
        <option value="event_done">已完成</option>
        <option value="event_cancelled">已取消</option>
      </optgroup>
    </select>
    <input name="reason" placeholder="原因（可选）" size="16">
    <label class="muted"><input type="checkbox" name="confirm" id="bulk-confirm" value="yes">
      我确认要批量修改勾选的这些记录</label>
    <button type="submit" class="danger" id="bulk-submit" disabled>执行批量操作</button>
    <span class="muted" id="bulk-hint">先选一个操作</span>
  </form>
  <script>
  (function () {{
    var f = document.getElementById("bulk-form");
    var a = document.getElementById("bulk-action");
    var c = document.getElementById("bulk-confirm");
    var b = document.getElementById("bulk-submit");
    var h = document.getElementById("bulk-hint");
    function picked() {{
      return document.querySelectorAll("input[name=memory_ids]:checked").length;
    }}
    function sync() {{
      var n = picked();
      b.disabled = !(a.value !== "" && c.checked && n > 0);
      if (!a.value) h.textContent = "先选一个操作";
      else if (n === 0) h.textContent = "还没勾选任何记录";
      else if (!c.checked) h.textContent = "勾上确认框才能提交";
      else h.textContent = "可以提交了（已选 " + n + " 条）";
    }}
    a.addEventListener("change", sync);
    c.addEventListener("change", sync);
    document.addEventListener("change", function (e) {{
      if (e.target && e.target.name === "memory_ids") sync();
    }});
    f.addEventListener("submit", function (e) {{
      if (!picked()) {{ e.preventDefault(); h.textContent = "还没勾选任何记录"; }}
    }});
    sync();
  }})();
  </script>
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
        store.set_state(conn, memory_id, store.STATE_INACTIVE)
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


BULK_ACTIONS: dict[str, tuple[str, str]] = {
    "activate": ("state", "active"),
    "deactivate": ("state", "inactive"),
    "soft_delete": ("state", "deleted"),
    "event_planned": ("event", "planned"),
    "event_done": ("event", "done"),
    "event_cancelled": ("event", "cancelled"),
}

BULK_LABELS: dict[str, str] = {
    "activate": "启用",
    "deactivate": "停用",
    "soft_delete": "软删",
    "event_planned": "「计划中」",
    "event_done": "「已完成」",
    "event_cancelled": "「已取消」",
}


@router.post("/console/actions/bulk")
def action_bulk(
    request: Request,
    memory_ids: list[int] = Form(default=[]),
    action: str = Form(""),
    status: str = Form(""),  # 兼容旧页面：action=event_status 时用

    reason: str = Form(""),
    confirm: str = Form(""),
    next: str = Form("/console/memories"),
    csrf: str = Form(""),
) -> RedirectResponse:
    _ensure_csrf(request, csrf)
    ids = sorted({int(value) for value in memory_ids if int(value) > 0})
    target = next if next.startswith("/console/memories") else "/console/memories"

    def _back(msg: str) -> RedirectResponse:
        """失败/拒绝：红字提示。"""
        sep = "&" if "?" in target else "?"
        return RedirectResponse(
            f"{target}{sep}{urlencode({'msg': msg, 'err': '1'})}", status_code=303
        )

    def _ok(msg: str) -> RedirectResponse:
        """执行成功：绿字提示。"""
        sep = "&" if "?" in target else "?"
        return RedirectResponse(
            f"{target}{sep}{urlencode({'msg': msg})}", status_code=303
        )

    if not ids:
        return _back("没有勾选任何记录")
    if len(ids) > 200:
        return _back(f"一次最多 200 条，这次勾了 {len(ids)} 条")
    if action == "event_status":  # 旧页面
        action = f"event_{status}" if status in ("planned", "done", "cancelled") else ""
    if action not in BULK_ACTIONS:
        return _back("先在下拉框里选一个操作")
    if confirm.strip().lower() not in ("yes", "on", "1", "true"):
        return _back("还没勾选确认框，批量操作没有执行")
    kind, state_value = BULK_ACTIONS[action]

    settings: Settings = request.app.state.settings
    placeholders = ",".join("?" for _ in ids)
    count = 0
    conn = store.open_conn(settings)
    try:
        if kind == "event":
            cur = conn.execute(
                f"UPDATE memories SET event_status=? WHERE id IN ({placeholders}) "
                f"AND category='event' AND (event_at IS NOT NULL OR event_status IS NOT NULL)",
                (state_value, *ids),
            )
            count = int(cur.rowcount or 0)
        else:
            count = store.set_state(conn, ids, state_value)
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
    label = BULK_LABELS.get(action, action)
    if count == 0 and kind == "event":
        return _back(
            "0 条命中——勾选的全是普通对话，没有事件时间；"
            "普通记忆请用「启用 / 停用 / 软删」"
        )
    return _ok(f"{count} 条已改为{label}")


@router.post("/console/actions/restore")
def action_restore(
    request: Request,
    memory_id: int = Form(0),
    csrf: str = Form(""),
) -> RedirectResponse:
    """把停用/软删的记忆恢复回来：清 deleted_at、清取代链、置回 active。"""
    _ensure_csrf(request, csrf)
    settings: Settings = request.app.state.settings
    conn = store.open_conn(settings)
    try:
        row = conn.execute(
            "SELECT id, content, is_active, deleted_at, superseded_by FROM memories WHERE id=?",
            (memory_id,),
        ).fetchone()
        if row is None:
            return _back_to_list("找不到这条记忆")
        store.set_state(conn, memory_id, store.STATE_ACTIVE)
        conn.commit()
    finally:
        conn.close()
    audit(settings, "restore", memory_id=memory_id, was_active=bool(row["is_active"]),
          had_deleted_at=row["deleted_at"], superseded_by=row["superseded_by"],
          peer=_peer(request), token_fp=_token_fp(request.app.state.console_token))
    return _back_to_list(f"已恢复 #{memory_id}")


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
            "UPDATE memories SET deleted_at=?, is_active=0, superseded_by=NULL WHERE id=? "
            "AND deleted_at IS NULL",
            (iso_now(), memory_id),
        )
        store.set_state(conn, memory_id, store.STATE_DELETED)
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


_SYNC_HINTS = ("download", "下载", "onedrive", "dropbox", "坚果云", "百度网盘", "icloud")


def _path_hint(path) -> str:
    """库放在下载/同步目录里，SQLite 很容易被占用或拦写。"""
    text = str(path).lower()
    if any(word in text for word in _SYNC_HINTS):
        return "（这个目录是下载/同步目录，SQLite 容易被占用或被安全软件拦写，建议换到普通本地目录）"
    return ""


def _import_key(body: dict) -> tuple | None:
    """同一 account / 会话 / 角色 / 内容 / 时间 视为同一条记忆。"""
    try:
        scope_type, user_id, group_id = store.parse_scope(body.get("scope"))
    except ValueError:
        return None
    account = str(body.get("account") or "")
    if not account:
        return None
    key = store.scope_key(account, scope_type, user_id, group_id)
    return (
        account,
        key,
        str(body.get("role") or "user"),
        str(body.get("content") or ""),
        str(body.get("happened_at") or ""),
    )


def _fetch_import_keys(conn) -> set[tuple]:
    """把库里已有的判重键取出来，避免逐条查询。"""
    rows = conn.execute(
        "SELECT account, scope_key, role, content, COALESCE(happened_at, '') FROM memories"
    ).fetchall()
    return {tuple(row) for row in rows}


def _parse_import_payload(raw: str) -> tuple[list, list[dict]]:
    """解析导入内容：JSON 数组、{"items":[...]}、或 JSONL（每行一个对象）。

    JSONL 是关键：导出就是每行一个对象的格式，导出的文件得能直接导回来。
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("内容为空")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as first:
        items: list = []
        problems: list[dict] = []
        for lineno, line in enumerate(text.splitlines(), 1):
            line = line.strip().rstrip(",")
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                problems.append({"index": lineno, "ok": False,
                                 "error": f"第 {lineno} 行 JSON 解析失败：{exc.msg}"})
                continue
            if isinstance(obj, dict):
                items.append(obj)
            else:
                problems.append({"index": lineno, "ok": False, "error": f"第 {lineno} 行不是对象"})
        if not items:
            raise ValueError(
                f"JSON 解析失败：第 {first.lineno} 行第 {first.colno} 列 {first.msg}"
            )
        return items, problems
    if isinstance(data, dict) and "items" in data:
        data = data["items"]
    if not isinstance(data, list):
        raise ValueError("需要 JSON 数组，或每行一个对象的 JSONL")
    return data, []


def _import_result_page(
    results: list[dict], ok_count: int, total: int, error: str = "", skipped: int = 0
) -> HTMLResponse:
    rows = []
    for item in results[:200]:
        index = item.get("index")
        outcome = item.get("outcome") or {}
        if item.get("error"):
            state, detail = '<span class="bad">失败</span>', html.escape(str(item["error"]))
        elif outcome.get("recorded"):
            state, detail = '<span class="ok">已写入</span>', f'#{outcome.get("id")}'
        elif item.get("skipped"):
            state, detail = '<span class="muted">已存在，跳过</span>', html.escape(
                str(item.get("reason") or "")
            )
        elif outcome.get("deduped"):
            state, detail = '<span class="muted">重复，已跳过</span>', html.escape(
                str(outcome.get("reason") or "")
            )
        else:
            state = '<span class="bad">失败</span>'
            detail = html.escape(str(outcome.get("error") or outcome.get("reason") or "未写入"))
        rows.append(f"<tr><td>{index}</td><td>{state}</td><td>{detail}</td></tr>")
    table = (
        "<table><tr><th>#</th><th>结果</th><th>说明</th></tr>" + "".join(rows) + "</table>"
        if rows
        else '<p class="muted">没有条目</p>'
    )
    banner = (
        f'<p class="bad">{html.escape(error)}</p>' if error
        else (f'<p class="ok">共 {total} 条：写入 {ok_count} 条'
              + (f'，已存在跳过 {skipped} 条' if skipped else '')
              + '，其余为重复或错误。</p>')
    )
    body = (
        '<div class="card"><h2>导入结果</h2>'
        + banner
        + table
        + '<p><a href="/console">返回总览</a> · <a href="/console/memories">查看记忆</a></p>'
        + "</div>"
    )
    return _shell("导入结果", body)


@router.post("/console/actions/import")
def action_import(
    request: Request,
    payload: str = Form(""),
    csrf: str = Form(""),
    file: UploadFile | None = File(None),
    json_out: str = Form(""),
    mode: str = Form(""),
) -> Response:
    _ensure_csrf(request, csrf)
    settings: Settings = request.app.state.settings
    wants_json = json_out == "1" or "application/json" in (request.headers.get("accept") or "")
    form_mode = mode

    text = payload
    if file is not None and getattr(file, "filename", ""):
        raw = file.file.read()
        if raw:
            text = raw.decode("utf-8-sig", "replace")

    try:
        data, problems = _parse_import_payload(text)
    except ValueError as exc:
        if wants_json:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return _import_result_page([], 0, 0, error=str(exc))

    writer = request.app.state.writer
    results = list(problems)
    ok_count = 0
    skipped = 0
    mode = "force" if (form_mode or "").strip().lower() == "force" else "skip"
    existing: set[tuple] = set()
    if mode == "skip":
        conn0 = store.open_conn(settings)
        try:
            existing = _fetch_import_keys(conn0)
        finally:
            conn0.close()
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            results.append({"index": index, "error": "不是对象"})
            continue
        body = dict(item)
        body.pop("id", None)
        if body.get("deleted_at"):
            results.append({"index": index, "error": "这条是已删除的记录，跳过"})
            continue
        body.setdefault("source", "import")
        body.setdefault("meta", {"trigger": "command"})
        key = _import_key(body)
        if mode == "skip" and key is not None and key in existing:
            skipped += 1
            results.append({"index": index, "skipped": True, "reason": "同一条记忆已存在"})
            continue
        try:
            outcome = writer.write(body)
        except Exception as exc:  # noqa: BLE001 - 单条失败不能中断整批
            results.append({"index": index, "error": f"{type(exc).__name__}: {exc}"})
            continue
        if outcome.get("ok") and outcome.get("recorded"):
            ok_count += 1
            if key is not None:
                existing.add(key)
        results.append({"index": index, "outcome": outcome})

    audit(settings, "import", items=len(data), recorded=ok_count,
          peer=_peer(request), token_fp=_token_fp(request.app.state.console_token))
    if wants_json:
        return JSONResponse({"ok": True, "total": len(data), "recorded": ok_count,
                             "skipped": skipped, "mode": mode, "results": results})
    return _import_result_page(results, ok_count, len(data), skipped=skipped)


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
            "message_id": row["message_id"],
            "session_id": row["session_id"],
            "generation": row["generation"],
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
        else:
            fmt = db_init.inspect_db(row["path"])
            if not fmt["ok"]:
                state.append(f'<span class="bad" title="{html.escape(fmt["reason"])}">格式不符</span>')
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
        danger = ""
        if not row["in_use"]:
            danger = (
                '<form class="inline" method="post" action="/console/db/forget">'
                f'<input type="hidden" name="csrf" value="{csrf}">'
                f'<input type="hidden" name="name" value="{html.escape(row["name"])}">'
                '<button type="submit">移除档案</button></form>'
                '<form class="inline" method="post" action="/console/db/delete">'
                f'<input type="hidden" name="csrf" value="{csrf}">'
                f'<input type="hidden" name="name" value="{html.escape(row["name"])}">'
                f'<input type="hidden" name="confirm" value="yes">'
                '<button type="submit">删除文件</button></form>'
            )
        body_rows.append(
            "<tr>"
            f'<td>{html.escape(mark)} {html.escape(row["name"])}</td>'
            f'<td><code>{html.escape(row["path"])}</code></td>'
            f"<td>{counts}</td>"
            f'<td>{_fmt_size(row["size_bytes"])}</td>'
            f'<td>{" ".join(state) or "-"}</td>'
            f"<td>{action} {danger}</td>"
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
        + '<div class="card"><h2>新建一个库</h2>'
        + '<p class="muted">填目录或完整路径都行：填目录会补上 qq-memory.db。'
          "会先建目录、再建库并初始化表；建好就登记，切换后重启生效。</p>"
        + '<form class="inline" method="post" action="/console/db/create">'
        + f'<input type="hidden" name="csrf" value="{csrf}">'
        + '<input name="name" placeholder="名称，例如 工作" size="12">'
        + '<input name="path" placeholder="例如 D:\\qq-memory\\工作.db" size="42">'
        + '<button type="submit">新建</button></form></div>'
        + '<div class="card"><h2>登记一个库</h2>'
        + '<p class="muted">填名称和路径；路径不存在时，下次启动会自动建库并建表。'
          "填目录会补上 qq-memory.db。同名会覆盖路径。</p>"
        + '<form class="inline" method="post" action="/console/db/register">'
        + f'<input type="hidden" name="csrf" value="{csrf}">'
        + '<input name="name" placeholder="名称，例如 工作" size="12">'
        + '<input name="path" placeholder="路径，例如 D:\\qq-memory\\work.db" size="42">'
        + '<button type="submit">登记</button></form></div>'
    )
    card, busy = _tier_card(request, csrf)
    return _shell("数据库", boot_banner(request) + body + card, refresh=3 if busy else 0)


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
    state = profiles.load()
    target = next((Path(e["path"]) for e in state["entries"] if e["name"] == name.strip()), None)
    if target is None:
        return _db_page(request, error=f"没有这个档案：{name.strip()}")
    info = db_init.inspect_db(target)
    if not info["ok"]:
        return _db_page(
            request,
            error=f"没有切换：{target} —— {info['reason']}",
        )
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
        message=f"已选中「{entry['name']}」，下次启动生效。" + _path_hint(entry["path"]),
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
        target = profiles.normalize_path(path)
    except ValueError as exc:
        return _db_page(request, error=str(exc))
    info = db_init.inspect_db(target)
    if not info["ok"]:
        return _db_page(request, error=f"不能登记：{target} —— {info['reason']}")
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
    return _db_page(
        request,
        message=f"已登记「{entry['name']}」→ {entry['path']}。"
        + (f"（{info['note']}）" if info.get("note") else "")
        + _path_hint(entry["path"]),
    )

# ---------------------------------------------------------------------------
# 词典档位
# ---------------------------------------------------------------------------

TIER_LABEL = {"full": "完整词典", "3w": "3 万词"}


def _tier_label(tier: str) -> str:
    text = (tier or "").strip()
    return TIER_LABEL.get(text.lower(), text or "完整词典")


def _post_form(action: str, csrf: str, label: str) -> str:
    return (
        f'<form class="inline" method="post" action="{action}">'
        f'<input type="hidden" name="csrf" value="{csrf}">'
        f"<button type=\"submit\">{html.escape(label)}</button></form>"
    )


def _tier_card(request: Request, csrf: str) -> tuple[str, bool]:
    """数据库页里的档位卡片；返回 (HTML, 是否在迁移中)。"""
    settings: Settings = request.app.state.settings
    conn = None
    try:
        conn = store.open_conn(settings)
        migrate.ensure_schema(conn)
        st = migrate.status(conn)
        est = migrate.estimate(conn)
        stale = (migrate.meta_get(conn, "tokens.stale", "0") or "0") == "1"
    except Exception as exc:  # noqa: BLE001 - 控制台不能因为读不到状态就白屏
        return (
            '<div class="card"><h2>词典档位</h2>'
            f'<p class="bad">迁移状态读取失败：{html.escape(str(exc))}</p></div>',
            False,
        )
    finally:
        if conn is not None:
            conn.close()

    now_tier = profiles.entry_tier()
    busy = migrate.job_running() or st["state"] == "running"
    parts = [
        '<div class="card"><h2>词典档位</h2>',
        '<p class="muted">档位决定 jieba 常驻内存与分词质量。切换会重算全库分词：'
        "期间可以正常聊天，完成后<b>重启侧车</b>才生效（不会自动重启）。</p>",
    ]
    if os.environ.get("QQ_MEMORY_JIEBA_DICT", "").strip():
        parts.append('<p class="warn">环境变量 QQ_MEMORY_JIEBA_DICT 已设置，它会覆盖这里的档位。</p>')
    parts.append(
        f'<p>当前档位：<b>{html.escape(_tier_label(now_tier))}</b>　'
        f'库内记忆 {est["rows"]} 条　'
        f'切换预估：约 {max(1, round(est["seconds"]))} 秒（含备份 {est["backup_seconds"]:.0f} 秒）</p>'
    )

    if st["state"] == "ready":
        parts.append(
            f'<p class="ok">迁移已完成（{st["done"]} 行重算），<b>重启侧车后生效</b>。</p>'
        )
        parts.append(_post_form("/console/db/tier/cancel", csrf, "放弃并回退"))
    elif st["state"] in ("running", "paused"):
        paused = st["state"] == "paused"
        parts.append(
            f'<p>迁移进度：{st["done"]} / {st["total"]}（{st["percent"]}%）'
            + ('　<span class="muted">已暂停</span>' if paused else "")
            + "</p>"
        )
        if paused:
            parts.append(_post_form("/console/db/tier/continue", csrf, "继续"))
        else:
            parts.append(_post_form("/console/db/tier/stop", csrf, "暂停"))
        parts.append(_post_form("/console/db/tier/cancel", csrf, "放弃"))
    elif stale:
        import slang as slang_mod

        snap = slang_mod.status()
        parts.append(
            f'<p class="warn">黑话词表变过（{snap["total"]} 条词）：已有记忆的分词可能过期，'
            "重算后检索才会用上新词。</p>"
        )
        parts.append(_post_form("/console/db/retokenize", csrf, "重算分词"))
    else:
        cur = (now_tier or "full").lower()
        options = "".join(
            f'<option value="{value}"{" selected" if value == cur else ""}>{label}</option>'
            for value, label in (
                ("3w", "3 万词（约省 88 MB）"),
                ("full", "完整词典（分词质量最高）"),
            )
        )
        parts.append(
            '<form class="inline" method="post" action="/console/db/tier">'
            f'<input type="hidden" name="csrf" value="{csrf}">'
            f'<select name="tier">{options}</select>'
            '<button type="submit">开始迁移</button></form>'
        )
        if st.get("error"):
            parts.append(f'<p class="bad">{html.escape(str(st["error"]))}</p>')

    parts.append("</div>")
    return "".join(parts), busy


def _tier_error(request: Request) -> str:
    error = migrate.job_error()
    return f"后台迁移出错：{html.escape(error)}" if error else ""


@router.post("/console/db/tier")
def db_tier(request: Request, tier: str = Form(""), csrf: str = Form("")) -> HTMLResponse:
    _ensure_csrf(request, csrf)
    settings: Settings = request.app.state.settings
    target = (tier or "").strip().lower()
    if target not in ("full", "3w"):
        return _db_page(request, error="档位只能是 3w 或 full")
    if migrate.job_running():
        return _db_page(request, error="已有迁移在运行，等它结束或先放弃")
    try:
        conn = store.open_conn(settings)
        try:
            info = migrate.start(conn, target, from_tier=profiles.entry_tier())
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        return _db_page(request, error=f"迁移启动失败：{exc}")
    profiles.set_tier_for_path(settings.db_path, target)
    migrate.start_async(settings.db_path)
    audit(
        settings, "db_tier_start", to=target, rows=info["total"],
        peer=_peer(request), token_fp=_token_fp(request.app.state.console_token),
    )
    return _db_page(
        request,
        message=f"已开始迁移到「{_tier_label(target)}」，共 {info['total']} 条记忆。",
    )


@router.post("/console/db/tier/stop")
def db_tier_stop(request: Request, csrf: str = Form("")) -> HTMLResponse:
    _ensure_csrf(request, csrf)
    settings: Settings = request.app.state.settings
    migrate.request_stop()
    audit(settings, "db_tier_stop", peer=_peer(request),
          token_fp=_token_fp(request.app.state.console_token))
    return _db_page(request, message="已请求暂停：当前这一批做完就停，随时可以继续。")


@router.post("/console/db/tier/continue")
def db_tier_continue(request: Request, csrf: str = Form("")) -> HTMLResponse:
    _ensure_csrf(request, csrf)
    settings: Settings = request.app.state.settings
    result = migrate.start_async(settings.db_path)
    if not result.get("started"):
        return _db_page(request, error=result.get("reason") or "启动失败")
    audit(settings, "db_tier_continue", peer=_peer(request),
          token_fp=_token_fp(request.app.state.console_token))
    return _db_page(request, message="已继续迁移。")


@router.post("/console/db/tier/cancel")
def db_tier_cancel(request: Request, csrf: str = Form("")) -> HTMLResponse:
    _ensure_csrf(request, csrf)
    settings: Settings = request.app.state.settings
    migrate.request_stop()
    try:
        conn = store.open_conn(settings)
        try:
            migrate.cancel(conn)
        finally:
            conn.close()
        # 取消失败不回滚档位：档位是"下次启动用哪个词典"的意向，迁移没做完也不会生效
    except Exception as exc:  # noqa: BLE001
        return _db_page(request, error=f"取消失败：{exc}")
    audit(settings, "db_tier_cancel", peer=_peer(request),
          token_fp=_token_fp(request.app.state.console_token))
    return _db_page(request, message="已放弃迁移，影子列清空，记忆分词保持原样。")


@router.post("/console/db/retokenize")
def db_retokenize(request: Request, csrf: str = Form("")) -> HTMLResponse:
    """词表变化后按当前档位重算 tokens（走同一套影子列通道）。"""
    _ensure_csrf(request, csrf)
    settings: Settings = request.app.state.settings
    if migrate.job_running():
        return _db_page(request, error="已有任务在运行，等它结束")
    target = profiles.entry_tier() or "full"
    try:
        conn = store.open_conn(settings)
        try:
            info = migrate.start(conn, target, from_tier=target)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        return _db_page(request, error=f"重算启动失败：{exc}")
    migrate.start_async(settings.db_path)
    audit(settings, "db_retokenize", tier=target, rows=info["total"],
          peer=_peer(request), token_fp=_token_fp(request.app.state.console_token))
    return _db_page(request, message=f"已开始重算分词（{info['total']} 条），完成后重启侧车生效。")

# ---------------------------------------------------------------------------
# 词典 / 黑话自动学习
# ---------------------------------------------------------------------------

_SCAN_LOCK = threading.Lock()
_SCAN_STATE: dict = {"running": False, "error": ""}


def _scan_worker(db_path, apply: bool) -> None:
    import slangscan

    conn = None
    try:
        conn = store.open_conn(Settings(db_path=Path(db_path)))
        slangscan.scan(conn, apply=apply)
        _SCAN_STATE["error"] = ""
    except Exception as exc:  # noqa: BLE001 - 后台线程
        _SCAN_STATE["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if conn is not None:
            conn.close()
        _SCAN_STATE["running"] = False


def _start_scan(settings: Settings, apply: bool) -> bool:
    with _SCAN_LOCK:
        if _SCAN_STATE["running"]:
            return False
        _SCAN_STATE["running"] = True
        _SCAN_STATE["error"] = ""
    threading.Thread(target=_scan_worker, args=(settings.db_path, apply),
                     name="slang-scan", daemon=True).start()
    return True


def _dict_page(request: Request, message: str = "", error: str = "") -> HTMLResponse:
    import slangscan
    import tuning

    settings: Settings = request.app.state.settings
    cfg = tuning.slang()
    snap = slang.status()
    csrf = csrf_for(request.app.state.console_token, _session_exp(request))

    conn = store.open_conn(settings)
    try:
        migrate.ensure_schema(conn)
        last = slangscan.last_scan(conn)
    finally:
        conn.close()

    banners = []
    if message:
        banners.append(f'<p class="ok">{html.escape(message)}</p>')
    if error:
        banners.append(f'<p class="bad">{html.escape(error)}</p>')
    if _SCAN_STATE.get("error"):
        banners.append(f'<p class="bad">上次扫描出错：{html.escape(str(_SCAN_STATE["error"]))}</p>')
    if _SCAN_STATE.get("running"):
        banners.append('<p class="warn">正在扫描，几秒后刷新本页看结果。</p>')

    def num(name: str, label: str) -> str:
        return (f'<label>{label} <input name="{name}" type="number" value="{cfg[name]}" '
                f'style="width:70px"></label>')

    settings_form = (
        '<form method="post" action="/console/dict/settings">'
        f'<input type="hidden" name="csrf" value="{csrf}">'
        f'<p><label><input type="checkbox" name="enabled" value="1"'
        f'{" checked" if cfg["enabled"] else ""}> 启用自动学习</label>　'
        f'<label><input type="checkbox" name="auto_accept" value="1"'
        f'{" checked" if cfg["auto_accept"] else ""}> 通过校验直接收词（不勾就只出候选）</label></p>'
        f'<p>{num("min_count", "出现次数≥")} {num("min_speakers", "说话人数≥")} '
        f'{num("min_days", "时间跨度(天)≥")} {num("max_word_len", "词长上限")} '
        f'{num("max_entries", "词表上限")}</p>'
        f'<p>扫描方式：<label><input type="radio" name="scan_mode" value="manual"'
        f'{" checked" if cfg["scan_mode"] == "manual" else ""}> 手动</label>　'
        f'<label><input type="radio" name="scan_mode" value="daily"'
        f'{" checked" if cfg["scan_mode"] == "daily" else ""}> 每日定时</label> '
        f'<label>在 <input name="scan_hour" type="number" value="{cfg["scan_hour"]}" '
        f'style="width:55px"> 点跑</label></p>'
        '<p><button type="submit">保存设置</button></p></form>'
    )

    scan_btn = (
        '<form class="inline" method="post" action="/console/dict/scan">'
        f'<input type="hidden" name="csrf" value="{csrf}">'
        '<button type="submit"' + ('' if cfg["enabled"] else ' disabled') + '>立即扫描</button></form>'
        + ('' if cfg["enabled"] else '<span class="muted">　先勾上「启用自动学习」</span>')
    )

    if last:
        accepted = last.get("accepted") or []
        rejected = last.get("rejected") or []
        acc_rows = "".join(
            f"<tr><td>{html.escape(str(a.get('word')))}</td><td>{a.get('count')}</td>"
            f"<td>{a.get('speakers')}</td><td>{a.get('days')}</td>"
            f"<td>{'已收词' if a.get('applied') else '仅候选'}</td></tr>"
            for a in accepted
        ) or '<tr><td colspan="5" class="muted">没有通过校验的候选</td></tr>'
        last_html = (
            f'<p class="muted">上次扫描 {html.escape(str(last.get("started_at")))}　'
            f'看了 {last.get("turns")} 条发言、{last.get("candidates")} 个候选，'
            f'耗时 {last.get("seconds")} 秒</p>'
            '<table><tr><th>词</th><th>出现</th><th>人数</th><th>天数</th><th>状态</th></tr>'
            + acc_rows + "</table>"
            + (f'<p class="muted">被拒绝的（前 {len(rejected)} 条）：'
               + "、".join(html.escape(str(r.get("word"))) + "（" + html.escape(str(r.get("reason"))) + "）"
                          for r in rejected[:12]) + "</p>" if rejected else "")
        )
    else:
        last_html = '<p class="muted">还没扫描过。</p>'

    user_rows = "".join(
        f'<tr><td>{html.escape(w)}</td><td>'
        f'<form class="inline" method="post" action="/console/dict/delete">'
        f'<input type="hidden" name="csrf" value="{csrf}">'
        f'<input type="hidden" name="word" value="{html.escape(w)}">'
        '<button type="submit">删除</button></form></td></tr>'
        for w in slang.user_words()
    ) or '<tr><td colspan="2" class="muted">本地还没有词</td></tr>'

    body = (
        '<div class="card"><h2>黑话自动学习</h2>'
        + "".join(banners)
        + '<p class="muted">只在 <code>conversation_turns</code> 的 user 发言上挖词；'
          "候选要过频次、人数、天数、最大短语、子串冲突、dry-run 六道关，"
          "任何一步会把已有词切碎就拒绝。</p>"
        + settings_form + "<p>" + scan_btn + "</p>"
        + "</div>"
        + '<div class="card"><h2>上次扫描</h2>' + last_html + "</div>"
        + f'<div class="card"><h2>词表</h2><p class="muted">内置 {snap["builtin_count"]} 条，'
          f'本地 {snap["user_count"]} 条，指纹 <code>{snap["fingerprint"]}</code></p>'
        + '<form class="inline" method="post" action="/console/dict/add">'
        + f'<input type="hidden" name="csrf" value="{csrf}">'
        + '<input name="word" placeholder="加一个词" size="16"><button type="submit">添加</button></form>'
        + "<table><tr><th>本地词</th><th>操作</th></tr>" + user_rows + "</table>"
        + '<p class="muted">加词或删词后要重算分词才会影响检索：'
          '去 <a href="/console/db">数据库</a> 页点「重算分词」。</p></div>'
    )
    return _shell("词典", body, refresh=5 if _SCAN_STATE.get("running") else 0)


@router.get("/console/dict")
def dict_page(request: Request) -> HTMLResponse:
    return _dict_page(request)


@router.post("/console/dict/settings")
async def dict_settings(request: Request) -> HTMLResponse:
    import tuning

    form = await request.form()
    _ensure_csrf(request, str(form.get("csrf") or ""))
    patch = {
        "enabled": form.get("enabled") == "1",
        "auto_accept": form.get("auto_accept") == "1",
        "min_count": form.get("min_count"),
        "min_speakers": form.get("min_speakers"),
        "min_days": form.get("min_days"),
        "max_word_len": form.get("max_word_len"),
        "max_entries": form.get("max_entries"),
        "scan_mode": form.get("scan_mode") or "manual",
        "scan_hour": form.get("scan_hour"),
    }
    try:
        tuning.update_slang(patch)
    except ValueError as exc:
        return _dict_page(request, error=str(exc))
    audit(request.app.state.settings, "dict_settings", mode=patch["scan_mode"],
          enabled=patch["enabled"], peer=_peer(request),
          token_fp=_token_fp(request.app.state.console_token))
    return _dict_page(request, message="设置已保存。")


@router.post("/console/dict/scan")
async def dict_scan(request: Request) -> HTMLResponse:
    import tuning

    form = await request.form()
    _ensure_csrf(request, str(form.get("csrf") or ""))
    cfg = tuning.slang()
    if not cfg.get("enabled"):
        return _dict_page(request, error="自动学习没启用，先勾上再扫描。")
    started = _start_scan(request.app.state.settings, bool(cfg.get("auto_accept", True)))
    if not started:
        return _dict_page(request, error="已经有扫描在跑。")
    audit(request.app.state.settings, "dict_scan", peer=_peer(request),
          token_fp=_token_fp(request.app.state.console_token))
    return _dict_page(request, message="已开始扫描。")


@router.post("/console/dict/add")
async def dict_add(request: Request) -> HTMLResponse:
    form = await request.form()
    _ensure_csrf(request, str(form.get("csrf") or ""))
    try:
        result = slang.add_word(str(form.get("word") or ""))
    except ValueError as exc:
        return _dict_page(request, error=str(exc))
    if not result.get("added"):
        return _dict_page(request, message=f"没加：{result.get('reason')}")
    audit(request.app.state.settings, "dict_add", word=result["word"], peer=_peer(request),
          token_fp=_token_fp(request.app.state.console_token))
    return _dict_page(request, message=f"已加入「{result['word']}」，记得重算分词。")


@router.post("/console/dict/delete")
async def dict_delete(request: Request) -> HTMLResponse:
    form = await request.form()
    _ensure_csrf(request, str(form.get("csrf") or ""))
    word = str(form.get("word") or "")
    if slang.remove_word(word):
        audit(request.app.state.settings, "dict_delete", word=word, peer=_peer(request),
              token_fp=_token_fp(request.app.state.console_token))
        return _dict_page(request, message=f"已删除「{word}」。")
    return _dict_page(request, error="没找到这个词（内置词不能删）。")


@router.post("/console/db/create")
def db_create(
    request: Request,
    name: str = Form(""),
    path: str = Form(""),
    csrf: str = Form(""),
) -> HTMLResponse:
    """新建一个库：建目录 → 初始化 → 登记。"""
    _ensure_csrf(request, csrf)
    settings: Settings = request.app.state.settings
    try:
        target = profiles.normalize_path(path)
    except ValueError as exc:
        return _db_page(request, error=str(exc))
    if target.exists() and target.stat().st_size > 0:
        return _db_page(request, error=f"这个位置已经有文件了：{target}，换一个路径或直接登记它。")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _db_page(request, error=f"建目录失败：{exc}")
    ok, why = db_init.check_ready(target)
    if not ok:
        return _db_page(request, error=f"建库失败：{why}")
    try:
        entry = profiles.register(name, str(target))
    except ValueError as exc:
        return _db_page(request, error=str(exc))
    audit(settings, "db_create", name=entry["name"], path=entry["path"],
          peer=_peer(request), token_fp=_token_fp(request.app.state.console_token))
    return _db_page(
        request,
        message=f"已新建并登记「{entry['name']}」→ {entry['path']}，切过去并重启侧车就能用了。",
    )


# ---------------------------------------------------------------------------
# 环境自检
# ---------------------------------------------------------------------------

def _probe_write(folder: Path) -> dict:
    """在一个目录里试着写一个临时文件，返回结果与错误码。"""
    result = {"path": str(folder), "exists": folder.exists(), "ok": False, "error": "", "winerror": None}
    if not result["exists"]:
        result["error"] = "目录不存在"
        return result
    probe = folder / f".qqm-probe-{os.getpid()}.tmp"
    try:
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        result["ok"] = True
    except OSError as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["winerror"] = getattr(exc, "winerror", None)
    return result


def _env_page(request: Request) -> HTMLResponse:
    import getpass
    import platform
    import tempfile

    settings: Settings = request.app.state.settings
    probes = [
        ("临时目录", Path(tempfile.gettempdir())),
        ("项目 data 目录", Path(__file__).resolve().parent / "data"),
        ("当前库所在目录", Path(settings.db_path).parent),
    ]
    rows = []
    for label, folder in probes:
        info = _probe_write(folder)
        if info["ok"]:
            state = '<span class="ok">可写</span>'
            detail = ""
        elif not info["exists"]:
            state = '<span class="bad">目录不存在</span>'
            detail = html.escape(info["path"])
        else:
            state = '<span class="bad">写不进去</span>'
            detail = html.escape(info["error"])
            if info["winerror"] == 5:
                detail += '<br><span class="muted">WinError 5 = 拒绝访问：多半是杀毒软件/Windows「受控文件夹访问」挡的</span>'
        rows.append(
            f"<tr><td>{html.escape(label)}</td><td><code>{html.escape(info['path'])}</code></td>"
            f"<td>{state}</td><td>{detail}</td></tr>"
        )

    env_rows = [
        ("进程用户", getpass.getuser()),
        ("Python", sys.version.split()[0]),
        ("解释器", sys.executable),
        ("工作目录", os.getcwd()),
        ("操作系统", platform.platform()),
        ("当前库", str(settings.db_path)),
    ]
    env_html = "".join(
        f"<tr><td>{html.escape(k)}</td><td><code>{html.escape(str(v))}</code></td></tr>"
        for k, v in env_rows
    )

    body = (
        '<div class="card"><h2>目录可写性</h2>'
        '<p class="muted">侧车在运行期要往这些目录写文件：建库、写 -wal、写控制台令牌、写备份。'
        "任何一个失败都会导致登记/新建/启动出问题。</p>"
        '<table><tr><th>用途</th><th>路径</th><th>结果</th><th>说明</th></tr>'
        + "".join(rows) + "</table></div>"
        + '<div class="card"><h2>运行环境</h2>'
        '<table><tr><th>项</th><th>值</th></tr>' + env_html + "</table></div>"
        + '<div class="card"><h2>写不进去怎么办</h2>'
        "<ol>"
        "<li><b>Windows 安全中心 → 病毒和威胁防护 → 管理勒索软件防护（受控文件夹访问）</b>："
        "如果开着，把 <code>python.exe</code> 加进允许列表，或者干脆关掉它</li>"
        "<li><b>第三方杀毒/安全卫士</b>（360、火绒、电脑管家）的「文档保护 / 文件夹保护」："
        "把项目目录加白名单</li>"
        "<li><b>换目录</b>：不要把库放在「下载」「桌面」「文档」这类被重点保护的目录，"
        "换成 <code>D:\\qq-memory\\</code> 这种普通目录</li>"
        "<li><b>以管理员身份运行</b>：如果侧车是用低权限账户启动的，换管理员身份跑一次试试</li>"
        "</ol></div>"
    )
    return _shell("环境自检", boot_banner(request) + body)


@router.get("/console/env")
def env_page(request: Request) -> HTMLResponse:
    return _env_page(request)


def _db_files(path: Path) -> list[Path]:
    return [p for p in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")) if p.exists()]


@router.post("/console/db/forget")
def db_forget(request: Request, name: str = Form(""), csrf: str = Form("")) -> HTMLResponse:
    """只从档案里移除，不动磁盘上的文件。"""
    _ensure_csrf(request, csrf)
    settings: Settings = request.app.state.settings
    try:
        entry = profiles.remove(name)
    except ValueError as exc:
        return _db_page(request, error=str(exc))
    audit(settings, "db_forget", name=entry["name"], path=entry["path"],
          peer=_peer(request), token_fp=_token_fp(request.app.state.console_token))
    return _db_page(request, message=f"已从档案里移除「{entry['name']}」，文件还在：{entry['path']}")


@router.post("/console/db/delete")
def db_delete(
    request: Request,
    name: str = Form(""),
    confirm: str = Form(""),
    csrf: str = Form(""),
) -> HTMLResponse:
    """删除库文件（先备份一份），并移除档案。正在用的库不允许删。"""
    _ensure_csrf(request, csrf)
    settings: Settings = request.app.state.settings
    if confirm.strip().lower() != "yes":
        return _db_page(request, error="要删文件必须先确认")
    state = profiles.load()
    entry = next((e for e in state["entries"] if e["name"] == name.strip()), None)
    if entry is None:
        return _db_page(request, error=f"没有这个档案：{name.strip()}")
    target = Path(entry["path"])
    current_name = state.get("current")
    if target.resolve() == Path(settings.db_path).resolve() or name.strip() == current_name:
        return _db_page(
            request,
            error="这是正在使用（或当前选中）的库，删不了。先切换到别的库、重启侧车，再回来删它。",
        )
    # 不"复制再删除"，直接把文件挪进 backup 目录：挪不动就不算删过，随时能拿回来。
    # 必须连 -wal / -shm 一起挪：只挪主文件会丢掉还在 WAL 里没 checkpoint 的数据。
    backup_path = ""
    if target.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        try:
            settings.backup_dir.mkdir(parents=True, exist_ok=True)
            dest = settings.backup_dir / f"{target.stem}-{stamp}.db"
            shutil.move(str(target), str(dest))
            backup_path = str(dest)
            for suffix in ("-wal", "-shm"):
                side = Path(str(target) + suffix)
                if side.exists():
                    shutil.move(str(side), str(settings.backup_dir / (dest.name + suffix)))
        except OSError as exc:
            return _db_page(request, error=f"没能把库挪到备份目录，操作已取消：{exc}")
    removed = []
    for path in _db_files(target):
        try:
            path.unlink()
            removed.append(path.name)
        except OSError:
            pass
    try:
        profiles.remove(name)
    except ValueError as exc:
        return _db_page(request, error=str(exc))
    audit(settings, "db_delete", name=entry["name"], path=entry["path"],
          removed=removed, backup=backup_path,
          peer=_peer(request), token_fp=_token_fp(request.app.state.console_token))
    return _db_page(
        request,
        message=f"已删除「{entry['name']}」（{', '.join(removed) or '文件本就不存在'}）"
        + (f"，备份在 {backup_path}" if backup_path else ""),
    )
