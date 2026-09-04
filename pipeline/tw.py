#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""繁体版：把构建好的简体站整树转到 /podcast/tw/。

由 build.py 在最后调用 —— 必须由 build.py 自己产出，不能是另一个脚本，
因为 CI 有两条判据是「跑一遍 build.py 后 git diff 必须干净」和「连续两次构建
结果一致」。繁体站如果由别的脚本生成，这两条就管不到它，内容一改它就悄悄过期。

三条和主站同样的规矩：

① **URL 一个字都不能动。** 这个站比主站更要紧 —— 256 个节目页的目录名
   本身就是中文（`p/2026-08-30-asianometry-别只盯着光刻机…`）。转换前先把
   所有 href/src 以及任何以 http、/、#、% 开头的属性值挖出来占位，转完再放回。

② **占位符不能用 \\x00。** OpenCC 底层是 C 字符串，遇 \\x00 直接截断，
   转出来的页面里 URL 会被正文填满，而页面照样渲染。用私用区字符。

③ **只对简体源转一次。** 不对 tw/ 再转。

用 s2tw 不用 s2twp：后者会替换词汇（信息→資訊、对象→物件、支持→支援），
这个站的正文是播客摘要，混着技术词和日常叙述，词汇替换误伤面比收益大。
"""
import os
import re
import shutil

import opencc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tw")
ALLOW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tw_allow.txt")

_cc = opencc.OpenCC("s2tw")

# 一简多繁里两边都讲得通、必须逐处确认的字
AMBIGUOUS = "髮隻餘覆複麵乾鬆檯曆製彆繫捲穀僕醜範衝徵"

# ── 修正表：OpenCC 转错、逐条在真实语料里核对过 ──────────────────
FIX = [
    # 副词「只」被当成量词「隻」。量词的用法（一隻手/每隻眼/船隻/艦隻）不动。
    ("是隻", "是只"), ("別隻", "別只"), ("多隻有", "多只有"), ("多隻能", "多只能"),
    ("艦隻能", "艦只能"), ("艦隻有", "艦只有"), ("萬隻是", "萬只是"),
    ("億隻是", "億只是"), ("萬隻剩", "萬只剩"), ("那隻會把", "那只會把"),
    # 「没有只靠…」= 不是仅仅依靠，副词。OpenCC 切成了量词 隻。
    ("有隻靠", "有只靠"), ("是隻靠", "是只靠"), ("隻靠", "只靠"),
    # 「发」被当成「髮」（头发）。理髮/頭髮/美髮/假髮/毫髮/長髮 不动。
    ("人髮指", "人發指"),      # 令人发指
    ("被髮明", "被發明"), ("被髮配", "被發配"),
    ("斷髮新", "斷發新"), ("換髮新", "換發新"),   # 不断发新券 / 换发新券
    ("秋髮起", "秋發起"),
    # 「用水冲」是冲洗，繁体作 沖；衝 是碰撞、冲突。OpenCC 认得「沖洗」
    # （在它词典里），认不得「冲多久」「冲一下」这种，于是切成了 衝。
    # 同一页里「常規沖洗」是对的、「拿水衝多久」是错的 —— 差别只在词典收没收。
    ("水衝", "水沖"), ("衝多久", "沖多久"), ("衝一下", "沖一下"),
    ("衝乾淨", "沖乾淨"), ("衝掉", "沖掉"), ("衝走", "沖走"),
    # 「发卡行」是发行银行卡，不是头发的卡子（那个在台湾叫髮夾）。
    # 这个站聊支付，「发卡」出现得很密，整词替换比逐个前缀安全。
    ("髮卡", "發卡"),
    # 「这台笔记本」的量词是臺，不是柜台的檯。
    ("檯筆記本", "臺筆記本"),
    # 「历史」被切成「历|史」
    ("年曆史", "年歷史"), ("國曆史", "國歷史"), ("長曆史", "長歷史"),
    # 「面」不是「麵」。意大利麵 / 麵包 / 麵條化 是对的，不动。
    ("洋麵上", "洋面上"), ("麵部", "面部"),
    # 跨词边界切错的两处，靠 NEVER 表扫出来的：
    ("心繫統", "心系統"),    # 核心|系统 被切成 核|心系|统
    ("調製度", "調制度"),    # 强调|制度 被切成 强|调制|度
]

# 繁体里**绝不该出现**的组合。它们全都只可能来自分词切错 ——
# 这一条是系统性的：逐字审只能看见你想到要看的字，这张表能把
# 「核心系统→核心繫統」「强调制度→強調製度」这种一次扫出来。
# 只放高置信度的：像「隻看」「隻用」这种量词后面跟动词的情况是合法的
# （「一隻用統計學思考的火雞」），放进来会变成噪音。
NEVER = (
    "繫統 髮生 髮明 髮現 髮展 髮布 髮起 髮動 髮配 髮送 髮表 髮言 髮揮 髮達 髮電 髮射 髮行 "
    "隻能 隻有 隻是 隻要 隻好 隻剩 "
    "麵對 麵臨 麵子 麵前 麵積 麵板 麵貌 麵向 麵試 "
    "乾部 乾預 乾擾 乾涉 曆史 曆程 曆來 徵服 徵途 徵戰 "
    "製度 體製 機製 控製 專製 限製 抑製 強製 調製度 "
    "鬆樹 穀歌 試捲 問捲 僕後 檯風 覆雜 覆習 姓範 彆的 彆人 醜時"
).split()

# 用字风格：台湾教育部把「佈」并入「布」
POST = [("佈", "布")]

FONT = [("Noto+Serif+SC", "Noto+Serif+TC"), ("Noto Serif SC", "Noto Serif TC")]

# 语言标记：繁体页必须自报繁体，否则搜索引擎和分享卡片都按简体归类。
# 这几处转换转不到（它们是标记不是正文），只能显式替换。
LOCALE = [
    ('property="og:locale" content="zh_CN"', 'property="og:locale" content="zh_TW"'),
    ('"inLanguage": "zh-CN"', '"inLanguage": "zh-TW"'),
    ('"inLanguage":"zh-CN"', '"inLanguage":"zh-TW"'),
    ("<language>zh-cn</language>", "<language>zh-tw</language>"),
    ("<language>zh-CN</language>", "<language>zh-TW</language>"),
]

URLISH = re.compile(r"^(https?:|//|/|#|\.\.?/|mailto:|data:)")
ATTR = re.compile(r'\b(href|src|action|srcset|content|url)\s*=\s*"([^"]*)"')

# 注意 sources/ **不在**这里：它虽然名字像数据目录，但 build.py 往
# sources/index.html 写的是一个真页面。第一版跳过了它，繁体站少一页 ——
# 而结构判据用的是同一份跳过表，两边一起漏，它看不见自己漏了什么。
# 所以下面 test_tw 里另有一条独立的「顶层该有哪些东西」断言，不依赖这张表。
SKIP_DIRS = {".git", ".github", "pipeline", "scripts", "tests", "data",
             "node_modules", "__pycache__", "tw"}
SKIP_FILES = {"robots.txt", "CNAME", "LICENSE"}
TEXT_EXT = {".html", ".js", ".json", ".txt", ".xml", ".css", ".svg"}
BIN_EXT = {".png", ".jpg", ".jpeg", ".ico", ".webp", ".gif", ".woff", ".woff2"}
# 这些文件里的地址在元素文本和裸行里，不是属性，得整体替换
URLFILE = {"sitemap.xml", "feed.xml", "llms.txt", "llms-full.txt", "search.json"}


def convert(text):
    if not text:
        return text
    out = _cc.convert(text)
    for a, b in FIX:
        out = out.replace(a, b)
    for a, b in POST:
        out = out.replace(a, b)
    return out


def contexts(text):
    """歧义字的三字上下文。三字而不是二字：分词错会造出合法的二字词
    （「明白髮生」里的「白髮」是正经词），二字白名单放它过就抓不到。"""
    out, n = [], len(text)
    for i, ch in enumerate(text):
        if ch in AMBIGUOUS:
            out.append((text[i - 1] if i else "　") + ch + (text[i + 1] if i + 1 < n else "　"))
    return out


def load_allow():
    if not os.path.exists(ALLOW):
        return set()
    return {l.rstrip("\n") for l in open(ALLOW, encoding="utf-8")
            if l.strip() and not l.startswith("#")}


def _protect(s):
    keep = []

    def stash(v):
        keep.append(v)
        return "%d" % (len(keep) - 1)

    def attr(m):
        name, val = m.group(1), m.group(2)
        if URLISH.match(val) or "%" in val:
            return '%s="%s"' % (name, stash(val))
        return m.group(0)

    return ATTR.sub(attr, s), keep


# 同域下的另外两个站。它们各归独立仓库，繁体版在各自站内：
#   主站繁体 = /tw/          品味繁体 = /skill/tw/
# 原来 _retarget 只改自己 base 底下的路径，跨站链接一律不动 —— 于是繁体
# 原声页上的「人类世界生存法则」指向**简体**主站。读者切了一次语言，
# 一点页脚就掉回简体，还得再切一次。
SISTER = {"/": "/tw/", "/skill/": "/skill/tw/"}


def _sister(val):
    """跨站地址：返回该指向的繁体地址；不是跨站就返回 None。"""
    for a, b in sorted(SISTER.items(), key=lambda kv: -len(kv[0])):
        if val == a:
            return b
        if a != "/" and val.startswith(a) and not val.startswith(b):
            return b + val[len(a):]
    return None


def _retarget(s, base):
    def one(m):
        name, val = m.group(1), m.group(2)
        bare = (val.replace("https://ourword.ai", "", 1)
                if val.startswith("https://ourword.ai") else val)
        if not bare.startswith(base + "/") and bare != base:
            sis = _sister(bare)
            if sis is not None:
                return '%s="%s"' % (name, val.replace(bare, sis, 1))
        if val.startswith(base + "/") and not val.startswith(base + "/tw/"):
            val = base + "/tw" + val[len(base):]
        elif val.startswith("https://ourword.ai" + base + "/") and "/tw/" not in val:
            val = val.replace("https://ourword.ai" + base + "/",
                              "https://ourword.ai" + base + "/tw/", 1)
        return '%s="%s"' % (name, val)

    holes = []
    s = re.sub(r'<link rel="alternate" hreflang[^>]*>',
               lambda m: holes.append(m.group(0)) or "%d" % (len(holes) - 1), s)
    s = ATTR.sub(lambda m: one(m) if (URLISH.match(m.group(2)) or "%" in m.group(2))
                 else m.group(0), s)
    return re.sub("(\\d+)", lambda m: holes[int(m.group(1))], s)


def build(base="/podcast"):
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    n_t = n_b = 0
    for dp, dn, fn in os.walk(ROOT):
        rel = os.path.relpath(dp, ROOT)
        if set(rel.split(os.sep)) & SKIP_DIRS:
            dn[:] = []
            continue
        dn[:] = [d for d in dn if d not in SKIP_DIRS]
        for f in fn:
            if f in SKIP_FILES or f.startswith("."):
                continue
            ext = os.path.splitext(f)[1].lower()
            src = os.path.join(dp, f)
            dst = os.path.join(OUT, os.path.relpath(src, ROOT))
            if ext in TEXT_EXT:
                s = open(src, encoding="utf-8").read()
                if f.endswith(".html"):
                    s = re.sub(r'<html([^>]*)\blang="[^"]*"', r'<html\1lang="zh-Hant"', s, count=1)
                    if 'lang="zh-Hant"' not in s:
                        s = re.sub(r"<html\b", '<html lang="zh-Hant"', s, count=1)
                kept, keep = _protect(s)
                kept = convert(kept)
                s = re.sub("(\\d+)", lambda m: keep[int(m.group(1))], kept)
                s = _retarget(s, base)
                if f in URLFILE:
                    s = s.replace("https://ourword.ai" + base + "/",
                                  "https://ourword.ai" + base + "/tw/")
                    s = s.replace(base + "/tw/tw/", base + "/tw/")
                for a, b in FONT:
                    s = s.replace(a, b)
                for a, b in LOCALE:
                    s = s.replace(a, b)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                open(dst, "w", encoding="utf-8").write(s)
                n_t += 1
            elif ext in BIN_EXT:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                n_b += 1
    return n_t, n_b
