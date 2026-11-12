#!/usr/bin/env python3
"""把说话人字段译成英文，存 data/en/_speakers.json。

为什么单独一支：说话人是**跨集复用**的短字符串（「主持人」出现 23 次，
「曾鸣」15 次），按集译会把同一个名字译出好几种写法。全站去重后只有几十个
不同的值，一次调用译完，页面上就永远一致。

它原来根本没被译过——译文数据里 2143 条论点，带 spk 的是 0 条，于是英文页
一律回退到中文原值：「主持人」、「Tim Harford（旁白）」、
「西格尔·塞缪尔」——最后这个是 Sigal Samuel 音译成中文，又原样端给了
英文读者。HTML 的「零漏译」闸门放行了它们，因为渲染处包了 lang="zh"，
而那条判据的用意是保住**人名的原文**，不是给漏译开后门。
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lib import llm                                              # noqa: E402
from lib.util import log                                         # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
EPS = ROOT / "data" / "episodes"
OUT = ROOT / "data" / "en" / "_speakers.json"
CJK = re.compile(r"[一-鿿㐀-䶿]")

SYSTEM = (
    "You render podcast speaker labels for the English edition of a podcast "
    "digest site. Each input is a speaker label written in Chinese. Rules, in "
    "order of priority:\n"
    "1. A role word becomes the plain English role: 主持人 -> Host, "
    "受访者 -> Guest, 旁白 -> narration, 听众来信 -> listener mail, "
    "工程师 -> engineer, 转述X -> paraphrasing X.\n"
    "2. A Chinese person's name becomes its standard romanization "
    "(Hanyu Pinyin, given name after family name, e.g. 曾鸣 -> Zeng Ming). "
    "Use the spelling the person is actually known by in English when there "
    "is one (贾扬清 -> Yangqing Jia, 张忠谋 -> Morris Chang).\n"
    "3. A name that is a Chinese transliteration OF A NON-CHINESE NAME must be "
    "restored to its original spelling: 西格尔·塞缪尔 -> Sigal Samuel, "
    "凯文·凯利 -> Kevin Kelly. Never leave these transliterated.\n"
    "4. A nickname or handle stays recognizable: 老石 -> Lao Shi.\n"
    "5. Keep the shape of the label. 'Tim Harford（旁白）' -> "
    "'Tim Harford (narration)'. Latin-script parts are copied byte-identical.\n"
    "Output valid JSON only. No Chinese characters in any output value."
)

ASK = ('Render each label. Return JSON: {"items": [{"zh": "<input, verbatim>", '
       '"en": "..."}, ...]} with one entry per input line, inputs copied exactly.')


def collect() -> list[str]:
    seen: dict[str, int] = {}
    for f in sorted(EPS.glob("*.json")):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        dg = d.get("digest") or {}
        for row in (dg.get("points") or []) + (dg.get("quotes") or []):
            s = (row.get("spk") or "").strip()
            if s and CJK.search(s):
                seen[s] = seen.get(s, 0) + 1
    # 出现次数多的先译：一批失败时先保住露脸最多的
    return sorted(seen, key=lambda k: (-seen[k], k))


_SEP = re.compile(r"\s*([/、])\s*")


def _rejoin(label: str, known: dict) -> str:
    """合写的标签（「甲/乙」「甲、乙」）按每一截的译名拼回去；有一截没有译名就返回空串。
    分隔符照表里已有的写法：「/」→ " / "，「、」→ ", "。"""
    bits = _SEP.split(label)
    if len(bits) < 3:
        return ""
    out = []
    for n, b in enumerate(bits):
        if n % 2:
            out.append(" / " if b == "/" else ", ")
            continue
        b = b.strip()
        en = b if (b and not CJK.search(b)) else known.get(b, "")
        if not en:
            return ""
        out.append(en)
    return "".join(out)


def main() -> int:
    have = {}
    if OUT.exists():
        try:
            have = json.loads(OUT.read_text()).get("speakers", {})
        except Exception:
            have = {}
    todo = [s for s in collect() if s not in have]
    if not todo:
        log(f"说话人都译过了（{len(have)} 个）")
        return 0
    log(f"要译 {len(todo)} 个说话人（已有 {len(have)} 个）")
    bad = 0
    for i in range(0, len(todo), 40):
        batch = todo[i:i + 40]
        try:
            r = llm.call_json(SYSTEM, "\n".join(batch) + "\n\n" + ASK,
                              max_tokens=4000, temperature=0.1, role="review", step="speakers")
        except Exception as ex:
            log(f"  这一批失败：{type(ex).__name__}: {ex}")
            continue
        got = 0
        parts = {}                  # 模型答了、但不在这一批里的：多半是把合写的标签拆开了
        for it in (r.get("items") or []):
            zh, en = (it.get("zh") or "").strip(), (it.get("en") or "").strip()
            if not zh or not en:
                continue
            # **判据：输出里不许再有汉字。** 不合格就不收——留着中文原值，
            # 下一轮还会被挑出来重译，比写进去一个假译文好。
            if CJK.search(en):
                log(f"    不收：{zh} -> {en}（还有汉字）")
                bad += 1
                continue
            if zh not in set(batch):
                parts[zh] = en
                continue
            have[zh] = en
            got += 1
        # **合写的标签被拆开答回来，就按原来的写法拼回去。** 2026-11-12：「肖文杰/陈瑞」被答成
        # 「肖文杰」「陈瑞」两条，哪一条都对不上这一批，原来就悄悄丢了 —— 日志写「0 个不合格」，
        # 每一班重问一次、每次照样丢，体检的硬伤一直挂着。每一截都有译名才拼，缺一截就不收。
        for zh in batch:
            if zh in have:
                continue
            en = _rejoin(zh, {**have, **parts})
            if en:
                have[zh] = en
                got += 1
            else:
                log(f"    没译出来：{zh}（模型没按原样答回这一条）")
                bad += 1
        log(f"  第 {i//40 + 1} 批：{got}/{len(batch)}")
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps({"speakers": have}, ensure_ascii=False,
                                  indent=1, sort_keys=True) + "\n")
    log(f"共 {len(have)} 个说话人有译名，{bad} 个不合格")
    return 0


if __name__ == "__main__":
    import atexit
    # 用量进账本（data/usage.json）：中途崩掉、提前退出的那一轮也要记（见 lib/llm.persist_usage）
    atexit.register(lambda: llm.persist_usage())
    raise SystemExit(main())
