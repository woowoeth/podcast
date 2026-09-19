"""发稿前把**转写残留**从金句里摘掉。

**为什么单独做一步。** 金句是逐字引用、署着真人名字的。语音转写会把专名
写成别的词，而查重闸拿这句去对**同一份错的转写** —— 内部一致，所以过了。
闸测的是一致性，不是正确性，这个洞它堵不上。

实测（张小珺存量补齐的头 6 集）：5 篇里 4 篇的金句带这种残留 ——
Elon 转成「英朗／伊朗」（单集 78 处）、「护城河」转成「户层盒」、
Anthropic 转成「Anthorpeg」、「机器人」转成「机程」。
**要点里一处都没有** —— 深读是转述，模型会把话理顺；只有逐字引用会原样带出来。

试过更便宜的办法，不行：给 whisper 喂 initial_prompt 词表，在真出错的那段
音频上测了两次（一次通用词表、一次专门加「埃隆/Elon」），**错字一处没少** ——
initial_prompt 只影响第一个窗口，300 秒一片里后面的窗口够不着它。

也不能做成固定替换表：「伊朗」本身是正常词（Iran），一刀切会把真讲伊朗的集改坏。

所以用模型判，但只判一件很窄的事：这句里有没有**明显不是人话**的专名残留。
判错的代价不对称 —— 漏掉一个错字只是难看，误删一句好金句是损失内容，
所以提示词要求「拿不准就不要标」。

取不到模型、调用失败、返回读不懂 —— 一律当作「没有坏句」放行。
这一步是加分项，不该成为发不出去的新理由。
"""
from __future__ import annotations

from . import llm
from .util import log, squeeze

SYSTEM = (
    "你在校对一批从语音转写里摘出来的中文引语。"
    "只找一类问题：**专有名词被语音识别写成了不成词的东西**"
    "（人名、公司名、术语被转成同音的无意义字串，例如把 Elon 写成「英朗」、"
    "把「护城河」写成「户层盒」、把 Anthropic 写成「Anthorpeg」）。"
    "不要管标点、口语重复、语气词、断句、用词好坏 —— 那些都不算。"
    "**拿不准就不要标。** 漏掉一个错字只是难看，误删一句好引语是损失内容。"
)
SCHEMA = ('只输出 JSON：{"bad": [{"i": 序号, "s": "那个不成词的字串"}]}。'
          '没有问题就输出 {"bad": []}。')


def bad_quotes(quotes: list[dict]) -> dict[int, str]:
    """返回 {下标: 坏字串}。判不了就返回空 —— 放行，不拦。"""
    items = [(i, squeeze(str(q.get("raw") or q.get("zh") or "")))
             for i, q in enumerate(quotes)]
    items = [(i, t) for i, t in items if t]
    if not items or not llm.available():
        return {}
    body = "\n".join(f"{i}. {t}" for i, t in items)
    try:
        r = llm.call_json(SYSTEM, body + "\n\n" + SCHEMA,
                          max_tokens=400, temperature=0.0, retries=1, role="triage")
    except Exception as ex:
        log(f"    金句校对调用失败（放行）：{type(ex).__name__}")
        return {}
    out: dict[int, str] = {}
    for x in (r.get("bad") or []):
        try:
            i = int(x.get("i"))
        except (TypeError, ValueError):
            continue
        s = squeeze(str(x.get("s") or ""))
        # **模型说的那个字串必须真在那句里。** 不核这一下的话，它随口编一个
        # 字串就能删掉一条好金句 —— 判据要落在真东西上。
        if 0 <= i < len(quotes) and s and s in str(quotes[i].get("raw") or ""):
            out[i] = s
    return out


def drop_bad_quotes(d: dict) -> list[str]:
    """就地摘掉坏金句，返回给日志看的说明。"""
    qs = d.get("quotes") or []
    if len(qs) < 2:
        return []
    bad = bad_quotes(qs)
    if not bad:
        return []
    d["quotes"] = [q for i, q in enumerate(qs) if i not in bad]
    return [f"「{s}」" for _, s in sorted(bad.items())]
