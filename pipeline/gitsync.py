#!/usr/bin/env python3
"""发布线同步用的两个小工具。所有路径一律走 -z。

  restore-deleted <前缀>   把「索引里有、磁盘上没有」的文件从索引取回来
  missing <ref> <前缀>     数一数 ref 里有、磁盘上没有的文件；有就退出码 1

**为什么要单独成文件。** 2026-09-23 本机线推送被拒后走重试分支：
`git reset --mixed origin/main`，再用

    git diff --name-only --diff-filter=D -- data | while read -r f; do git checkout -- "$f"; done

把「远端有、本机磁盘没有」的数据文件取回。可 git 默认会把非 ASCII 路径转义成
`"data/episodes/2022-10-18-zhangxiaojun-\\350\\266…"`，checkout 拿着这串找不到文件，
`|| true` 把失败吞掉 —— 于是 19 篇中文名的已发布集被当成本机删除，提交、推送，
从站上消失。同一段代码还抄在云端 daily / fast / backfill 三条工作流里。
提交前那道缺集检查用的是 -z，所以它是对的；但它只在第一次提交前跑，重试分支里没有。

这里取回之后**逐个核对文件真的回来了**，回不来就报错，不许静默吞掉。
"""
from __future__ import annotations

import os
import subprocess
import sys


def _z(args: list[str]) -> list[str]:
    out = subprocess.run(["git", *args], capture_output=True, check=True).stdout
    return [p.decode("utf-8") for p in out.split(b"\0") if p]


def restore_deleted(prefix: str) -> int:
    gone = _z(["diff", "-z", "--name-only", "--diff-filter=D", "--", prefix])
    for i in range(0, len(gone), 200):
        subprocess.run(["git", "checkout", "-q", "--", *gone[i:i + 200]], check=True)
    still = [p for p in gone if not os.path.exists(p)]
    if still:
        raise SystemExit(f"取回失败 {len(still)} 个：{still[:5]}")
    return len(gone)


def missing(ref: str, prefix: str) -> list[str]:
    return sorted(p for p in _z(["ls-tree", "-r", "-z", "--name-only", ref, "--", prefix])
                  if not os.path.exists(p))


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "restore-deleted":
        n = restore_deleted(argv[2] if len(argv) > 2 else "data")
        print(f"取回 {n} 个索引里有、磁盘上没有的文件")
        return 0
    if len(argv) >= 2 and argv[1] == "missing":
        ref = argv[2] if len(argv) > 2 else "origin/main"
        pre = argv[3] if len(argv) > 3 else "data/episodes"
        m = missing(ref, pre)
        print(len(m))
        if m:
            print(f"{ref} 里有、磁盘上没有：{len(m)} 个，例如 {m[:3]}", file=sys.stderr)
        return 1 if m else 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
