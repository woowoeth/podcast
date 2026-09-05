#!/usr/bin/env python3
"""给候选信源做入库前体检：**它能不能真的产出内容**。

    python3 pipeline/evalsource.py "Onboard!" "忽左忽右" ...
    python3 pipeline/evalsource.py --feed https://example.com/rss
    python3 pipeline/evalsource.py --json 候选.json

为什么要单独一支：`curate.py --discover` 判的是"这档节目值不值得看"，
判不了"我们取不取得到它的文稿"。而后者才是这个站的硬约束——库里现在有
20 档因为拿不到文稿一篇都产不出，它们占着名额、在覆盖率报表上和好源
长得一模一样。加新源之前不验这一条，等于继续往那张名单上加。

用**跑批同一套文稿层**来判（feed → notes → page → youtube → asr），所以
这里的结论和日更那天会做的判断是同一个，不是另起一套近似。

asr 那一层尤其不能漏：已发布的中文集里 33/36 篇的文稿来自本机转写，
第一版没有它，把「高能量」「不合时宜」这些和已收录中文源形状完全一样的
节目判成了「取不到文稿」。

输出四档：
  可收        近期有更新，而且抽查的集里取得到够密的文稿
  取不到文稿  节目可能很好，但我们写不出可核对的稿子 —— 收了也是零产出
  太安静      feed 好的，但很久没更新
  找不到      iTunes 搜不到，或 feed 拉不动
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lib import feeds                                            # noqa: E402
from lib import transcript as T                                  # noqa: E402
from lib.util import log                                         # noqa: E402
import resolve_sources as RS                                     # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
# 抽查几集。1 集不够：某一集恰好没文稿说明不了什么；抽 3 集能分开
# "这档没有文稿"和"这一集碰巧没有"。
SAMPLE = 3
# 够密才算拿得到：一集 40 分钟的访谈至少该有几千词，几百词的是节目简介。
MIN_WORDS = 1200


def _known() -> dict:
    try:
        blob = json.loads((DATA / "sources.json").read_text())
    except Exception:
        return {}
    out = {}
    for s in blob.get("sources") or []:
        out[(s.get("zh") or s["name"]).lower()] = s["id"]
        if s.get("feed"):
            out[s["feed"]] = s["id"]
    return out


def _transcript_for(ep: dict, src: dict) -> tuple[str, int]:
    """跑批那套文稿层，按同样的顺序试一遍。返回（层名, 词数）。"""
    lang = src.get("lang") or "en"
    for name, fn in (("feed", lambda: T.from_feed(ep)),
                     ("notes", lambda: T.from_notes(ep, lang)),
                     ("page", lambda: T.from_page(ep, lang))):
        try:
            got = fn()
        except Exception:
            got = None
        if got and got.get("segments"):
            n = sum(len((s.get("text") or "").split()) or
                    len(s.get("text") or "") // 2 for s in got["segments"])
            if n >= MIN_WORDS:
                return name, n
    # youtube 这一层需要住宅 IP，这里只报"可能有"，不下结论
    try:
        vid = T.match_youtube(ep, src)
    except Exception:
        vid = None
    if vid:
        return "youtube?", 0
    return "", 0


def evaluate(name: str | None = None, feed: str | None = None,
             cat: str = "biz", lang: str = "en") -> dict:
    row = {"name": name or feed, "feed": feed, "verdict": "找不到", "note": ""}
    known = _known()
    if name and name.lower() in known:
        row.update(verdict="已在册", note=f"id={known[name.lower()]}")
        return row
    src = {"id": "_probe", "name": name or "?", "cat": cat, "lang": lang,
           "kind": "rss", "tier": 2}
    if feed:
        src["feed"] = feed
    else:
        meta = RS.itunes_find(name)
        if not meta or not meta.get("feedUrl"):
            row["note"] = "iTunes 搜不到"
            return row
        src["feed"] = meta["feedUrl"]
        row["feed"] = meta["feedUrl"]
        row["itunes"] = meta.get("collectionId")
        row["name"] = meta.get("collectionName") or name
    if src["feed"] in known:
        row.update(verdict="已在册", note=f"id={known[src['feed']]}")
        return row
    try:
        eps = feeds.fetch(src, cache_ttl=0)
    except Exception as ex:
        row["note"] = f"feed 拉不动：{type(ex).__name__}"
        return row
    if not eps:
        row["note"] = "feed 是空的"
        return row
    def d10(x):
        return x.date().isoformat() if isinstance(x, dt.datetime) else str(x)[:10]
    latest = max((d10(e.get("published")) for e in eps if e.get("published")),
                 default="")
    row["episodes"] = len(eps)
    row["latest"] = latest
    try:
        age = (dt.date.today() - dt.date.fromisoformat(latest)).days
    except Exception:
        age = 999
    row["age_days"] = age
    if age > 90:
        row.update(verdict="太安静", note=f"{age} 天没更新")
        return row
    hits = []
    for ep in eps[:SAMPLE]:
        tier, n = _transcript_for(ep, src)
        hits.append((tier, n, (ep.get("title") or "")[:40]))
    good = [h for h in hits if h[0] and h[0] != "youtube?"]
    maybe = [h for h in hits if h[0] == "youtube?"]
    # **ASR 也是一条正常路径，而且是中文源的主路径。** 已发布的中文集里
    # 33/36 篇的文稿来自本机转写，只有 2 篇来自 YouTube、1 篇来自 feed。
    # 第一版的尺子只试 feed/notes/page/youtube，于是把「高能量」「不合时宜」
    # 这些和已收录中文源形状完全一样的节目判成了「取不到文稿」——
    # 差点按这个结论把它们排除掉。
    # 层名和 quality.transcript_source 里的字面值保持一致（feed / notes /
    # page / youtube / asr），守护按站上真实用过的层反查这个文件。
    audio = [e for e in eps[:SAMPLE] if e.get("audio")]
    ASR = "asr"
    row["samples"] = hits
    if not good and not maybe and audio:
        row.update(verdict=f"可收（走 {ASR} 转写）",
                   note=f"三层都没有现成文稿，但 {len(audio)}/{len(eps[:SAMPLE])} 集"
                        f"有可下载音频 —— 和已收录的中文源同一条路（本机线 ASR，"
                        f"每集有转写成本）")
        return row
    if good:
        # 「可收」也要把更新频率说出来：跑批的回溯窗口只有几天到几周，
        # 一个 60 天更新一次的源就算文稿齐全也很少被抓到（bg2 就是这样，
        # 状态写着 no-transcript，其实是它自己 84 天没更新）。
        stale = f"，但已 {age} 天没更新" if age > 45 else ""
        row.update(verdict="可收",
                   note=f"{len(good)}/{len(hits)} 集取到文稿"
                        f"（{good[0][0]} 层，{good[0][1]} 词）{stale}")
    elif maybe:
        row.update(verdict="要住宅 IP",
                   note=f"只有 YouTube 那一层可能有，云端取不到；"
                        f"本机线（住宅 IP）才够得着")
    else:
        row.update(verdict="取不到文稿",
                   note=f"抽查 {len(hits)} 集，feed/notes/page 三层都没有够密的文稿")
    return row


# 顺序表要覆盖 evaluate() 会给出的**每一个** verdict，漏一个那一档就
# 整个不打印——「可收（走转写）」第一版就漏了，三档候选凭空消失。
ORDER = ["可收", "可收（走 asr 转写）", "要住宅 IP", "取不到文稿",
         "太安静", "已在册", "找不到"]


def main() -> int:
    ap = argparse.ArgumentParser(description="候选信源入库前体检")
    ap.add_argument("names", nargs="*", help="节目名，按 iTunes 搜")
    ap.add_argument("--feed", action="append", default=[], help="直接给 feed 地址")
    ap.add_argument("--json", help="从 JSON 读候选：[{name, cat, lang}]")
    ap.add_argument("--cat", default="biz")
    ap.add_argument("--lang", default="en")
    ap.add_argument("--out", help="结果写到这个 JSON")
    a = ap.parse_args()
    todo = [{"name": n, "cat": a.cat, "lang": a.lang} for n in a.names]
    todo += [{"feed": f, "cat": a.cat, "lang": a.lang} for f in a.feed]
    if a.json:
        todo += json.loads(pathlib.Path(a.json).read_text())
    if not todo:
        ap.error("给几个节目名，或 --feed / --json")
    rows = []
    for i, c in enumerate(todo, 1):
        log(f"[{i}/{len(todo)}] {c.get('name') or c.get('feed')}")
        r = evaluate(c.get("name"), c.get("feed"),
                     c.get("cat", a.cat), c.get("lang", a.lang))
        rows.append(r)
        print(f"    {r['verdict']:10} {r.get('note','')}")
    unknown = sorted({r["verdict"] for r in rows} - set(ORDER))
    if unknown:
        print(f"\n⚠ 这些结论不在 ORDER 里，会被漏掉：{unknown}")
        ORDER.extend(unknown)
    print("\n" + "=" * 60)
    for v in ORDER:
        got = [r for r in rows if r["verdict"] == v]
        if not got:
            continue
        print(f"\n{v}（{len(got)}）")
        for r in got:
            extra = (f" · {r.get('episodes')} 集 · 最新 {r.get('latest')}"
                     if r.get("episodes") else "")
            print(f"  {r['name'][:38]:40}{extra}")
            if r.get("note"):
                print(f"      {r['note']}")
            if r.get("feed"):
                print(f"      {r['feed'][:88]}")
    if a.out:
        pathlib.Path(a.out).write_text(
            json.dumps(rows, ensure_ascii=False, indent=1) + "\n")
        print(f"\n写入 {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
