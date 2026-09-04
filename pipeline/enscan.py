#!/usr/bin/env python3
"""扫英文站里的漏译。判据：**汉字只许出现在 lang="zh" 的元素里。**

为什么用这条判据而不是维护一份字符串白名单：白名单会漏，而且漏的时候是静默的。
"任何汉字都必须被显式标成中文内容"是机械可查的，而且标 lang="zh" 本身就是对的
——节目名（张小珺·商业访谈录）、说话人名、中文源节目的金句原文，都该标，
对屏幕阅读器和字体选择也都有用。

漏一条界面文案，构建就失败。不会交出一个中英混排的半成品。
"""
from __future__ import annotations

import collections
import pathlib
import re

CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_SCRIPT = re.compile(r"<(script|style)[\s\S]*?</\1>", re.I)
_ZH_EL = re.compile(r'<(\w+)([^>]*\blang="zh"[^>]*)>[\s\S]*?</\1>')


def leaks(root: pathlib.Path) -> collections.Counter:
    hits: collections.Counter = collections.Counter()
    for f in sorted(root.rglob("*.html")):
        h = f.read_text()
        h = _SCRIPT.sub(" ", h)
        h = _ZH_EL.sub(" ", h)
        text = re.sub(r"<[^>]+>", "\x01", h)
        for seg in re.split(r"[\x01\n]+", text):
            seg = seg.strip()
            if seg and CJK.search(seg):
                hits[seg[:60]] += 1
    return hits


if __name__ == "__main__":
    import sys
    c = leaks(pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "en"))
    print(f"漏译片段 {len(c)} 种")
    for k, n in c.most_common(40):
        print(f"  x{n:<4} {k}")
    raise SystemExit(1 if c else 0)
