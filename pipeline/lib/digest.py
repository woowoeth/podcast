"""Turn a transcript into the structured Chinese deep-read record.

Two things make this different from "ask a model for a summary":

1. Every claim is anchored. Points and quotes must carry a timestamp taken from
   the [mm:ss] markers in the transcript, so a reader can go check.
2. Quotes must be copied verbatim. The gate later greps the transcript for them
   and deletes any that are not there, which removes the whole class of
   plausible-sounding invented quotes.
"""
from __future__ import annotations

import json
import re

from . import llm
from .util import hhmmss, log, squeeze

def _default_max_chars() -> int:
    """How much transcript fits in one call.

    Anthropic's current models take 1M tokens, so a whole 35k-word transcript
    fits with room to spare. DeepSeek/Qwen/GLM are 64-128K, where the same
    transcript would overflow — so those providers hand long episodes to the
    map-reduce path instead of failing at the API.
    """
    from . import llm
    return 150000 if llm.provider() in ("anthropic", "claude-cli") else 80000


MAX_CHARS = int(__import__("os").environ.get("DIGEST_MAX_CHARS", "0")) or _default_max_chars()
CHUNK_CHARS = int(__import__("os").environ.get("DIGEST_CHUNK_CHARS", "0")) or min(MAX_CHARS // 2, 60000)

SYSTEM = """你是一个中文科技播客深读编辑。读者是中国的创业者、投资人和工程师：时间很贵，英文能读但不想读两万字逐字稿，最想要的是"这集里有什么我不知道、且能改变判断的东西"。

写作纪律：
- 只写逐字稿里真实出现过的内容。宁可少写，绝不补全、绝不推测、绝不"根据常识补充"。
- 每条要点必须落在逐字稿的某个 [mm:ss] 锚点附近，时间戳取那个锚点。
- 金句必须逐字复制原文，一个词都不许改。这一条会被机器逐字比对：转述、合并两处、
  改标点或大小写、把口语数字写成阿拉伯数字，都会导致这条金句被直接删掉。
  做法是从逐字稿里选一段**连续**的话原样粘过来；不确定长句能不能完整复制时，
  宁可选更短的一段。可以用 … 省略中间，但保留的每一段都必须逐字来自原文。
- 数字同理会被回原文核对。逐字稿里数字常是念出来的（"one point five billion"、
  "two hundred and fifty"），你写成 15亿、250 都可以，但**逐字稿里没出现过的数字
  一律不要写**——包括你自己知道的年份、排名、市值。
- 不写"这期节目讨论了…""值得一听""干货满满""令人深思"这类没有信息量的话。
- 不写任何关于抓取、字幕、脚本、转写、数据来源的元信息。读者不关心内容是怎么来的。
- 判断句优于概括句。写"Anthropic 的收入曲线是 2 万亿估值叙事的核心"，不写"节目讨论了 Anthropic 的估值"。
- 有分歧就写出分歧的两侧，不要拉平成共识。
- 中文里出现的公司名、模型名、产品名保留英文原写法。
- 中文引号一律用「」，不要用英文双引号——输出是 JSON，未转义的 " 会让整篇作废。"""

SCHEMA = """输出一个 JSON 对象，字段如下（不要输出任何别的东西）：

{
 "title": "中文标题，12-24 字。必须是一个判断或一个反常识结论，不是话题名。不用书名号引号。",
 "dek": "一句话结论，40-80 字。读者只看这一句也能拿走这集最重要的东西。",
 "why": "为什么值得听（或为什么可以不听），30-60 字。允许说这集信息密度低。",
 "points": [
   {"t": "mm:ss（必须来自逐字稿锚点）", "h": "小标题 6-14 字", "body": "120-240 字，写清楚论证链条而不只是结论", "spk": "说这段的人名，不确定就空字符串"}
 ],
 "quotes": [
   {"t": "mm:ss", "spk": "说话人名", "raw": "逐字复制的原文，30-320 字符", "zh": "中文翻译，信息不丢"}
 ],
 "facts": [
   {"k": "指标名，如 Anthropic 年化收入", "v": "数值与单位，如 1000-1200 亿美元", "t": "mm:ss"}
 ],
 "terms": [ {"term": "英文术语原文", "zh": "中文译名", "def": "一句话解释，≤50 字"} ],
 "who": "谁该听这集，25-50 字，具体到角色和场景",
 "skip": "哪一段可以跳过，≤40 字。全程都值得听就填空字符串。",
 "tags": ["3-6 个中文标签，如 算力、企业销售、后训练"]
}

数量要求：points 5-8 条；quotes 5-8 条；facts 0-10 条（逐字稿里没有具体数字就给空数组，
不要编）；terms 0-6 条（只收对中文读者真的陌生的术语）。

金句和数字都会过逐字校验，验不过的会被删掉。所以宁可多给几条稳的，也不要为了凑数
写不确定的——被删到少于 2 条金句，整篇就不会发布。"""


def _prompt(ep: dict, src: dict, text: str, chapters: list[dict]) -> str:
    head = [f"节目：{src.get('zh') or src['name']}",
            f"这一集的原标题：{ep['title']}"]
    if ep.get("duration"):
        head.append(f"时长：{hhmmss(ep['duration'])}（时间戳不得超过这个值）")
    if src.get("lang") == "zh":
        head.append("这是一档中文播客，金句请复制中文原文。")
    if chapters:
        head.append("节目方发布的章节表（可作为结构参考，但时间戳仍以逐字稿锚点为准）：\n"
                    + "\n".join(f"  {hhmmss(c['t'])} {c['label']}" for c in chapters[:24]))
    return ("\n".join(head) + "\n\n" + SCHEMA
            + "\n\n逐字稿（[mm:ss] 是时间锚点）：\n<<<\n" + text + "\n>>>")


MAP_SYSTEM = SYSTEM + "\n\n现在你只处理一集的一个片段。不要写总结性标题，只把这一段里真正有信息的部分记下来。"
MAP_SCHEMA = """输出 JSON：{"notes":[{"t":"mm:ss","h":"6-14字小标题","body":"100-220字","spk":"说话人或空"}],
"quotes":[{"t":"mm:ss","spk":"说话人","raw":"逐字原文","zh":"中文翻译"}],
"facts":[{"k":"指标名","v":"数值","t":"mm:ss"}]}
notes 3-6 条，quotes 1-3 条，facts 0-4 条。"""


def _chunks(text: str, size: int) -> list[str]:
    out, i = [], 0
    while i < len(text):
        j = min(i + size, len(text))
        if j < len(text):                       # break on a timestamp anchor
            k = text.rfind("\n[", i + size // 2, j)
            if k > i:
                j = k
        out.append(text[i:j])
        i = j
    return out


def _map_reduce(ep: dict, src: dict, text: str, chapters: list[dict]) -> dict:
    parts = _chunks(text, CHUNK_CHARS)
    log(f"    long episode: map over {len(parts)} chunks")
    notes, quotes, facts = [], [], []
    for i, part in enumerate(parts):
        try:
            r = llm.call_json(MAP_SYSTEM,
                              f"节目：{src.get('zh') or src['name']}\n这一集：{ep['title']}\n"
                              f"片段 {i+1}/{len(parts)}\n\n{MAP_SCHEMA}\n\n逐字稿片段：\n<<<\n{part}\n>>>",
                              max_tokens=4000)
        except Exception as e:
            log(f"    chunk {i+1} failed: {type(e).__name__}: {str(e)[:120]}")
            continue
        notes += r.get("notes") or []
        quotes += r.get("quotes") or []
        facts += r.get("facts") or []
    if not notes:
        raise RuntimeError("map pass produced nothing")
    digest_in = json.dumps({"notes": notes, "quotes": quotes, "facts": facts},
                           ensure_ascii=False)[:120000]
    return llm.call_json(
        SYSTEM + "\n\n下面给你的是同一集按时间顺序的分段笔记。请合并成一篇完整深读，"
                 "去掉重复、保留最有信息量的部分。时间戳沿用笔记里的时间戳，不要新造。",
        f"节目：{src.get('zh') or src['name']}\n这一集：{ep['title']}\n"
        f"时长：{hhmmss(ep.get('duration'))}\n\n{SCHEMA}\n\n分段笔记：\n{digest_in}",
        max_tokens=8000)


def build(ep: dict, src: dict, tr: dict, chapters: list[dict]) -> dict:
    from .transcript import flatten
    text = flatten(tr["segments"])
    if len(text) > MAX_CHARS:
        raw = _map_reduce(ep, src, text, chapters)
    else:
        raw = llm.call_json(SYSTEM, _prompt(ep, src, text, chapters), max_tokens=8000)
    return normalize(raw)


_LIST_FIELDS = {"points": ("t", "h", "body", "spk"),
                "quotes": ("t", "spk", "raw", "zh"),
                "facts": ("k", "v", "t"),
                "terms": ("term", "zh", "def")}


_TITLE_TAIL = re.compile(r"[\s，,、；;：。.…／/｜|]+$")
_WRAPPED = re.compile(r"^([「『\"“])(.+)([」』\"”])$")


def _clean_title(t: str) -> str:
    """A title should not end mid-clause, and should not be wrapped in quotes.

    Unwrap only when the whole title is quoted: 「大脑是计算机」不是哲学立场
    uses the same marks for an internal quotation, and stripping the opening one
    leaves a dangling 」."""
    t = squeeze(t)
    m = _WRAPPED.match(t)
    if m and m.group(2) and not any(c in m.group(2) for c in "「」『』“”"):
        t = m.group(2)
    return _TITLE_TAIL.sub("", t)


def normalize(raw: dict) -> dict:
    out = {k: squeeze(str(raw.get(k) or "")) for k in ("title", "dek", "why", "who", "skip")}
    out["title"] = _clean_title(out["title"])
    for name, keys in _LIST_FIELDS.items():
        items = raw.get(name)
        rows = []
        if isinstance(items, list):
            for it in items:
                if not isinstance(it, dict):
                    continue
                row = {k: squeeze(str(it.get(k) or "")) for k in keys}
                if any(row.values()):
                    rows.append(row)
        out[name] = rows
    tags = raw.get("tags")
    out["tags"] = [squeeze(str(t)) for t in tags if squeeze(str(t))][:6] if isinstance(tags, list) else []
    return out
