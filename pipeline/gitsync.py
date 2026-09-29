#!/usr/bin/env python3
"""发布线同步用的两个小工具。所有路径一律走 -z。

  restore-deleted <前缀>   把「索引里有、磁盘上没有」的文件从索引取回来
  missing <ref> <前缀>     数一数 ref 里有、磁盘上没有的文件；有就退出码 1
  adopt <ours> <theirs> <前缀>...
                           推送重试 `reset --mixed <theirs>` 之后用：磁盘上那份和远端不一样的文件，
                           本机这轮没动过的取远端的，两边都动过的走 .gitattributes 登记的合并驱动

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

import hashlib
import os
import shlex
import subprocess
import sys
import tempfile


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


def _blob_of_file(path: str, algo: str) -> str | None:
    """磁盘上这个文件如果进了 git，blob id 是多少（不存在就 None）。"""
    if not os.path.isfile(path) or os.path.islink(path):
        return None
    data = open(path, "rb").read()
    h = hashlib.new(algo)
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


def _tree(rev: str, prefixes: list[str]) -> dict[str, str]:
    out = subprocess.run(["git", "ls-tree", "-r", "-z", rev, "--", *prefixes],
                         capture_output=True, check=True).stdout
    tree = {}
    for rec in out.split(b"\0"):
        if rec:
            meta, path = rec.split(b"\t", 1)
            tree[path.decode("utf-8")] = meta.split()[2].decode()
    return tree


def _driver(path: str) -> str | None:
    """.gitattributes 给这个路径登记的合并驱动命令（没登记、或是 git 内置的就 None）。"""
    out = subprocess.run(["git", "check-attr", "-z", "merge", "--", path],
                         capture_output=True, check=True).stdout.split(b"\0")
    name = out[2].decode() if len(out) > 2 else ""
    if name in ("", "unspecified", "set", "unset", "text", "binary", "union"):
        return None
    r = subprocess.run(["git", "config", "--get", f"merge.{name}.driver"],
                       capture_output=True, text=True)
    return r.stdout.strip() or None


def _blob(sha: str | None) -> bytes:
    if not sha:
        return b""
    return subprocess.run(["git", "cat-file", "blob", sha],
                          capture_output=True, check=True).stdout


def adopt(ours: str, theirs: str, prefixes: list[str]) -> dict:
    """推送被拒后，把磁盘对齐成「远端 + 本机这一轮真正改过的」。

    **为什么需要它。** 重试分支是 `git reset --mixed <theirs>` → 重建 → `git add`。
    reset 只挪索引，磁盘上每个文件都还是本机那份 —— 包括**本机这轮根本没碰、
    而另一条线刚改过的**文件。那一刻它们的「改动」其实是把别人的提交倒回去，
    `git add` 照单全收。本机线原来只 add 一张手写清单，倒回去的只有清单里那几个；
    2026-09-29 改成 `git add -A data`（清单漏了 catchup.json，体检因此连报三天
    「建档没跑」），这一步不先做，倒回去的就是整个 data/。

    判据是标准的三方比较（base = ours 和 theirs 的分叉点，磁盘 = 本机这一方）：
      · 磁盘 == base：本机没动，取远端（远端删掉的就删掉）
      · 远端 == base：只有本机动过，留本机
      · 两边都动过：有登记的合并驱动（state/usage/心跳/留痕/封面清单）就交给它；
        没有就留本机，并且**点名打出来**，不静默
    """
    algo = subprocess.run(["git", "rev-parse", "--show-object-format"],
                          capture_output=True, text=True).stdout.strip() or "sha1"
    base = subprocess.run(["git", "merge-base", ours, theirs],
                          capture_output=True, text=True, check=True).stdout.strip()
    B, T = _tree(base, prefixes), _tree(theirs, prefixes)
    cand = set(_z(["diff", "-z", "--name-only", theirs, "--", *prefixes]))
    cand |= set(_z(["ls-files", "-z", "--others", "--exclude-standard", "--", *prefixes]))
    tally = {"theirs": [], "ours": [], "merged": [], "kept_both_changed": []}
    for p in sorted(cand):
        b, t, o = B.get(p), T.get(p), _blob_of_file(p, algo)
        if o == t:
            continue
        if o == b:
            if t is None:
                os.remove(p)
            else:
                os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
                open(p, "wb").write(_blob(t))
            tally["theirs"].append(p)
        elif t == b:
            tally["ours"].append(p)
        else:
            cmd = _driver(p) if (o and t) else None
            if cmd:
                with tempfile.TemporaryDirectory() as d:
                    fo, fa, fb = (os.path.join(d, x) for x in ("O", "A", "B"))
                    open(fo, "wb").write(_blob(b))
                    open(fa, "wb").write(open(p, "rb").read())
                    open(fb, "wb").write(_blob(t))
                    run = cmd.replace("%O", shlex.quote(fo)).replace("%A", shlex.quote(fa)) \
                             .replace("%B", shlex.quote(fb)).replace("%P", shlex.quote(p))
                    if subprocess.run(run, shell=True).returncode == 0:
                        open(p, "wb").write(open(fa, "rb").read())
                        tally["merged"].append(p)
                        continue
            tally["kept_both_changed"].append(p)
    return tally


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "adopt":
        if len(argv) < 5:
            print("用法: gitsync.py adopt <ours> <theirs> <前缀>...", file=sys.stderr)
            return 2
        t = adopt(argv[2], argv[3], argv[4:])
        print(f"对齐远端：取远端 {len(t['theirs'])} 个（本机没动过）· 留本机 {len(t['ours'])} 个"
              f" · 合并驱动合了 {len(t['merged'])} 个")
        if t["kept_both_changed"]:
            # 不静默：两边都改了、又没有合并驱动的，留的是本机版本，远端那次改动会被这次提交盖掉
            print(f"两边都改过、没有合并驱动，留本机版本 {len(t['kept_both_changed'])} 个："
                  f"{t['kept_both_changed'][:5]}", file=sys.stderr)
        return 0
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
