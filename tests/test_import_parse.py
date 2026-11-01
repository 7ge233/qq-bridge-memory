# -*- coding: utf-8 -*-
"""批量导入的解析逻辑：数组 / items / JSONL / 坏输入。"""

from __future__ import annotations

import json

import pytest

from console import _parse_import_payload

REC = {"account": "1", "scope": {"type": "private", "user_id": "2"}, "content": "x"}


def test_array():
    items, problems = _parse_import_payload(json.dumps([REC, REC]))
    assert len(items) == 2
    assert problems == []


def test_items_wrapper():
    items, _ = _parse_import_payload(json.dumps({"items": [REC]}))
    assert items == [REC]


def test_jsonl():
    text = "\n".join(json.dumps(dict(REC, content=f"x{i}"), ensure_ascii=False) for i in range(3))
    items, problems = _parse_import_payload(text)
    assert [i["content"] for i in items] == ["x0", "x1", "x2"]
    assert problems == []


def test_jsonl_with_bad_line_reports_line():
    text = json.dumps(REC, ensure_ascii=False) + "\n{坏行\n" + json.dumps(REC, ensure_ascii=False)
    items, problems = _parse_import_payload(text)
    assert len(items) == 2
    assert len(problems) == 1
    assert "第 2 行" in problems[0]["error"]


def test_empty_raises():
    with pytest.raises(ValueError):
        _parse_import_payload("   ")


def test_non_list_raises():
    with pytest.raises(ValueError):
        _parse_import_payload("123")
