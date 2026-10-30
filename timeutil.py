# -*- coding: utf-8 -*-
"""时间工具：ISO8601（+08:00）解析/格式化，避免各模块各写一套。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))


def parse_iso(value: str | None) -> datetime | None:
    """解析 ISO8601；无时区按 +08:00 处理；解析失败返回 None。"""
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt


def iso_now() -> str:
    return datetime.now(TZ).isoformat(timespec="milliseconds")


def days_since(value: str | None, *, now: datetime | None = None) -> float | None:
    ts = parse_iso(value)
    if ts is None:
        return None
    now = now or datetime.now(TZ)
    return max(0.0, (now - ts).total_seconds() / 86400.0)


def short_date(value: str | None) -> str:
    ts = parse_iso(value)
    return ts.astimezone(TZ).strftime("%m-%d %H:%M") if ts else ""


def date_only(value: str | None) -> str:
    ts = parse_iso(value)
    return ts.astimezone(TZ).strftime("%Y-%m-%d") if ts else ""


def hours_ago(hours: float, *, now: datetime | None = None) -> str:
    now = now or datetime.now(TZ)
    return (now - timedelta(hours=hours)).isoformat(timespec="milliseconds")


def days_ago(days: float, *, now: datetime | None = None) -> str:
    now = now or datetime.now(TZ)
    return (now - timedelta(days=days)).isoformat(timespec="milliseconds")
