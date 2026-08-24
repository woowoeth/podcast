#!/usr/bin/env python3
"""给已发布的稿子补成稿评分，不到线的下站。

    python3 pipeline/rescore.py            # 全量重评
    python3 pipeline/rescore.py --min 8    # 指定及格线
    python3 pipeline/rescore.py --dry-run  # 只看分数，不动文件

评审需要原文，而原文不存在仓库里（第三方版权内容，不该整份存进公开仓库），
所以这里按原路重新取一次文稿。feed/notes/page 三层有磁盘缓存，很快；
youtube 层只有住宅 IP 能取，所以云端跑的时候那几篇会跳过而不是误判。

下站不是删除：记录移到 data/retired/，附上分数和理由，随时可查可回。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lib import feeds, llm, review, transcript as T                # noqa: E402
from lib.util import iso, log, now                                 # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
EPS = ROOT / "data" / "episodes"
RETIRED = ROOT / "data" / "retired"
STATE = ROOT / "data" / "state.json"


def find_episode(rec: dict, src: dict) -> dict | None:
    """在信源的 feed 里找回这一集（按 guid，退回按标题）。"""
    try:
        eps = feeds.fetch(src, cache_ttl=7200)
    except Exception as ex:
        log(f"    取 feed 失败：{type(ex).__name__}")
        return None
    for e in eps:
        if e["guid"] == rec.get("id", "").split("-", 1)[-1] or e["title"] == rec.get("title_original"):
            return e
    for e in eps:
        if e["title"].strip() == (rec.get("title_original") or "").strip():
            return e
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=float, default=review.MIN_SCORE)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tiers", default="", help="限制取稿层，云端应设 feed,notes,page")
    ap.add_argument("--only", help="只重评某个信源")
    a = ap.parse_args()

    if not llm.available():
        log("没有可用的模型后端，无法评分")
        return 2
    allow = tuple(t.strip() for t in a.tiers.split(",") if t.strip() in T.ORDER) or T.ORDER
    srcs = {s["id"]: s for s in json.loads((ROOT / "data" / "sources.json").read_text())["sources"]}
    files = sorted(EPS.glob("*.json"))
    log(f"重评 {len(files)} 篇 · 及格线 {a.min} · 取稿层 {', '.join(allow)} · "
        f"{llm.provider()}:{llm.model_name()}")

    kept, dropped, skipped = [], [], []
    for f in files:
        rec = json.loads(f.read_text())
        d = rec["digest"]
        if a.only and rec["source_id"] != a.only:
            continue
        title = d["title"][:34]
        if rec.get("review") and rec["review"].get("score") is not None:
            sc = rec["review"]["score"]
            if sc >= a.min:
                log(f"  已有 {sc:.0f} 分 · {title}")
                kept.append(f.name)
                continue
        src = srcs.get(rec["source_id"])
        if not src:
            skipped.append((f.name, "信源已不在册"))
            continue
        ep = find_episode(rec, src)
        if not ep:
            log(f"  ? 找不到原集，跳过 · {title}")
            skipped.append((f.name, "feed 里找不到这一集"))
            continue
        tr = T.acquire(ep, src.get("lang", "en"), allow=allow, src=src)
        if not tr:
            log(f"  ? 取不到原文，跳过（不当作不合格）· {title}")
            skipped.append((f.name, "重取原文失败"))
            continue
        rv = review.check(d, tr, ep, src)
        if rv is None:
            skipped.append((f.name, "评分调用失败"))
            continue
        dims = " ".join(f"{k[:4]}{v}" for k, v in rv["dims"].items() if v is not None)
        verdict = "保留" if rv["score"] >= a.min else "下站"
        log(f"  {rv['score']:.0f}/10 [{dims}] {verdict} · {title}")
        log(f"        {rv['why']}")
        rec["review"] = rv
        if a.dry_run:
            continue
        if rv["score"] >= a.min:
            f.write_text(json.dumps(rec, ensure_ascii=False, indent=1) + "\n")
            kept.append(f.name)
        else:
            RETIRED.mkdir(parents=True, exist_ok=True)
            rec["retired"] = {"at": iso(now()), "reason": f"成稿评分 {rv['score']:.0f} < {a.min}"}
            (RETIRED / f.name).write_text(json.dumps(rec, ensure_ascii=False, indent=1) + "\n")
            f.unlink()
            dropped.append((f.name, rv["score"], rv["why"]))
            # 记进 state，避免下一轮又被重新做一遍
            st = json.loads(STATE.read_text()) if STATE.exists() else {"done": {}, "fp": {}, "fail": {}}
            st["done"][rec["id"]] = {"skip": "below-bar", "score": rv["score"],
                                     "why": rv["why"], "at": iso(now())}
            STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1, sort_keys=True) + "\n")

    log(f"\n保留 {len(kept)} · 下站 {len(dropped)} · 跳过 {len(skipped)}")
    for n, sc, why in dropped:
        log(f"  下站 {sc:.0f} 分 · {n[:60]} · {why}")
    for n, r in skipped:
        log(f"  跳过 · {n[:60]} · {r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
