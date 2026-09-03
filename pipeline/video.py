#!/usr/bin/env python3
"""给已发布的每一集核对／补上可跳时间戳的视频。

两件事，都是同一条判据的两面：

  --audit  查已经挂着的视频。频道 Atom feed 不带时长，match_youtube 只比标题，
           于是片花、预告、剪辑版都能通过。线上实测 25 篇里 14 篇挂错了——
           80,000 Hours 挂了 176 秒的片花（音频 2968 秒）、Lenny's 挂了 133 秒、
           Acquired 挂了 1674 秒对 14360 秒的正片。这些页面上每个时间戳都跳错。

  --find   给只有音频的那些找视频。用户的问题是"为啥看不了视频只有音频"，真实
           原因是：视频只在**文稿需要它**的时候才去找，而 165 档信源里只有 43 档
           填了 YouTube 频道 id。这里主动找一遍。

两件事共用 transcript.video_aligned：**时间戳不是从视频字幕来的，视频时长就必须
和音频时长对得上**，差得多就是另一个剪辑，宁可不给视频。

    python3 pipeline/video.py --audit --dry-run
    python3 pipeline/video.py --audit
    python3 pipeline/video.py --find --dry-run --limit 20
    python3 pipeline/video.py --find
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import pathlib
import sys
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lib import transcript as T                                   # noqa: E402
from lib.util import log                                          # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
EPS = ROOT / "data" / "episodes"

_LOCK = threading.Lock()


def load_sources() -> dict:
    try:
        raw = json.loads((ROOT / "data" / "sources.json").read_text())
    except Exception:
        return {}
    return {s["id"]: s for s in raw.get("sources") or []}


def tier_of(d: dict) -> str:
    return ((d.get("digest") or {}).get("quality") or {}).get("transcript_source", "")


def label(d: dict) -> str:
    return f"{(d.get('source_zh') or d.get('source') or '')[:20]} · " \
           f"{((d.get('digest') or {}).get('title') or '')[:30]}"


# --------------------------------------------------------------------- audit

def audit(files: list[pathlib.Path], dry: bool, limit: int = 0) -> tuple[int, int]:
    rows = []
    for f in files:
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        if d.get("youtube_id"):
            rows.append((f, d))
    # 没核对过的排前面：日更每天带 --limit 跑一小批，几天就能把存量清完，
    # 而"清完了没"由体检报出来，不用人记着。
    rows.sort(key=lambda r: bool(r[1].get("video_len")))
    if limit:
        rows = rows[:limit]
    log(f"体检 {len(rows)} 篇挂着视频的"
        f"（其中没核对过的 {len([r for r in rows if not r[1].get('video_len')])} 篇）")

    def one(item):
        f, d = item
        ok, why = T.video_aligned(d, tier_of(d) == "youtube")
        return f, d, ok, why

    kept = dropped = unknown = 0
    with cf.ThreadPoolExecutor(6) as pool:
        for f, d, ok, why in pool.map(one, rows):
            if ok:
                kept += 1
                if d.get("video_len") and not dry:
                    with _LOCK:
                        f.write_text(json.dumps(d, ensure_ascii=False, indent=1) + "\n")
                continue
            if ok is None:
                # 摘视频是破坏性的，不能建立在"这次查不出来"上
                unknown += 1
                log(f"  待查  {label(d)}｜{why}")
                continue
            dropped += 1
            log(f"  摘掉  {label(d)}")
            log(f"        {why}")
            if not dry:
                d["youtube_id"] = ""
                with _LOCK:
                    f.write_text(json.dumps(d, ensure_ascii=False, indent=1) + "\n")
    log(f"{'（dry-run，一个字没写）' if dry else '已写回'} "
        f"保留 {kept} · 摘掉 {dropped} · 这次没查出来 {unknown}")
    if unknown > max(3, len(rows) * 0.3):
        log(f"::error::{unknown}/{len(rows)} 篇查不出来——这是限流，不是数据问题，"
            f"过一阵重跑 --audit")
    return kept, dropped


# ---------------------------------------------------------------------- find

def find(files: list[pathlib.Path], dry: bool, limit: int, workers: int) -> int:
    srcs = load_sources()
    todo = []
    for f in files:
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        if d.get("youtube_id") or not d.get("duration"):
            continue
        todo.append((f, d))
    todo = todo[:limit] if limit else todo
    log(f"给 {len(todo)} 篇只有音频的找视频（并发 {workers}）")

    def one(item):
        f, d = item
        src = srcs.get(d.get("source_id")) or {"id": d.get("source_id") or "",
                                               "name": d.get("source") or ""}
        ep = {"title": d.get("title_original") or "", "duration": d.get("duration"),
              "link": d.get("link") or ""}
        try:
            vid = T.match_youtube(ep, src)
        except Exception as ex:
            return f, d, None, f"查找报错 {type(ex).__name__}"
        if not vid:
            return f, d, None, "找不到同剪辑的视频"
        ep["youtube_id"] = vid
        ok, why = T.video_aligned(ep, False)
        return f, d, (vid if ok else None), why

    got = 0
    with cf.ThreadPoolExecutor(workers) as pool:
        for f, d, vid, why in pool.map(one, todo):
            if not vid:
                log(f"  —     {label(d)}｜{why}")
                continue
            got += 1
            log(f"  ＋视频 {label(d)}｜{why}")
            if not dry:
                d["youtube_id"] = vid
                if ep.get("video_len"):
                    d["video_len"] = ep["video_len"]
                with _LOCK:
                    f.write_text(json.dumps(d, ensure_ascii=False, indent=1) + "\n")
    log(f"{'（dry-run，一个字没写）' if dry else '已写回'} "
        f"{got}/{len(todo)} 篇配上了可跳时间戳的视频")
    return got


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", action="store_true", help="核对已挂的视频，摘掉对不上的")
    ap.add_argument("--find", action="store_true", help="给只有音频的找视频")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    if not (a.audit or a.find):
        ap.error("至少给一个 --audit 或 --find")

    files = sorted(EPS.glob("*.json"))
    changed = 0
    if a.audit:
        changed += audit(files, a.dry_run, a.limit)[1]
    if a.find:
        changed += find(files, a.dry_run, a.limit, a.workers)
    if changed and not a.dry_run:
        log("记得跑 python3 pipeline/build.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
