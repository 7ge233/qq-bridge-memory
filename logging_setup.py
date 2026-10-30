# -*- coding: utf-8 -*-
"""结构化 JSONL 日志（7.2）：logs/qq-memory-YYYY-MM-DD.log，保留 7 天。

只记录元数据（端点、状态、延迟、命中数、错误码），**绝不记录记忆原文**。
排障需要原文时用 QQ_MEMORY_DEBUG=1 只提升级别，不改变"不落原文"的约束。
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from config import Settings
from timeutil import TZ, iso_now

_LOG_PREFIX = "qq-memory"
_retention_days = 7


class DailyJsonlHandler(logging.Handler):
    def __init__(self, log_dir: Path, *, retention_days: int = 7, prefix: str = _LOG_PREFIX):
        super().__init__()
        self.log_dir = Path(log_dir)
        self.retention_days = retention_days
        self.prefix = prefix
        self._day = ""
        self._fh = None

    def _ensure(self) -> None:
        day = datetime.now(TZ).strftime("%Y-%m-%d")
        if day == self._day and self._fh is not None:
            return
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._fh = (self.log_dir / f"{self.prefix}-{day}.log").open("a", encoding="utf-8")
        self._day = day
        self._prune()

    def _prune(self) -> None:
        cutoff = datetime.now(TZ) - timedelta(days=self.retention_days)
        for path in self.log_dir.glob(f"{self.prefix}-*.log"):
            try:
                stamp = datetime.strptime(path.stem[len(self.prefix) + 1:], "%Y-%m-%d").replace(tzinfo=TZ)
            except ValueError:
                continue
            if stamp < cutoff:
                try:
                    path.unlink()
                except OSError:
                    pass

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._ensure()
            payload: dict = {
                "ts": iso_now(),
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            }
            event = getattr(record, "event", None)
            if isinstance(event, dict):
                payload.update(event)
            assert self._fh is not None
            self._fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._fh.flush()
        except Exception:  # 日志失败绝不能影响主链路
            self.handleError(record)

    def close(self) -> None:
        try:
            if self._fh is not None:
                self._fh.close()
        finally:
            self._fh = None
            super().close()


def setup_logging(settings: Settings) -> None:
    logger = logging.getLogger("qq_memory")
    debug = os.environ.get("QQ_MEMORY_DEBUG", "").strip() == "1"
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.propagate = False
    if any(isinstance(handler, DailyJsonlHandler) for handler in logger.handlers):
        return
    handler = DailyJsonlHandler(settings.log_dir, retention_days=settings.log_retention_days)
    handler.setLevel(logging.DEBUG if debug else logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    stream = logging.StreamHandler()
    stream.setLevel(logging.INFO)
    stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(stream)


def log_event(name: str, **fields: object) -> None:
    """写一条结构化事件（字段进入 JSONL 顶层）。"""
    try:
        logging.getLogger("qq_memory").info(name, extra={"event": fields})
    except Exception:
        pass


def elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
