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
NAMES = {"zh": "原声", "en": "OurWord"}
TAGLINES = {
    "zh": "世界太吵，来原声听播客",
    "en": "The world is too loud. Read the podcasts that matter.",
}

# 界面文案对照表。键是简体原文，值是英文。
# 只放**会进页面**的文案；llms.txt 那些给模型读的段落本来就是双语，不进这里。
UI: dict[str, str] = {
    # —— 导航与全局 ——
    "首页": "Home",
    "信源": "Shows",
    "分享": "Share",
    "分类": "Topics",
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
    "分享本站": "Share this site",
    "复制分享文本": "Copy share text",
    "正在打开": "Opening",
    "点这里": "tap here",
}


def name() -> str:
    return NAMES.get(LANG, NAMES["zh"])


def tagline() -> str:
    return TAGLINES.get(LANG, TAGLINES["zh"])


class Missing(KeyError):
    """英文模式下遇到没登记的文案。**故意抛异常而不是回落到中文**——
    回落会让漏译静默变成中英混排，那正是要防的东西。"""


_missed: set[str] = set()


def T(zh: str) -> str:
    """界面文案。简体模式是恒等函数。"""
    if LANG == "zh":
        return zh
    v = UI.get(zh)
    if v is None:
        _missed.add(zh)
        # 不抛异常：构建要能跑完，好让漏的一次全列出来，而不是修一条撞一条。
        # 构建结束时 missed() 非空就让整次构建失败。
        return zh
    return v


def missed() -> list[str]:
    return sorted(_missed)


def reset() -> None:
    _missed.clear()
