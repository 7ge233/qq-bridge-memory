# -*- coding: utf-8 -*-
"""可调参数：data/tuning.json（不进仓库）。

目前只有一个 section：slang（黑话自动学习）。参数改了不需要重启，
扫描时按当下的值算；只有 enabled / scan_mode 影响定时任务是否启动。
"""

from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
TUNING_PATH = PROJECT_ROOT / "data" / "tuning.json"

DEFAULTS: dict = {
    "slang": {
        "enabled": False,       # 总开关：关着就不扫，也不收词
        "auto_accept": True,    # 通过四道校验就直接收词；False 只记录候选
        "min_count": 8,         # 出现次数
        "min_speakers": 3,      # 至少几个人说过
        "min_days": 7,          # 时间跨度（天）
        "max_entries": 2000,    # 本地词表上限
        "max_word_len": 6,      # 候选最长字数
        "scan_mode": "manual",  # manual | daily
        "scan_hour": 4,         # daily 时几点跑（0-23）
    }
}

RANGES: dict = {
    "min_count": (2, 100),
    "min_speakers": (2, 20),
    "min_days": (1, 90),
    "max_entries": (100, 10000),
    "max_word_len": (2, 12),
    "scan_hour": (0, 23),
}


def _merge(base: dict, patch: dict) -> dict:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        elif key in out:
            out[key] = value
    return out


def load() -> dict:
    try:
        raw = json.loads(TUNING_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return _merge(DEFAULTS, raw)


def save(cfg: dict) -> None:
    TUNING_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = TUNING_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(TUNING_PATH)


def slang() -> dict:
    return dict(load().get("slang") or DEFAULTS["slang"])


def update_slang(patch: dict) -> dict:
    """校验后写入；非法值直接报错，不做静默纠正。"""
    current = slang()
    clean: dict = {}
    for key, raw in (patch or {}).items():
        if key not in DEFAULTS["slang"]:
            continue
        if key in ("enabled", "auto_accept"):
            if isinstance(raw, str):
                raw = raw.strip().lower() in ("1", "true", "on", "yes")
            clean[key] = bool(raw)
        elif key == "scan_mode":
            mode = str(raw or "manual").strip().lower()
            if mode not in ("manual", "daily"):
                raise ValueError("扫描方式只能是 manual 或 daily")
            clean[key] = mode
        else:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                raise ValueError(f"{key} 必须是整数") from None
            low, high = RANGES[key]
            if not (low <= value <= high):
                raise ValueError(f"{key} 需在 {low}-{high} 之间")
            clean[key] = value
    merged = _merge(current, clean)
    cfg = load()
    cfg["slang"] = merged
    save(cfg)
    return merged
