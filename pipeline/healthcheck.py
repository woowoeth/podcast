#!/usr/bin/env python3
"""体检：把"坏了"变成一个会自己喊出来的信号。

写这个脚本的原因很具体。过去这些故障，**全部**是被人问起才发现的：

  · Pages 部署卡在 Upload artifact 20 分钟，代码推了站上没变
  · 本机定时任务装好之后 runs = 0，一次都没跑过，每天静默失败
  · bot 的提交把源码回退了，提交信息写的是 "digest + build"
  · 日更 cron 整轮失败，站上不增不减，看起来像"今天没内容"
  · 推送重试 8 次全撞在同一个未合并冲突上

它们的共同点不是难修，是**没有信号**。所以这里只做一件事：把可观测的不变量列
出来，破了就非零退出并说清是哪一条。谁来跑它、坏了通知谁，交给 watch.yml。

判据分两类，故意分开：
  硬伤（exit 1）  内容停更、构建不一致、线上和仓库不一致、某条线心跳断了
  提醒（exit 0）  快到阈值、单个信源连续失败——值得看一眼，但不该半夜报警

用法：
    python3 pipeline/healthcheck.py              # 本地跑，只查文件
    python3 pipeline/healthcheck.py --online     # 连线上一起查（CI 用这个）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SITE = "https://ourword.ai/podcast"

# 两条线的班次：云端一天三班、本机一天两班。阈值给到两倍班距再加点余量——
# 目的是"断了要知道"，不是"晚了半小时就吵"。
CLOUD_MAX_H = 16
LOCAL_MAX_H = 30
# 内容停更多久算异常。周末信源本来就少，给三天。
CONTENT_MAX_H = 72


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse(ts: str | None) -> dt.datetime | None:
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _hours_since(ts: str | None) -> float | None:
    d = _parse(ts)
    return None if d is None else (now() - d).total_seconds() / 3600


def _get(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "ourword-healthcheck"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


class Report:
    def __init__(self) -> None:
        self.bad: list[str] = []
        self.warn: list[str] = []
        self.ok: list[str] = []

    def fail(self, msg: str) -> None:
        self.bad.append(msg)

    def note(self, msg: str) -> None:
        self.warn.append(msg)

    def good(self, msg: str) -> None:
        self.ok.append(msg)


# ------------------------------------------------------------------ 各项检查

def check_heartbeats(r: Report) -> None:
    """两条线各自还活着吗。

    这一条是专门为"本机定时任务 runs = 0"那次故障加的：任务装上了、列表里也在，
    但一次都没触发过，而没有任何东西会告诉你。现在每轮跑批写一份心跳，
    心跳停了就是这条线停了。
    """
    for line, limit, who in (("cloud", CLOUD_MAX_H, "云端 GitHub Actions"),
                             ("local", LOCAL_MAX_H, "本机 launchd")):
        f = DATA / f"heartbeat-{line}.json"
        if not f.exists():
            r.fail(f"{who}：没有心跳文件 {f.relative_to(ROOT)}——这条线从没跑过")
            continue
        try:
            hb = json.loads(f.read_text())
        except Exception as ex:
            r.fail(f"{who}：心跳文件读不出来（{type(ex).__name__}）")
            continue
        h = _hours_since(hb.get("at"))
        if h is None:
            r.fail(f"{who}：心跳里没有可解析的时间戳")
        elif h > limit:
            r.fail(f"{who}：{h:.0f} 小时没有心跳（阈值 {limit}h）"
                   f"，最后一次 {hb.get('at')}")
        else:
            if h > limit * 0.75:
                r.note(f"{who}：{h:.0f} 小时没跑，快到 {limit}h 阈值了")
            r.good(f"{who}：{h:.1f} 小时前跑过"
                   f"（发布 {hb.get('published', '?')} 篇，退出码 {hb.get('exit', '?')}）")
        if hb.get("exit") not in (0, "0", None):
            r.fail(f"{who}：最后一轮退出码 {hb.get('exit')}")


def check_content_freshness(r: Report) -> None:
    """内容还在更新吗。四道闸门可能把一整轮都拦下来，那是正常的；
    但连着三天一篇都没有，说明不是内容问题就是管线问题。"""
    eps = list((DATA / "episodes").glob("*.json"))
    if not eps:
        r.fail("data/episodes 是空的")
        return
    newest = None
    for f in eps:
        try:
            g = json.loads(f.read_text()).get("generated")
        except Exception:
            continue
        d = _parse(g)
        if d and (newest is None or d > newest):
            newest = d
    if newest is None:
        r.fail("没有一篇带得出时间的 generated 字段")
        return
    h = (now() - newest).total_seconds() / 3600
    if h > CONTENT_MAX_H:
        r.fail(f"内容停更 {h:.0f} 小时（阈值 {CONTENT_MAX_H}h），最新一篇 "
               f"{newest.isoformat()}")
    else:
        r.good(f"内容 {h:.0f} 小时前更新过，共 {len(eps)} 篇")


def check_build_consistency(r: Report) -> None:
    """仓库里三个数字必须相等：数据、正文页、分享短链。

    不等就说明有一轮跑批用了 --no-build 之后没人重建，或者重建被中断了——
    这两种都真发生过。
    """
    n_data = len(list((DATA / "episodes").glob("*.json")))
    n_pages = len([d for d in (ROOT / "p").iterdir() if d.is_dir()]) \
        if (ROOT / "p").exists() else 0
    n_alias = len([d for d in (ROOT / "e").iterdir() if d.is_dir()]) \
        if (ROOT / "e").exists() else 0
    if n_data == n_pages == n_alias:
        r.good(f"构建一致：数据／正文页／短链都是 {n_data}")
    else:
        r.fail(f"构建不一致：数据 {n_data} · 正文页 {n_pages} · 短链 {n_alias}"
               f"——跑一次 python3 pipeline/build.py")


def check_sources(r: Report) -> None:
    """信源清单本身的健康度。连续失败的源该被策展降级，但策展三天一次，
    中间这段时间至少要能看见。"""
    try:
        srcs = json.loads((DATA / "sources.json").read_text()).get("sources") or []
    except Exception as ex:
        r.fail(f"sources.json 读不出来（{type(ex).__name__}）")
        return
    # blocked_here 是"机房 IP 取不到、本机线负责"，不是抓取异常
    dead = [s for s in srcs if (s.get("status") or {}).get("ok") is False
            and not (s.get("status") or {}).get("blocked_here")]
    streak = [s for s in srcs if (s.get("status") or {}).get("fail_streak", 0) >= 2]
    r.good(f"信源 {len(srcs)} 档")
    if dead:
        r.note(f"{len(dead)} 档抓取异常：" + "、".join(s["name"] for s in dead[:6]))
    if streak:
        r.note(f"{len(streak)} 档连续失败 ≥2 次，再失败一次会被移除："
               + "、".join(s["name"] for s in streak[:6]))


def check_online(r: Report) -> None:
    """线上和仓库是不是同一个版本。

    这一条是为 Pages 卡死那次加的：代码推上去了、CI 绿了，但站上还是旧的，
    而唯一的症状是"用户觉得没发布"。
    """
    n_data = len(list((DATA / "episodes").glob("*.json")))
    try:
        home = _get(SITE + "/")
    except Exception as ex:
        r.fail(f"首页取不到：{type(ex).__name__} {str(ex)[:60]}")
        return
    m = re.search(r"(\d+)\s*篇深读", home)
    if not m:
        r.fail("首页上找不到篇数——模板变了还是页面坏了？")
    else:
        live = int(m.group(1))
        if live != n_data:
            r.fail(f"线上 {live} 篇，仓库 {n_data} 篇——推上去了但没部署，"
                   f"或者部署卡住了")
        else:
            r.good(f"线上和仓库一致：{live} 篇")
    for path in ("/sources/", "/log/", "/feed.xml", "/sitemap.xml",
                 "/llms.txt", "/robots.txt"):
        try:
            _get(SITE + path, timeout=20)
            r.good(f"{path} 可访问")
        except Exception as ex:
            r.fail(f"{path} 取不到：{type(ex).__name__}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--online", action="store_true", help="连线上一起查")
    a = ap.parse_args(argv)

    r = Report()
    check_heartbeats(r)
    check_content_freshness(r)
    check_build_consistency(r)
    check_sources(r)
    if a.online:
        check_online(r)

    print(f"体检 · {now().isoformat(timespec='seconds')}")
    for line in r.ok:
        print(f"  ok    {line}")
    for line in r.warn:
        print(f"  注意  {line}")
    for line in r.bad:
        print(f"  坏了  {line}")
    print(f"\n{len(r.bad)} 项硬伤 · {len(r.warn)} 项提醒 · {len(r.ok)} 项正常")
    if r.bad:
        # GitHub Actions 会把 ::error:: 高亮出来，也是 watch.yml 建 issue 的依据
        for line in r.bad:
            print(f"::error::{line}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
