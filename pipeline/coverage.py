#!/usr/bin/env python3
"""每一档信源为什么有／没有产出。

为什么需要它：163 档里 67 档一篇都没出过，而**这 67 档看起来是一样的**——
"源自己三个月没更新"和"我们的抓取坏了"在任何一处输出里都长得一模一样。
于是新加一档源、它悄悄不工作，没有任何人会知道。

分五类，每一类对应一个不同的动作：

  抓取坏了        体检过、feed 拉不动。**要修**，这是我们的问题。
  从没体检过      status.ok 是 None。跑 resolve_sources.py --check。
                  这一类和"坏了"必须分开：我第一版混在一起，把 8 档好的
                  源报成了坏的，而它们要的动作完全不同。
  从没被尝试过    feed 是新鲜的，却连一条 fail 记录都没有。**要查**——
                  云端只跑 feed/notes/page 三层，拿不到文稿的源在云端
                  结构上不可达，只有本机那条线（带 asr）才碰得到。
  拿不到文稿      试过，没有可核对的文稿。按设计不发，不是故障。
  闸门没过        试过，选题或评审判不合格。按设计不发。
  源自己安静了    feed 好的，源自己很久没更新。不是我们的事。

单独跑：python3 pipeline/coverage.py（加 --json 给别的脚本用）
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# feed 好、却这么久没更新，就算源自己安静了：至少 60 天，且超过它自己
# 三个更新周期——只看天数会把季播节目误判成故障。
QUIET_MIN_DAYS = 60
QUIET_CADENCE_MULT = 3


def classify() -> dict:
    srcs = json.loads((DATA / "sources.json").read_text())["sources"]
    state = json.loads((DATA / "state.json").read_text())
    fails: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for v in state.get("fail", {}).values():
        fails[v.get("src")][v.get("why") or "?"] += 1
    produced = collections.Counter()
    newest: dict[str, str] = {}
    for f in (DATA / "episodes").glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        sid = d.get("source_id")
        produced[sid] += 1
        pub = (d.get("published") or "")[:10]
        if pub > newest.get(sid, ""):
            newest[sid] = pub

    out: dict[str, list] = collections.defaultdict(list)
    for s in srcs:
        sid = s["id"]
        st = s.get("status") or {}
        age, cad = st.get("age_days"), st.get("cadence_days") or 30
        why = fails.get(sid)
        row = {"id": sid, "name": s.get("zh") or s.get("name"),
               "tier": s.get("tier"), "cat": s.get("cat"),
               "residential": bool(s.get("residential")),
               "episodes": produced[sid], "latest_ours": newest.get(sid),
               "feed_age_days": age, "cadence_days": cad,
               "tries": sum(why.values()) if why else 0}
        if produced[sid]:
            out["有产出"].append(row)
        elif st.get("ok") is None:
            # **ok=None 是"从没体检过"，不是"坏了"。** 我第一版把
            # `not st.get("ok")` 当成坏，于是把 8 档从没体检过的源报成
            # "feed 拉不动"——实测它们全都拉得动（zeroknowledge 有 421 集）。
            # 两者要的动作完全不同：一个是修抓取，一个是跑体检。
            row["detail"] = "从没体检过，跑 resolve_sources.py --check"
            out["从没体检过"].append(row)
        elif not st.get("ok"):
            row["detail"] = st.get("err") or "feed 拉不动"
            out["抓取坏了"].append(row)
        elif why:
            top = why.most_common(1)[0][0]
            row["detail"] = top
            out["拿不到文稿" if "transcript" in top else "闸门没过"].append(row)
        elif age is not None and age > max(QUIET_MIN_DAYS, cad * QUIET_CADENCE_MULT):
            row["detail"] = f"{age:.0f} 天没更新（周期 {cad:.0f} 天）"
            out["源自己安静了"].append(row)
        else:
            row["detail"] = (f"feed {age:.0f} 天前还在更新（周期 {cad:.0f} 天）"
                             if age is not None else "feed 时间未知")
            out["从没被尝试过"].append(row)
    return out


# 这两类是"我们的问题"，要能被别的脚本直接问
def broken(c: dict) -> list:
    return c.get("抓取坏了") or []


def untried(c: dict) -> list:
    return (c.get("从没被尝试过") or []) + (c.get("从没体检过") or [])



# ---------------------------------------------------------------- 实探

def probe_untried(rows: list[dict], days: int = 21) -> list[dict]:
    """对"从没被尝试过"的源真去拉一次 feed，把原因**算出来**。

    为什么不写成一张"已知不可达"的名单：名单是断言，会过期，而且下一个人
    看不出它当初为什么在名单上。这里判据落在**跑批用的同一组过滤器**上，
    所以原因是推导出来的：

      Sharp Tech 是 tier1、每周更新、feed 好的，却一集都没出过。实探之后
      一句话就说清了：它公开 feed 里**每一集标题都是 (Preview)**——订阅制
      节目只放试听片段，SKIP_TITLE 拦得对。名单写不出这个，实探能。
    """
    sys.path.insert(0, str(ROOT / "pipeline"))
    from lib import feeds                                       # noqa: E402
    import run as R                                             # noqa: E402
    state = R.load_state()
    cut = R.now() - dt.timedelta(days=days)
    out = []
    for row in rows:
        srcs = {x["id"]: x for x in
                json.loads((DATA / "sources.json").read_text())["sources"]}
        s = srcs.get(row["id"])
        if not s:
            continue
        try:
            eps = feeds.fetch(s, cache_ttl=1800)
        except Exception as ex:
            row["probe"] = f"feed 拉不动：{type(ex).__name__}"
            out.append(row)
            continue
        # 窗口外的集**不进**这个计数器。第一版把它们混在一起，于是
        # "最常见的原因"永远是"超出回溯窗口"（25 集里通常 22 集在窗口外），
        # 把窗口内那 3 集真正被挡的原因盖掉了——Sharp Tech 的真原因是
        # 每集标题都带 (Preview)，而它报的是"超出回溯窗口"。
        why: collections.Counter = collections.Counter()
        fresh = old = 0
        for ep in eps[:25]:
            pub = ep.get("published")
            if not pub or pub < cut:
                old += 1
                continue
            fresh += 1
            if R.eid(s["id"], ep["guid"]) in state["done"]:
                why["已发过"] += 1
                continue
            low = (ep.get("title") or "").lower()
            hit = [w for w in R.SKIP_TITLE if w in low]
            if hit:
                why[f"标题命中 {hit[0]}"] += 1
                continue
            d = ep.get("duration")
            if d and d < R.MIN_SECONDS:
                why[f"时长不足 {R.MIN_SECONDS}s"] += 1
                continue
            why["能进候选"] += 1
        if not fresh:
            row["probe"] = f"回溯窗口内没有新集（窗口外 {old} 集）"
        elif why.get("能进候选"):
            row["probe"] = (f"{why['能进候选']} 集能进候选 —— "
                            f"**它本该被抓到，去查跑批为什么没碰它**")
        else:
            top = why.most_common(1)
            row["probe"] = (f"窗口内 {fresh} 集全被挡：{top[0][0]}（{top[0][1]} 集）"
                            if top else "窗口内的集全被挡，原因未知")
        out.append(row)
    return out


ORDER = ["抓取坏了", "从没体检过", "从没被尝试过", "拿不到文稿", "闸门没过",
         "源自己安静了", "有产出"]


def main() -> int:
    ap = argparse.ArgumentParser(description="每档信源为什么有/没有产出")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--only", help="只看这几类，逗号分隔")
    ap.add_argument("--probe", action="store_true",
                    help="对「从没被尝试过」的源真拉一次 feed，把原因算出来（要联网）")
    ap.add_argument("--days", type=int, default=21, help="实探用的回溯天数")
    ap.add_argument("--write", action="store_true",
                    help="把实探结果写进 data/coverage.json，供体检读")
    a = ap.parse_args()
    c = classify()
    if a.probe:
        rows = probe_untried(untried(c), a.days)
        for r in sorted(rows, key=lambda r: (r.get("tier") or 9, r["id"])):
            print(f"  T{r['tier']} {r['id']:18} {r['probe']}")
        should = [r for r in rows if "本该被抓到" in (r.get("probe") or "")]
        print(f"\n实探 {len(rows)} 档 · 其中 {len(should)} 档本该被抓到"
              + (f"：{'、'.join(x['id'] for x in should)}" if should else ""))
        if a.write:
            (DATA / "coverage.json").write_text(json.dumps(
                {"at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "days": a.days,
                 "untried": {r["id"]: r["probe"] for r in rows}},
                ensure_ascii=False, indent=1) + "\n")
            print(f"写入 {(DATA / 'coverage.json').relative_to(ROOT)}")
        return 1 if should else 0
    if a.json:
        print(json.dumps(c, ensure_ascii=False, indent=1))
        return 0
    keys = a.only.split(",") if a.only else ORDER
    total = sum(len(v) for v in c.values())
    for k in keys:
        rows = c.get(k) or []
        if not rows:
            continue
        print(f"\n{k}（{len(rows)}/{total}）")
        rows.sort(key=lambda r: (r.get("tier") or 9, r["id"]))
        for r in rows if k != "有产出" else rows[:0]:
            res = " 住宅IP" if r["residential"] else ""
            print(f"  T{r['tier']} {r['id']:18}{res:6} {r.get('detail','')}")
    n_b, n_u = len(broken(c)), len(untried(c))
    t1 = [r["id"] for r in untried(c) if r["tier"] == 1]
    print(f"\n有产出 {len(c.get('有产出') or [])}/{total} 档 · "
          f"抓取坏了 {n_b} 档 · 从没被尝试过 {n_u} 档"
          + (f"（其中 tier1：{'、'.join(t1)}）" if t1 else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
