"""选题闸门：在花钱做深读之前，先判断这一集值不值得做。

为什么需要这一层：原来的打分（run.py:score）只看信源等级、新鲜度、有没有文稿，
不看这一集本身讲什么。于是往回抓存量时，Odd Lots 讲沙丁鱼罐头装罐流程的那集
和讲钨市场预示战争的那集是同等待遇——前者抓过来就是凑数。

成本对比：triage 只喂标题 + show notes（约 600 token 进、100 出），深读要喂整份
逐字稿（1-5 万 token）。所以先筛一遍在经济上是压倒性的：把 90% 的不合格集挡在
模型的大开销之前。
"""
from __future__ import annotations

import threading
import json
import os
import re

from . import llm
from .util import hhmmss, log, squeeze, strip_html

MIN_SCORE = float(os.environ.get("TRIAGE_MIN", "7"))
NOTES_CHARS = 1800

SYSTEM = """你在给一个中文播客深读站做选题。站点有多分类，**用该集所属分类的尺子**，
不要用「科技/商业」一票否决其他分类的合格内容。

共同读者：认真、要可核对信息的中文读者（含创业者、投资人、工程师，也含关心历史、
思想、育儿、科学的人）。任务不是看话题热不热，而是判断读完手里会多出什么。

**所有分类共用的高分条件（必须满足其一）：**
- 事实与机制：数字、时间线、成本、实验、制度如何运作
- 判断与框架：有立场、能被反驳的论断，讲清取舍与边界
- 一手经历：亲历者讲自己做过的事——具体到步骤、代价、失败在哪、当时怎么
  取舍、后来怎么改。这一条**不要求可复现的推导链**，要求具体、可追问、
  不是二手转述。「我们团队原来这样干、现在这样干、为什么换」是高分；
  「要拥抱 AI」是低分。**失败与踩坑是加分，不是「不够系统」。**
- 一手事实：别处拿不到的事实——文件、数据、当事人访谈、现场。
  调查报道属于这一条：判断可以少，事实要具体、可核、有出处。

分界线不是「有没有数字」，也不是「有没有推导链」，而是
**读完手里是不是多出了别处拿不到的东西**。
套话（「要有耐心」「保持学习」）低分。

**按分类加分轴（分类对了就用这条，不要再拿商业框架苛责）：**
- ai / biz / cn：产业机制、监管、供应链、投资决策、一手创业经验、中美对照
- ideas：论证链、可争辩哲学/社科结论、思想史中的明确命题
- hist：因果与时间线、制度/文明机制、可回史料或一手记述的判断
- parent：可检验的教养原则、发展心理学证据、边界条件（何种孩子/年龄有效）
- sci：机制、实验或论文可回、对流行说法的限定条件（不是养生口播）
- edu（AI 课程）：**用讲课的尺子量，不要拿产业机制苛责**。加分的是
  推导链完整、把一个概念讲到能动手复现、给出前提与失效边界、
  指出常见误解错在哪。一堂讲透注意力机制怎么算的课是高分，
  哪怕它不含任何行业数字或可争辩的立场。
  但这一条**不放宽宣传**：课程预告片、报名页口播、产品演示仍然低分——
  分界是「听完能不能自己复现这一步」，不是「说的是不是技术」。

**所有分类都给低分（0-4），这条不放宽：**
- 新闻综述、一周回顾、榜单
- 纯宣传、广告口播、课程推销、课程预告片、报名页口播、产品演示
- 趣味闲聊、景点打卡、纯鸡汤、无法证伪的正确话

**低分的理由只能是「空泛」，不能是「体裁」。** 下面这些都**不是**减分理由：
「偏经验分享」「属调查报道」「多为即兴讨论」「缺乏完整推导链」
「不够系统」——只要它给了具体的一手经历或一手事实，就按上面那两条给分。
真正要压的是：讲了半小时而读者手里什么都没多出来。

不要因为「与科技商业无关」就否掉 hist / parent / sci / ideas 的合格集。
中间分（5-6）：有内容但密度不高。7 分及以上才做深读。

判断依据：**给了字幕原文就以字幕为准**，节目介绍只作参考——YouTube 的
简介基本是赞助与订阅链接，**简介空洞在这种情况下不构成任何减分理由**。
只有节目介绍、没有字幕时，介绍空洞本身才是信号。"""

SCHEMA = """输出 JSON：{"score": 0-10 的数字, "why": "不超过 40 字的中文理由",
"kind": "一手访谈|机制拆解|新闻综述|宣传|闲聊|其他"}"""


# YouTube 频道的"节目介绍"就是视频简介，而那基本是赞助和订阅链接。
# 实测：Isaac Arthur 那一集被判 2/10「介绍全是订阅链接，无实质内容」，
# 而它的字幕有几千词、内容扎实。**闸门跑在取稿之前**（那是它省钱的理由），
# 所以它只看得到广告文案，YouTube 原生频道因此普遍判低——
# 一批 32 个频道里 Kings and Generals、Sabine Hossenfelder、RealLifeLore
# 全是 4.0，判词都是"二手转述、无一手"。
#
# 字幕是**免费**的（yt-dlp，不花模型钱），所以对 kind=youtube 的源先取一段
# 字幕样本喂给闸门。判据不变，只是把输入从广告换成真内容。
# 取样必须**铺开取**，不能只取开头。实测：Anthropic 官方那条 22 分钟圆桌
# 《How the Claude Code team uses Claude Code》，只取开头 2400 字判 4.0
# （开场全是"你明年打算干什么"这种寒暄），同样长度改成三段铺开就判 6.0
# ——判词从"缺乏可复现机制"变成"有具体工作流细节与判断"。
# 访谈和圆桌的开场是寒暄，只看开头会**系统性低估**这一类，而这一类正是
# YouTube 这条线的主力。
CAPTION_SAMPLE = 2400
# 铺开取 12 段，不是 3 段。curate.py 的 _excerpt 早就是这么做的
# （`step = len(segs) // 20`，铺开取 20 段）—— 同样的总长度，段越多越有
# 代表性：3 段容易整段落在某个跑题的地方，12 段不会。
CAPTION_WINDOWS = tuple(round(0.04 + i * 0.08, 2) for i in range(12))


# 一集只取一次字幕。score() 要判 basis、_brief() 要拿样本，各调一次就等于
# 把 yt-dlp 的请求翻倍 —— 而 429 正是请求太多造成的，那是在给自己制造限流。
_SAMPLE_CACHE: dict[str, str] = {}
_SAMPLE_LOCK = threading.Lock()


def _caption_sample(ep: dict, src: dict) -> str:
    vid0 = ep.get("youtube_id") or (ep.get("guid") or "").split(":")[-1]
    if vid0:
        with _SAMPLE_LOCK:
            if vid0 in _SAMPLE_CACHE:
                return _SAMPLE_CACHE[vid0]
        out = _caption_sample_uncached(ep, src)
        with _SAMPLE_LOCK:
            _SAMPLE_CACHE[vid0] = out
        return out
    return _caption_sample_uncached(ep, src)


def _caption_sample_uncached(ep: dict, src: dict) -> str:
    if (src.get("kind") or "") != "youtube":
        return ""
    vid = ep.get("youtube_id") or (ep.get("guid") or "").split(":")[-1]
    if not vid or not vid.startswith(("UC", "-", "_")) and len(vid) != 11:
        # YouTube 的 videoId 是 11 位；guid 形如 yt:video:<id>
        if len(vid) != 11:
            return ""
    try:
        from lib import transcript as T
        tr = T.from_youtube(vid, src.get("lang") or "en")
    except Exception:
        return ""
    if not tr or not tr.get("segments"):
        return ""
    text = squeeze(" ".join((x.get("text") or "") for x in tr["segments"]))
    if len(text) <= CAPTION_SAMPLE:
        return text
    w = CAPTION_SAMPLE // len(CAPTION_WINDOWS)
    n = len(text)
    return " …… ".join(text[int(n * f):int(n * f) + w] for f in CAPTION_WINDOWS)


def _brief(ep: dict, src: dict) -> str:
    notes = strip_html(ep.get("notes"))
    # 章节表比宣传语更能说明这一集讲了什么
    notes = squeeze(notes)[:NOTES_CHARS]
    parts = [f"分类：{src.get('cat','ai')}",
             f"节目：{src.get('zh') or src['name']}（{src.get('desc', '')}）",
             f"这一集标题：{ep['title']}"]
    if ep.get("duration"):
        parts.append(f"时长：{hhmmss(ep['duration'])}")
    cap = _caption_sample(ep, src)
    if cap:
        # 有字幕样本时**以它为准**：视频简介是广告，字幕是内容本身。
        parts.append(f"这一集的开头（字幕原文，判断请以它为准）：\n{cap}")
        if notes:
            parts.append(f"视频简介（多为赞助与订阅链接，仅作参考）：\n{notes[:400]}")
    else:
        parts.append(f"节目介绍：\n{notes or '（没有介绍）'}")
    return "\n".join(parts)


def rubric_id() -> str:
    """当前这把尺子的指纹。

    判决要连着**判它的那把尺子**一起存。不存的话，尺子一改，账本里就躺着
    一堆用已经不存在的标准判出来的**永久**结论，而没有任何东西会发现 ——
    实测清点：224 条「不做」里 182 条是旧尺子判的，其中 66 条 6 分、
    69 条 4 分，判词还在用新尺子明令禁止的体裁理由（「属调查报道」）。
    这已经是第三次同形状的事故（按简介判、按更严的线判、按旧尺子判），
    所以这次做成机制：指纹不一致 = 这条判决不算数。
    """
    import hashlib
    return hashlib.sha1(SYSTEM.encode("utf-8")).hexdigest()[:10]


def score(ep: dict, src: dict) -> dict | None:
    """返回 {"score", "why", "kind", "basis"}；模型不可用时返回 None。

    **basis 是给调用方看的：这一分是按什么判出来的。**
    kind=youtube 的源，取到字幕就是 "captions"，取不到就只剩视频简介
    （"notes"）—— 而 YouTube 简介基本是赞助和订阅链接，按它判出来的低分
    不可信。实测：斯坦福那条扩散式 LLM 正课，取到字幕时 7/10
    「正课拆解扩散LLM机制，可核对讲义」，撞上 429 取不到时 2/10
    「课程宣传片，仅概述概念无推导细节」。**同一条视频，同一把尺子。**
    调用方必须据此决定这个"不做"要不要落成永久结论。
    """
    if not llm.available():
        return None
    basis = "notes"
    if (src.get("kind") or "") == "youtube":
        basis = "captions" if _caption_sample(ep, src) else "notes"
    try:
        r = llm.call_json(SYSTEM, _brief(ep, src) + "\n\n" + SCHEMA,
                          max_tokens=300, temperature=0.1, retries=1, role="triage")
    except Exception as ex:
        log(f"    选题闸门调用失败（放行）：{type(ex).__name__}")
        return None
    try:
        s = float(r.get("score"))
    except (TypeError, ValueError):
        return None
    return {"score": max(0.0, min(10.0, s)),
            "why": squeeze(str(r.get("why") or ""))[:60],
            "kind": squeeze(str(r.get("kind") or ""))[:12],
            "basis": basis,
            "rubric": rubric_id()}


def passes(v: dict | None, minimum: float = MIN_SCORE) -> bool:
    """闸门失灵时放行——宁可多做一集，也不要因为闸门本身坏了而空转。"""
    return True if v is None else v["score"] >= minimum
