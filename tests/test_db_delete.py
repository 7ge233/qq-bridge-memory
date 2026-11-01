# -*- coding: utf-8 -*-
"""删除库：不能删正在用的，删的时候是"挪进备份"而不是真删。"""

from __future__ import annotations

import dataclasses
import json

import config
import console
import profiles


def test_delete_refuses_current_profile(tmp_path, monkeypatch):
    entries = {"current": "当前库", "entries": [
        {"name": "当前库", "path": str(tmp_path / "a.db")},
        {"name": "别的", "path": str(tmp_path / "b.db")},
    ]}
    prof = tmp_path / "db-profiles.json"
    prof.write_text(json.dumps(entries), encoding="utf-8")
    monkeypatch.setattr(profiles, "PROFILES_PATH", prof)
    # 当前档案即使不是正在跑的库，也不允许删
    assert profiles.load()["current"] == "当前库"
    with __import__("pytest").raises(ValueError):
        profiles.remove("不存在的库")


def test_remove_entry_keeps_file(tmp_path, monkeypatch):
    target = tmp_path / "b.db"
    target.write_bytes(b"x")
    prof = tmp_path / "db-profiles.json"
    prof.write_text(json.dumps({"current": "别的", "entries": [
        {"name": "别的", "path": str(tmp_path / "a.db")},
        {"name": "要移除", "path": str(target)},
    ]}), encoding="utf-8")
    monkeypatch.setattr(profiles, "PROFILES_PATH", prof)
    entry = profiles.remove("要移除")
    assert entry["name"] == "要移除"
    assert target.exists()
    assert [e["name"] for e in profiles.load()["entries"]] == ["别的"]


def test_remove_current_falls_back_to_other_entry(tmp_path, monkeypatch):
    prof = tmp_path / "db-profiles.json"
    prof.write_text(json.dumps({"current": "A", "entries": [
        {"name": "A", "path": str(tmp_path / "a.db")},
        {"name": "B", "path": str(tmp_path / "b.db")},
    ]}), encoding="utf-8")
    monkeypatch.setattr(profiles, "PROFILES_PATH", prof)
    profiles.remove("A")
    assert profiles.load()["current"] == "B"
