# -*- coding: utf-8 -*-
"""数据库档案（profiles）：登记、选择、优先级、统计。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import db_init
import profiles


@pytest.fixture()
def iso_profiles(tmp_path, monkeypatch):
    """把档案文件挪到临时目录，避免动到仓库里的 data/。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(profiles, "DATA_DIR", data_dir)
    monkeypatch.setattr(profiles, "PROFILES_PATH", data_dir / "db-profiles.json")
    monkeypatch.setattr(profiles, "DEFAULT_DB_PATH", data_dir / "qq-memory.db")
    return data_dir


def test_register_then_select(iso_profiles, tmp_path):
    work = tmp_path / "work.db"
    profiles.register("工作", str(work))
    profiles.select("工作")

    state = profiles.load()
    assert state["current"] == "工作"
    assert [e["name"] for e in state["entries"]] == ["工作"]
    assert profiles.resolve_db_path(None) == work.resolve()

    raw = json.loads(profiles.PROFILES_PATH.read_text(encoding="utf-8"))
    assert raw["current"] == "工作"
    assert raw["entries"][0]["path"] == str(work.resolve())


def test_register_same_name_overwrites(iso_profiles, tmp_path):
    profiles.register("工作", str(tmp_path / "a.db"))
    profiles.register("工作", str(tmp_path / "b.db"))
    state = profiles.load()
    assert len(state["entries"]) == 1
    assert state["entries"][0]["path"].endswith("b.db")


def test_select_unknown_name_raises(iso_profiles):
    with pytest.raises(ValueError):
        profiles.select("不存在")


def test_env_has_priority_over_profile(iso_profiles, tmp_path):
    profiles.register("工作", str(tmp_path / "work.db"))
    profiles.select("工作")
    env_db = tmp_path / "env.db"
    assert profiles.resolve_db_path(str(env_db)) == env_db.resolve()


def test_default_when_no_profile(iso_profiles):
    assert profiles.resolve_db_path(None) == profiles.DEFAULT_DB_PATH


def test_invalid_names_rejected(iso_profiles):
    for bad in ["", "   ", "a/b", "a\\b", "..", "x" * 41]:
        with pytest.raises(ValueError):
            profiles.validate_name(bad)
    assert profiles.validate_name("  工作 一号 ") == "工作 一号"


def test_directory_path_gets_db_file(iso_profiles, tmp_path):
    folder = tmp_path / "bundle"
    folder.mkdir()
    entry = profiles.register("目录", str(folder))
    assert entry["path"].endswith("qq-memory.db")


def test_describe_reports_counts(iso_profiles, tmp_path):
    fresh = tmp_path / "fresh.db"
    profiles.register("新库", str(fresh))
    profiles.register("缺席", str(tmp_path / "missing.db"))
    db_init.init_db(fresh)

    rows = {row["name"]: row for row in profiles.describe(fresh)}
    assert rows["新库"]["exists"] is True
    assert rows["新库"]["memories"] == 0
    assert rows["新库"]["turns"] == 0
    assert rows["缺席"]["exists"] is False
    assert rows["缺席"]["memories"] is None


def test_ensure_default_registers_once(iso_profiles):
    profiles.ensure_default()
    assert profiles.load()["current"] == "默认"
    profiles.ensure_default()
    assert len(profiles.load()["entries"]) == 1