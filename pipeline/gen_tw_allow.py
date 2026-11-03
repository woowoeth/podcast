#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重新生成歧义字白名单（审阅记录）。

**日常审阅不靠它了**：日更建完站跑 pipeline/twreview.py，由模型审、记在
data/tw-review.json。这个脚本只给人工收尾用 —— 体检报「待审超过 2 天」（模型
拿不准的几条）时，人看过、确认都对，再跑这个。它会把当前 tw/ 里**所有**上下文
一次性登记成「已审」，没看就跑等于把这道检查关掉。
"""
import os, re, sys, collections
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tw as TW  # noqa: E402
ROOT = os.path.dirname(HERE)
TAG = re.compile(r"<script.*?</script>|<style.*?</style>", re.S)
cnt = collections.Counter()
for dp, dn, fn in os.walk(os.path.join(ROOT, "tw")):
    for f in fn:
        if f not in ("index.html", "404.html"):
            continue
        s = open(os.path.join(dp, f), encoding="utf-8").read()
        cnt.update(TW.contexts(" ".join(re.findall(r">([^<>]+)<", TAG.sub("", s)))))
p = os.path.join(HERE, "tw_allow.txt")
hdr = "".join(l for l in open(p, encoding="utf-8")) if os.path.exists(p) else ""
hdr = "".join(l for l in hdr.splitlines(True) if l.startswith("#"))
if not hdr:
    hdr = ("# 繁体转换里「两种写法都讲得通」的字，逐处登记过的三字上下文。\n"
           "# 这不是词典，是**审阅记录**：每一行表示「这处我看过，是对的」。\n"
           "# 日常的新上下文由日更的模型审（pipeline/twreview.py → data/tw-review.json），这里是人工记录。\n")
open(p, "w", encoding="utf-8").write(hdr + "\n".join(sorted(cnt)) + "\n")
print("白名单 %d 条 → pipeline/tw_allow.txt" % len(cnt))
