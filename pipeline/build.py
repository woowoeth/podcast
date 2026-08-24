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
    """信源数从 sources.json 读，别写死——加了源之后文案会悄悄过期。"""
    try:
        return len(json.loads((DATA / "sources.json").read_text())["sources"])
    except Exception:
        return 0


def _blurb() -> str:
    n = _n_sources()
    head = f"每天从 {n} 档中英文播客里挑出值得记住的判断。" if n else "每天从中英文播客里挑出值得记住的判断。"
    return (head + "要点和金句都带时间戳，点一下就回到它在原声里被说出的那一秒；"
            "金句逐字校验过、数字回原文核对过——查不到出处的，一律不上站。")

CAT_ORDER = ["ai", "biz", "cn"]
CAT_LABEL = {"ai": "AI / 技术", "biz": "投资 / 商业", "cn": "中国视角"}


BLURB = ""          # 首次 build 时填充（要先读到 data/sources.json）


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


# --------------------------------------------------------------------- chrome

def head(title: str, desc: str, *, path: str = "/", image: str = "",
         extra: str = "") -> str:
    url = SITE + path
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<meta name="description" content="{e(desc)}">
<link rel="canonical" href="{e(url)}">
<meta property="og:type" content="website">
<meta property="og:title" content="{e(title)}">
<meta property="og:description" content="{e(desc)}">
<meta property="og:url" content="{e(url)}">
{f'<meta property="og:image" content="{e(image)}">' if image else ''}
<meta name="twitter:card" content="summary_large_image">
<link rel="icon" type="image/svg+xml" href="{BASE}/icon.svg">
<link rel="apple-touch-icon" href="{BASE}/icon.svg">
<link rel="alternate" type="application/rss+xml" title="{e(NAME)}" href="{BASE}/feed.xml">
<link rel="stylesheet" href="{BASE}/assets/site.css">
<script>try{{var t=localStorage.getItem('podcast-theme');if(t)document.documentElement.setAttribute('data-theme',t)}}catch(e){{}}</script>
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


def masthead(n: int | None, *, home: bool) -> str:
    """字标和右侧那几个入口同一行，slogan 独占下一行。

    原来 slogan 在 .brand 里面，于是 .brand 整块占满宽度，右侧那几项在窄屏被挤到
    单独一行。把 slogan 提出来做兄弟节点，字标左、入口右，任何宽度都成立。
    n 为 None 时不显示篇数（单集页用）。
    """
    mark = f'<h1>{NAME}<span class="dot">.</span></h1>'
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
<a href="{BASE}/feed.xml">RSS</a><a href="https://github.com/woowoeth/podcast">源码</a></div>
</div></div></footer>
<script src="{BASE}/assets/site.js" defer></script>
</body></html>
"""


# ----------------------------------------------------------------------- feed

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
    """Everything worth searching, in one lazily-fetched file.

    The cards carry a short haystack inline so search works before the fetch
    lands, but the real index includes the body of every point, every quote (in
    both languages), the facts and the glossary. That is the whole point of
    having verifiable structure: the reader can find the episode where someone
    actually said a thing, which a title-only search cannot do.
    """
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
        # Cap each row so the index stays fetchable as the archive grows:
        # ~5KB per episode is 1MB raw at 200 episodes, and Pages serves it
        # gzipped at roughly a fifth of that.
        rows.append({"s": x["slug"], "h": squeeze(" ".join(parts)).lower()[:5000]})
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


def index_page(eps: list[dict], srcs: dict) -> str:
    counts = {c: sum(1 for x in eps if x.get("cat") == c) for c in CAT_ORDER}
    chips = [f'<button class="chip" data-cat-chip="all" aria-pressed="true">全部'
             f'<span class="n">{len(eps)}</span></button>']
    for c in CAT_ORDER:
        if counts.get(c):
            chips.append(f'<button class="chip" data-cat-chip="{c}" aria-pressed="false">'
                         f'{CAT_LABEL[c]}<span class="n">{counts[c]}</span></button>')
    cards = "\n".join(card(x, hero=(i == 0)) for i, x in enumerate(eps))
    # TAGLINE 里已经有"原声"，再前缀 NAME 会让标题出现两次品牌名
    return (head(TAGLINE, BLURB, path="/",
                 image=(eps[0].get("image") if eps else ""))
            + masthead(len(eps), home=True)
            + f"""
<div class="toolbar"><div class="wrap"><div class="toolbar-in">
<label class="search">{ICON_SEARCH}
<input data-search type="search" placeholder="搜正文、金句、数字、术语、节目…" aria-label="搜索">
<kbd>/</kbd></label>
<div class="chips">{''.join(chips)}</div>
</div></div></div>

<main class="wrap"><div class="feed" data-feed>
{cards}
<div class="empty" data-empty hidden><b>没有匹配的深读</b>
<p>换个词，或者清掉筛选再试。搜索会搜进每条要点的正文、金句的中英文原文、
数字和术语表——不只是标题。</p>
<p data-deep-note hidden style="color:var(--faint);font-size:13px"></p></div>
</div></main>
""" + foot())


# -------------------------------------------------------------- episode page

def seek_href(ep: dict, t: int) -> str:
    """Where a timestamp points when there is no inline player to seek."""
    if ep.get("youtube_id"):
        return f"https://www.youtube.com/watch?v={ep['youtube_id']}&t={int(t)}s"
    if ep.get("audio"):
        return f"{ep['audio']}#t={int(t)}"
    return ep.get("link") or "#"


def episode_page(ep: dict, prev: dict | None, nxt: dict | None) -> str:
    d = ep["digest"]
    q = d.get("quality") or {}
    src_label = ep.get("source_zh") or ep.get("source") or ""
    date = (ep.get("published") or "")[:10]

    def ts(t, cls="ts"):
        return (f'<a class="{cls}" data-t="{int(t)}" href="{e(seek_href(ep, t))}" '
                f'target="_blank" rel="noopener">{hhmmss(t)}</a>')

    points = "\n".join(
        f"""<div class="point">{ts(p['t'])}<div><h4>{e(p['h'])}</h4>
<p class="body">{e(p['body'])}</p>
{f'<span class="spk">— {e(p["spk"])}</span>' if p.get('spk') else ''}</div></div>"""
        for p in d.get("points") or [])

    quotes = "\n".join(
        f"""<blockquote class="quote"><p class="raw">{e(qq['raw'])}</p>
{f'<p class="zh">{e(qq["zh"])}</p>' if qq.get('zh') else ''}
<div class="attrib">{f'<b>{e(qq["spk"])}</b>' if qq.get('spk') else ''}{ts(qq['t'])}</div>
</blockquote>"""
        for qq in d.get("quotes") or [])

    facts = ""
    if d.get("facts"):
        rows = "\n".join(
            f'<tr><td class="k">{e(f["k"])}</td><td class="v">{e(f["v"])}</td>'
            f'<td class="t">{ts(f["t"]) if f.get("t") is not None else ""}</td></tr>'
            for f in d["facts"])
        facts = f'<section class="section"><h3>数字与实体</h3><table class="facts">{rows}</table></section>'

    terms = ""
    if d.get("terms"):
        items = "\n".join(
            f'<div><dt>{e(t["term"])}<span>{e(t["zh"])}</span></dt>'
            f'<dd>{e(t.get("def"))}</dd></div>' for t in d["terms"])
        terms = f'<section class="section"><h3>术语</h3><dl class="terms">{items}</dl></section>'

    toc = "\n".join(f'<a href="#p{i}"><span class="t">{hhmmss(p["t"])}</span>'
                    f'<span>{e(p["h"])}</span></a>'
                    for i, p in enumerate(d.get("points") or []))
    points = re.sub(r'<div class="point">', lambda m, c=iter(range(999)):
                    f'<div class="point" id="p{next(c)}">', points)

    player = (f'<audio data-player controls preload="none" src="{e(ep["audio"])}"></audio>'
              if ep.get("audio") else "")
    tsrc = TSRC_LABEL.get(q.get("transcript_source"), q.get("transcript_source") or "—")
    rv = ep.get("review") or {}
    rvs = f"{rv['score']:.0f}" if isinstance(rv.get("score"), (int, float)) else ""
    orig = ep.get("link") or (f"https://www.youtube.com/watch?v={ep['youtube_id']}"
                              if ep.get("youtube_id") else "")

    tags = "".join(f'<span class="tag">{e(t)}</span>' for t in (d.get("tags") or []))
    prevnext = ""
    if prev or nxt:
        left = (f'<a href="{BASE}/p/{e(prev["slug"])}/"><span class="lbl">← 更新</span>'
                f'<strong>{e(prev["digest"]["title"])}</strong></a>' if prev else "<span></span>")
        right = (f'<a class="r" href="{BASE}/p/{e(nxt["slug"])}/"><span class="lbl">更早 →</span>'
                 f'<strong>{e(nxt["digest"]["title"])}</strong></a>' if nxt else "<span></span>")
        prevnext = f'<nav class="prevnext">{left}{right}</nav>'

    ld = json.dumps({
        "@context": "https://schema.org", "@type": "PodcastEpisode",
        "name": d.get("title"), "description": d.get("dek"),
        "datePublished": ep.get("published"), "url": f"{SITE}/p/{ep['slug']}/",
        "timeRequired": f"PT{int((ep.get('duration') or 0)//60)}M",
        "partOfSeries": {"@type": "PodcastSeries", "name": ep.get("source")},
        "inLanguage": "zh-CN",
        "isBasedOn": orig or None,
    }, ensure_ascii=False)

    return (head(f"{d.get('title')} — {src_label} · {NAME}", d.get("dek", ""),
                 path=f"/p/{ep['slug']}/", image=ep.get("image", ""),
                 extra=f'<script type="application/ld+json">{ld}</script>')
            + masthead(None, home=False)
            + f"""
<main class="wrap ep">
<nav class="crumb"><a href="{BASE}/">首页</a><span class="sep">/</span>
<a href="{BASE}/s/{e(ep.get('source_id'))}/">{e(src_label)}</a>
<span class="sep">/</span><span>{e(date)}</span></nav>

<div class="ep-grid">
<article>
<div class="ep-head">
<div class="kicker" data-cat="{e(ep.get('cat'))}"><span class="src">{e(src_label)}</span>
<span class="date">{e(date)}</span></div>
<h1>{e(d.get('title'))}</h1>
<p class="dek-lead">{e(d.get('dek'))}</p>
<div class="ep-meta">{tags}</div>
</div>

{f'<section class="section"><div class="why">{e(d.get("why"))}</div></section>' if d.get('why') else ''}

<section class="section"><h3>核心论点 · {'时间戳为按文稿位置估算' if q.get('approx_timestamps') else '点时间戳可跳到原声'}</h3>{points}</section>
{f'<section class="section"><h3>原话 · 已逐字校验</h3>{quotes}</section>' if quotes else ''}
{facts}
{terms}
{f'''<section class="section"><h3>收听指南</h3>
<div class="panel guide">
<div><span class="k">谁该听</span><p>{e(d.get("who"))}</p></div>
{f'<div><span class="k">可跳过</span><p>{e(d.get("skip"))}</p></div>' if d.get('skip') else ''}
</div></section>''' if d.get('who') else ''}
{prevnext}
</article>

<aside class="aside">
<div class="panel"><h4>原节目</h4>
<div class="row"><span>节目</span><span>{e(ep.get('source'))}</span></div>
<div class="row"><span>原标题</span><span>{e(ep.get('title_original'))}</span></div>
<div class="row"><span>发布</span><span>{e(date)}</span></div>
<div class="row"><span>时长</span><span>{hhmmss(ep.get('duration')) or '—'}</span></div>
{f'<a class="row" href="{e(orig)}" target="_blank" rel="noopener"><span>原页面</span><span>打开 ↗</span></a>' if orig else ''}
{player}
{'<p class="note">时间戳会直接跳到上面的播放器。</p>' if player else '<p class="note">时间戳会跳到原节目对应位置。</p>'}
</div>

{f'<div class="panel"><h4>本篇结构</h4><nav class="toc">{toc}</nav></div>' if toc else ''}

<div class="panel"><h4>这篇是怎么来的</h4>
{f'<div class="row"><span>成稿评分</span><span>{rvs} / 10</span></div>' if rvs else ''}
<div class="row"><span>文稿来源</span><span>{e(tsrc)}</span></div>
<div class="row"><span>文稿字数</span><span>{q.get('words') or '—'}</span></div>
<div class="row"><span>语速核验</span><span>{q.get('wpm') or '—'} wpm</span></div>
<div class="row"><span>逐字校验金句</span><span>{q.get('verified_quotes', 0)} 条</span></div>
<div class="row"><span>回溯校验数字</span><span>{q.get('grounded_facts', 0)} 条</span></div>
{f'<div class="row"><span>质检剔除</span><span>{q["pruned"]} 处</span></div>' if q.get('pruned') else ''}
<p class="note">金句在逐字稿里逐字比对过，数字回原文核对过；对不上的当场删掉，不上站。
{f'成稿另经一道独立评审（信息密度／忠实度／选择力／具体性／中文），低于 8 分不展示。' if rvs else ''}
{'这一集的文稿没有原始时间码，页面上的时间戳是按文稿位置估算的，只作粗略定位。' if q.get('approx_timestamps') else ''}</p>
</div>
</aside>
</div></main>
""" + foot())


# --------------------------------------------------------------- sources page

def sources_page(srcs: dict, eps: list[dict]) -> str:
    per = {}
    for x in eps:
        per[x.get("source_id")] = per.get(x.get("source_id"), 0) + 1
    blocks = []
    for c in CAT_ORDER:
        rows = [s for s in srcs["sources"] if s.get("cat") == c]
        if not rows:
            continue
        rows.sort(key=lambda s: (s.get("tier", 3), -per.get(s["id"], 0), s["name"]))
        cards = []
        for s in rows:
            st = s.get("status") or {}
            dead = st.get("ok") is False
            meta = [f'T{s.get("tier", 3)}']
            if per.get(s["id"]):
                total = (s.get("status") or {}).get("episodes")
                meta.append(f'本站 {per[s["id"]]} / {total} 集' if total
                            else f'本站 {per[s["id"]]} 篇')
            else:
                meta.append("本站尚未收录")
            if st.get("cadence_days"):
                meta.append(f'约 {st["cadence_days"]} 天一集')
            if st.get("latest"):
                meta.append(f'最新 {st["latest"]}')
            if st.get("official_transcripts"):
                meta.append("自带官方逐字稿")
            if s.get("kind") == "youtube":
                meta.append("YouTube 源")
            if s.get("lang") == "zh":
                meta.append("中文")
            if dead:
                meta.append("抓取异常")
            mine = per.get(s["id"], 0)
            body = f"""<h3>{e(s.get('zh') or s['name'])}</h3>
<p>{e(s.get('desc'))}</p>
<div class="meta">{' · '.join(e(m) for m in meta)}</div>"""
            cards.append(
                f'<a class="src-card{" dead" if dead else ""}" id="{e(s["id"])}" '
                f'href="{BASE}/s/{e(s["id"])}/">{body}</a>' if mine else
                f'<div class="src-card{" dead" if dead else ""}" id="{e(s["id"])}">{body}</div>')
        blocks.append(f'<h2 class="sec-title">{CAT_LABEL[c]}<span class="stat" '
                      f'style="margin-left:10px">{len(rows)} 档</span></h2>'
                      f'<div class="src-grid">{"".join(cards)}</div>')
    n = len(srcs["sources"])
    return (head(f"信源 — {NAME}", f"{NAME} 目前追踪 {n} 档中英文播客的完整信源清单与抓取健康度。",
                 path="/sources/")
            + masthead(len(eps), home=False)
            + f"""<main class="wrap">
<h2 class="sec-title" style="margin-top:34px">信源 · {n} 档</h2>
<p class="lede">以 Apple Podcasts 官方 RSS 为主干，而不是只抓 YouTube 频道——这样纯音频节目
（Acquired、Odd Lots、Invest Like the Best）和中文播客才不会整块缺失。feed 地址不写死在代码里，
节目换托管商时会自动从 Apple 目录重新解析，所以不会悄悄断更。</p>
<p class="lede">T1 表示每集必读，T2 有实质内容时收，T3 只在特别强的一集时收。</p>
{''.join(blocks)}
<div style="height:56px"></div></main>""" + foot())


# ------------------------------------------------------------- source browse

def source_page(src: dict, eps: list[dict], total_known: int | None) -> str:
    """One show's own page. An aggregator is judged on whether you can follow a
    single show through it, not only on the front page firehose."""
    name = src.get("zh") or src["name"]
    cards = "\n".join(card(x, hero=(i == 0)) for i, x in enumerate(eps))
    st = src.get("status") or {}
    rows = []
    if st.get("episodes"):
        covered = f"{len(eps)} / {st['episodes']}"
        rows.append(("本站已深读", f"{covered} 集"))
    if st.get("cadence_days"):
        rows.append(("更新节奏", f"约 {st['cadence_days']} 天一集"))
    if st.get("latest"):
        rows.append(("最新一集", st["latest"]))
    if st.get("official_transcripts"):
        rows.append(("官方逐字稿", "自带"))
    rows.append(("分类", CAT_LABEL[src["cat"]]))
    rows.append(("优先级", f"T{src.get('tier', 3)}"))
    meta = "".join(f'<div class="row"><span>{e(k)}</span><span>{e(v)}</span></div>'
                   for k, v in rows)
    return (head(f"{name} — {NAME}", src.get("desc", ""), path=f"/s/{src['id']}/")
            + masthead(len(eps), home=False)
            + f"""<main class="wrap">
<nav class="crumb" style="margin-top:26px"><a href="{BASE}/">首页</a><span class="sep">/</span>
<a href="{BASE}/sources/">信源</a><span class="sep">/</span><span>{e(name)}</span></nav>
<h2 class="sec-title" style="margin-top:0">{e(name)}</h2>
<p class="lede">{e(src.get('desc'))}</p>
<div class="panel" style="max-width:420px;margin:18px 0 4px">{meta}</div>
<div class="feed" data-feed>{cards or
  '<div class="empty"><b>这档还没有深读</b>取不到可核对的文稿时不会发，等文稿到位再上。</div>'}</div>
</main>""" + foot())


# ---------------------------------------------------------------- feed / seo

def rss(eps: list[dict]) -> str:
    items = []
    for x in eps[:60]:
        d = x["digest"]
        body = [f"<p><strong>{xesc(d.get('dek',''))}</strong></p>"]
        if d.get("why"):
            body.append(f"<p>{xesc(d['why'])}</p>")
        for p in (d.get("points") or [])[:8]:
            body.append(f"<p><strong>{hhmmss(p['t'])} {xesc(p['h'])}</strong><br>"
                        f"{xesc(p['body'])}</p>")
        from email.utils import format_datetime
        import datetime as dt
        try:
            pd = dt.datetime.strptime(x["published"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=dt.timezone.utc)
            pub = format_datetime(pd)
        except Exception:
            pub = ""
        items.append(f"""<item>
<title>{xesc(d.get('title',''))}</title>
<link>{xesc(SITE)}/p/{xesc(x['slug'])}/</link>
<guid isPermaLink="true">{xesc(SITE)}/p/{xesc(x['slug'])}/</guid>
{f'<pubDate>{pub}</pubDate>' if pub else ''}
<category>{xesc(x.get('source',''))}</category>
<description>{xesc(d.get('dek',''))}</description>
<content:encoded><![CDATA[{''.join(body)}]]></content:encoded>
</item>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"
 xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
<title>{xesc(NAME)} — {xesc(TAGLINE)}</title>
<link>{xesc(SITE)}/</link>
<atom:link href="{xesc(SITE)}/feed.xml" rel="self" type="application/rss+xml"/>
<description>{xesc(BLURB)}</description>
<language>zh-CN</language>
{''.join(items)}
</channel></rss>
"""


def sitemap(eps: list[dict]) -> str:
    urls = [f"<url><loc>{xesc(SITE)}/</loc><changefreq>daily</changefreq><priority>1.0</priority></url>",
            f"<url><loc>{xesc(SITE)}/sources/</loc><changefreq>weekly</changefreq></url>"]
    for sid in sorted({x["source_id"] for x in eps}):
        urls.append(f"<url><loc>{xesc(SITE)}/s/{xesc(sid)}/</loc><changefreq>weekly</changefreq></url>")
    for x in eps:
        urls.append(f"<url><loc>{xesc(SITE)}/p/{xesc(x['slug'])}/</loc>"
                    f"<lastmod>{xesc((x.get('published') or '')[:10])}</lastmod></url>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + "\n".join(urls) + "\n</urlset>\n")


def not_found() -> str:
    return (head(f"找不到这一页 — {NAME}", "", path="/404.html")
            + masthead(0, home=False)
            + f"""<main class="wrap"><div class="empty" style="padding:110px 0">
<b>这一页不在了</b><p>回 <a href="{BASE}/" style="color:var(--accent)">首页</a> 看最新深读。</p>
</div></main>""" + foot())


def main() -> int:
    global BLURB
    BLURB = _blurb()
    eps, srcs = load()
    log(f"building {len(eps)} episodes, {len(srcs.get('sources') or [])} sources")
    (ROOT / "index.html").write_text(index_page(eps, srcs))
    (ROOT / "sources").mkdir(exist_ok=True)
    (ROOT / "sources" / "index.html").write_text(sources_page(srcs, eps))
    (ROOT / "404.html").write_text(not_found())
    (ROOT / "feed.xml").write_text(rss(eps))
    (ROOT / "sitemap.xml").write_text(sitemap(eps))
    (ROOT / "search.json").write_text(search_index(eps))

    sdir = ROOT / "s"
    by_src: dict[str, list[dict]] = {}
    for x in eps:
        by_src.setdefault(x["source_id"], []).append(x)
    live_src = set()
    for src in srcs["sources"]:
        rows = by_src.get(src["id"]) or []
        if not rows:
            continue
        out = sdir / src["id"]
        out.mkdir(parents=True, exist_ok=True)
        (out / "index.html").write_text(source_page(src, rows, None))
        live_src.add(src["id"])
    if sdir.exists():
        for d in sdir.iterdir():
            if d.is_dir() and d.name not in live_src:
                shutil.rmtree(d)
    log(f"  {len(live_src)} source pages")

    (ROOT / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nSitemap: {SITE}/sitemap.xml\n")
    (ROOT / ".nojekyll").write_text("")

    pdir = ROOT / "p"
    live = set()
    for i, x in enumerate(eps):
        prev = eps[i - 1] if i > 0 else None
        nxt = eps[i + 1] if i + 1 < len(eps) else None
        out = pdir / x["slug"]
        out.mkdir(parents=True, exist_ok=True)
        (out / "index.html").write_text(episode_page(x, prev, nxt))
        live.add(x["slug"])
    if pdir.exists():                      # drop pages whose record is gone
        for d in pdir.iterdir():
            if d.is_dir() and d.name not in live:
                shutil.rmtree(d)
                log(f"  removed stale page /p/{d.name}/")
    log(f"built: index, sources, {len(eps)} episode pages, feed.xml, sitemap.xml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
