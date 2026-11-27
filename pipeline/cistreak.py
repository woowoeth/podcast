#!/usr/bin/env python3
"""main 上的 CI 是不是已经连着红了。

为什么需要它：CI 是推完才跑的，红了不拦部署，站照常更新——所以 2026-11-25 起
test_people 里三条测试在 CI 上连红两天（CI 机器上没有 claude，被「没有模型就不跑」
那道检查提前退出），体检一直是绿的，没有任何信号。CI 红着的时候，**新的真故障
也只是又一次红**，谁都看不出来。

判据：最近 STREAK 次跑完的 ci（只算 success / failure；cancelled、skipped 是并发
顶掉的，不算）全是 failure 才报。单次红可能是网络抖，报出来就是误报——
一道正常操作后必然红一阵的闸，两周内就会被学会忽略。

取不到数据（API 出错、一次都没跑完）不算绿：返回 2，让体检步骤红。

用法：gh api 'repos/OWNER/REPO/actions/workflows/ci.yml/runs?branch=main&status=completed&per_page=30' \\
        | python pipeline/cistreak.py
"""
from __future__ import annotations

import json
import sys

STREAK = 3


def verdict(runs: list[dict], streak: int = STREAK) -> tuple[int, str]:
    """runs 按时间新→旧。返回 (退出码, 一句话)。0 绿，1 连红，2 取不到。"""
    done = [r for r in runs if r.get("conclusion") in ("success", "failure")]
    if not done:
        return 2, "取不到 main 上跑完的 ci 记录——不能当成绿"
    head = done[:streak]
    if len(head) == streak and all(r["conclusion"] == "failure" for r in head):
        n = next((i for i, r in enumerate(done) if r["conclusion"] != "failure"), len(done))
        since = done[n - 1]
        more = "+" if n == len(done) else ""  # 取到的整页都红：实际可能更久
        return 1, (f"main 上的 ci 连续 {n}{more} 次失败，最早一次 {since.get('created_at', '?')}"
                   f"（{since.get('html_url', '')}）")
    return 0, f"main 上的 ci：最近一次 {done[0]['conclusion']}"


def main() -> int:
    try:
        runs = json.load(sys.stdin).get("workflow_runs") or []
    except Exception as e:  # noqa: BLE001
        print(f"::error::读不懂 ci 记录：{e}")
        return 2
    code, msg = verdict(runs)
    print(("::error::" if code else "") + msg)
    return code


if __name__ == "__main__":
    sys.exit(main())
