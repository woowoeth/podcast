#!/usr/bin/env python3
"""把已发布内容里漏掉的半角标点补成全角。

为什么需要回填：归一化判据原来要求标点**两侧都是汉字**，于是句末和引号前的
`？，！：。` 全漏了（用户挑出来的：「…不一定成功.」的句号、「这是什么?」的问号）。
判据修好只影响以后生成的，已经在站上的 255 篇得单独走一遍。

纯本地，不调模型。**绝不动 quotes[].raw**——那一栏是逐字原文，改一个字符就
通不过机械闸门的逐字校验。

    python3 pipeline/repunct.py --dry-run
    python3 pipeline/repunct.py
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lib.digest import _cn_punct                                  # noqa: E402
from lib.util import log                                          # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
EPS = ROOT / "data" / "episodes"

# 和 digest.normalize 保持一致：每一栏都过，除了 quotes.raw
FIELDS = ("title", "dek", "why", "who", "skip")


def fix(d: dict) -> int:
    """返回改动的字段数。"""
    g = d.get("digest") or {}
    n = 0

    def put(obj, key):
        nonlocal n
        v = obj.get(key)
        if isinstance(v, str) and v:
            new = _cn_punct(v)
            if new != v:
                obj[key] = new
                n += 1

    for k in FIELDS:
        put(g, k)
    for p in (g.get("points") or []):
        put(p, "h")
        put(p, "body")
    for q in (g.get("quotes") or []):
        put(q, "zh")          # raw 不动：逐字原文
    for f in (g.get("facts") or []):
        put(f, "k")
        put(f, "v")
    for t in (g.get("terms") or []):
        put(t, "zh")
        put(t, "def")
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    files, touched, fields = sorted(EPS.glob("*.json")), 0, 0
    samples = []
    for f in files:
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        before = json.dumps(d, ensure_ascii=False)
        n = fix(d)
        if not n:
            continue
        touched += 1
        fields += n
        if len(samples) < 6:
            g = d["digest"]
            samples.append(g.get("title") or "")
        if not a.dry_run:
            f.write_text(json.dumps(d, ensure_ascii=False, indent=1) + "\n")
        del before
    log(f"{'（dry-run，一个字没写）' if a.dry_run else '已写回'} "
        f"{touched}/{len(files)} 篇有半角标点，共修 {fields} 处字段")
    for t in samples:
        log(f"  例：{t}")
    if not a.dry_run and touched:
        log("记得跑 python3 pipeline/build.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
