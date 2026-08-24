"""Small shared helpers: ids, time formatting, text normalisation, dedupe keys."""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import re
import unicodedata

_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")
# Punctuation/casing/spacing all vary between an RSS title and a YouTube title
# for the same episode, so the dedupe key strips everything but letters+digits.
_NOISE = re.compile(r"[^0-9a-z一-鿿]+")
# Leading episode numbers: "#430 Title", "430. Title", "Ep 12 - Title", "151. Title".
_EPNUM = re.compile(r"^\s*(?:#|ep(?:isode)?\.?\s*)?\d{1,4}\s*[-–—:|.]\s*"
                    r"|^\s*#\d{1,4}\s+", re.I)


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", s)
    s = _TAG.sub(" ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    return "\n".join(ln.strip() for ln in s.split("\n")).strip()


def squeeze(s: str | None) -> str:
    return _WS.sub(" ", (s or "")).strip()


def norm_title(t: str) -> str:
    """Canonical form of an episode title, for cross-source dedupe."""
    t = unicodedata.normalize("NFKC", strip_html(t)).lower()
    t = _EPNUM.sub("", t)
    return _NOISE.sub("", t)


def fingerprint(title: str, seconds: int | None) -> str:
    """Dedupe key: normalised title + duration bucket (2-minute granularity).

    Same episode published to an RSS feed and a YouTube channel differs in
    title punctuation and by a few seconds of intro, never by 2 minutes.
    """
    bucket = "" if not seconds else str(int(seconds) // 120)
    return hashlib.sha1((norm_title(title) + "|" + bucket).encode()).hexdigest()[:16]


def eid(source_id: str, guid: str) -> str:
    return source_id + "-" + hashlib.sha1(guid.encode()).hexdigest()[:10]


def slugify(s: str, maxlen: int = 60) -> str:
    s = unicodedata.normalize("NFKC", strip_html(s)).lower()
    s = re.sub(r"[^\w一-鿿]+", "-", s, flags=re.U).strip("-")
    return (s[:maxlen].rstrip("-") or "episode")


def parse_duration(v: str | int | None) -> int | None:
    """iTunes <itunes:duration> is seconds, mm:ss, or hh:mm:ss depending on host."""
    if v is None:
        return None
    if isinstance(v, int):
        return v or None
    v = str(v).strip()
    if not v:
        return None
    if v.isdigit():
        return int(v)
    parts = v.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    sec = 0.0
    for n in nums:
        sec = sec * 60 + n
    # 0 is a valid timestamp. Callers that mean "duration unknown" already test
    # truthiness, so returning 0 here keeps parse_ts("0:00") == 0 honest.
    return int(sec)


def hhmmss(sec: float | int | None) -> str:
    if sec is None:
        return ""
    sec = int(sec)
    h, rem = divmod(max(sec, 0), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_ts(v: str | int | float | None) -> int | None:
    """Accept 123, '1:23', '01:02:03', '1h2m3s' -> seconds."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return int(v)
    v = str(v).strip()
    if re.fullmatch(r"\d+", v):
        return int(v)
    if ":" in v:
        return parse_duration(v)
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", v, re.I)
    if m and any(m.groups()):
        h, mi, s = (int(x or 0) for x in m.groups())
        return h * 3600 + mi * 60 + s
    return None


def iso(d: dt.datetime | None) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if d else ""


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


_SECRET = re.compile(r"(gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}"
                     r"|sk-[A-Za-z0-9\-_]{16,}|gsk_[A-Za-z0-9]{20,})")


def redact(s: object) -> str:
    """Every log line goes through this. Tokens must never reach a run log."""
    return _SECRET.sub("[REDACTED]", str(s))


def log(*a) -> None:
    print(" ".join(redact(x) for x in a), flush=True)
