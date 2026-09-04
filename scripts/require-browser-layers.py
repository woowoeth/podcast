#!/usr/bin/env python3
"""这两层浏览器检查必须**真的跑了**，不是被跳过了。

为什么单独一支：依赖缺失时 unittest 会静默 skip——"跳过了整层浏览器检查"
和"全过"在输出上分不出来，而 CI 里还套着 `| tail -40`。

这两层是仓库里唯一会打开真实页面的检查：
  tests/test_render.py      页面长得对不对（布局位移、hidden 有没有藏住、
                            封面比例、图有没有真解码、首屏体积、两套主题的
                            对比度到不到 WCAG AA）
  tests/test_walkthrough.py 点下去有没有发生该发生的事（搜索、清空、
                            分类筛选、加载更多、时间戳、分享的两条路径、
                            JS 运行时文案跟不跟语言、有没有 JS 报错）

他报过一轮 11 个问题，10 个在渲染层，而那时仓库里 300 多项守护没有一项
打开过真实页面。这一层不能悄悄消失。
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
LAYERS = (("tests.test_render", "渲染层"),
          ("tests.test_walkthrough", "读者走查"))


def main() -> int:
    bad: list[str] = []
    for mod, why in LAYERS:
        r = subprocess.run([sys.executable, "-m", "unittest", mod, "-v"],
                           cwd=ROOT, capture_output=True, text=True)
        out = r.stdout + r.stderr
        m = re.search(r"^Ran (\d+) test", out, re.M)
        ran = int(m.group(1)) if m else 0
        skipped = len(re.findall(r"\bskipped\b", out))
        print(f"  {why}（{mod}）：跑了 {ran} 条，跳过 {skipped} 处")
        if ran == 0:
            bad.append(f"{why}一条都没跑")
        elif "没装 playwright" in out:
            bad.append(f"{why}因为没装 playwright 被整层跳过"
                       f"（python3 -m pip install playwright && "
                       f"python3 -m playwright install chromium）")
        if r.returncode != 0:
            print(out[-2500:])
            bad.append(f"{why}有不通过的项")
    if bad:
        msg = "；".join(bad)
        print(f"::error::{msg}")
        print(f"\n不通过：{msg}")
        return 1
    print("两层浏览器检查都真的跑了，全过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
