# -*- coding: utf-8 -*-
"""控制台令牌位置：换库不能换令牌。"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import config
import console


def test_token_path_independent_of_db(tmp_path):
    a = dataclasses.replace(config.get_settings(), db_path=tmp_path / "a.db")
    b = dataclasses.replace(config.get_settings(), db_path=tmp_path / "sub" / "b.db")
    assert console.token_path(a) == console.token_path(b)
    assert console.token_path(a).name == "console-token"


def test_legacy_token_is_adopted(tmp_path, monkeypatch):
    fixed = tmp_path / "data" / "console-token"
    monkeypatch.setattr(console, "token_path", lambda settings: fixed)
    settings = dataclasses.replace(config.get_settings(), db_path=tmp_path / "db" / "x.db")
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    (settings.db_path.parent / "console-token").write_text("legacy-token\n", encoding="utf-8")
    monkeypatch.delenv("QQ_MEMORY_CONSOLE_TOKEN", raising=False)
    assert console.load_console_token(settings) == "legacy-token"
    assert fixed.read_text(encoding="utf-8").strip() == "legacy-token"
