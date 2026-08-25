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

SYSTEM = """你在给一个中文播客深读站做选题。读者是中国的创业者、投资人和工程师。

你的任务不是判断话题热不热，而是判断这一集**读完之后读者手里会多出什么**。

**价值有两种形态，都算高分，不要只认第一种。**

形态一 · 事实与机制：
- 一手当事人讲自己做过的事，有具体数字、成本、时间线、失败细节
- 讲清某个机制怎么运作（定价、供应链、组织、技术选型），读者能拿去做决策

形态二 · 判断与框架：
- 提出一个**有立场、能被反驳**的论断，并给出理由和取舍
- 把一个复杂问题拆成少数几个真正起作用的变量，并说明为什么其余是噪音
- 挑战一个流行共识，且给得出反例或边界条件
- 思想史、哲学、方法论讨论 —— 只要它得出的是可争辩的结论，不是"要保持好奇心"

这里的分界线不是"有没有数字"，而是**能不能被反驳**：
  「要长期投资、控制情绪」          无法反驳的套话 → 低分
  「只有储蓄率、资产配置、不恐慌三件事重要，其余全是噪音」  有立场能反驳 → 高分
第一版把这两者混为一谈，用"无可核对数据"拒掉了一集严肃的投资哲学访谈——数字少
不是缺陷，无法证伪才是。

给高分的还包括：
- 对中国读者有额外价值：涉及中美产业、出海、算力、供应链、监管

给低分的（0-4）：
- 新闻综述、一周回顾、榜单，信息都能在别处更快拿到
- 纯宣传、广告口播、产品发布会
- 泛泛而谈：通篇是无法反驳的正确话（"要有耐心""长期主义""保持学习"），
  既没有事实也没有可争辩的立场
- 话题与科技/商业/投资无关，且没有可迁移的方法或框架

中间分（5-7）：有内容但密度不高，或者只对很窄的人群有用。

只看给你的标题和节目介绍来判断——不要假设里面还有你没看到的内容。介绍写得空洞
本身就是信号。"""

SCHEMA = """输出 JSON：{"score": 0-10 的数字, "why": "不超过 40 字的中文理由",
"kind": "一手访谈|机制拆解|新闻综述|宣传|闲聊|其他"}"""


def _brief(ep: dict, src: dict) -> str:
    notes = strip_html(ep.get("notes"))
    # 章节表比宣传语更能说明这一集讲了什么
    notes = squeeze(notes)[:NOTES_CHARS]
    parts = [f"节目：{src.get('zh') or src['name']}（{src.get('desc', '')}）",
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
