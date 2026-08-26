#!/usr/bin/env python3
"""Render data/ into the static site.

Everything is pre-rendered HTML: the feed works with JavaScript off, and search
only hides rows that are already in the document. That keeps the page fast and
keeps every episode indexable, which a client-rendered feed does not.
"""
from __future__ import annotations

import html
import json
import os
import pathlib
import re
import shutil
import sys
import urllib.parse
from xml.sax.saxutils import escape as xesc

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lib.util import hhmmss, log, squeeze                      # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
BASE = os.environ.get("PODCAST_BASE", "/podcast").rstrip("/")
SITE = os.environ.get("PODCAST_SITE", "https://ourword.ai") + BASE
NAME = "原声"
TAGLINE = "世界太吵，来原声听播客"
def _n_sources() -> int:
    try:
        return len(json.loads((DATA / "sources.json").read_text())["sources"])
    except Exception:
        return 0


def _blurb() -> str:
    n = _n_sources()
    head = f"每天从 {n} 档中英文播客里挑出值得记住的判断。" if n else "每天从中英文播客里挑出值得记住的判断。"
    return (head + "要点和金句都带时间戳，点一下就回到它在原声里被说出的那一秒；"
            "金句逐字校验过、数字回原文核对过——查不到出处的，一律不上站。")

CAT_ORDER = ["ai", "biz", "cn", "ideas", "hist", "parent"]
CAT_LABEL = {
    "ai": "AI / 技术",
    "biz": "投资 / 商业",
    "cn": "中国视角",
    "ideas": "人文 / 思想",
    "hist": "历史",
    "parent": "育儿",
}

BLURB = ""


def e(s) -> str:
    return html.escape(str(s or ""), quote=True)


def load() -> tuple[list[dict], dict]:
    srcs = json.loads((DATA / "sources.json").read_text())
    eps = []
    for f in sorted((DATA / "episodes").glob("*.json")):
        try:
            eps.append(json.loads(f.read_text()))
        except Exception as ex:
            log(f"  ! skipping unreadable {f.name}: {type(ex).__name__}")
    eps.sort(key=lambda x: (x.get("published") or ""), reverse=True)
    return eps, srcs


GA_ID = os.environ.get("GA_ID", "G-DHD3WEXQ8T")
ROBOTS = "index,follow,max-snippet:-1,max-image-preview:large,max-video-preview:-1"


def head(title: str, desc: str, *, path: str = "/", image: str = "",
         extra: str = "", robots: str = ROBOTS, published: str = "",
         modified: str = "") -> str:
    url = SITE + path
    ga = (f'<script async src="https://www.googletagmanager.com/gtag/js?id={GA_ID}"></script>\n'
          f'<script>window.dataLayer=window.dataLayer||[];function gtag(){{dataLayer.push(arguments)}}\n'
          f"gtag('js',new Date());gtag('config','{GA_ID}');</script>") if GA_ID else ""
    dates = ""
    if published:
        dates += f'<meta property="article:published_time" content="{e(published)}">\n'
    if modified:
        dates += f'<meta property="article:modified_time" content="{e(modified)}">\n'
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<meta name="description" content="{e(desc)}">
<meta name="robots" content="{e(robots)}">
<link rel="canonical" href="{e(url)}">
<meta property="og:type" content="{'article' if published else 'website'}">
<meta property="og:site_name" content="{e(NAME)}">
<meta property="og:locale" content="zh_CN">
<meta property="og:title" content="{e(title)}">
<meta property="og:description" content="{e(desc)}">
<meta property="og:url" content="{e(url)}">
{f'<meta property="og:image" content="{e(image)}">' if image else ''}
{dates}<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{e(title)}">
<meta name="twitter:description" content="{e(desc)}">
{f'<meta name="twitter:image" content="{e(image)}">' if image else ''}
<link rel="icon" type="image/svg+xml" href="{BASE}/icon.svg">
<link rel="apple-touch-icon" href="{BASE}/icon.svg">
<link rel="alternate" type="application/rss+xml" title="{e(NAME)}" href="{BASE}/feed.xml">
<link rel="stylesheet" href="{BASE}/assets/site.css">
<script>try{{var t=localStorage.getItem('podcast-theme');if(t)document.documentElement.setAttribute('data-theme',t)}}catch(e){{}}</script>
{ga}
{extra}
</head>
<body>
"""


ICON_SEARCH = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
               'stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>')
ICON_THEME = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
              'stroke-linecap="round"><path d="M12 3v1.5M12 19.5V21M3 12h1.5M19.5 12H21'
              'M5.6 5.6l1.1 1.1M17.3 17.3l1.1 1.1M18.4 5.6l-1.1 1.1M6.7 17.3l-1.1 1.1"/>'
              '<circle cx="12" cy="12" r="4"/></svg>')
ICON_SHARE = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
              'stroke-linecap="round" stroke-linejoin="round">'
              '<path d="M12 3v11M12 3 8.5 6.5M12 3l3.5 3.5"/>'
              '<path d="M5 12v7.5a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V12"/></svg>')


def share_button(text: str, *, url: str, title: str, label: str = "分享") -> str:
    return (f'<button class="share-btn" type="button" data-share '
            f'data-share-title="{e(title)}" data-share-url="{e(url)}" '
            f'data-share-text="{e(text).replace(chr(10), "&#10;")}" '
            f'aria-label="复制分享文本">{ICON_SHARE}<span>{e(label)}</span></button>')


def _clip(s: str, n: int) -> str:
    s = squeeze(s or "")
    return s if len(s) <= n else s[:n - 1].rstrip("，。、；：,. ") + "…"


def episode_share_text(ep: dict) -> str:
    d = ep["digest"]
    src = ep.get("source_zh") or ep.get("source") or ""
    mins = int((ep.get("duration") or 0) // 60)
    meta = " · ".join(x for x in (src, f"{mins} 分钟" if mins else "", f"{NAME}深读") if x)
    lines = [f"《{_clip(d.get('title'), 40)}》", meta, ""]
    if d.get("dek"):
        lines += [_clip(d["dek"], 100), ""]
    for pt in (d.get("points") or [])[:3]:
        lines.append("· " + _clip(pt.get("h"), 34))
    q = next((x for x in (d.get("quotes") or []) if x.get("zh") or x.get("raw")), None)
    if q:
        lines += ["", "「" + _clip(q.get("zh") or q.get("raw"), 76) + "」"
                  + (f" — {q['spk']}" if q.get("spk") else "")]
    lines += ["", ep_url(ep)]
    return "\n".join(lines)


def ep_url(ep: dict) -> str:
    return f"{SITE}/e/{ep['id']}/"


def alias_page(ep: dict) -> str:
    real = f"{BASE}/p/{urllib.parse.quote(ep['slug'])}/"
    full = f"{SITE}/p/{urllib.parse.quote(ep['slug'])}/"
    t = e(ep["digest"].get("title") or "")
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{t} — {e(NAME)}</title>
<meta name="robots" content="noindex,follow">
<link rel="canonical" href="{e(full)}">
<meta http-equiv="refresh" content="0;url={e(real)}">
<script>location.replace({json.dumps(real)})</script>
</head><body>
<p>正在打开《{t}》…… 没有自动跳转的话
<a href="{e(real)}">点这里</a>。</p>
</body></html>
"""


def source_share_text(src: dict, rows: list[dict]) -> str:
    name = src.get("zh") or src.get("name") or ""
    lines = [f"《{_clip(name, 34)}》· {NAME}深读", ""]
    if src.get("desc"):
        lines += [_clip(src["desc"], 96), ""]
    for x in rows[:3]:
        lines.append("· " + _clip(x["digest"].get("title"), 34))
    lines += ["", f"本站已深读 {len(rows)} 篇：{SITE}/s/{src['id']}/"]
    return "\n".join(lines)


def site_share_text(eps: list[dict]) -> str:
    lines = [f"{NAME} · {TAGLINE}", "",
             _clip(BLURB, 110), ""]
    for x in eps[:3]:
        src = x.get("source_zh") or x.get("source") or ""
        lines.append(f"· {_clip(x['digest'].get('title'), 30)}（{_clip(src, 14)}）")
    lines += ["", f"{SITE}/"]
    return "\n".join(lines)


def masthead(n: int | None, *, home: bool) -> str:
    mark = (f'<h1>{NAME}<span class="dot">.</span></h1>' if home
            else f'<span class="wordmark">{NAME}<span class="dot">.</span></span>')
    brand = (f'<div class="brand">{mark}</div>' if home
             else f'<a class="brand" href="{BASE}/">{mark}</a>')
    count = f'<span class="stat">{n} 篇深读</span>' if n else ""
    return f"""<header class="mast"><div class="wrap">
<div class="mast-top">
{brand}
<div class="mast-side">
{count}
<a class="pill ghost" href="{BASE}/sources/">信源</a>
<a class="pill ghost" href="{BASE}/feed.xml">RSS</a>
<button class="icon-btn" data-theme-toggle aria-label="切换深浅色">{ICON_THEME}</button>
</div></div>
<p class="slogan">{TAGLINE}</p>
</div></header>"""


def foot() -> str:
    return f"""<footer class="foot"><div class="wrap"><div class="foot-in">
<div>{NAME} · <a href="https://ourword.ai">OurWord.ai</a> 的播客线。内容为原播客的中文深读，
版权归各节目所有；每篇都附原节目链接，请去支持原作者。</div>
<div class="links"><a href="{BASE}/">首页</a><a href="{BASE}/sources/">信源</a>
<a href="{BASE}/log/">更新日志</a><a href="{BASE}/feed.xml">RSS</a>
<a href="{BASE}/llms.txt">llms.txt</a>
<a href="https://github.com/woowoeth/podcast">源码</a></div>
</div></div></footer>
<script src="{BASE}/assets/site.js" defer></script>
</body></html>
"""


TSRC_LABEL = {"feed": "官方逐字稿", "notes": "官方全文", "page": "官方文稿页",
              "youtube": "YouTube 字幕", "asr": "音频转写"}


def card(ep: dict, *, hero: bool) -> str:
    d = ep["digest"]
    q = d.get("quality") or {}
    date = (ep.get("published") or "")[5:10].replace("-", "")
    hay = " ".join([d.get("title", ""), d.get("dek", ""), ep.get("source", ""),
                    ep.get("source_zh", ""), ep.get("title_original", ""),
                    " ".join(d.get("tags") or []),
                    " ".join(t.get("term", "") for t in d.get("terms") or [])])
    img = ep.get("image") or ""
    cover = (f'<img src="{e(img)}" alt="" loading="lazy" decoding="async" '
             f'data-initial="{e((ep.get("source") or "?")[:1])}">' if img
             else f'<div class="fallback">{e((ep.get("source") or "?")[:1])}</div>')
    dur = (f'<span class="dur">{hhmmss(ep["duration"])}</span>' if ep.get("duration") else "")
    tags = "".join(f'<span class="tag">{e(t)}</span>' for t in (d.get("tags") or [])[:2])
    src_label = ep.get("source_zh") or ep.get("source") or ""
    return f"""<a class="card{' hero' if hero else ''}" data-card data-cat="{e(ep.get('cat'))}"
 data-hay="{e(hay)}" href="{BASE}/p/{e(ep['slug'])}/">
<div class="cover">{cover}{dur}</div>
<div class="card-body">
<div class="kicker" data-cat="{e(ep.get('cat'))}"><span class="src">{e(src_label)}</span>
<span class="date">{e(date)}</span></div>
<h2>{e(d.get('title'))}</h2>
<p class="dek">{e(d.get('dek'))}</p>
<div class="card-foot">
<span class="badge"><b>{q.get('points', 0)}</b> 要点</span>
<span class="badge"><b>{q.get('verified_quotes', 0)}</b> 金句</span>
{tags}
</div></div></a>"""


def search_index(eps: list[dict]) -> str:
    rows = []
    for x in eps:
        d = x["digest"]
        parts = [d.get("title", ""), d.get("dek", ""), d.get("why", ""),
                 d.get("who", ""), x.get("source", ""), x.get("source_zh", ""),
                 x.get("title_original", ""), " ".join(d.get("tags") or [])]
        for p in d.get("points") or []:
            parts += [p.get("h", ""), p.get("body", ""), p.get("spk", "")]
        for q in d.get("quotes") or []:
            parts += [q.get("raw", ""), q.get("zh", ""), q.get("spk", "")]
        for f in d.get("facts") or []:
            parts += [f.get("k", ""), f.get("v", "")]
        for t in d.get("terms") or []:
            parts += [t.get("term", ""), t.get("zh", ""), t.get("def", "")]
        rows.append({"s": x["slug"], "h": squeeze(" ".join(parts)).lower()[:5000]})
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


def _ld(obj: dict) -> str:
    return ('<script type="application/ld+json">'
            + json.dumps(obj, ensure_ascii=False) + "</script>")


def _publisher() -> dict:
    return {"@type": "Organization", "name": "OurWord.ai", "url": "https://ourword.ai/"}


def index_page(eps: list[dict], srcs: dict) -> str:
    counts = {c: sum(1 for x in eps if x.get("cat") == c) for c in CAT_ORDER}
    chips = [f'<button class="chip" data-cat-chip="all" aria-pressed="true">全部'
             f'<span class="n">{len(eps)}</span></button>']
    for c in CAT_ORDER:
        chips.append(f'<button class="chip" data-cat-chip="{c}" aria-pressed="false">'
                     f'{CAT_LABEL[c]}<span class="n">{counts.get(c, 0)}</span></button>')
    cards = "\n".join(card(x, hero=(i == 0)) for i, x in enumerate(eps))
    ld = _ld({"@context": "https://schema.org", "@graph": [
        {"@type": "WebSite", "@id": SITE + "/#site", "url": SITE + "/",
         "name": NAME, "alternateName": "OurWord Podcast", "description": BLURB,
         "inLanguage": "zh-CN", "publisher": _publisher(),
         "potentialAction": {"@type": "SearchAction",
                             "target": {"@type": "EntryPoint",
                                        "urlTemplate": SITE + "/?q={search_term_string}"},
                             "query-input": "required name=search_term_string"}},
        {"@type": "CollectionPage", "@id": SITE + "/#page", "url": SITE + "/",
         "name": f"{NAME} — {TAGLINE}", "isPartOf": {"@id": SITE + "/#site"},
         "inLanguage": "zh-CN",
         "mainEntity": {"@type": "ItemList", "numberOfItems": len(eps),
                        "itemListElement": [
                            {"@type": "ListItem", "position": i + 1,
                             "url": f"{SITE}/p/{x['slug']}/",
                             "name": x["digest"].get("title")}
                            for i, x in enumerate(eps[:60])]}}]})
    return (head(TAGLINE, BLURB, path="/",
                 image=(eps[0].get("image") if eps else ""), extra=ld)
            + masthead(len(eps), home=True)
            + f"""
<div class="toolbar"><div class="wrap"><div class="toolbar-in">
<label class="search">{ICON_SEARCH}
<input data-search type="search" placeholder="搜正文、金句、数字、术语、节目…" aria-label="搜索">
<kbd>/</kbd></label>
<div class="chips">{''.join(chips)}</div>
{share_button(site_share_text(eps), url=SITE + "/", title=f"{NAME} · {TAGLINE}", label="分享本站")}
</div></div></div>

<main class="wrap"><div class="feed" data-feed>
{cards}
<div class="empty" data-empty hidden><b>没有匹配的深读</b>
<p>换个词，或者清掉筛选再试。搜索会搜进每条要点的正文、金句的中英文原文、
数字和术语表——不只是标题。</p>
<p data-deep-note hidden style="color:var(--faint);font-size:13px"></p></div>
</div></main>
""" + foot())


# NOTE: remainder of build.py unchanged from upstream — episode/sources/log/llms/rss/sitemap/main
# This truncated push is INVALID. Use full file from local instead.
"""
