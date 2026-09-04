#!/usr/bin/env python3
"""只重出要点小标题，不重生成整篇。

为什么单独做这个：全站 2095 条小标题里 **84% 是名词短语式的话题名**，不是论断
——「折旧与晶圆厂滞留风险」「与长鑫合作意义」「第一笔钱投向哪里」「2017年的转折」。
读者扫一遍拿不到任何判断，等于把节目的目录翻译了一遍。这是用户说的
"展示的重点还不够核心"的主要来源。

判据已经写进 digest.SYSTEM（要点那节），但那只影响**以后**生成的。已发布的
266 篇要不要回填是产品决定，而重跑一篇整稿要一次推理调用（一集上万输出 token）。
小标题只依赖已有的要点正文，拿正文喂便宜模型重出一个标题，一集几百 token。

和 retitle.py 同一个套路，同一个理由：改了判据必须能验证它真的有效。

**只改 h 这一个字段，绝不调 digest.normalize()。** normalize 是给模型的原始输出
用的：它把每个字段 str() 一遍，于是 `t: None` 会变成 `""`。对已发布的数据调它，
时间戳会被整片写成空串——而 hhmmss("") 当时会抛 ValueError，把整站构建炸掉。
我自己就这么干了一次（幸好回填还没写盘就发现了）。retitle.py 和 repunct.py
一直是逐字段过 _cn_punct，正是因为这个。

    python3 pipeline/repoint.py --dry-run --limit 6     # 只看新旧对比
    python3 pipeline/repoint.py --limit 40              # 写回
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lib import llm                                              # noqa: E402
from lib.digest import SYSTEM, _cn_punct                         # noqa: E402
from lib.util import log, squeeze                                # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
EPS = ROOT / "data" / "episodes"
# 断点续跑。266 篇一趟要三小时，中间一定会被打断（要发别的东西、要重启）。
# 没有这个文件的话，重启会把已经改好的再花一遍钱——而且它们按 mtime 排在最前面，
# 等于每次重启都先把钱花在已经做完的那批上。
DONE = ROOT / "data" / "repoint-done.json"


def load_done() -> set[str]:
    try:
        return set(json.loads(DONE.read_text()).get("slugs") or [])
    except Exception:
        return set()


def save_done(s: set[str]) -> None:
    DONE.write_text(json.dumps({"slugs": sorted(s)}, ensure_ascii=False, indent=1) + "\n")

# 带否定/转折/断言标记的才算论断；名词短语一个都没有。粗糙但可量化，
# 用来在回填前后对比，而不是当硬闸门。
CLAIM = re.compile(r"不是.*而是|不|没|非|反而|其实|却|才|只|会|要|能")

ASK = ('只做一件事：把下面每条要点的小标题重写成**一句能被反对的话**，'
       '严格按系统提示里「要点怎么选、怎么写」那一节的小标题判据（8-18 字）。\n'
       '正文一个字都不要改，也不要增删要点，顺序不变。\n'
       '返回 JSON：{"h": ["新标题1", "新标题2", ...]}，条数必须和给你的一样。')


def brief(d: dict) -> str:
    g = d["digest"]
    out = [f"节目：{d.get('source_zh') or d.get('source')}",
           f"这篇的标题：{g.get('title')}", "", "要点（小标题 → 正文）："]
    for i, p in enumerate(g.get("points") or [], 1):
        out.append(f"{i}. 现在的小标题：{p.get('h')}")
        out.append(f"   正文：{squeeze(p.get('body') or '')[:320]}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=6)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", default="", help="只处理这些 source_id，逗号分隔")
    ap.add_argument("--redo", action="store_true", help="忽略断点记录，全部重跑")
    a = ap.parse_args()

    want = {x.strip() for x in a.only.split(",") if x.strip()}
    seen = set() if a.redo else load_done()
    # 按稿子的 generated 排，不按 mtime：mtime 会被这个工具自己改掉
    files = sorted(EPS.glob("*.json"), reverse=True)
    if seen:
        log(f"已回填过 {len(seen)} 篇，跳过")
    done = before = after = n_h = 0
    for f in files:
        if done >= a.limit:
            break
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        if want and d.get("source_id") not in want:
            continue
        if d.get("slug") in seen:
            continue
        pts = (d.get("digest") or {}).get("points") or []
        if not pts:
            continue
        old = [p.get("h") or "" for p in pts]
        try:
            r = llm.call_json(SYSTEM, brief(d) + "\n\n" + ASK,
                              max_tokens=900, temperature=0.2, role="review")
        except Exception as ex:
            log(f"  {(d['digest'].get('title') or '')[:24]} 重出失败：{type(ex).__name__}")
            continue
        new = [squeeze(str(x)) for x in (r.get("h") or [])]
        if len(new) != len(old) or not all(new):
            log(f"  条数不对（要 {len(old)} 给了 {len(new)}），跳过这篇")
            continue
        done += 1
        log(f"\n《{d['digest'].get('title')}》")
        for o, x in zip(old, new):
            n_h += 1
            before += bool(CLAIM.search(o))
            after += bool(CLAIM.search(x))
            log(f"  {'＝' if o == x else '→'} {o}" + ("" if o == x else f"    ⇒ {x}"))
        if not a.dry_run:
            for p, x in zip(pts, new):
                p["h"] = _cn_punct(x)
            f.write_text(json.dumps(d, ensure_ascii=False, indent=1) + "\n")
            seen.add(d.get("slug") or "")
            save_done(seen)          # 每篇都存：三小时的任务不能靠"跑完再存"
    if n_h:
        log(f"\n{'（dry-run，一个字没写）' if a.dry_run else '已写回'} "
            f"{done} 篇 · {n_h} 条小标题｜带断言标记：{before} → {after}")
    if not a.dry_run and done:
        log("记得跑 python3 pipeline/build.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
