# -*- coding: utf-8 -*-
"""运行配置：路径、模型、算法常量与开关。

全部通过环境变量覆盖，默认值与设计稿一致：
  QQ_MEMORY_DB        数据库路径（默认 data/qq-memory.db；设了就忽略控制台里选的档案）
  QQ_MEMORY_MODEL_DIR 模型缓存目录（默认 <项目>/models，首次运行下载约 200MB；可改到别的盘）
  QQ_MEMORY_SIM_GATE  向量余弦硬门槛（默认 0.55，按 8.3 标定后修改）
  QQ_MEMORY_WRITE_SYNC=1 时 /v1/write 同步落库（默认 0=入队异步，测试用 1）
  QQ_MEMORY_PROFILE_CHARS 画像注入块上限（默认 400；叠加在召回块之外，合计 1200）
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import profiles

APP_VERSION = "2.6.1"

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = profiles.DEFAULT_DB_PATH
DEFAULT_MODEL_DIR = PROJECT_ROOT / "models"
DEFAULT_LOG_DIR = PROJECT_ROOT / "logs"
DEFAULT_BACKUP_DIR = PROJECT_ROOT / "backup"

MODEL_NAME = "BAAI/bge-small-zh-v1.5"
EMBEDDING_DIM = 512


def _env(name: str) -> str | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """不可变配置；测试可直接构造，不必改环境变量。"""

    db_path: Path = DEFAULT_DB_PATH
    model_dir: Path = DEFAULT_MODEL_DIR
    log_dir: Path = DEFAULT_LOG_DIR
    backup_dir: Path = DEFAULT_BACKUP_DIR

    # --- 模型 ---
    model_name: str = MODEL_NAME
    embedding_dim: int = EMBEDDING_DIM
    embed_threads: int = 2
    # 8.2 红线：模型不可用时不能阻塞。文件缺失默认直接降级；
    # 需要首次下载时显式开 QQ_MEMORY_MODEL_DOWNLOAD=1，且下载在后台线程里做。
    model_load_timeout_s: float = 20.0
    model_allow_download: bool = False

    # --- 召回（4.2）---
    sim_gate: float = 0.55
    recall_limit: int = 6
    recall_max_chars: int = 800
    # 画像注入块：整体叠加在召回块之外，不占召回额度（指令书 8.3）
    profile_max_chars: int = 400
    candidate_pool: int = 200
    # 分层抽样：把 id 区间切成 pool_strata 段，每段取最新一条进候选池。
    # 否则老记忆只有被 FTS 命中才可能进池（v2.5.1）。
    pool_strata: int = 100
    # 最终分权重（三项相加后归一到 1）
    weight_sim: float = 0.7
    weight_fresh: float = 0.2
    weight_importance: float = 0.1
    # 多样性重排（MMR）：λ 越大越偏向相关，越小越偏向互不重复
    mmr_lambda: float = 0.70
    # 刚被召回过的记忆降权（治复读）：60 分钟内最多降 15%
    recall_penalty: float = 0.15
    recall_penalty_minutes: float = 60.0
    # 日记层：日记日按 day_cut_hour 切（跨 0 点不劈开）；命中日期的小结加分，其余压低
    day_cut_hour: int = 5
    diary_boost: float = 1.35
    diary_decay: float = 0.85
    diary_max_items: int = 2
    fts_top: int = 30
    fts_max_ids: int = 60
    df_window: int = 5000
    gate_min_info: float = 0.8
    gate_min_words: int = 2
    half_life_days: float = 30.0
    fact_half_life_days: float = 180.0
    item_max_chars: int = 300
    time_hint_multiplier: float = 1.2
    recall_limit_max: int = 20

    # --- 事件时效（v2.3）---
    temporal_enabled: bool = True
    event_half_life_days: float = 7.0
    include_past_default: bool = False

    # --- 续接包（4.5）---
    handoff_max_chars: int = 1500
    handoff_recent: int = 20
    handoff_recent_min: int = 12
    handoff_recent_max: int = 40
    handoff_profile_chars: int = 150
    handoff_recent_chars: int = 900
    handoff_loops_chars: int = 150
    handoff_topic_chars: int = 100
    handoff_prefs_chars: int = 150

    # --- 写入（4.1）---
    max_content_chars: int = 4000
    queue_max: int = 1000
    dedupe_sim: float = 0.95
    dedupe_len_ratio: float = 0.2
    dedupe_window_hours: int = 24
    sampling_3min: int = 2
    sampling_10min: int = 5
    write_sync: bool = False

    # --- 生命周期 ---
    scope_cap: int = 5000
    turn_retention_days: int = 90
    soft_delete_days: int = 30
    log_retention_days: int = 7

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=profiles.resolve_db_path(_env("QQ_MEMORY_DB") or None),
            model_dir=Path(_env("QQ_MEMORY_MODEL_DIR") or DEFAULT_MODEL_DIR),
            log_dir=Path(_env("QQ_MEMORY_LOG_DIR") or DEFAULT_LOG_DIR),
            backup_dir=Path(_env("QQ_MEMORY_BACKUP_DIR") or DEFAULT_BACKUP_DIR),
            model_name=_env("QQ_MEMORY_MODEL_NAME") or MODEL_NAME,
            embedding_dim=_env_int("QQ_MEMORY_EMBED_DIM", EMBEDDING_DIM),
            embed_threads=_env_int("QQ_MEMORY_EMBED_THREADS", 2),
            model_load_timeout_s=_env_float("QQ_MEMORY_MODEL_LOAD_TIMEOUT", 20.0),
            model_allow_download=_env_bool("QQ_MEMORY_MODEL_DOWNLOAD", False),
            sim_gate=_env_float("QQ_MEMORY_SIM_GATE", 0.55),
            recall_limit=_env_int("QQ_MEMORY_RECALL_LIMIT", 6),
            recall_max_chars=_env_int("QQ_MEMORY_RECALL_CHARS", 800),
    profile_max_chars=_env_int("QQ_MEMORY_PROFILE_CHARS", 400),
            candidate_pool=_env_int("QQ_MEMORY_CANDIDATE_POOL", 200),
            pool_strata=_env_int("QQ_MEMORY_POOL_STRATA", 100),
            weight_sim=_env_float("QQ_MEMORY_W_SIM", 0.7),
            weight_fresh=_env_float("QQ_MEMORY_W_FRESH", 0.2),
            weight_importance=_env_float("QQ_MEMORY_W_IMPORTANCE", 0.1),
            mmr_lambda=_env_float("QQ_MEMORY_MMR_LAMBDA", 0.70),
            recall_penalty=_env_float("QQ_MEMORY_RECALL_PENALTY", 0.15),
            recall_penalty_minutes=_env_float("QQ_MEMORY_RECALL_PENALTY_MIN", 60.0),
            day_cut_hour=_env_int("QQ_MEMORY_DAY_CUT_HOUR", 5),
            diary_boost=_env_float("QQ_MEMORY_DIARY_BOOST", 1.35),
            diary_decay=_env_float("QQ_MEMORY_DIARY_DECAY", 0.85),
            diary_max_items=_env_int("QQ_MEMORY_DIARY_MAX", 2),
            fts_top=_env_int("QQ_MEMORY_FTS_TOP", 30),
            gate_min_info=_env_float("QQ_MEMORY_GATE_MIN_INFO", 0.8),
            gate_min_words=_env_int("QQ_MEMORY_GATE_MIN_WORDS", 2),
            half_life_days=_env_float("QQ_MEMORY_HALF_LIFE", 30.0),
            fact_half_life_days=_env_float("QQ_MEMORY_FACT_HALF_LIFE", 180.0),
            item_max_chars=_env_int("QQ_MEMORY_ITEM_CHARS", 300),
            time_hint_multiplier=_env_float("QQ_MEMORY_TIME_BOOST", 1.2),
            temporal_enabled=_env_bool("QQ_MEMORY_TEMPORAL", True),
            event_half_life_days=_env_float("QQ_MEMORY_EVENT_HALF_LIFE", 7.0),
            include_past_default=_env_bool("QQ_MEMORY_INCLUDE_PAST", False),
            handoff_max_chars=_env_int("QQ_MEMORY_HANDOFF_CHARS", 1500),
            handoff_recent=_env_int("QQ_MEMORY_HANDOFF_RECENT", 20),
            max_content_chars=_env_int("QQ_MEMORY_MAX_CONTENT", 4000),
            queue_max=_env_int("QQ_MEMORY_QUEUE_MAX", 1000),
            dedupe_sim=_env_float("QQ_MEMORY_DEDUPE_SIM", 0.95),
            write_sync=_env_bool("QQ_MEMORY_WRITE_SYNC", False),
            scope_cap=_env_int("QQ_MEMORY_SCOPE_CAP", 5000),
        )

    def ensure_dirs(self) -> None:
        """数据/日志/备份目录（均在项目内，随项目走）。"""
        for path in (self.db_path.parent, self.log_dir, self.backup_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
