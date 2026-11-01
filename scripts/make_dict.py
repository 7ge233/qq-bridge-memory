#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按词频生成裁剪后的 jieba 词典。

    python scripts/make_dict.py --words 30000 --out dict-30k.txt

必须按词频排序：jieba 自带的 dict.txt 不是按词频排的（首行词频 3，末行 732），
直接取前 N 行只会拿到一段按字符码点聚集的词表，切词质量明显变差。
"""
from __future__ import annotations

import argparse
import io
import os


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--words", type=int, default=30000)
    parser.add_argument("--out", default="dict-30k.txt")
    args = parser.parse_args()

    import jieba
    src = os.path.join(os.path.dirname(jieba.__file__), "dict.txt")
    rows = []
    with io.open(src, encoding="utf-8") as fh:
        for line in fh:
            parts = line.split()
            freq = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            rows.append((freq, line))
    rows.sort(key=lambda item: -item[0])
    with io.open(args.out, "w", encoding="utf-8") as dst:
        for _freq, line in rows[: args.words]:
            dst.write(line)
    print("写入 %s：%d 条（原词典 %d 条）" % (args.out, min(args.words, len(rows)), len(rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
