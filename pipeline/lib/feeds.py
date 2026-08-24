"""Feed parsing: RSS 2.0 (+ the podcast namespace) and YouTube's Atom feed.

Deliberately one parser for both, because a show can appear in either place and
downstream code should not care which door an episode came through.
"""
from __future__ import annotations

import datetime as dt
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from . import net
from .util import parse_duration, squeeze, strip_html

NS = {
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "podcast": "https://podcastindex.org/namespace/1.0",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "atom": "http://www.w3.org/2005/Atom",
    "media": "http://search.yahoo.com/mrss/",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "dc": "http://purl.org/dc/elements/1.1/",
}

YT_WATCH = "https://www.youtube.com/watch?v="


def _txt(el, path: str) -> str:
    if el is None:
        return ""
    f = el.find(path, NS)
    return squeeze(f.text) if f is not None and f.text else ""


def _date(s: str) -> dt.datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def _yt_id(url: str) -> str | None:
    m = re.search(r"(?:youtu\.be/|[?&]v=|/embed/|/shorts/)([\w-]{11})", url or "")
    return m.group(1) if m else None


def parse(xml: bytes, source: dict) -> list[dict]:
    """Return normalised episode dicts, newest first."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        # A few hosts emit stray control chars that break the strict parser.
        cleaned = re.sub(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]", b"", xml)
        root = ET.fromstring(cleaned)
    tag = root.tag.split("}")[-1]
    return _atom(root, source) if tag == "feed" else _rss(root, source)


def _rss(root, source: dict) -> list[dict]:
    ch = root.find("channel")
    if ch is None:
        return []
    show_art = ""
    for p in ("itunes:image", "image/url"):
        el = ch.find(p, NS)
        if el is not None:
            show_art = (el.get("href") or squeeze(el.text) or "")
            if show_art:
                break
    out = []
    for it in ch.findall("item"):
        title = _txt(it, "title")
        if not title:
            continue
        guid = _txt(it, "guid") or _txt(it, "link") or title
        enc = it.find("enclosure")
        audio = enc.get("url") if enc is not None else ""
        if not audio:
            for m in it.findall("media:content", NS):
                if (m.get("type") or "").startswith("audio"):
                    audio = m.get("url") or ""
                    break
        art = ""
        for p in ("itunes:image", "media:thumbnail"):
            el = it.find(p, NS)
            if el is not None:
                art = el.get("href") or el.get("url") or ""
                if art:
                    break
        notes = (_txt(it, "content:encoded") or _txt(it, "description")
                 or _txt(it, "itunes:summary"))
        # Official machine-readable transcripts, when the show ships them.
        tr = []
        for t in it.findall("podcast:transcript", NS):
            u, ty = t.get("url"), (t.get("type") or "")
            if u:
                tr.append({"url": u, "type": ty, "lang": t.get("language") or ""})
        link = _txt(it, "link")
        out.append({
            "source_id": source["id"], "source": source["name"],
            "guid": guid, "title": strip_html(title), "link": link,
            "published": _date(_txt(it, "pubDate") or _txt(it, "dc:date")),
            "duration": parse_duration(_txt(it, "itunes:duration")),
            "audio": audio, "image": art or show_art,
            "notes": notes,
            "episode_no": _txt(it, "itunes:episode"),
            "explicit": _txt(it, "itunes:explicit").lower() in ("yes", "true"),
            "transcripts": tr,
            "youtube_id": _yt_id(link) or _yt_id(notes),
            "feed_kind": "rss",
        })
    return out


def _atom(root, source: dict) -> list[dict]:
    out = []
    for e in root.findall("atom:entry", NS):
        vid = _txt(e, "yt:videoId")
        title = _txt(e, "atom:title")
        if not title:
            continue
        link_el = e.find("atom:link", NS)
        link = (link_el.get("href") if link_el is not None else "") or (YT_WATCH + vid)
        grp = e.find("media:group", NS)
        thumb = ""
        desc = ""
        if grp is not None:
            th = grp.find("media:thumbnail", NS)
            thumb = th.get("url") if th is not None else ""
            desc = _txt(grp, "media:description")
        out.append({
            "source_id": source["id"], "source": source["name"],
            "guid": vid or link, "title": strip_html(title), "link": link,
            "published": _date(_txt(e, "atom:published") or _txt(e, "atom:updated")),
            "duration": None,                 # Atom carries none; filled later
            "audio": "", "image": thumb, "notes": desc,
            "episode_no": "", "explicit": False, "transcripts": [],
            "youtube_id": vid, "feed_kind": "youtube",
        })
    return out


def fetch(source: dict, *, cache_ttl: int = 1800) -> list[dict]:
    xml = net.get(source["feed"], cache_ttl=cache_ttl,
                  accept="application/rss+xml, application/atom+xml, application/xml, */*")
    eps = parse(xml, source)
    eps.sort(key=lambda e: e["published"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
             reverse=True)
    return eps
