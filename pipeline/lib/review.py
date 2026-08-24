"""成稿评分：写完之后独立判一次，不到线就不上站。

和 triage 的分工：
  triage  —— 生成之前，判断"这一集值不值得做"（看标题和节目介绍）
  review  —— 生成之后，判断"这篇写得够不够好"（看成稿 + 原文证据）

为什么必须看原文证据：只看成稿的评审会奖励文笔，而这个站要的是信息。所以评审
看到的不是全文（太贵），而是**每一条要点和金句所在时间戳附近的原文片段**——
于是它能判断的是"这些是不是那一段里最值得记的东西、有没有被写歪"，
而不是"这段中文读起来顺不顺"。

机器闸门（gate.py）已经保证了金句逐字存在、数字回原文能找到、时间戳在时长内。
这一层判的是机器判不了的：信息密度、判断力、有没有说人话。
"""
from __future__ import annotations

import json
import os

from . import llm
from .util import hhmmss, log, squeeze

MIN_SCORE = float(os.environ.get("REVIEW_MIN", "8"))
WINDOW = 45          # 每个时间戳前后取多少秒的原文
EVIDENCE_CHARS = 7000

SYSTEM = """你是这个中文播客深读站的审稿人。你的唯一职责是把不够好的稿子拦下来，
不是把它改好，也不是鼓励作者。

读者是中国的创业者、投资人和工程师。他们读这篇是为了不听那两小时，所以这篇必须
把那两小时里真正值钱的东西端出来。

给你的材料有两部分：一篇成稿，以及成稿里每个时间戳附近的原文片段。请对照着看。

按这五项打分，然后给总分（0-10）：

1. 信息密度（权重最高）——读者读完手里多出了什么？如果每条要点都可以换成
   "他们讨论了 X"，那就是低分。要点必须是判断、机制或具体数字。
2. 忠实度 —— 对照原文片段：成稿有没有把原意写歪、把假设说成结论、把某人的
   反驳记成他的主张？有一处严重歪曲就不该过。
3. 选择力 —— 原文片段里明显更值钱的东西，成稿有没有漏掉而去写了次要的？
4. 具体性 —— 有没有数字、成本、时间线、人名、产品名？还是全是"某公司""大幅提升"。
5. 中文 —— 有没有翻译腔（"这是一个关于…的故事"）、空话（"值得深思"）、
   过长的从句？术语该保留英文的有没有保留？

打分基准：
  9-10  这一篇本身就值得转给同行，有别处看不到的判断
  8     合格：信息密度够，忠实，没有空话。可以上站
  6-7   能读，但至少一项明显不足（例如都对但没什么新东西）
  4-5   像是把节目介绍扩写了一遍
  0-3   有歪曲，或者通篇空话

不要因为原播客本身水平高就给成稿加分——你判的是这篇稿子。
不要因为文笔好就放过信息密度不足。宁可拦下，不要放过。"""

SCHEMA = """输出 JSON：
{"score": 0-10 的数字,
 "dims": {"density": 0-10, "faithful": 0-10, "selection": 0-10, "concrete": 0-10, "chinese": 0-10},
 "verdict": "上站|拦下",
 "why": "不超过 60 字的中文，说清最主要的那个理由",
 "worst": "最该改的一处，指出是哪条要点或哪句金句；没有就填空字符串"}"""


def _evidence(tr: dict, d: dict) -> str:
    """成稿引用到的每个时间点，取原文前后各 WINDOW 秒。"""
    segs = tr.get("segments") or []
    if not segs:
        return ""
    marks = sorted({int(p["t"]) for p in (d.get("points") or []) if p.get("t") is not None}
                   | {int(q["t"]) for q in (d.get("quotes") or []) if q.get("t") is not None})
    if not marks:
        return ""
    out, used = [], 0
    for m in marks:
        lo, hi = m - WINDOW, m + WINDOW
        chunk = " ".join(s["text"] for s in segs if lo <= s["t"] <= hi)
        chunk = squeeze(chunk)
        if not chunk:
            continue
        room = EVIDENCE_CHARS - used
        if room <= 200:
            out.append("……（原文片段已截断）")
            break
        piece = f"[{hhmmss(m)}] {chunk[:room]}"
        out.append(piece)
        used += len(piece)
    return "\n\n".join(out)


def _draft(d: dict) -> str:
    L = [f"标题：{d.get('title')}", f"导语：{d.get('dek')}", f"为什么听：{d.get('why')}", ""]
    L.append("要点：")
    for p in d.get("points") or []:
        spk = f"（{p['spk']}）" if p.get("spk") else ""
        L.append(f"  [{hhmmss(p.get('t'))}] {p.get('h')}{spk}：{p.get('body')}")
    L.append("\n金句：")
    for q in d.get("quotes") or []:
        L.append(f"  [{hhmmss(q.get('t'))}] {q.get('spk')}：{q.get('raw')}")
        L.append(f"      译：{q.get('zh')}")
    if d.get("facts"):
        L.append("\n数字：")
        for f in d["facts"]:
            L.append(f"  {f.get('k')} = {f.get('v')}")
    if d.get("terms"):
        L.append("\n术语：" + "、".join(t.get("term", "") for t in d["terms"]))
    L.append(f"\n谁该听：{d.get('who')}\n可跳过：{d.get('skip') or '（无）'}")
    return "\n".join(L)


def check(d: dict, tr: dict, ep: dict, src: dict) -> dict | None:
    """返回 {"score","dims","verdict","why","worst"}；模型不可用时返回 None。"""
    if not llm.available():
        return None
    ev = _evidence(tr, d)
    user = (f"节目：{src.get('zh') or src['name']}\n原集标题：{ep.get('title')}\n"
            f"时长：{hhmmss(ep.get('duration'))}\n\n"
            f"=== 成稿 ===\n{_draft(d)}\n\n"
            f"=== 成稿引用处的原文片段 ===\n{ev or '（拿不到原文片段）'}\n\n{SCHEMA}")
    try:
        r = llm.call_json(SYSTEM, user, max_tokens=900, temperature=0.1, retries=1)
    except Exception as ex:
        log(f"    成稿评分调用失败（放行）：{type(ex).__name__}: {str(ex)[:90]}")
        return None
    try:
        s = float(r.get("score"))
    except (TypeError, ValueError):
        return None
    dims = r.get("dims") if isinstance(r.get("dims"), dict) else {}
    return {"score": max(0.0, min(10.0, s)),
            "dims": {k: dims.get(k) for k in
                     ("density", "faithful", "selection", "concrete", "chinese")},
            "verdict": squeeze(str(r.get("verdict") or ""))[:8],
            "why": squeeze(str(r.get("why") or ""))[:90],
            "worst": squeeze(str(r.get("worst") or ""))[:120]}


def passes(v: dict | None, minimum: float = MIN_SCORE) -> bool:
    """评审失灵时放行——但这种情况会在日志和记录里留痕，不会假装评过。"""
    return True if v is None else v["score"] >= minimum
