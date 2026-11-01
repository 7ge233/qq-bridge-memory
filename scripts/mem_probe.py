#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mem_probe.py — 只读测量内存占用与 jieba 词典裁剪的影响。

不写数据库、不联网、不改配置：数据库用只读连接，裁剪词典只写系统临时目录。

用法
----
    python scripts/mem_probe.py
    python scripts/mem_probe.py --db /opt/x/data/qq-memory.db --out /tmp/report.json
    python scripts/mem_probe.py --samples 800
    python scripts/mem_probe.py --skip-model

返回类型（标准输出最后一段是 JSON，前后各有标记行）
------------------------------------------------
schema = "memory_probe_report/1"

{
  "schema": "memory_probe_report/1",             # str
  "generated_at": "2026-11-01T10:00:00+08:00",   # str  ISO8601
  "python": "3.12.8",                            # str
  "platform": "Linux-6.8.0-x86_64",              # str
  "cpu_count": 2,                                # int
  "db": {
    "path": "/opt/qq-bridge-memory/data/qq-memory.db",  # str
    "readable": true,                            # bool
    "rows": {"turns": 1200, "memories": 340},    # obj  int
    "error": null                                # str|null
  },
  "stages": [                                    # arr  按执行顺序
    {"name": "interpreter", "rss_mb": 13.2},     # name=str, rss_mb=float(MB)
    {"name": "jieba_full",  "rss_mb": 118.4},
    {"name": "jieba_30k",   "rss_mb": 60.1},
    {"name": "model",       "rss_mb": 280.5}
  ],
  "model": {
    "loaded": true,                              # bool
    "state": "ready",                            # str  ready|degraded|skipped
    "load_seconds": 0.61,                        # float
    "dim": 512,                                  # int
    "reason": ""                                 # str
  },
  "dict_ab": {
    "samples": 400,                              # int
    "source": "conversation_turns.content",      # str
    "full": {
      "avg_tokens": 9.82,                        # float
      "identical_to_full_pct": 100.0,            # float  与全量分词序列完全一致的句子占比
      "jaccard": 1.0,                            # float  0-1 词集合平均 Jaccard
      "recall_at_1_pct": 97.0,                   # float
      "recall_at_6_pct": 98.2,                   # float  原句进前 6 的比例
      "mrr": 0.975                               # float  0-1
    },
    "30k": {"...同上..."},
    "10k": {"...同上..."},
    "note": "一句话结论"                          # str
  },
  "exit": {
    "ok": true,                                  # bool
    "warnings": []                               # arr[str]
  }
}

退出码：0 正常；2 数据库不可读；3 模型未就绪。参数错误由 argparse 以 2 退出。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import platform
import random
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime

SCHEMA = "memory_probe_report/1"
_WORK = None


def work_dir() -> str:
    """临时文件统一放这里，避免落进仓库目录。"""
    global _WORK
    if _WORK is None:
        _WORK = tempfile.mkdtemp(prefix="qq-memory-probe-")
    return _WORK
DICT_SIZES = {"full": None, "30k": 30000, "10k": 10000}
CJK = re.compile(r"[\u4e00-\u9fff]")


def rss_mb() -> float:
    """当前进程常驻内存（MB）。Linux 读 /proc/self/status。"""
    try:
        with io.open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024.0, 1)
    except OSError:
        pass
    try:
        import resource  # 非 Linux（如 Windows）没有这个模块
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)
    except ImportError:
        return -1.0


def trimmed_dict(name: str) -> str:
    """按词频从高到低取前 N 条词，写入临时词典并返回路径。

    注意：jieba 自带的 dict.txt 不是按词频排序的（首行词频 3，末行 732），
    直接取前 N 行会得到一段按字符码点聚集的词表，切词效果很差，必须自己排序。
    """
    limit = DICT_SIZES[name]
    import jieba
    src = os.path.join(os.path.dirname(jieba.__file__), "dict.txt")
    out = os.path.join(work_dir(), "dict-freq-%s.txt" % name)
    if name != "full" and not (os.path.exists(out) and os.path.getsize(out) > 0):
        rows = []
        with io.open(src, encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                freq = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                rows.append((freq, line))
        rows.sort(key=lambda item: -item[0])
        with io.open(out, "w", encoding="utf-8") as dst:
            for _freq, line in rows[:limit]:
                dst.write(line)
    return out


def load_texts(path: str, samples: int) -> list[str]:
    """从 JSON（父进程传入的句子列表）或数据库里取句子。"""
    if path.endswith(".json"):
        with io.open(path, encoding="utf-8") as fh:
            return json.load(fh)[:samples]
    return sample_texts(path, samples)[0]


def read_corpus(path: str, want: int) -> list[str]:
    """从导出的聊天文本里取句子：每行按制表符切成多条，去掉字间空格。"""
    texts: list[str] = []
    seen = set()
    with io.open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            for part in line.rstrip("\n").split("\t"):
                clean = part.replace(" ", "").strip()
                if not (8 <= len(clean) <= 60) or len(CJK.findall(clean)) < 6:
                    continue
                if clean in seen:
                    continue
                seen.add(clean)
                texts.append(clean)
                if len(texts) >= want:
                    return texts
    return texts


def tokenize_all(texts: list[str], name: str) -> list[list[str]]:
    """用指定规模的词典切词；必须在尚未初始化 jieba 的进程里调用。"""
    import jieba
    if name != "full":
        jieba.set_dictionary(trimmed_dict(name))
    jieba.initialize()
    return [list(jieba.lcut(t)) for t in texts]


def sample_texts(db_path: str, want: int):
    """从 conversation_turns.content 抽样，取不到再试 memories.content。"""
    if not os.path.exists(db_path):
        return [], "", {}, "文件不存在"
    uri = "file:%s?mode=ro&immutable=1" % db_path.replace("\\", "/")
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        return [], "", {}, str(exc)
    try:
        rows: dict = {}
        picked: list[str] = []
        source = ""
        for table, key in (("conversation_turns", "turns"), ("memories", "memories")):
            try:
                rows[key] = int(conn.execute("SELECT count(*) FROM %s" % table).fetchone()[0])
            except sqlite3.Error:
                continue
            if picked:
                continue
            try:
                columns = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
            except sqlite3.Error:
                continue
            if "content" not in columns:
                continue
            source = "%s.content" % table
            cursor = conn.execute(
                "SELECT content FROM %s WHERE length(content) BETWEEN 8 AND 60 "
                "ORDER BY random() LIMIT ?" % table, (want * 3,))
            for (text,) in cursor.fetchall():
                clean = (text or "").strip()
                if len(clean) < 8 or len(CJK.findall(clean)) < 6:
                    continue
                picked.append(clean)
                if len(picked) >= want:
                    break
        return picked, source, rows, None
    finally:
        conn.close()


def fts_quality(tokens: list[list[str]], seed: int = 11) -> dict:
    """用给定分词建 FTS5 索引，抽样查询，看原句能否检索到自己。"""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE fts USING fts5(toks)")
        conn.executemany("INSERT INTO fts(toks) VALUES (?)", [(" ".join(t),) for t in tokens])
        picks = random.Random(seed).sample(range(len(tokens)), min(400, len(tokens)))
        hit1 = hit6 = 0
        total_rank = 0.0
        for index in picks:
            terms = ['"%s"' % t.replace('"', "") for t in tokens[index] if len(t) > 1]
            query = " OR ".join(terms) if terms else '"%s"' % tokens[index][0]
            try:
                ids = [r[0] for r in conn.execute(
                    "SELECT rowid FROM fts WHERE fts MATCH ? ORDER BY rank LIMIT 10", (query,))]
            except sqlite3.Error:
                ids = []
            if not ids:
                continue
            if ids[0] == index + 1:
                hit1 += 1
            if (index + 1) in ids[:6]:
                hit6 += 1
            for rank, rowid in enumerate(ids, 1):
                if rowid == index + 1:
                    total_rank += 1.0 / rank
                    break
        count = len(picks) or 1
        return {
            "recall_at_1_pct": round(hit1 / count * 100, 1),
            "recall_at_6_pct": round(hit6 / count * 100, 1),
            "mrr": round(total_rank / count, 3),
        }
    finally:
        conn.close()


def summarise(tokens: list[list[str]], base: list[list[str]] | None) -> dict:
    identical, jaccard = 100.0, 1.0
    if base is not None:
        same = sum(1 for a, b in zip(base, tokens) if a == b)
        identical = round(same / max(1, len(base)) * 100, 1)
        scores = []
        for a, b in zip(base, tokens):
            sa, sb = set(a), set(b)
            scores.append(len(sa & sb) / max(1, len(sa | sb)))
        jaccard = round(sum(scores) / max(1, len(scores)), 3)
    out = {
        "avg_tokens": round(sum(len(t) for t in tokens) / max(1, len(tokens)), 2),
        "identical_to_full_pct": identical,
        "jaccard": jaccard,
    }
    out.update(fts_quality(tokens))
    return out


def worker(job: str, source: str, samples: int) -> None:
    """子进程任务：jieba 词典一旦初始化就不能换，每种配置都要独立进程。"""
    payload: dict = {"job": job}
    if job.startswith("rss:"):
        name = job.split(":", 1)[1]
        payload["rss_mb"] = rss_mb()
        if name in DICT_SIZES:
            tokenize_all(["触发词典加载"], name)
            payload["rss_mb"] = rss_mb()
        elif name == "model":
            import config
            from embedder import Embedder
            started = time.perf_counter()
            embedder = Embedder(config.get_settings())
            embedder.warmup()
            health = embedder.health()
            payload["rss_mb"] = rss_mb()
            payload["model"] = {
                "state": health.get("state", "unknown"),
                "dim": int(health.get("dim") or 0),
                "load_seconds": round(time.perf_counter() - started, 2),
                "reason": health.get("reason", ""),
            }
    elif job.startswith("tokens:"):
        name = job.split(":", 1)[1]
        texts = load_texts(source, samples)
        payload["tokens"] = tokenize_all(texts, name)
        payload["count"] = len(texts)
    print("__JSON__" + json.dumps(payload, ensure_ascii=False))


def project_root(here: str) -> str:
    """找项目根：脚本所在目录的上一级，找不到就退回到当前工作目录。

    脚本放在 scripts/ 里时是上一级；被单独拷到别处（比如 /tmp）时靠工作目录，
    所以运行时要在项目根目录下执行。
    """
    candidates = [os.path.dirname(here), os.getcwd(), here]
    for path in candidates:
        if path and os.path.exists(os.path.join(path, "config.py")):
            return path
    return os.getcwd()


def spawn(job: str, source: str, samples: int) -> dict:
    here = os.path.dirname(os.path.abspath(__file__))
    root = project_root(here)             # 项目根目录（config.py / embedder.py 所在处）
    env = dict(os.environ)
    env["PYTHONPATH"] = root + os.pathsep + here + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, os.path.abspath(__file__), "_worker", job, source, str(samples)],
        capture_output=True, text=True, encoding="utf-8", cwd=root, env=env)
    for line in proc.stdout.splitlines():
        if line.startswith("__JSON__"):
            return json.loads(line[len("__JSON__"):])
    return {"job": job, "error": (proc.stderr or "no output")[-400:]}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="只读测量内存与 jieba 词典裁剪影响")
    parser.add_argument("--db", default=os.path.join("data", "qq-memory.db"))
    parser.add_argument("--samples", type=int, default=400)
    parser.add_argument("--out", default="")
    parser.add_argument("--corpus", default="", help="改从导出的聊天文本取样（每行一条）")
    parser.add_argument("--skip-model", action="store_true")
    args = parser.parse_args(argv)
    args.db = os.path.abspath(args.db)    # 子进程的工作目录是 scripts/，路径必须先绝对化

    report: dict = {
        "schema": SCHEMA,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count() or 1,
        "db": {"path": os.path.abspath(args.db), "readable": False, "rows": {}, "error": None},
        "stages": [],
        "model": {"loaded": False, "state": "skipped", "load_seconds": 0.0, "dim": 0, "reason": ""},
        "dict_ab": {"samples": 0, "source": "", "full": {}, "30k": {}, "10k": {}, "note": ""},
        "exit": {"ok": True, "warnings": []},
    }
    warnings: list = report["exit"]["warnings"]

    if args.corpus:
        texts = read_corpus(args.corpus, args.samples)
        source, rows, error = "corpus:%s" % os.path.basename(args.corpus), {}, None
    else:
        texts, source, rows, error = sample_texts(args.db, args.samples)
    report["db"].update({"readable": error is None, "rows": rows, "error": error})
    if error:
        warnings.append("数据库不可读：%s" % error)
    if 0 < len(texts) < 50:
        warnings.append("可用会话句子只有 %d 条，A/B 结论参考价值有限" % len(texts))

    report["stages"].append({"name": "interpreter", "rss_mb": rss_mb()})

    tokens_path = args.db
    if texts:
        tokens_path = os.path.join(work_dir(), "texts-%d.json" % os.getpid())
        with io.open(tokens_path, "w", encoding="utf-8") as fh:
            json.dump(texts, fh, ensure_ascii=False)
    tokens: dict = {}
    if texts:
        for name in ("full", "30k", "10k"):
            got = spawn("tokens:" + name, tokens_path, len(texts))
            tokens[name] = got.get("tokens") or []
            if not tokens[name]:
                warnings.append("词典 %s 切词结果为空：%s" % (name, got.get("error", "")))
                continue
            report["dict_ab"][name] = summarise(tokens[name], tokens.get("full"))
            report["dict_ab"]["samples"] = len(texts)
            report["dict_ab"]["source"] = source

    if tokens.get("full"):
        report["stages"].append({"name": "jieba_full", "rss_mb": spawn("rss:full", args.db, 0).get("rss_mb", -1.0)})
        report["stages"].append({"name": "jieba_30k", "rss_mb": spawn("rss:30k", args.db, 0).get("rss_mb", -1.0)})

    if not args.skip_model:
        stage = spawn("rss:model", args.db, 0)
        if "error" in stage:
            warnings.append("模型阶段失败：%s" % stage["error"])
            report["exit"]["ok"] = False
        else:
            report["stages"].append({"name": "model", "rss_mb": stage.get("rss_mb", -1.0)})
            model = stage.get("model") or {}
            report["model"].update({
                "loaded": model.get("state") == "ready",
                "state": model.get("state", "unknown"),
                "load_seconds": model.get("load_seconds", 0.0),
                "dim": model.get("dim", 0),
                "reason": model.get("reason", ""),
            })
            if model.get("state") != "ready":
                warnings.append("模型未就绪：%s" % (model.get("reason") or model.get("state")))
                report["exit"]["ok"] = False

    full = report["dict_ab"].get("full") or {}
    short = report["dict_ab"].get("30k") or {}
    if full and short:
        report["dict_ab"]["note"] = (
            "裁到 3 万词：分词序列与全量一致 %s%%，词集合 Jaccard %s，"
            "FTS 自召回 recall@6 从 %s%% 变为 %s%%"
            % (short.get("identical_to_full_pct"), short.get("jaccard"),
               full.get("recall_at_6_pct"), short.get("recall_at_6_pct")))

    blob = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        with io.open(args.out, "w", encoding="utf-8") as fh:
            fh.write(blob + "\n")

    print("\n===== 分阶段常驻内存 =====")
    for stage in report["stages"]:
        print("  %-12s %7.1f MB" % (stage["name"], stage["rss_mb"]))
    print("\n===== 词典裁剪 A/B（%d 条真实句子，来源 %s）=====" % (report["dict_ab"]["samples"], source or "无"))
    for name in ("full", "30k", "10k"):
        item = report["dict_ab"].get(name) or {}
        if item:
            print("  %-4s 平均 %.2f 词  与全量一致 %5.1f%%  Jaccard %.3f  recall@6 %5.1f%%  MRR %.3f"
                  % (name, item.get("avg_tokens", 0), item.get("identical_to_full_pct", 0),
                     item.get("jaccard", 0), item.get("recall_at_6_pct", 0), item.get("mrr", 0)))
    if report["dict_ab"]["note"]:
        print("  结论：" + report["dict_ab"]["note"])
    if warnings:
        print("\n===== 注意 =====")
        for item in warnings:
            print("  - " + item)
    print("\n===== JSON BEGIN =====")
    print(blob)
    print("===== JSON END =====")
    return 0 if report["db"]["readable"] and report["exit"]["ok"] else (2 if not report["db"]["readable"] else 3)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "_worker":
        worker(sys.argv[2], sys.argv[3], int(sys.argv[4]))
    else:
        sys.exit(main(sys.argv[1:]))
