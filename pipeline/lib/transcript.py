"""Transcript acquisition, in strict quality order.

The whole platform stands on this file. A summary is only as good as the text it
was written from, so every transcript carries provenance (`source`) and a
measured `wpm`; anything that fails the density check is treated as *no
transcript at all* rather than quietly summarised into a thin post.

Tiers, best first:
  1. feed        - <podcast:transcript> shipped by the show (VTT/SRT/JSON)
  2. notes       - the feed item already contains the full text (Substack shows
                   routinely paste the whole transcript into content:encoded)
  3. page        - a transcript link in the show notes, fetched and extracted
  4. youtube     - auto-captions via yt-dlp, if it is installed and not blocked
  5. asr         - download the audio and transcribe it (Whisper-compatible API)
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import html as html_mod
import json
import shutil
import subprocess
import tempfile
import threading
import time

from . import net
from .util import hhmmss, log, parse_ts, squeeze, strip_html

# Below this words-per-minute a "transcript" is really show notes or a partial
# capture. English conversation runs 130-170 wpm; Chinese ~220 chars/min.
MIN_WPM = {"en": 70, "zh": 110}
# And an upper bound: nobody speaks at 300 wpm. Exceeding it means we grabbed
# the wrong document — a show's archive page, a combined feed, an index.
MAX_WPM = {"en": 300, "zh": 520}
# An absolute floor as well as a ratio. YouTube's Atom feed carries no duration,
# so the wpm ratio degenerates to "words per one minute" and a 185-word clip
# sails through it. No real episode has a transcript this short.
MIN_WORDS = {"en": 1200, "zh": 1800}
ASR_KEY = os.environ.get("TRANSCRIBE_API_KEY", "").strip()
ASR_BASE = os.environ.get("TRANSCRIBE_BASE_URL", "https://api.groq.com/openai/v1").strip().rstrip("/")
ASR_MODEL = os.environ.get("TRANSCRIBE_MODEL", "whisper-large-v3-turbo").strip()
ASR_MAX_MB = int(os.environ.get("TRANSCRIBE_MAX_MB", "24"))
# 切片长度决定时间戳的最坏粒度。不是所有转写模型都返回逐句时间戳——
# SenseVoice 只给整段文本，于是"段"数等于切片数：900 秒切片意味着时间戳可能差
# 15 分钟，"点时间戳跳回原声"就成了空话。300 秒 + 片内按字数插值把最坏误差压到
# 一分钟级。代价只是请求数变多，音频总时长不变，所以按时长计费的价格不变。
CHUNK_SEC = int(os.environ.get("TRANSCRIBE_CHUNK_SEC", "300"))


# ---------------------------------------------------------------- caption files

def _vtt_srt(text: str) -> list[dict]:
    """Parse WebVTT or SubRip into [{t, text}] with seconds."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"^WEBVTT.*?\n\n", "", text, flags=re.S)
    segs: list[dict] = []
    pat = re.compile(r"(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}|\d{1,2}:\d{2}[.,]\d{1,3})"
                     r"\s*-->\s*(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}|\d{1,2}:\d{2}[.,]\d{1,3})")
    blocks = re.split(r"\n\s*\n", text)
    for b in blocks:
        m = pat.search(b)
        if not m:
            continue
        start = m.group(1).replace(",", ".")
        parts = start.split(":")
        sec = 0.0
        for p in parts:
            sec = sec * 60 + float(p)
        # Cut from the end of the timing LINE, not the end of the match: WebVTT
        # puts cue settings (align:start position:0%) after the timestamps, and
        # YouTube emits them on every cue.
        nl = b.find("\n", m.end())
        body = b[nl + 1:] if nl != -1 else ""
        body = re.sub(r"<[^>]+>", "", body)                  # karaoke/word tags
        body = html_mod.unescape(body)
        body = re.sub(r"(?:^|\s)>>\s*", " ", body)            # caption speaker marker
        body = squeeze(body.replace("\n", " "))
        if body:
            segs.append({"t": int(sec), "text": body})
    return _dedupe_rolling(segs)


_OVERLAP_MAX = 40


def _dedupe_rolling(segs: list[dict]) -> list[dict]:
    """Undo YouTube's rolling captions.

    Auto-captions scroll: cue N carries "A", cue N+1 carries "A B", cue N+2
    "B C". Naive concatenation therefore says everything twice. For each cue we
    find the longest suffix of what we have already emitted that is also a
    prefix of the incoming cue, and drop that prefix.
    """
    out: list[dict] = []
    tail: list[str] = []                       # last few words actually emitted
    for s in segs:
        words = s["text"].split()
        if not words:
            continue
        k = 0
        for cand in range(min(len(tail), len(words), _OVERLAP_MAX), 0, -1):
            if tail[-cand:] == words[:cand]:
                k = cand
                break
        words = words[k:]
        if not words:
            continue
        text = " ".join(words)
        out.append({"t": s["t"], "text": text})
        tail = (tail + words)[-_OVERLAP_MAX:]
    return out


def _json_captions(raw: str) -> list[dict]:
    """Transistor/OpenAI-style JSON transcripts. Shapes vary; probe for one."""
    try:
        d = json.loads(raw)
    except Exception:
        return []
    cands = None
    if isinstance(d, dict):
        for k in ("segments", "results", "transcript", "cues", "items", "data"):
            v = d.get(k)
            if isinstance(v, list) and v:
                cands = v
                break
    elif isinstance(d, list):
        cands = d
    if not cands:
        return []
    segs = []
    for c in cands:
        if not isinstance(c, dict):
            continue
        t = None
        for k in ("start", "startTime", "start_time", "begin", "offset", "t", "startMs"):
            if k in c:
                t = parse_ts(c[k])
                if t is not None and k.endswith("Ms") and t > 100000:
                    t //= 1000
                break
        body = ""
        for k in ("text", "body", "utterance", "content", "transcript"):
            if isinstance(c.get(k), str):
                body = c[k]
                break
        if not body and isinstance(c.get("speaker"), str) and isinstance(c.get("words"), list):
            body = " ".join(w.get("word", "") for w in c["words"])
        spk = c.get("speaker") or c.get("speaker_label") or ""
        body = squeeze(body)
        if body:
            seg = {"t": int(t or 0), "text": body}
            if isinstance(spk, str) and spk:
                seg["spk"] = spk
            segs.append(seg)
    return segs


# -------------------------------------------------------------- tier 1: in-feed

_TYPE_RANK = [("application/json", 0), ("json", 0), ("text/vtt", 1), ("vtt", 1),
              ("srt", 2), ("subrip", 2), ("text/html", 4), ("text/plain", 3)]


def _rank(t: str) -> int:
    t = (t or "").lower()
    for needle, r in _TYPE_RANK:
        if needle in t:
            return r
    return 5


def from_feed(ep: dict) -> dict | None:
    cands = sorted(ep.get("transcripts") or [], key=lambda x: (_rank(x.get("type")),
                                                              0 if "en" in (x.get("lang") or "en") else 1))
    for c in cands:
        try:
            raw = net.get_text(c["url"], timeout=60, cache_ttl=86400)
        except Exception as e:
            log(f"    feed transcript {c.get('type')} failed: {type(e).__name__}")
            continue
        ty = (c.get("type") or "").lower()
        segs: list[dict] = []
        if "json" in ty:
            segs = _json_captions(raw)
        if not segs and ("vtt" in ty or "srt" in ty or "subrip" in ty or "-->" in raw[:2000]):
            segs = _vtt_srt(raw)
        if not segs:
            body = strip_html(raw) if "<" in raw[:400] else raw
            if len(body) > 2000:
                segs = _plain_to_segs(body, ep.get("duration"))
        if segs:
            return {"segments": segs, "source": "feed", "detail": c.get("type") or "",
                    "url": c["url"]}
    return None


def _plain_to_segs(body: str, duration: int | None) -> list[dict]:
    """Untimed text: keep paragraphs, spread timestamps evenly so the reader
    still gets a rough anchor. Marked `approx` so the gate knows not to trust
    them to the second."""
    paras = [squeeze(p) for p in re.split(r"\n{2,}", body) if squeeze(p)]
    if len(paras) < 3:
        paras = [squeeze(p) for p in re.split(r"(?<=[.!?。！？])\s+", body) if squeeze(p)]
        paras = ["\n".join(paras[i:i + 8]) for i in range(0, len(paras), 8)]
    total = sum(len(p) for p in paras) or 1
    segs, run = [], 0
    for p in paras:
        t = int((duration or 0) * run / total) if duration else 0
        segs.append({"t": t, "text": p, "approx": True})
        run += len(p)
    return segs


# ------------------------------------------------- tier 2/3: notes & linked page

_NOTE_NOISE = re.compile(
    r"(?i)^(subscribe|sponsored|thanks to our sponsor|brought to you by|"
    r"follow (us|me)|timestamps?|chapters?|links?|mentioned|where to find|"
    r"referenced|production and marketing|advertise)\b")


def from_notes(ep: dict, lang: str) -> dict | None:
    body = strip_html(ep.get("notes"))
    if not body:
        return None
    lines = [ln for ln in body.split("\n") if not _NOTE_NOISE.match(ln.strip())]
    body = "\n".join(lines)
    words = _count(body, lang)
    dur_min = (ep.get("duration") or 0) / 60
    if not dur_min:
        return None
    # Only accept notes as the primary text when they are dense enough to be
    # the actual transcript or the full essay the episode reads out.
    if words / dur_min < MIN_WPM.get(lang, 70):
        return None
    segs = _timestamped_notes(body) or _plain_to_segs(body, ep.get("duration"))
    return {"segments": segs, "source": "notes", "detail": "feed item full text", "url": ep.get("link", "")}


# "0:31:12 Label" / "[00:31] Label" — a chapter list or a bare timed transcript.
_TS_LINE = re.compile(r"^\s*[\[\(]?(\d{1,2}:\d{2}(?::\d{2})?)[\]\)]?\s*[-–—:]?\s*(.+)$")
# "Joon [00:31:12]: ..." / "Swyx (31:12): ..." — the Substack transcript house
# style, and the only free source that hands us speaker attribution for nothing.
_SPK_LINE = re.compile(
    r"^\s*([^\n\[\](){}:]{1,28}?)\s*[\[\(](\d{1,2}:\d{2}(?::\d{2})?)[\]\)]\s*:?\s*(.*)$")


def _timestamped_notes(body: str) -> list[dict]:
    """Pull timed segments out of prose. Speaker-prefixed lines win, because
    they carry attribution; a bare chapter list is the weaker fallback."""
    spk_segs, ts_segs = [], []
    for ln in body.split("\n"):
        m = _SPK_LINE.match(ln)
        if m:
            t, name, txt = parse_ts(m.group(2)), squeeze(m.group(1)), squeeze(m.group(3))
            if t is not None and txt:
                seg = {"t": t, "text": txt}
                if name and not name[0].isdigit():
                    seg["spk"] = name
                spk_segs.append(seg)
                continue
        m = _TS_LINE.match(ln)
        if m:
            t, txt = parse_ts(m.group(1)), squeeze(m.group(2))
            if t is not None and txt:
                ts_segs.append({"t": t, "text": txt})
    segs = spk_segs if len(spk_segs) >= 5 else ts_segs
    if len(segs) < 5:
        return []
    # A chapter list matches _TS_LINE too but covers a fraction of the text.
    # Only trust these segments if they actually *are* most of the document.
    if sum(len(s["text"]) for s in segs) < 0.5 * len(body):
        return []
    return segs


_TR_LINK = re.compile(r'(?i)href="([^"]+)"[^>]*>\s*(?:full\s+)?transcript')


def _is_this_episode(body: str, title: str) -> bool:
    """A show's link often points at an archive or index page that happens to
    contain plenty of text. Require a distinctive run of the episode title to
    appear in the document before believing it is the right one."""
    words = [w for w in re.findall(r"[A-Za-z0-9\u4e00-\u9fff]{4,}", title)][:12]
    if len(words) < 3:
        return True
    low = body.lower()
    hits = sum(1 for w in words if w.lower() in low)
    return hits >= max(3, int(len(words) * 0.5))


# 有的节目把官方逐字稿放在兄弟路径上，而 shownotes 里根本不给链接
# （Darknet Diaries：/episode/178/ 的稿在 /transcript/178/）。按站点补一条改写规则，
# 比让整档有全文逐字稿的节目走音频转写划算得多。
_PAGE_ALT = (
    ("darknetdiaries.com", r"/episode/(\d+)", r"/transcript/\1"),
)


def _alt_urls(url: str) -> list[str]:
    out = []
    for host, pat, rep in _PAGE_ALT:
        if host in url and re.search(pat, url):
            out.append(re.sub(pat, rep, url))
    return out


# 每隔多少字符出现一次"其他集标题"就算列表页。见 _is_listing 的实测数字。
_LISTING_CHARS_PER_TITLE = 5000


def _is_listing(body: str, others: list[str]) -> str | None:
    """这页是不是分集列表，而不是逐字稿。

    真踩过：Y Combinator 的单集页底部列着其他集的标题和导语，抓下来 6700 字
    ——字数和语速检查都过（都是别人的简介），`_is_this_episode` 也过（本集标题
    确实在页面上），于是"导语扩写"被当成深读生成出来，成稿评分 3/10。
    四次被评审拦下的记录全是这一个 bug，而评审是唯一挡住它的东西。

    判据看**密度**，不看数量。第一版只数个数（≥3 就拒），误杀了 Dwarkesh——
    Substack 页面底部有"推荐文章"区块列着 7 篇，而页面主体是真逐字稿。
    "页面提到其他集"在 Substack、Libsyn 这类平台上普遍存在，不能当成列表页的证据。

    实测三例，密度差一个数量级：
        Y Combinator 列表页   39,514 字符 / 24 个标题 = 每 1,646 字符一个
        Dwarkesh 真逐字稿    130,090 字符 /  7 个标题 = 每 18,584 字符一个
        Cognitive Revolution 102,283 字符 /  4 个标题 = 每 25,570 字符一个
    阈值放在每 5000 字符一个：比真逐字稿的最坏情况严三倍，比列表页松三倍。
    """
    low = body.lower()
    hits = [t for t in others if len(t) >= 18 and t.lower() in low]
    if len(hits) < 3:
        return None
    per = len(body) / len(hits)
    if per < _LISTING_CHARS_PER_TITLE:
        return (f"每 {per:.0f} 字符就提到一次本节目其他集的标题"
                f"（共 {len(hits)} 集），是分集列表不是逐字稿")
    return None


def from_page(ep: dict, lang: str, others: list[str] | None = None) -> dict | None:
    urls = []
    m = _TR_LINK.search(ep.get("notes") or "")
    if m:
        urls.append(m.group(1))
    if ep.get("link"):
        urls += _alt_urls(ep["link"]) + [ep["link"]]
    dur_min = max((ep.get("duration") or 0) / 60, 1)
    lo, hi = MIN_WPM.get(lang, 70), MAX_WPM.get(lang, 300)
    for u in dict.fromkeys(urls):
        try:
            html = net.get_text(u, timeout=60, cache_ttl=86400)
        except Exception as ex:
            log(f"    page 层取不到 {u[:60]}：{type(ex).__name__} {str(ex)[:60]}")
            continue
        # Try every plausible container AND the whole document, then keep the
        # densest one that still looks like speech. Picking the first matching
        # <article> loses Substack transcripts, where <article> is a teaser card
        # and the 26k-word transcript sits outside it.
        best, best_words = None, 0
        for cand in _candidate_blocks(html):
            body = strip_html(cand)
            words = _count(body, lang)
            if words <= best_words:
                continue
            if not (lo <= words / dur_min <= hi):
                continue
            if not _is_this_episode(body, ep.get("title", "")):
                continue
            listing = _is_listing(body, others or [])
            if listing:
                log(f"    page 层：{listing}（{u[:50]}）")
                continue
            best, best_words = body, words
        if best is None:
            log(f"    page 层拿到了页面但没有够密的文稿（{u[:56]}）")
            continue
        segs = _timestamped_notes(best) or _plain_to_segs(best, ep.get("duration"))
        return {"segments": segs, "source": "page", "detail": "show page", "url": u}
    return None


_BLOCK_PATS = (
    r'(?is)<div[^>]+class="[^"]*(?:transcript|post-content|entry-content|'
    r'available-content|body markup)[^"]*".*?</div>',
    r"(?is)<article\b.*?</article>",
    r"(?is)<main\b.*?</main>",
)


def _candidate_blocks(html: str):
    """Containers worth trying, densest-looking first, then the full document."""
    for pat in _BLOCK_PATS:
        for m in re.finditer(pat, html):
            if len(m.group(0)) > 3000:
                yield m.group(0)
    yield html


# ------------------------------------------------------------ tier 4: youtube

_TOKEN = re.compile(r"[0-9a-z\u4e00-\u9fff]+")
_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "with", "for", "is",
         "how", "why", "what", "podcast", "episode", "ep", "part", "feat", "ft"}


def _tokens(t: str) -> set[str]:
    return {w for w in _TOKEN.findall(t.lower()) if w not in _STOP and len(w) > 1}


def match_youtube(ep: dict, src: dict) -> str | None:
    """Find this RSS episode on YouTube.

    以前这里先走频道 Atom feed（`feeds/videos.xml?channel_id=…`，15 条最新、不用
    yt-dlp）当快路径。那个端点已经废了：**连确认存在的真实频道 id 也返回 404**
    （拿 Dwarkesh 真实频道 UCXl4i9dYBrFOabk0xGmbkRA 实测过）。留着它只是给每一集
    多一次注定失败的请求，而失败日志长得像偶发网络问题。

    更糟的是它当年怎么错的：Atom feed **不带时长**，所以只能比标题，而 sources.json
    里 dwarkesh 的 yt 指向的是 **Dwarkesh Clips**（剪辑号）而不是正片频道——挂上去
    的是 176 秒、1674 秒的片花，页面上每个时间戳都跳错。

    现在只走搜索：它带时长，能用 seek_tolerance 挡掉片花。实测 215 篇只有音频的
    里配上了 57 篇，偏差 0～30 秒。
    """
    if len(_tokens(ep.get("title", ""))) < 3:
        return None
    return _search_youtube(ep, src)


def _search_youtube(ep: dict, src: dict) -> str | None:
    """Fall back to a YouTube search when the channel feed does not have it.

    The channel feed only carries the 15 newest uploads, and shows that post
    clips between episodes push the real episode out of it within days — which
    is why several Chinese shows looked like they had no video at all. Duration
    is the guard against matching a clip: a two-minute excerpt shares most of
    the title with the hour-long episode it came from.
    """
    if not shutil.which("yt-dlp"):
        return None
    title = re.sub(r"^\s*\d{1,4}\s*[.、|｜:：-]\s*", "", ep.get("title", ""))[:60]
    if not title:
        return None
    show = src.get("zh") or src["name"]
    dur = ep.get("duration")
    # 两轮：先"节目名 + 集标题"，再只用集标题。
    # 为什么要第二轮：sources.json 里"张小珺"曾被拼成"张小珲"，一个错字让这档节目的
    # 视频发现完全归零，而且是静默的（搜索返回空，和"这集真没视频"长得一模一样）。
    # 集标题本身已经足够独特，节目名只是帮着排序——不该成为单点。
    for q in (f"{show} {title}", title):
        vid = _yt_search_once(q, ep, dur)
        if vid:
            return vid
    return None


def _yt_search_once(query: str, ep: dict, dur: int | None) -> str | None:
    with _YT_LOCK:
        try:
            r = subprocess.run(
                ["yt-dlp", f"ytsearch5:{query}", "--flat-playlist",
                 "--no-warnings", "--socket-timeout", "30",
                 "--print", "%(id)s\t%(title)s\t%(duration)s"],
                capture_output=True, text=True, timeout=180)
        except Exception as ex:
            log(f"    youtube search failed: {type(ex).__name__}")
            return None
    want = _tokens(ep.get("title", ""))
    for line in (r.stdout or "").strip().split("\n"):
        parts = line.split("\t")
        if len(parts) < 3 or not parts[0]:
            continue
        vid, vtitle, vdur = parts[0], parts[1], parts[2]
        try:
            vdur = int(float(vdur))
        except (TypeError, ValueError):
            vdur = 0
        # 片花带着正片的标题，但带不来正片的长度。容差用 seek_tolerance：
        # 时间戳能错多少，这里就只能差多少，两处必须是同一个数。
        if dur and vdur and abs(vdur - dur) > seek_tolerance(dur):
            continue
        if dur and not vdur:
            continue                     # 长度未知 = 无法验证，不要
        have = _tokens(vtitle)
        if not have:
            continue
        inter = len(want & have)
        if inter >= max(3, int(len(want) * 0.6)):
            log(f"    found via YouTube search: {vtitle[:56]} ({vdur}s vs {dur}s)")
            return vid
    return None


# YouTube rate-limits hard (429) the moment two caption pulls overlap, which is
# how a YouTube-only pipeline ends up publishing "captions unavailable" posts.
# Serialising this one tier costs a little wall-clock and removes the failure.
_YT_LOCK = threading.Lock()

# 区分"这一集本来就没有文稿"和"这次没拿到"。前者该消耗重试预算（试三次就别再试），
# 后者不该——限流、机器人拦截、连接中断都属于后者，而它们在日志里长得跟前者一样。
_transient = {"hit": False}


def last_was_transient() -> bool:
    return _transient["hit"]


def from_youtube(vid: str, lang: str) -> dict | None:
    if not vid or not shutil.which("yt-dlp"):
        return None
    with _YT_LOCK:
        return _from_youtube(vid, lang)


def _from_youtube(vid: str, lang: str) -> dict | None:
    langs = "zh-Hans,zh,en" if lang == "zh" else "en,en-US,en-GB"
    with tempfile.TemporaryDirectory() as td:
        cmd = ["yt-dlp", "--skip-download", "--write-auto-subs", "--write-subs",
               "--sub-langs", langs, "--sub-format", "vtt", "--no-warnings",
               "--retries", "2", "--socket-timeout", "30",
               "-o", str(pathlib.Path(td) / "c"), "https://www.youtube.com/watch?v=" + vid]
        # YouTube 429s readily, and a 429 is indistinguishable from "no captions"
        # unless you read stderr. For the Chinese shows YouTube is the only
        # transcript path, so a rate limit must be waited out, not mistaken for
        # an absent track.
        files: list[pathlib.Path] = []
        err = ""
        for attempt in range(3):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=300, text=True)
            except Exception as e:
                log(f"    yt-dlp failed: {type(e).__name__}")
                return None
            files = sorted(pathlib.Path(td).glob("*.vtt"),
                           key=lambda p: p.stat().st_size, reverse=True)
            if files:
                break
            err = squeeze(((r.stderr or "") + " " + (r.stdout or ""))[-260:])
            if re.search(r"429|too many requests|rate.?limit|sign in|cookies|"
                         r"bot|captcha|confirm you", err, re.I):
                _transient["hit"] = True
                wait = 30 * (2 ** attempt)
                log(f"    YouTube 限流或要求登录，等 {wait}s 再试（{attempt + 1}/3）")
                time.sleep(wait)
                continue
            break
        if not files:
            if err:
                low = err.lower()
                if "no automatic captions" in low or "no subtitles" in low:
                    log("    这个视频没有字幕轨")
                    _transient["hit"] = False
                else:
                    log(f"    yt-dlp: 拿不到字幕（{err[:120]}）")
            return None
        segs = _vtt_srt(files[0].read_text("utf-8", "replace"))
        if segs:
            return {"segments": segs, "source": "youtube", "detail": files[0].name.split(".")[-2],
                    "url": "https://www.youtube.com/watch?v=" + vid}
    return None


# --------------------------------------------------- tier 5a: 本地转写（无需 key）

LOCAL_MODEL = os.environ.get("TRANSCRIBE_LOCAL_MODEL",
                             "mlx-community/whisper-large-v3-turbo")
LOCAL_ON = os.environ.get("TRANSCRIBE_LOCAL", "auto").lower()


def local_available() -> bool:
    """本机能不能自己转写。凭证在云端的 secrets 里读不到，所以本机这条线
    只能靠本地模型——mlx-whisper 在 Apple Silicon 上够快，且不需要任何 key。"""
    if LOCAL_ON in ("0", "off", "no"):
        return False
    try:
        import mlx_whisper                                   # noqa: F401
    except Exception:
        return False
    return _ffmpeg() is not None


def _local_asr(path: pathlib.Path, lang: str) -> list[dict] | None:
    try:
        import mlx_whisper
    except Exception:
        return None
    ff = _ffmpeg()
    if ff:
        # openai-whisper 系的 load_audio 直接调 "ffmpeg"，系统里没有就得把
        # 静态二进制所在目录塞进 PATH
        os.environ["PATH"] = str(pathlib.Path(ff).parent) + os.pathsep + os.environ.get("PATH", "")
        if pathlib.Path(ff).name != "ffmpeg":
            link = pathlib.Path(ff).parent / "ffmpeg"
            if not link.exists():
                try:
                    link.symlink_to(ff)
                except Exception:
                    pass
    try:
        r = mlx_whisper.transcribe(str(path), path_or_hf_repo=LOCAL_MODEL,
                                   language=lang if lang in ("en", "zh") else None,
                                   word_timestamps=False, verbose=False)
    except Exception as ex:
        log(f"    本地转写失败：{type(ex).__name__}: {str(ex)[:110]}")
        return None
    segs = [{"t": int(x.get("start") or 0), "text": squeeze(x.get("text") or "")}
            for x in (r.get("segments") or []) if squeeze(x.get("text") or "")]
    if not segs and r.get("text"):
        segs = _spread(squeeze(r["text"]), 0)
    return segs


# ---------------------------------------------------------------- tier 5: ASR

def _ffmpeg() -> str | None:
    """系统 ffmpeg 优先；没有就用 imageio-ffmpeg 自带的静态二进制。

    这台机器没有 brew，装系统 ffmpeg 要动包管理器；而切片和音频解码都需要它，
    没有它长音频根本没法转写。pip 包里那个静态二进制刚好解决。
    """
    got = shutil.which("ffmpeg")
    if got:
        return got
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _yt_audio(vid: str, td: str) -> pathlib.Path | None:
    """从 YouTube 抓音频。纯 YouTube 频道没有 RSS enclosure，没有这一步，
    没开字幕的频道就完全取不到文稿——而"频道有没有开字幕"不该决定选题。"""
    if not shutil.which("yt-dlp"):
        return None
    out = pathlib.Path(td) / "yt.m4a"
    cmd = ["yt-dlp", "-f", "bestaudio[abr<=96]/bestaudio/best",
           "-x", "--audio-format", "m4a", "--audio-quality", "5",
           "--no-warnings", "--retries", "2", "--socket-timeout", "40",
           "-o", str(out.with_suffix("")), f"https://www.youtube.com/watch?v={vid}"]
    with _YT_LOCK:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        except Exception as ex:
            log(f"    YouTube 音频下载失败：{type(ex).__name__}")
            return None
    hits = sorted(pathlib.Path(td).glob("yt*"), key=lambda x: x.stat().st_size, reverse=True)
    hits = [h for h in hits if h.stat().st_size > 100_000]
    if not hits:
        err = squeeze(((r.stderr or "") + " " + (r.stdout or ""))[-200:])
        if re.search(r"429|too many|sign in|cookies|bot", err, re.I):
            _transient["hit"] = True
            log(f"    YouTube 拦了音频下载（限流或要求登录）")
        else:
            log(f"    YouTube 音频下载没产出：{err[:110]}")
        return None
    return hits[0]


def from_audio(ep: dict, lang: str) -> dict | None:
    use_local = not ASR_KEY and local_available()
    if not ASR_KEY and not use_local:
        return None
    src_url = ep.get("audio") or ""
    if not src_url and not ep.get("youtube_id"):
        return None
    if src_url:
        try:
            raw = net.get(src_url, timeout=600, tries=2)
        except Exception as e:
            log(f"    audio download failed: {type(e).__name__}")
            _transient["hit"] = True
            return None
    else:
        raw = None      # 走 YouTube 抓音频，见下
    with tempfile.TemporaryDirectory() as td:
        if raw is None:
            got = _yt_audio(ep["youtube_id"], td)
            if not got:
                return None
            src = got
            src_url = f"https://www.youtube.com/watch?v={ep['youtube_id']}"
        else:
            src = pathlib.Path(td) / "a.mp3"
            src.write_bytes(raw)
        mb = src.stat().st_size / 1e6
        log(f"    audio {mb:.1f}MB ({'YouTube' if raw is None else 'RSS'}) -> {ASR_MODEL}")
        if use_local:
            # 本地模型直接给逐句时间戳，不用切片，也不用按字数插值
            log(f"    本地转写 {LOCAL_MODEL.split('/')[-1]}")
            segs = _local_asr(src, lang)
            if segs:
                return {"segments": segs, "source": "asr",
                        "detail": "local:" + LOCAL_MODEL.split("/")[-1], "url": src_url}
            return None
        chunks = _split(src, mb, td)
        if chunks is None:
            return None
        segs: list[dict] = []
        for offset, path in chunks:
            got = _asr_one(path, lang, span=CHUNK_SEC if len(chunks) > 1 else
                           int(ep.get("duration") or CHUNK_SEC))
            if got is None:
                return None
            for s in got:
                segs.append({"t": int(s["t"] + offset), "text": s["text"]})
        if segs:
            return {"segments": segs, "source": "asr", "detail": ASR_MODEL, "url": src_url}
    return None


def _split(src: pathlib.Path, mb: float, td: str) -> list[tuple[int, pathlib.Path]] | None:
    if mb <= ASR_MAX_MB:
        return [(0, src)]
    ff = _ffmpeg()
    if not ff:
        log(f"    audio is {mb:.0f}MB (limit {ASR_MAX_MB}MB) and ffmpeg is absent — skipping")
        return None
    out = []
    i = 0
    while True:
        dst = pathlib.Path(td) / f"c{i}.mp3"
        r = subprocess.run([ff, "-nostdin", "-v", "error", "-ss", str(i * CHUNK_SEC),
                           "-t", str(CHUNK_SEC), "-i", str(src),
                           "-ac", "1", "-ar", "16000", "-b:a", "48k", str(dst)],
                          capture_output=True)
        if r.returncode != 0 or not dst.exists() or dst.stat().st_size < 20000:
            break
        out.append((i * CHUNK_SEC, dst))
        i += 1
        if i > 40:                       # 10h ceiling; nothing legitimate is longer
            break
    return out or None


def _asr_one(path: pathlib.Path, lang: str, span: int = CHUNK_SEC) -> list[dict] | None:
    fields = {"model": ASR_MODEL, "response_format": "verbose_json",
              "timestamp_granularities[]": "segment"}
    if lang in ("en", "zh"):
        fields["language"] = lang
    try:
        r = net.post_multipart(ASR_BASE + "/audio/transcriptions", fields,
                               path.name, path.read_bytes(),
                               {"Authorization": "Bearer " + ASR_KEY})
    except Exception as e:
        log(f"    ASR failed: {type(e).__name__}: {str(e)[:120]}")
        _transient["hit"] = True
        return None
    segs = [{"t": int(s.get("start") or 0), "text": squeeze(s.get("text") or "")}
            for s in (r.get("segments") or []) if squeeze(s.get("text") or "")]
    if segs:
        return segs
    # 模型只返回整段文本（SenseVoice 就是这样）。按句切开，用字数在片内线性
    # 插值出时间。标 approx=True，页面上会明说时间戳是估算的。
    text = squeeze(r.get("text") or "")
    if not text:
        return []
    return _spread(text, span)


def _spread(text: str, span: int) -> list[dict]:
    """把一段无时间码的文本按句子切开，按字数在 span 秒内线性铺开。"""
    parts = [p for p in re.split(r"(?<=[。！？!?；;])\s*|\n+", text) if p and p.strip()]
    if len(parts) < 2:
        parts = [text[i:i + 220] for i in range(0, len(text), 220)] or [text]
    total = sum(len(p) for p in parts) or 1
    out, run = [], 0
    for p in parts:
        out.append({"t": int(span * run / total), "text": squeeze(p), "approx": True})
        run += len(p)
    return out


# ------------------------------------------------------------------ orchestrate

def _count(text: str, lang: str) -> int:
    if lang == "zh":
        return len(re.findall(r"[一-鿿]", text)) + len(re.findall(r"[A-Za-z]+", text))
    return len(re.findall(r"[A-Za-z0-9']+", text))


def chapters(ep: dict) -> list[dict]:
    """Chapter markers the show already published — free, exact anchors."""
    out = []
    for ln in strip_html(ep.get("notes")).split("\n"):
        m = _TS_LINE.match(ln)
        if not m:
            continue
        t, label = parse_ts(m.group(1)), squeeze(m.group(2))
        if t is None or not label or len(label) > 120:
            continue
        if ep.get("duration") and t > ep["duration"] + 60:
            continue
        out.append({"t": t, "label": label})
    seen, dedup = set(), []
    for c in sorted(out, key=lambda c: c["t"]):
        if c["t"] in seen:
            continue
        seen.add(c["t"])
        dedup.append(c)
    return dedup if len(dedup) >= 3 else []


_LAT = re.compile(r"[A-Z]{2,}|[A-Z][A-Za-z]{2,}|[a-z]{6,}")
_TITLE_STOP = {"podcast", "episode", "interview", "conversation", "special", "the",
               "regrets", "product", "management", "exists", "building", "about",
               "become", "should", "really", "better", "things", "people", "everything",
               "history", "coming", "finally", "biggest", "myths", "personal"}


def belongs_to(text: str, title: str) -> bool | None:
    """到手的文稿是不是这一集的？

    起因：YouTube 匹配把 Bezos 的访谈配给了 Lenny's 一集 Whatnot CPO 的节目，
    整篇深读因此基于另一集写成，署着错的嘉宾发出去了。标题相似度和时长都拦不住
    （Atom feed 不带时长），所以文稿到手后再验一次。

    两个通道一起看，因为单靠哪一个都会误杀：
      拉丁词    专有名词与长词。中英混排的技术标题真信号几乎都在这里
                （OpenClaw / Hermes / ICML / Simile）
      中文 3-gram  中文标题没有空格，整句匹配会切出「这是你该知道的一切」这种
                标题式说法，逐字稿里永远不会出现——第一版就是这么把一份正确的
                80 分钟转写误杀的。3-gram 才对得上「理想汽车」这类实体。

    命中任一通道即通过；两个通道都挑不出特征时返回 None，不做判断。
    """
    title, low = title or "", (text or "").lower()
    if not low:
        return None
    lat = [w for w in _LAT.findall(title) if w.lower() not in _TITLE_STOP and len(w) >= 3]
    lat_hit = sum(1 for w in lat if w.lower() in low)
    runs = re.findall(r"[\u4e00-\u9fff]{3,}", title)
    grams = {r[i:i + 3] for r in runs for i in range(len(r) - 2)}
    cjk_hit = sum(1 for g in grams if g in low)
    if not lat and len(grams) < 3:
        return None
    # 配错集的文稿在两个通道上都会接近 0；标题修饰语不会被念出来，所以门槛压低，
    # 宁可放过个别错配（后面还有成稿评审对照原文），也不要误杀正确的文稿。
    return lat_hit >= 1 or cjk_hit >= 2


ORDER = ("feed", "notes", "page", "youtube", "asr")


def _cache_path(ep: dict) -> pathlib.Path:
    import hashlib
    key = hashlib.sha1((ep.get("guid") or ep.get("link") or ep["title"]).encode()).hexdigest()[:20]
    return pathlib.Path(os.environ.get("PODCAST_CACHE", ".cache")) / "tr" / (key + ".json")


def _sibling_titles(ep: dict, src: dict | None) -> list[str]:
    """同一个 feed 里其他集的标题，用来识别分集列表页。取不到就返回空——
    宁可少一道检查，也不能因为取标题失败就整条取稿路径挂掉。"""
    if not src:
        return []
    try:
        from . import feeds
        return [e["title"] for e in feeds.fetch(src, cache_ttl=3600)[:25]
                if e.get("title") and e["title"] != ep.get("title")]
    except Exception:
        return []


def acquire(ep: dict, lang: str, *, allow: tuple[str, ...] = ORDER,
            src: dict | None = None) -> dict | None:
    """Walk the tiers and return the first transcript that passes the density
    check. Returns None when nothing does — the caller must then NOT publish."""
    attempts = []
    _transient["hit"] = False
    # 本地转写一集要几分钟，重跑（改了提示词、调了闸门）不该重做一遍。
    # 只缓存在本机 .cache 下，不进仓库——原文是第三方版权内容。
    cp = _cache_path(ep)
    if cp.exists():
        try:
            cached = json.loads(cp.read_text())
            if cached.get("segments"):
                log(f"    文稿取自本地缓存（{cached.get('source')}，"
                    f"{cached.get('words')} 字）")
                return cached
        except Exception:
            pass
    if src and not ep.get("youtube_id") and "youtube" in allow:
        vid = match_youtube(ep, src)
        if vid:
            ep["youtube_id"] = vid          # also gives the page clickable timestamps
    for tier in ORDER:
        if tier not in allow:
            continue
        try:
            got = {"feed": lambda: from_feed(ep),
                   "notes": lambda: from_notes(ep, lang),
                   "page": lambda: from_page(ep, lang, _sibling_titles(ep, src)),
                   "youtube": lambda: from_youtube(ep.get("youtube_id") or "", lang),
                   "asr": lambda: from_audio(ep, lang)}[tier]()
        except Exception as e:
            log(f"    tier {tier} raised {type(e).__name__}: {str(e)[:100]}")
            got = None
        if not got or not got.get("segments"):
            attempts.append(f"{tier}:none")
            continue
        text = "\n".join(s["text"] for s in got["segments"])
        words = _count(text, lang)
        if words < MIN_WORDS.get(lang, 1200):
            attempts.append(f"{tier}:short({words}w)")
            log(f"    tier {tier}: only {words} words — too short to be an episode "
                f"transcript, rejected")
            continue
        # A timed transcript knows the runtime better than a feed that omits it.
        if not ep.get("duration") and got["segments"] and not got["segments"][-1].get("approx"):
            ep["duration"] = int(got["segments"][-1]["t"]) + 30
            log(f"    duration was missing; taking {ep['duration']}s from the transcript")
        dur_min = max((ep.get("duration") or 0) / 60, 1)
        wpm = words / dur_min
        if wpm < MIN_WPM.get(lang, 70):
            attempts.append(f"{tier}:thin({wpm:.0f}wpm)")
            log(f"    tier {tier}: {words} words = {wpm:.0f} wpm, too thin — rejected")
            continue
        if wpm > MAX_WPM.get(lang, 300):
            attempts.append(f"{tier}:bloated({wpm:.0f}wpm)")
            log(f"    tier {tier}: {words} words = {wpm:.0f} wpm — that is not this "
                f"episode's transcript, rejected")
            continue
        # 归属校验：拿到的文稿必须能和这一集的标题对上，否则就是配错了集。
        # 官方 feed 里带的逐字稿天然属于这一集，不必验。
        if tier != "feed":
            ok = belongs_to(text, ep.get("title", ""))
            if ok is False:
                attempts.append(f"{tier}:wrong-episode")
                log(f"    tier {tier}: 文稿里找不到标题中的任何识别性词——"
                    f"这份文稿不属于这一集，丢弃")
                _transient["hit"] = False
                continue
        got.update(words=words, wpm=round(wpm, 1), chars=len(text),
                   timed=not all(s.get("approx") for s in got["segments"]),
                   attempts=attempts + [f"{tier}:ok"])
        log(f"    transcript via {tier} ({got['detail']}): {words} words, "
            f"{wpm:.0f} wpm, {len(got['segments'])} segments")
        # 时间戳定下来了，现在才能判断挂着的视频和它是不是同一条时间轴。
        # 不一致就把视频摘掉：时间戳退回音频，比每个都跳错好。
        if ep.get("youtube_id"):
            ok, why = video_aligned(ep, tier == "youtube")
            if ok:
                log(f"    视频可跳时间戳：{why}")
            elif ok is None:
                # 查不出来就保持原样：视频是搜索阶段按时长挑出来的，本来就过了一遍。
                # 因为一次 429 把它摘掉，等于让临时故障造成永久损失。
                log(f"    视频暂时无法核对（{why}），保留，下次 audit 再查")
            else:
                log(f"    摘掉视频（{why}）")
                ep["youtube_id"] = ""
        try:
            cp.parent.mkdir(parents=True, exist_ok=True)
            cp.write_text(json.dumps(got, ensure_ascii=False))
        except Exception:
            pass
        return got
    log(f"    no usable transcript ({', '.join(attempts)})")
    return None


def flatten(segs: list[dict], *, stamp_every: int = 60) -> str:
    """Transcript rendered for the model: a [mm:ss] anchor roughly every minute
    so quoted timestamps can be grounded instead of invented."""
    out, last = [], -10 ** 9
    for s in segs:
        if s["t"] - last >= stamp_every:
            out.append(f"\n[{hhmmss(s['t'])}] ")
            last = s["t"]
        spk = s.get("spk")
        out.append((f"{spk}: " if spk else "") + s["text"] + " ")
    return squeeze("".join(out).replace(" \n", "\n"))


# ------------------------------------------------- 视频和音频是不是同一条时间轴

def yt_length(vid: str) -> int:
    """这个视频多长（秒）。拿不到返回 0。

    走 watch 页里的 lengthSeconds，不依赖 yt-dlp——护栏必须在每条线上都能跑，
    包括只装了 certifi 的快车道。
    """
    if not vid:
        return 0
    try:
        h = net.get_text("https://www.youtube.com/watch?v=" + vid, timeout=25, tries=2,
                         headers={"Accept-Language": "en-US,en;q=0.9"})
    except Exception:
        return 0
    m = re.search(r'"lengthSeconds":"(\d+)"', h)
    return int(m.group(1)) if m else 0


def seek_tolerance(dur: int) -> int:
    """时间戳允许错多少秒。

    这个站的承诺是"点一下回到它被说出的那一秒"，所以宽容度必须小到读者察觉不到。
    实测对得上的那些差 0～28 秒（编码和片头微差），差 84 秒的那条是另一个剪辑版本
    ——一分半的偏移已经跳到别的话上了，宁可不给视频。
    """
    return min(90, max(40, int(dur * 0.01)))


def video_aligned(ep: dict, from_youtube_tier: bool,
                  known_len: int = 0) -> tuple[bool | None, str]:
    """挂在这一集上的视频，能不能拿来跳时间戳。

    为什么需要这道检查：频道 Atom feed **不带时长**，所以 match_youtube 只能比标题，
    而片花、预告、剪辑片段的标题和正片几乎一样。线上真实后果——80,000 Hours 挂了
    176 秒的片花（音频 2968 秒）、Lenny's 挂了 133 秒、Acquired 挂了 1674 秒对
    14360 秒的正片。这些页面上**每一个**时间戳都跳错，比没有视频糟得多。

    文稿本来就是从视频字幕来的，时间轴就是视频自己的，不用查也不该查。

    三态，不是两态：True 验过一致、False 验过不一致、**None 这次查不出来**。
    第三个必须和第二个分开——YouTube 会 429，而"限流"和"挂错了视频"在返回值上
    长得一样。把 None 当 False 处理，就会因为一次限流把好视频摘掉。
    """
    vid = ep.get("youtube_id")
    if not vid:
        return False, "没有视频"
    if from_youtube_tier:
        return True, "文稿即视频字幕，时间轴天然一致"
    dur = int(ep.get("duration") or 0)
    if not dur:
        return None, "音频没有时长，无法验证是不是同一个剪辑"
    # 搜索那一步已经拿到过时长就别再抓一次：多一次网络请求就多一次"抖一下就把
    # 好视频判死"的机会，而这个判死是静默的。
    vlen = known_len or yt_length(vid)
    if not vlen:
        return None, "拿不到视频时长（限流或页面变了），这次无法验证"
    ep["video_len"] = vlen        # 存下来，守护测试才能离线复查这一条
    tol = seek_tolerance(dur)
    diff = abs(vlen - dur)
    if diff <= tol:
        return True, f"音频 {dur}s / 视频 {vlen}s，差 {diff}s（容差 {tol}s）"
    return False, f"音频 {dur}s / 视频 {vlen}s，差 {diff}s > 容差 {tol}s——不是同一个剪辑"
