#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""繁体站歧义字的日常审阅：日更建完站，把没审过的三字上下文交给模型看。

为什么有它：pipeline/tw_allow.txt 是人工审阅记录，test_tw.test_4 原来见到没登记的
上下文就红。可新节目每天都带来新上下文 —— 那道闸**正常发稿之后必红**，要等人来审。
2026-10-15 到 11-03 它和别的红混在一起，19 天没人看，184 条积压里藏着 7 处错字
（麵板、淨髮行、持有隻是……），都在站上。

现在由日更（scripts/local-daily.sh）建完站后跑这一步：
  · 模型说对 → 记进 data/tw-review.json 的 ok（tw.load_allow 一并读）；
  · 模型说错，而且改法只是把中间那个歧义字换成它另一种写法 → 记进 fix，
    tw.convert 一并套用，繁体树当场重建；
  · 拿不准、改法不合规、模型没回、整段不是汉字 → 留在 pending，记第一次见到的日子。
体检和 test_4 只在 overdue() 非空时红：有一条待审超过 2 天，或者这一步 2 天没跑成。
红了说明这一步坏了，不是「今天又来了新稿」。

    python3 pipeline/twreview.py            # 审一遍（要模型）
    python3 pipeline/twreview.py --check    # 只看有没有过期，不调模型；过期退出 1
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tw as TW  # noqa: E402

ROOT = os.path.dirname(HERE)
TWDIR = os.path.join(ROOT, "tw")
REVIEW = TW.REVIEW
SKIP = (TW.SKIP_DIRS - {"tw"}) | {"en"}
TAG = re.compile(r"<script.*?</script>|<style.*?</style>", re.S)
GRACE_DAYS = 2
BATCH = 80

# 每个歧义字允许换成的写法（台湾用字）。模型给的改法不在这张表里就不收。
ALT = {"髮": "發", "隻": "只", "餘": "余", "覆": "復複", "複": "復覆", "麵": "面",
       "乾": "幹干", "鬆": "松", "檯": "臺台", "曆": "歷", "製": "制", "彆": "別",
       "繫": "系係", "捲": "卷", "穀": "谷", "僕": "仆", "醜": "丑", "範": "范",
       "衝": "沖", "徵": "征"}
HAN = re.compile(r"^[㐀-鿿豈-﫿]{3}$")

SYSTEM = (
    "你在审简体转繁体（台湾用字）的结果。每一条给一个三字片段和它所在的句子，"
    "片段中间那个字是一简多繁的歧义字（例如 发→發/髮、只→只/隻、面→面/麵、干→乾/幹/干、"
    "复→復/複/覆、系→系/係/繫）。判断这个字在这句话里的繁体写法对不对。\n"
    "只回一个 JSON 数组，每条一个对象：\n"
    '  对：{"i": 编号, "ok": true}\n'
    '  错：{"i": 编号, "ok": false, "fix": "改正后的三字片段"}（只改中间那个字）\n'
    '  拿不准：{"i": 编号, "unsure": true}\n'
    "片段在几句话里意思不同、有对有错时，回拿不准。不要输出 JSON 以外的任何文字。")


def today() -> dt.date:
    return dt.date.today()


def load() -> dict:
    try:
        with open(REVIEW, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        d = {}
    for k in ("ok", "fix", "pending"):
        d.setdefault(k, {})
    return d


def save(d: dict) -> None:
    out = {k: dict(sorted(d[k].items())) for k in ("ok", "fix", "pending")}
    out["last_ok"] = d.get("last_ok", "")
    with open(REVIEW, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    TW.reset_review()


def _body(s: str) -> str:
    return " ".join(re.findall(r">([^<>]+)<", TAG.sub("", s)))


def unreviewed(twdir: str = TWDIR) -> dict[str, list[str]]:
    """{三字上下文: [例句, …]}：tw/ 里出现、还没登记成对的。最多留两句不同页的例句。"""
    allow = TW.load_allow()
    out: dict[str, list[str]] = {}
    for dp, dn, fn in os.walk(twdir):
        rel = os.path.relpath(dp, twdir)
        if set(rel.split(os.sep)) & SKIP:
            dn[:] = []
            continue
        for f in fn:
            if f not in ("index.html", "404.html"):
                continue
            with open(os.path.join(dp, f), encoding="utf-8") as fh:
                text = _body(fh.read())
            n = len(text)
            for i, ch in enumerate(text):
                if ch not in TW.AMBIGUOUS:
                    continue
                # 和 TW.contexts 同一个切法（句首句末用全角空格占位），这里要位置取例句
                c = (text[i - 1] if i else "　") + ch + (text[i + 1] if i + 1 < n else "　")
                if c in allow:
                    continue
                eg = out.setdefault(c, [])
                if len(eg) < 2:
                    s = text[max(0, i - 18):i + 19].strip()
                    if s not in eg:
                        eg.append(s)
    return out


def valid_fix(ctx: str, to: object) -> bool:
    """改法只许动中间那个字，换成 ALT 表里的写法。整段必须三个汉字：
    跨标签、带空格或句首句末占位的片段在页面源码里不一定能原样匹配，改了也改不到。"""
    return (isinstance(to, str) and HAN.match(ctx) is not None and len(to) == 3
            and to[0] == ctx[0] and to[2] == ctx[2] and to[1] in ALT.get(ctx[1], ""))


def ask_model(items: list[tuple[str, list[str]]]) -> list[dict]:
    from lib import llm
    if not llm.available():
        raise RuntimeError("没有可用的模型")
    lines = [f"{n}. 「{c}」 例：" + " ／ ".join(eg) for n, (c, eg) in enumerate(items, 1)]
    # role="tw"：不在 llm.model_name 的分工表里，走 LLM_MODEL（本机线是 sonnet）。
    # 判错会直接改站上的字，不交给最便宜的那档。
    txt = llm.call(SYSTEM, "\n".join(lines), max_tokens=4000, temperature=0, role="tw")
    m = re.search(r"\[.*\]", txt, re.S)
    if not m:
        raise RuntimeError("模型没回 JSON 数组：" + txt[:120])
    return json.loads(m.group(0))


def review(found: dict[str, list[str]], d: dict, ask=ask_model) -> tuple[int, int, int]:
    """审一遍，就地改 d。返回 (对, 改, 留待审)。模型调用出错照样抛出 —— last_ok 不更新。"""
    day = today().isoformat()
    n_ok = n_fix = 0
    items = sorted(found.items())
    for b in range(0, len(items), BATCH):
        chunk = items[b:b + BATCH]
        for v in ask(chunk):
            try:
                c, eg = chunk[int(v.get("i")) - 1]
            except (TypeError, ValueError, IndexError, AttributeError):
                continue
            if v.get("unsure"):
                continue
            if v.get("ok") is True:
                d["ok"][c] = day
                d["pending"].pop(c, None)
                n_ok += 1
            elif v.get("ok") is False and valid_fix(c, v.get("fix")):
                d["fix"][c] = {"to": v["fix"], "on": day, "eg": eg[0] if eg else ""}
                d["pending"].pop(c, None)
                n_fix += 1
    left = [c for c in found if c not in d["ok"] and c not in d["fix"]]
    for c in left:
        d["pending"].setdefault(c, day)
    d["last_ok"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    return n_ok, n_fix, len(left)


def overdue(d: dict | None = None, found: dict | None = None,
            now: dt.date | None = None) -> list[str]:
    """有毛病就返回说明，没毛病返回空列表。体检（硬伤）和 test_tw.test_4 都用它。"""
    d = load() if d is None else d
    found = unreviewed() if found is None else found
    now = now or today()
    out = []
    old = sorted(c for c in found
                 if c in d["pending"]
                 and (now - dt.date.fromisoformat(d["pending"][c])).days >= GRACE_DAYS)
    if old:
        out.append(f"{len(old)} 条歧义字上下文待审超过 {GRACE_DAYS} 天（模型拿不准或改法不合规）："
                   f"{old[:8]} —— 人看一眼：对就跑 python3 pipeline/gen_tw_allow.py，"
                   f"错就往 pipeline/tw.py 的 FIX 里加一条")
    fresh = [c for c in found if c not in d["pending"]]
    last = (d.get("last_ok") or "")[:10]
    stale = (not last) or (now - dt.date.fromisoformat(last)).days >= GRACE_DAYS
    if fresh and stale:
        out.append(f"繁体审阅（pipeline/twreview.py）{'从没跑成过' if not last else '上次跑成是 ' + last}，"
                   f"{len(fresh)} 条歧义字上下文没人审 —— 查日更日志里「繁体审阅」那一行")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只看过期没有，不调模型")
    a = ap.parse_args()
    d = load()
    found = unreviewed()
    if a.check:
        bad = overdue(d, found)
        print("\n".join(bad) or f"繁体歧义字：待审 {len(found)} 条，没有过期")
        return 1 if bad else 0
    if not found:
        d["last_ok"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        save(d)
        print("繁体审阅：没有新的歧义字上下文")
        return 0
    try:
        n_ok, n_fix, n_left = review(found, d)
    except Exception as ex:  # noqa: BLE001 —— 记下第一次见到的日子再退出，过期判据要用
        for c in found:
            d["pending"].setdefault(c, today().isoformat())
        save(d)
        print(f"繁体审阅没跑成：{ex}（{len(found)} 条留待下一轮；{GRACE_DAYS} 天没跑成体检报硬伤）")
        return 1
    save(d)
    print(f"繁体审阅：{len(found)} 条 · 对 {n_ok} · 改 {n_fix} · 留待审 {n_left}")
    for c, v in sorted(d["fix"].items()):
        if v["on"] == today().isoformat():
            print(f"  改 {c} → {v['to']}   例：{v['eg']}")
    if n_fix:
        t, b = TW.build(os.environ.get("PODCAST_BASE", "/podcast").rstrip("/"))
        print(f"  繁体树重建：文本 {t}，二进制 {b}")
        miss = [c for c in d["fix"] if c in unreviewed()]
        if miss:
            print(f"  ::error:: 这些改法没改到页面上：{miss}")
            return 1
    return 0


if __name__ == "__main__":
    import atexit
    from lib import llm as _llm
    # 用量进账本（data/usage.json）：中途崩掉、提前退出的那一轮也要记（见 lib/llm.persist_usage）
    atexit.register(lambda: _llm.persist_usage())
    sys.exit(main())
