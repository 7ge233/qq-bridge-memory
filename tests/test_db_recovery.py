# -*- coding: utf-8 -*-
"""库打不开时的兜底：登记/切换前先验证，启动时回退到默认库。"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import db_init


def test_check_ready_creates_new_db(tmp_path):
    target = tmp_path / "new" / "a.db"
    ok, why = db_init.check_ready(target)
    assert ok, why
    assert target.exists()


def test_check_ready_rejects_garbage(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"this is not a sqlite file")
    ok, why = db_init.check_ready(bad)
    assert ok is False
    assert "DatabaseError" in why or "not a database" in why


def test_check_ready_reports_missing_parent_silently_created(tmp_path):
    ok, _ = db_init.check_ready(tmp_path / "deep" / "deeper" / "b.db")
    assert ok


def test_startup_falls_back_to_default(tmp_path, monkeypatch):
    import app as app_mod
    import config
    from fastapi.testclient import TestClient

    bad = tmp_path / "broken.db"
    bad.write_bytes(b"garbage")
    good = tmp_path / "good.db"
    monkeypatch.setattr(config, "DEFAULT_DB_PATH", good)
    real_get = config.get_settings
    monkeypatch.setattr(
        config, "get_settings", lambda: dataclasses.replace(real_get(), db_path=bad)
    )
    monkeypatch.setattr(app_mod, "get_settings", config.get_settings)
    # 不真正接管全局日志，避免污染其它测试的 root handler
    monkeypatch.setattr(app_mod.logging_setup, "setup_logging", lambda *a, **k: None)

    with TestClient(app_mod.app) as client:
        assert app_mod.app.state.settings.db_path == good
        assert "回退" in app_mod.app.state.boot_error
        page = client.get("/console")
        assert page.status_code in (200, 401)
