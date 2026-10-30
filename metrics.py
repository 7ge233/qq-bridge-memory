# -*- coding: utf-8 -*-
"""进程内指标（7.1）：计数 + 延迟分位数，供 /v1/stats 与控制台展示。

Step 3 先覆盖召回侧；写侧计数来自 MemoryWriter.stats()，Step 5 再统一。
"""

from __future__ import annotations

import threading
from collections import defaultdict, deque


class Metrics:
    def __init__(self, *, window: int = 500) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = defaultdict(int)
        self._latencies: dict[str, deque[float]] = {
            "recall": deque(maxlen=window),
            "write": deque(maxlen=window),
        }
        self._last: dict[str, int] = {}

    def inc(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] += amount

    def observe(self, kind: str, ms: float) -> None:
        with self._lock:
            bucket = self._latencies.get(kind)
            if bucket is not None:
                bucket.append(float(ms))

    def set_last(self, kind: str, value: int) -> None:
        with self._lock:
            self._last[kind] = int(value)

    def snapshot(self) -> dict:
        with self._lock:
            counters = dict(self._counters)
            last = dict(self._last)
            lat = {kind: sorted(values) for kind, values in self._latencies.items()}
        out: dict[str, object] = dict(counters)
        out.update(last)
        for kind, values in lat.items():
            out[f"{kind}_latency_ms_p50"] = _percentile(values, 50)
            out[f"{kind}_latency_ms_p95"] = _percentile(values, 95)
            out[f"{kind}_latency_samples"] = len(values)
        return out


def _percentile(sorted_values: list[float], pct: int) -> float:
    if not sorted_values:
        return 0.0
    k = max(0, min(len(sorted_values) - 1, int(round((pct / 100) * (len(sorted_values) - 1)))))
    return round(float(sorted_values[k]), 2)


metrics = Metrics()
