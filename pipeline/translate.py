#!/usr/bin/env python3
"""把成稿译成英文，存到 data/en/<slug>.json。

**金句绝不回译。** 268 篇里 235 篇是英文源节目，它们的 `quotes[].raw` 本来就是
逐字英文原文——英文版直接用它，读者看到的是说话人真说过的话。回译会直接毁掉这个
站的前提（每句都能跳回原声核对），而且英文读者**看不出那是译文**，比中文站上更糟。
剩下 33 篇中文源节目才需要译金句，并且必须标出来是译文。

要译的：标题、一句话结论、要点（小标题 + 正文）、术语、收听指南、数字条目的键名。
不译的：时间戳、数字本身、公司名产品名（中文稿里本来就保留英文原写法）。

一集一次调用，用便宜模型（role=review），带断点续跑——268 篇一趟要一个多小时，
中间一定会被打断。

    python3 pipeline/translate.py --dry-run --limit 2
    python3 pipeline/translate.py --limit 40
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import pathlib
import re
import sys
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lib import llm                                              # noqa: E402
from lib.util import log, squeeze                                # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
EPS = ROOT / "data" / "episodes"
OUT = ROOT / "data" / "en"
DONE = ROOT / "data" / "translate-done.json"

_CJK = re.compile(r"[一-鿿㐀-䶿]")
# 中文标点也算漏译。实测第一篇就出了 `「generation and compression」`——
# 源稿的 「」 约定被原样抄进英文。闸门只查汉字是查不到的。
# 只列**英文里不该出现**的：全角逗号句号、方括号引号、书名号这些。
# 破折号 —(U+2014)、省略号 …(U+2026)、间隔号 ·(U+00B7) 都是正常英文标点，
# 我第一版把它们也列进来了，于是每篇都被判"有中文标点"，而译文其实是对的。
# 判据要贴着失败机制：em dash 不是失败。
_CJK_PUNCT = re.compile(r"[「」『』，。、；：？！（）《》【】〔〕]")

# 归一化而不是打回：源稿一直用这套标点，模型会一直照抄，打回只是白花钱。
_PUNCT_MAP = {
    "「": "\u2018", "」": "\u2019",        # 嵌套术语 → 英文单引号
    "『": "\u201c", "』": "\u201d",
    "，": ", ", "。": ". ", "、": ", ", "；": "; ", "：": ": ",
    "？": "? ", "！": "! ", "（": " (", "）": ") ",
    "《": "\u201c", "》": "\u201d", "【": "[", "】": "]",
    "〔": "[", "〕": "]",
}


def _en_punct(s: str) -> str:
    if not s:
        return s
    for a, b in _PUNCT_MAP.items():
        s = s.replace(a, b)
    return re.sub(r"\s{2,}", " ", s).strip()

SYSTEM = """You are the English editor of a podcast deep-read site. The Chinese
edition is written for founders, investors and engineers; the English edition is
the same journalism for the same kind of reader, in English.

You are translating an already-finished piece. Discipline:
- Translate the argument, not the words. Chinese written for this audience is
  dense and elliptical; English needs the connective tissue. Do not pad.
- Keep every claim exactly as strong as the original. If the Chinese hedges
  ("大概率", "也许"), hedge in English. If it asserts, assert.
- Company, model and product names are already in Latin script in the source —
  keep them byte-identical. Never re-spell them.
- Chinese apps, companies and people that have no Latin name: romanise (pinyin,
  no tone marks) and put the characters in parentheses on first mention, e.g.
  Dongchedi (懂车帝). Do not leave a bare Chinese name with no romanisation, and
  do not invent an English name that the company does not use.
- Numbers, dates and timestamps: copy exactly. Never convert units, never round,
  never add a figure that is not in the source.
- A point heading must stay a claim someone could disagree with, not a topic
  label. 6-14 words. Same rule as the Chinese: not "The definition of misconduct"
  but "The test for crossing the line is: this is not your money".
- Do not add anything: no "in this episode", no editorializing, no summary of
  the summary.
- Output valid JSON. Use straight ASCII quotes inside strings only when escaped.
"""

ASK = """Translate this finished piece into English.

Return JSON with exactly these keys:
{"title": "...", "dek": "...", "why": "...", "who": "...", "skip": "...",
 "tags": ["...", ...],
 "points": [{"h": "...", "body": "..."}, ...],
 "terms": [{"term": "...", "def": "..."}, ...],
 "facts": [{"k": "...", "v": "..."}, ...]%s}

- points / terms / facts / tags must have exactly the same number of entries,
  in the same order. Never merge or drop one.
- tags are short topic labels (1-3 words), the English equivalent of the source
  tags. Keep them as labels, not sentences.
- terms[].term is the English term itself (the source keeps it in Latin script
  where one exists); terms[].def is its one-line English explanation.
- facts[].k is the label, facts[].v the value. **Copy every number verbatim.**
- Leave a key as "" if the source has nothing for it.
"""

QUOTE_ASK = """,
 "quotes": [{"en": "..."}, ...]"""


def is_english_source(ep: dict) -> bool:
    """金句原文是英文吗。看 raw 里有没有汉字，比信 lang 字段可靠——
    lang 描述的是节目，而我们要判断的是**这几段引文**能不能直接用。"""
    qs = [q.get("raw") or "" for q in ((ep.get("digest") or {}).get("quotes") or [])]
    if not qs:
        return (ep.get("lang") or "en") == "en"
    cjk = sum(len(_CJK.findall(q)) for q in qs)
    return cjk < 5


def brief(ep: dict) -> str:
    """喂给模型的原稿。

    **把条数写在最前面。** 不写的话模型会自己合并或拆分条目（实测第一次就把
    5 条术语给成了 6 条），闸门拦下来就得重花一次钱。把 must-match 的数字明确
    列出来，比事后校验便宜。
    """
    d = ep["digest"]
    counts = (f"COUNTS THAT MUST MATCH EXACTLY — points: {len(d.get('points') or [])}, "
              f"terms: {len(d.get('terms') or [])}, "
              f"facts: {len(d.get('facts') or [])}, "
              f"tags: {len(d.get('tags') or [])}")
    out = [counts, "",
           f"Show: {ep.get('source') or ''}",
           f"Original episode title: {ep.get('title_original') or ''}", "",
           f"标题: {d.get('title')}", f"一句话: {d.get('dek')}"]
    for k, label in (("why", "为什么听"), ("who", "谁该听"), ("skip", "可跳过")):
        if d.get(k):
            out.append(f"{label}: {d[k]}")
    out.append("\n要点:")
    for i, p in enumerate(d.get("points") or [], 1):
        out.append(f"{i}. {p.get('h')}")
        out.append(f"   {p.get('body')}")
    if d.get("tags"):
        out.append("标签: " + " / ".join(d["tags"]))
    if d.get("terms"):
        out.append("\n术语:")
        for t in d["terms"]:
            out.append(f"- {t.get('term')} / {t.get('zh')}: {t.get('def')}")
    if d.get("facts"):
        out.append("\n数字:")
        for f in d["facts"]:
            out.append(f"- {f.get('k')}: {f.get('v')}")
    return "\n".join(out)


def quote_brief(ep: dict) -> str:
    qs = (ep["digest"].get("quotes") or [])
    lines = ["\n中文源节目，金句原文是中文，需要译成英文（保持是引语，不要转述）："]
    for i, q in enumerate(qs, 1):
        lines.append(f"{i}. {q.get('raw') or q.get('zh')}")
    return "\n".join(lines)


def load_done() -> set[str]:
    try:
        return set(json.loads(DONE.read_text()).get("slugs") or [])
    except Exception:
        return set()


def save_done(s: set[str]) -> None:
    DONE.write_text(json.dumps({"slugs": sorted(s)}, ensure_ascii=False,
                               indent=1) + "\n")


def check(ep: dict, en: dict, en_source: bool) -> list[str]:
    """译文自己的闸门。判据只查**能机械查的**，不评价译得好不好。"""
    d = ep["digest"]
    bad = []
    if len(en.get("title") or "") < 12:
        bad.append("title 太短或空")
    if len(en.get("dek") or "") < 30:
        bad.append("dek 太短或空")
    for k in ("points", "terms", "facts", "tags"):
        want, got = len(d.get(k) or []), len(en.get(k) or [])
        if want != got:
            bad.append(f"{k} 条数不对：要 {want} 给了 {got}")
    # 漏译：英文字段里不该有汉字（公司名产品名在中文稿里本来就是拉丁字母）
    def scan(v, path):
        if isinstance(v, str):
            # 只有中文名的中国应用和公司（懂车帝、幸福里、海豚股票）在英文正文里
            # 是**合法的专名**，不是漏译——实测这类占比 0.30%。而真正漏译一整句
            # 的话汉字占比在 30% 以上。给一个预算，两者离得很远。
            # 渲染时这些汉字会被 build.py 包进 <span lang="zh">，所以英文站的
            # "零漏译"闸门也不会误报。
            n_cjk = len(_CJK.findall(v))
            if n_cjk and (n_cjk > max(8, len(v) * 0.04)
                          or not re.search(r"[A-Za-z]", v)):
                bad.append(f"{path} 里有 {n_cjk} 个汉字没译（占 "
                           f"{n_cjk / max(len(v), 1) * 100:.0f}%）：{v[:40]}")
            elif _CJK_PUNCT.search(v):
                bad.append(f"{path} 里有中文标点：{v[:40]}")
        elif isinstance(v, list):
            for i, x in enumerate(v):
                scan(x, f"{path}[{i}]")
        elif isinstance(v, dict):
            for kk, x in v.items():
                scan(x, f"{path}.{kk}")
    for k in ("title", "dek", "why", "who", "skip", "points", "terms",
              "facts", "tags"):
        scan(en.get(k), k)
    if not en_source:
        want, got = len(d.get("quotes") or []), len(en.get("quotes") or [])
        if want != got:
            bad.append(f"quotes 条数不对：要 {want} 给了 {got}")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", default="", help="只处理这些 slug 片段，逗号分隔")
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--workers", type=int, default=4,
                    help="并发数。串行一篇约 25-30 秒，269 篇要两小时")
    a = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    seen = set() if a.redo else load_done()
    want = [x.strip() for x in a.only.split(",") if x.strip()]
    files = sorted(EPS.glob("*.json"), reverse=True)
    if seen:
        log(f"已译过 {len(seen)} 篇，跳过")

    # 并发。llm.py 里只有 _json_mode 一个可变标志（能力探测用，并发写同一个值
    # 无害），其余都是只读配置，所以线程池是安全的。断点记录和日志各上一把锁：
    # save_done 每次重写整个文件，两个线程同时写会互相截断。
    todo = []
    for f in files:
        try:
            ep = json.loads(f.read_text())
        except Exception:
            continue
        slug = ep.get("slug") or ""
        if want and not any(w in slug for w in want):
            continue
        if slug in seen:
            continue
        todo.append(ep)
    if a.limit:
        todo = todo[:a.limit]
    log(f"要译 {len(todo)} 篇，并发 {a.workers}")

    lock = threading.Lock()
    tally = {"done": 0, "failed": 0}

    def one(ep):
        slug = ep.get("slug") or ""
        en_src = is_english_source(ep)
        ask = ASK % ("" if en_src else QUOTE_ASK)
        body = brief(ep) + ("" if en_src else quote_brief(ep))
        need = max(4000, int(len(body) * 1.6))
        try:
            try:
                r = llm.call_json(SYSTEM, body + "\n\n" + ask,
                                  max_tokens=need, temperature=0.2, role="review")
            except Exception as ex:
                if "内容为空" not in str(ex):
                    raise
                with lock:
                    log(f"      额度不够（{need}），加倍重试 {slug[:34]}")
                r = llm.call_json(SYSTEM, body + "\n\n" + ask,
                                  max_tokens=need * 2, temperature=0.2, role="review")
        except Exception as ex:
            with lock:
                log(f"  {slug[:44]} 译失败：{type(ex).__name__}: {str(ex)[:160]}")
                tally["failed"] += 1
            return

        def clean(v):
            if isinstance(v, str):
                return _en_punct(v)
            if isinstance(v, list):
                return [clean(x) for x in v]
            if isinstance(v, dict):
                return {k: clean(x) for k, x in v.items()}
            return v
        r = clean(r)
        problems = check(ep, r, en_src)
        if problems:
            with lock:
                log(f"  {slug[:44]} 不合格，不写：")
                for x in problems[:4]:
                    log(f"      {x}")
                tally["failed"] += 1
            return

        quotes = []
        for i, q in enumerate(ep["digest"].get("quotes") or []):
            if en_src:
                quotes.append({"text": q.get("raw") or "", "translated": False})
            else:
                got = (r.get("quotes") or [])[i] if i < len(r.get("quotes") or []) else {}
                quotes.append({"text": squeeze(str(got.get("en") or "")),
                               "translated": True})
        rec = {"slug": slug, "of": ep.get("id"),
               "source_lang": "en" if en_src else "zh",
               "title": squeeze(r.get("title") or ""),
               "dek": squeeze(r.get("dek") or ""),
               "why": squeeze(r.get("why") or ""),
               "who": squeeze(r.get("who") or ""),
               "skip": squeeze(r.get("skip") or ""),
               "tags": [squeeze(str(x)) for x in (r.get("tags") or [])][:6],
               "points": [{"h": squeeze(x.get("h") or ""),
                           "body": squeeze(x.get("body") or "")}
                          for x in (r.get("points") or [])],
               "terms": [{"term": squeeze(x.get("term") or ""),
                          "def": squeeze(x.get("def") or "")}
                         for x in (r.get("terms") or [])],
               "facts": [{"k": squeeze(x.get("k") or ""),
                          "v": squeeze(x.get("v") or "")}
                         for x in (r.get("facts") or [])],
               "quotes": quotes,
               "model": f"{llm.provider()}:{llm.model_name()}"}
        with lock:
            tally["done"] += 1
            log(f"  {'[dry] ' if a.dry_run else ''}[{tally['done']}/{len(todo)}] "
                f"{slug[:38]}（{'英文源，金句用原话' if en_src else '中文源，金句已译'}）")
            log(f"      {rec['title']}")
            if not a.dry_run:
                (OUT / f"{slug}.json").write_text(
                    json.dumps(rec, ensure_ascii=False, indent=1) + "\n")
                seen.add(slug)
                save_done(seen)

    with cf.ThreadPoolExecutor(max_workers=max(1, a.workers)) as pool:
        list(pool.map(one, todo))
    done, failed = tally["done"], tally["failed"]
    log(f"\n{'（dry-run，一个字没写）' if a.dry_run else '已写入 data/en/'} "
        f"成 {done} · 不合格 {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
