#!/usr/bin/env python3
"""发布线同步用的小工具。所有路径一律走 -z。

  restore-deleted <前缀>   把「索引里有、磁盘上没有」的文件从索引取回来
  missing <ref> <前缀>     数一数 ref 里有、磁盘上没有的文件；有就退出码 1
  adopt <ours> <theirs> <前缀>...
                           推送重试 `reset --mixed <theirs>` 之后用：磁盘上那份和远端不一样的文件，
                           本机这轮没动过的取远端的，两边都动过的走 .gitattributes 登记的合并驱动
  add-site [额外路径…]     把建站产物（SITE）整套加进索引，加完核对一个没剩
  site-paths [额外路径…]   打印 SITE 此刻真实存在的路径，给 shell 的 git add 用

**为什么要单独成文件。** 2026-09-23 本机线推送被拒后走重试分支：
`git reset --mixed origin/main`，再用

    git diff --name-only --diff-filter=D -- data | while read -r f; do git checkout -- "$f"; done

把「远端有、本机磁盘没有」的数据文件取回。可 git 默认会把非 ASCII 路径转义成
`"data/episodes/2022-10-18-zhangxiaojun-\\350\\266…"`，checkout 拿着这串找不到文件，
`|| true` 把失败吞掉 —— 于是 19 篇中文名的已发布集被当成本机删除，提交、推送，
从站上消失。同一段代码还抄在云端 daily / fast / backfill 三条工作流里。
提交前那道缺集检查用的是 -z，所以它是对的；但它只在第一次提交前跑，重试分支里没有。

这里取回之后**逐个核对文件真的回来了**，回不来就报错，不许静默吞掉。

**建站产物也只有一份清单，就是下面的 SITE。** 2026-09-28 curate.yml 跑完 build.py，
只 `git add` 了一张手抄的清单（index.html sources s p log feed.xml …），里面没有
tw/ en/ c/ e/ zt/ hot.json cards-*.json api.json。信源等级一改，简体的 s/ 更新了，
繁体、英文、分类页和分页卡片带着旧的「必看」留在线上。backfill.yml 抄的是同一张，
本机线另有一张（漏过 e、log，又漏过 c、api.json、zt）。现在它们都从这里取；
tests/test_guards.py 从 build.py / tw.py 的写入点推导产物集合，和 SITE 对不上就红。
"""
from __future__ import annotations

import fnmatch
import glob
import hashlib
import os
import shlex
import subprocess
import sys
import tempfile

# 一次建站写出的全部顶层路径：build.py 往仓库根和 en/ 写的，加上 tw.py 生成的繁体树。
# 加了新产物就加在这里；漏了守护会红（它从 build.py 的写入点推导，不看这张表）。
SITE = ("index.html", "hot.json", "sources", "404.html", "feed.xml", "sitemap.xml",
        "search.json", "api.json", "log", "zt", "s", "c", "robots.txt",
        "llms.txt", "llms-full.txt", ".nojekyll", "p", "e",
        "cards-*.json",   # 分页数随篇数变：通配，而且要连被删掉的那几页一起提交
        "cards.json",     # 第一版的单文件分页；build.py 见到就删，删除也要进提交
        "en", "tw")


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


def _tracked(paths: list[str]) -> list[str]:
    return _z(["ls-files", "-z", "--", *paths]) if paths else []


def site_paths(extra: list[str] | tuple[str, ...] = ()) -> list[str]:
    """SITE 加上 extra，展开成此刻真实存在的路径：磁盘上有，或者索引里有。

    - 通配（cards-*.json）磁盘和索引**两边**都展开。只按磁盘展开（shell 的 `ls cards-*.json`
      就是这样），页数变少时 build.py 删掉的 cards-N.json 永远不进提交，线上留着过期的那页。
    - 两边都没有的丢掉。git add 碰到一个不匹配的 pathspec 会**整条命令作废**，
      而调用处的 `2>/dev/null || true` 把它吞掉 —— 一个没建出来的目录就能让整站一个文件都不提交。
    """
    specs = list(SITE) + [e for e in extra if e not in SITE]
    tracked = _tracked(specs)
    out: list[str] = []
    for spec in specs:
        if any(ch in spec for ch in "*?["):
            depth = spec.count("/")
            hits = set(glob.glob(spec)) | {p for p in tracked
                                           if p.count("/") == depth and fnmatch.fnmatch(p, spec)}
            out += sorted(hits - set(out))
        elif os.path.lexists(spec) or any(p == spec or p.startswith(spec + "/") for p in tracked):
            out.append(spec)
    return out


def add_site(extra: list[str] | tuple[str, ...] = ()) -> int:
    """把建站产物整套加进索引（增、改、删都算），然后**从磁盘核一遍**：
    这些路径下不许还剩没暂存的改动或未跟踪的文件。剩了就报错，不许推半套站。
    返回暂存了多少个产物文件的变动。"""
    paths = site_paths(extra)
    if not paths:
        raise SystemExit("一个建站产物都没找到 —— 不在仓库根目录跑，还是没跑 build.py？")
    subprocess.run(["git", "add", "-A", "--", *paths], check=True)
    left = (_z(["diff", "-z", "--name-only", "--", *paths])
            + _z(["ls-files", "-z", "--others", "--exclude-standard", "--", *paths]))
    if left:
        raise SystemExit(f"加完还有 {len(left)} 个产物没进索引：{left[:5]}")
    return len(_z(["diff", "-z", "--cached", "--name-only", "--", *paths]))


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
    if len(argv) >= 2 and argv[1] == "add-site":
        n = add_site(argv[2:])
        print(f"建站产物已整套暂存：{n} 个文件有变动")
        return 0
    if len(argv) >= 2 and argv[1] == "site-paths":
        paths = site_paths(argv[2:])
        bad = [p for p in paths if any(c.isspace() for c in p)]
        if bad or not paths:
            # 输出要被 shell 按空白切开；带空白的路径会被切碎，空输出等于什么都不加
            raise SystemExit(f"建站产物路径没法交给 shell：{bad[:3] or '一个都没找到'}")
        print(" ".join(paths))
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
