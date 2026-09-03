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

CAT_ORDER = ["ai", "biz", "cn", "ideas", "hist", "parent"]
CAT_LABEL = {
    "ai": "AI / 技术",
    "biz": "投资 / 商业",
    "cn": "中国视角",
    "ideas": "人文 / 思想",
    "hist": "历史",
    "parent": "育儿",
}


BLURB = ""          # 首次 build 时填充（要先读到 data/sources.json）


# 分享卡片的图必须小。张小珺那集的封面是 3000×3000 的 PNG、3.2 MB——微信抓图
# 直接放弃了，卡片上只有一个灰色占位符。多数播客 CDN 支持缩略参数，实测：
#   image.xyzcdn.net（七牛，小宇宙）  3.2 MB → 39 KB
#   megaphone.imgix.net（imgix）      283 KB → 77 KB
#   www.omnycontent.com               加参数直接 HTTP 400，绝对不能加
#   image.simplecastcdn.com           参数被忽略，加了没用也没害
# 只对实测有效的加，其余原样放行——宁可慢，也不能因为参数写错让图整个 404。
_OG_RESIZE = (
    ("image.xyzcdn.net", "?imageMogr2/thumbnail/600x600/format/jpg/quality/80"),
    ("imgix.net", "?w=600&h=600&fit=crop&auto=format&q=70"),
)


def og_image(url: str) -> str:
    """把封面图换成 600×600 的缩略版本，能换就换。"""
    if not url or "?" in url:          # 已经带参数的不动，免得叠加出错
        return url
    for host, q in _OG_RESIZE:
        if host in url:
            return url + q
    return url


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

# 首屏渲染多少张卡片。剩下的进 cards.json，滚到底、点"加载更多"或一搜索就补齐。
FIRST_PAGE = 24

GA_ID = os.environ.get("GA_ID", "G-DHD3WEXQ8T")   # 与 ourword.ai 其他站同一个属性

# max-snippet/max-image-preview 放开：答案引擎和搜索结果都靠这个决定能引多少。
# 对这个站尤其重要——它的价值就是被引用时能带出可核对的判断。
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
{f'<meta property="og:image" content="{e(og_image(image))}">'
  f'<meta property="og:image:width" content="600">'
  f'<meta property="og:image:height" content="600">' if image else ''}
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
    """一个按钮，把内容拼成一段能直接粘贴的文本。

    微信和朋友圈不给网页调起分享——那需要认证公众号、JS 接口安全域名和服务端签名。
    所以走和「走你」同一套办法：复制一段写好的文本，用户粘到哪都成立（微信、朋友圈、
    群、微博、备忘录）。手机上如果有系统分享面板就先用它，装了微信就直接出现在里面。

    文本放 data 属性里，换行写成 &#10;——这样不需要额外的 JSON 或内联脚本。
    """
    return (f'<button class="share-btn" type="button" data-share '
            f'data-share-title="{e(title)}" data-share-url="{e(url)}" '
            f'data-share-text="{e(text).replace(chr(10), "&#10;")}" '
            f'aria-label="复制分享文本">{ICON_SHARE}<span>{e(label)}</span></button>')


def _clip(s: str, n: int) -> str:
    s = squeeze(s or "")
    return s if len(s) <= n else s[:n - 1].rstrip("，。、；：,. ") + "…"


def episode_share_text(ep: dict) -> str:
    """粘到微信里要立得住：标题、一句话、三条要点、一句金句、链接。

    控制在 500 字以内——朋友圈超长会折叠，群里刷屏也没人读。
    """
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
    """分享用的短链。

    正文页的 slug 是中文，percent-encode 之后有两百多字符——粘到朋友圈里，链接
    比内容还长，而朋友圈超长会折叠。所以另外生成一个纯 ASCII 短链 /e/<id>/，
    只用于分享；站内和搜索引擎看到的仍然是可读的中文 URL。
    """
    return f"{SITE}/e/{ep['id']}/"


def alias_page(ep: dict) -> str:
    """短链页：canonical 指回正文，noindex 防止和正文抢排名，然后立刻跳走。"""
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
    """整站的分享文本。不吹功能，说清它凭什么值得点开：
    每条判断都能跳回原声，对不上的当场删掉。"""
    lines = [f"{NAME} · {TAGLINE}", "",
             _clip(BLURB, 110), ""]
    for x in eps[:3]:
        src = x.get("source_zh") or x.get("source") or ""
        lines.append(f"· {_clip(x['digest'].get('title'), 30)}（{_clip(src, 14)}）")
    lines += ["", f"{SITE}/"]
    return "\n".join(lines)


def masthead(n: int | None, *, home: bool) -> str:
    """字标和右侧那几个入口同一行，slogan 独占下一行。

    原来 slogan 在 .brand 里面，于是 .brand 整块占满宽度，右侧那几项在窄屏被挤到
    单独一行。把 slogan 提出来做兄弟节点，字标左、入口右，任何宽度都成立。
    n 为 None 时不显示篇数（单集页用）。
    """
    # 字标只在首页是 h1。单集页的 h1 必须是文章标题——一页两个 h1 会让搜索引擎
    # 和读屏软件都拿不准这一页在讲什么。
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


def _ld(obj: dict) -> str:
    return ('<script type="application/ld+json">'
            + json.dumps(obj, ensure_ascii=False) + "</script>")


def _publisher() -> dict:
    return {"@type": "Organization", "name": "OurWord.ai", "url": "https://ourword.ai/"}


def write_card_pages(eps: list[dict]) -> int:
    """首屏之外的卡片，按页写成 cards-1.json、cards-2.json……

    为什么分页而不是一个大文件：第一版把剩下 231 张全塞进一个 cards.json，
    滚到底一次性插入——那不是分页加载，是"晚一点的全量加载"（96 KB + 231 个
    DOM 节点一次进来）。现在一页 24 张，滚到哪加载到哪。

    为什么存 HTML 而不是存数据让前端拼：卡片的标记必须和首屏那批一模一样，
    两份渲染逻辑迟早会长歪（首屏加了个角标、这边没加）。存 HTML 只有一份真相。
    """
    rest = eps[FIRST_PAGE:]
    pages = 0
    for i in range(0, len(rest), FIRST_PAGE):
        pages += 1
        (ROOT / f"cards-{pages}.json").write_text(
            json.dumps([card(x, hero=False) for x in rest[i:i + FIRST_PAGE]],
                       ensure_ascii=False))
    # 页数变少时把多出来的旧文件删掉，否则前端会取到过期的卡片
    n = pages + 1
    while (ROOT / f"cards-{n}.json").exists():
        (ROOT / f"cards-{n}.json").unlink()
        n += 1
    old = ROOT / "cards.json"          # 第一版的单文件，不再用
    if old.exists():
        old.unlink()
    return pages


def index_page(eps: list[dict], srcs: dict) -> str:
    counts = {c: sum(1 for x in eps if x.get("cat") == c) for c in CAT_ORDER}
    chips = [f'<button class="chip" data-cat-chip="all" aria-pressed="true">全部'
             f'<span class="n">{len(eps)}</span></button>']
    for c in CAT_ORDER:
        chips.append(f'<button class="chip" data-cat-chip="{c}" aria-pressed="false">'
                     f'{CAT_LABEL[c]}<span class="n">{counts.get(c, 0)}</span></button>')
    # 首屏只渲染前 FIRST_PAGE 张。原来 257 张全内联，index.html 288 KB
    # （gzip 后 109 KB），手机上打开明显慢。其余的进 cards.json，滚到底或一搜索
    # 就补齐——**搜索和筛选必须覆盖全部**，所以补齐是前提不是可选项。
    cards = "\n".join(card(x, hero=(i == 0)) for i, x in enumerate(eps[:FIRST_PAGE]))
    # TAGLINE 里已经有"原声"，再前缀 NAME 会让标题出现两次品牌名
    # WebSite + CollectionPage + ItemList：让搜索与答案引擎知道这是一个持续更新的
    # 条目集合，而不是一张零散的落地页。SearchAction 指向 ?q=，那是站内搜索真实
    # 用的参数，所以这个声明是可用的而不是装饰。
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

<main class="wrap"><div class="feed" data-feed data-total="{len(eps)}"
     data-page-size="{FIRST_PAGE}"
     data-pages="{max(0, (len(eps) - FIRST_PAGE + FIRST_PAGE - 1) // FIRST_PAGE)}">
{cards}
<div class="empty" data-empty hidden><b>没有匹配的深读</b>
<p>换个词，或者清掉筛选再试。搜索会搜进每条要点的正文、金句的中英文原文、
数字和术语表——不只是标题。</p>
<p data-deep-note hidden style="color:var(--faint);font-size:13px"></p></div>
</div>
{f'''<div class="more" data-sentinel>
<span class="more-count" data-more-count>{FIRST_PAGE} / {len(eps)}</span>
<button class="more-btn" data-more type="button" hidden>继续加载</button>
<noscript><p class="note">没有 JavaScript 时只显示最新 {FIRST_PAGE} 篇，
完整清单见 <a href="{BASE}/sitemap.xml">sitemap</a> 或 <a href="{BASE}/llms.txt">llms.txt</a>。</p></noscript>
</div>''' if len(eps) > FIRST_PAGE else ""}
</main>
""" + foot())


# -------------------------------------------------------------- episode page

def seek_href(ep: dict, t: int) -> str:
    """Where a timestamp points when there is no inline player to seek."""
    if ep.get("youtube_id"):
        return f"https://www.youtube.com/watch?v={ep['youtube_id']}&t={int(t)}s"
    if ep.get("audio"):
        return f"{ep['audio']}#t={int(t)}"
    return ep.get("link") or "#"


def player_block(ep: dict) -> str:
    """正文顶部的播放器。

    放这里而不是侧栏或文末：这个站的前提是"每条判断都能跳回原声核对"，播放器是
    为时间戳服务的。侧栏只有 264px，视频小到没法看；文末的话，正文各处的时间戳
    都要往回滚很远。放第一屏，读者的用法是"扫一眼摘要 → 点时间戳 → 就地看那一段"。

    视频用**封面图加播放按钮的假门**，点了才换成真 iframe。YouTube 的嵌入代码有
    1 MB 以上的 JS，直接塞进去会把首页刚从 109 KB 压到 17 KB 的成果吃掉。
    """
    vid = ep.get("youtube_id")
    if vid:
        # 封面用 YouTube 自己的缩略图，i.ytimg.com 不需要 cookie
        poster = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        return f'''
<div class="player" data-player-box>
  <button class="video-facade" data-yt="{e(vid)}" type="button"
          aria-label="播放视频">
    <img src="{e(poster)}" alt="" loading="lazy" width="480" height="360">
    <span class="play" aria-hidden="true">▶</span>
  </button>
  <p class="note">点时间戳会跳到视频对应位置。视频由 YouTube 提供，点播放才会加载。</p>
</div>'''
    if ep.get("audio"):
        return f'''
<div class="player" data-player-box>
  <audio data-player controls preload="none" src="{e(ep["audio"])}"></audio>
  <p class="note">点时间戳会跳到这里对应的位置。</p>
</div>'''
    return ""


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
        facts = f'<section class="section"><h2>数字与实体</h2><table class="facts">{rows}</table></section>'

    terms = ""
    if d.get("terms"):
        items = "\n".join(
            f'<div><dt>{e(t["term"])}<span>{e(t["zh"])}</span></dt>'
            f'<dd>{e(t.get("def"))}</dd></div>' for t in d["terms"])
        terms = f'<section class="section"><h2>术语</h2><dl class="terms">{items}</dl></section>'

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

    # 关键词给答案引擎用：标签 + 术语 + facts 的指标名，都是这一篇真实覆盖的实体
    kw = [t for t in (d.get("tags") or [])]
    kw += [t.get("term") for t in (d.get("terms") or []) if t.get("term")]
    kw += [f.get("k") for f in (d.get("facts") or []) if f.get("k")][:6]
    graph = [{
        "@type": "PodcastEpisode",
        "@id": f"{SITE}/p/{ep['slug']}/#episode",
        "name": d.get("title"), "headline": d.get("title"),
        "description": d.get("dek"), "abstract": d.get("dek"),
        "datePublished": ep.get("published"),
        "dateModified": ep.get("generated") or ep.get("published"),
        "url": f"{SITE}/p/{ep['slug']}/",
        "timeRequired": f"PT{int((ep.get('duration') or 0) // 60)}M",
        "partOfSeries": {"@type": "PodcastSeries", "name": ep.get("source"),
                         "url": f"{SITE}/s/{ep.get('source_id')}/"},
        "inLanguage": "zh-CN",
        "isBasedOn": orig or None,
        "publisher": _publisher(),
        "keywords": ", ".join(x for x in kw if x) or None,
        "isAccessibleForFree": True,
        # 答案引擎优先朗读/引用这两块——正好是本站最该被引用的部分
        "speakable": {"@type": "SpeakableSpecification",
                      "cssSelector": [".dek-lead", ".point .body"]},
    }]
    if ep.get("audio"):
        graph[0]["associatedMedia"] = {"@type": "AudioObject", "contentUrl": ep["audio"]}
    graph.append({"@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": "首页", "item": SITE + "/"},
        {"@type": "ListItem", "position": 2, "name": src_label,
         "item": f"{SITE}/s/{ep.get('source_id')}/"},
        {"@type": "ListItem", "position": 3, "name": d.get("title")}]})
    ld = json.dumps({"@context": "https://schema.org", "@graph": graph},
                    ensure_ascii=False)

    return (head(f"{d.get('title')} — {src_label} · {NAME}", d.get("dek", ""),
                 path=f"/p/{ep['slug']}/", image=ep.get("image", ""),
                 published=ep.get("published", ""),
                 modified=ep.get("generated") or ep.get("published", ""),
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
<time class="date" datetime="{e(ep.get('published'))}">{e(date)}</time>
{share_button(episode_share_text(ep), url=ep_url(ep), title=d.get('title') or '')}</div>
<h1>{e(d.get('title'))}</h1>
<p class="dek-lead">{e(d.get('dek'))}</p>
<div class="ep-meta">{tags}</div>
</div>
{player_block(ep)}

{f'<section class="section"><div class="why">{e(d.get("why"))}</div></section>' if d.get('why') else ''}

<section class="section"><h2>核心论点 · {'时间戳为按文稿位置估算' if q.get('approx_timestamps') else '点时间戳可跳到原声'}</h2>{points}</section>
{f'<section class="section"><h2>原话 · 已逐字校验</h2>{quotes}</section>' if quotes else ''}
{facts}
{terms}
{f'''<section class="section"><h2>收听指南</h2>
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
{'' if (ep.get("audio") or ep.get("youtube_id")) else '<p class="note">时间戳会跳到原节目对应位置。</p>'}
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
{f'成稿另经一道独立评审（信息密度／忠实度／选择力／具体性／中文），低于 7 分不展示。' if rvs else ''}
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
            # 机房 IP 取不到的那批由本机线负责，站上不该显示成抓取异常
            dead = st.get("ok") is False and not st.get("blocked_here")
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
    ld = _ld({"@context": "https://schema.org", "@type": "CollectionPage",
              "url": SITE + "/sources/", "name": f"信源 — {NAME}",
              "inLanguage": "zh-CN", "isPartOf": {"@id": SITE + "/#site"},
              "mainEntity": {"@type": "ItemList", "numberOfItems": n,
                             "itemListElement": [
                                 {"@type": "ListItem", "position": i + 1,
                                  "item": {"@type": "PodcastSeries",
                                           "name": s0.get("zh") or s0["name"],
                                           "description": s0.get("desc", ""),
                                           "url": (f"{SITE}/s/{s0['id']}/" if per.get(s0["id"])
                                                   else None)}}
                                 for i, s0 in enumerate(srcs["sources"])]}})
    return (head(f"信源 — {NAME}", f"{NAME} 目前追踪 {n} 档中英文播客的完整信源清单与抓取健康度。",
                 path="/sources/", extra=ld)
            + masthead(len(eps), home=False)
            + f"""<main class="wrap">
<h1 class="sec-title" style="margin-top:34px">信源 · {n} 档</h1>
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
    rows.append(("分类", CAT_LABEL.get(src["cat"], src["cat"])))
    rows.append(("优先级", f"T{src.get('tier', 3)}"))
    meta = "".join(f'<div class="row"><span>{e(k)}</span><span>{e(v)}</span></div>'
                   for k, v in rows)
    ld = _ld({"@context": "https://schema.org", "@graph": [
        {"@type": "PodcastSeries", "name": name, "description": src.get("desc", ""),
         "url": f"{SITE}/s/{src['id']}/", "inLanguage":
             "zh-CN" if src.get("lang") == "zh" else "en",
         "webFeed": src.get("feed")},
        {"@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "首页", "item": SITE + "/"},
            {"@type": "ListItem", "position": 2, "name": "信源", "item": SITE + "/sources/"},
            {"@type": "ListItem", "position": 3, "name": name}]}]})
    return (head(f"{name} — {NAME}", src.get("desc", ""), path=f"/s/{src['id']}/", extra=ld)
            + masthead(len(eps), home=False)
            + f"""<main class="wrap">
<nav class="crumb" style="margin-top:26px"><a href="{BASE}/">首页</a><span class="sep">/</span>
<a href="{BASE}/sources/">信源</a><span class="sep">/</span><span>{e(name)}</span></nav>
<div class="page-head">
<h1 class="sec-title" style="margin-top:0">{e(name)}</h1>
{share_button(source_share_text(src, eps), url=f"{SITE}/s/{src['id']}/", title=name)}
</div>
<p class="lede">{e(src.get('desc'))}</p>
<div class="panel" style="max-width:420px;margin:18px 0 4px">{meta}</div>
<div class="feed" data-feed>{cards or
  '<div class="empty"><b>这档还没有深读</b>取不到可核对的文稿时不会发，等文稿到位再上。</div>'}</div>
</main>""" + foot())


# ---------------------------------------------------------------- 更新日志

KIND_LABEL = {"added": "收录", "removed": "移除", "demoted": "降级", "dormant": "休眠"}
KIND_TONE = {"added": "add", "removed": "drop", "demoted": "down", "dormant": "down"}


def log_page(eps: list[dict], srcs: dict) -> str:
    """信源的增删记录。

    做成站上的一页而不是只留在仓库里，是因为读者有权知道信源清单变过什么：
    一个聚合站悄悄换掉信源，等于悄悄换掉它的口味。
    """
    rows = []
    path = DATA / "curation.json"
    if path.exists():
        try:
            rows = json.loads(path.read_text())
        except Exception:
            rows = []
    rows = sorted(rows, key=lambda r: r.get("at", ""), reverse=True)
    n_src = len(srcs.get("sources") or [])

    items = []
    for r in rows:
        kind = r.get("kind", "")
        tone = KIND_TONE.get(kind, "")
        detail = e(r.get("why") or "")
        extra = ""
        if kind == "added" and r.get("score") is not None:
            # 收录分是照着标题与分集说明打的，节目自己写的宣传文案也算在内。
            # 不标出来，读者会把它当成对内容的结论——而这档节目可能一篇都跑不出来。
            # 标记塞在同一个 span 里：.ev 是四列网格，多一个子元素会另起一行。
            flag = '<em class="ev-flag">试用</em>' if r.get("probation") else ""
            extra = f'<span class="ev-score">{r["score"]:.1f} 分{flag}</span>'
        elif kind in ("demoted", "dormant") and r.get("from_tier"):
            extra = f'<span class="ev-score">T{r["from_tier"]} → T{r["to_tier"]}</span>'
        items.append(f"""<li class="ev {tone}">
<span class="ev-when">{e((r.get('at') or '')[:10])}</span>
<span class="ev-what">{e(KIND_LABEL.get(kind, kind))}</span>
<span class="ev-who">{e(r.get('name'))}</span>{extra}
<span class="ev-why">{detail}</span></li>""")

    body = ("<ul class=\"evlist\">" + "".join(items) + "</ul>") if items else (
        '<div class="empty"><b>还没有信源变动</b>'
        '<p>信源清单每三天自动复查一次：feed 失效、停更超过 120 天、选题通过率低于 25%、'
        '或成稿评分中位不高于 7 的会被降级或移除；同时从近期内容里挖新源，只收 8 分以上。'
        '任何一次改动都会记在这里。</p></div>')

    ld = _ld({"@context": "https://schema.org", "@type": "CollectionPage",
              "url": SITE + "/log/", "name": f"更新日志 — {NAME}", "inLanguage": "zh-CN",
              "isPartOf": {"@id": SITE + "/#site"}})
    return (head(f"更新日志 — {NAME}",
                 f"{NAME} 的信源增删记录：什么时候收了谁、踢了谁、为什么。当前 {n_src} 档。",
                 path="/log/", extra=ld)
            + masthead(len(eps), home=False)
            + f"""<main class="wrap">
<h1 class="sec-title" style="margin-top:34px">更新日志</h1>
<p class="lede">信源清单每三天自动复查一次。判据全部来自实测数据，不靠印象：feed 是否
失效、停更多少天、选题闸门的通过率、成稿评分的中位数。同时从近期发布的内容里挖新线索
（被提到的其他节目、反复出现的受访者），实测文稿可得性后打分，只收 8 分以上。</p>
<p class="lede">标着<em class="ev-flag">试用</em>的是刚收的：那个分数照着标题、分集说明和
文稿抽样打的，还没有任何一篇成稿走完四道闸门。跑一段之后，出得来内容的提级，出不来的
移除，两种结果都会记在下面。</p>
<p class="lede">一个聚合站悄悄换掉信源，等于悄悄换掉它的口味，所以每一次改动都记在这里。
完整清单见 <a href="{BASE}/sources/" style="color:var(--accent)">信源页</a>。</p>
{body}
<div style="height:56px"></div></main>""" + foot())


# ------------------------------------------------------------- llms.txt (GEO)

def llms_txt(eps: list[dict], srcs: dict) -> str:
    """给答案引擎读的导览。

    搜索引擎爬 HTML，答案引擎更愿意先读一份说明"这站是什么、内容怎么组织、
    凭什么可信"的纯文本。对这个站尤其重要：它的差异化是可核对性，而那一点在
    HTML 里散落在侧栏和角标上，说明白反而更容易被引用。
    """
    n = len(srcs.get("sources") or [])
    per: dict[str, int] = {}
    for x in eps:
        per[x["source_id"]] = per.get(x["source_id"], 0) + 1
    L = [f"# {NAME} (Yuansheng)", "",
         f"> {TAGLINE}", "> Chinese deep-reads of Chinese and English podcasts, "
         "every claim anchored to a timestamp in the original audio.", "",
         f"每天从 {n} 档中英文播客里挑出值得记住的判断。要点和金句都带时间戳，"
         "点一下就回到它在原声里被说出的那一秒。", "",
         "A daily digest of podcasts, written in Chinese. Each entry carries 5-8 argued "
         "points with clickable timestamps, verbatim quotes in the original language plus "
         "a Chinese translation, and the numbers stated in the episode. "
         "Quotes are checked character-by-character against the transcript and numbers are "
         "traced back to it; anything that cannot be located is deleted before publishing.",
         "", "## Read it", "",
         f"- Site: {SITE}/",
         f"- Every source, with coverage and fetch health: {SITE}/sources/",
         f"- One page per source: {SITE}/s/<source-id>/",
         f"- One page per episode: {SITE}/p/<slug>/",
         f"- Full text of every entry, one file: {SITE}/llms-full.txt",
         f"- All URLs: {SITE}/sitemap.xml",
         f"- RSS: {SITE}/feed.xml",
         f"- Search index (JSON, one row per entry): {SITE}/search.json",
         "", "## How an entry is made", "",
         "1. 文稿 / transcript, in strict order of quality: the show's own machine-readable "
         "transcript from its RSS feed; the full text when the feed carries it; a transcript "
         "page on the show's site; YouTube auto-captions; audio transcription as the last "
         "resort. Every entry states which one it used.",
         "2. 选题 / triage: an editorial pass on the title and show notes decides whether the "
         "episode carries anything worth writing about before any expensive work happens.",
         "3. 机器闸门 / mechanical gate: quotes must occur verbatim in the transcript "
         "(spoken numbers included — \"twenty fourteen\" counts as 2014); every number in the "
         "facts table must be traceable to the transcript; every timestamp must fall inside "
         "the episode's duration. Whatever fails is deleted, not softened.",
         "4. 成稿评分 / review: a separate model scores the finished piece 0-10 against the "
         "transcript around every citation plus an even sample of the whole episode, judging "
         "information density, faithfulness, selection, concreteness and Chinese prose. "
         "Below 7 does not publish. The score is shown on each page.",
         "",
         "Nothing is published when the transcript cannot be verified. A day with no "
         "verifiable material is a day with no new entries.",
         "", f"## Sources ({n})", ""]
    for c in CAT_ORDER:
        rows = [x for x in srcs["sources"] if x.get("cat") == c]
        if not rows:
            continue
        L.append(f"### {CAT_LABEL[c]} ({len(rows)})")
        L.append("")
        rows.sort(key=lambda x: (x.get("tier", 3), x["name"]))
        for x in rows:
            mine = per.get(x["id"], 0)
            url = f"{SITE}/s/{x['id']}/" if mine else ""
            L.append(f"- {x.get('zh') or x['name']} — {x.get('desc','')}"
                     + (f" ({mine} entries: {url})" if mine else " (not yet covered)"))
        L.append("")
    L += [f"## Entries ({len(eps)})", ""]
    for x in eps:
        d = x["digest"]
        L.append(f"- [{d.get('title')}]({SITE}/p/{x['slug']}/) — {d.get('dek')} "
                 f"[{x.get('source_zh') or x.get('source')}, {(x.get('published') or '')[:10]}]")
    L.append("")
    return "\n".join(L)


def llms_full_txt(eps: list[dict]) -> str:
    """全部条目的完整正文，一个文件。答案引擎要引用时不必逐页抓。"""
    out = [f"# {NAME} — {TAGLINE}", "",
           f"{len(eps)} entries. Source: {SITE}/ · Generated from the sources listed in "
           f"{SITE}/llms.txt", ""]
    for x in eps:
        d = x["digest"]
        q = d.get("quality") or {}
        rv = x.get("review") or {}
        out += ["=" * 78, "",
                f"## {d.get('title')}", "",
                f"- URL: {SITE}/p/{x['slug']}/",
                f"- 节目 / show: {x.get('source_zh') or x.get('source')}"
                f" ({SITE}/s/{x.get('source_id')}/)",
                f"- 原集标题 / original episode: {x.get('title_original')}",
                f"- 发布 / published: {(x.get('published') or '')[:10]}"
                f" · 时长 / duration: {hhmmss(x.get('duration')) or '—'}",
                f"- 文稿来源 / transcript: {TSRC_LABEL.get(q.get('transcript_source'), q.get('transcript_source') or '—')}"
                f" · {q.get('words') or '?'} words"
                + (" · timestamps are estimated from position in the transcript"
                   if q.get("approx_timestamps") else " · timestamps are exact"),
                f"- 校验 / verification: {q.get('verified_quotes', 0)} quotes matched verbatim,"
                f" {q.get('grounded_facts', 0)} numbers traced to the transcript"
                + (f", review score {rv['score']:.0f}/10" if isinstance(rv.get("score"), (int, float)) else ""),
                "", f"**{d.get('dek')}**", ""]
        if d.get("why"):
            out += [f"为什么听 / why: {d['why']}", ""]
        out.append("### 核心论点 / points")
        out.append("")
        for pt in d.get("points") or []:
            spk = f" ({pt['spk']})" if pt.get("spk") else ""
            out.append(f"[{hhmmss(pt.get('t'))}]{spk} **{pt.get('h')}** — {pt.get('body')}")
            out.append("")
        if d.get("quotes"):
            out += ["### 原话 / verbatim quotes", ""]
            for qq in d["quotes"]:
                out.append(f"[{hhmmss(qq.get('t'))}] {qq.get('spk')}: \"{qq.get('raw')}\"")
                out.append(f"    译 / zh: {qq.get('zh')}")
                out.append("")
        if d.get("facts"):
            out += ["### 数字 / figures", ""]
            for f in d["facts"]:
                t = f" [{hhmmss(f['t'])}]" if f.get("t") is not None else ""
                out.append(f"- {f.get('k')}: {f.get('v')}{t}")
            out.append("")
        if d.get("terms"):
            out += ["### 术语 / glossary", ""]
            for t in d["terms"]:
                out.append(f"- {t.get('term')} ({t.get('zh')}): {t.get('def')}")
            out.append("")
        if d.get("who"):
            out += [f"谁该听 / who: {d['who']}"]
        if d.get("skip"):
            out += [f"可跳过 / skip: {d['skip']}"]
        out += [f"标签 / tags: {', '.join(d.get('tags') or [])}", ""]
    return "\n".join(out)


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
    newest = max((x.get("published") or "" for x in eps), default="")[:10]
    urls = [f"<url><loc>{xesc(SITE)}/</loc>"
            + (f"<lastmod>{xesc(newest)}</lastmod>" if newest else "")
            + "<changefreq>daily</changefreq><priority>1.0</priority></url>",
            f"<url><loc>{xesc(SITE)}/log/</loc><changefreq>weekly</changefreq>"
            f"<priority>0.5</priority></url>",
            f"<url><loc>{xesc(SITE)}/sources/</loc>"
            + (f"<lastmod>{xesc(newest)}</lastmod>" if newest else "")
            + "<changefreq>weekly</changefreq><priority>0.6</priority></url>"]
    for sid in sorted({x["source_id"] for x in eps}):
        urls.append(f"<url><loc>{xesc(SITE)}/s/{xesc(sid)}/</loc><changefreq>weekly</changefreq></url>")
    for x in eps:
        urls.append(f"<url><loc>{xesc(SITE)}/p/{xesc(x['slug'])}/</loc>"
                    f"<lastmod>{xesc((x.get('published') or '')[:10])}</lastmod>"
                    f"<changefreq>monthly</changefreq><priority>0.8</priority></url>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + "\n".join(urls) + "\n</urlset>\n")


def not_found() -> str:
    return (head(f"找不到这一页 — {NAME}", "", path="/404.html",
                 robots="noindex,follow")
            + masthead(0, home=False)
            + f"""<main class="wrap"><div class="empty" style="padding:110px 0">
<h1><b>这一页不在了</b></h1><p>回 <a href="{BASE}/" style="color:var(--accent)">首页</a> 看最新深读。</p>
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
    n_pages = write_card_pages(eps)
    (ROOT / "log").mkdir(exist_ok=True)
    (ROOT / "log" / "index.html").write_text(log_page(eps, srcs))

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

    # 显式放行答案引擎的爬虫。这个站的价值就是被引用时能带出可核对的判断，
    # 所以默认允许而不是默认拦——沉默的 User-agent: * 在有些爬虫那里等于不确定。
    ai_bots = ["GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "Claude-User",
               "Claude-SearchBot", "anthropic-ai", "PerplexityBot", "Perplexity-User",
               "Google-Extended", "Googlebot", "Bingbot", "Applebot", "Applebot-Extended",
               "Amazonbot", "meta-externalagent", "Bytespider", "Baiduspider",
               "YisouSpider", "CCBot", "DuckDuckBot", "cohere-ai", "Diffbot",
               "Timpibot", "omgili"]
    rb = ["# 允许被搜索与答案引擎抓取和引用。", "",
          "User-agent: *", "Allow: /", ""]
    for b in ai_bots:
        rb += [f"User-agent: {b}", "Allow: /", ""]
    rb += [f"Sitemap: {SITE}/sitemap.xml",
           f"# 给模型读的导览：{SITE}/llms.txt", ""]
    (ROOT / "robots.txt").write_text("\n".join(rb))
    (ROOT / "llms.txt").write_text(llms_txt(eps, srcs))
    (ROOT / "llms-full.txt").write_text(llms_full_txt(eps))
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

    # 分享短链：/e/<id>/ → /p/<中文 slug>/
    edir = ROOT / "e"
    alive = set()
    for x in eps:
        out = edir / x["id"]
        out.mkdir(parents=True, exist_ok=True)
        (out / "index.html").write_text(alias_page(x))
        alive.add(x["id"])
    if edir.exists():
        for d in edir.iterdir():
            if d.is_dir() and d.name not in alive:
                shutil.rmtree(d)
    log(f"built: index, sources, {len(eps)} episode pages "
        f"(+{len(alive)} 分享短链), feed.xml, sitemap.xml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
