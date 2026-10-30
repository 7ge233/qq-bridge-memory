# -*- coding: utf-8 -*-
"""相对时间归一化（事件时效 Phase 1）。

只做“事件时间”解析，不做语义推理：
  * 以消息的 happened_at 为基准，不用入库时的 now；
  * 支持今天/今晚/明天/明晚/后天/大后天/周X/下周X/周末/月底/下月X号/绝对日期；
  * 模糊表达（过两天/改天/下次/有空）一律返回 None，宁可不解析；
  * 解析失败不抛异常，不影响写入。
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from timeutil import TZ, parse_iso

_WEEKDAY_INDEX = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
}
_REL_DAYS = {
    "大后天": 3,
    "后天": 2,
    "明天": 1,
    "明晚": 1,
    "今天": 0,
    "今晚": 0,
    "昨天": -1,
    "前天": -2,
    "明早": 1,
    "今早": 0,
}
_DEFAULT_HOUR = {"今晚": 18, "明晚": 18, "今早": 8, "明早": 8}

_RE_ISO_DATE = re.compile(r"(?<!\d)(20\d{2})[-/](\d{1,2})[-/](\d{1,2})(?!\d)")
_RE_MONTH_DAY = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_RE_PREFIX_DAY = re.compile(r"(下个月|下月|这个月|本月)\s*(\d{1,2})\s*[日号]")
_RE_WEEKEND = re.compile(r"(下周末|这周末|本周末|周末)")
_RE_WEEKDAY = re.compile(
    r"(?:(这周|本周|下周|上周)\s*(?:周|星期|礼拜)?|(?:周|星期|礼拜))\s*([一二三四五六日天])"
)
_RE_REL_DAY = re.compile(r"(大后天|后天|明天|明晚|今天|今晚|昨天|前天|明早|今早)")
_RE_MONTH_EDGE = re.compile(r"(下个月|下月|这个月|本月)?\s*(月底|月末|月初)")
_RE_NUM_MONTH_EDGE = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(底|末|初)")
_RE_TIME_POINT = re.compile(
    r"(上午|下午|晚上|中午|凌晨|早上|今晚|明晚|今早|明早)?\s*"
    r"(\d{1,2}|[一二三四五六七八九十]{1,3})\s*[点时]"
    r"(半|[0-5]?\d分?|整|钟|刻)?"
)
_RE_COLON_TIME = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)")


@dataclass(frozen=True)
class EventTime:
    event_at: str
    valid_until: str
    precision: str  # day | datetime | range
    matched: str


def _iso(dt: datetime) -> str:
    return dt.astimezone(TZ).isoformat(timespec="seconds")


def _end_of_day(day: date) -> datetime:
    return datetime.combine(day, time(23, 59, 59), tzinfo=TZ)


def _cn_number(raw: str) -> int | None:
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if raw == "十":
        return 10
    if "十" in raw:
        left, _, right = raw.partition("十")
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    if len(raw) == 1 and raw in digits:
        return digits[raw]
    return None


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    total = year * 12 + (month - 1) + delta
    return total // 12, total % 12 + 1


def _month_day(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        # 例：2 月 30 日 → 不猜，直接放弃
        return None


def _parse_time(text: str) -> tuple[int, int, str] | None:
    m = _RE_COLON_TIME.search(text)
    if m:
        return int(m.group(1)), int(m.group(2)), m.group(0)
    m = _RE_TIME_POINT.search(text)
    if not m:
        return None
    prefix = m.group(1) or ""
    raw_hour = m.group(2)
    suffix = m.group(3) or ""
    hour = _cn_number(raw_hour)
    if hour is None or hour > 23:
        return None
    # “一点事/早一点/快一点/差一点/两点意见/三点水”都可能是非时间语义。
    # 只有“时段前缀”或“明确时间后缀（半/整/钟/刻/分）”才解析钟点；
    # 否则只保留日期精度（00:00），宁可少解析，不可把“一点小事”当 01:00。
    if not prefix and not suffix:
        return None
    minute = 0
    if suffix == "半":
        minute = 30
    elif suffix in ("整", "钟"):
        minute = 0
    elif suffix == "刻":
        minute = 15
    elif suffix:
        minute = _cn_number(suffix.rstrip("分")) or 0
    if minute > 59:
        return None
    if prefix in ("下午", "晚上") and hour < 12:
        hour += 12
    elif prefix == "中午" and hour < 11:
        hour = 12
    elif prefix == "凌晨" and hour == 12:
        hour = 0
    elif prefix in ("今晚", "明晚") and hour < 12:
        hour += 12
    return hour, minute, m.group(0)


def _parse_date(text: str, base_day: date) -> tuple[date, date, str, str] | None:
    """返回 (开始日, 结束日, 命中文本, 精度)。"""
    m = _RE_ISO_DATE.search(text)
    if m:
        day = _month_day(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return (day, day, m.group(0), "day") if day else None

    m = _RE_MONTH_DAY.search(text)
    if m:
        day = _month_day(base_day.year, int(m.group(1)), int(m.group(2)))
        return (day, day, m.group(0), "day") if day else None

    m = _RE_PREFIX_DAY.search(text)
    if m:
        delta = 1 if "下" in m.group(1) else 0
        year, month = _add_months(base_day.year, base_day.month, delta)
        day = _month_day(year, month, int(m.group(2)))
        return (day, day, m.group(0), "day") if day else None

    m = _RE_WEEKEND.search(text)
    if m:
        monday = base_day - timedelta(days=base_day.weekday())
        saturday = monday + timedelta(days=5)
        if m.group(1) == "下周末":
            saturday += timedelta(days=7)
        return saturday, saturday + timedelta(days=1), m.group(0), "range"

    m = _RE_WEEKDAY.search(text)
    if m:
        monday = base_day - timedelta(days=base_day.weekday())
        offset = _WEEKDAY_INDEX[m.group(2)]
        prefix = m.group(1) or ""
        if prefix == "下周":
            offset += 7
        elif prefix == "上周":
            offset -= 7
        day = monday + timedelta(days=offset)
        return day, day, m.group(0), "day"

    m = _RE_REL_DAY.search(text)
    if m:
        day = base_day + timedelta(days=_REL_DAYS[m.group(1)])
        return day, day, m.group(0), "day"

    m = _RE_NUM_MONTH_EDGE.search(text)
    if m:
        month = int(m.group(1))
        if not 1 <= month <= 12:
            return None
        if m.group(2) in ("底", "末"):
            day_num = calendar.monthrange(base_day.year, month)[1]
        else:
            day_num = 1
        day = date(base_day.year, month, day_num)
        return day, day, m.group(0), "day"

    m = _RE_MONTH_EDGE.search(text)
    if m:
        delta = 1 if m.group(1) and "下" in m.group(1) else 0
        year, month = _add_months(base_day.year, base_day.month, delta)
        if "底" in m.group(2) or "末" in m.group(2):
            day_num = calendar.monthrange(year, month)[1]
        else:
            day_num = 1
        day = date(year, month, day_num)
        return day, day, m.group(0), "day"

    return None


def parse_event_time(text: str, base: datetime | str | None = None) -> EventTime | None:
    """把“明天/下周三/周末…”解析成绝对时间；解析不了返回 None。"""
    clean = str(text or "").strip()
    if not clean:
        return None
    if isinstance(base, str):
        base = parse_iso(base)
    if not isinstance(base, datetime):
        return None
    if base.tzinfo is None:
        base = base.replace(tzinfo=TZ)
    base_day = base.astimezone(TZ).date()

    parsed_date = _parse_date(clean, base_day)
    parsed_time = _parse_time(clean)

    if parsed_date is None and parsed_time is None:
        return None

    if parsed_date is None:
        # 只有时间没有日期：按当天算（保守，事件文本里很常见）
        start_day = end_day = base_day
        precision = "datetime"
        date_matched = ""
    else:
        start_day, end_day, date_matched, date_precision = parsed_date
        precision = date_precision

    default_hour = None
    if parsed_date is not None:
        for word, hour in _DEFAULT_HOUR.items():
            if word in clean and word in date_matched:
                default_hour = hour
                break

    if parsed_time is not None:
        hour, minute, time_matched = parsed_time
        event_dt = datetime.combine(start_day, time(hour, minute), tzinfo=TZ)
        precision = "datetime"
        matched = f"{date_matched} {time_matched}".strip()
    else:
        hour = default_hour if default_hour is not None else 0
        event_dt = datetime.combine(start_day, time(hour, 0), tzinfo=TZ)
        matched = date_matched

    return EventTime(
        event_at=_iso(event_dt),
        valid_until=_iso(_end_of_day(end_day)),
        precision=precision,
        matched=matched,
    )
