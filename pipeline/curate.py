#!/usr/bin/env python3
"""信源策展：按实测表现淘汰劣化源，从自己的内容里挖新源，全程记进更新日志。

    python3 pipeline/curate.py --audit          只看体检报告，不动任何东西
    python3 pipeline/curate.py --demote         执行降级／移除
    python3 pipeline/curate.py --discover 8     找新源，只收 8 分以上
    python3 pipeline/curate.py --demote --discover 8

为什么要自动化：信源不该一次挑完就固定。节目会停更、会转向、会把长访谈换成短切片；
而值得收的新节目往往就出现在已收节目的嘉宾名单里。人工每几天复查一遍不现实，
所以把判据写成可测的数字。

判据全部来自落盘数据，不靠印象：
  feed 健康        resolve_sources --check 写进 sources.json 的 status
  选题通过率        triage 打过分的集里有多少过线 —— 直接反映"这档节目还值不值得看"
  成稿评分中位      过线的稿子实际写出来什么水平
  取稿成功率        一直取不到文稿的源占着名额却产不出内容
每次改动都追加进 data/curation.json，并渲染成站上的更新日志。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import statistics
import sys
import urllib.parse
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lib import feeds, llm, net                                    # noqa: E402
from lib.util import iso, log, now, squeeze, strip_html                        # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LEDGER = DATA / "curation.json"

# 淘汰阈值。定得宽松是有意的：误踢一个好源的代价比留一个平庸源大得多，
# 而且踢掉之后不会有人注意到它不见了。
MIN_TRIAGE_EVALS = 6        # 少于这个次数不作判断，样本太小
MIN_TRIAGE_PASS = 0.25      # 选题通过率低于此 → 降级
MIN_PUBLISHED_FOR_REVIEW = 3
MIN_REVIEW_MEDIAN = 7.0     # 成稿评分中位不高于此 → 降级
STALE_DAYS = 120            # 停更超过此 → 休眠
DEAD_ATTEMPTS = 10          # 尝试这么多次仍一篇取不到文稿 → 移除
DEAD_STREAK = 3             # feed 连续这么多次体检失败 → 才算真的死了


def load_ledger() -> list[dict]:
    if LEDGER.exists():
        try:
            return json.loads(LEDGER.read_text())
        except Exception:
            log("  ! curation.json 读不动，从空开始")
    return []


def save_ledger(rows: list[dict]) -> None:
    LEDGER.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n")


def performance() -> dict[str, dict]:
    """每个源的实测表现。"""
    srcs = {s["id"]: s for s in json.loads((DATA / "sources.json").read_text())["sources"]}
    state = json.loads((DATA / "state.json").read_text()) if (DATA / "state.json").exists() \
        else {"done": {}, "fail": {}}
    per: dict[str, dict] = {sid: {"published": 0, "review": [], "triage": [],
                                  "no_transcript": 0}
                            for sid in srcs}
    for f in (DATA / "episodes").glob("*.json"):
        try:
            r = json.loads(f.read_text())
        except Exception:
            continue
        p = per.get(r.get("source_id"))
        if p is None:
            continue
        p["published"] += 1
        sc = (r.get("review") or {}).get("score")
        if isinstance(sc, (int, float)):
            p["review"].append(float(sc))
        t = (r.get("triage") or {}).get("score")
        if isinstance(t, (int, float)):
            p["triage"].append(float(t))
    for v in state.get("done", {}).values():
        if v.get("skip") == "off-brief" and v.get("src") in per:
            per[v["src"]]["triage"].append(float(v.get("score") or 0))
    for v in state.get("fail", {}).values():
        if v.get("src") in per and "no-transcript" in str(v.get("why", "")):
            per[v["src"]]["no_transcript"] += 1

    out = {}
    for sid, p in per.items():
        s = srcs[sid]
        st = s.get("status") or {}
        tri = p["triage"]
        out[sid] = {
            "name": s.get("zh") or s["name"], "tier": s.get("tier", 3), "cat": s["cat"],
            "published": p["published"],
            "review_median": statistics.median(p["review"]) if p["review"] else None,
            "triage_n": len(tri),
            "triage_pass": (sum(1 for x in tri if x >= 7) / len(tri)) if tri else None,
            "no_transcript": p["no_transcript"],
            "feed_ok": st.get("ok", True), "fail_streak": st.get("fail_streak", 0),
            "age_days": st.get("age_days"),
            "official_transcripts": st.get("official_transcripts", 0),
        }
    return out


def judge(sid: str, m: dict) -> tuple[str, str] | None:
    if m["feed_ok"] is False:
        streak = m.get("fail_streak") or 1
        if streak < DEAD_STREAK:
            return None
        return "drop", f"feed 连续 {streak} 次体检失败，已失效"
    if m["published"] == 0 and m["no_transcript"] >= DEAD_ATTEMPTS:
        return "drop", (f"尝试 {m['no_transcript']} 次一篇都取不到文稿，"
                        f"占着名额产不出内容")
    if m["age_days"] is not None and m["age_days"] > STALE_DAYS:
        return "dormant", f"已停更 {m['age_days']:.0f} 天"
    if (m["triage_n"] >= MIN_TRIAGE_EVALS and m["triage_pass"] is not None
            and m["triage_pass"] < MIN_TRIAGE_PASS):
        return "demote", (f"选题通过率 {m['triage_pass']*100:.0f}%"
                          f"（{m['triage_n']} 次评估），选题质量下降")
    if (m["published"] >= MIN_PUBLISHED_FOR_REVIEW and m["review_median"] is not None
            and m["review_median"] <= MIN_REVIEW_MEDIAN):
        return "demote", (f"成稿评分中位 {m['review_median']:.1f}"
                          f"（{m['published']} 篇），产出质量偏低")
    return None


def audit(perf: dict) -> list[tuple[str, str, str]]:
    log(f"{'源':<24}{'层':>3}{'发布':>5}{'评审':>6}{'选题通过':>9}{'停更':>7}  判定")
    log("-" * 82)
    actions = []
    for sid, m in sorted(perf.items(), key=lambda kv: (-(kv[1]["published"]), kv[0])):
        v = judge(sid, m)
        mark = {"drop": "移除", "demote": "降级", "dormant": "休眠"}.get(v[0], "") if v else ""
        rev = f"{m['review_median']:.0f}" if m["review_median"] is not None else "—"
        tp = f"{m['triage_pass'] * 100:.0f}%" if m["triage_pass"] is not None else "—"
        ag = f"{m['age_days']:.0f}d" if m["age_days"] is not None else "—"
        note = f"{mark}{' · ' + v[1] if v else ''}"
        log(f"{m['name'][:23]:<24}T{m['tier']:>2}{m['published']:>5}{rev:>6}"
            f"{tp:>9}{ag:>7}  {note}")
        if v:
            actions.append((sid, v[0], v[1]))
    return actions


def apply_actions(actions: list[tuple[str, str, str]], dry: bool = False) -> list[dict]:
    path = DATA / "sources.json"
    blob = json.loads(path.read_text())
    by = {s["id"]: s for s in blob["sources"]}
    entries = []
    for sid, act, why in actions:
        s = by.get(sid)
        if not s:
            continue
        name = s.get("zh") or s["name"]
        if act == "drop":
            blob["sources"] = [x for x in blob["sources"] if x["id"] != sid]
            entries.append({"at": iso(now()), "kind": "removed", "id": sid,
                            "name": name, "cat": s["cat"], "why": why})
            log(f"  移除 {name}：{why}")
        else:
            old = s.get("tier", 3)
            new = min(3, old + 1)
            if new == old and act == "demote":
                blob["sources"] = [x for x in blob["sources"] if x["id"] != sid]
                entries.append({"at": iso(now()), "kind": "removed", "id": sid,
                                "name": name, "cat": s["cat"],
                                "why": why + "（已在最低层，移除）"})
                log(f"  移除 {name}：{why}（已在最低层）")
                continue
            s["tier"] = new
            entries.append({"at": iso(now()),
                            "kind": "dormant" if act == "dormant" else "demoted",
                            "id": sid, "name": name, "cat": s["cat"],
                            "from_tier": old, "to_tier": new, "why": why})
            log(f"  降级 {name} T{old}→T{new}：{why}")
    if entries and not dry:
        blob["generated"] = iso(now())
        path.write_text(json.dumps(blob, ensure_ascii=False, indent=1) + "\n")
    return entries


LEADS_SYSTEM = """你在给一个中文播客深读站找新的信源。

下面给你的是本站近期发布的深读摘要。只找出材料里**明确出现的播客节目名**。

严格规则：
- **只要节目名，不要人名。**第一版把嘉宾名字（贾扬清、姚颂、刘洺堉）当线索交出来，
  拿去搜出的是 "Learn Persian with Chai"、"Within with Dr. Vivian Carrasco" 这种
  毫不相关的节目 —— 因为你无法知道某个人有没有自己的播客，别猜。
- 节目名必须在材料里字面出现过。材料里没有节目名就返回空数组，这是完全可接受的答案。
- 已在「现有信源」列表里的不要提。"""

LEADS_SCHEMA = """输出 JSON：{"leads":[{"name":"播客节目的完整名字","why":"不超过25字"}]}
最多 8 条。一个都没有就返回 {"leads":[]}。"""


def find_leads(existing: list[str], sample: list[dict]) -> list[dict]:
    if not llm.available() or not sample:
        return []
    body = []
    for x in sample:
        d = x["digest"]
        body.append(f"《{d.get('title')}》（{x.get('source_zh')}）：{d.get('dek')}")
        for q in (d.get("quotes") or [])[:2]:
            if q.get("spk"):
                body.append(f"  发言人：{q['spk']}")
        for t in (d.get("terms") or [])[:3]:
            body.append(f"  术语：{t.get('term')}")
    try:
        r = llm.call_json(LEADS_SYSTEM,
                          "现有信源（不要重复提）：" + "、".join(existing) + "\n\n"
                          + "近期深读：\n" + "\n".join(body)[:14000] + "\n\n" + LEADS_SCHEMA,
                          max_tokens=900, temperature=0.2, role="triage")
    except Exception as ex:
        log(f"  线索提取失败：{type(ex).__name__}")
        return []
    out = []
    for it in (r.get("leads") or [])[:12]:
        if isinstance(it, dict) and squeeze(str(it.get("name") or "")):
            out.append({"name": squeeze(str(it["name"]))[:60],
                        "why": squeeze(str(it.get("why") or ""))[:40]})
    return out


CHARTS = [(1318, "us", "Technology"), (1321, "us", "Business"),
          (1318, "cn", "中国区 Technology")]


def apple_charts(limit: int = 30) -> list[dict]:
    out, seen = [], set()
    for genre, cc, label in CHARTS:
        url = (f"https://itunes.apple.com/{cc}/rss/toppodcasts/"
               f"limit={limit}/genre={genre}/json")
        try:
            d = net.get_json(url, timeout=25, cache_ttl=3600)
        except Exception as ex:
            log(f"    {label} 榜取不到：{type(ex).__name__}")
            continue
        for e in ((d or {}).get("feed") or {}).get("entry") or []:
            name = ((e.get("im:name") or {}).get("label") or "").strip()
            iid = ((e.get("id") or {}).get("attributes") or {}).get("im:id")
            if not name or name in seen:
                continue
            seen.add(name)
            out.append({"name": name, "itunes": iid, "chart": label})
    return out


SHORTLIST_SYSTEM = """你在给一个中文播客深读站从榜单里挑候选。站点覆盖
AI/技术、投资/商业、中国视角、人文/思想、文明/历史、育儿/教育、健康/科学。
读者要可核对的判断、机制和证据，不限科技商业。

只凭节目名做粗筛，把明显不值得的剔掉即可，不确定的留下。

明显不要的：个人成长／励志／效率鸡汤、销售话术、加密币喊单、日播新闻综述、
泛泛访谈秀、宗教布道、语言学习、纯娱乐闲聊。

值得留的：各领域深访或机制拆解——产业与投资、思想与哲学、历史与制度、
循证育儿、可回论文的科学，以及中国视角的一手观察。"""

SHORTLIST_SCHEMA = """输出 JSON：{"keep":["节目名", ...]}
只回你决定留下的名字，原样照抄。可以一个都不留。"""


def shortlist(cands: list[dict]) -> list[dict]:
    if not cands or not llm.available():
        return cands[:8]
    names = [c["name"] for c in cands]
    try:
        r = llm.call_json(SHORTLIST_SYSTEM,
                          "榜单候选：\n" + "\n".join(f"- {n}" for n in names)
                          + "\n\n" + SHORTLIST_SCHEMA,
                          max_tokens=900, temperature=0.1, role="triage")
    except Exception as ex:
        log(f"    批量粗筛失败（放行前 8 个）：{type(ex).__name__}")
        return cands[:8]
    keep = {squeeze(str(x)) for x in (r.get("keep") or []) if squeeze(str(x))}
    out = [c for c in cands if c["name"] in keep]
    log(f"    榜单 {len(cands)} 档 → 粗筛留下 {len(out)} 档")
    return out


def _name_match(want: str, got: str) -> bool:
    import re
    STOP = {"the", "podcast", "a", "an", "this", "week", "in", "with", "and", "of",
            "on", "show", "daily", "news", "talk", "hour", "radio", "today", "live",
            "对话", "聊", "说", "科技", "商业"}
    tok = lambda t: {w for w in re.findall(r"[0-9a-z\u4e00-\u9fff]+", t.lower())
                     if len(w) > 1}
    a, b = tok(want), tok(got)
    if not a or not b:
        return False
    if len(a & b) / len(a | b) < 0.5:
        return False
    return bool((a & b) - STOP)


PROBATION_TIER = 3


def _lang_of(name: str, cat: str) -> str:
    """语言看名字，不看分类。

    这一条改了三次才对：
      一版「cat == cn 才算中文」——中文节目归到 AI/技术 就拿到 lang=en。
      二版改看 `zh` 字段——那是我们起的显示名，"Empire: World History" 的显示名是
        「Empire 世界史」，照它判会把英文节目判成中文，比原来更糟。
      三版只看原名字形，分类完全不参与——ChinaTalk 归在「中国视角」但整档是英文，
        分类说的是题材不是语言。
    这不是标注问题：中英文的文稿密度阈值不同（MIN_WORDS en 1200 / zh 1800，
    语速上限 300 / 520 wpm），语言判错会让整套闸门用错标准，ASR 的语言提示也会
    给错。名字里有汉字就是中文节目——这个判据比分类可靠。
    """
    del cat        # 故意不用：分类说的是题材，不是语言
    return "zh" if re.search(r"[\u4e00-\u9fff]", name or "") else "en"
EXCERPT_CHARS = 3200
NOTES_CHARS = 2600


def _notes_sample(head: list[dict]) -> str:
    out, used = [], 0
    for e in head[:5]:
        t = squeeze(strip_html(e.get("notes") or ""))
        if len(t) < 120:
            continue
        piece = f"【{e['title'][:60]}】{t[:900]}"
        if used + len(piece) > NOTES_CHARS:
            break
        out.append(piece)
        used += len(piece)
    return "\n\n".join(out)


def _excerpt(tr: dict | None) -> str:
    if not tr:
        return ""
    segs = tr.get("segments") or []
    if not segs:
        return squeeze(tr.get("text") or "")[:EXCERPT_CHARS]
    step = max(1, len(segs) // 20)
    picks, used = [], 0
    for i in range(0, len(segs), step):
        piece = squeeze(segs[i]["text"])[:220]
        if used + len(piece) > EXCERPT_CHARS:
            break
        picks.append(piece)
        used += len(piece)
    return "\n".join(picks)


def probe_candidate(name: str, itunes_id: str | None = None,
                    feed: str | None = None) -> dict | None:
    """iTunes 找 feed，然后实测：能不能取到文稿、更新是否健康。

    直接给了 feed 就跳过 iTunes——按名字搜是这套流程里最不可靠的一环（曾经把一个
    冒用 Anthropic 品牌的 AI 生成播客匹配成了 Anthropic 官方节目），能绕开就绕开。
    """
    if feed:
        # YouTube 频道 feed 的正文全在字幕里。不放行 youtube 层的话，探测取不到任何
        # 内容，评分又退回凭标题猜——那正是我们修掉的毛病。
        yt = "youtube.com/feeds/videos.xml" in feed
        s = {"id": "cand", "name": name, "feed": feed, "cat": "ai", "lang": "en"}
        if yt:
            s["kind"] = "youtube"
        try:
            eps = feeds.fetch(s, cache_ttl=3600)
        except Exception as ex:
            log(f"    {name[:26]:<28} feed 取不到：{type(ex).__name__}")
            return None
        if not eps:
            return None
        return _measure(name, feed, None, eps, s,
                        allow=("feed", "notes", "page", "youtube") if yt
                        else ("feed", "notes", "page"))
    # 榜单给了 iTunes id 就直接 lookup，比按名字搜准得多
    if itunes_id:
        url = "https://itunes.apple.com/lookup?" + urllib.parse.urlencode(
            {"id": itunes_id, "entity": "podcast"})
    else:
        url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
            {"term": name, "entity": "podcast", "limit": 5, "country": "US"})
    try:
        res = (net.get_json(url, timeout=25) or {})
    except Exception:
        return None
    from lib import transcript as T
    for it in (res.get("results") or []):
        feed = it.get("feedUrl")
        if not feed:
            continue
        cn = it.get("collectionName") or ""
        if not itunes_id and not _name_match(name, cn):
            continue
        s = {"id": "cand", "name": cn, "feed": feed, "cat": "ai", "lang": "en"}
        try:
            eps = feeds.fetch(s, cache_ttl=3600)
        except Exception:
            continue
        if not eps:
            continue
        head = eps[:10]
        ds = [e["published"] for e in head if e["published"]]
        age = (now() - ds[0]).total_seconds() / 86400 if ds else None
        gaps = sorted((ds[i] - ds[i + 1]).total_seconds() / 86400
                      for i in range(len(ds) - 1)) if len(ds) > 1 else []
        return _measure(cn, feed, it.get("collectionId"), eps, s)
    return None


def _measure(name: str, feed: str, itunes, eps: list[dict], s: dict,
             allow: tuple[str, ...] = ("feed", "notes", "page")) -> dict:
    """实测一档候选。两条入口（iTunes 与直接给 feed）共用，否则两份实现迟早只改一份。"""
    from lib import transcript as T
    head = eps[:10]
    ds = [e["published"] for e in head if e["published"]]
    age = (now() - ds[0]).total_seconds() / 86400 if ds else None
    gaps = sorted((ds[i] - ds[i + 1]).total_seconds() / 86400
                  for i in range(len(ds) - 1)) if len(ds) > 1 else []
    # 挑一集够长的来测：YouTube 频道里混着短视频，拿切片判字幕会得出错误结论
    probe_ep = next((e for e in head if (e.get("duration") or 0) > 1200), head[0])
    tr = T.acquire(dict(probe_ep), s.get("lang", "en"), allow=allow, src=s)
    return {"name": name, "feed": feed, "itunes": itunes,
            "items": len(eps), "age_days": age,
            "cadence": gaps[len(gaps) // 2] if gaps else None,
            "official_transcripts": sum(1 for e in head if e["transcripts"]),
            "transcript_words": tr["words"] if tr else 0,
            "transcript_source": tr["source"] if tr else None,
            "has_audio": sum(1 for e in head if e.get("audio")),
            "sample": probe_ep["title"][:70],
            "titles": [e["title"][:80] for e in head[:6]],
            "notes_sample": _notes_sample(head),
            # 评分要看实际内容，不能只凭节目名和一个标题猜——那样同一档节目
            # 两次跑分能差 2 分，通过与否取决于措辞运气。
            "excerpt": _excerpt(tr)}


SCORE_SYSTEM = """你在给一个中文播客深读站评估要不要收一档新节目。
站点覆盖 AI、商业、中国视角、思想、历史、育儿、科学。读者要可核对的判断、
机制和证据；不要只按科技商业来打分。
**只评内容价值。**能不能取到文稿、更新是否健康，由程序作为前置条件判掉了，不进你的
打分——那两项决定的是"我们能不能做"，不是"该不该做"。

三项，满分 10：

1. 信息密度与一手性（0-5，权重最高）
   价值有两种形态，都算：一是事实与机制（数字、成本、时间线、失败细节），
   二是判断与框架（有立场、能被反驳的论断，讲清取舍与边界）。思想类、方法论、
   投资哲学不因为数字少而降级——分界线是能不能被反驳，不是有没有数字。
   5  每集都能拿走新东西：一手经历，或一个说得够硬、能被反驳的论断
   4  机制拆解扎实，或论断清晰有取舍
   3  有内容但不稳定，好集与水集混杂
   2  多为观点转述，既无事实也无明确立场
   0-1 泛泛而谈的正确话、新闻综述、励志、喊单

2. 补位价值（0-3）
   3  补上现有信源完全没覆盖的领域或视角
   2  与现有有部分重叠，但角度或深度明显不同
   1  基本重叠，只是多一个声音
   0  完全重复

3. 可核对性（0-2）
   2  受访者习惯把话说到能被检验的程度：给具体数字、点名公司与产品、
      说得出自己错在哪、或把论断收窄到可反驳
   1  有时如此
   0  几乎全是不可证伪的正确话

打分要狠。8 分意味着"我会主动向同行推荐追踪这档节目"。

**把区间用起来。**如果你发现自己给一批候选打的分都一样，那不是在评分。
真正值得收的节目应该在密度上给到 5，或者在补位上给到 3。

还有一条硬规则：**如果你的理由里要写"补位有限""与现有信源重叠""部分集数偏泛"，
那补位分就不该给到 2，密度分也不该给到 4。**分数和理由必须一致。"""

SCORE_SCHEMA = """输出 JSON：{"density":0-5,"gap":0-3,"checkable":0-2,
"desc":"一句话中文简介，写清它到底讲什么、凭什么值得读，不超过40字",
"cat":"ai|biz|cn|ideas|hist|parent|sci","tier":1|2|3,"why":"给这些分数的理由，不超过40字"}"""

MIN_CAND_WORDS = 3000
MAX_CAND_STALE = 60

HEDGE = ("重叠", "补位有限", "偏泛", "未到顶尖", "不稳", "边缘", "有限",
         "略逊", "不如", "稍弱", "尚可", "不算", "谈不上", "略低", "勉强",
         "一般", "不足", "略高", "较少")


def prerequisites(c: dict) -> str | None:
    age = c.get("age_days")
    if age is not None and age > MAX_CAND_STALE:
        return f"停更 {age:.0f} 天"
    if not c["transcript_words"] and not c["has_audio"]:
        return "既无文稿也无音频，取不到内容"
    if c["transcript_words"] and c["transcript_words"] < MIN_CAND_WORDS:
        return f"抽样文稿仅 {c['transcript_words']} 字，属短节目"
    if c["transcript_words"] == 0 and (c.get("cadence") or 0) and c["cadence"] < 1.2:
        return "日播且无文稿，多为新闻综述"
    return None


def score_candidate(c: dict, existing_desc: str) -> dict | None:
    if not llm.available():
        return None
    bad = prerequisites(c)
    if bad:
        return {"score": 0.0, "reject": bad, "desc": "", "cat": "ai", "tier": 3,
                "why": bad, "density": 0, "gap": 0, "checkable": 0}
    facts = f"共 {c['items']} 集"
    if c.get("age_days") is not None:
        facts += f"；最新一集 {c['age_days']:.0f} 天前"
    if c.get("cadence"):
        facts += f"；约 {c['cadence']:.0f} 天一集"
    facts += (f"；实测取到文稿 {c['transcript_words']} 字（{c['transcript_source']} 层）"
              if c["transcript_words"] else "；前三层无文稿，需音频转写")
    try:
        titles = "\n".join(f"- {t}" for t in (c.get("titles") or [c["sample"]]))
        ex = c.get("excerpt") or ""
        nt = c.get("notes_sample") or ""
        if ex:
            ev = f"实测抓到的文稿（全篇等距抽样，判密度就看这一份）：\n{ex}\n\n"
        elif nt:
            ev = ("节目自己写的分集说明（多集，判主题与具体程度看这一份）：\n"
                  f"{nt}\n\n**注意**：没有文稿只说明我们要走音频转写，"
                  "那是前置条件、已经判过了，不进你的打分。不要因为"
                  "「无文稿难核」而扣分——按标题和说明本身的具体程度公平地判。\n\n")
        else:
            ev = ("（既没有文稿也没有分集说明，证据确实不足——这时打分要保守，"
                  "不要凭节目名的名气给分。）\n\n")
        body = (f"候选节目：{c['name']}\n{facts}\n\n最近几集标题：\n{titles}\n\n"
                + ev + f"现有信源覆盖：{existing_desc[:2600]}\n\n{SCORE_SCHEMA}")
        r = llm.call_json(SCORE_SYSTEM, body,
                          max_tokens=600, temperature=0.1, role="review")
    except Exception as ex:
        log(f"    评分失败：{type(ex).__name__}")
        return None
    def num(k, hi):
        try:
            return max(0.0, min(float(hi), float(r.get(k) or 0)))
        except (TypeError, ValueError):
            return 0.0
    d, g, ck = num("density", 5), num("gap", 3), num("checkable", 2)
    why = squeeze(str(r.get("why") or ""))
    total = d + g + ck
    hedged = [w for w in HEDGE if w in why]
    inconsistent = bool(hedged) and total >= 8 and not (d >= 5 and g >= 3)
    return {"score": round(total, 1), "density": d, "gap": g, "checkable": ck,
            "reject": None, "hedged": hedged if inconsistent else [],
            "inconsistent": inconsistent,
            "desc": squeeze(str(r.get("desc") or ""))[:80],
            "cat": r.get("cat") if r.get("cat") in ("ai", "biz", "cn", "ideas", "hist", "parent", "sci") else "ai",
            "tier": r.get("tier") if r.get("tier") in (1, 2, 3) else 2,
            "why": squeeze(str(r.get("why") or ""))[:60]}


def slug_for(name: str, taken: set[str]) -> str:
    import hashlib
    import re
    ascii_part = re.sub(r"[^a-z0-9]+", "", name.lower())
    if len(ascii_part) >= 4:
        base = ascii_part[:14]
    else:
        h = hashlib.sha1(name.encode()).hexdigest()[:6]
        base = ("cn" if re.search(r"[\u4e00-\u9fff]", name) else "src") + h
    s = base
    i = 2
    while s in taken:
        s = f"{base}{i}"
        i += 1
    return s


def feed_pool(path: str) -> list[dict]:
    """从一个 {名字, feed} 列表里读候选。

    为什么要这条入口：现在的候选池只有 Apple 分类榜，而榜单按流行度排，天然偏大众
    ——那正是我们要避开的。任何别的目录（Radar 的 13 万档、朋友推荐、一份手写清单）
    只要能给出名字和 RSS 就能进来，判断仍然全部由我们自己的探测和评分做。
    直接给 feed 还绕开了按名字搜 iTunes 这一环，那是整套流程里最不可靠的地方。
    """
    try:
        rows = json.loads(pathlib.Path(path).read_text())
    except Exception as ex:
        log(f"  读不了候选清单 {path}：{type(ex).__name__}")
        return []
    out = []
    for r in rows if isinstance(rows, list) else []:
        name = r.get("name") or r.get("title")
        feed = r.get("feed") or r.get("url")
        if name and feed:
            out.append({"name": name, "feed": feed, "itunes": None,
                        "chart": r.get("chart") or "外部清单"})
    log(f"  外部清单 {path}：{len(out)} 档")
    return out


def discover(minimum: float, dry: bool = False,
             from_feeds: str | None = None) -> list[dict]:
    blob = json.loads((DATA / "sources.json").read_text())
    existing = [s.get("zh") or s["name"] for s in blob["sources"]]
    existing_desc = "；".join(f"{s.get('zh') or s['name']}（{s.get('desc','')[:16]}）"
                              for s in blob["sources"])
    eps = sorted((DATA / "episodes").glob("*.json"),
                 key=lambda f: f.stat().st_mtime, reverse=True)[:18]
    sample = [json.loads(f.read_text()) for f in eps]
    known_feeds = {s.get("feed") for s in blob["sources"]}
    known_it = {str(s.get("itunes")) for s in blob["sources"] if s.get("itunes")}
    known_nm = {(s.get("zh") or s["name"]).lower() for s in blob["sources"]}

    def is_dup(name: str) -> str | None:
        for kn in known_nm:
            if _name_match(name, kn):
                return kn
        return None

    pool = []
    for c in shortlist(apple_charts()):
        if str(c.get("itunes")) in known_it:
            continue
        dup = is_dup(c["name"])
        if dup:
            log(f"    {c['name'][:26]:<28} 与已在册的「{dup}」重复")
            continue
        pool.append(c)
    leads = [{"name": l["name"], "why": l["why"], "itunes": None, "chart": "本站内容"}
             for l in find_leads(existing, sample) if not is_dup(l["name"])]
    ext = [c for c in (feed_pool(from_feeds) if from_feeds else [])
           if c["feed"] not in known_feeds and not is_dup(c["name"])]
    cands = pool + leads + ext
    if not cands:
        log("  没有新候选")
        return []
    log(f"  候选 {len(cands)} 档（榜单 {len(pool)} + 本站内容 {len(leads)}"
        + (f" + 外部清单 {len(ext)}" if ext else "") + "），逐个实测：")
    taken = {s["id"] for s in blob["sources"]}
    added = []
    for ld in cands:
        c = probe_candidate(ld["name"], ld.get("itunes"), ld.get("feed"))
        if not c:
            log(f"    {ld['name'][:26]:<28} 找不到 feed")
            continue
        if c["feed"] in known_feeds:
            log(f"    {ld['name'][:26]:<28} 已在册")
            continue
        v = score_candidate(c, existing_desc)
        if not v:
            continue
        if v.get("reject"):
            log(f"    {c['name'][:26]:<28} 前置不合格 · {v['reject']}")
            continue
        if v.get("inconsistent"):
            log(f"    {c['name'][:26]:<28} {v['score']:.1f} 分 但理由里写了"
                f"「{'、'.join(v['hedged'])}」→ 分数与理由不一致，不收")
            continue
        verdict = "收" if v["score"] >= minimum else "不收"
        log(f"    {c['name'][:26]:<28} {v['score']:.1f} 分 "
            f"[密度{v['density']:.0f}/5 补位{v['gap']:.0f}/3 可核对{v['checkable']:.0f}/2] "
            f"{verdict} · {v['why']}")
        if v["score"] < minimum:
            continue
        sid = slug_for(c["name"], taken)
        taken.add(sid)
        known_feeds.add(c["feed"])
        entry = {"id": sid, "name": c["name"], "zh": c["name"], "cat": v["cat"],
                 "tier": PROBATION_TIER, "lang": _lang_of(c["name"], v["cat"]),
                 "kind": "youtube" if "youtube.com/feeds/videos.xml" in c["feed"]
                         else "rss",
                 "feed": c["feed"], "desc": v["desc"],
                 "cat_label": {"ai": "AI / 技术", "biz": "投资 / 商业",
                               "cn": "中国视角", "ideas": "人文 / 思想",
                               "hist": "文明 / 历史", "parent": "育儿 / 教育", "sci": "健康 / 科学"}.get(v["cat"], v["cat"])}
        if c.get("itunes"):
            entry["itunes"] = c["itunes"]
        # 文稿只能从 YouTube 字幕拿的源，云端做不了：GitHub 机房 IP 会被 YouTube
        # 判成机器人并索要 cookie。标成 residential 交给本机线，否则它每天在云端
        # 白失败一次，而"没有文稿"这条日志看不出是 IP 问题。
        if c.get("transcript_source") == "youtube":
            entry["residential"] = True
        blob["sources"].append(entry)
        added.append({"at": iso(now()), "kind": "added", "id": sid, "name": c["name"],
                      "probation": True,
                      "cat": v["cat"], "score": v["score"], "why": v["why"],
                      "desc": v["desc"], "lead": ld.get("chart") or ld.get("why")})
    if added and not dry:
        blob["generated"] = iso(now())
        (DATA / "sources.json").write_text(
            json.dumps(blob, ensure_ascii=False, indent=1) + "\n")
    elif added:
        log(f"  --dry-run：{len(added)} 档建议收录，未写入")
    return added


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", action="store_true", help="只出体检报告")
    ap.add_argument("--demote", action="store_true", help="执行降级／移除")
    ap.add_argument("--discover", type=float, metavar="MIN",
                    help="找新源，只收不低于 MIN 分的")
    ap.add_argument("--from-feeds", metavar="PATH",
                    help="额外的候选清单（JSON 列表，每项含 name/title 与 feed/url）。"
                         "判断仍由本站的探测与评分做，清单只提供候选")
    ap.add_argument("--dry-run", action="store_true",
                    help="只出建议，不改 sources.json 也不写日志")
    a = ap.parse_args()
    if not (a.audit or a.demote or a.discover):
        a.audit = True

    log(f"信源策展 · {iso(now())}")
    perf = performance()
    log(f"\n— 体检（{len(perf)} 档）—")
    actions = audit(perf)
    entries = []
    if a.demote and actions:
        log(f"\n— 执行 {len(actions)} 项 —")
        entries += apply_actions(actions, a.dry_run)
    elif actions:
        log(f"\n{len(actions)} 项待处理（加 --demote 执行）")
    if a.discover is not None:
        log(f"\n— 找新源（及格线 {a.discover}）—")
        entries += discover(a.discover, a.dry_run, a.from_feeds)
    if entries and a.dry_run:
        out = ROOT / ".cache" / "curate-dry-run.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(entries, ensure_ascii=False, indent=1) + "\n")
        log(f"\n--dry-run：共 {len(entries)} 项建议，一个字都没写进 sources.json")
        for e in entries:
            if e.get("kind") == "added":
                log(f"  建议收录 {e['name']}（{e['id']}，{e['score']:.1f} 分）{e['why']}")
            else:
                log(f"  建议{e.get('kind')} {e.get('name', e.get('id'))}")
        log(f"  完整建议已存 {out.relative_to(ROOT)}，要落地就去掉 --dry-run")
    elif entries:
        rows = load_ledger()
        rows.extend(entries)
        save_ledger(rows)
        log(f"\n更新日志追加 {len(entries)} 条 → data/curation.json")
    else:
        log("\n没有需要记录的改动")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
