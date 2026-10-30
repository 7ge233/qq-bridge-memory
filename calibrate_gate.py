# -*- coding: utf-8 -*-
"""SIM_GATE 标定：用真实库数据给门槛取值找依据。

用真实库里的记忆评估「相关 / 不相关」的余弦分布，给出门槛建议：

  * 相关样本：取一条记忆的正文片段（6~14 字，模拟用户的短查询，如「那个加湿器拼团」），
    与它**自己**那条记忆算余弦——这是"该召回"的上界；
  * 不相关样本：同一片段与随机 300 条其他记忆算余弦——这是"不该召回"的下界；
  * 取「相关 p5 ≥ 门槛」且「不相关 p99 < 门槛」的交集，报告候选门槛下的召回率。

只读：DB 以 mode=ro 打开，不写任何东西。

用法：
    .venv\\Scripts\\python.exe calibrate_gate.py                 # 默认 50 条 / 300 负样本
    .venv\\Scripts\\python.exe calibrate_gate.py --samples 100 --negatives 500
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys

import numpy as np

from config import get_settings
from embedder import Embedder, from_blob


def pick_snippet(text: str, rng: random.Random) -> str | None:
    """从记忆正文里挑一段 6~14 字的片段，模拟用户复述时的短查询。"""
    body = "".join(ch for ch in text if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
    if len(body) < 8:
        return None
    span = min(14, max(6, len(body) // 3))
    start = rng.randrange(0, max(1, len(body) - span))
    return body[start:start + span]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--samples", type=int, default=50)
    ap.add_argument("--negatives", type=int, default=300)
    ap.add_argument("--seed", type=int, default=20261029)
    args = ap.parse_args()

    settings = get_settings()
    db_path = args.db or settings.db_path
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT id, content, embedding FROM memories "
        "WHERE is_active = 1 AND embedding IS NOT NULL AND length(content) >= 10"
    ).fetchall()
    conn.close()
    if len(rows) < 10:
        print(f"库里可用记忆太少（{len(rows)} 条），先积累数据再标定")
        return 1

    embedder = Embedder(settings)
    if not embedder.warmup():
        print(f"模型不可用：{embedder.reason}")
        return 1

    rng = random.Random(args.seed)
    pool = rows[:]
    rng.shuffle(pool)
    picked = pool[: min(args.samples, len(pool))]

    vectors = {row[0]: from_blob(row[2]) for row in rows if row[2]}
    all_ids = list(vectors)

    relevant: list[float] = []
    irrelevant: list[float] = []
    used = 0
    for mid, content, _blob in picked:
        snippet = pick_snippet(content, rng)
        if not snippet:
            continue
        q = embedder.embed_query(snippet)
        if q is None:
            print("查询编码失败，终止")
            return 1
        own = vectors.get(mid)
        if own is None:
            continue
        relevant.append(float(np.dot(q, own)))
        used += 1
        others = [i for i in all_ids if i != mid]
        for oid in rng.sample(others, min(args.negatives, len(others))):
            v = vectors[oid]
            if v is not None:
                irrelevant.append(float(np.dot(q, v)))

    if not relevant:
        print("没有可用样本")
        return 1

    rel = np.array(relevant)
    irr = np.array(irrelevant)
    print(f"样本：相关 {rel.size} 条、不相关 {irr.size} 条（seed={args.seed}）")
    print(f"相关    min={rel.min():.4f} p5={np.percentile(rel, 5):.4f} "
          f"p25={np.percentile(rel, 25):.4f} p50={np.percentile(rel, 50):.4f}")
    print(f"不相关  p95={np.percentile(irr, 95):.4f} p99={np.percentile(irr, 99):.4f} "
          f"p99.9={np.percentile(irr, 99.9):.4f} max={irr.max():.4f}")
    print()
    print("门槛  相关召回率  不相关误召率")
    for gate in (0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70):
        hit = float((rel >= gate).mean())
        fp = float((irr >= gate).mean())
        mark = "  <- 当前默认" if abs(gate - settings.sim_gate) < 1e-9 else ""
        print(f"{gate:.2f}  {hit:>10.1%}  {fp:>11.2%}{mark}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
