#!/usr/bin/env python3
"""开机补跑：launchd 在登录时拉起（plist 的 RunAtLoad）这一轮，要不要跑。

退出码 0 = 跑；**3** = 不用跑（打印原因）。别的非零（脚本自己坏了）调用方照跑 ——
判断坏掉时宁可多跑一轮，也不能让整条线静默停掉。只在**开机 20 分钟内**才判断，
别的时候（定时触发、唤醒后补的那一班、手动 kickstart）一律照跑，不归它管。

**为什么要有它。** 本机线是唯一能深读的线（云端没有 Claude 凭据），而它跑在一台笔记本上。
StartCalendarInterval 只管「到点时睡着了，醒来补跑」，**关着机就整班错过**；
跑到一半被重启，也要等到下一个 10:30 / 21:30。2026-10-12 到 10-14：
10-12 11:22 重启打断上午那轮、20:51 关机错过 21:30、10-14 10:40 重启又打断一轮 ——
站上两天半没更新，而每次开机之后机器都闲着。

判据：最近一个班次（10:30 / 21:30）之后，本机心跳有没有写过。心跳在主轮跑完就写
（失败、拒绝深读也写），所以「心跳比那一班新」= 那一班的主轮跑过了；主轮的稿那时已经先推上去，
补课没跑完的部分留给下一班，不值得为它开机就再跑一轮。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys

SLOTS = ((10, 30), (21, 30))     # 和 scripts/com.ourword.podcast.plist 的 StartCalendarInterval 一致
BOOT_WINDOW_S = 20 * 60
HEARTBEAT = pathlib.Path("data/heartbeat-local.json")
SKIP = 3


def boot_age_s() -> float | None:
    if os.environ.get("BOOT_AGE_S"):                 # 测试用
        return float(os.environ["BOOT_AGE_S"])
    try:
        out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True,
                             text=True, timeout=5).stdout
        m = re.search(r"sec = (\d+)", out)
        if m:
            return dt.datetime.now().timestamp() - int(m.group(1))
    except Exception:
        pass
    try:
        return float(pathlib.Path("/proc/uptime").read_text().split()[0])
    except Exception:
        return None


def last_slot(now: dt.datetime) -> dt.datetime:
    """now（本地时间，带时区）之前最近的一个班次。"""
    for day in (0, 1):
        d = now - dt.timedelta(days=day)
        for h, m in sorted(SLOTS, reverse=True):
            s = d.replace(hour=h, minute=m, second=0, microsecond=0)
            if s <= now:
                return s
    raise AssertionError("unreachable")


def heartbeat_at() -> dt.datetime | None:
    try:
        at = json.loads(HEARTBEAT.read_text())["at"]
        return dt.datetime.fromisoformat(at.replace("Z", "+00:00"))
    except Exception:
        return None


def decide(now: dt.datetime, boot_age: float | None, hb: dt.datetime | None) -> tuple[bool, str]:
    if boot_age is None or boot_age > BOOT_WINDOW_S:
        return True, ""
    slot = last_slot(now)
    if hb is not None and hb >= slot:
        return False, f"开机补跑：{slot:%m-%d %H:%M} 那一班已经跑过（心跳 {hb.astimezone():%m-%d %H:%M}），不用补"
    return True, f"开机补跑：{slot:%m-%d %H:%M} 那一班没跑（或被重启 / 关机打断），现在补"


def main() -> int:
    now = dt.datetime.now().astimezone()
    if os.environ.get("NOW"):                        # 测试用
        now = dt.datetime.fromisoformat(os.environ["NOW"]).astimezone()
    run, why = decide(now, boot_age_s(), heartbeat_at())
    if why:
        print(why)
    return 0 if run else SKIP


if __name__ == "__main__":
    sys.exit(main())
