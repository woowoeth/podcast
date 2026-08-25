#!/usr/bin/env python3
"""写一行心跳：这条线跑过没有、跑成什么样。

为什么单独成文而不是内联 heredoc：第一版是 shell heredoc，嵌在一个被管道接走的
花括号块里。单独执行那段完全正常，真跑批时却一声不响地没写出文件——而心跳的
全部意义就是"没跑会被发现"，它自己静默失效等于白做。

用法：
    python3 pipeline/heartbeat.py local 0 --published 2
    python3 pipeline/heartbeat.py cloud "$?"
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def write(line: str, exit_code: int, published: int | None = None) -> pathlib.Path:
    eps = len(list((ROOT / "data" / "episodes").glob("*.json")))
    rec = {
        "at": dt.datetime.now(dt.timezone.utc)
              .isoformat(timespec="seconds").replace("+00:00", "Z"),
        "line": line,
        "exit": int(exit_code),
        "episodes": eps,
    }
    if published is not None:
        rec["published"] = int(published)
    p = ROOT / "data" / f"heartbeat-{line}.json"
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=1) + "\n")
    return p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("line", choices=("local", "cloud"))
    ap.add_argument("exit_code", nargs="?", default="0")
    ap.add_argument("--published", type=int, default=None)
    a = ap.parse_args(argv)
    try:
        code = int(a.exit_code)
    except ValueError:
        code = 1
    p = write(a.line, code, a.published)
    # 打出来：心跳失效过一次就是因为它一声不响
    print(f"心跳已写 {p.relative_to(ROOT)}（退出码 {code}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
