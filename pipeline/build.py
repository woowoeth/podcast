#!/usr/bin/env python3
"""Render data/ into the static site.

Everything is pre-rendered HTML: the feed works with JavaScript off, and search
only hides rows that are already in the document. That keeps the page fast and
keeps every episode indexable, which a client-rendered feed does not.
"""
from __future__ import annotations

import hashlib
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
import i18n                                                    # noqa: E402
from i18n import T                                             # noqa: E402
from lib.util import hhmmss, log, squeeze                      # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
BASE = os.environ.get("PODCAST_BASE", "/podcast").rstrip("/")
BASE_ZH = BASE          # 简体的原值，render_site 切语言时要回退到它
LANG = "zh"
LANG_ATTR = "zh-CN"
# 英文站上线了吗 —— **由数据决定，不由环境变量决定。**
# 原来是 PODCAST_EN_LIVE 开关，后果是同样的数据能产出两种 HTML：裸跑
# build.py 的中文页不声明英文版，工作流里带开关跑的声明——committed HTML
# 会在两次构建之间来回翻，而"构建是幂等的"那道闸门只在同一次调用里比两遍，
# 抓不到这种跨调用的分叉。
EN_LIVE = (DATA / "en").is_dir() and any(
    f for f in (DATA / "en").glob("*.json") if not f.name.startswith("_"))


def asset(rel: str) -> str:
    """给 CSS / JS 的 URL 带上内容指纹。

    为什么必须有：GitHub Pages 给这两个文件的是 `max-age=600`，而 URL 从不变。
    于是每次改样式，读者的浏览器都可能拿**缓存里的旧 CSS 配新 HTML**——线上真
    出过：新 HTML 有 .frame / .vdur / .aui 这些新结构，旧 CSS 里没有对应规则，
    结果播放器卡片没描边、时长掉到图片外面、播放圈直接看不见、自定义音频控件
    也不显（旧 JS 不会去摘 native controls）。看起来像我交了个半成品，其实是
    两半不同版本拼在一起。

    指纹变了 URL 就变，浏览器必然重新取——这是唯一能保证 HTML 和资源同版本的
    办法，靠调 max-age 只能缩短窗口，消不掉。
    """
    f = ROOT / rel.lstrip("/")
    try:
        h = hashlib.sha256(f.read_bytes()).hexdigest()[:10]
    except OSError:
        h = "0"
    # **一律用简体的 BASE。** assets/ 只有一份，在仓库根；用英文的 BASE 会指向
    # /podcast/en/assets/，那里什么都没有——英文站上线时就这么裸奔了一轮，
    # 而我的渲染层测试当时没断言"样式生效"，所以没拦住。
    # 共用一份还有个好处：读者切语言时命中同一个缓存条目。
    return f"{BASE_ZH}/{rel.lstrip('/')}?v={h}"
SITE = os.environ.get("PODCAST_SITE", "https://ourword.ai") + BASE
SITE_ZH = SITE
NAME = i18n.name()          # 语言切换时由 render_site 重算
TAGLINE = i18n.tagline()
def _n_sources() -> int:
    """信源数从 sources.json 读，别写死——加了源之后文案会悄悄过期。"""
    try:
        return len(json.loads((DATA / "sources.json").read_text())["sources"])
    except Exception:
        return 0


def _blurb() -> str:
    n = _n_sources()
    head = (T("BLURB_HEAD").replace("{n}", str(n)) if n else T("BLURB_HEAD_NONE"))
    return head + T("BLURB_TAIL")

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
    """把封面图换成 600×600 的缩略版本，能换就换。

    协议一律升到 https：数据源给的有 3 张是 http://（BBC ichef），而页面本身是 https——
    抓图的一方（微信、各家社交）对 https 页面上的 http 图常常直接拒，分享卡就成了空白。
    尺寸不动：og 要的是大图。
    """
    if not url:
        return url
    if url.startswith("http://"):
        url = url.replace("http://", "https://", 1)
    if "?" in url:                     # 已经带参数的不动，免得叠加出错
        return url
    for host, q in _OG_RESIZE:
        if host in url:
            return url + q
    return url


_HAS_CJK = re.compile(r"[\u4e00-\u9fff]")


def T_dict(table: dict, key, default=None) -> str:
    """常量字典的值过一遍文案表。

    字典是模块常量，语言是运行时才定的——所以不能在字典里存两套，
    取的时候翻译。TSRC_LABEL / KIND_LABEL / CAT_LABEL 都走这里。

    **字典里没有这个键时，兜底值原样返回，不进 T()。** 兜底值是 id 本身
    （比如 cat="sci" 在 CAT_LABEL 里没有条目），那不是界面文案，塞进文案表
    只会让"漏译清单"里混进一堆 id。
    """
    if key in table:
        v = table[key]
        return T(v) if isinstance(v, str) else v
    return default if default is not None else key


def show_name(x: dict) -> str:
    """这一集的节目名。

    简体／繁体用 source_zh（中文译名），英文用 source（节目自己的名字）。
    中文节目在英文站上仍然显示中文名——那是它的名字，不是漏译，所以
    渲染处一律配 zh_attr()。
    """
    if LANG == "en":
        return x.get("source") or x.get("source_zh") or ""
    return x.get("source_zh") or x.get("source") or ""


def src_desc(src: dict) -> str:
    """节目简介。英文取 data/en/_sources.json 里的译文；没有就退回中文，
    而中文会被"零漏译"闸门拦下来——所以不会静默漏。"""
    if LANG == "en":
        return (_EN_SRC.get(src.get("id") or "") or {}).get("desc") or src.get("desc", "")
    return src.get("desc", "")


def src_display(src: dict) -> str:
    """信源清单里的节目名，同上。"""
    if LANG == "en":
        return src.get("name") or src.get("zh") or ""
    return src.get("zh") or src.get("name") or ""


_CJK_RUN = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]+")


def mark_zh(html: str) -> str:
    """英文页正文里的中文专名包进 <span lang="zh">。

    只有中文名的中国应用和公司（懂车帝、幸福里、海豚股票）在英文正文里是合法的
    专名——译文里会按"Dongchedi (懂车帝)"这样写。它们**必须被标注**，两个理由：
    一是字体和读屏软件要知道这几个字是中文；二是英文站的"零漏译"闸门判据就是
    "汉字只许出现在 lang=zh 里"，不标就会把合法专名报成漏译。

    **必须在 HTML 转义之后再调**——先包 span 再转义会把标签本身转掉。
    简体页上是恒等函数。
    """
    if LANG == "zh" or not html:
        return html
    return _CJK_RUN.sub(lambda m: f'<span lang="zh">{m.group(0)}</span>', html)


def zh_attr(text) -> str:
    """英文页上，中文内容要显式标 lang="zh"。

    节目名（张小珺·商业访谈录）、说话人名、中文源节目的金句原文——这些**本来
    就该是中文**，不是漏译。标上 lang 对屏幕阅读器和字体选择也是对的，
    而"零漏译"闸门正是靠这个标记区分"该是中文"和"忘了译"。
    简体页上不加，避免改变现有输出。
    """
    if LANG == "zh" or not text or not _HAS_CJK.search(str(text)):
        return ""
    return ' lang="zh"'


def e(s) -> str:
    return html.escape(str(s or ""), quote=True)


def en_store() -> dict[str, dict]:
    """data/en/<slug>.json，按 slug 取。"""
    out = {}
    d = DATA / "en"
    if not d.exists():
        return out
    for f in d.glob("*.json"):
        try:
            r = json.loads(f.read_text())
        except Exception:
            continue
        if r.get("slug"):
            out[r["slug"]] = r
    return out


_EN: dict[str, dict] = {}
_EN_SRC: dict[str, dict] = {}


def D(ep: dict) -> dict:
    """这一集在**当前语言**下要显示的成稿。

    简体模式就是 ep["digest"]。英文模式把正文字段换成译文，但
    **quality / tags 的时间戳、数字这些不动**，金句另有规矩：
    英文源节目直接用 raw（说话人原话），中文源才用译文并标出来。
    """
    d = ep["digest"]
    if LANG == "zh":
        return d
    en = _EN.get(ep.get("slug") or "")
    if not en:
        return d
    q_src = d.get("quotes") or []
    q_en = en.get("quotes") or []
    quotes = []
    for i, q in enumerate(q_src):
        got = q_en[i] if i < len(q_en) else {}
        text = got.get("text") or q.get("raw") or ""
        quotes.append({"t": q.get("t"), "spk": q.get("spk"),
                       "raw": text,
                       # 中文源：把中文原文留在 zh 位上，页面会标 lang="zh"
                       "zh": (q.get("raw") or "") if got.get("translated") else "",
                       "translated": bool(got.get("translated"))})
    merged = dict(d)
    merged.update({
        "title": en.get("title") or d.get("title"),
        "dek": en.get("dek") or d.get("dek"),
        "why": en.get("why") or "",
        "who": en.get("who") or "",
        "skip": en.get("skip") or "",
        "tags": en.get("tags") or [],
        "points": [dict(p, h=(en["points"][i].get("h") or p.get("h")),
                        body=(en["points"][i].get("body") or p.get("body")))
                   for i, p in enumerate(d.get("points") or [])
                   if i < len(en.get("points") or [])],
        "terms": [dict(t, term=(en["terms"][i].get("term") or t.get("term")),
                       zh="", **{"def": en["terms"][i].get("def") or t.get("def")})
                  for i, t in enumerate(d.get("terms") or [])
                  if i < len(en.get("terms") or [])],
        "facts": [dict(f, k=(en["facts"][i].get("k") or f.get("k")),
                       v=(en["facts"][i].get("v") or f.get("v")))
                  for i, f in enumerate(d.get("facts") or [])
                  if i < len(en.get("facts") or [])],
        "quotes": quotes,
    })
    return merged


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


# 语言层：静态 hreflang（爬虫不执行 JS，只有静态标签能让搜索引擎知道
# 这两个地址是同一篇的两种语言）+ 首访按浏览器语言跟随 + 头部的切换按钮。
#
# 按钮本身由 mast() 渲染在 .mast-side 里（跟主题按钮并排），这里只负责接上它。
# 第一版是 JS 造一个 position:fixed 贴在 body 上 —— 它飘在页面最右上角，
# 跟下面那排头部控件完全脱节，看着像掉出来的。
#
# 两种语言的文案写在 data-sc / data-tw 上，由 JS 按当前路径选：简体页显示
# 「繁體」，繁体页显示「简体」（tw.py 会把它一并转成「簡體」，正好对）。
# 不能只写一个字符串靠转换 —— 繁体页需要的是「簡體」，而「繁體」转换后还是
# 「繁體」，一个字符串出不来两种结果。
# 点过切换就把选择记进 localStorage，优先级高于浏览器语言，否则一个在台湾
# 用简体的读者每次都被弹走。整段在 <head> 里同步跑，首屏渲染前完成。
LANG_JS = ("<script>(function(){try{"
           # 语言偏好的键**三个站共用**：ourword.ai 下的主站、原声、品味同源，
           # localStorage 是通的。原来各站一个键（hwx_lang / podcast_lang），
           # 读者在主站选了繁體，进原声还是简体 —— 同一个人同一个域，
           # 选一次却不通用。
           "var K='hwx_lang',p=location.pathname;"
           "var tw=/^\\/podcast\\/tw(\\/|$)/.test(p),en=/^\\/podcast\\/en(\\/|$)/.test(p);"
           "var cur=en?'en':(tw?'tw':'sc');"
           # 三棵树之间互相换前缀。先剥掉现有前缀拿到"简体路径"，再加目标前缀。
           "var bare=p.replace(/^\\/podcast\\/(tw|en)/,'/podcast')||'/podcast/';"
           "var to={sc:bare,tw:bare.replace(/^\\/podcast/,'/podcast/tw'),"
           "en:bare.replace(/^\\/podcast/,'/podcast/en')};"
           "var saved=null;try{saved=localStorage.getItem(K)}catch(e){}"
           # **URL 里已经写了语言，就以 URL 为准。**
           # 原来无条件跟随浏览器语言，后果实测过：打开
           # /podcast/tw/p/… 会被改写成 /podcast/p/… —— 只要浏览器的语言列表里
           # 有 zh。台湾读者转给朋友的繁体链接，落地全变简体。
           # 现在只在读者落在默认语言（无前缀）时才跟随偏好或浏览器语言。
           "if(cur!=='sc'){"
           # 没记过偏好的把这次当成他的选择；已经记过的不动 ——
           # 一条别人分享的链接不该永久改掉你的语言。
           "if(!saved){try{localStorage.setItem(K,cur)}catch(e){}}"
           "}else{"
           "var L=(navigator.languages||[navigator.language||'']).join(',');"
           "var want=saved||(/zh-(hant|tw|hk|mo)/i.test(L)?'tw':'sc');"
           # 只有这一页真有英文版时才跟随 en 偏好——译文是逐篇补的，
           # 跳到一个 404 比不跳糟得多。占位元素上的 data-en 说明有没有。
           "if(want==='en'){var g=document.getElementById('lang-toggle');"
           "if(!(g&&g.getAttribute('data-en')))want='sc'}"
           "if(want!=='sc'){location.replace(to[want]);return}"
           "}"
           "document.addEventListener('DOMContentLoaded',function(){"
           # 切换控件是**一个下拉、三项**。原来是"简繁一个按钮 + 英文一个链接"，
           # 对读者是同一件事，却给了两种控件。三个站的这个控件长得一样。
           "var b=document.getElementById('lang-toggle');if(!b)return;"
           "var hasEn=!!b.getAttribute('data-en');"
           "var sel=document.createElement('select');sel.id='lang-toggle';"
           "sel.className=b.className;sel.setAttribute('aria-label','\\u8bed\\u8a00 Language');"
           # 每一项用**它自己的语言**写（简体 / 繁體 / English），这是语言选择器
           # 的惯例——一个只读英文的人也认得出 English 那一项。所以中文那两项
           # 带 lang，不是漏译：渲染层体检查的是"中文必须被显式标注"，
           # 而这些 option 是 JS 建的，构建期的 enscan 看不到，只有它能抓到。
           # 标签只用一个字／两个字母，和另外两个站一致：「English」
           # 一个词就占 87px，窄屏页头放不下。原生 <select> 收起和展开
           # 是同一份文字，所以列表里也是短的 —— 语言选择器的惯例是
           # 每一项用它自己的语言写，简／繁／EN 三个都认得出。
           "var opts=[['sc','\\u7b80','zh-Hans'],"
           "['tw','\\u7e41','zh-Hant']];"
           # 这一页没有英文版就不给这一项——比跳 404 或悄悄跳回英文首页都好
           "if(hasEn)opts.push(['en','EN','en']);"
           "opts.forEach(function(kv){"
           "var o=document.createElement('option');o.value=kv[0];o.textContent=kv[1];"
           "if(kv[2])o.lang=kv[2];"
           "if(kv[0]===cur)o.selected=true;sel.appendChild(o)});"
           "sel.onchange=function(){if(sel.value===cur)return;"
           "try{localStorage.setItem(K,sel.value)}catch(e){};location.href=to[sel.value]};"
           "b.parentNode.replaceChild(sel,b)})"
           "}catch(e){}})();</script>")


def _has_en(path: str) -> bool:
    """这个 path 有英文版吗。

    列表页（/、/sources/、/log/、/s/<id>/）总是有；单集页要看那一篇译了没有。
    判断落在**磁盘上真有那个目录**，不是"数据里有译文"——两者会在构建中途
    不一致（英文那趟还没跑到），而 hreflang 说的是"那个 URL 存在"。
    """
    if LANG == "en":
        return True
    if not path.startswith("/p/"):
        return True
    slug = path[3:].rstrip("/")
    from urllib.parse import unquote
    # **看译文数据，不看构建产物。** 我第一版读的是 en/p/<slug> 目录，
    # 而简体那趟**先跑**——CI 上是全新 checkout，那时 en/ 还不存在，于是没有
    # 一个中文页会声明英文版。本地能过只因为上一轮的 en/ 还留在盘上：
    # 典型的顺序依赖被残留状态掩盖。
    # 译文数据在两趟之前就都在，而英文那趟渲染的正是"有译文的那些集"，
    # 所以这个判断和最终产物必然一致。
    return (DATA / "en" / f"{unquote(slug)}.json").exists()


def hreflangs(path: str) -> str:
    """三语互指。

    hreflang 必须**每个版本都列出全部三个**，而且都指向同一个 path 的三个语言
    版本——只在中文页上写、或者各写各的，搜索引擎就认不出它们是同一篇的不同语言。
    zh-Hans / zh-Hant 是同一棵树的两份产物（tw.py 转的），英文是另一棵树。

    英文站没上线时不列 en：指向一个不存在或半成品的页面比不指更糟。
    """
    zh = SITE_ZH + path
    rows = [f'<link rel="alternate" hreflang="zh-Hans" href="{e(zh)}">',
            f'<link rel="alternate" hreflang="zh-Hant" href="{e(SITE_ZH + "/tw" + path)}">']
    # **只在这一页真有英文版时才声明 en。** 译文是逐篇补的，没译的集不进 /en/；
    # 在它们的中文页上写 hreflang=en 就等于把搜索引擎指到一个 404。
    if EN_LIVE and _has_en(path):
        rows.append(f'<link rel="alternate" hreflang="en" href="{e(SITE_ZH + "/en" + path)}">')
    rows.append(f'<link rel="alternate" hreflang="x-default" href="{e(zh)}">')
    return "\n".join(rows)


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
<html lang="{LANG_ATTR}">
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
<link rel="apple-touch-icon" sizes="180x180" href="{BASE}/apple-touch-icon.png">
<link rel="alternate" type="application/rss+xml" title="{e(NAME)}" href="{BASE}/feed.xml">
{hreflangs(path)}
{LANG_JS}
<link rel="stylesheet" href="{asset("assets/site.css")}">
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


def share_button(text: str, *, url: str, title: str, label: str = "") -> str:
    """一个按钮，把内容拼成一段能直接粘贴的文本。

    微信和朋友圈不给网页调起分享——那需要认证公众号、JS 接口安全域名和服务端签名。
    所以走和「走你」同一套办法：复制一段写好的文本，用户粘到哪都成立（微信、朋友圈、
    群、微博、备忘录）。手机上如果有系统分享面板就先用它，装了微信就直接出现在里面。

    文本放 data 属性里，换行写成 &#10;——这样不需要额外的 JSON 或内联脚本。
    """
    return (f'<button class="share-btn" type="button" data-share '
            f'data-share-title="{e(title)}" data-share-url="{e(url)}" '
            f'data-share-text="{e(text).replace(chr(10), "&#10;")}" '
            f'aria-label="{T("复制分享文本")}">{ICON_SHARE}<span>{e(label or T("分享"))}</span></button>')


def _clip(s: str, n: int) -> str:
    s = squeeze(s or "")
    return s if len(s) <= n else s[:n - 1].rstrip("，。、；：,. ") + "…"


def episode_share_text(ep: dict) -> str:
    """粘到微信里要立得住：标题、一句话、三条要点、一句金句、链接。

    控制在 500 字以内——朋友圈超长会折叠，群里刷屏也没人读。
    """
    d = D(ep)
    src = show_name(ep)
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
    """短链页：canonical 指回正文，noindex 防止和正文抢排名，然后立刻跳走。

    **og 标签必须齐。** 分享按钮给出的就是这个短链（正文 slug 是中文，
    percent-encode 之后两百多字符，粘到朋友圈里链接比内容还长），而抓预览图的
    一方（微信、Twitter、Slack）**只读它拿到的那个 URL 的 meta，不跟 canonical、
    不跟 refresh、更不执行 JS**。这个页面原来只有 title 和 canonical，
    于是**全站每一次分享都没有预览图**——用户报的"分享 url 时没带预览图片"。

    canonical 指回正文是给搜索引擎看的，跟社交抓图是两套完全独立的机制，
    别指望前者能替后者办事。
    """
    real = f"{BASE}/p/{urllib.parse.quote(ep['slug'])}/"
    full = f"{SITE}/p/{urllib.parse.quote(ep['slug'])}/"
    d = D(ep)
    t = e(d.get("title") or "")
    desc = e(_clip(d.get("dek") or "", 150))
    img = e(og_image(ep.get("image") or ""))
    src = e(show_name(ep))
    pic = ""
    if img:
        pic = ('<meta property="og:image" content="%s">\n'
               '<meta property="og:image:alt" content="%s">\n'
               '<meta name="twitter:image" content="%s">' % (img, t, img))
    head = "\n".join([
        '<!DOCTYPE html>',
        '<html lang="{LANG_ATTR}"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f'<title>{t} — {e(NAME)}</title>',
        '<meta name="robots" content="noindex,follow">',
        f'<link rel="canonical" href="{e(full)}">',
        f'<meta name="description" content="{desc}">',
        '<meta property="og:type" content="article">',
        f'<meta property="og:site_name" content="{e(NAME)}">',
        '<meta property="og:locale" content="zh_CN">',
        f'<meta property="og:title" content="{t}">',
        f'<meta property="og:description" content="{desc}">',
        f'<meta property="og:url" content="{e(full)}">',
        f'<meta property="article:section" content="{src}">',
        pic,
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{t}">',
        f'<meta name="twitter:description" content="{desc}">',
        f'<meta http-equiv="refresh" content="0;url={e(real)}">',
        f'<script>location.replace({json.dumps(real)})</script>',
        '</head><body>',
        f'<p>{T("正在打开")}{t}……',
        f'<a href="{e(real)}">{T("点这里")}</a></p>',
        '</body></html>',
    ])
    return head.replace("\n\n", "\n") + "\n"


def source_share_text(src: dict, rows: list[dict]) -> str:
    name = src_display(src)
    lines = [f"《{_clip(name, 34)}》· {NAME}深读", ""]
    if src.get("desc"):
        lines += [_clip(src_desc(src), 96), ""]
    for x in rows[:3]:
        lines.append("· " + _clip(D(x).get("title"), 34))
    lines += ["", f"本站已深读 {len(rows)} 篇：{SITE}/s/{src['id']}/"]
    return "\n".join(lines)


def site_share_text(eps: list[dict]) -> str:
    """整站的分享文本。不吹功能，说清它凭什么值得点开：
    每条判断都能跳回原声，对不上的当场删掉。"""
    lines = [f"{NAME} · {TAGLINE}", "",
             _clip(BLURB, 110), ""]
    for x in eps[:3]:
        src = show_name(x)
        lines.append(f"· {_clip(D(x).get('title'), 30)}（{_clip(src, 14)}）")
    lines += ["", f"{SITE}/"]
    return "\n".join(lines)


def lang_switch(path: str = "/") -> str:
    """三语切换，**一个下拉搞定三项**。

    简体 ↔ 繁体是同一份 HTML 的两棵树（tw.py 转的），英文是第三棵树；对读者来说
    这是同一件事，所以不该是"一个按钮加一个链接"两种控件。三个站的这个控件
    长得一样（同伴统一过），这里只是把英文加进同一个下拉。

    `data-en` 说明**这一页有没有英文版**：译文是逐篇补的，没译的集不进 /en/。
    没有就不给英文那一项——比给一个跳到 404 或悄悄跳回英文首页都好。
    JS 里再降级一次（没有 JS 时它就是个空占位，不会显示一个点不动的控件）。
    """
    if LANG == "en":
        # 英文树上：JS 把它换成三项下拉；没有 JS 时退化成一个回中文站的链接
        return (f'<a class="pill ghost" id="lang-toggle" lang="zh" '
                f'data-cur="en" data-en="1" href="{BASE_ZH}/">中文</a>')
    en = "1" if (EN_LIVE and _has_en(path)) else ""
    return ('<button class="pill ghost" id="lang-toggle" type="button"'
            f' data-en="{en}" data-sc="繁體" data-tw="简体"></button>')


def masthead(n: int | None, *, home: bool, path: str = "/") -> str:
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
    count = f'<span class="stat">{i18n.n(n, "read", "篇深读")}</span>' if n else ""
    return f"""<header class="mast"><div class="wrap">
<div class="mast-top">
{brand}
<div class="mast-side">
{count}
{lang_switch(path)}
<button class="icon-btn" data-theme-toggle aria-label="{T("切换深浅色")}">{ICON_THEME}</button>
</div></div>
<p class="slogan">{TAGLINE}</p>
</div></header>"""


def foot() -> str:
    return f"""<footer class="foot"><div class="wrap"><div class="foot-in">
<div>{NAME} · <a href="https://ourword.ai">OurWord.ai</a>{T("的播客线。内容为原播客的中文深读，")}
{T("版权归各节目所有；每篇都附原节目链接，请去支持原作者。")}</div>
<div class="family" style="margin:0 0 14px;font-size:13px;opacity:.72">\
<a href="/" lang="zh">人类世界生存法则</a> · <a href="{BASE}/">{NAME}</a> · \
<a href="/skill/" lang="zh">品味</a></div>
<div class="links"><a href="{BASE}/">{T("首页")}</a><a href="{BASE}/sources/">{T("信源")}</a>
<a href="{BASE}/log/">{T("更新日志")}</a><a href="{BASE}/feed.xml">RSS</a>
<a href="{BASE}/llms.txt">llms.txt</a>
<a href="https://github.com/woowoeth/podcast">{T("源码")}</a></div>
</div></div></footer>
<script src="{asset("assets/site.js")}" defer></script>
</body></html>
"""


# ----------------------------------------------------------------------- feed

TSRC_LABEL = {"feed": "官方逐字稿", "notes": "官方全文", "page": "官方文稿页",
              "youtube": "YouTube 字幕", "asr": "音频转写"}


def thumb(url: str, w: int = 400) -> str:
    """把外链封面换成 CDN 的小尺寸版本。

    封面全是热链的第三方 CDN，改不了文件本身，但这几家都支持尺寸参数，
    而卡片上这张只显示 ~150px：
      小宇宙 image.xyzcdn.net   3249 KB → 10 KB（七牛 imageMogr2 + webp）
      Omny  size=Large         383 KB → 51 KB（改 Medium）
      imgix / simplecast        支持 ?w=
    首页原来一次要下 5.6 MB 图片。不认识的域名原样返回——宁可大，不要开天窗。
    """
    if not url or "?" in url and "imageMogr2" in url:
        return url
    try:
        host = url.split("/")[2].lower()
    except IndexError:
        return url
    if host.endswith("image.xyzcdn.net"):
        return url + ("&" if "?" in url else "?") + "imageMogr2/thumbnail/%dx/format/webp" % w
    if host.endswith("omnycontent.com"):
        return url.replace("size=Large", "size=Medium").replace("size=large", "size=Medium")
    if host.endswith("imgix.net"):
        return url + ("&" if "?" in url else "?") + "w=%d&auto=format" % w
    if host.endswith("ichef.bbci.co.uk"):
        # 路径里那段就是尺寸：/images/ic/3000x3000/xxx.jpg。3000 见方一张 2952 KB，
        # 换成 400 见方是 32 KB，同一张图。顺手把 http 升成 https——原始数据给的是
        # http，在 https 页面上要么被拦要么被浏览器升级，不如自己写对。
        u = re.sub(r"/images/ic/\d+x\d+/", "/images/ic/%dx%d/" % (w, w), url)
        return u.replace("http://", "https://", 1)
    # 其余域名不认识尺寸参数：只把 http 升成 https，别的原样返回。
    return url.replace("http://", "https://", 1) if url.startswith("http://") else url


def card(ep: dict, *, hero: bool) -> str:
    d = D(ep)
    q = d.get("quality") or {}
    date = (ep.get("published") or "")[5:10].replace("-", "")
    hay = " ".join([d.get("title", ""), d.get("dek", ""), ep.get("source", ""),
                    ep.get("source_zh", ""), ep.get("title_original", ""),
                    " ".join(d.get("tags") or []),
                    " ".join(t.get("term", "") for t in d.get("terms") or [])])
    img = ep.get("image") or ""
    cover = (f'<img src="{e(thumb(img))}" alt="" loading="lazy" decoding="async" '
             f'data-initial="{e((ep.get("source") or "?")[:1])}">' if img
             else f'<div class="fallback">{e((ep.get("source") or "?")[:1])}</div>')
    dur = (f'<span class="dur">{hhmmss(ep["duration"])}</span>' if ep.get("duration") else "")
    tags = "".join(f'<span class="tag">{e(t)}</span>' for t in (d.get("tags") or [])[:2])
    src_label = show_name(ep)
    return f"""<a class="card{' hero' if hero else ''}" data-card data-cat="{e(ep.get('cat'))}"
 data-hay="{e(hay)}" href="{BASE}/p/{e(ep['slug'])}/">
<div class="cover">{cover}{dur}</div>
<div class="card-body">
<div class="kicker" data-cat="{e(ep.get('cat'))}"><span class="src"{zh_attr(src_label)}>{e(src_label)}</span>
<span class="date">{e(date)}</span></div>
<h2>{mark_zh(e(d.get('title')))}</h2>
<p class="dek">{mark_zh(e(d.get('dek')))}</p>
<div class="card-foot">
<span class="badge"><b>{q.get('points', 0)}</b> {T("要点")}</span>
<span class="badge"><b>{q.get('verified_quotes', 0)}</b> {T("金句")}</span>
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
        d = D(x)
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


def write_card_pages(eps: list[dict], out: pathlib.Path | None = None) -> int:
    """首屏之外的卡片，按页写成 cards-1.json、cards-2.json……

    为什么分页而不是一个大文件：第一版把剩下 231 张全塞进一个 cards.json，
    滚到底一次性插入——那不是分页加载，是"晚一点的全量加载"（96 KB + 231 个
    DOM 节点一次进来）。现在一页 24 张，滚到哪加载到哪。

    为什么存 HTML 而不是存数据让前端拼：卡片的标记必须和首屏那批一模一样，
    两份渲染逻辑迟早会长歪（首屏加了个角标、这边没加）。存 HTML 只有一份真相。
    """
    # **必须收输出目录。** 这个函数原来直接写 ROOT，而 render_site 现在会被
    # 调两次（简体渲到仓库根、英文渲到 en/）——于是英文那一趟把**根目录**的
    # cards-*.json 覆盖成了带 /podcast/en/ 链接的版本，简体首页滚到第二页就
    # 全跳到英文站去了。凡是写文件的函数都不许再自己决定往哪写。
    out = out or ROOT
    rest = eps[FIRST_PAGE:]
    pages = 0
    for i in range(0, len(rest), FIRST_PAGE):
        pages += 1
        (out / f"cards-{pages}.json").write_text(
            json.dumps([card(x, hero=False) for x in rest[i:i + FIRST_PAGE]],
                       ensure_ascii=False))
    # 页数变少时把多出来的旧文件删掉，否则前端会取到过期的卡片
    n = pages + 1
    while (out / f"cards-{n}.json").exists():
        (out / f"cards-{n}.json").unlink()
        n += 1
    old = ROOT / "cards.json"          # 第一版的单文件，不再用
    if old.exists():
        old.unlink()
    return pages


def index_page(eps: list[dict], srcs: dict) -> str:
    counts = {c: sum(1 for x in eps if x.get("cat") == c) for c in CAT_ORDER}
    chips = [f'<button class="chip" data-cat-chip="all" aria-pressed="true">{T("全部")}'
             f'<span class="n">{len(eps)}</span></button>']
    for c in CAT_ORDER:
        chips.append(f'<button class="chip" data-cat-chip="{c}" aria-pressed="false">'
                     f'{T(CAT_LABEL[c])}<span class="n">{counts.get(c, 0)}</span></button>')
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
                             "name": D(x).get("title")}
                            for i, x in enumerate(eps[:60])]}}]})
    return (head(TAGLINE, BLURB, path="/",
                 image=(eps[0].get("image") if eps else ""), extra=ld)
            + masthead(len(eps), home=True, path="/")
            + f"""
<div class="toolbar"><div class="wrap"><div class="toolbar-in">
<label class="search">{ICON_SEARCH}
<input data-search type="search" placeholder="{T("搜正文、金句、数字、术语、节目…")}" aria-label="{T("搜索")}">
<kbd>/</kbd></label>
<div class="chips">{''.join(chips)}</div>
{share_button(site_share_text(eps), url=SITE + "/", title=f"{NAME} · {TAGLINE}", label=T("分享本站"))}
</div></div></div>

<main class="wrap"><div class="feed" data-feed data-total="{len(eps)}"
     data-page-size="{FIRST_PAGE}"
     data-pages="{max(0, (len(eps) - FIRST_PAGE + FIRST_PAGE - 1) // FIRST_PAGE)}">
{cards}
<div class="empty" data-empty hidden><b>{T("没有匹配的深读")}</b>
<p>{T("换个词，或者清掉筛选再试。搜索会搜进每条要点的正文、金句的中英文原文、数字和术语表——不只是标题。")}</p>
<p data-deep-note hidden style="color:var(--faint);font-size:13px"></p></div>
</div>
{f'''<div class="more" data-sentinel>
<span class="more-count" data-more-count>{FIRST_PAGE} / {len(eps)}</span>
<button class="more-btn" data-more type="button" hidden>{T("继续加载")}</button>
<noscript><p class="note">{T("没有 JavaScript 时只显示最新 N 篇，完整清单见 sitemap 或 llms.txt。").replace("N", str(FIRST_PAGE))}</p></noscript>
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


# 三角形的内联 SVG。为什么不靠 CSS 画：CSS 没加载上时（缓存旧版本、请求被拦）
# 空 span 彻底看不见，读者就看不出这张图能点。SVG 自带尺寸和颜色，零 CSS 也在。
PLAY_SVG = ('<svg viewBox="0 0 44 44" width="44" height="44" focusable="false">'
            '<circle cx="22" cy="22" r="21" fill="rgba(18,16,13,.55)" '
            'stroke="rgba(255,255,255,.92)" stroke-width="1.5"/>'
            '<path d="M17 14.5 32 22 17 29.5Z" fill="#fff"/></svg>')


def player_block(ep: dict) -> str:
    """正文顶部的播放器：一张卡，视频在上、音频在下。

    位置：放这里而不是侧栏或文末。这个站的前提是"每条判断都能跳回原声核对"，
    播放器是为时间戳服务的——侧栏只有 264px，视频小到没法看；放文末的话，
    正文各处的时间戳都要往回滚很远。

    有视频时**只显示视频**：同一张卡上摆两个播放器，读者只会用一个，另一个是
    噪音。但音频元素仍然留在 DOM 里、默认 hidden——视频加载失败（区域限制、
    嵌入被关、脚本被拦）时由脚本露出来。这是"看起来干净"和"不走进死路"的两全：
    之前那版有视频就干脆不输出音频，YouTube 一放不出来读者就只剩一个黑框和
    一堆没处跳的时间戳。

    视频先给封面图加播放按钮的假门，点了才换成真播放器：YouTube 的 iframe API
    约 100 KB，不点视频的读者一个字节都不下载。

    封面尺寸的坑（线上真出过）：给 img 写 height="360" 属性等于指定了 height，
    两边都定死时 CSS 的 aspect-ratio **不生效**，16:9 的框退回 4:3，露出 YouTube
    缩略图自带的黑边。所以只给 width，高度交给外层 padding-top 百分比撑。

    播放圈里放内联 SVG，不放空的 span 靠 CSS 画：CSS 万一没加载上（缓存拿到旧
    版本、请求被拦），空 span 就是彻底看不见——线上真这样过一轮，读者只看见一张
    静态图，唯一能点的是下面那条音频，于是"点播放器只有声音没有视频"。

    时长不再单独标一个徽标：音频那条已经写着 0:00 / 2:34:18，同一个数字在同一张
    卡上出现两次是噪音。

    音频那一栏是渐进增强：<audio> 带着 controls 出，自定义那层默认 hidden，
    脚本跑起来才对调。脚本没跑就还是原生控件，不会变成一个点不动的死条。
    """
    vid = ep.get("youtube_id")
    audio = ep.get("audio")
    if not (vid or audio):
        return ""
    secs = int(ep.get("duration") or 0)
    dur = hhmmss(secs) or ""
    out = ['<div class="player" data-player-box>']
    if vid:
        # hq720 是真 16:9；hqdefault 是 4:3 补黑边的，只当兜底
        poster = f"https://i.ytimg.com/vi/{vid}/hq720.jpg"
        fallback = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        out.append(
            f'  <button class="video-facade" data-yt="{e(vid)}" type="button"\n'
            f'          aria-label="{T("播放原节目视频")}">\n'
            f'    <span class="frame">\n'
            f'      <img src="{e(poster)}" alt="" loading="lazy" width="1280"\n'
            f'           onerror="this.onerror=null;this.src=\'{e(fallback)}\'">\n'
            f'      <span class="play" aria-hidden="true">{PLAY_SVG}</span>\n'
            '    </span>\n'
            '  </button>')
    if audio:
        # 有视频时音频条默认收起：同一张卡上两个播放器，读者只会用一个。
        # 但元素留在 DOM 里——视频加载失败（区域限制、嵌入被关、脚本被拦）时
        # 由脚本露出来，不然读者就走进死路：一个黑框加没处跳的时间戳。
        hide = ' hidden' if vid else ''
        out.append(
            f'  <div class="strip" data-audio-strip{hide}>\n'
            f'    <p class="afallback">{T("原视频在 YouTube 上放不出来，用音频听：")}</p>\n'
            f'    <audio data-player controls preload="none" src="{e(audio)}"></audio>\n'
            f'    <div class="aui" data-audio-ui data-dur="{secs}" hidden>\n'
            f'      <button class="aplay" type="button" aria-label="{T("播放")}"></button>\n'
            f'      <div class="abar" role="slider" tabindex="0" aria-label="{T("播放进度")}"\n'
            '           aria-valuemin="0" aria-valuemax="100"><div class="afill"></div></div>\n'
            '      <span class="atime"><span class="acur">0:00</span>'
            '<span class="asep">/</span><span class="atot">--:--</span></span>\n'
            '    </div>\n'
            '  </div>')
    out.append('</div>')
    return "\n".join(out)


def episode_page(ep: dict, prev: dict | None, nxt: dict | None) -> str:
    d = D(ep)
    q = d.get("quality") or {}
    src_label = show_name(ep)
    date = (ep.get("published") or "")[:10]

    def ts(t, cls="ts"):
        return (f'<a class="{cls}" data-t="{int(t)}" href="{e(seek_href(ep, t))}" '
                f'target="_blank" rel="noopener">{hhmmss(t)}</a>')

    points = "\n".join(
        f"""<div class="point">{ts(p['t'])}<div><h4>{mark_zh(e(p['h']))}</h4>
<p class="body">{mark_zh(e(p['body']))}</p>
{f'<span class="spk"{zh_attr(p["spk"])}>— {e(p["spk"])}</span>' if p.get('spk') else ''}</div></div>"""
        for p in d.get("points") or [])

    quotes = "\n".join(
        f"""<blockquote class="quote"><p class="raw"{zh_attr(qq['raw'])}>{e(qq['raw'])}</p>
{f'<p class="zh"{zh_attr(qq["zh"])}>{e(qq["zh"])}</p>' if qq.get('zh') else ''}
<div class="attrib">{f'<b{zh_attr(qq["spk"])}>{e(qq["spk"])}</b>' if qq.get('spk') else ''}{ts(qq['t'])}</div>
</blockquote>"""
        for qq in d.get("quotes") or [])

    facts = ""
    if d.get("facts"):
        rows = "\n".join(
            f'<tr><td class="k">{mark_zh(e(f["k"]))}</td><td class="v">{mark_zh(e(f["v"]))}</td>'
            f'<td class="t">{ts(f["t"]) if f.get("t") is not None else ""}</td></tr>'
            for f in d["facts"])
        facts = f'<section class="section"><h2>{T("数字与实体")}</h2><table class="facts">{rows}</table></section>'

    terms = ""
    if d.get("terms"):
        items = "\n".join(
            f'<div><dt>{mark_zh(e(t["term"]))}<span{zh_attr(t["zh"])}>{e(t["zh"])}</span></dt>'
            f'<dd>{mark_zh(e(t.get("def")))}</dd></div>' for t in d["terms"])
        terms = f'<section class="section"><h2>{T("术语")}</h2><dl class="terms">{items}</dl></section>'

    toc = "\n".join(f'<a href="#p{i}"><span class="t">{hhmmss(p["t"])}</span>'
                    f'<span>{mark_zh(e(p["h"]))}</span></a>'
                    for i, p in enumerate(d.get("points") or []))
    points = re.sub(r'<div class="point">', lambda m, c=iter(range(999)):
                    f'<div class="point" id="p{next(c)}">', points)

    player = (f'<audio data-player controls preload="none" src="{e(ep["audio"])}"></audio>'
              if ep.get("audio") else "")
    tsrc = T_dict(TSRC_LABEL, q.get("transcript_source"), q.get("transcript_source") or "—")
    rv = ep.get("review") or {}
    rvs = f"{rv['score']:.0f}" if isinstance(rv.get("score"), (int, float)) else ""
    orig = ep.get("link") or (f"https://www.youtube.com/watch?v={ep['youtube_id']}"
                              if ep.get("youtube_id") else "")

    tags = "".join(f'<span class="tag">{e(t)}</span>' for t in (d.get("tags") or []))
    prevnext = ""
    if prev or nxt:
        left = (f'<a href="{BASE}/p/{e(prev["slug"])}/"><span class="lbl">{T("← 更新")}</span>'
                f'<strong>{e(D(prev)["title"])}</strong></a>' if prev else "<span></span>")
        right = (f'<a class="r" href="{BASE}/p/{e(nxt["slug"])}/"><span class="lbl">{T("更早 →")}</span>'
                 f'<strong>{e(D(nxt)["title"])}</strong></a>' if nxt else "<span></span>")
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
        {"@type": "ListItem", "position": 1, "name": T("首页"), "item": SITE + "/"},
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
            + masthead(None, home=False, path=f"/p/{urllib.parse.quote(ep['slug'])}/")
            + f"""
<main class="wrap ep">
<nav class="crumb"><a href="{BASE}/">{T("首页")}</a><span class="sep">/</span>
<a href="{BASE}/s/{e(ep.get('source_id'))}/"{zh_attr(src_label)}>{e(src_label)}</a>
<span class="sep">/</span><span>{e(date)}</span></nav>

<div class="ep-grid">
<article>
<div class="ep-head">
<div class="kicker" data-cat="{e(ep.get('cat'))}"><span class="src"{zh_attr(src_label)}>{e(src_label)}</span>
<time class="date" datetime="{e(ep.get('published'))}">{e(date)}</time>
{share_button(episode_share_text(ep), url=ep_url(ep), title=d.get('title') or '')}</div>
<h1>{mark_zh(e(d.get('title')))}</h1>
<p class="dek-lead">{mark_zh(e(d.get('dek')))}</p>
<div class="ep-meta">{tags}</div>
</div>
{player_block(ep)}

{f'<section class="section"><div class="why">{mark_zh(e(d.get("why")))}</div></section>' if d.get('why') else ''}

<section class="section"><h2>{T('核心论点 · 时间戳为按文稿位置估算') if q.get('approx_timestamps') else T('核心论点 · 点时间戳可跳到原声')}</h2>{points}</section>
{f'<section class="section"><h2>{T("原话 · 已逐字校验")}</h2>{quotes}</section>' if quotes else ''}
{facts}
{terms}
{f'''<section class="section"><h2>{T("收听指南")}</h2>
<div class="panel guide">
<div><span class="k">{T("谁该听")}</span><p>{mark_zh(e(d.get("who")))}</p></div>
{f'<div><span class="k">{T("可跳过")}</span><p>{mark_zh(e(d.get("skip")))}</p></div>' if d.get('skip') else ''}
</div></section>''' if d.get('who') else ''}
{prevnext}
</article>

<aside class="aside">
<div class="panel"><h4>{T("原节目")}</h4>
<div class="row"><span>{T("节目")}</span><span{zh_attr(ep.get('source'))}>{e(ep.get('source'))}</span></div>
<div class="row"><span>{T("原标题")}</span><span{zh_attr(ep.get('title_original'))}>{e(ep.get('title_original'))}</span></div>
<div class="row"><span>{T("发布")}</span><span>{e(date)}</span></div>
<div class="row"><span>{T("时长")}</span><span>{hhmmss(ep.get('duration')) or '—'}</span></div>
{f'<a class="row" href="{e(orig)}" target="_blank" rel="noopener"><span>{T("原页面")}</span><span>{T("打开 ↗")}</span></a>' if orig else ''}
{'' if (ep.get("audio") or ep.get("youtube_id")) else f'<p class="note">{T("时间戳会跳到原节目对应位置。")}</p>'}
</div>

{f'<div class="panel"><h4>{T("本篇结构")}</h4><nav class="toc">{toc}</nav></div>' if toc else ''}

<div class="panel"><h4>{T("这篇是怎么来的")}</h4>
{f'<div class="row"><span>{T("成稿评分")}</span><span>{rvs} / 10</span></div>' if rvs else ''}
<div class="row"><span>{T("文稿来源")}</span><span>{e(tsrc)}</span></div>
<div class="row"><span>{T("文稿字数")}</span><span>{q.get('words') or '—'}</span></div>
<div class="row"><span>{T("语速核验")}</span><span>{q.get('wpm') or '—'} wpm</span></div>
<div class="row"><span>{T("逐字校验金句")}</span><span>{i18n.n(q.get('verified_quotes', 0), 'quote')}</span></div>
<div class="row"><span>{T("回溯校验数字")}</span><span>{i18n.n(q.get('grounded_facts', 0), 'figure')}</span></div>
{f'<div class="row"><span>{T("质检剔除")}</span><span>{i18n.n(q["pruned"], 'dropped', '处')}</span></div>' if q.get('pruned') else ''}
<p class="note">{T("金句在逐字稿里逐字比对过，数字回原文核对过；对不上的当场删掉，不上站。")}
{T('成稿另经一道独立评审（信息密度／忠实度／选择力／具体性／中文），低于 7 分不展示。') if rvs else ''}
{T('这一集的文稿没有原始时间码，页面上的时间戳是按文稿位置估算的，只作粗略定位。') if q.get('approx_timestamps') else ''}</p>
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
                meta.append(i18n.covered(per[s["id"]], total) if total
                            else i18n.n(per[s["id"]], "read", "篇"))
            else:
                meta.append(T("本站尚未收录"))
            if st.get("cadence_days"):
                meta.append(i18n.cadence(st["cadence_days"]))
            if st.get("latest"):
                meta.append(f'{T("最新一集")} {st["latest"]}')
            if st.get("official_transcripts"):
                meta.append(T("自带官方逐字稿"))
            if s.get("kind") == "youtube":
                meta.append(T("YouTube 源"))
            if s.get("lang") == "zh":
                meta.append(T("中文"))
            if dead:
                meta.append(T("抓取异常"))
            mine = per.get(s["id"], 0)
            _nm = src_display(s)
            body = f"""<h3{zh_attr(_nm)}>{e(_nm)}</h3>
<p>{e(src_desc(s))}</p>
<div class="meta">{' · '.join(e(m) for m in meta)}</div>"""
            cards.append(
                f'<a class="src-card{" dead" if dead else ""}" id="{e(s["id"])}" '
                f'href="{BASE}/s/{e(s["id"])}/">{body}</a>' if mine else
                f'<div class="src-card{" dead" if dead else ""}" id="{e(s["id"])}">{body}</div>')
        blocks.append(f'<h2 class="sec-title">{T(CAT_LABEL[c])}<span class="stat" '
                      f'style="margin-left:10px">{i18n.n(len(rows), "show", "档")}</span></h2>'
                      f'<div class="src-grid">{"".join(cards)}</div>')
    n = len(srcs["sources"])
    ld = _ld({"@context": "https://schema.org", "@type": "CollectionPage",
              "url": SITE + "/sources/", "name": f'{T("信源")} — {NAME}',
              "inLanguage": "zh-CN", "isPartOf": {"@id": SITE + "/#site"},
              "mainEntity": {"@type": "ItemList", "numberOfItems": n,
                             "itemListElement": [
                                 {"@type": "ListItem", "position": i + 1,
                                  "item": {"@type": "PodcastSeries",
                                           "name": src_display(s0),
                                           "description": src_desc(s0),
                                           "url": (f"{SITE}/s/{s0['id']}/" if per.get(s0["id"])
                                                   else None)}}
                                 for i, s0 in enumerate(srcs["sources"])]}})
    return (head(f'{T("信源")} — {NAME}', T("SOURCES_DESC").replace("NAME", NAME).replace("{n}", str(n)),
                 path="/sources/", extra=ld)
            + masthead(len(eps), home=False, path="/sources/")
            + f"""<main class="wrap">
<h1 class="sec-title" style="margin-top:34px">{T("信源")} · {i18n.n(n, "show", "档")}</h1>
<p class="lede">{T("SRC_LEDE_1")}</p>
<p class="lede">{T("SRC_LEDE_2")}</p>
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
        rows.append((T("本站已深读"), i18n.n(covered, "episode", "集")))
    if st.get("cadence_days"):
        rows.append((T("更新节奏"), i18n.cadence(st['cadence_days'])))
    if st.get("latest"):
        rows.append((T("最新一集"), st["latest"]))
    if st.get("official_transcripts"):
        rows.append((T("官方逐字稿"), T("自带")))
    rows.append((T("分类"), T_dict(CAT_LABEL, src["cat"], src["cat"])))
    rows.append((T("优先级"), f"T{src.get('tier', 3)}"))
    meta = "".join(f'<div class="row"><span>{e(k)}</span><span>{e(v)}</span></div>'
                   for k, v in rows)
    ld = _ld({"@context": "https://schema.org", "@graph": [
        {"@type": "PodcastSeries", "name": name, "description": src_desc(src),
         "url": f"{SITE}/s/{src['id']}/", "inLanguage":
             "zh-CN" if src.get("lang") == "zh" else "en",
         "webFeed": src.get("feed")},
        {"@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "首页", "item": SITE + "/"},
            {"@type": "ListItem", "position": 2, "name": T("信源"), "item": SITE + "/sources/"},
            {"@type": "ListItem", "position": 3, "name": name}]}]})
    return (head(f"{name} — {NAME}", src_desc(src), path=f"/s/{src['id']}/", extra=ld)
            + masthead(len(eps), home=False, path=f"/s/{src['id']}/")
            + f"""<main class="wrap">
<nav class="crumb" style="margin-top:26px"><a href="{BASE}/">{T("首页")}</a><span class="sep">/</span>
<a href="{BASE}/sources/">{T("信源")}</a><span class="sep">/</span><span{zh_attr(name)}>{e(name)}</span></nav>
<div class="page-head">
<h1 class="sec-title" style="margin-top:0"{zh_attr(name)}>{e(name)}</h1>
{share_button(source_share_text(src, eps), url=f"{SITE}/s/{src['id']}/", title=name)}
</div>
<p class="lede">{e(src_desc(src))}</p>
<div class="panel" style="max-width:420px;margin:18px 0 4px">{meta}</div>
<div class="feed" data-feed>{cards or
  f'<div class="empty"><b>{T("这档还没有深读")}</b>{T("取不到可核对的文稿时不会发，等文稿到位再上。")}</div>'}</div>
</main>""" + foot())


# ---------------------------------------------------------------- 更新日志

KIND_LABEL = {"added": "收录", "removed": "移除", "demoted": "降级", "dormant": "休眠"}
# 用 T() 取，别在字典里存两套：字典是常量，语言是运行时决定的
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
        # why 是策展管线写的审计记录（中文）。英文版保留原文并标 lang="zh"：
        # 含义已经由结构化徽标（收录／降级 + 层级变化 + 分数）用英文表达了，
        # 这一行是可核对的出处，不是需要翻译的界面文案。
        _why = r.get("why") or ""
        detail = f'<span{zh_attr(_why)}>{e(_why)}</span>' if _why else ""
        extra = ""
        if kind == "added" and r.get("score") is not None:
            # 收录分是照着标题与分集说明打的，节目自己写的宣传文案也算在内。
            # 不标出来，读者会把它当成对内容的结论——而这档节目可能一篇都跑不出来。
            # 标记塞在同一个 span 里：.ev 是四列网格，多一个子元素会另起一行。
            flag = f'<em class="ev-flag">{T("试用")}</em>' if r.get("probation") else ""
            sc = i18n.score(f"{r['score']:.1f}")
            extra = f'<span class="ev-score">{sc}{flag}</span>'
        elif kind in ("demoted", "dormant") and r.get("from_tier"):
            extra = f'<span class="ev-score">T{r["from_tier"]} → T{r["to_tier"]}</span>'
        items.append(f"""<li class="ev {tone}">
<span class="ev-when">{e((r.get('at') or '')[:10])}</span>
<span class="ev-what">{e(T_dict(KIND_LABEL, kind, kind))}</span>
<span class="ev-who"{zh_attr(r.get('name'))}>{e(r.get('name'))}</span>{extra}
<span class="ev-why">{detail}</span></li>""")

    body = ("<ul class=\"evlist\">" + "".join(items) + "</ul>") if items else (
        '<div class="empty"><b>还没有信源变动</b>'
        '<p>信源清单每三天自动复查一次：feed 失效、停更超过 120 天、选题通过率低于 25%、'
        '或成稿评分中位不高于 7 的会被降级或移除；同时从近期内容里挖新源，只收 8 分以上。'
        '任何一次改动都会记在这里。</p></div>')

    ld = _ld({"@context": "https://schema.org", "@type": "CollectionPage",
              "url": SITE + "/log/", "name": f'{T("更新日志")} — {NAME}', "inLanguage": ("en" if LANG == "en" else "zh-CN"),
              "isPartOf": {"@id": SITE + "/#site"}})
    return (head(f'{T("更新日志")} — {NAME}',
                 f"{NAME} 的信源增删记录：什么时候收了谁、踢了谁、为什么。当前 {n_src} 档。",
                 path="/log/", extra=ld)
            + masthead(len(eps), home=False, path="/log/")
            + f"""<main class="wrap">
<h1 class="sec-title" style="margin-top:34px">{T("更新日志")}</h1>
<p class="lede">{T("LOG_LEDE_1")}</p>
<p class="lede">{T("LOG_LEDE_2A")}<em class="ev-flag">{T("试用")}</em>{T("LOG_LEDE_2B")}</p>
<p class="lede">{T("LOG_LEDE_3")}
<a href="{BASE}/sources/" style="color:var(--accent)">{T("信源页")}</a>。</p>
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
        d = D(x)
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
        d = D(x)
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
                f"- 文稿来源 / transcript: {T_dict(TSRC_LABEL, q.get('transcript_source'), q.get('transcript_source') or '—')}"
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
        d = D(x)
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
    return (head(f'{T("找不到这一页")} — {NAME}', "", path="/404.html",
                 robots="noindex,follow")
            + masthead(0, home=False, path="/404.html")
            + f"""<main class="wrap"><div class="empty" style="padding:110px 0">
<h1><b>{T("这一页不在了")}</b></h1><p><a href="{BASE}/" style="color:var(--accent)">{T("回到首页")}</a></p>
</div></main>""" + foot())


def render_site(out: pathlib.Path, lang: str = "zh") -> int:
    """把整站渲染到 out 目录。lang 决定文案层和正文取哪份数据。

    从 main() 里抽出来，就为了能用**同一套模板**渲染两次：简体渲到仓库根，
    英文渲到 en/。繁体不走这条路——它是 tw.py 对构建好的 HTML 做字形转换，
    那对翻译不成立（见 i18n.py 的说明）。

    抽的时候唯一的要求是简体输出**逐字节不变**，这一点由构建幂等那道闸门
    和重构前后的指纹对比一起兜住。
    """
    global BLURB, BASE, SITE, LANG, NAME, TAGLINE, LANG_ATTR
    LANG = lang
    out.mkdir(parents=True, exist_ok=True)
    i18n.LANG = lang
    NAME, TAGLINE = i18n.name(), i18n.tagline()   # 站名和口号跟着语言换
    LANG_ATTR = "en" if lang == "en" else "zh-CN"
    if lang != "zh":
        BASE = f"{BASE_ZH}/{lang}"
        SITE = f"{SITE_ZH}/{lang}"
    else:
        BASE, SITE = BASE_ZH, SITE_ZH
    BLURB = _blurb()
    eps, srcs = load()
    global _EN
    global _EN_SRC
    if lang == "en":
        _EN = en_store()
        f = DATA / "en" / "_sources.json"
        _EN_SRC = json.loads(f.read_text()).get("sources", {}) if f.exists() else {}
        # 没译文的不进英文站。宁可少几篇，也不要中英混排的页面。
        eps = [x for x in eps if x.get("slug") in _EN]
    log(f"building {len(eps)} episodes, {len(srcs.get('sources') or [])} sources")
    (out / "index.html").write_text(index_page(eps, srcs))
    (out / "sources").mkdir(exist_ok=True)
    (out / "sources" / "index.html").write_text(sources_page(srcs, eps))
    (out / "404.html").write_text(not_found())
    (out / "feed.xml").write_text(rss(eps))
    (out / "sitemap.xml").write_text(sitemap(eps))
    (out / "search.json").write_text(search_index(eps))
    n_pages = write_card_pages(eps, out)
    (out / "log").mkdir(exist_ok=True)
    (out / "log" / "index.html").write_text(log_page(eps, srcs))

    sdir = out / "s"
    by_src: dict[str, list[dict]] = {}
    for x in eps:
        by_src.setdefault(x["source_id"], []).append(x)
    live_src = set()
    for src in srcs["sources"]:
        rows = by_src.get(src["id"]) or []
        if not rows:
            continue
        # 变量名不能叫 out——那是本函数的输出根目录参数。第一版把 main() 里的
        # ROOT 机械替换成 out 时，这个循环把参数覆盖掉了，于是后面的 p/、e/、
        # robots.txt、llms.txt 全写进了**最后一个源**的目录下（仓库里留下了
        # s/tokcast/p/ 这种垃圾）。机械替换省下的时间，都赔在这一处上了。
        sout = sdir / src["id"]
        sout.mkdir(parents=True, exist_ok=True)
        (sout / "index.html").write_text(source_page(src, rows, None))
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
    (out / "robots.txt").write_text("\n".join(rb))
    (out / "llms.txt").write_text(llms_txt(eps, srcs))
    (out / "llms-full.txt").write_text(llms_full_txt(eps))
    (out / ".nojekyll").write_text("")

    pdir = out / "p"
    live = set()
    for i, x in enumerate(eps):
        prev = eps[i - 1] if i > 0 else None
        nxt = eps[i + 1] if i + 1 < len(eps) else None
        # 同上：第三处遮蔽。三处都是同一次机械替换（main() 里的 ROOT → out）
        # 留下的，而每一处的症状都不一样：源站页循环让 p/、e/、robots.txt 写进了
        # s/<最后一个源>/ 下；短链循环让 out 在循环后指向最后一集的目录。
        pout = pdir / x["slug"]
        pout.mkdir(parents=True, exist_ok=True)
        (pout / "index.html").write_text(episode_page(x, prev, nxt))
        live.add(x["slug"])
    if pdir.exists():                      # drop pages whose record is gone
        for d in pdir.iterdir():
            if d.is_dir() and d.name not in live:
                shutil.rmtree(d)
                log(f"  removed stale page /p/{d.name}/")

    # 分享短链：/e/<id>/ → /p/<中文 slug>/
    edir = out / "e"
    alive = set()
    for x in eps:
        # 同上：不许用 out 当循环变量，那是本函数的输出根目录。
        # 这是那次机械替换留下的**第二处**遮蔽（第一处是源站页循环）。
        eout = edir / x["id"]
        eout.mkdir(parents=True, exist_ok=True)
        (eout / "index.html").write_text(alias_page(x))
        alive.add(x["id"])
    if edir.exists():
        for d in edir.iterdir():
            if d.is_dir() and d.name not in alive:
                shutil.rmtree(d)
    # 繁体版：拿刚构建好的简体树整树转一遍。
    # 必须在这里、由 build.py 自己产出 —— CI 有两条判据是「跑一遍 build.py 后
    # git diff 必须干净」和「连续两次构建结果一致」，繁体站交给别的脚本生成的话
    # 这两条就管不到它，内容一改它就悄悄过期。
    if lang == "zh":
        import tw as _tw
        n_t, n_b = _tw.build(BASE)
        log(f"  繁体站 /tw/：文本 {n_t}，二进制 {n_b}")

    log(f"built: index, sources, {len(eps)} episode pages "
        f"(+{len(alive)} 分享短链), feed.xml, sitemap.xml")
    return 0


def main() -> int:
    render_site(ROOT, "zh")
    # 英文站默认**不建**：界面文案层还没做完（零漏译闸门会拦），而这个闸门要是
    # 挂在日常构建上，简体站的部署就一起被挡住了。用显式开关而不是静默跳过——
    # 静默跳过会让这件事被忘掉，体检那边也会一直报未完工的进度。
    n = len([f for f in (DATA / "en").glob("*.json")
             if not f.name.startswith("_")]) if (DATA / "en").exists() else 0
    if n:
        i18n.reset()
        render_site(ROOT / "en", "en")
        miss = i18n.missed()
        if miss:
            log(f"  ::error:: 英文站有 {len(miss)} 条界面文案没登记："
                f"{miss[:8]}")
            return 1
        # 零漏译闸门：汉字只许在 lang="zh" 里。这条不过就不该有英文站。
        import enscan
        leak = enscan.leaks(ROOT / "en")
        if leak:
            log(f"  ::error:: 英文站有 {len(leak)} 种中文漏在 lang=\"zh\" 之外，"
                f"最多的几种：{[k for k, _ in leak.most_common(5)]}")
            return 1
        n_en = len(list((ROOT / "en" / "p").iterdir())) if (ROOT / "en" / "p").exists() else 0
        log(f"  英文站 /en/：{n_en} 篇，零漏译")
    else:
        log("  英文站：data/en/ 还没有译文，跳过")
    # 语言切完了要把常量还原，免得同一个进程里后续调用拿到英文的 BASE
    render_site.__globals__["BASE"] = BASE_ZH
    render_site.__globals__["SITE"] = SITE_ZH
    i18n.LANG = "zh"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
