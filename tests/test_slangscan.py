# -*- coding: utf-8 -*-
"""L3 黑话自动学习：挖掘、校验、定时判定、参数校验。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import db_init
import migrate
import slang
import slangscan
import tuning

TZ = timezone(timedelta(hours=8))


def make_conn(tmp_path):
    db = tmp_path / "scan.db"
    db_init.init_db(db)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def seed(conn, samples, *, n=20, speakers=3, hours=20):
    now = datetime.now(TZ)
    for i in range(n):
        conn.execute(
            "INSERT INTO conversation_turns(account, scope_key, scope_type, seq, role,"
            " speaker_id, content, happened_at) VALUES('1','g:1','group',0,'user',?,?,?)",
            (f"u{i % speakers}", f"{samples[i % len(samples)]} 第{i}条",
             (now - timedelta(hours=i % hours)).strftime("%Y-%m-%dT%H:%M:%S+08:00")),
        )
    conn.commit()


def cfg(**over):
    base = dict(tuning.DEFAULTS["slang"])
    base.update({"min_count": 4, "min_speakers": 2, "min_days": 1, "max_word_len": 4})
    base.update(over)
    return base


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(slang, "USER_PATH", tmp_path / "userdict.txt")
    monkeypatch.setattr(tuning, "TUNING_PATH", tmp_path / "tuning.json")
    return tmp_path


def test_finds_repeated_phrase(tmp_path, sandbox):
    conn = make_conn(tmp_path)
    seed(conn, ["我是晚八人"])
    report = slangscan.scan(conn, cfg=cfg(), apply=False)
    words = [a["word"] for a in report["accepted"]]
    assert "晚八人" in words
    assert report["turns"] == 20


def test_rejects_word_already_tokenized(tmp_path, sandbox):
    conn = make_conn(tmp_path)
    seed(conn, ["今天很开心"])
    report = slangscan.scan(conn, cfg=cfg(), apply=False)
    assert "开心" not in [a["word"] for a in report["accepted"]]


def test_rejects_substring_of_known_word(tmp_path, sandbox):
    conn = make_conn(tmp_path)
    seed(conn, ["记忆系统很好用"])
    report = slangscan.scan(conn, cfg=cfg(), apply=False)
    reasons = {r["word"]: r["reason"] for r in report["rejected"]}
    hit = [w for w in reasons if w in ("记忆系", "记忆系统")
           and ("已有词" in reasons[w] or "覆盖" in reasons[w])]
    assert hit, reasons


def test_apply_writes_to_userdict(tmp_path, sandbox):
    conn = make_conn(tmp_path)
    seed(conn, ["我是晚八人"])
    slangscan.scan(conn, cfg=cfg(), apply=True)
    assert "晚八人" in slang.user_words()


def test_report_persisted(tmp_path, sandbox):
    conn = make_conn(tmp_path)
    seed(conn, ["我是晚八人"])
    slangscan.scan(conn, cfg=cfg(), apply=False)
    saved = slangscan.last_scan(conn)
    assert saved and saved["turns"] == 20


def test_due_now_requires_daily_and_enabled(tmp_path, sandbox):
    conn = make_conn(tmp_path)
    migrate.ensure_schema(conn)
    assert slangscan.due_now(conn, cfg(enabled=False, scan_mode="daily")) is False
    assert slangscan.due_now(conn, cfg(enabled=True, scan_mode="manual")) is False


def test_due_now_true_when_not_scanned_today(tmp_path, sandbox):
    conn = make_conn(tmp_path)
    migrate.ensure_schema(conn)
    migrate.meta_set(conn, "slang.last_scan", '{"started_at": "2020-01-01T04:00:00+08:00"}')
    conn.commit()
    conf = cfg(enabled=True, scan_mode="daily", scan_hour=0)
    assert slangscan.due_now(conn, conf) is True


def test_tuning_rejects_bad_values(tmp_path, sandbox):
    with pytest.raises(ValueError):
        tuning.update_slang({"min_count": 1})
    with pytest.raises(ValueError):
        tuning.update_slang({"scan_mode": "weekly"})


def test_tuning_roundtrip(tmp_path, sandbox):
    saved = tuning.update_slang({"enabled": True, "scan_mode": "daily", "scan_hour": 4,
                                 "min_count": 12})
    assert saved["enabled"] is True and saved["min_count"] == 12
    assert tuning.slang()["scan_mode"] == "daily"
