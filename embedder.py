# -*- coding: utf-8 -*-
"""嵌入器：fastembed + BAAI/bge-small-zh-v1.5（512 维，归一化）。

职责与降级（对应 2.5 / 4.2）：
  * 模型懒加载；加载失败或维度不符 → degraded=True，不抛异常、不影响主链路；
  * recall 路径只读已加载模型（embed_query），不触发加载，保证 300ms 预算；
  * write 路径可以触发加载（embed_passage），失败则 embedding 置空、稍后补；
  * 向量统一 L2 归一化后以 float32 BLOB 存库，余弦 = 点积。
"""

from __future__ import annotations

import logging
import os
import threading

import numpy as np

from config import Settings, get_settings

logger = logging.getLogger("qq_memory.embedder")


def _normalize(vec: object) -> np.ndarray:
    arr = np.asarray(vec, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(arr))
    if norm > 0:
        arr = arr / norm
    return arr.astype(np.float32, copy=False)


def to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: bytes | memoryview | None) -> np.ndarray | None:
    if not blob:
        return None
    try:
        return np.frombuffer(blob, dtype=np.float32).copy()
    except (TypeError, ValueError):
        return None


class Embedder:
    """线程安全的懒加载嵌入器；一个进程一个实例。"""

    def __init__(self, settings: Settings | None = None, *, enabled: bool = True):
        self.settings = settings or get_settings()
        self._enabled = enabled
        self._model = None
        self._dim = 0
        self._state = "idle"  # idle | ready | degraded
        self._reason = ""
        self._lock = threading.Lock()

    # --- 状态 ---
    @property
    def state(self) -> str:
        return self._state

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def degraded(self) -> bool:
        return self._state == "degraded"

    @property
    def dim(self) -> int:
        return self._dim or self.settings.embedding_dim

    @property
    def ready(self) -> bool:
        return self._model is not None

    def health(self) -> dict:
        return {
            "state": self._state,
            "model": self.settings.model_name,
            "dim": self.dim,
            "ready": self.ready,
            "degraded": self.degraded,
            "reason": self._reason,
        }

    # --- 加载 ---
    def _prepare_env(self) -> None:
        # hf-mirror 不支持 Xet CAS（401）；默认关掉，少一个踩坑点。
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
        endpoint = os.environ.get("QQ_MEMORY_HF_ENDPOINT") or os.environ.get("HF_ENDPOINT")
        if endpoint:
            os.environ.setdefault("HF_ENDPOINT", endpoint)

    def _degrade(self, reason: str) -> bool:
        self._state = "degraded"
        self._reason = reason[:300]
        logger.warning("embedder 不可用，进入降级：%s", self._reason)
        return False

    def _has_model_files(self) -> bool:
        """缓存目录里已经落了 onnx 文件就认为「不用联网也能加载」。"""
        try:
            for _root, _dirs, files in os.walk(self.settings.model_dir):
                if any(name.endswith(".onnx") for name in files):
                    return True
        except OSError:
            return False
        return False

    def _load(self) -> None:
        """真正加载（可能联网）；由 warmup/后台线程调用，异常由调用方处理。"""
        self._prepare_env()
        from fastembed import TextEmbedding

        model = TextEmbedding(
            model_name=self.settings.model_name,
            cache_dir=str(self.settings.model_dir),
            threads=self.settings.embed_threads,
            lazy_load=False,
        )
        probe = _normalize(next(iter(model.embed(["维度探针"]))))
        if probe.size != self.settings.embedding_dim:
            raise RuntimeError(f"模型维度 {probe.size} 与配置 {self.settings.embedding_dim} 不一致")
        with self._lock:
            self._model = model
            self._dim = int(probe.size)
            self._state = "ready"
            self._reason = ""

    def _load_with_timeout(self, timeout_s: float) -> bool:
        box: dict[str, object] = {}

        def run() -> None:
            try:
                self._load()
                box["ok"] = True
            except Exception as exc:  # 下载失败 / ONNX 失败 / 维度不符
                box["error"] = f"{type(exc).__name__}: {exc}"[:300]

        worker = threading.Thread(target=run, name="embedder-load", daemon=True)
        worker.start()
        worker.join(timeout_s)
        if worker.is_alive():
            # 线程继续在后台跑；状态先置降级，等它自己成功会覆盖为 ready。
            return self._degrade(f"加载超过 {timeout_s:g}s（后台继续尝试）")
        if "error" in box:
            return self._degrade(str(box["error"]))
        return self.ready

    def warmup(self) -> bool:
        """启动时调用：加载模型并编码一条探针，避免首条消息吃冷启动。

        8.2 红线：任何情况下都不能把调用方（写队列 worker / 启动流程）挂死。
        - 缓存目录里没有 onnx 且没开下载 → 立刻降级，不发网络请求；
        - 开了下载 → 后台线程下，主流程不等（state=loading，写入先存空向量）。
        """
        if not self._enabled:
            self._state = "degraded"
            self._reason = "disabled"
            return False
        if self._model is not None:
            return True
        if self._state in ("degraded", "loading"):
            return self._model is not None
        if not self._has_model_files():
            if not self.settings.model_allow_download:
                return self._degrade(
                    "模型文件不存在：如需首次下载，设 QQ_MEMORY_MODEL_DOWNLOAD=1 后重启"
                )
            with self._lock:
                if self._state in ("degraded", "loading") or self._model is not None:
                    return self._model is not None
                self._state = "loading"
                self._reason = "后台下载/加载中"
            threading.Thread(target=self._load_with_timeout, args=(600.0,), name="embedder-download", daemon=True).start()
            return False
        return self._load_with_timeout(self.settings.model_load_timeout_s)

    # --- 编码 ---
    def embed_query(self, text: str) -> np.ndarray | None:
        """查询向量；模型未就绪时立即返回 None（绝不触发加载）。"""
        if self._model is None or not text:
            return None
        try:
            return _normalize(next(iter(self._model.query_embed(text))))
        except Exception as exc:
            logger.warning("query_embed 失败：%s", type(exc).__name__)
            return None

    def embed_passage(self, text: str) -> np.ndarray | None:
        """写入向量；允许触发加载，但绝不为下载/model 不可用阻塞写队列。"""
        if not text:
            return None
        if self._model is None and not self.warmup():
            return None
        try:
            return _normalize(next(iter(self._model.embed([text]))))
        except Exception as exc:
            logger.warning("passage_embed 失败：%s", type(exc).__name__)
            return None

    def embed_passages(self, texts: list[str]) -> list[np.ndarray | None]:
        return [self.embed_passage(text) for text in texts]

    def model_signature(self) -> tuple[str, int]:
        return self.settings.model_name, self.dim
