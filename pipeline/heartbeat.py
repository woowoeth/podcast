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
    try:
        # **这一轮开跑时看见的远端是哪一版。** 两条线都在开头同步（本机 git pull、云端 checkout），
        # 之后到写心跳之间没有别的 fetch，所以此刻的 origin/main 就是开跑时的那一版。
        # 体检拿它判「pull 有没有起作用」：比它旧的提交没跑到才是故障，比它新的只是还没轮到。
        # 原来只能拿心跳时间（**跑完**的时刻）去比提交时间，于是跑批中途别的线推一个提交，
        # 体检就报「这条线的 git pull 没起作用」（2026-09-28 21:30 那轮：13:30 拉、13:42 fast lane 推、
        # 13:51 写心跳）。
        r = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "origin/main"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            out["synced"] = r.stdout.strip()
    except Exception:
        pass
    return out


def _autostash() -> int | None:
    """这份副本的 git stash 里有几个 autostash。

    `git pull --rebase --autostash` 恢复时冲突，退出码仍是 0，stash 留着 —— 本机那份副本
    就这样一声不响堆到 26 个，而每个 stash 都可能是某一轮改动的唯一一份。
    写进心跳，体检才看得见（check_heartbeats）。取不到就不写。
    """
    import subprocess
    try:
        r = subprocess.run(["git", "-C", str(ROOT), "stash", "list"],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return sum(1 for l in r.stdout.splitlines() if l.rstrip().endswith("autostash"))


def cloud_down(path="data/heartbeat-cloud.json", max_age_h: float = 10) -> str:
    """云端这条线还在出稿吗？在出返回空串，停了返回一句原因。

    本机线决定要不要接班（local-daily.sh 的 CLOUD_DOWN）、体检判「住宅之外那批有没有人管」
    （check_every_source_has_a_producer）**用的是这同一个函数**。原来两边各写一份：本机按
    「深读关着 / 最后一轮失败 / 10 小时没心跳」判，体检只认「深读关着」—— 云端因为别的原因停了、
    本机又没接全站时，体检一声不响。
    """
    import datetime as _d
    try:
        h = json.loads(pathlib.Path(path).read_text())
    except Exception:
        return "没有云端心跳"
    if h.get("llm") == "off":
        return "云端深读关着（%s）" % (h.get("why") or "没有凭据")
    if h.get("exit") not in (0, "0", None):
        return "云端最后一轮失败（%s）" % (h.get("why") or "退出码 %s" % h.get("exit"))
    try:
        at = _d.datetime.fromisoformat(str(h["at"]).replace("Z", "+00:00"))
        age = (_d.datetime.now(_d.timezone.utc) - at).total_seconds() / 3600
    except Exception:
        return "云端心跳读不出时间"
    if age > max_age_h:
        return "云端 %.0f 小时没有心跳" % age
    return ""


def write(line: str, exit_code: int, published: int | None = None,
          why: str | None = None, llm: str | None = None,
          scope: str | None = None) -> pathlib.Path:
    eps = len(list((ROOT / "data" / "episodes").glob("*.json")))
    rec = {
        "at": dt.datetime.now(dt.timezone.utc)
              .isoformat(timespec="seconds").replace("+00:00", "Z"),
        "line": line,
        "exit": int(exit_code),
        "episodes": eps,
    }
    rec.update(_rev())
    if line == "local":
        n = _autostash()
        if n is not None:
            rec["autostash"] = n
    if published is not None:
        rec["published"] = int(published)
    if llm:
        rec["llm"] = llm      # "off"：这条线有意不跑模型（比如云端没有凭据），不是故障
    if scope:
        rec["scope"] = scope  # all / residential / only：全站、只有住宅 IP 那批、手动 --only 跑的几档
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
    ap.add_argument("--scope", default=None, choices=("all", "residential", "only"),
                    help="这一轮管的信源范围")
    a = ap.parse_args(argv)
    try:
        code = int(a.exit_code)
    except ValueError:
        code = 1
    p = write(a.line, code, a.published, a.why, a.llm, a.scope)
    # 打出来：心跳失效过一次就是因为它一声不响
    print(f"心跳已写 {p.relative_to(ROOT)}（退出码 {code}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
