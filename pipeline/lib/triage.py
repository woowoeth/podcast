"""选题闸门：在花钱做深读之前，先判断这一集值不值得做。

为什么需要这一层：原来的打分（run.py:score）只看信源等级、新鲜度、有没有文稿，
不看这一集本身讲什么。于是往回抓存量时，Odd Lots 讲沙丁鱼罐头装罐流程的那集
和讲钨市场预示战争的那集是同等待遇——前者抓过来就是凑数。

成本对比：triage 只喂标题 + show notes（约 600 token 进、100 出），深读要喂整份
逐字稿（1-5 万 token）。所以先筛一遍在经济上是压倒性的：把 90% 的不合格集挡在
模型的大开销之前。
"""
from __future__ import annotations

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
分界线不是「有没有数字」，而是**能不能被反驳**。套话（「要有耐心」「保持学习」）低分。

**按分类加分轴（分类对了就用这条，不要再拿商业框架苛责）：**
- ai / biz / cn：产业机制、监管、供应链、投资决策、一手创业经验、中美对照
- ideas：论证链、可争辩哲学/社科结论、思想史中的明确命题
- hist：因果与时间线、制度/文明机制、可回史料或一手记述的判断
- parent：可检验的教养原则、发展心理学证据、边界条件（何种孩子/年龄有效）
- sci：机制、实验或论文可回、对流行说法的限定条件（不是养生口播）

**所有分类都给低分（0-4），这条不放宽：**
- 新闻综述、一周回顾、榜单
- 纯宣传、广告口播、课程推销
- 趣味闲聊、景点打卡、纯鸡汤、无法证伪的正确话

不要因为「与科技商业无关」就否掉 hist / parent / sci / ideas 的合格集。
中间分（5-6）：有内容但密度不高。7 分及以上才做深读。

只看标题和节目介绍——介绍空洞本身就是信号。"""

SCHEMA = """输出 JSON：{"score": 0-10 的数字, "why": "不超过 40 字的中文理由",
"kind": "一手访谈|机制拆解|新闻综述|宣传|闲聊|其他"}"""


def _brief(ep: dict, src: dict) -> str:
    notes = strip_html(ep.get("notes"))
    # 章节表比宣传语更能说明这一集讲了什么
    notes = squeeze(notes)[:NOTES_CHARS]
    parts = [f"分类：{src.get('cat','ai')}",
             f"节目：{src.get('zh') or src['name']}（{src.get('desc', '')}）",
             f"这一集标题：{ep['title']}"]
    if ep.get("duration"):
        parts.append(f"时长：{hhmmss(ep['duration'])}")
    parts.append(f"节目介绍：\n{notes or '（没有介绍）'}")
    return "\n".join(parts)


def score(ep: dict, src: dict) -> dict | None:
    """返回 {"score": float, "why": str, "kind": str}；模型不可用时返回 None。"""
    if not llm.available():
        return None
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
            "kind": squeeze(str(r.get("kind") or ""))[:12]}


def passes(v: dict | None, minimum: float = MIN_SCORE) -> bool:
    """闸门失灵时放行——宁可多做一集，也不要因为闸门本身坏了而空转。"""
    return True if v is None else v["score"] >= minimum
