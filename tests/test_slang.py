# -*- coding: utf-8 -*-
"""黑话词表：L2 内置 + L3 本地。"""

from __future__ import annotations

import pytest

import migrate
import slang
import textproc


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    builtin = tmp_path / "builtin.txt"
    builtin.write_text("# 注释\n栓Q\n破防\n显眼包\n\n栓Q\n", encoding="utf-8")
    user = tmp_path / "userdict.txt"
    monkeypatch.setattr(slang, "BUILTIN_PATH", builtin)
    monkeypatch.setattr(slang, "USER_PATH", user)
    return builtin, user


def test_builtin_words_are_clean():
    words = slang.builtin_words()
    assert len(words) > 100
    assert all(2 <= len(w) <= 12 for w in words)
    assert not any(w.startswith("#") for w in words)
    assert len(set(words)) == len(words)


def test_read_dedupes_and_skips_comments(sandbox):
    assert slang.builtin_words() == ["栓Q", "破防", "显眼包"]
    assert slang.user_words() == []


def test_fingerprint_changes_with_wordlist(sandbox):
    base = slang.fingerprint()
    assert base == slang.fingerprint()
    slang.add_word("吃白饭")
    assert slang.fingerprint() != base


def test_add_and_remove_roundtrip(sandbox):
    assert slang.add_word("吃白饭")["added"] is True
    assert "吃白饭" in slang.user_words()
    assert slang.remove_word("吃白饭") is True
    assert slang.user_words() == []
    assert slang.remove_word("不存在") is False


def test_add_duplicate_reports_reason(sandbox):
    assert slang.add_word("破防")["added"] is False
    slang.add_word("吃白饭")
    again = slang.add_word("吃白饭")
    assert again["added"] is False
    assert "已有" in again["reason"]


def test_add_rejects_bad_words(sandbox):
    with pytest.raises(ValueError):
        slang.add_word("单")
    with pytest.raises(ValueError):
        slang.add_word("带 空格")


def test_apply_makes_word_survive_tokenize(sandbox, tmp_path):
    slang.add_word("吃白饭")
    tok = migrate.build_tokenizer(None)
    tokens = textproc.tokenize_with(tok.lcut, "他说他要吃白饭")
    assert "吃白饭" in tokens


def test_status_shape(sandbox):
    snap = slang.status()
    assert snap["builtin_count"] == 3
    assert snap["total"] == 3
    assert len(snap["fingerprint"]) == 16
