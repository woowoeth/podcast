#!/usr/bin/env python3
"""每一篇「再也不会被尝试」的集，都要有人看过并写下原因。

**为什么要有这个文件。**
重试有上限（MAX_FAILS=3 / MAX_SOFT_FAILS=8），撞满之后 candidates() 直接跳过 ——
那一集从此消失，没有任何地方会再提它一句。实测某一刻躺着 24 篇这样的，
其中 5 篇是 `n=0 soft=8`：一次真失败都没有，纯粹是基础设施抖了八次
（包括硅谷101 那集 1:16:50 的 mRNA 疫苗访谈）。
还有 18 篇是被一个已经修好的 bug 判死的（切音频没丢内嵌封面图），
bug 修完了也没人去捞。

**「发不出去」本身不是问题，悄悄发不出去才是。**
所以这里不拦「有多少篇死了」——有些集确实哪儿都找不到文稿。
这里拦的是「死了却没人看过」：体检会为每一篇没有记录在案的死亡报硬伤，
要么把它救活，要么写下为什么放弃。两条路都行，唯独不许当没看见。

    python3 pipeline/giveup.py                     # 列出没人看过的
    python3 pipeline/giveup.py --accept KEY --why "…"
    python3 pipeline/giveup.py --accept-all --why "…"
    python3 pipeline/giveup.py --forget KEY        # 撤回，让它重新参与重试
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LEDGER = DATA / "gave-up.json"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load() -> dict:
    try:
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save(d: dict) -> None:
    LEDGER.write_text(json.dumps(d, ensure_ascii=False, indent=1, sort_keys=True) + "\n")


def dead(state: dict | None = None) -> dict[str, dict]:
    """当前「再也不会被尝试」的集：键 → 失败记录。"""
    sys.path.insert(0, str(ROOT / "pipeline"))
    import run as R
    if state is None:
        try:
            state = json.loads((DATA / "state.json").read_text(encoding="utf-8"))
        except Exception:
            return {}
    out = {}
    for k, v in (state.get("fail") or {}).items():
        # **只算「再也不会被尝试」的。**
        # 软失败撞满上限之后会过期（SOFT_COOLS_AFTER_DAYS 天后清零重来），
        # 那是「这几天挂起」，不是「永久出局」。
        # 把它们也算进来，这道检查就会为一件会自愈的事天天报硬伤 ——
        # 检查喊狼，下次就没人认真看它了。
        if v.get("n", 0) < R.MAX_FAILS:
            continue
        # **「撞满上限」不等于「再也不会被尝试」。**
        # run.py 对 no-transcript 的记录还有一条：记这条判决时可用的取稿层
        # 比现在少（云端没有 ASR），那它说明不了什么，下一轮照样重试。
        # 这里原来不看这一条，于是对着 9 篇**下一轮就会被重试**的集报硬伤，
        # 其中 6 篇正是刚改派到本机线的那批 —— 改派是在救它们，
        # 而体检把这件事报成了「再也不会被尝试、没人看过」。
        # 检查喊狼，下次就没人认真看它了。判据直接调 run 里那个函数，
        # 不在这儿另写一遍。
        if "no-transcript" in str(v.get("why") or "") \
                and R._weaker_tiers(v.get("tiers")):
            continue
        # **源都已经不在册了，那不是「我们发不出去的集」，是退役源留下的残渣。**
        # 拿它报硬伤，只会让人学会忽略这个检查。
        if v.get("src") and v["src"] not in _registered():
            continue
        out[k] = v
    return out


def _registered() -> set[str]:
    try:
        return {s["id"] for s in
                json.loads((DATA / "sources.json").read_text())["sources"]}
    except Exception:
        return set()


def unaccepted(state: dict | None = None) -> dict[str, dict]:
    """死了、而且没人看过的。

    **同一个键换了死因要重新看一遍。** 记录里存下当时的 why；
    下次如果是因为别的原因死的，那是一件新事，不能拿旧的放行条盖过去。
    """
    seen = load()
    out = {}
    for k, v in dead(state).items():
        rec = seen.get(k)
        if not rec or rec.get("why_then") != str(v.get("why", "")):
            out[k] = v
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--accept", metavar="KEY", help="记下放弃这一篇")
    ap.add_argument("--accept-all", action="store_true", help="记下放弃所有没人看过的")
    ap.add_argument("--forget", metavar="KEY", help="撤回放弃，让它重新参与重试")
    ap.add_argument("--why", default="", help="为什么放弃（必填，给以后的人看）")
    a = ap.parse_args(argv)

    if a.forget:
        d = load()
        if d.pop(a.forget, None) is None:
            print(f"没有这条记录：{a.forget}", file=sys.stderr)
            return 1
        save(d)
        print(f"已撤回 {a.forget}，它会重新参与重试")
        return 0

    todo = unaccepted()
    if a.accept or a.accept_all:
        if not a.why.strip():
            print("要写 --why：为什么这一篇放弃了。"
                  "没有原因的放弃，下一个人（包括未来的我）没法判断该不该翻案。",
                  file=sys.stderr)
            return 2
        d = load()
        keys = [a.accept] if a.accept else list(todo)
        alive = dead()
        n = 0
        for k in keys:
            if k not in alive:
                print(f"  跳过 {k}：它现在并没有出局", file=sys.stderr)
                continue
            d[k] = {"at": _now(), "why": a.why.strip(),
                    "why_then": str(alive[k].get("why", "")),
                    "src": alive[k].get("src"), "title": alive[k].get("title")}
            n += 1
        save(d)
        print(f"记下放弃 {n} 篇 → data/gave-up.json")
        return 0

    alive = dead()
    print(f"出局 {len(alive)} 篇，其中没人看过的 {len(todo)} 篇")
    for k, v in sorted(todo.items(), key=lambda x: str(x[1].get("src"))):
        print(f"  {str(v.get('src')):16} n={v.get('n',0)} soft={v.get('soft',0)} "
              f"{str(v.get('why'))[:28]:30} {str(v.get('title'))[:40]}")
        print(f"      {k}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
