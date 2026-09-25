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


def _rev() -> dict:
    """这一轮跑的是哪一版代码。

    **没有指纹，「我改了」和「它在跑」是两件互不相关的事。**
    实测：改了一整天（轮转、剔除规则、吞吐预算共 14 个提交），而真正执行的
    那份副本停在当天早上的commit —— 它在每轮开头才 git pull，所以下一轮才会
    看到。这一整天里，「本机线的建档预算是 24」只在我的工作区里成立。
    心跳不记版本的话，从外面根本看不出来这件事。

    取不到就不写这几个字段（比如那份副本不是 git 仓库）—— 宁可没有，
    不要写一个假的。
    """
    import subprocess
    out = {}
    try:
        r = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            out["rev"] = r.stdout.strip()
    except Exception:
        return out
    try:
        r = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain",
                            "--", "pipeline", "scripts"],
                           capture_output=True, text=True, timeout=10)
        # 只看代码目录：data/ 和构建产物每轮都在变，拿它们判「有没有本地改动」
        # 会永远是 dirty，等于没判。
        if r.returncode == 0:
            out["dirty"] = bool(r.stdout.strip())
    except Exception:
        pass
    return out


def write(line: str, exit_code: int, published: int | None = None,
          why: str | None = None, llm: str | None = None) -> pathlib.Path:
    eps = len(list((ROOT / "data" / "episodes").glob("*.json")))
    rec = {
        "at": dt.datetime.now(dt.timezone.utc)
              .isoformat(timespec="seconds").replace("+00:00", "Z"),
        "line": line,
        "exit": int(exit_code),
        "episodes": eps,
    }
    rec.update(_rev())
    if published is not None:
        rec["published"] = int(published)
    if llm:
        rec["llm"] = llm      # "off"：这条线有意不跑模型（比如云端没有凭据），不是故障
    if why:
        # 非零退出时写清楚**为什么**。只有退出码的话，体检只能说"这条线坏了"，
        # 说不出"坏在哪"，而排查要从头看一遍日志。
        rec["why"] = why
    p = ROOT / "data" / f"heartbeat-{line}.json"
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=1) + "\n")
    return p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("line", choices=("local", "cloud"))
    ap.add_argument("exit_code", nargs="?", default="0")
    ap.add_argument("--published", type=int, default=None)
    ap.add_argument("--why", default=None, help="非零退出时，一句话说明原因")
    ap.add_argument("--llm", default=None, help="off = 这条线有意不跑模型（不是故障）")
    a = ap.parse_args(argv)
    try:
        code = int(a.exit_code)
    except ValueError:
        code = 1
    p = write(a.line, code, a.published, a.why, a.llm)
    # 打出来：心跳失效过一次就是因为它一声不响
    print(f"心跳已写 {p.relative_to(ROOT)}（退出码 {code}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
