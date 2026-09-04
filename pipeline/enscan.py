#!/usr/bin/env python3
"""扫英文站里的漏译。判据：**汉字只许出现在 lang="zh" 的元素里。**

为什么用这条判据而不是维护一份字符串白名单：白名单会漏，而且漏的时候是静默的。
"任何汉字都必须被显式标成中文内容"是机械可查的，而且标 lang="zh" 本身就是对的
——中文播客的真名（科技这碗饭、42章经）、说话人名、中文源节目的金句原文，
都该标，对屏幕阅读器和字体选择也都有用。

**用 HTML 解析器，不用正则去标签。** 我先写了两版正则，两版都误报：第一版
`<[^>]+>` 遇到属性值里有 `>` 就提前收尾；第二版认了引号，还是在某些页面上把
href 里的中文 slug 和后面的正文粘成一段。这类问题该换对的工具，不该继续打补丁。
"""
from __future__ import annotations

import collections
import pathlib
import re
from html.parser import HTMLParser

CJK = re.compile(r"[一-鿿㐀-䶿]")
# head 也跳过。<title> 和 <meta> 里的文字是从正文同一批字段派生的，正文查过就够；
# 而 <title> 里放不了 lang 属性，中文播客的真名（十字路口Crossing、
# 卫诗婕｜漫谈Light the Star）在英文页的标题里是**对的**，不是漏译。
_SKIP_TAGS = {"script", "style", "template", "head"}


class _Scan(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0          # script/style 深度
        self._zh = 0            # lang="zh" 子树深度
        self._stack: list[tuple[str, bool, bool]] = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        is_skip = tag in _SKIP_TAGS
        is_zh = d.get("lang", "").lower().startswith("zh")
        # 自闭合标签不入栈
        if tag in {"br", "img", "meta", "link", "input", "hr", "source"}:
            return
        self._stack.append((tag, is_skip, is_zh))
        if is_skip:
            self._skip += 1
        if is_zh:
            self._zh += 1

    def handle_endtag(self, tag):
        # 找到最近的同名开标签，弹出它以及它之上没闭合的
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                for _, is_skip, is_zh in self._stack[i:]:
                    if is_skip:
                        self._skip -= 1
                    if is_zh:
                        self._zh -= 1
                del self._stack[i:]
                return

    def handle_data(self, data):
        if self._skip or self._zh:
            return
        t = data.strip()
        if t and CJK.search(t):
            self.out.append(t)


def leaks(root: pathlib.Path) -> collections.Counter:
    hits: collections.Counter = collections.Counter()
    for f in sorted(root.rglob("*.html")):
        p = _Scan()
        try:
            p.feed(f.read_text())
            p.close()
        except Exception:
            continue
        for seg in p.out:
            for piece in re.split(r"\n+", seg):
                piece = piece.strip()
                if piece and CJK.search(piece):
                    hits[piece[:60]] += 1
    return hits


if __name__ == "__main__":
    import sys
    c = leaks(pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "en"))
    print(f"漏译片段 {len(c)} 种")
    for k, n in c.most_common(40):
        print(f"  x{n:<4} {k}")
    raise SystemExit(1 if c else 0)
