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


def check_english_edition(r: Report) -> None:
    """英文站做到哪一步了。

    为什么要报：它默认不建（界面文案层没做完，零漏译闸门会拦，而那个闸门挂在
    日常构建上会把简体站的部署一起挡住）。默认不建就意味着**它会被忘掉**，
    所以进度必须有个地方一直说着。
    """
    n_eps = len(list((DATA / "episodes").glob("*.json")))
    # 下划线开头的是**跨集共用的译名表**（_sources / _speakers），不是集。
    # 原来一并算进去，于是这行报"译文 275/273 篇"——超过总数的分数是假的，
    # 而假的分数会盖住真的缺口。
    n_tr = (len([f for f in (DATA / "en").glob("*.json")
                 if not f.name.startswith("_")]) if (DATA / "en").exists() else 0)
    ui = 0
    try:
        import sys as _s
        _s.path.insert(0, str(ROOT / "pipeline"))
        import i18n as _i
        ui = len(_i.UI)
    except Exception:
        pass
    if not n_tr:
        r.note("英文站：还没有译文。跑 python3 pipeline/translate.py")
        return
    leak = None
    en = ROOT / "en"
    if en.exists():
        try:
            import sys as _s2
            _s2.path.insert(0, str(ROOT / "pipeline"))
            import enscan
            leak = len(enscan.leaks(en))
        except Exception:
            leak = None
    line = f"英文站：译文 {n_tr}/{n_eps} 篇 · 界面文案表 {ui} 条"
    if leak:
        r.note(line + f" · **还有 {leak} 种中文漏在 lang=\"zh\" 之外，未完工**")
    elif leak == 0:
        r.good(line + " · 零漏译，可以上线")
    else:
        r.note(line + " · 还没建过，跑 python3 pipeline/build.py")


def check_parked_translations(r: Report) -> None:
    """有没有篇目因为连着译不合格被搁置了。

    搁置本身是对的（译不好就不上，别烧钱），但**它必须被看见**：
    简体和繁体有这一篇、英文没有，而构建输出只报"英文站 N 篇，零漏译"
    —— 它说的是"我建的这 N 篇都译全了"，不是"一篇都没少"。
    两个数差 1 的时候，输出上完全看不出来。
    """
    f = DATA / "translate-failed.json"
    if not f.exists():
        r.good("没有译不合格被搁置的篇目")
        return
    try:
        d = json.loads(f.read_text())
    except Exception as ex:
        r.fail(f"data/translate-failed.json 读不出来（{type(ex).__name__}）")
        return
    parked = {k: v for k, v in d.items() if (v or {}).get("n", 0) >= 3}
    trying = {k: v for k, v in d.items() if 0 < (v or {}).get("n", 0) < 3}
    if parked:
        # **个别篇目搁置是提醒，不是硬伤。** 搁置本身是对的（译不好不上，
        # 别烧钱），报成硬伤就等于把一篇顽固的稿子变成永久的推送闸门 ——
        # 这个仓库栽过一次：给新机制加的「上一轮零产出就报硬伤」刚建好就
        # 自己响了。判据的时间／数量尺度要和被测事物的波动尺度对齐。
        # 到 3 篇就是硬伤：那说明质量闸门和模型不匹配，不是个别篇目的事。
        line = (f"{len(parked)} 篇连着译不合格已搁置，英文站少这几篇："
                + "；".join(f"{k[:40]}（{(v or {}).get('why', '')[:50]}）"
                            for k, v in list(parked.items())[:3]))
        (r.fail if len(parked) >= 3 else r.note)(line)
    if trying:
        r.note(f"{len(trying)} 篇译不合格、还在重试："
               + "；".join(f"{k[:40]} 第 {(v or {}).get('n')} 轮"
                           for k, v in list(trying.items())[:3]))
    if not parked and not trying:
        r.good("没有译不合格被搁置的篇目")


def check_core_sources(r: Report) -> None:
    """核心源（tier 1）有没有被结构性地挡住。只查文件，不连线上。

    两条判据，都来自实测过的故障：

    **① 只能走本机线的核心源必须标 residential。** ASR 只有本机线有
    （云端没有 ASR key），而本机线只跑 residential=True 的源。没标的话
    云端会反复挑走、转不了、撞满 3 次上限**永久失败**。
    实测：ycsp（YC）已发布的 5 篇全靠 ASR／字幕、zhangxiaojun 6 篇全靠 ASR，
    两档都没标 —— 用户点名的正是这两档「发不全」。一共 9 档核心源如此，
    近 30 天核心源 108 集里 17 集因此卡在「取不到文稿」。

    **② 核心源的「不做」只能是广告。** 核心源值不值得做，在收源那一刻
    已经答过了；再用集级的分数拦一次，拦掉的都是「密度中等」这类理由。
    """
    try:
        raw = json.loads((DATA / "sources.json").read_text())
    except Exception:
        return
    d = raw["sources"] if isinstance(raw, dict) else raw
    # 范围是**优质源**（tier 1+2），不只核心源 —— 用户：「除了 yc 和张小珺，
    # 其他优质源也要检查是否全推」。实测扩到 tier 2 之后又抓出 32 档
    # 同样路由错的（ancients、throughline、ezra、restishistory…），
    # 全站有 98 集因此撞满 3 次上限、永久失败。
    core = [s for s in d if s.get("tier") in (1, 2)]
    if not core:
        return
    # ① 路由
    import re as _re
    def _local_only(u: str) -> bool:
        if not u:
            return False
        if "youtube.com/watch" in u:
            return True
        if _re.search(r"transcript|\.vtt|\.srt|/captions", u, _re.I):
            return False
        return bool(_re.search(r"\.mp3|\.m4a|\.aac|audio|traffic\.", u, _re.I))
    by = {}
    for f in (DATA / "episodes").glob("*.json"):
        try:
            e = json.loads(f.read_text())
        except Exception:
            continue
        if e.get("tier") in (1, 2) and e.get("source_id"):
            k = e["source_id"]
            n, loc = by.get(k, (0, 0))
            by[k] = (n + 1, loc + (1 if _local_only(e.get("transcript_url") or "") else 0))
    bad = [s["id"] for s in core
           if not s.get("residential") and by.get(s["id"], (0, 0))[0] > 0
           and by[s["id"]][1] == by[s["id"]][0]]
    if bad:
        r.fail(f"{len(bad)} 档优质源只能走本机线（已发布的全靠 ASR／字幕）却没标 "
               f"residential —— 云端会反复挑走、转不了、撞满上限永久失败："
               + "、".join(bad[:6]))
    else:
        r.good(f"优质源路由正确（{sum(1 for s in core if s.get('residential'))}/"
               f"{len(core)} 档走本机线）")
    # ② 核心源不该被分数判掉
    try:
        done = json.loads((DATA / "state.json").read_text()).get("done") or {}
    except Exception:
        return
    # 「只拦广告」现在覆盖 tier 1+2（用户先认可了 tier1，随后说「放吧」）。
    # 放开的是**选题**这一道，不是上站门槛 —— 稿子仍要过机械闸门和成稿评分。
    ids = {s["id"] for s in core if s.get("tier") in (1, 2)}
    # **用代码的同一把尺子判。** 原来这里写的是「判词不是宣传就算错拦」，
    # 而代码里的规则是 `_core_blocks`（广告 **或 ≤3 分**）——
    # 两把尺子一不一样，体检就会对着 2 分的「趣味测验」喊冤。
    # 检查和被检查的东西必须共用同一个判断函数，不能各写一遍。
    try:
        sys.path.insert(0, str(ROOT / "pipeline"))
        import importlib
        _blocks = importlib.import_module("run")._core_blocks
    except Exception:
        _blocks = lambda v: "宣传" in str(v.get("kind") or "")
    wrong = [v for v in done.values()
             if isinstance(v, dict) and v.get("skip") == "off-brief"
             and v.get("src") in ids and not _blocks(v)]
    if wrong:
        r.fail(f"{len(wrong)} 集优质源的稿被**分数**判掉了（判词不是「宣传」）——"
               f"核心源只该拦广告：" + "；".join(
                   f"{(v.get('src') or '')}:{v.get('score')} {(v.get('why') or '')[:24]}"
                   for v in wrong[:3]))
    else:
        r.good("优质源没有被分数判掉的集（只拦广告）")


def check_translation_side_tables(r: Report) -> None:
    """说话人和信源简介的译名表跟得上吗。

    为什么单独报：这两张表是**全站去重**的短字符串，不按集跑，所以
    translate.py 的进度看不出它们缺没缺。说话人表原来根本不存在——
    英文页上 3144 处说话人里 451 处是中文，其中「西格尔·塞缪尔」
    是 Sigal Samuel 音译过去又原样端给了英文读者。HTML 的零漏译闸门
    放行了它们（渲染处包了 lang="zh"），所以只有这里能报。
    """
    import re as _re
    cjk = _re.compile(r"[一-鿿]")
    d = DATA / "en"
    if not d.exists():
        return
    want = set()
    for f in sorted((DATA / "episodes").glob("*.json")):
        try:
            dg = (json.loads(f.read_text()).get("digest") or {})
        except Exception:
            continue
        for row in (dg.get("points") or []) + (dg.get("quotes") or []):
            v = (row.get("spk") or "").strip()
            if v and cjk.search(v):
                want.add(v)
    have = {}
    f = d / "_speakers.json"
    if f.exists():
        try:
            have = json.loads(f.read_text()).get("speakers", {})
        except Exception:
            have = {}
    miss = sorted(want - set(have))
    if not want:
        pass
    elif miss:
        r.fail(f"说话人译名缺 {len(miss)}/{len(want)} 个，英文页上会显示中文"
               f"（{'、'.join(miss[:4])}…）。跑 python3 pipeline/transspeakers.py")
    else:
        r.good(f"说话人译名 {len(want)}/{len(want)} 个齐全")

    srcs = []
    f = DATA / "sources.json"
    if f.exists():
        srcs = [x for x in json.loads(f.read_text()).get("sources") or [] if x.get("desc")]
    hs = {}
    f = d / "_sources.json"
    if f.exists():
        try:
            hs = json.loads(f.read_text()).get("sources", {})
        except Exception:
            hs = {}
    m = [x["id"] for x in srcs if x["id"] not in hs]
    if srcs and m:
        r.fail(f"信源简介译文缺 {len(m)}/{len(srcs)} 条，英文站的信源页会显示中文"
               f"（{'、'.join(m[:4])}…）。跑 python3 pipeline/transsources.py")
    elif srcs:
        r.good(f"信源简介译文 {len(srcs)}/{len(srcs)} 条齐全")


def check_source_coverage(r: Report) -> None:
    """每档信源为什么有／没有产出——按需要的**动作**分类。

    为什么必须报：163 档里 67 档一篇都没出过，而这 67 档在任何一处输出里
    都长得一模一样。"源自己三个月没更新"和"我们的抓取坏了"要的动作完全不同，
    混在一起的结果是：**新加一档源、它悄悄不工作，没有任何人会知道。**

    硬伤只给"我们的问题"：抓取坏了、tier1 从没被尝试过。
    拿不到文稿和源自己安静了是按设计不发，只报数。
    """
    try:
        import sys as _s
        _s.path.insert(0, str(ROOT / "pipeline"))
        import srccoverage as _cov
    except Exception as ex:
        r.fail(f"覆盖率诊断跑不起来（{type(ex).__name__}）——"
               f"信源有没有被抓到就没人报了")
        return
    c = _cov.classify()
    n = sum(len(v) for v in c.values())
    ok = len(c.get("有产出") or [])
    broke = _cov.broken(c)
    if broke:
        r.fail(f"{len(broke)} 档信源抓取坏了（我们的问题）："
               f"{'、'.join(x['id'] for x in broke[:6])}"
               f" —— 跑 python3 pipeline/srccoverage.py 看详情")
    nocheck = c.get("从没体检过") or []
    if nocheck:
        r.note(f"{len(nocheck)} 档从没体检过（status.ok 是 None，不是坏）："
               f"{'、'.join(x['id'] for x in nocheck[:6])}"
               f" —— 跑 python3 pipeline/resolve_sources.py --check")
    untried = c.get("从没被尝试过") or []
    # 实探结果（srccoverage.py --probe --write 写的）：**原因是算出来的，
    # 不是一张"已知不可达"的名单**。名单会过期，而且下一个人看不出它当初
    # 为什么在名单上。Sharp Tech 就是例子：tier1、每周更新、feed 好的，
    # 却一集没出过——实探一句话说清，它公开 feed 里每一集标题都是
    # (Preview)（订阅制节目只放试听），SKIP_TITLE 拦得对。
    probe, probe_age = {}, None
    f = DATA / "coverage.json"
    if f.exists():
        try:
            d = json.loads(f.read_text())
            probe = d.get("untried") or {}
            import datetime as _dt
            t = _dt.datetime.strptime(d["at"][:19], "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=_dt.timezone.utc)
            probe_age = (_dt.datetime.now(_dt.timezone.utc) - t).days
        except Exception:
            probe = {}
    def explained(sid: str) -> str | None:
        why = probe.get(sid)
        # "本该被抓到"不算解释——那正是要报的那一类
        return None if (not why or "本该被抓到" in why) else why
    t1_bad = [x["id"] for x in untried
              if x["tier"] == 1 and not explained(x["id"])]
    t1_ok = [(x["id"], explained(x["id"])) for x in untried
             if x["tier"] == 1 and explained(x["id"])]
    if t1_bad:
        r.fail(f"tier1 信源 {'、'.join(t1_bad)} feed 是新鲜的、却从没被尝试过，"
               f"而且实探也说不出原因 —— tier1 的意思是「必收」。"
               f"跑 python3 pipeline/srccoverage.py --probe")
    for sid, why in t1_ok:
        r.note(f"tier1 信源 {sid} 一集都出不来，原因已实探：{why}"
               f"（不是故障；要么它不该是 tier1，要么这档只能放弃）")
    should = [k for k, v in probe.items() if "本该被抓到" in v]
    # 硬伤的判据是**结构上到不了**，不是"还没赢到预算"。
    #
    # 主跑批按 tier 打分（1=100 / 2=55 / 3=25），每天预算 6-8 集，所以
    # tier2/3 靠的是分类扫描波轮到它。原来那三波是硬编码的 34 个 id、
    # 只覆盖 hist/ideas/sci——ai/biz/cn/parent 的低优先级源**一次都轮不到**，
    # 实测 23 档 feed 里有能进候选的集却一篇没出过，15 档就落在那几个分类。
    #
    # 所以这里查的是"它所在的分类在不在轮转清单里"。轮到了还没出，那是
    # 排序和预算的结果，报个数就够；轮不到，才是漏。
    swept = set()
    wf = ROOT / ".github" / "workflows" / "daily.yml"
    if wf.exists():
        import re as _re
        m = _re.search(r"CATS=\(([a-z ]+)\)", wf.read_text())
        if m:
            swept = set(m.group(1).split())
    srcs = {x["id"]: x for x in
            json.loads((DATA / "sources.json").read_text())["sources"]}
    unreachable = [k for k in should
                   if (srcs.get(k, {}).get("cat") or "?") not in swept]
    if unreachable:
        r.fail(f"{len(unreachable)} 档信源的 feed 里有能进候选的集，而它们所在的"
               f"分类**不在定时扫描的轮转清单里**——结构上永远轮不到："
               f"{'、'.join(unreachable[:6])}。"
               f"把分类加进 daily.yml 的 CATS")
    elif should:
        r.note(f"{len(should)} 档信源有能进候选的集但还没出过稿（分类都在轮转"
               f"清单里，是排序和每日预算的结果，不是漏）—— "
               f"python3 pipeline/srccoverage.py --probe 看每一档")
    if untried:
        r.note(f"{len(untried)} 档 feed 新鲜却从没被尝试过 —— "
               f"python3 pipeline/srccoverage.py --probe 看每一档的原因")
    if probe_age is None:
        r.note("信源实探还没跑过（data/coverage.json 不存在）—— "
               "跑 python3 pipeline/srccoverage.py --probe --write")
    elif probe_age > 7:
        r.note(f"信源实探是 {probe_age} 天前的，可能过期了 —— "
               f"跑 python3 pipeline/srccoverage.py --probe --write")
    r.good(f"信源覆盖：{ok}/{n} 档有产出 · "
           f"拿不到文稿 {len(c.get('拿不到文稿') or [])} 档 · "
           f"源自己安静了 {len(c.get('源自己安静了') or [])} 档")


def check_language_parity(r: Report) -> None:
    """三棵树是不是**同一次推送**一起上线的。

    判据落在后果上：有没有哪一篇只有中文、以及它已经这样多久了。

    四条发布线的顺序都是「跑批 → 译 → 建站 → 推」，所以正常情况下三棵树
    同时上线。会打破它的是三件事，都实测过：
      · 译的上限比发的上限小（日更单轮能发 18 篇、只译 12 篇）；
      · translate.py 是 continue-on-error，失败就整轮只推中文；
      · build.py 只渲染有译文的集——少几篇是**静默**的，
        产物齐全检查照样全过，因为它查的是"三个数字相等"，
        而那三个数字都是从同一棵树数出来的。

    所以这条不看流程，只看结果，并且报出**滞后时长**：超过一个发布周期
    （快车道 2 小时）还没补上，就说明不是"正在译"，而是"漏了"。
    """
    import datetime as _dt
    d = DATA / "en"
    if not d.exists():
        return
    have = {f.stem for f in d.glob("*.json") if not f.name.startswith("_")}
    # **搁置的不算「漏了」。** 译不合格连着三轮就搁置（那是对的：译不好
    # 不上，别烧钱），由 check_parked_translations 单独报。这里不排除的话，
    # 一篇永久译不出的稿会让这道检查永远红 —— 会喊狼来了的检查比没有更糟。
    parked = set()
    try:
        pf = json.loads((DATA / "translate-failed.json").read_text())
        parked = {k for k, v in pf.items() if (v or {}).get("n", 0) >= 3}
    except Exception:
        pass
    lag = []
    for f in sorted((DATA / "episodes").glob("*.json")):
        try:
            ep = json.loads(f.read_text())
        except Exception:
            continue
        slug = ep.get("slug") or ""
        if not slug or slug in have or slug in parked:
            continue
        at = ep.get("generated") or ep.get("at") or ""
        hours = None
        try:
            t = _dt.datetime.strptime(at[:19], "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=_dt.timezone.utc)
            hours = (_dt.datetime.now(_dt.timezone.utc) - t).total_seconds() / 3600
        except Exception:
            pass
        lag.append((hours if hours is not None else 1e9, slug))
    if not lag:
        r.good(f"三语齐平：{len(have)}/{len(have)} 篇都有译文，"
               f"三棵树同一次推送上线")
        return
    lag.sort(reverse=True)
    worst, slug = lag[0]
    age = "时间未知" if worst > 1e8 else f"{worst:.1f} 小时"
    line = (f"{len(lag)} 篇只有中文，最久的已经 {age}"
            f"（{slug[:44]}）")
    # 一个快车道周期是 2 小时。超过 3 小时还没补上，就不是"正在译"。
    if worst > 3:
        r.fail(line + " —— 超过一个发布周期还没补上，"
                      "跑 python3 pipeline/translate.py --limit 24 --workers 4")
    else:
        r.note(line + " —— 还在一个发布周期内，下一班会补上")


def check_source_status_is_fresh(r: Report) -> None:
    """信源健康状态有没有在刷新。

    `resolve_sources.py --check` 在本机线里是 `|| true`：它连着失败，
    sources.json 的 status 就悄悄过期，而过期的状态和正常的长得一模一样。
    今天那 8 档「从没体检过」（status 是空字典）就是这么积下来的——
    它们的 feed 其实全好，zeroknowledge 有 421 集。

    判据落在 generated 这个时间戳上：它不动就是没在刷新。
    """
    import datetime as _dt
    try:
        d = json.loads((DATA / "sources.json").read_text())
    except Exception:
        return
    at = d.get("generated") or ""
    try:
        t = _dt.datetime.strptime(at[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=_dt.timezone.utc)
        days = (_dt.datetime.now(_dt.timezone.utc) - t).days
    except Exception:
        r.note("sources.json 没有 generated 时间戳，判断不了状态新不新")
        return
    n = len(d.get("sources") or [])
    if days > 7:
        r.fail(f"信源健康状态 {days} 天没刷新过（{n} 档）—— "
               f"resolve_sources --check 在本机线里是 || true，"
               f"连着失败不会有人知道，而过期的状态和正常的长得一样")
    elif days > 3:
        r.note(f"信源健康状态 {days} 天前刷新的（{n} 档）")
    else:
        r.good(f"信源健康状态 {days} 天内刷新过（{n} 档）")


def check_token_usage(r: Report) -> None:
    """模型用量：这几天花了多少，哪一步在涨。

    原来用量只在跑批的输出里印一次、跑完就没了，所以"这周花了多少""哪一步
    在涨"都答不上来，只能等账单。现在累计在 data/usage.json 里。

    只报数不设阈值：多少算多是产品判断，不该由这里替人定。
    但**思考 token 的占比要单独说**——实测每篇深读约 16.7k 输出 token，
    其中 87% 是思考。那是这个站最大的一笔，也是唯一可以靠换模型直接压的。
    """
    f = DATA / "usage.json"
    if not f.exists():
        return
    try:
        blob = json.loads(f.read_text())
    except Exception:
        return
    days = sorted(blob)[-7:]
    if not days:
        return
    tin = tout = tthink = calls = 0
    byrole = {}
    for d in days:
        for k, v in (blob[d] or {}).items():
            tin += v.get("in", 0); tout += v.get("out", 0)
            tthink += v.get("think", 0); calls += v.get("calls", 0)
            role = k.split("/")[0]
            byrole[role] = byrole.get(role, 0) + v.get("in", 0) + v.get("out", 0)
    top = sorted(byrole.items(), key=lambda x: -x[1])[:3]
    share = tthink * 100 // max(tout, 1)
    r.good(f"模型用量（近 {len(days)} 天）：{calls} 次 · "
           f"{(tin + tout) / 1000:.0f}k tokens · "
           f"大头 {'、'.join(f'{k} {v//1000}k' for k, v in top)}"
           + (f" · 思考占输出 {share}%" if tthink else ""))


def check_local_asr(r: Report) -> None:
    """本机转写能不能用——**坏了要说出来，不能只显示 asr=off**。

    `transcript.local_available()` 捕获一切异常返回 False，所以
    「没装 mlx-whisper」和「装了但 import 炸」长得完全一样，
    而跑批输出只写 `asr=off`，看起来像配置选择。

    实测过的事故（我自己一天之内造成的）：加了 `pipeline/srccoverage.py`，
    它遮住了第三方 coverage 包；numba 容忍这个包缺失、不容忍它存在但不对，
    于是 mlx_whisper 一 import 就炸 → 本机转写整条死掉。而它是 19 档
    residential 源（多为中文）的**唯一**取稿路径——云端定时跑批不含 asr。
    后果是建档队列一天也不缩，而没有一行输出提到转写坏了。

    这条把「没装」和「坏了」分开：坏了报硬伤，并带上真实的报错。
    """
    import importlib
    import shutil
    try:
        _s = __import__("sys")
        _s.path.insert(0, str(ROOT / "pipeline"))
        T = importlib.import_module("lib.transcript")
    except Exception as ex:
        r.fail(f"transcript 模块都导不进来（{type(ex).__name__}）")
        return
    if T.ASR_KEY:
        r.good("转写：配了 TRANSCRIBE_API_KEY，走云端接口")
        return
    if not shutil.which("ffmpeg"):
        r.note("转写：本机没有 ffmpeg，本地转写用不了"
               "（residential 源只能靠 YouTube 字幕）")
        return
    try:
        importlib.import_module("mlx_whisper")
    except ImportError:
        r.note("转写：没装 mlx-whisper，本地转写用不了。"
               "residential 源里靠转写取稿的那批不会有产出")
        return
    except Exception as ex:
        # **这才是要抓的那一类**：装了却坏了
        r.fail(f"转写：mlx-whisper 装着但 import 就炸"
               f"（{type(ex).__name__}: {str(ex)[:90]}）—— "
               f"本机转写整条死掉，而它是 residential 源的唯一取稿路径。"
               f"常见原因是 pipeline/ 里有模块遮住了第三方包"
               f"（run.py 把它插在 sys.path 最前面）")
        return
    if not T.local_available():
        r.fail("转写：mlx-whisper 导得进来，local_available() 却是 False —— "
               "去看 LOCAL_ON 开关和 ffmpeg")
        return
    r.good("转写：本机 mlx-whisper 可用")


def check_catchup_is_working(r: Report) -> None:
    """新源建档这条线**在产出吗**——不是"跑没跑"，是"有没有结果"。

    为什么必须有这一条：建档在两条线上都是 continue-on-error / || true。
    它失败、或者长期零产出，都不会有任何输出提到。而"加了 30 档源、两周
    产出 2 篇"正是这么藏了两周的——sources.json 涨了、分类数涨了、
    体检全绿，没有一个数字在说这件事。

    判据落在**队列有没有在缩**：建档名单是「账本里 added、且出稿不足 6 篇」
    推导出来的，建起来就自动退出。所以队列长期不变 = 机制没在工作，
    不管它每天有没有"成功执行"。
    """
    import datetime as _dt
    f = DATA / "catchup.json"
    if not f.exists():
        # 有待建档的源、却从没留过痕，说明这一步一次都没真的跑过
        try:
            import sys as _s
            _s.path.insert(0, str(ROOT / "pipeline"))
            import run as _run
            n = len(_run._catchup_ids())
        except Exception:
            return
        if n:
            r.fail(f"{n} 档新源等着建档，而建档这一步**一次都没跑过**"
                   f"（data/catchup.json 不存在）—— 加了源却没有内容，"
                   f"正是这么藏住的")
        return
    try:
        d = json.loads(f.read_text())
    except Exception:
        r.note("data/catchup.json 读不了")
        return
    pend, prev = d.get("pending"), d.get("prev_pending")
    age, t = None, None
    try:
        t = _dt.datetime.strptime(d["at"][:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=_dt.timezone.utc)
        age = (_dt.datetime.now(_dt.timezone.utc) - t).total_seconds() / 3600
    except Exception:
        pass
    if not pend:
        r.good("新源建档：队列空了，所有新源都起量了")
        return
    if age is not None and age > 48:
        r.fail(f"新源建档 {age/24:.0f} 天没跑过了，队列里还有 {pend} 档 —— "
               f"两条发布线里那一步是 continue-on-error，坏了不会有人知道")
        return
    line = f"新源建档：队列 {pend} 档，上一轮发了 {d.get('published', 0)} 篇"
    # **判据要看时间跨度，不是看单轮。** 单轮零产出是正常的——闸门本来
    # 就会拒掉大半（实测一轮 16 个候选 15 个取不到文稿）。第一版写成
    # 「上一轮零产出就报硬伤」，刚建好就自己响了：前一轮发了 2 篇、
    # 这一轮 0 篇，而那完全正常。一条会喊狼来了的检查比没有更糟。
    #
    # 真正的失效是「队列长期不缩」。所以和 prev_at 那个时间点比：
    # 隔了 3 天以上、队列一点没缩，才是机制没在工作。
    shrunk = prev is None or pend < prev
    span_days = None
    if d.get("prev_at"):
        try:
            t0 = _dt.datetime.strptime(d["prev_at"][:19],
                                       "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=_dt.timezone.utc)
            span_days = ((t - t0).total_seconds() / 86400) if t else None
        except Exception:
            span_days = None
    if not shrunk and span_days is not None and span_days >= 3:
        r.fail(line + f"（{span_days:.0f} 天前也是 {prev} 档，一点没缩）—— "
                      f"建档没在产出，去看那一步的日志")
    else:
        r.note(line + (f"（上一轮 {prev} 档）" if prev is not None else ""))


def check_asr_only_routing(r: Report) -> None:
    """只靠转写出稿、却没标 residential 的源，在定时线上两边都不做。

    云端定时跑批用 --tiers feed,notes,page（不含 asr），本机 launchd 带
    ONLY_RESIDENTIAL=1（只挑标了的）。夹在中间的源只有手动 dispatch 才出得来，
    而每次定时跑还白失败一次、攒 DEAD_ATTEMPTS（满 10 次自动移除）。

    只报数不报错：全标上会把负载压到本机那一台，是成本取舍，不是纯技术问题。
    """
    import collections as _c
    try:
        srcs = {s["id"]: s for s in
                json.loads((DATA / "sources.json").read_text())["sources"]}
        state = json.loads((DATA / "state.json").read_text())
    except Exception:
        return
    by = _c.defaultdict(_c.Counter)
    for f in (DATA / "episodes").glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        q = (d.get("digest") or {}).get("quality") or {}
        by[d.get("source_id")][q.get("transcript_source") or "?"] += 1
    fails = _c.Counter()
    for v in (state.get("fail") or {}).values():
        if "transcript" in (v.get("why") or ""):
            fails[v.get("src")] += 1
    stuck = []
    for sid, c in by.items():
        if set(c) == {"asr"} and not (srcs.get(sid) or {}).get("residential"):
            stuck.append((fails[sid], sid))
    if not stuck:
        return
    stuck.sort(reverse=True)
    burning = [s for n, s in stuck if n >= 3]
    line = (f"{len(stuck)} 档只靠转写出稿、却没标 residential —— "
            f"定时线两边都不做它们，只有手动 dispatch 才出得来")
    if burning:
        r.note(line + f"；其中 {len(burning)} 档已在云端反复白失败"
                      f"（{'、'.join(burning[:5])}），攒满 10 次会被自动移除")
    else:
        r.note(line)


def check_discovery(r: Report) -> None:
    """三棵树都能被爬虫和答案引擎找到吗。

    robots.txt 只在**站点根目录**被读取，里面只声明一个 sitemap 的后果是
    /en/ 和 /tw/ 只能靠中文页上的 hreflang 被发现——英文站等于半个隐身。
    """
    rb = ROOT / "robots.txt"
    if not rb.exists():
        r.fail("robots.txt 不见了")
        return
    decl = [l.split(":", 1)[1].strip() for l in rb.read_text().splitlines()
            if l.startswith("Sitemap:")]
    missing = []
    for sub in ("", "tw", "en"):
        d = ROOT / sub if sub else ROOT
        if not (d / "index.html").exists():
            continue
        want = "/sitemap.xml" if not sub else f"/{sub}/sitemap.xml"
        if not any(u.endswith(want) for u in decl):
            missing.append(want)
    if missing:
        r.fail(f"robots.txt 没声明 {'、'.join(missing)} —— 爬虫找不到这些树")
    else:
        r.good(f"robots.txt 声明了 {len(decl)} 份 sitemap，每棵树都有")


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
    check_english_edition(r)
    check_translation_side_tables(r)
    check_core_sources(r)
    check_parked_translations(r)
    check_discovery(r)
    check_source_coverage(r)
    check_language_parity(r)
    check_asr_only_routing(r)
    check_token_usage(r)
    check_local_asr(r)
    check_catchup_is_working(r)
    check_source_status_is_fresh(r)
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
