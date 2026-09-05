#!/usr/bin/env python3
"""三语站的文案层：简体（原文）、繁体（tw.py 机械转换）、英文（人工审过的对照表）。

为什么英文不能照繁体那套做：`tw.py` 是**对构建好的 HTML 做后处理**，走文件树把
汉字转成繁体——那对字形转换成立，对翻译不成立。所以英文走另一条路：
  · 正文内容来自 `data/en/<slug>.json`（translate.py 生成，金句用原话不回译）
  · 界面文案来自这张表，**一条一条人工审过**

这张表的完整性是**机械可查的**：`/en/` 输出里任何汉字都必须在 `lang="zh"` 的
元素里（节目名、中文源节目的金句原文）。漏一条界面文案，构建就失败——
不会交出一个中英混排的半成品。

`T()` 在简体模式下是恒等函数，所以给模板加 `T(...)` 不改变简体站的输出，
这一点由"连续两次构建结果一致"和守护测试一起兜住。
"""
from __future__ import annotations

import os

LANG = os.environ.get("PODCAST_LANG", "zh")      # zh | en

# 站名与口号
# 英文名叫 Podcast，不叫 OurWord —— OurWord 是主站（ourword.ai）的名字。
# 两个站都自称 OurWord 的话，页脚那行「OurWord · OurWord · 品味」读者
# 分不出哪个是哪个，而这一行的整个作用就是分清楚。
NAMES = {"zh": "原声", "en": "Podcast"}
TAGLINES = {
    "zh": "世界太吵，来原声听播客",
    # 52 字符在 375px 上会折成两行。用户："移动端 slogn 换行了，简化下写法"。
    # 中文原句是"世界太吵，来原声听播客"——短句 + 一个立场。英文照这个结构，
    # 41 字符，320px 上也只有一行。
    "en": "The world is too loud. Read what matters.",
}

# 界面文案对照表。键是简体原文，值是英文。
# 只放**会进页面**的文案；llms.txt 那些给模型读的段落本来就是双语，不进这里。
UI: dict[str, str] = {
    # —— 导航与全局 ——
    "首页": "Home",
    "信源": "Shows",
    "分享": "Share",
    "分类": "Topics",
    # 「最新」取代了「全部」：默认只看最近七天。
    "最新": "Latest",
    "以上是最近七天。想看更早的，点上面的分类。":
        "That's the last seven days. For anything older, pick a category above.",
    "全部": "All",
    "更新日志": "Changelog",
    "切换到简体": "简体",
    "切換到繁體": "繁體",
    "切换到英文": "English",
    "找不到这一页": "Page not found",
    # —— 分类 ——
    "AI / 技术": "AI & Tech",
    "投资 / 商业": "Investing & Business",
    "中国视角": "China",
    "人文 / 思想": "Ideas",
    "历史": "History",
    "育儿": "Parenting",
    "中文": "Chinese",
    # —— 正文小标题 ——
    "核心论点": "The argument",
    "原话": "In their own words",
    "已逐字校验": "verbatim, checked",
    "数字与实体": "Figures",
    "术语": "Glossary",
    "收听指南": "How to listen",
    "谁该听": "Who it's for",
    "可跳过": "Skip",
    "原节目": "The episode",
    "本篇结构": "On this page",
    "这篇是怎么来的": "How this was made",
    # —— 播放器与时间戳 ——
    "点时间戳可跳到原声": "tap a timestamp to hear it",
    "时间戳为按文稿位置估算": "timestamps estimated from transcript position",
    "时间戳会跳到原节目对应位置。": "Timestamps open the original episode at that point.",
    "播放": "Play",
    "暂停": "Pause",
    "播放进度": "Progress",
    "播放原节目视频": "Play the original video",
    "原声音频": "Audio",
    "点播放才向 YouTube 请求": "Nothing is sent to YouTube until you press play",
    "作者已关闭站外播放 · 去 YouTube ↗": "Embedding disabled by the uploader · open on YouTube ↗",
    "原视频在 YouTube 上放不出来，用音频听：":
        "The video won't play here. Listen to the audio instead:",
    "加载 YouTube 播放器失败。": "Could not load the YouTube player.",
    # —— 来源面板 ——
    "节目": "Show",
    "原标题": "Original title",
    "发布": "Published",
    "时长": "Length",
    "原页面": "Episode page",
    "打开 ↗": "Open ↗",
    "成稿评分": "Editorial score",
    "文稿来源": "Transcript",
    "文稿字数": "Transcript words",
    "语速核验": "Words per minute",
    "逐字校验金句": "Quotes checked verbatim",
    "回溯校验数字": "Figures checked against source",
    "质检剔除": "Dropped in QA",
    # —— 文稿来源标签 ——
    "官方全文": "Official full text",
    "官方文稿页": "Official transcript page",
    "官方逐字稿": "Official transcript",
    "自带官方逐字稿": "Ships an official transcript",
    "自带": "included",
    "YouTube 字幕": "YouTube captions",
    "YouTube 源": "YouTube",
    "音频转写": "Machine transcription",
    # —— 列表与分页 ——
    "篇深读": "deep reads",
    "深读": "deep reads",
    "加载更多": "Load more",
    "正在载入…": "Loading…",
    "加载失败，点一下重试": "Failed to load — tap to retry",
    "没有匹配的深读": "No matching deep reads",
    "搜正文、金句、数字、术语、节目…":
        "Search points, quotes, figures, glossary, shows…",
    "更早 →": "Older →",
    "← 更新": "← Newer",
    "本站已深读": "Deep reads here",
    "本站尚未收录": "Not covered yet",
    "最新一集": "Latest episode",
    "更新节奏": "Cadence",
    "优先级": "Priority",
    "收录": "added",
    "移除": "removed",
    "降级": "downgraded",
    "休眠": "dormant",
    "抓取异常": "fetch failing",
    "试用": "on trial",
    "还没有信源变动": "No show changes yet",
    '世界太吵，来原声听播客': 'The world is too loud. Read the podcasts that matter.',
    '要点': 'Points',
    '金句': 'Quotes',
    '源码': 'Source',
    '原话 · 已逐字校验': 'In their own words · checked verbatim',
    '核心论点 · 点时间戳可跳到原声': 'The argument · tap a timestamp to hear it',
    '核心论点 · 时间戳为按文稿位置估算': 'The argument · timestamps estimated from transcript position',
    '金句在逐字稿里逐字比对过，数字回原文核对过；对不上的当场删掉，不上站。': 'Every quote is matched word for word against the transcript and every figure is checked back to the source. Anything that cannot be found is cut, not published.',
    '成稿另经一道独立评审（信息密度／忠实度／选择力／具体性／中文），低于 7 分不展示。': 'A separate model then scores the finished piece on density, fidelity, selection and specificity. Below 7 out of 10 it is not shown.',
    '这一集的文稿没有原始时间码，页面上的时间戳是按文稿位置估算的，只作粗略定位。': "This episode's transcript carries no original timecodes, so the timestamps here are estimated from position in the transcript and are approximate.",
    '本站尚未收录': 'Not covered yet',
    '本站': 'Here',
    '旁白': 'narrator',
    '主播': 'host',
    '找不到这一页': 'Page not found',
    '回到首页': 'Back to home',
    '这一页不在了，或者从来没有过。': 'This page is gone, or never existed.',
    '信源清单': 'Shows',
    '全部信源': 'All shows',
    '深读': 'deep reads',
    '篇': '',
    '集': '',
    '条': '',
    '处': '',
    '分': '',
    '天': '',
    '搜正文、金句、数字、术语、节目…': 'Search points, quotes, figures, glossary, shows…',
    '搜索': 'Search',
    '继续加载': 'Load more',
    '换个词，或者清掉筛选再试。搜索会搜进每条要点的正文、金句的中英文原文、数字和术语表——不只是标题。': 'Try another word, or clear the filters. Search covers the body of every point, quotes in both languages, the figures and the glossary — not just titles.',
    '没有 JavaScript 时只显示最新 N 篇，完整清单见 sitemap 或 llms.txt。': 'Without JavaScript only the latest N are shown; the full list is in the sitemap or llms.txt.',
    '这一页不在了': 'This page is gone',
    '切换深浅色': 'Toggle light/dark',
    '的播客线。内容为原播客的中文深读，': " — the podcast desk. Deep reads of other people's podcasts;",
    '版权归各节目所有；每篇都附原节目链接，请去支持原作者。': 'copyright stays with each show. Every piece links to the original — please go support them.',
    'SOURCES_DESC': 'The full list of the {n} Chinese- and English-language podcasts NAME tracks, with fetch health.',
    '这档还没有深读': 'No deep reads from this show yet',
    '取不到可核对的文稿时不会发，等文稿到位再上。': 'Nothing goes up without a transcript we can check against. It waits until one exists.',
    '正在打开': 'Opening ',
    '最新一集': 'Latest',
    '科学 / 医学': 'Science & Medicine',
    'BLURB_HEAD': 'Every day, the judgements worth remembering from {n} Chinese- and English-language podcasts. ',
    'BLURB_HEAD_NONE': 'Every day, the judgements worth remembering from the podcasts worth reading. ',
    'BLURB_TAIL': 'Points and quotes carry timestamps — tap one and you are back at the second it was said. Quotes are checked word for word and figures back to the source; anything unverifiable is cut, not published.',
    'LOG_LEDE_1': 'The show list is re-checked every three days, on measured data rather than impressions: whether the feed still resolves, how many days since the last episode, the triage pass rate, the median editorial score. New candidates are mined from recent episodes (shows that get mentioned, guests who recur), scored after actually testing whether a transcript is obtainable, and only taken at 8+.',
    'LOG_LEDE_2A': 'Anything marked ',
    'LOG_LEDE_2B': " is newly added: that score comes from the title, the show notes and a transcript sample — no finished piece has been through all four gates yet. After a run, the ones that produce get promoted and the ones that don't get removed. Both outcomes are recorded below.",
    'LOG_LEDE_3': 'An aggregator that quietly swaps out its sources quietly swaps out its taste, so every change is on the record here. Full list on the ',
    '信源页': 'shows page',
    'SRC_LEDE_1': "Built on Apple Podcasts' official RSS rather than only scraping YouTube channels — otherwise audio-only shows (Acquired, Odd Lots, Invest Like the Best) and Chinese-language podcasts would be missing wholesale. Feed URLs are not hard-coded: when a show changes host, the address is re-resolved from Apple's directory, so it does not silently go stale.",
    'SRC_LEDE_2': 'T1 means read every episode; T2 when there is real substance; T3 only for an unusually strong episode.',
    "分享本站": "Share this site",
    "复制分享文本": "Copy share text",
    "正在打开": "Opening",
    "点这里": "tap here",
}


# 带数字的组合串：不能靠查表，得参数化。
# 中文里量词紧贴数字（"7 条"），英文要单复数（"7 quotes" / "1 quote"），
# 这类必须写成函数，硬塞进查表只会得到 "7 条" 这种半成品。
_PLURALS = {
    "quote": ("quote", "quotes"),
    "figure": ("figure", "figures"),
    # 用户："去掉 deep"。"215 deep reads" 在 375px 上把字标挤到了第二行，
    # 而 deep 也没带来信息——它是中文"深读"的直译。
    "read": ("read", "reads"),
    "episode": ("episode", "episodes"),
    "point": ("point", "points"),
    "dropped": ("dropped", "dropped"),
    "show": ("show", "shows"),
}


def n(count, kind: str, zh_unit: str = "条") -> str:
    """N 条 / N quotes。zh_unit 只在简体下用。"""
    if LANG == "zh":
        return f"{count} {zh_unit}"
    one, many = _PLURALS.get(kind, (kind, kind + "s"))
    return f"{count} {one if count == 1 else many}"


def score(v) -> str:
    return f"{v} 分" if LANG == "zh" else f"{v} / 10"


def cadence(days) -> str:
    """约 7.0 天一集 / roughly one every 7.0 days"""
    if LANG == "zh":
        return f"约 {days} 天一集"
    return f"~1 every {days} days"


def covered(here, total) -> str:
    """1 / 15 集"""
    if LANG == "zh":
        return f"{here} / {total} 集"
    return f"{here} of {total} episodes"


def name() -> str:
    return NAMES.get(LANG, NAMES["zh"])


def tagline() -> str:
    return TAGLINES.get(LANG, TAGLINES["zh"])


class Missing(KeyError):
    """英文模式下遇到没登记的文案。**故意抛异常而不是回落到中文**——
    回落会让漏译静默变成中英混排，那正是要防的东西。"""


_missed: set[str] = set()


# 长段落用人造键（LOG_LEDE_1 这种），因为原文太长、不适合当字典键。
# **人造键必须在这里给出中文原文**，否则简体模式下 T() 的恒等行为会把键名
# 直接印到页面上——线上真出过：日志页显示 "LOG_LEDE_1"，首页显示
# "BLURB_HEADBLURB_TAIL"。是同伴写的一条守护测试抓到的。
ZH: dict[str, str] = {
    "BLURB_HEAD": "每天从 {n} 档中英文播客里挑出值得记住的判断。",
    "BLURB_HEAD_NONE": "每天从中英文播客里挑出值得记住的判断。",
    "BLURB_TAIL": "要点和金句都带时间戳，点一下就回到它在原声里被说出的那一秒；"
                  "金句逐字校验过、数字回原文核对过——查不到出处的，一律不上站。",
    "SOURCES_DESC": "NAME 目前追踪 {n} 档中英文播客的完整信源清单与抓取健康度。",
    "SRC_LEDE_1": "以 Apple Podcasts 官方 RSS 为主干，而不是只抓 YouTube 频道"
                  "——这样纯音频节目（Acquired、Odd Lots、Invest Like the Best）"
                  "和中文播客才不会整块缺失。feed 地址不写死在代码里，节目换托管商时"
                  "会自动从 Apple 目录重新解析，所以不会悄悄断更。",
    "SRC_LEDE_2": "T1 表示每集必读，T2 有实质内容时收，T3 只在特别强的一集时收。",
    "LOG_LEDE_1": "信源清单每三天自动复查一次。判据全部来自实测数据，不靠印象："
                  "feed 是否失效、停更多少天、选题闸门的通过率、成稿评分的中位数。"
                  "同时从近期发布的内容里挖新线索（被提到的其他节目、反复出现的"
                  "受访者），实测文稿可得性后打分，只收 8 分以上。",
    "LOG_LEDE_2A": "标着",
    "LOG_LEDE_2B": "的是刚收的：那个分数照着标题、分集说明和文稿抽样打的，"
                   "还没有任何一篇成稿走完四道闸门。跑一段之后，出得来内容的提级，"
                   "出不来的移除，两种结果都会记在下面。",
    "LOG_LEDE_3": "一个聚合站悄悄换掉信源，等于悄悄换掉它的口味，"
                  "所以每一次改动都记在这里。完整清单见",
}


def T(zh: str) -> str:
    """界面文案。

    简体模式下：键就是原文时是恒等函数；人造键（长段落）从 ZH 表取中文。
    """
    if LANG == "zh":
        return ZH.get(zh, zh)
    v = UI.get(zh)
    if v is None:
        _missed.add(zh)
        # 不抛异常：构建要能跑完，好让漏的一次全列出来，而不是修一条撞一条。
        # 构建结束时 missed() 非空就让整次构建失败。
        return zh
    return v


def unresolved_zh() -> list[str]:
    """简体模式下会把键名直接印出去的人造键。

    判据：键本身不含汉字（说明是人造键），却没有在 ZH 表里给中文原文。
    这条是补上一次真实故障的——日志页曾经显示 "LOG_LEDE_1"。
    """
    import re as _re
    bad = []
    for k in UI:
        if not _re.search(r"[一-鿿]", k) and k not in ZH:
            bad.append(k)
    return sorted(bad)


def missed() -> list[str]:
    return sorted(_missed)


def reset() -> None:
    _missed.clear()
