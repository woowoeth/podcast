#!/usr/bin/env python3
"""跑批锁 data/.run.lock：同一时刻只许一个 run.py 写 data/。

run.py 拿它（拿不到就退出，见 run.acquire_run_lock）。日更开头用这个脚本**只等不拿**：

    python3 pipeline/runlock.py 180      # 最多等 180 分钟，等到锁空出来就返回 0，超时返回 1

为什么要等：补存量（scripts/backfill-source.sh）一批要一个多钟头，日更撞上它就「拿不到锁、
这一轮跳过」，什么都不提交；同时 pull／commit 还会把它正在写的 state.json 写乱。原来靠补存量
在日更前后各让出两个多钟头来躲，GPU 一天闲 4.5 小时（2026-10-10 补罗永浩全集时改成按顺序来）。

单独成一个脚本、不走 run.py --wait-lock：守护靠「run.py 有没有被调用」判断这一轮有没有去深读，
等锁不该被算成一次深读。
"""
from __future__ import annotations

import fcntl
import pathlib
import sys
import time

LOCK_FILE = pathlib.Path(__file__).resolve().parent.parent / "data" / ".run.lock"


def wait_free(minutes: float, poll: float = 20.0) -> bool:
    """等到没有别人占着锁，然后放开返回 True；超时返回 False。只等、不占。"""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + minutes * 60
    while True:
        with open(LOCK_FILE, "a") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                return True
            except OSError:
                pass
        if time.time() >= deadline:
            return False
        time.sleep(min(poll, max(0.01, deadline - time.time())))


if __name__ == "__main__":
    mins = float(sys.argv[1]) if len(sys.argv) > 1 else 180.0
    ok = wait_free(mins)
    print("跑批锁空着" if ok else f"等了 {mins:g} 分钟，跑批锁还被占着")
    sys.exit(0 if ok else 1)
