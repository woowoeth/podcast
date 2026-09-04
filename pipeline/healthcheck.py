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


def _commits_behind() -> int:
    """本地落后 origin/main 几个提交。

    体检把"线上篇数"和"本地仓库篇数"对比，这在 CI 里是对的（每次都是新 checkout），
    但在一份过期的本地副本上跑就会把"我没 pull"报成"部署卡住了"。
    不联网、不 fetch：只看已有的远端引用，拿不到就返回 0（宁可不报，不误报）。
    """
    import subprocess
    try:
        out = subprocess.run(["git", "rev-list", "--count", "HEAD..origin/main"],
                             cwd=ROOT, capture_output=True, text=True, timeout=10)
        return int((out.stdout or "0").strip() or 0)
    except Exception:
        return 0


# ------------------------------------------------------------------ 各项检查

def check_heartbeats(r: Report) -> None:
    """两条线各自还活着吗。

    通则：**凡是拿仓库里的状态和当前时间比的检查，都要先考虑仓库本身是不是过期的。**
    心跳文件在 git 里，所以一份落后几个提交的本地副本必然拿到旧心跳，会把
    "我没 pull"报成"这条线死了"。CI 里每次都是新 checkout，不受影响。

    这一条是专门为"本机定时任务 runs = 0"那次故障加的：任务装上了、列表里也在，
    但一次都没触发过，而没有任何东西会告诉你。现在每轮跑批写一份心跳，
    心跳停了就是这条线停了。
    """
    behind = _commits_behind()
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
        elif h > limit and behind:
            r.note(f"{who}：心跳 {h:.0f} 小时前，但本地落后 origin/main {behind} 个"
                   f"提交——先 git pull 再判断，这可能只是副本过期")
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
    if behind:
        r.note(f"本地落后 origin/main {behind} 个提交——上面凡是和时间有关的判断"
               f"都可能因此失真")


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
    # 文案必须和 curate.judge 的真实处置一致。改了规则却没改文案，告警就在说谎——
    # 而误导性的告警比没有告警更糟：它让人对下一次真告警也不当真。
    streak = [s for s in srcs if (s.get("status") or {}).get("fail_streak", 0) >= 2]
    to_local = [s for s in streak if not s.get("residential")]
    to_drop = [s for s in streak if s.get("residential")]
    r.good(f"信源 {len(srcs)} 档")
    if dead:
        r.note(f"{len(dead)} 档抓取异常：" + "、".join(s["name"] for s in dead[:6]))
    if to_local:
        r.note(f"{len(to_local)} 档连续失败 ≥2 次，再失败一次会改派本机线（不是移除）："
               + "、".join(s["name"] for s in to_local[:6]))
    if to_drop:
        r.note(f"{len(to_drop)} 档已在本机线且连续失败 ≥2 次，再失败一次会被移除："
               + "、".join(s["name"] for s in to_drop[:6]))


def check_videos(r: Report) -> None:
    """挂在正文里的视频，有多少还没核对过时间轴。

    为什么要报这一条：判据（视频时长必须和音频时长对得上）只有在真的跑过之后
    才写下 video_len，而守护测试是靠 video_len 离线复查全站的。核对没跑过，
    那条守护就是空转——而空转的守护比没有守护更糟，它让人以为查过了。
    """
    eps = list((DATA / "episodes").glob("*.json"))
    vids = unver = 0
    for f in eps:
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        if not d.get("youtube_id"):
            continue
        vids += 1
        q = (d.get("digest") or {}).get("quality") or {}
        if q.get("transcript_source") == "youtube":
            continue          # 文稿即字幕，时间轴天然一致，不需要核对
        if not d.get("video_len"):
            unver += 1
    if not vids:
        return
    if unver:
        r.note(f"{vids} 篇有内嵌视频，其中 {unver} 篇还没核对过时间轴"
               f"——跑 python3 pipeline/video.py --audit（时间戳跳错比没视频糟）")
    else:
        r.good(f"{vids} 篇内嵌视频，时间轴都核对过")


def check_point_headings(r: Report) -> None:
    """要点小标题是论断，还是节目目录的翻译。

    为什么要报这一条：用户说"展示的重点还不够核心"时，我才第一次去量这个——
    全站 2095 条里只有 16% 带否定/转折/断言标记，另外 84% 是名词短语式的话题名
    （「与长鑫合作意义」「第一笔钱投向哪里」「2017年的转折」）。判据已经写进
    digest.SYSTEM，但**判据只影响以后生成的**，而"以后生成的到底变好了没有"
    需要一个持续的数字，不能每次都等人来问。

    这是提醒，不是硬伤：单篇比例天然波动，而且它是内容判断，不该半夜报警。
    """
    import re as _re
    claim = _re.compile(r"不是.*而是|不|没|非|反而|其实|却|才|只|会|要|能")
    # 按稿子自己的 generated 排，**不是文件 mtime**：回填工具一跑，mtime 就变成
    # 回填顺序，"最近 30 篇"会立刻变成"最后被回填的 30 篇"，这个信号就失真了。
    rows = []
    for f in (DATA / "episodes").glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        hh = [bool(claim.search(p.get("h") or ""))
              for p in ((d.get("digest") or {}).get("points") or [])]
        if hh:
            rows.append((d.get("generated") or "", hh))
    rows.sort(key=lambda x: x[0], reverse=True)
    hs = [x for _, hh in rows for x in hh]
    recent = [x for _, hh in rows[:30] for x in hh]
    if not hs:
        return
    all_pct = sum(hs) / len(hs) * 100
    new_pct = (sum(recent) / len(recent) * 100) if recent else 0
    line = (f"要点小标题里是论断的：全站 {all_pct:.0f}%（{len(hs)} 条）、"
            f"最近 30 篇 {new_pct:.0f}%")
    # 40% 是从回填前后的实测差里定的：判据生效时单批能到 70% 上下，
    # 掉回 40% 以下说明提示词那节被改坏了或者被模型忽略了。
    if new_pct < 40:
        r.note(line + "——新稿掉回话题名了，检查 digest.SYSTEM 的要点判据那节")
    else:
        r.good(line)


def check_render_layer(r: Report) -> None:
    """渲染层体检这一层还在吗。

    为什么要报：它依赖 playwright，而依赖缺失时 unittest 会**静默 skip**——
    检查从"全过"变成"没跑"，输出上看不出区别。用户一轮报的 11 个问题里 10 个
    在渲染层，这一层不能悄悄消失。
    """
    f = ROOT / "tests" / "test_render.py"
    if not f.exists():
        r.fail("tests/test_render.py 不见了——渲染层体检没了")
        return
    try:
        import importlib.util
        have = importlib.util.find_spec("playwright") is not None
    except Exception:
        have = False
    n = len(re.findall(r"    def test_", f.read_text()))
    if have:
        r.good(f"渲染层体检 {n} 项可跑（playwright 在）")
    else:
        r.note(f"渲染层体检 {n} 项**跑不了**：没装 playwright。"
               f"装：python3 -m pip install playwright && "
               f"python3 -m playwright install chromium")


def check_online(r: Report) -> None:
    """线上和仓库是不是同一个版本。

    这一条是为 Pages 卡死那次加的：代码推上去了、CI 绿了，但站上还是旧的，
    而唯一的症状是"用户觉得没发布"。
    """
    n_data = len(list((DATA / "episodes").glob("*.json")))
    behind = _commits_behind()
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
        if live != n_data and behind:
            # 在过期的本地副本上跑就会这样：线上比本地新，那不是部署故障。
            # 我自己就被这条误报骗过一次，去查"部署卡住了"，实际是本地落后 3 个提交。
            r.note(f"线上 {live} 篇、本地仓库 {n_data} 篇，而本地落后 origin/main "
                   f"{behind} 个提交——先 git pull，这不是部署问题")
        elif live != n_data:
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
    check_videos(r)
    check_point_headings(r)
    check_render_layer(r)
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
