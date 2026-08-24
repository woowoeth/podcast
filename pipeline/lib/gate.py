"""The editorial gate: nothing reaches the site that this file cannot verify.

Onepod publishes whatever its pipeline produced, which is why its archive
contains posts whose body explains that the caption script broke. The rule here
is the opposite: a record either passes every check or it is not published, and
the reasons are written to the run log either way.

Checks, in order of how much they matter:
  * verbatim   - every quote must literally occur in the transcript
  * grounded   - every number in `facts` must occur in the transcript
  * anchored   - every timestamp must fall inside the episode's duration
  * clean      - no pipeline/meta leakage, no filler sentences
  * complete   - enough points, quotes and prose to be worth a page
"""
from __future__ import annotations

import re
import unicodedata

from .util import log, parse_ts, squeeze

# Text that must never reach a reader: it is about our own plumbing.
LEAK = re.compile(
    r"(yt-dlp|youtube-transcript-api|whisper|ffmpeg|本机|脚本依赖|环境异常|"
    r"抓取失败|无法获取字幕|自动字幕|逐字稿(?:获取|抓取)|转写失败|"
    r"根据公开|备份整理|由于.{0,12}异常|作为(?:一个)?(?:AI|语言模型)|"
    r"as an ai language model|i (?:cannot|can't) access)", re.I)

# Sentences that occupy space without carrying information.
FILLER = re.compile(
    r"(值得一听|干货满满|令人深思|发人深省|受益匪浅|不容错过|信息量满满|"
    r"这期节目(?:主要)?讨论了|本期节目介绍了|总而言之|综上所述|"
    r"让我们一起|带你了解|深入探讨了(?:很多)?话题)")

MIN_POINTS = 5
MIN_QUOTES = 2
MIN_QUOTE_CHARS = 24
# Points must land in different places. When every one carries the same
# timestamp the anchoring is decorative, and the "deep read" is really a
# paraphrase of one short passage.
MIN_DISTINCT_TS = 4


def _norm(s: str) -> str:
    """Fold everything that a transcriber and a model would render differently:
    smart quotes, casing, punctuation, whitespace, CJK width."""
    s = unicodedata.normalize("NFKC", s).lower()
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"[^0-9a-z一-鿿]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _quote_in(q: str, hay: str) -> bool:
    """A quote may elide with … / ... — each retained run must be present."""
    parts = [p for p in re.split(r"…+|\.{3,}|\[\.{3}\]", q) if _norm(p)]
    if not parts:
        return False
    for p in parts:
        n = _norm(p)
        if len(n) < 12:            # too short to verify; ignore this fragment
            continue
        if n not in hay:
            return False
    return any(len(_norm(p)) >= 12 for p in parts)


_NUM = re.compile(r"\d[\d,.]*")
_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen "
         "fourteen fifteen sixteen seventeen eighteen nineteen").split()
_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty", 6: "sixty",
         7: "seventy", 8: "eighty", 9: "ninety"}
_CN = "零一二三四五六七八九"
# A fact may be stated in one scale and spoken in another: "1000 亿" / "100 billion".
_SCALES = ((10 ** 12, ("trillion", "万亿")), (10 ** 9, ("billion",)),
           (10 ** 8, ("亿",)), (10 ** 6, ("million",)), (10 ** 4, ("万",)),
           (10 ** 3, ("thousand", "千")))
_SCALE_RE = re.compile(r"(trillion|billion|million|thousand|万亿|亿|万|千|[kmb]\b)", re.I)


def _en_under_1000(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        t, r = divmod(n, 10)
        return _TENS[t] + (" " + _ONES[r] if r else "")
    h, r = divmod(n, 100)
    return _ONES[h] + " hundred" + (" " + _en_under_1000(r) if r else "")


def _en_words(n: int) -> set[str]:
    """Plausible spoken renderings of an integer, including the year reading
    ("twenty fourteen") that transcribers use and digit matching never catches."""
    out: set[str] = set()
    if n < 0 or n >= 10 ** 12:
        return out
    if n < 1000:
        out.add(_en_under_1000(n))
    else:
        for scale, word in ((10 ** 9, "billion"), (10 ** 6, "million"), (10 ** 3, "thousand")):
            if n >= scale:
                q, r = divmod(n, scale)
                if q < 1000:
                    head = _en_under_1000(q) + " " + word
                    out.add(head)
                    if r:
                        out.add(head + " " + _en_words_first(r))
                        out.add(head + " and " + _en_words_first(r))
                break
    if 1100 <= n <= 2099:                       # "twenty fourteen", "nineteen ninety"
        hi, lo = divmod(n, 100)
        if lo:
            out.add(_en_under_1000(hi) + " " + _en_under_1000(lo))
            if lo < 10:
                out.add(_en_under_1000(hi) + " oh " + _ONES[lo])
    return out


def _en_words_first(n: int) -> str:
    return next(iter(_en_words(n)), str(n))


def _cn_words(n: int) -> set[str]:
    if n >= 100:
        return set()
    if n < 10:
        return {_CN[n], "两" if n == 2 else _CN[n]}
    t, r = divmod(n, 10)
    return {("" if t == 1 else _CN[t]) + "十" + (_CN[r] if r else "")}


def _renderings(tok: str, scale: float) -> set[str]:
    """Every way this quantity might appear in a transcript."""
    try:
        val = float(tok.replace(",", ""))
    except ValueError:
        return set()
    out: set[str] = set()
    if scale == 1.0:
        # No scale word attached, so the digits themselves are the claim.
        out |= {tok, tok.replace(",", "")}
        if val == int(val):
            n = int(val)
            out |= {f"{n:,}", str(n)} | _en_words(n) | _cn_words(n)
    mag = val * scale
    # Restate the magnitude in each scale, so 1e11 matches "100 billion" too.
    for s, words in _SCALES:
        if mag < s:
            continue
        q = mag / s
        if abs(q - round(q, 2)) > 1e-9 or q >= 1000:
            continue
        q_txt = f"{q:g}"
        for w in words:
            sep = "" if re.match(r"[\u4e00-\u9fff]", w) else " "
            out.add(q_txt + sep + w)
            if q == int(q):
                for wd in _en_words(int(q)) | _cn_words(int(q)):
                    out.add(wd + sep + w)
    return {o for o in out if len(o) >= 1}


def _num_in(v: str, hay_raw: str) -> bool:
    """A fact is grounded only if EVERY quantity in it appears in the transcript.

    Digit matching alone is not enough: spoken audio says "twenty fourteen",
    "four hundred dollars", "one percent". Checking digits only would delete
    most true facts, which is worse than not checking at all — so each number is
    expanded into its plausible spoken forms before searching.
    """
    nums = [n.strip(".,") for n in _NUM.findall(v)]
    if not nums:
        return True                # purely qualitative; nothing to verify
    hay = _norm(hay_raw)
    flat = hay.replace(" ", "")
    for tok in nums:
        # A scale word attached to this number in the fact ("1000 亿", "2 trillion").
        after = v.split(tok, 1)[1][:12] if tok in v else ""
        m = _SCALE_RE.search(after)
        scale = 1.0
        if m:
            w = m.group(1).lower()
            scale = {"trillion": 1e12, "billion": 1e9, "million": 1e6, "thousand": 1e3,
                     "万亿": 1e12, "亿": 1e8, "万": 1e4, "千": 1e3,
                     "k": 1e3, "m": 1e6, "b": 1e9}.get(w, 1.0)
        hit = False
        for r in _renderings(tok, scale):
            n = _norm(r)
            if not n:
                continue
            if n in hay or n.replace(" ", "") in flat:
                hit = True
                break
        if not hit:
            return False
    return True


def check(d: dict, tr: dict, ep: dict) -> tuple[bool, list[str], dict]:
    """Return (publishable, problems, cleaned_digest). Recoverable problems
    prune the offending row; unrecoverable ones fail the whole episode."""
    from .transcript import flatten
    raw_text = flatten(tr["segments"], stamp_every=10 ** 9)
    hay = _norm(raw_text)
    dur = ep.get("duration") or 0
    limit = dur + 180 if dur else 10 ** 9
    problems: list[str] = []
    d = dict(d)

    # --- clean: leakage and filler are unrecoverable, they mean bad instructions
    blob = " ".join([d.get("title", ""), d.get("dek", ""), d.get("why", ""),
                     d.get("who", ""), d.get("skip", "")]
                    + [p.get("body", "") + p.get("h", "") for p in d.get("points", [])])
    m = LEAK.search(blob)
    if m:
        return False, [f"meta/pipeline leakage: {m.group(0)!r}"], d
    m = FILLER.search(blob)
    if m:
        problems.append(f"filler phrase {m.group(0)!r}")

    # --- anchored + complete: points
    kept = []
    for p in d.get("points", []):
        t = parse_ts(p.get("t"))
        if t is None or not (0 <= t <= limit):
            problems.append(f"point timestamp out of range: {p.get('t')!r}")
            continue
        if len(p.get("body", "")) < 60:
            problems.append(f"point too thin: {p.get('h')!r}")
            continue
        p = dict(p, t=t)
        kept.append(p)
    d["points"] = sorted(kept, key=lambda p: p["t"])

    # --- verbatim: quotes the transcript does not contain are deleted
    kept = []
    for q in d.get("quotes", []):
        raw = q.get("raw", "")
        if len(raw) < MIN_QUOTE_CHARS:
            problems.append("quote too short")
            continue
        if not _quote_in(raw, hay):
            problems.append(f"quote not in transcript: {raw[:56]!r}")
            continue
        t = parse_ts(q.get("t"))
        if t is None or not (0 <= t <= limit):
            t = _locate(raw, tr["segments"])
            if t is None:
                problems.append("quote timestamp unrecoverable")
                continue
        kept.append(dict(q, t=t))
    d["quotes"] = sorted(kept, key=lambda q: q["t"])

    # --- grounded: facts whose numbers are absent are deleted
    kept = []
    for f in d.get("facts", []):
        if not f.get("k") or not f.get("v"):
            continue
        if not _num_in(f["v"], raw_text):
            problems.append(f"fact not in transcript: {f['k']}={f['v']}")
            continue
        t = parse_ts(f.get("t"))
        kept.append(dict(f, t=t if (t is not None and 0 <= t <= limit) else None))
    d["facts"] = kept
    d["terms"] = [t for t in d.get("terms", []) if t.get("term") and t.get("zh")][:6]

    # --- complete
    fatal = []
    if len(d["points"]) < MIN_POINTS:
        fatal.append(f"only {len(d['points'])} usable points (need {MIN_POINTS})")
    if len(d["quotes"]) < MIN_QUOTES:
        fatal.append(f"only {len(d['quotes'])} verifiable quotes (need {MIN_QUOTES})")
    distinct = len({p["t"] for p in d["points"]})
    if len(d["points"]) >= MIN_POINTS and distinct < MIN_DISTINCT_TS:
        fatal.append(f"points share only {distinct} distinct timestamp(s) — not anchored")
    elif dur and dur > 600 and d["points"]:
        span = max(p["t"] for p in d["points"]) - min(p["t"] for p in d["points"])
        if span < dur * 0.2:
            fatal.append(f"points span {span}s of a {dur}s episode — one passage, not the episode")
    if len(d.get("title", "")) < 6:
        fatal.append("title missing or too short")
    if len(d.get("dek", "")) < 20:
        fatal.append("dek missing or too short")
    if re.fullmatch(r"[\x00-\x7f]+", d.get("title", "") or "x"):
        fatal.append("title is not Chinese")

    d["quality"] = {
        "transcript_source": tr.get("source"), "transcript_detail": tr.get("detail"),
        "words": tr.get("words"), "wpm": tr.get("wpm"), "timed": tr.get("timed"),
        "verified_quotes": len(d["quotes"]), "grounded_facts": len(d["facts"]),
        "points": len(d["points"]), "pruned": len(problems),
    }
    return (not fatal), (fatal + problems), d


def _locate(quote: str, segs: list[dict]) -> int | None:
    """Recover a timestamp by finding the quote's opening words in the segments."""
    head = _norm(quote)[:60]
    if len(head) < 12:
        return None
    run = ""
    marks = []
    for s in segs:
        marks.append((len(run), s["t"]))
        run += _norm(s["text"]) + " "
    i = run.find(head)
    if i == -1:
        return None
    best = 0
    for pos, t in marks:
        if pos <= i:
            best = t
        else:
            break
    return best


def report(problems: list[str], ok: bool) -> None:
    if ok and not problems:
        log("    gate: clean")
        return
    log(f"    gate: {'passed with pruning' if ok else 'REJECTED'}")
    for p in problems[:8]:
        log(f"      - {p}")
    if len(problems) > 8:
        log(f"      … {len(problems) - 8} more")
