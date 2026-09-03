#!/usr/bin/env python3
"""只重出标题，不重生成整篇。

为什么单独做这个：标题判据改了之后必须验证它真的有效，而重跑一篇成稿要一次推理
调用（一集 1.5 万输出 token，其中 80% 是思考）。标题只依赖已有的要点和金句，
拿它们喂便宜模型重出一个标题，一集几百 token，十几集就能看出判据改对没改对。

    python3 pipeline/retitle.py --dry-run --limit 12    # 只看新旧对比
    python3 pipeline/retitle.py --limit 12             # 写回并重建

回填是可选的：改判据的目的是让**以后**的标题变好，已发布的要不要重写是产品决定。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lib import llm                                            # noqa: E402
from lib.digest import SYSTEM                                  # noqa: E402
from lib.util import log, squeeze                              # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
EPS = ROOT / "data" / "episodes"

ASK = ('只做一件事：给这篇深读重写标题。严格按系统提示里「标题怎么写」那一节。\n'
       '返回 JSON：{"title": "...", "why": "一句话说明它符合哪几条"}')


def brief(d: dict) -> str:
    g = d["digest"]
    out = [f"节目：{d.get('source_zh') or d.get('source')}",
           f"原集标题：{d.get('title_original')}",
           f"现在的标题（要替换掉）：{g.get('title')}",
           f"一句话结论：{g.get('dek')}", "", "要点："]
    for p in (g.get("points") or [])[:8]:
        out.append(f"- {p.get('h')}：{squeeze(p.get('body') or '')[:150]}")
    qs = [q for q in (g.get("quotes") or []) if q.get("zh") or q.get("raw")][:3]
    if qs:
        out.append("\n金句：")
        for q in qs:
            out.append(f"- {squeeze(q.get('zh') or q.get('raw'))[:120]}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--dry-run", action="store_true", help="只打印新旧对比，不写入")
    ap.add_argument("--only", default="", help="只处理这些 source_id，逗号分隔")
    a = ap.parse_args()

    want = {x.strip() for x in a.only.split(",") if x.strip()}
    files = sorted(EPS.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    done = 0
    for f in files:
        if done >= a.limit:
            break
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        if want and d.get("source_id") not in want:
            continue
        old = d["digest"].get("title") or ""
        try:
            r = llm.call_json(SYSTEM, brief(d) + "\n\n" + ASK,
                              max_tokens=400, temperature=0.2, role="review")
        except Exception as ex:
            log(f"  {old[:24]} 重出失败：{type(ex).__name__}")
            continue
        new = squeeze(str(r.get("title") or ""))
        if not new:
            continue
        done += 1
        mark = "＝" if new == old else "→"
        log(f"  旧 {old}")
        log(f"  {mark} {new}    〔{squeeze(str(r.get('why') or ''))[:40]}〕")
        if not a.dry_run and new != old:
            d["digest"]["title"] = new
            f.write_text(json.dumps(d, ensure_ascii=False, indent=1) + "\n")
    log(f"\n{'（dry-run，一个字没写）' if a.dry_run else '已写回'} 共 {done} 篇")
    if not a.dry_run and done:
        log("记得跑 python3 pipeline/build.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
