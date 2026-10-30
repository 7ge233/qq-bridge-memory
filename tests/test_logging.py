# -*- coding: utf-8 -*-
"""7.2：结构化 JSONL 日志落盘、字段可解析。"""

from __future__ import annotations

import json

from logging_setup import log_event, setup_logging


def test_jsonl_log_fields(settings):
    setup_logging(settings)
    log_event("demo", endpoint="/v1/test", latency_ms=3, items=2)
    files = sorted(settings.log_dir.glob("qq-memory-*.log"))
    assert files, "未生成日志文件"
    lines = files[-1].read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[-1])
    assert record["endpoint"] == "/v1/test"
    assert record["latency_ms"] == 3
    assert record["items"] == 2
    assert "ts" in record and "level" in record
