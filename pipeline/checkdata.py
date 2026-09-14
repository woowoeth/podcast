#!/usr/bin/env python3
"""同步之后、花钱之前，检查 data 下的账本还能不能读。

**同步「成功」不等于工作区是好的。**
`git pull --rebase --autostash` 在恢复 autostash 时冲突了，**退出码仍是 0**：
git 只警告一句，把冲突标记留在工作区，stash 也留着（实测那台机器堆了 5 个）。
于是那一轮照跑，深读发了 6 篇，最后在建站读 data/en/_sources.json 时撞上
`<<<<<<< Updated upstream` 崩掉 —— 6 篇稿子既没提交也没推，只躺在那台机器的
工作区里，而体检看到的是「这条线没跑」。

坏账本要在**花钱之前**发现，不是在花完之后。
判据不看 git 的退出码，直接量真东西：data 下每个 JSON 能不能解析。

能从 origin/main 取回的就地修好（data 下的这些都是可再生或已推送的），
修不好就返回非零，让调用方停手。

    python3 pipeline/checkdata.py            # 检查并尝试修复
    python3 pipeline/checkdata.py --dry-run  # 只报告，不改任何文件
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def broken_json(root: pathlib.Path) -> list[pathlib.Path]:
    """root 下所有解析不了的 JSON。"""
    out = []
    for f in sorted(root.rglob("*.json")):
        try:
            json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            out.append(f)
    return out


def _git(*args: str) -> int:
    return subprocess.run(["git", "-C", str(ROOT), *args],
                          capture_output=True).returncode


def repair(f: pathlib.Path) -> bool:
    """从 origin/main 取回这个文件。取不到或取回来仍然坏，返回 False。"""
    rel = f.relative_to(ROOT).as_posix()
    if _git("cat-file", "-e", f"origin/main:{rel}") != 0:
        return False
    if _git("checkout", "origin/main", "--", rel) != 0:
        return False
    try:
        json.loads(f.read_text(encoding="utf-8"))
        return True
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只报告，不改文件")
    a = ap.parse_args()

    bad = broken_json(DATA)
    if not bad:
        return 0

    print(f"data 下有 {len(bad)} 个 JSON 解析不了：", file=sys.stderr)
    for f in bad:
        print(f"  {f.relative_to(ROOT)}", file=sys.stderr)
    if a.dry_run:
        return 1

    left = [f for f in bad if not repair(f)]
    fixed = [f for f in bad if f not in left]
    if fixed:
        print(f"  从 origin/main 取回修好 {len(fixed)} 个", file=sys.stderr)
    if left:
        print(f"  还有 {len(left)} 个修不好 —— 停手，不要带着坏账本往下跑：",
              file=sys.stderr)
        for f in left:
            print(f"    {f.relative_to(ROOT)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
