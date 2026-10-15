#!/usr/bin/env python3
"""该跟的源，最新一集有没有被落下 —— 本机日更每轮记一笔，体检读它。

**为什么要有它。** 2026-10-15 凌晨张小珺出了 #154，10:30 那一轮扫到了（「zhangxiaojun 1 new」），
8 个名额却全给了别人；是用户先问「张小珺更新了，你为啥还不更新」。原有的体检拿缓存里的
原平台日期去比、差 3 天才算、而且只是提醒 —— 它量的是「日期差」，不是「这一集被没被做」。
选集那一刻其实什么都知道：这一集看得见、合格、没被选。这里把它记下来。用户随后说
「不要再漏信息源」，所以「该跟」不只是钉住和 tier 1，也包括首页热门那一批（见 followed）。

只看**每档源最新的那一集**：问的是「这档源跟上了没有」，不是「积压清完了没有」——
日更节目窗口里有二十几集，按集记的话每轮都是红的。

data/followup.json：
  at        最后一次记账的时间（本机日更主轮）
  seed      只在随代码一起提交的那份种子里为 true：还没有本机主轮记过账
  src       {源 id: {eid, title, pub, first_seen, lost, tries, last, seen_at}}
            lost  = **连续**几轮看得见、没被选（被选中一次就清零）
            tries = 被选中处理过的轮数；last = 最近一轮的结果
  unseen    这一轮没量到的该跟的源（feed 失败，或不在本机这一轮的范围）
  aged_out  没做完就离开账本的集：掉出回溯窗口（window），或在红的时候被更新的一集顶掉
            （superseded）。后来做完了自动消掉；人决定不做了用 --ack。保留 14 天。
  acked     人确认过的 aged_out（eid → 理由）

    python3 pipeline/followup.py                   # 看账
    python3 pipeline/followup.py --ack EID --why "…"
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PATH = DATA / "followup.json"
KEEP_AGED_DAYS = 14
HOT_N = 24             # 和 build.HOT_N 一致：首页热门的格数

# 体检的线。都按「轮」和小时定，不按原平台日期：有的 feed 只给日期（#154 是 00:00:00Z），
# 本机一天只跑两轮，按 pubDate 算延迟会差出半天，天天狼来了。
LOST_FAIL = 2          # 看得见、合格，**连着**两轮没被选 —— 选集规则坏了或预算不够
STUCK_HOURS = 72       # 第一次看见之后三天还没做成（限流重试、评审没过、冷却中……）
STALE_HOURS = 30       # 账本 30 小时没更新 —— 本机主轮没跑完，判据等于被跳过
# 这一轮就有了结论的：发了，或者按规则判过不做（广告 / 和已发的重复）。这档源算跟上了。
TERMINAL = ("published", "duplicate", "off-brief")


_hot: dict = {"ids": None}


def hot_ids() -> set:
    """本站深读篇数最多的 HOT_N 档（不看最近有没有更新）。

    和首页热门（build.hot_sources）差一处：那边要求近 60 天有新稿，这里不要 ——
    停更两个月又回来的第一集，恰恰是最容易被落下的那一集。"""
    if _hot["ids"] is None:
        n: dict[str, int] = {}
        for f in (DATA / "episodes").glob("*.json"):
            parts = f.name.split("-")
            if len(parts) > 4:
                n[parts[3]] = n.get(parts[3], 0) + 1     # YYYY-MM-DD-<源>-<slug>.json
        _hot["ids"] = set(sorted(n, key=lambda k: (-n[k], k))[:HOT_N])
    return _hot["ids"]


def followed(s: dict) -> bool:
    """该跟的源：用户钉住的、tier 1、被降级之前是 tier 1 的（tier_base），以及首页热门那一批。

    tier_base 也算，是因为降级正是这次出事的方式：自动规则把张小珺从 1 降到 2，
    它就出了「核心源先取」。一条规则改了等级，不该连带改掉「这档源要不要跟」。"""
    return (bool(s.get("pinned")) or s.get("tier", 3) == 1 or s.get("tier_base") == 1
            or s.get("id") in hot_ids())


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s: str | None) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def load(path: pathlib.Path | None = None) -> dict | None:
    try:
        return json.loads((path or PATH).read_text())
    except Exception:
        return None


def _hours(e: dict, now: dt.datetime) -> float:
    return (now - (_parse(e.get("first_seen")) or now)).total_seconds() / 3600


def red(e: dict, now: dt.datetime) -> str | None:
    """这一条该不该红，红的话为什么。**归档旧集和体检共用这一个判定** ——
    原来归档只认 lost，卡了三天的旧集一被新集顶掉就悄悄变绿。"""
    t = f"「{str(e.get('title', ''))[:30]}」"
    if e.get("last") == "too-long":
        return f"{t} 因为太长被跳过 —— 该跟的源不该受 --max-words 限制"
    if e.get("lost", 0) >= LOST_FAIL:
        return f"{t} 看得见、合格，连着 {e['lost']} 轮没被选上 —— 选集规则或预算出了问题"
    h = _hours(e, now)
    if h > STUCK_HOURS:
        return (f"{t} 出现 {h:.0f} 小时还没做成（处理过 {e.get('tries', 0)} 次，"
                f"最近一次：{e.get('last')}）")
    return None


def update(prev: dict | None, *, newest: dict, cands: dict, outcomes: dict, done: dict,
           fail: dict, max_fails: int, srcs: dict, now: dt.datetime,
           cutoff: dt.datetime) -> dict:
    """算出这一轮之后的账。

    newest   {源 id: 这档源窗口内最新一集的 key}（这一轮 feed 取到了的源才有）
    cands    {key: 候选集}（这一轮合格、可以做的）
    outcomes {key: 结果}（这一轮被选中的：process() 的结果，崩了是 error，没轮到是 not-run）
    done     state["done"]；fail state["fail"]；max_fails = run.MAX_FAILS
    """
    prev = prev or {}
    old = prev.get("src") or {}
    acked = dict(prev.get("acked") or {})
    aged = [a for a in (prev.get("aged_out") or [])
            if a.get("eid") not in done and a.get("eid") not in acked
            and (_parse(a.get("at")) or now) > now - dt.timedelta(days=KEEP_AGED_DAYS)]
    have = {a.get("eid") for a in aged}

    def retire(e, sid, why):
        if e["eid"] not in have:
            aged.append(dict(e, sid=sid, at=_iso(now), why=why))
            have.add(e["eid"])

    out: dict[str, dict] = {}
    unseen = []
    for sid, s in srcs.items():
        if not followed(s):
            continue
        e = old.get(sid)
        if e and e["eid"] in done:
            e = None                       # 已经做完（这一轮或别的线做的）
        if sid not in newest:
            # 这一轮没量到（feed 失败，或不在本机这一轮的范围）：账原样留着，不涨也不清
            unseen.append(sid)
            if e:
                if (_parse(e.get("pub")) or now) < cutoff:
                    retire(e, sid, "window")
                else:
                    out[sid] = e
            continue
        key = newest[sid]
        if e and e["eid"] != key:
            if red(e, now):
                retire(e, sid, "superseded")     # 顶掉之前已经是红的 —— 别让「出了新集」把它洗白
            e = None
        if key in done:
            continue                       # 最新一集做过了（发了，或按规则判过不做）：跟上了
        ep = cands.get(key)
        if ep is None:
            f = fail.get(key) or {}
            if f.get("n", 0) >= max_fails:
                continue                   # 硬失败出局：归「再也不会被尝试、没人看过」那道检查管
            # 不在候选里、也没做：软失败撞满在冷却。**不许当成跟上了** —— 条目留着，
            # first_seen 不重置，三天的钟接着走（原来这里 continue，红一阵就自己绿了）
            e = e or {"eid": key, "title": "", "pub": None, "first_seen": _iso(now),
                      "lost": 0, "tries": 0, "last": None}
            tag = "cooling" if f.get("soft", 0) else "excluded"
            out[sid] = dict(e, seen_at=_iso(now), lost=0, last=f"{tag}:{f.get('why') or '?'}")
            continue
        if not e:
            e = {"eid": key, "title": str(ep.get("title") or "")[:120],
                 "pub": _iso(ep["published"]) if ep.get("published") else None,
                 "first_seen": _iso(now), "lost": 0, "tries": 0, "last": None}
        e = dict(e, seen_at=_iso(now))
        r = outcomes.get(key)
        if r is None:
            e["lost"] = e.get("lost", 0) + 1
            e["last"] = "not-picked"
        else:
            if r in TERMINAL:
                continue
            e["lost"] = 0                  # 选上了：不再是「没被选」，是「在重试」
            e["tries"] = e.get("tries", 0) + 1
            e["last"] = r
        out[sid] = e
    return {"at": _iso(now), "src": dict(sorted(out.items())), "unseen": sorted(unseen),
            "aged_out": aged, "acked": acked}


def judge(d: dict | None, now: dt.datetime) -> tuple[list[str], list[str]]:
    """体检用：返回 (硬伤, 提醒)。"""
    if d is None:
        return (["该跟的源有没有被落下，账本 data/followup.json 不存在 —— 判据被跳过不许算绿"
                 "（本机日更主轮带 --followup 才会写它）"], [])
    bad, note = [], []
    at = _parse(d.get("at"))
    if not at or now - at > dt.timedelta(hours=STALE_HOURS):
        bad.append(f"跟随账本 {STALE_HOURS} 小时没更新（上次 {d.get('at')}）—— 本机日更主轮没跑完，"
                   f"该跟的源有没有被落下没人在看")
    elif d.get("seed"):
        note.append("跟随账本是随代码提交的种子，本机日更主轮还没记过账")
    for sid, e in (d.get("src") or {}).items():
        why = red(e, now)
        t = f"{sid}「{str(e.get('title', ''))[:30]}」"
        if why:
            bad.append(f"{sid}{why}")
        elif e.get("last") == "not-picked":
            note.append(f"{t} 上一轮名额满了没排上（下一轮排第一）")
        elif str(e.get("last") or "").startswith("cooling:"):
            note.append(f"{t} 重试撞满在冷却（{e['last'][8:]}），{_hours(e, now):.0f} 小时了")
        elif str(e.get("last") or "").startswith("excluded:"):
            note.append(f"{t} 没做、也不在候选里（{e['last'][9:]}），{_hours(e, now):.0f} 小时了")
        elif e.get("tries"):
            note.append(f"{t} 处理过 {e['tries']} 次还没做成（最近一次：{e.get('last')}）")
    for a in d.get("aged_out") or []:
        why = "掉出回溯窗口" if a.get("why") == "window" else "红着的时候被更新的一集顶掉"
        bad.append(f"{a.get('sid')}「{str(a.get('title', ''))[:30]}」{why}，到现在还没做"
                   f"（要么用 --episode 补，要么 python3 pipeline/followup.py --ack {a.get('eid')} --why …）")
    if d.get("unseen"):
        note.append(f"{len(d['unseen'])} 档该跟的源这一轮没量到（feed 失败或不在本机范围）："
                    + "、".join(d["unseen"][:6]))
    return bad, note


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ack", metavar="EID", help="确认一条 aged_out：人看过了，决定不补")
    ap.add_argument("--why", default="")
    a = ap.parse_args(argv)
    d = load()
    if d is None:
        print("data/followup.json 不存在", file=sys.stderr)
        return 1
    if a.ack:
        if not a.why.strip():
            print("--ack 要写 --why：为什么不补", file=sys.stderr)
            return 2
        if a.ack not in {x.get("eid") for x in d.get("aged_out") or []}:
            print(f"aged_out 里没有 {a.ack}", file=sys.stderr)
            return 1
        d.setdefault("acked", {})[a.ack] = a.why.strip()
        d["aged_out"] = [x for x in d["aged_out"] if x.get("eid") != a.ack]
        PATH.write_text(json.dumps(d, ensure_ascii=False, indent=1) + "\n")
        print(f"已确认 {a.ack}：{a.why.strip()}")
        return 0
    bad, note = judge(d, dt.datetime.now(dt.timezone.utc))
    for m in bad:
        print("  坏了 ", m)
    for m in note:
        print("  注意 ", m)
    print(f"{len(d.get('src') or {})} 档在路上 · {len(bad)} 条硬伤")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
