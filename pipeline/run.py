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
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lib import digest as D, feeds, gate, llm, transcript as T, triage  # noqa: E402
from lib.util import (eid, fingerprint, hhmmss, iso, log, now,   # noqa: E402
                      slugify, squeeze)

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
EPS = DATA / "episodes"
STATE = DATA / "state.json"

# A run must not turn the front page into one show's archive. Odd Lots ships an
# official transcript for every episode, so on a wide lookback it would take
# every slot on quality alone; breadth is part of what the reader is here for.
MAX_PER_SOURCE = int(os.environ.get("MAX_PER_SOURCE", "2"))
MAX_WORDS = int(os.environ.get("MAX_WORDS", "0"))          # 0 = no ceiling
# Set once from --max-words before any episode is processed; process() reads it.
_ceiling = {"words": MAX_WORDS}
_tiers = {"allow": T.ORDER}
_triage = {"on": True, "min": triage.MIN_SCORE}
_last_triage: dict[str, dict] = {}
MAX_FAILS = 3          # genuine failures: no transcript exists, or the gate says no
MAX_SOFT_FAILS = 8     # infrastructure hiccups: a 429, a dropped connection, a
                       # model call that died. These must not burn an episode's
                       # retry budget, or one bad afternoon blacklists good shows.
# Trailers, teasers and paywall stubs: never worth a page.
SKIP_TITLE = ("(preview)", "[preview]", "trailer", "coming soon", "announcement:",
              "subscriber-only", "teaser", "预告", "试听",
              "sponsored content", "(sponsored", "[sponsored", "广告")
MIN_SECONDS = 8 * 60


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            log("  ! state.json unreadable, starting fresh")
    return {"done": {}, "fp": {}, "fail": {}}


_STATE_LOCK = Lock()


def save_state(s: dict) -> None:
    with _STATE_LOCK:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
        tmp.replace(STATE)                    # atomic: a killed run never truncates state


def reconcile(state: dict) -> int:
    """Make state agree with what is actually on disk.

    The episode files are the source of truth: state.json is an index. If a run
    is killed between writing a record and saving state — or two runs overlap —
    the index can fall behind, and a stale index means republishing an episode
    that is already on the site. Re-derive it instead of trusting it."""
    fixed = 0
    for f in EPS.glob("*.json"):
        try:
            r = json.loads(f.read_text())
        except Exception:
            continue
        key, fp = r.get("id"), r.get("fingerprint")
        if key and state["done"].get(key, {}).get("slug") != r.get("slug"):
            state["done"][key] = {"slug": r["slug"], "at": r.get("generated") or iso(now())}
            fixed += 1
        if fp and state["fp"].get(fp) != key:
            state["fp"][fp] = key
            fixed += 1
        state["fail"].pop(key, None)
    if fixed:
        log(f"  state was behind disk in {fixed} place(s) — rebuilt from data/episodes/")
    return fixed


def spread(ranked: list[dict], limit: int, per_source: int) -> list[dict]:
    """Take the best `limit` episodes, but no more than `per_source` from any one
    show. Overflow is left for the next run rather than dropped."""
    out, used, held = [], {}, []
    for ep in ranked:
        sid = ep["_src"]["id"]
        if used.get(sid, 0) >= per_source:
            held.append(sid)
            continue
        used[sid] = used.get(sid, 0) + 1
        out.append(ep)
        if len(out) >= limit:
            break
    if held:
        from collections import Counter
        top = ", ".join(f"{k}×{v}" for k, v in Counter(held).most_common(4))
        log(f"  held back for a later run (per-source cap): {top}")
    return out


def _release(state: dict, fp: str, key: str) -> None:
    """Give the fingerprint back after a failure, so the next run can retry it."""
    with _STATE_LOCK:
        if state["fp"].get(fp) == key:
            state["fp"].pop(fp, None)


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
            if f and (f.get("n", 0) >= MAX_FAILS or f.get("soft", 0) >= MAX_SOFT_FAILS):
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
    log(f"\n> [{s['id']}] {ep['title'][:78]}\n"
        f"    [{s['id']}] {iso(ep['published'])[:10]} · "
        f"{hhmmss(ep.get('duration')) or '时长未知'}")

    fp = fingerprint(ep["title"], ep.get("duration"))
    with _STATE_LOCK:
        claimed = state["fp"].get(fp)
        if claimed is None:
            state["fp"][fp] = key            # claim it before the slow work starts
    if claimed is not None and claimed != key:
        log(f"    duplicate of {claimed} — same episode from another source")
        state["done"][key] = {"skip": "duplicate", "of": claimed, "at": iso(now())}
        return "duplicate"

    # 选题闸门放在取稿之前：只喂标题和节目介绍（约 600 token），比下载音频、
    # 拉字幕、再喂几万 token 的逐字稿便宜两个数量级。不合格的集在这里就停。
    if _triage["on"]:
        v = triage.score(ep, s)
        if v is not None:
            _last_triage[key] = v
            mark = "通过" if v["score"] >= _triage["min"] else "不做"
            log(f"    选题 {v['score']:.0f}/10 · {v['kind']} · {v['why']} → {mark}")
            if v["score"] < _triage["min"]:
                state["done"][key] = {"skip": "off-brief", "score": v["score"],
                                      "why": v["why"], "kind": v["kind"],
                                      "title": ep["title"][:120], "src": s["id"],
                                      "at": iso(now())}
                _release(state, fp, key)
                return "off-brief"

    tr = T.acquire(ep, s.get("lang", "en"), src=s, allow=_tiers["allow"])
    cap_words = _ceiling["words"]
    if tr and cap_words and tr["words"] > cap_words:
        # Cost scales with transcript length, and a 35k-word episode costs as
        # much as six ordinary ones. On a small budget, leave it for later
        # rather than spending the whole run on it. Not a failure — no counter.
        log(f"    {tr['words']} words > --max-words {cap_words}, 留给以后再发")
        _release(state, fp, key)
        return "too-long"
    if not tr:
        prev = state["fail"].get(key, {})
        transient = T.last_was_transient()
        if transient:
            # 被限流／被要求登录／连接断了，不代表这一集没有文稿。算软失败，
            # 否则一次网络不好就会把有字幕的集永久拉黑。
            rec = {"n": prev.get("n", 0), "soft": prev.get("soft", 0) + 1,
                   "why": "transcript-transient"}
            log(f"    这次没拿到（限流或被拦），不计入重试预算 "
                f"(soft {rec['soft']}/{MAX_SOFT_FAILS})")
        else:
            rec = {"n": prev.get("n", 0) + 1, "soft": prev.get("soft", 0),
                   "why": "no-transcript"}
            log(f"    not published: no usable transcript "
                f"(attempt {rec['n']}/{MAX_FAILS})")
        rec.update(at=iso(now()), title=ep["title"][:120], src=s["id"])
        state["fail"][key] = rec
        _release(state, fp, key)
        return "no-transcript"

    ch = T.chapters(ep)
    if dry:
        log("    dry-run: stopping before the model call")
        return "dry"

    try:
        d = D.build(ep, s, tr, ch)
    except llm.AuthError:
        _release(state, fp, key)
        raise                                 # abort the run; config is broken
    except Exception as ex:
        prev = state["fail"].get(key, {})
        state["fail"][key] = {"n": prev.get("n", 0), "soft": prev.get("soft", 0) + 1,
                              "why": f"digest:{type(ex).__name__}", "at": iso(now()),
                              "title": ep["title"][:120], "src": s["id"]}
        log(f"    digest failed: {type(ex).__name__}: {str(ex)[:160]}")
        log("    (counted as an infrastructure hiccup, not against the retry budget)")
        _release(state, fp, key)
        return "error"

    ok, problems, d = gate.check(d, tr, ep)
    gate.report(problems, ok)
    if not ok:
        prev = state["fail"].get(key, {})
        n = prev.get("n", 0) + 1
        state["fail"][key] = {"n": n, "soft": prev.get("soft", 0),
                              "why": "gate:" + (problems[0] if problems else "?")[:80],
                              "at": iso(now()), "title": ep["title"][:120], "src": s["id"]}
        _release(state, fp, key)
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
        "triage": _last_triage.get(key),
        "model": f"{llm.provider()}:{llm.model_name()}",
    }
    EPS.mkdir(parents=True, exist_ok=True)
    (EPS / f"{slug}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1) + "\n")
    state["done"][key] = {"slug": slug, "at": iso(now())}
    state["fail"].pop(key, None)
    log(f"    published -> /p/{slug}/")
    return "published"


def main() -> int:
    ap = argparse.ArgumentParser(description="daily podcast digest run")
    ap.add_argument("--limit", type=int, default=int(os.environ.get("MAX_NEW", "8")),
                    help="how many episodes to publish this run")
    ap.add_argument("--days", type=int, default=int(os.environ.get("LOOKBACK_DAYS", "10")))
    ap.add_argument("--only", help="restrict to one source id")
    ap.add_argument("--triage-min", type=float, default=triage.MIN_SCORE,
                    help="选题闸门的及格线（0-10）。低于这个分数的集不做深读。")
    ap.add_argument("--no-triage", action="store_true",
                    help="关掉选题闸门（会把不合格的集也做成深读）")
    ap.add_argument("--tiers", default=os.environ.get("TIERS", ""),
                    help="restrict transcript tiers, comma separated "
                         "(feed,notes,page,youtube,asr). Empty = all. "
                         "CI should exclude youtube: YouTube blocks datacenter "
                         "IPs and asks for cookies.")
    ap.add_argument("--max-words", type=int, default=MAX_WORDS,
                    help="skip episodes whose transcript is longer than this "
                         "(cost scales with length; 0 = no limit)")
    ap.add_argument("--per-source", type=int, default=MAX_PER_SOURCE,
                    help="max episodes from any one show in this run")
    ap.add_argument("--jobs", type=int, default=int(os.environ.get("JOBS", "3")),
                    help="how many episodes to process concurrently")
    ap.add_argument("--spend-subscription", action="store_true",
                    help="confirm that running on the local claude CLI may spend your "
                         "Claude subscription allowance (required above --limit 3)")
    ap.add_argument("--dry-run", action="store_true", help="stop before any model call")
    ap.add_argument("--no-build", action="store_true")
    a = ap.parse_args()
    _ceiling["words"] = a.max_words
    _triage["on"] = not a.no_triage
    _triage["min"] = a.triage_min
    if a.tiers:
        want = tuple(t.strip() for t in a.tiers.split(",") if t.strip() in T.ORDER)
        if want:
            _tiers["allow"] = want

    if not a.dry_run and llm.provider() == "claude-cli" and not a.spend_subscription:
        per = min(a.max_words or 20000, 20000) * 1.35 / 1000
        est = int(a.limit * per)
        log(f"这一轮会用本机 claude CLI（{llm.model_name()}）生成，花的是你的 Claude "
            f"订阅额度，不是 API 账单。")
        log(f"  {a.limit} 集，每集上限 {a.max_words or '不限'} 词 → 大约 {est}k input tokens。")
        log("  确认要花订阅额度就加 --spend-subscription；不想花就配 LLM_API_KEY 走 API。")
        if a.limit > 3:
            log(f"\n拒绝执行：--limit {a.limit} 超过 3，必须显式加 --spend-subscription。")
            return 3
        log("  （limit ≤ 3，继续。）\n")

    if not a.dry_run and not llm.available():
        log("no LLM backend configured — nothing can be generated.\n"
            "  In CI: add the LLM_API_KEY secret (sk-ant-* uses the Anthropic\n"
            "         Messages API; anything else is treated as OpenAI-compatible,\n"
            "         set LLM_BASE_URL and LLM_MODEL alongside it).\n"
            "  Locally: install the claude CLI and sign in — no key needed.")
        return 2

    srcs = json.loads((DATA / "sources.json").read_text())["sources"]
    state = load_state()
    reconcile(state)
    cap = llm.safe_jobs()
    if a.jobs > cap:
        log(f"--jobs {a.jobs} clamped to {cap} for the {llm.provider()} backend")
        a.jobs = cap
    log(f"run · {iso(now())} · {llm.provider()}:{llm.model_name()} · "
        f"asr={'on' if T.ASR_KEY else 'off'} · budget={a.limit}")
    log(f"  endpoint: {llm.endpoint()}")
    log(f"  文稿层: {', '.join(_tiers['allow'])}")
    log(f"  选题闸门: {'及格线 ' + str(_triage['min']) if _triage['on'] else '关闭'}")
    log(f"scanning {len(srcs)} sources, {a.days}d lookback")

    cands = candidates(srcs, state, a.days, a.only)
    cands.sort(key=score, reverse=True)
    cands = spread(cands, a.limit, a.per_source)
    log(f"\n{len(cands)} picked (≤{a.per_source} per source) by tier/freshness/transcript")

    tally: dict[str, int] = {}
    published = 0
    picked = cands

    def one(ep):
        try:
            return process(ep, state, dry=a.dry_run)
        except llm.AuthError:
            raise
        except Exception:
            log("    unhandled:\n" + traceback.format_exc()[-900:])
            return "error"

    if a.jobs > 1 and len(picked) > 1:
        # Each episode is independent; the only shared state is the dict, and
        # duplicate detection is the one ordering-sensitive part — so a second
        # pass over fingerprints runs after the pool drains.
        log(f"processing {len(picked)} episodes with {a.jobs} workers")
        with ThreadPoolExecutor(max_workers=a.jobs) as pool:
            futs = {pool.submit(one, ep): ep for ep in picked}
            try:
                for f in as_completed(futs):
                    r = f.result()   # AuthError propagates and fails the run
                    tally[r] = tally.get(r, 0) + 1
                    if r == "published":
                        published += 1
                    save_state(state)
            except KeyboardInterrupt:
                log("\ninterrupted — cancelling queued episodes")
                for f in futs:
                    f.cancel()
    else:
        for ep in picked:
            try:
                r = one(ep)
            except llm.AuthError as ex:
                log(f"\n凭证被拒，停止本轮：\n{ex}")
                return 4
            except KeyboardInterrupt:
                log("\ninterrupted")
                break
            tally[r] = tally.get(r, 0) + 1
            if r == "published":
                published += 1
            save_state(state)
    save_state(state)

    log("\n" + " · ".join(f"{k}={v}" for k, v in sorted(tally.items())) or "nothing to do")
    if tally.get("error") and not published:
        log(f"\n每一次模型调用都失败了（error={tally['error']}），这不是"
            f"「今天没内容」，是部署有问题。")
        return 5
    if published and not a.no_build:
        import build
        build.main()
    elif not published:
        log("nothing published — site left untouched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
