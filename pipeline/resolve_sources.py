#!/usr/bin/env python3
"""Rebuild data/sources.json.

Feed URLs are resolved from the Apple Podcasts directory rather than hardcoded,
because shows migrate hosts (Anchor -> Megaphone -> Transistor) and a dead URL
is the single most common way an aggregator silently stops updating. Run this
when a source goes quiet:  python3 pipeline/resolve_sources.py --check
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lib import feeds, net                                    # noqa: E402
from lib.util import log, now                                 # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "sources.json"

# residential=True：这个 feed 拒绝机房 IP（Substack 对 GitHub Actions 一律 403，
# 换 UA 无效，是按 IP 拦的）。云端跳过它们，交给本机那条线，否则每天都会静默
# 丢掉 Latent Space 和 Interconnects——恰好是自带完整官方逐字稿的两个。
#
# tier 1 = always ingest · 2 = ingest when the episode looks substantive
# 3 = only on an unusually strong episode.  `kind`: rss | youtube
# `itunes` lets --check re-resolve a moved feed from the Apple directory.
CURATED: list[dict] = [
  # ---------- AI / 技术 ----------
  dict(id="latentspace", name="Latent Space", zh="Latent Space", cat="ai", tier=1, lang="en",
       itunes=1674008350, feed="https://api.substack.com/feed/podcast/1084089.rss",
       yt="UCxBcwypKK-W3GHd_RZ9FZrQ", residential=True, desc="AI 工程师视角的技术深访，是把论文和产品接起来的少数节目"),
  dict(id="dwarkesh", name="Dwarkesh Podcast", zh="Dwarkesh", cat="ai", tier=1, lang="en",
       itunes=1516093381, feed="https://apple.dwarkesh-podcast.workers.dev/feed.rss",
       yt="UCZa18YV7qayTh-MRIrBhDpA", desc="准备最充分的长访谈，逼受访者给出可反驳的具体判断"),
  dict(id="lennys", name="Lenny's Podcast", zh="Lenny's Podcast", cat="ai", tier=1, lang="en",
       itunes=1627920305, feed="https://api.substack.com/feed/podcast/10845.rss",
       yt="UC6t1O76G0jYXOAoYCm153dA", residential=True, desc="产品、增长与组织，一手操盘者的方法论"),
  dict(id="nopriors", name="No Priors", zh="No Priors", cat="ai", tier=1, lang="en",
       itunes=1668002688, feed="https://feeds.megaphone.fm/nopriors",
       yt="UCSI7h9hydQ40K5MJHnCrQvw", desc="Sarah Guo 与 Elad Gil 对 AI 创业者的投资人式追问"),
  dict(id="a16z", name="The a16z Show", zh="a16z", cat="ai", tier=2, lang="en",
       itunes=842818711, feed="https://feeds.simplecast.com/JGE3yC0V",
       yt="UCQ1VQj-37kl2yS_VUhfQHsw", desc="a16z 的行业趋势与投资视角"),
  dict(id="cogrev", name="The Cognitive Revolution", zh="Cognitive Revolution", cat="ai", tier=1, lang="en",
       itunes=1669813431, feed="https://feeds.megaphone.fm/RINTP3108857801",
       yt="UCoPTBQlwUm0m7gSbBW9wN6A", desc="对 AI 应用层与 agent 工程细节挖得最深的一档"),
  dict(id="interconnects", name="Interconnects", zh="Interconnects", cat="ai", tier=1, lang="en",
       itunes=1719789201, feed="https://api.substack.com/feed/podcast/48206.rss",
       yt="UCMhPRKnK8S_ruoGSt3vm1sQ", residential=True, desc="Nathan Lambert 的后训练与开源模型分析，技术判断少有水分"),
  dict(id="trainingdata", name="Training Data", zh="Training Data", cat="ai", tier=2, lang="en",
       itunes=1750736528, feed="https://feeds.megaphone.fm/trainingdata",
       yt="UCWrF0oN6unbXrWsTN7RctTw", desc="Sequoia 的 AI 研究与创业访谈"),
  dict(id="unsupervised", name="Unsupervised Learning", zh="Unsupervised Learning", cat="ai", tier=2, lang="en",
       itunes=1740609308, feed="https://feeds.simplecast.com/dOSE_bdP",
       desc="Redpoint 的 AI 从业者对话"),
  dict(id="madpod", name="The MAD Podcast", zh="MAD Podcast", cat="ai", tier=2, lang="en",
       itunes=1702572539, feed="https://anchor.fm/s/f2ee4948/podcast/rss",
       yt="UCQID78IY6EOojr5RUdD47MQ", desc="Matt Turck 的数据与 AI 基础设施访谈"),
  dict(id="hardfork", name="Hard Fork", zh="Hard Fork", cat="ai", tier=2, lang="en",
       itunes=1528594034, feed="https://feeds.simplecast.com/6HKOhNgS",
       yt="UCqnbDFdCpuN8CMEg0VuEBqA", desc="NYT 的科技周报，选题贴大众争议面"),
  dict(id="decoder", name="Decoder", zh="Decoder", cat="ai", tier=2, lang="en",
       itunes=1011668648, feed="https://feeds.megaphone.fm/recodedecode",
       yt="UCddiUEpeqJcYeBxX1IVBKvQ", desc="Nilay Patel 追问平台与组织的决策结构"),
  dict(id="pragmatic", name="The Pragmatic Engineer", zh="The Pragmatic Engineer", cat="ai", tier=2, lang="en",
       itunes=1769051199, feed="https://api.substack.com/feed/podcast/458709.rss",
       residential=True, desc="工程组织与研发实践，面向写代码的人"),
  dict(id="aiandi", name="AI & I", zh="AI & I（Every）", cat="ai", tier=2, lang="en",
       itunes=1719789201, feed="https://feeds.transistor.fm/how-do-you-use-chatgpt",
       yt="UCjIMtrzxYc0lblGhmOgC_CA", desc="Every 的 Dan Shipper，聚焦真实工作流里怎么用 AI"),
  dict(id="mlst", name="Machine Learning Street Talk", zh="ML Street Talk", cat="ai", tier=2, lang="en",
       itunes=1510472996, feed="https://anchor.fm/s/1e4a0eac/podcast/rss",
       yt="UCZHmQk67mSJgfCCTn7xBfew", desc="最硬的学术辩论场，常有和主流叙事对立的观点"),
  dict(id="twiml", name="The TWIML AI Podcast", zh="TWIML AI", cat="ai", tier=3, lang="en",
       itunes=1163412174, feed="https://feeds.megaphone.fm/MLN2155636147",
       desc="做了十年的研究者访谈"),
  dict(id="practicalai", name="Practical AI", zh="Practical AI", cat="ai", tier=3, lang="en",
       itunes=1406537385, feed="https://feeds.transistor.fm/practical-ai-machine-learning-data-science",
       desc="偏落地工程，自带官方逐字稿"),
  dict(id="lastweekai", name="Last Week in AI", zh="Last Week in AI", cat="ai", tier=3, lang="en",
       itunes=1502484418, feed="https://rss.art19.com/last-week-in-ai",
       desc="一周 AI 新闻综述，用来补漏"),
  dict(id="gradient", name="Gradient Dissent", zh="Gradient Dissent", cat="ai", tier=3, lang="en",
       itunes=1504567418, feed="https://feeds.captivate.fm/gradient-dissent/",
       desc="W&B 的从业者访谈"),
  dict(id="deepmind", name="Google DeepMind: The Podcast", zh="Google DeepMind", cat="ai", tier=2, lang="en",
       itunes=1465718009, feed="https://feeds.simplecast.com/JT6pbPkg",
       desc="DeepMind 官方，更新慢但都是一手研究"),
  dict(id="aidailybrief", name="The AI Daily Brief", zh="AI Daily Brief", cat="ai", tier=3, lang="en",
       itunes=1680633614, feed="https://anchor.fm/s/f7cac464/podcast/rss",
       desc="每日 AI 新闻，密度低但时效最快"),
  dict(id="lex", name="Lex Fridman Podcast", zh="Lex Fridman", cat="ai", tier=2, lang="en",
       itunes=1434243584, feed="https://lexfridman.com/feed/podcast/",
       desc="超长访谈，选题跨 AI、科学与政治"),
  dict(id="tbpn", name="TBPN", zh="TBPN", cat="ai", tier=2, lang="en",
       itunes=1772360235, feed="https://feeds.transistor.fm/technology-brother",
       yt="UC-DRzaGnL_vtBUpCFH5M0tg", desc="科技商业日播，自带官方逐字稿"),
  # ---------- 投资 / 商业（Onepod 完全空白的一块）----------
  dict(id="bg2", name="BG2Pod", zh="BG2", cat="biz", tier=1, lang="en", kind="youtube",
       yt="UC-yRDvpR99LUc5l7i7jLzew", feed="https://www.youtube.com/feeds/videos.xml?channel_id=UC-yRDvpR99LUc5l7i7jLzew",
       desc="Gerstner 与 Gurley 论 AI 资本开支，谁在为算力买单的第一现场"),
  dict(id="acquired", name="Acquired", zh="Acquired", cat="biz", tier=1, lang="en",
       itunes=1050462261, feed="https://feeds.transistor.fm/acquired",
       yt="UCyFqFYfTW2VoIQKylJ04Rtw", desc="单集三到五小时的公司史，研究深度是播客里的天花板"),
  dict(id="ilt", name="Invest Like the Best", zh="Invest Like the Best", cat="biz", tier=1, lang="en",
       itunes=1154105909, feed="https://feeds.megaphone.fm/CLS2859450455",
       desc="Patrick O'Shaughnessy 与一线投资人、创始人的长谈"),
  dict(id="oddlots", name="Odd Lots", zh="Odd Lots", cat="biz", tier=1, lang="en",
       itunes=1056200096, feed="https://www.omnycontent.com/d/playlist/e73c998e-6e60-432f-8610-ae21014b9f01/1c88a9dd-6b3f-4092-96e6-ae2500289e63/aab4c9d5-cb54-4437-8d17-ae250035c1e0/podcast.rss",
       desc="Bloomberg 的宏观与产业链解剖，全集自带官方逐字稿"),
  dict(id="sharptech", name="Sharp Tech", zh="Sharp Tech", cat="biz", tier=1, lang="en",
       itunes=1616249586, feed="https://sharptech.fm/feed/podcast",
       yt="UC9AHywQeW9BOcOl7dg-YMqA", desc="Ben Thompson 的战略分析，聚合理论的原产地"),
  dict(id="allin", name="All-In Podcast", zh="All-In", cat="biz", tier=2, lang="en",
       itunes=1502871393, feed="https://rss.libsyn.com/shows/254861/destinations/1928300.xml",
       yt="UCESLZhusAkFfsNsApnjF_Cg", desc="四位投资人的周谈，观点强、噪音也大"),
  dict(id="twentyvc", name="The Twenty Minute VC", zh="20VC", cat="biz", tier=2, lang="en",
       itunes=958230465, feed="https://rss.libsyn.com/shows/61840/destinations/240976.xml",
       yt="UC9jkoB5oKe1eAGZ5zOW6iZA", desc="一级市场融资与基金视角，更新极密"),
  dict(id="breakdowns", name="Business Breakdowns", zh="Business Breakdowns", cat="biz", tier=2, lang="en",
       itunes=1559120677, feed="https://feeds.megaphone.fm/breakdowns",
       desc="单公司拆解，商业模式讲得干净"),
  dict(id="capallocators", name="Capital Allocators", zh="Capital Allocators", cat="biz", tier=3, lang="en",
       itunes=1223764016, feed="https://rss.libsyn.com/shows/94820/destinations/482814.xml",
       desc="配置端视角，看机构资金怎么想"),
  dict(id="founders", name="Founders", zh="Founders", cat="biz", tier=3, lang="en",
       itunes=1141877104, feed="https://feeds.megaphone.fm/DSLLC6297708582",
       desc="创始人传记精读"),
  dict(id="thinkfast", name="Think Fast Talk Smart", zh="Think Fast Talk Smart", cat="biz", tier=3, lang="en",
       itunes=1494571212, feed="https://feeds.transistor.fm/think-fast-talk-smart-communication-techniques",
       desc="Stanford GSB 的沟通与领导力，自带官方逐字稿"),
  dict(id="equity", name="Equity", zh="Equity", cat="biz", tier=3, lang="en",
       itunes=1076853251, feed="https://feeds.megaphone.fm/YFL6537156961",
       desc="TechCrunch 的融资与交易速报"),
  dict(id="ycsp", name="Y Combinator", zh="Y Combinator", cat="biz", tier=1, lang="en",
       itunes=1236907421, feed="https://anchor.fm/s/8c1524bc/podcast/rss",
       yt="UCxIJaCMEptJjxmmQgGFsnCg", desc="YC 合伙人与创始人对早期公司的直给建议"),
  # ---------- 中国视角 / 中文播客（Onepod 一个都没有）----------
  dict(id="chinatalk", name="ChinaTalk", zh="ChinaTalk", cat="cn", tier=1, lang="en",
       itunes=1289062927, feed="https://feeds.megaphone.fm/CHTAL4990341033",
       yt="UCXOtILmQEo3pL_1bJfUOFWw", desc="中美科技与产业政策，英文世界里对中国讨论最细的一档"),
  dict(id="zhangxiaojun", name="张小珺·商业访谈录", zh="张小珺·商业访谈录", cat="cn", tier=1, lang="zh",
       itunes=1673203694, feed="https://feed.xyzfm.space/dk4yh3pkpjp3",
       yt="UC3Sv1JuKpbOx3csUO8FAo5g", desc="中文世界最扎实的 AI 与商业长访谈"),
  dict(id="sv101", name="硅谷101", zh="硅谷101", cat="cn", tier=1, lang="zh",
       itunes=1494229400, feed="https://feeds.fireside.fm/sv101/rss",
       yt="UChnNjLyx_5rk_iDPQ2BQDQA", desc="硅谷一线从业者中文解读，技术细节不含糊"),
  dict(id="latetalk", name="晚点聊 LateTalk", zh="晚点聊", cat="cn", tier=1, lang="zh",
       itunes=1662130580, feed="https://feeds.fireside.fm/latetalk/rss",
       yt="UC-bIYXBhoLKCfpY9AkmKm4w", desc="《晚点》的公司与人物访谈"),
  dict(id="whatsnext", name="What's Next｜科技早知道", zh="科技早知道", cat="cn", tier=2, lang="zh",
       itunes=1450909630, feed="https://feeds.fireside.fm/guiguzaozhidao/rss",
       yt="UC3OAvZbMjwvtwCTePkwZiCg", desc="面向中文听众的海外科技与资本解读"),
  dict(id="luanfanshu", name="乱翻书", zh="乱翻书", cat="cn", tier=2, lang="zh",
       itunes=1631554542, feed="https://feed.xyzfm.space/yxuruh3f9mc4",
       yt="UC0nqbhVSVHP9KVkHHBn58sw", desc="中国互联网公司与行业史，视角独立"),
  dict(id="mianji", name="面基", zh="面基", cat="cn", tier=3, lang="zh",
       itunes=1610952415, feed="https://feed.xyzfm.space/6hpdgggtxpxb",
       yt="UCroOLbclmz0FOit7d2Xe0PQ", desc="投资与个人财务，讲人话"),
  # ---------- 只在 YouTube 更新、没有播客 RSS 的一手信源 ----------
  dict(id="anthropic", name="Anthropic", zh="Anthropic", cat="ai", tier=1, lang="en", kind="youtube",
       yt="UCrDwWp7EBBv4NwvScIpBDOA", feed="https://www.youtube.com/feeds/videos.xml?channel_id=UCrDwWp7EBBv4NwvScIpBDOA",
       desc="Claude 与 AI 安全的官方一手材料"),
  dict(id="openai", name="OpenAI", zh="OpenAI", cat="ai", tier=2, lang="en", kind="youtube",
       yt="UCXZCJLdBC09xxGZ6gcdrc6A", feed="https://www.youtube.com/feeds/videos.xml?channel_id=UCXZCJLdBC09xxGZ6gcdrc6A",
       desc="OpenAI 官方发布与访谈"),
  dict(id="karpathy", name="Andrej Karpathy", zh="Andrej Karpathy", cat="ai", tier=1, lang="en", kind="youtube",
       yt="UCYO_jab_esuFRV4b17AJtAw", feed="https://www.youtube.com/feeds/videos.xml?channel_id=UCYO_jab_esuFRV4b17AJtAw",
       desc="更新极少，但每条都值得逐帧看"),
  dict(id="spc", name="South Park Commons", zh="South Park Commons", cat="biz", tier=3, lang="en", kind="youtube",
       yt="UCNT5auOEW5CzgngC8oE1nMA", feed="https://www.youtube.com/feeds/videos.xml?channel_id=UCNT5auOEW5CzgngC8oE1nMA",
       desc="pre-idea 阶段创业者的现场讨论"),
  dict(id="every", name="Every", zh="Every", cat="ai", tier=3, lang="en", kind="youtube",
       yt="UCjIMtrzxYc0lblGhmOgC_CA", feed="https://www.youtube.com/feeds/videos.xml?channel_id=UCjIMtrzxYc0lblGhmOgC_CA",
       desc="Every 的视频线，AI 与知识工作"),
  dict(id="peteryang", name="Peter Yang", zh="Peter Yang", cat="ai", tier=3, lang="en", kind="youtube",
       yt="UCnpBg7yqNauHtlNSpOl5-cg", feed="https://www.youtube.com/feeds/videos.xml?channel_id=UCnpBg7yqNauHtlNSpOl5-cg",
       desc="产品经理视角的 AI 工具实操"),
  # ---------- 2026-08 新增：按评分标准过 8 分的。文稿可得性全部实测过，
  # 不是"官网应该有文稿"这种假设——Conversations with Tyler 和半拿铁就是这么被
  # 挡下来的（假设有、实测没有）。----------
  # 云端 page 层 3/3 失败、本机实测可取 13771 字，先按需要住宅 IP 路由。
  # 具体原因待新加的 page 层日志下一轮跑批给出。
  dict(id="complexsys", name="Complex Systems", zh="Complex Systems", cat="biz", tier=1, lang="en",
       residential=True, itunes=1753399812, feed="https://feeds.transistor.fm/complex-systems-with-patrick-mckenzie-patio11",
       desc="patio11 拆支付、风控、银行与运营的实际机制，几乎每期都能直接拿去用"),
  dict(id="oxide", name="Oxide and Friends", zh="Oxide and Friends", cat="ai", tier=1, lang="en",
       itunes=1625932222, feed="https://feeds.transistor.fm/oxide-and-friends",
       desc="做服务器的人聊硬件、固件与系统工程，具体到芯片型号和踩过的坑"),
  dict(id="eightythousand", name="80,000 Hours Podcast", zh="80,000 Hours", cat="ai", tier=1, lang="en",
       itunes=1245002988, feed="https://feeds.transistor.fm/80000-hours-podcast",
       desc="超长深访 AI 安全与治理的研究者，准备程度接近学术，自带官方全文"),
  dict(id="mastersinbiz", name="Masters in Business", zh="Masters in Business", cat="biz", tier=2, lang="en",
       itunes=730188152, feed="https://www.omnycontent.com/d/playlist/e73c998e-6e60-432f-8610-ae210140c5b1/4e4cd910-40a1-4619-a5f3-ae2b0012ffff/5873a3cb-298f-40bc-b71f-ae2b0013000d/podcast.rss",
       desc="Bloomberg 访谈顶级资产管理人，讲清他们的决策框架而不是业绩"),
  dict(id="moneystuff", name="Money Stuff", zh="Money Stuff", cat="biz", tier=2, lang="en",
       itunes=1739582836, feed="https://www.omnycontent.com/d/playlist/e73c998e-6e60-432f-8610-ae210140c5b1/ee4336cb-155f-4488-90e0-b1400134e40e/77e6a3a7-290d-4a82-8164-b14001353ef2/podcast.rss",
       desc="Matt Levine 讲金融里那些结构性荒谬，监管与套利的机制"),
]

CATS = {"ai": "AI / 技术", "biz": "投资 / 商业", "cn": "中国视角"}


def itunes_lookup(cid: int) -> dict | None:
    q = urllib.parse.urlencode({"id": cid, "entity": "podcast"})
    try:
        r = net.get_json(f"https://itunes.apple.com/lookup?{q}", timeout=25)
    except Exception:
        return None
    res = (r or {}).get("results") or []
    return res[0] if res else None


def probe(s: dict) -> dict:
    """Fetch a source once: recency, cadence, artwork, transcript coverage."""
    st = {"ok": False}
    try:
        eps = feeds.fetch(s, cache_ttl=0)
    except Exception as e:
        st["error"] = f"{type(e).__name__}: {e}"[:120]
        return st
    if not eps:
        st["error"] = "feed parsed but empty"
        return st
    dates = [e["published"] for e in eps[:12] if e["published"]]
    st["ok"] = True
    st["episodes"] = len(eps)
    if dates:
        st["latest"] = dates[0].strftime("%Y-%m-%d")
        st["age_days"] = round((now() - dates[0]).total_seconds() / 86400, 1)
        gaps = sorted((dates[i] - dates[i + 1]).total_seconds() / 86400
                      for i in range(len(dates) - 1)) if len(dates) > 1 else []
        if gaps:
            st["cadence_days"] = round(gaps[len(gaps) // 2], 1)
    head = eps[:12]
    st["official_transcripts"] = sum(1 for e in head if e["transcripts"])
    st["image"] = next((e["image"] for e in head if e["image"]), "")
    return st


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="probe every feed, re-resolve dead ones via Apple, then write")
    a = ap.parse_args()

    out = []
    for s in CURATED:
        s = dict(s)
        s.setdefault("kind", "rss")
        s["cat_label"] = CATS[s["cat"]]
        if a.check:
            st = probe(s)
            if not st["ok"] and s.get("itunes"):
                meta = itunes_lookup(s["itunes"])
                if meta and meta.get("feedUrl") and meta["feedUrl"] != s["feed"]:
                    log(f"  ! {s['id']}: feed moved -> {meta['feedUrl']}")
                    s["feed"] = meta["feedUrl"]
                    st = probe(s)
            s["status"] = st
            flag = "ok " if st["ok"] else "DEAD"
            log(f"{flag} {s['id']:<15} {st.get('episodes','-'):>5} eps  "
                f"age={st.get('age_days','?'):>6}d  cadence={st.get('cadence_days','?'):>5}d  "
                f"tscr={st.get('official_transcripts','-')}/12  {st.get('error','')}")
            if st.get("image") and not s.get("image"):
                s["image"] = st["image"]
        out.append(s)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(
        {"generated": now().strftime("%Y-%m-%dT%H:%M:%SZ"),
         "categories": CATS, "sources": out}, ensure_ascii=False, indent=1) + "\n")
    live = sum(1 for s in out if s.get("status", {}).get("ok", True))
    log(f"\nwrote {OUT.relative_to(ROOT)} — {len(out)} sources"
        + (f", {live} live" if a.check else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
