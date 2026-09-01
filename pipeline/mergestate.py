#!/usr/bin/env python3
"""data/state.json 的 git 合并驱动。

为什么需要它：state.json 是唯一一个两条线都会改、且必然冲突的数据文件（每集的
JSON 文件名唯一，永不冲突；生成产物一律重建）。云端和本机同时跑的时候，git 的
三方合并会在这个文件上留下冲突标记，而推送重试循环里的 `git pull --rebase`
从此每次都报 "Pulling is not possible because you have unmerged files"——
**8 次重试全撞在同一面墙上，永远恢复不了**。真出过一次，那轮的产出全废。

合并规则本身很清楚，因为三张表都是并集：

    done  已发布：{id: {at, slug}}      并集。同一个 id 两边都有就是同一集，取谁都对
    fail  失败计数：{id: {n, soft, ...}} 并集，计数取 max——重试预算宁可少给不可多给
    fp    指纹去重：{fingerprint: id}    并集

用法（git 会自己带参数调用）：
    git config merge.podcast-state.driver \\
        'python3 pipeline/mergestate.py %O %A %B'
    # .gitattributes 里：data/state.json merge=podcast-state

约定：把结果写回 %A，退出码 0 表示合并成功。任何一侧读不出来（比如空文件）就
当成空表，而不是让整轮跑批死在这里——丢几个计数不影响正确性，reconcile() 每轮
开头都会拿磁盘上的 data/episodes 重建 done。
"""
from __future__ import annotations

import json
import pathlib
import sys


def _load(p: str) -> dict:
    try:
        d = json.loads(pathlib.Path(p).read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _merge_fail(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        if k not in out:
            out[k] = v
            continue
        cur = dict(out[k]) if isinstance(out[k], dict) else {}
        new = v if isinstance(v, dict) else {}
        # 计数取大：一集在两边各失败过，说明它真的失败了那么多次
        for f in ("n", "soft"):
            cur[f] = max(int(cur.get(f) or 0), int(new.get(f) or 0))
        for f, val in new.items():
            if f not in ("n", "soft") and f not in cur:
                cur[f] = val
        out[k] = cur
    return out


def merge(ours: dict, theirs: dict) -> dict:
    out: dict = {}
    for table in ("done", "fp"):
        out[table] = {**(ours.get(table) or {}), **(theirs.get(table) or {})}
    out["fail"] = _merge_fail(ours.get("fail") or {}, theirs.get("fail") or {})
    # 认不出的顶层键也别丢：以后加了新表，合并驱动不该静默把它抹掉
    for side in (ours, theirs):
        for k, v in side.items():
            if k not in out:
                out[k] = v
    return out


def merge_heartbeat(ours: dict, theirs: dict) -> dict:
    """心跳：取 `at` 更新的那一份。

    心跳是「某条线某一刻还活着」这个事实，不是累积状态——两边都改就是两次运行，
    留晚的那次即可。不给它驱动的话，两条线各写自己的心跳文件、却在同一次合并里
    相遇，工作区会留下冲突标记，之后本机线**每天照跑、心跳照写，但提交和推送
    全被挡住**，而日志里只有一行 "unmerged files"。真卡了一天。
    """
    a, b = ours.get("at") or "", theirs.get("at") or ""
    return theirs if b > a else ours


def _is_heartbeat(d: dict) -> bool:
    return "at" in d and "line" in d


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("用法: mergestate.py %O %A %B", file=sys.stderr)
        return 2
    _base, ours_path, theirs_path = argv[0], argv[1], argv[2]
    o, t = _load(ours_path), _load(theirs_path)
    merged = merge_heartbeat(o, t) if (_is_heartbeat(o) or _is_heartbeat(t)) \
        else merge(o, t)
    pathlib.Path(ours_path).write_text(
        json.dumps(merged, ensure_ascii=False, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
