#!/usr/bin/env python3
"""班次：launchd 每小时 :30 拉起一次本机线，由这里判断这一次要不要真跑。

    python3 pipeline/slot.py decide --pid PID   定时拉起的：退出码 0 = 跑（stdout 打「第几次<TAB>原因」），
                                                **3** = 不用跑。别的非零 = 判断自己坏了，调用方退回老规矩
                                                （只在班次那一个钟头里跑），不许变成每小时跑一整轮。
    python3 pipeline/slot.py claim --pid PID    手动跑的：只占锁。已经有一轮在跑就退出 3，不跑。
    python3 pipeline/slot.py quiet WHY          模型用不了、而且这一班已经为同一个原因报过了：退出码 0
                                                = 这次别再推心跳、别再弹通知（每小时重试不刷屏）。

**为什么每小时。** 本机线是唯一能深读的线（云端没有 Claude 凭据），而它跑在一台笔记本上。
原来只在 10:30 / 21:30 两个点起（加开机补跑），**到点那一下没跑成，就整整等 11 个小时**：
  · 2026-10-12～14：两次重启、一次关机，站上停了两天半，每次开机后机器都闲着；
  · 2026-10-27～29：claude CLI 登录过期，用户 10-29 10:45 重新登录，下一班要到 21:30；
  · 2026-10-22：订阅周额度用完，10-23 19:00 就恢复了，而那几天任务又被禁用着。
现在每小时看一眼「最近一班跑成了没有」，没成就补；跑成了这一下什么都不做（不碰 git、不写日志）。

**判据。** 最近一班（SLOTS）之后本机心跳写过、而且退出码 0 = 那一班跑完了。心跳在主轮跑完就写，
主轮的稿那时已经先推上去；补课没跑完的部分留给下一班。没跑完的按失败分类重试：
  · 模型用不了（心跳退出码 4 / 6：没登录、额度用完）—— 每小时都试，不设上限。
    开跑前的探针只花一次 haiku 调用，用户一登录、额度一恢复，一小时内就接上；
  · 别的（同步失败、被重启打断、所有调用都失败……）—— 每班最多试 MAX_TRIES 次，
    再不成就留给下一班。通知每次失败都弹过了，不该再拿整轮跑批去撞同一面墙。

**锁。** 每小时起一次，就会撞上还没跑完的上一轮（主轮 + 补课常常两个小时）。launchd 不会给同一个任务
起第二份，但手动跑的那一份它不知道。锁 = .cache/daily.pid 里的 PID，**还活着、而且确实是
local-daily.sh** 才算占着 —— 只认 PID 加命令行，不按名字搜进程（pgrep -f 会命中查它的那条命令自己）。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import subprocess
import sys

SLOTS = ((10, 30), (21, 30))     # 正点班次。plist 每小时 :30 拉一次，分钟必须和这里一致
MAX_TRIES = 3
MODEL_DOWN = (4, 6)              # run.py 4 = 凭证中途被拒；local-daily.sh 6 = 探针探出模型用不了
SKIP = 3
HEARTBEAT = pathlib.Path("data/heartbeat-local.json")
STATE = pathlib.Path(".cache/slot.json")
LOCK = pathlib.Path(".cache/daily.pid")
TICKS = pathlib.Path(".cache/logs/slot.log")


def last_slot(now: dt.datetime) -> dt.datetime:
    """now（本地时间，带时区）之前最近的一个班次。"""
    for day in (0, 1):
        d = now - dt.timedelta(days=day)
        for h, m in sorted(SLOTS, reverse=True):
            s = d.replace(hour=h, minute=m, second=0, microsecond=0)
            if s <= now:
                return s
    raise AssertionError("unreachable")


def heartbeat() -> dict | None:
    try:
        d = json.loads(HEARTBEAT.read_text())
        d["at"] = dt.datetime.fromisoformat(str(d["at"]).replace("Z", "+00:00"))
        return d
    except Exception:
        return None


def _load(p: pathlib.Path) -> dict:
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def running(own: int | None = None) -> int | None:
    """锁里那个 PID 还活着、而且是 local-daily.sh，就返回它。"""
    try:
        pid = int(LOCK.read_text().split()[0])
    except Exception:
        return None
    if pid == own:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    return pid if "local-daily.sh" in cmd else None


def _cause(why: str | None) -> str:
    """心跳里的原因去掉括号里每次都不一样的原文：「同一个原因」按这一截比。"""
    return (why or "").split("（")[0].strip()


def decide(now: dt.datetime, hb: dict | None, state: dict, busy: int | None = None
           ) -> tuple[bool, int, str]:
    """(跑不跑, 这是这一班的第几次, 原因)。纯函数：时间、心跳、状态都从外面给。"""
    if busy:
        return False, 0, f"上一轮还在跑（pid {busy}），这一下不起"
    slot = last_slot(now)
    tag = f"{slot:%m-%d %H:%M} 那一班"
    tries = int(state.get("tries") or 0) if state.get("slot") == slot.isoformat() else 0
    if hb and hb["at"] >= slot:
        code = hb.get("exit")
        if code == 0:
            return False, tries, f"{tag}已经跑完（心跳 {hb['at'].astimezone():%H:%M}，退出码 0）"
        if code in MODEL_DOWN:
            return True, tries + 1, (f"{tag}模型用不了（{_cause(hb.get('why')) or f'退出码 {code}'}），"
                                     f"第 {tries + 1} 次：先探一下，能用了就接着跑")
    if tries >= MAX_TRIES:
        last = f"退出码 {hb.get('exit')}" if hb and hb["at"] >= slot else "没写出心跳（多半是被打断）"
        return False, tries, f"{tag}已经试了 {tries} 次都没成（最后一次{last}），留给下一班"
    if tries == 0:
        late = now - slot
        if late < dt.timedelta(minutes=10):
            return True, 1, f"{tag}：到点"
        return True, 1, f"{tag}没跑（到点时睡着、关着机，或被重启打断），晚了 {int(late.total_seconds() // 60)} 分钟，现在补"
    return True, tries + 1, f"{tag}上次没跑成，第 {tries + 1} 次（最多 {MAX_TRIES} 次）"


def quiet(now: dt.datetime, hb: dict | None, state: dict, why: str) -> bool:
    """这一班第二次以后、心跳已经记着同一个「模型用不了」的原因：这次只记日志。"""
    slot = last_slot(now)
    if state.get("slot") != slot.isoformat() or int(state.get("tries") or 0) < 2:
        return False
    return bool(hb and hb["at"] >= slot and hb.get("exit") in MODEL_DOWN
                and _cause(hb.get("why")) == _cause(why))


def _now() -> dt.datetime:
    if os.environ.get("NOW"):                        # 测试用
        return dt.datetime.fromisoformat(os.environ["NOW"]).astimezone()
    return dt.datetime.now().astimezone()


def _tick(now: dt.datetime, word: str, why: str) -> None:
    try:
        TICKS.parent.mkdir(parents=True, exist_ok=True)
        with TICKS.open("a") as f:
            f.write(f"{now:%Y-%m-%d %H:%M:%S}  {word}  {why}\n")
    except Exception:
        pass


def _claim(pid: int) -> None:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    LOCK.write_text(f"{pid}\n")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else ""
    pid = int(argv[argv.index("--pid") + 1]) if "--pid" in argv else os.getppid()
    now = _now()
    if cmd == "decide":
        state = _load(STATE)
        run, tries, why = decide(now, heartbeat(), state, running(pid))
        _tick(now, "run " if run else "skip", why)
        if not run:
            return SKIP
        _claim(pid)
        STATE.write_text(json.dumps({"slot": last_slot(now).isoformat(), "tries": tries,
                                     "at": now.isoformat(timespec="seconds"), "pid": pid},
                                    ensure_ascii=False) + "\n")
        print(f"{tries}\t{why}")
        return 0
    if cmd == "claim":
        busy = running(pid)
        if busy:
            print(f"已经有一轮在跑（pid {busy}），这次不跑：同一个仓库同一时间只能有一个写入者")
            return SKIP
        _claim(pid)
        return 0
    if cmd == "quiet":
        return 0 if quiet(now, heartbeat(), _load(STATE), " ".join(argv[1:])) else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
