# -*- coding: utf-8 -*-
"""pytest 共享 fixture：临时库 + 确定性假嵌入器（不依赖模型下载）。"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db_init  # noqa: E402
import store  # noqa: E402
from config import Settings  # noqa: E402
from retrieval import Retriever  # noqa: E402
from timeutil import iso_now  # noqa: E402
from writer import MemoryWriter  # noqa: E402

DIM = 512
ACCOUNT = "10001"
PRIVATE = {"type": "private", "user_id": "20002", "group_id": ""}
GROUP = {"type": "group", "user_id": "20002", "group_id": "30003"}


class FakeEmbedder:
    """同文本 → 同一归一化向量；不同文本近似正交。"""

    dim = DIM
    degraded = False
    ready = True

    def _vec(self, text: str) -> np.ndarray:
        vec = np.zeros(DIM, dtype=np.float32)
        for index, char in enumerate(str(text)):
            digest = hashlib.blake2b(char.encode("utf-8"), digest_size=2).digest()
            slot = (int.from_bytes(digest, "little") + index * 37) % DIM
            vec[slot] += 1.0
        norm = float(np.linalg.norm(vec))
        return vec / norm if norm else vec

    def embed_query(self, text: str) -> np.ndarray:
        return self._vec(text)

    def embed_passage(self, text: str) -> np.ndarray:
        return self._vec(text)

    def health(self) -> dict:
        return {"state": "ready", "model": "fake", "dim": DIM, "degraded": False}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(
        db_path=tmp_path / "qq.db",
        model_dir=tmp_path / "models",
        log_dir=tmp_path / "logs",
        backup_dir=tmp_path / "backup",
        sim_gate=0.30,
        recall_max_chars=800,
        handoff_max_chars=1500,
        handoff_recent=20,
    )
    s.ensure_dirs()
    db_init.init_db(s.db_path)
    return s


@pytest.fixture
def fake() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def writer(settings: Settings, fake: FakeEmbedder) -> MemoryWriter:
    return MemoryWriter(settings, fake)


@pytest.fixture
def retriever(settings: Settings, fake: FakeEmbedder) -> Retriever:
    return Retriever(settings, fake)


@pytest.fixture
def seed_filler(settings: Settings):
    """灌一些与查询无关的记忆，让门控的 N/DF 有统计意义（单条库 info 恒为 0）。"""

    def _seed(count: int = 12) -> None:
        key = store.scope_key(ACCOUNT, "private", "20002", "")
        conn = store.open_conn(settings)
        try:
            for index in range(count):
                store.insert_memory(
                    conn,
                    {
                        "account": ACCOUNT,
                        "scope_key": key,
                        "scope_type": "private",
                        "user_id": "20002",
                        "role": "user",
                        "content": f"填充记忆第{index}条",
                        "tokens": "天气 不错",
                        "created_at": iso_now(),
                    },
                )
            conn.commit()
        finally:
            conn.close()

    return _seed


@pytest.fixture
def make_payload():
    def _make(content: str, message_id: str, *, scope=None, role: str = "user", **extra):
        body = {
            "account": ACCOUNT,
            "scope": scope or PRIVATE,
            "message_id": message_id,
            "role": role,
            "speaker": {"id": "20002", "name": "她"},
            "content": content,
            "happened_at": "2026-10-20T12:00:00+08:00",
            "source": "auto",
            "meta": {"trigger": "wake"},
        }
        body.update(extra)
        return body

    return _make
