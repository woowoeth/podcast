#!/usr/bin/env python3
"""从 YouTube 频道找信源：解析 channel_id → 建 feed → **验字幕** → 出候选。

    python3 pipeline/ytsource.py ycombinator lennyspodcast ...
    python3 pipeline/ytsource.py --json handles.json --out cand.json

为什么把重心放在 YouTube：**字幕是免费的**。全站 158 篇文稿走 ASR（每集都要
花钱转写），而 kind=youtube 的源基本都从字幕拿稿。同一档节目，走 YouTube
比走播客 RSS 便宜一个数量级，而且不依赖节目方有没有做文字版。

这一支只解决"找得到、拿得到"，**不判好不好**——好不好交给
`curate.py --from-feeds`，那边的评分标准（密度／补位／可核对）是全站统一的，
不该在这里另起一套。

输出的候选 JSON 直接喂给：
    python3 pipeline/curate.py --discover 7 --from-feeds cand.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lib import feeds, net                                      # noqa: E402
from lib import transcript as T                                 # noqa: E402
from lib.util import log                                        # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
FEED = "https://www.youtube.com/feeds/videos.xml?channel_id=%s"
CHANNEL_ID = re.compile(r'"channelId":"(UC[\w-]{20,})"')
CHANNEL_ID2 = re.compile(r'channel_id=(UC[\w-]{20,})')
SAMPLE = 3
# 字幕够密才算数。一集一小时的访谈几千词起步；几百词的是自动生成的标题描述。
MIN_WORDS = 1500


def resolve(handle: str) -> tuple[str | None, str]:
    """频道句柄 / 频道页地址 → channel_id。"""
    h = handle.strip()
    m = CHANNEL_ID2.search(h) or re.search(r"(UC[\w-]{20,})", h)
    if m:
        return m.group(1), "直接给了 id"
    h = re.sub(r"^https?://(www\.)?youtube\.com/", "", h).lstrip("@/")
    h = h.split("/")[0].split("?")[0]
    for url in (f"https://www.youtube.com/@{h}",
                f"https://www.youtube.com/c/{h}",
                f"https://www.youtube.com/user/{h}"):
        try:
            html = net.get(url, timeout=20)
            if isinstance(html, bytes):
                html = html.decode("utf-8", "replace")
        except Exception:
            continue
        m = CHANNEL_ID.search(html) or CHANNEL_ID2.search(html)
        if m:
            return m.group(1), url
    # **按名字搜。** 上面三条路都要求你已经知道句柄；而人给的通常是显示名
    # （「小Lin说」「硅谷徐老师」），中文频道尤其如此——实测一批 23 个里
    # 18 个卡在这里。用 yt-dlp 搜一支该频道的视频，再从视频取 channel_id。
    return _search_channel(h)


def _search_channel(name: str) -> tuple[str | None, str]:
    import subprocess
    try:
        r = subprocess.run(
            ["yt-dlp", f"ytsearch3:{name}", "--flat-playlist", "--no-warnings",
             "--socket-timeout", "30", "--print", "%(channel_id)s\t%(channel)s"],
            capture_output=True, text=True, timeout=180)
    except Exception:
        return None, ""
    import unicodedata

    def norm(x: str) -> str:
        return "".join(c for c in unicodedata.normalize("NFKC", x or "").lower()
                       if c.isalnum())
    want = norm(name)
    for line in (r.stdout or "").strip().split("\n"):
        parts = line.split("\t")
        if len(parts) < 2 or not parts[0].startswith("UC"):
            continue
        cid, chan = parts[0], parts[1]
        # **频道名要对得上**，否则搜「小Lin说」可能返回某个转载号。
        # 宽松匹配：一方包含另一方即可（频道名常带后缀）。
        c = norm(chan)
        if want and c and (want in c or c in want):
            return cid, f"搜索命中频道「{chan}」"
    return None, ""


def _known_channels() -> dict:
    out = {}
    try:
        blob = json.loads((DATA / "sources.json").read_text())
    except Exception:
        return out
    for s in blob.get("sources") or []:
        m = CHANNEL_ID2.search(s.get("feed") or "")
        if m:
            out[m.group(1)] = s["id"]
    return out


def check(handle: str, cat: str = "ai", captions: bool = True) -> dict:
    row = {"handle": handle, "verdict": "找不到频道", "note": ""}
    cid, how = resolve(handle)
    if not cid:
        row["note"] = "解析不出 channel_id（句柄拼写？频道页改版？）"
        return row
    row["channel_id"] = cid
    known = _known_channels()
    if cid in known:
        row.update(verdict="已在册", note=f"id={known[cid]}")
        return row
    feed = FEED % cid
    row["feed"] = feed
    src = {"id": "_probe", "name": handle, "cat": cat, "lang": "en",
           "kind": "youtube", "tier": 2, "feed": feed}
    try:
        eps = feeds.fetch(src, cache_ttl=0)
    except Exception as ex:
        row.update(verdict="feed 拉不动", note=f"{type(ex).__name__}")
        return row
    if not eps:
        row.update(verdict="feed 是空的")
        return row
    def d10(x):
        return x.date().isoformat() if isinstance(x, dt.datetime) else str(x)[:10]
    latest = max((d10(e.get("published")) for e in eps if e.get("published")),
                 default="")
    row["name"] = (eps[0].get("author") or eps[0].get("source_name")
                   or handle)
    row["episodes"] = len(eps)
    row["latest"] = latest
    try:
        age = (dt.date.today() - dt.date.fromisoformat(latest)).days
    except Exception:
        age = 999
    row["age_days"] = age
    if age > 60:
        row.update(verdict="太安静", note=f"{age} 天没更新")
        return row
    if not captions:
        # 云端机房 IP 取不到 YouTube 字幕（会被判成机器人索要 cookie），
        # 在那里验字幕只会全部报「没有字幕」——那是噪音不是信号。
        # 所以云端只查「频道还在不在、还更不更新」，字幕留给本机线。
        row.update(verdict="频道健康", note=f"{len(eps)} 支 · 最新 {latest}"
                                          f"（没验字幕）")
        return row
    # **验字幕**：这才是走 YouTube 的全部理由
    got, words = 0, 0
    for ep in eps[:SAMPLE]:
        vid = ep.get("youtube_id") or ep.get("guid", "").split(":")[-1]
        if not vid:
            continue
        try:
            tr = T.from_youtube(vid, "en")
        except Exception:
            tr = None
        if tr and tr.get("segments"):
            n = sum(len((s.get("text") or "").split()) for s in tr["segments"])
            if n >= MIN_WORDS:
                got += 1
                words = max(words, n)
    row["captions"] = f"{got}/{min(SAMPLE, len(eps))}"
    if got:
        row.update(verdict="可收",
                   note=f"{got}/{min(SAMPLE, len(eps))} 支视频有够密的字幕"
                        f"（最多 {words} 词）")
    else:
        row.update(verdict="没有字幕",
                   note="抽查的视频都没有够密的字幕 —— 走 YouTube 的理由就没了")
    return row


ORDER = ["可收", "频道健康", "没有字幕", "太安静", "已在册",
         "feed 拉不动", "feed 是空的", "找不到频道"]


def main() -> int:
    ap = argparse.ArgumentParser(description="从 YouTube 频道找信源")
    ap.add_argument("handles", nargs="*", help="频道句柄、@handle 或频道页地址")
    ap.add_argument("--json", help="从 JSON 读句柄列表")
    ap.add_argument("--cat", default="ai")
    ap.add_argument("--out", help="把「可收」的写成 curate --from-feeds 的输入")
    ap.add_argument("--no-captions", action="store_true",
                    help="只查频道在不在、更新健不健康，不验字幕（云端用："
                         "机房 IP 取不到字幕，验了只会全报没有）")
    a = ap.parse_args()
    todo = list(a.handles)
    if a.json:
        todo += json.loads(pathlib.Path(a.json).read_text())
    if not todo:
        ap.error("给几个频道句柄，或 --json")
    rows = []
    for i, h in enumerate(todo, 1):
        log(f"[{i}/{len(todo)}] {h}")
        r = check(h, a.cat, captions=not a.no_captions)
        rows.append(r)
        print(f"    {r['verdict']:10} {r.get('note', '')}")
    unknown = sorted({r["verdict"] for r in rows} - set(ORDER))
    if unknown:
        print(f"\n⚠ 这些结论不在 ORDER 里：{unknown}")
        ORDER.extend(unknown)
    print("\n" + "=" * 58)
    for v in ORDER:
        got = [r for r in rows if r["verdict"] == v]
        if not got:
            continue
        print(f"\n{v}（{len(got)}）")
        for r in got:
            ex = (f" · {r.get('episodes')} 支 · 最新 {r.get('latest')}"
                  if r.get("episodes") else "")
            print(f"  {r['handle'][:34]:36}{ex}")
            if r.get("note"):
                print(f"      {r['note']}")
    ok = [{"name": r.get("name") or r["handle"], "feed": r["feed"]}
          for r in rows if r["verdict"] == "可收"]
    if a.out:
        pathlib.Path(a.out).write_text(
            json.dumps(ok, ensure_ascii=False, indent=1) + "\n")
        print(f"\n{len(ok)} 档可收 → {a.out}")
        print(f"评分并入库：python3 pipeline/curate.py --discover 7 "
              f"--from-feeds {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
