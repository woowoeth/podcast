#!/usr/bin/env python3
"""收录一条只在 YouTube 上发的节目。

为什么需要这条路：管线是 feed 驱动的，而有些节目**只在频道上发**。
实测 Y Combinator：「Paul Graham On Startups, Ambition, and Great Founders」
（5bxp78i96S8，21:25）在频道上是最新一条，而 336 集的 RSS feed 里根本没有它
——全 feed 里和这个标题最相似的只有 0.18 分，是 5 月另一期 PG。这类内容我们
以前一集都收不到，而且没有任何信号告诉我们漏了。

它不绕过任何闸门：拼出一条和 feed 同形的条目，然后交给 run.process()，
选题闸门、文稿密度、机械校验、成稿评审四道原样跑。

    python3 pipeline/addvideo.py https://www.youtube.com/watch?v=XXX --source ycsp
    python3 pipeline/addvideo.py XXX --source ycsp --dry-run
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lib import net, transcript as T                              # noqa: E402
from lib.util import log                                          # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC_JSON = ROOT / "data" / "sources.json"

_ID = re.compile(r"(?:v=|youtu\.be/|/embed/|/shorts/)([\w-]{11})")


def video_id(s: str) -> str:
    m = _ID.search(s)
    if m:
        return m.group(1)
    return s if re.fullmatch(r"[\w-]{11}", s) else ""


def meta(vid: str) -> dict:
    """标题、时长、发布日、封面。

    先读 watch 页：批量跑的时候 yt-dlp 会撞 "Sign in to confirm you're not a
    bot"，而 watch 页不吃这个。yt-dlp 只作为补齐（页面结构变了时的第二条路）。
    """
    out: dict = {}
    try:
        h = net.get_text("https://www.youtube.com/watch?v=" + vid, timeout=25, tries=2,
                         headers={"Accept-Language": "en-US,en;q=0.9"})
    except Exception as ex:
        log(f"  watch 页取不到：{type(ex).__name__}")
        h = ""
    if h:
        def grab(pat):
            m = re.search(pat, h)
            return m.group(1) if m else ""
        out["title"] = (grab(r'"videoDetails":\{.*?"title":"(.*?)"')
                        or grab(r'<meta name="title" content="([^"]*)"'))
        secs = grab(r'"lengthSeconds":"(\d+)"')
        out["duration"] = int(secs) if secs else 0
        out["published"] = grab(r'"uploadDate":"(\d{4}-\d{2}-\d{2})')
        # 封面直接按 id 构造，不从页面里抠：抠出来的是 JSON 串里的值，带 \u0026
        # 之类的转义（我第一版就把 `\u0026` 原样写进了 og:image，链接是坏的），
        # 而且那张是带 sqp= 的小图，og 要的是大图。
        # maxresdefault 不是每个视频都有，hq720 也是 1280×720，取不到再退 hqdefault。
        out["image"] = f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg"
    if not (out.get("title") and out.get("duration")) and shutil.which("yt-dlp"):
        try:
            r = subprocess.run(
                ["yt-dlp", "--no-warnings", "--skip-download",
                 "--print", "%(title)s\t%(duration)s\t%(upload_date)s\t%(thumbnail)s",
                 "https://www.youtube.com/watch?v=" + vid],
                capture_output=True, text=True, timeout=90)
            p = (r.stdout or "").strip().split("\t")
            if len(p) >= 3 and p[0]:
                out.setdefault("title", p[0])
                out["title"] = out.get("title") or p[0]
                out["duration"] = out.get("duration") or int(float(p[1] or 0))
                if p[2] and len(p[2]) == 8:
                    out["published"] = out.get("published") or \
                        f"{p[2][:4]}-{p[2][4:6]}-{p[2][6:]}"
                # 封面不从 yt-dlp 取：上面已按 id 构造，那个更稳也更大
        except Exception as ex:
            log(f"  yt-dlp 补齐失败：{type(ex).__name__}")
    # 标题里可能带 \u 转义和 \" ——JSON 串里抠出来的，解一次
    if out.get("title"):
        try:
            out["title"] = json.loads('"%s"' % out["title"].replace('"', '\\"'))
        except Exception:
            pass
    return out


def already_have(vid: str) -> str:
    """这条视频是不是已经在站上了。两个判据：视频 id 直接命中，
    或者同一档节目已有一集时长对得上（feed 版和频道版标题常常不一样，
    实测 Max Hodak 那集 feed 叫 How Startups Build Speed、频道叫
    Average Is Not Good Enough，只按标题查会重复发一遍）。"""
    eps = ROOT / "data" / "episodes"
    for f in eps.glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        if d.get("youtube_id") == vid:
            return f"已收录（{d['slug']}）"
    return ""


def dup_by_duration(src_id: str, secs: int) -> str:
    if not secs:
        return ""
    for f in (ROOT / "data" / "episodes").glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        if d.get("source_id") != src_id or not d.get("duration"):
            continue
        if abs(int(d["duration"]) - secs) <= T.seek_tolerance(secs):
            return f"同一档节目里已有时长 {d['duration']}s 的一集（{d['slug']}）"
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video", help="YouTube 链接或 11 位 id")
    ap.add_argument("--source", required=True, help="信源 id，例如 ycsp")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不发布")
    ap.add_argument("--force", action="store_true", help="忽略重复判断")
    a = ap.parse_args()

    vid = video_id(a.video)
    if not vid:
        log("认不出视频 id"); return 2

    srcs = {s["id"]: s for s in json.loads(SRC_JSON.read_text())["sources"]}
    s = srcs.get(a.source)
    if not s:
        log(f"没有这个信源：{a.source}"); return 2

    m = meta(vid)
    if not m.get("title"):
        log("拿不到标题，放弃（别把一条没有标题的条目喂进管线）"); return 1
    log(f"{s['name']}｜{m['title']}")
    log(f"  {vid} · {m.get('duration') or '?'}s · {m.get('published') or '日期未知'}")

    if not a.force:
        for why in (already_have(vid), dup_by_duration(a.source, m.get("duration") or 0)):
            if why:
                log(f"  跳过：{why}"); return 0

    pub = m.get("published") or dt.datetime.now(dt.timezone.utc).date().isoformat()
    ep = {
        "source_id": s["id"], "source": s["name"],
        "guid": vid, "title": m["title"],
        "link": "https://www.youtube.com/watch?v=" + vid,
        "published": dt.datetime.fromisoformat(pub + "T12:00:00+00:00"),
        "duration": m.get("duration") or None,
        "audio": "", "image": m.get("image") or "", "notes": "",
        "episode_no": "", "explicit": False, "transcripts": [],
        "youtube_id": vid, "feed_kind": "youtube",
    }

    import run as R
    ep["_src"] = s
    ep["_key"] = R.eid(s["id"], vid)
    state = R.load_state()
    if ep["_key"] in state["done"] and not a.force:
        log(f"  跳过：这条已在 state 里（{state['done'][ep['_key']]}）"); return 0

    log("  交给管线，四道闸门原样跑")
    r = R.process(ep, state, dry=a.dry_run)
    log(f"  结果：{r}")
    if not a.dry_run:
        R.save_state(state)
        if r == "published":
            log("  记得跑 python3 pipeline/build.py")
    return 0 if r in ("published", "duplicate") else 1


if __name__ == "__main__":
    raise SystemExit(main())
