#!/usr/bin/env python3
"""Daily run: ingest -> dedupe -> pick -> transcript -> digest -> gate -> write.

Design rules that show up as code below:
  * An episode is published only if the gate can verify it. A run that produces
    nothing is a correct outcome, not a failure to paper over.
  * The same episode arriving from two sources (an RSS feed and the show's
    YouTube channel) is published once. Fingerprints live in state.json.
  * A source that cannot yield a transcript is retried a few times and then
    left alone, so one broken show does not burn the budget every day.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lib import digest as D, feeds, gate, llm, transcript as T   # noqa: E402
from lib.util import (eid, fingerprint, hhmmss, iso, log, now,   # noqa: E402
                      slugify, squeeze)

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
EPS = DATA / "episodes"
STATE = DATA / "state.json"

MAX_FAILS = 3
# Trailers, teasers and paywall stubs: never worth a page.
SKIP_TITLE = ("(preview)", "[preview]", "trailer", "coming soon", "announcement:",
              "subscriber-only", "teaser", "预告", "试听")
MIN_SECONDS = 8 * 60


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            log("  ! state.json unreadable, starting fresh")
    return {"done": {}, "fp": {}, "fail": {}}


def save_state(s: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=1, sort_keys=True) + "\n")


def candidates(srcs: list[dict], state: dict, days: int, only: str | None) -> list[dict]:
    """Fetch every feed concurrently — a serial pass over 49 feeds, some of them
    12MB, takes minutes and dominates the whole run."""
    cutoff = now() - dt.timedelta(days=days)
    todo = [s for s in srcs if not only or s["id"] == only]

    def pull(s):
        try:
            return s, feeds.fetch(s, cache_ttl=1800), None
        except Exception as ex:
            return s, [], f"{type(ex).__name__}: {str(ex)[:90]}"

    with ThreadPoolExecutor(max_workers=12) as pool:
        fetched = list(pool.map(pull, todo))

    out = []
    for s, eps, err in fetched:
        if err:
            log(f"  ! {s['id']}: feed failed — {err}")
            continue
        kept = 0
        for ep in eps[:25]:
            if not ep["published"] or ep["published"] < cutoff:
                continue
            key = eid(s["id"], ep["guid"])
            if key in state["done"]:
                continue
            f = state["fail"].get(key)
            if f and f.get("n", 0) >= MAX_FAILS:
                continue
            low = ep["title"].lower()
            if any(w in low for w in SKIP_TITLE):
                continue
            if ep.get("duration") and ep["duration"] < MIN_SECONDS:
                continue
            ep = dict(ep, _key=key, _src=s)
            out.append(ep)
            kept += 1
        if kept:
            log(f"  {s['id']:<15} {kept} new")
    return out


def score(ep: dict) -> float:
    """Rank candidates so a limited daily budget spends on the best episodes."""
    s = ep["_src"]
    v = {1: 100.0, 2: 55.0, 3: 25.0}.get(s.get("tier", 3), 25.0)
    age_h = (now() - ep["published"]).total_seconds() / 3600
    v -= min(age_h / 24, 10) * 4                    # freshness
    if ep.get("transcripts"):
        v += 30                                     # official transcript in hand
    if len(squeeze(ep.get("notes") or "")) > 20000:
        v += 22                                     # full text already in the feed
    if T.chapters(ep):
        v += 8
    d = ep.get("duration") or 0
    if d and d < 15 * 60:
        v -= 12                                     # short news hits are thin
    if d > 4 * 3600:
        v -= 6                                      # multi-hour needs map-reduce
    return v


def process(ep: dict, state: dict, *, dry: bool) -> str:
    """Return 'published' | 'duplicate' | 'no-transcript' | 'rejected' | 'error'."""
    s = ep["_src"]
    key = ep["_key"]
    log(f"\n> [{s['id']}] {ep['title'][:78]}")
    log(f"    {iso(ep['published'])[:10]} · {hhmmss(ep.get('duration')) or '时长未知'}")

    fp = fingerprint(ep["title"], ep.get("duration"))
    if fp in state["fp"] and state["fp"][fp] != key:
        log(f"    duplicate of {state['fp'][fp]} — same episode from another source")
        state["done"][key] = {"skip": "duplicate", "of": state["fp"][fp], "at": iso(now())}
        return "duplicate"

    tr = T.acquire(ep, s.get("lang", "en"), src=s)
    if not tr:
        n = state["fail"].get(key, {}).get("n", 0) + 1
        state["fail"][key] = {"n": n, "why": "no-transcript", "at": iso(now()),
                              "title": ep["title"][:120], "src": s["id"]}
        log(f"    not published: no usable transcript (attempt {n}/{MAX_FAILS})")
        return "no-transcript"

    ch = T.chapters(ep)
    if dry:
        log("    dry-run: stopping before the model call")
        return "dry"

    try:
        d = D.build(ep, s, tr, ch)
    except Exception as ex:
        n = state["fail"].get(key, {}).get("n", 0) + 1
        state["fail"][key] = {"n": n, "why": f"digest:{type(ex).__name__}", "at": iso(now()),
                              "title": ep["title"][:120], "src": s["id"]}
        log(f"    digest failed: {type(ex).__name__}: {str(ex)[:160]}")
        return "error"

    ok, problems, d = gate.check(d, tr, ep)
    gate.report(problems, ok)
    if not ok:
        n = state["fail"].get(key, {}).get("n", 0) + 1
        state["fail"][key] = {"n": n, "why": "gate:" + (problems[0] if problems else "?")[:80],
                              "at": iso(now()), "title": ep["title"][:120], "src": s["id"]}
        return "rejected"

    slug = f"{iso(ep['published'])[:10]}-{s['id']}-{slugify(d['title'], 40)}"
    rec = {
        "id": key, "slug": slug, "fingerprint": fp,
        "source_id": s["id"], "source": s["name"], "source_zh": s.get("zh") or s["name"],
        "cat": s["cat"], "tier": s.get("tier", 3), "lang": s.get("lang", "en"),
        "title_original": ep["title"], "published": iso(ep["published"]),
        "duration": ep.get("duration"), "audio": ep.get("audio") or "",
        "link": ep.get("link") or "", "image": ep.get("image") or "",
        "youtube_id": ep.get("youtube_id") or "",
        "transcript_url": tr.get("url", ""),
        "digest": d, "generated": iso(now()),
        "model": f"{llm.provider()}:{llm.model_name()}",
    }
    EPS.mkdir(parents=True, exist_ok=True)
    (EPS / f"{slug}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1) + "\n")
    state["done"][key] = {"slug": slug, "at": iso(now())}
    state["fp"][fp] = key
    state["fail"].pop(key, None)
    log(f"    published -> /p/{slug}/")
    return "published"


def main() -> int:
    ap = argparse.ArgumentParser(description="daily podcast digest run")
    ap.add_argument("--limit", type=int, default=int(os.environ.get("MAX_NEW", "8")),
                    help="how many episodes to publish this run")
    ap.add_argument("--days", type=int, default=int(os.environ.get("LOOKBACK_DAYS", "10")))
    ap.add_argument("--only", help="restrict to one source id")
    ap.add_argument("--dry-run", action="store_true", help="stop before any model call")
    ap.add_argument("--no-build", action="store_true")
    a = ap.parse_args()

    if not a.dry_run and not llm.available():
        log("no LLM backend configured (set LLM_API_KEY, or install the claude CLI)")
        return 2

    srcs = json.loads((DATA / "sources.json").read_text())["sources"]
    state = load_state()
    log(f"run · {iso(now())} · {llm.provider()}:{llm.model_name()} · "
        f"asr={'on' if T.ASR_KEY else 'off'} · budget={a.limit}")
    log(f"scanning {len(srcs)} sources, {a.days}d lookback")

    cands = candidates(srcs, state, a.days, a.only)
    cands.sort(key=score, reverse=True)
    log(f"\n{len(cands)} candidates; taking the top {a.limit} by tier/freshness/transcript")

    tally: dict[str, int] = {}
    published = 0
    for ep in cands:
        if published >= a.limit:
            break
        try:
            r = process(ep, state, dry=a.dry_run)
        except KeyboardInterrupt:
            log("\ninterrupted")
            break
        except Exception:
            log("    unhandled:\n" + traceback.format_exc()[-900:])
            r = "error"
        tally[r] = tally.get(r, 0) + 1
        if r == "published":
            published += 1
        save_state(state)

    log("\n" + " · ".join(f"{k}={v}" for k, v in sorted(tally.items())) or "nothing to do")
    if published and not a.no_build:
        import build
        build.main()
    elif not published:
        log("nothing published — site left untouched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
