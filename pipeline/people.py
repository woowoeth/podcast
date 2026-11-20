#!/usr/bin/env python3
"""人物频道：按「谁在说」归档，而不是按「哪档节目」。

数据在 data/people.json：每个人一张**视频线索表**（YouTube 编号、标题、频道、日期、时长、类型）。
线索表只是「去哪儿找原声」——稿子全部由本站管线自己写，选题、取稿、机械校验、成稿评审
四道闸门一道不少（走 run.process，和 addvideo.py 同一条路）。

**线索从哪来、为什么只拿这些：** 第一批线索取自 silicon-valley.tech 的公开索引
（2026-11-18，用户提议）。只取公开视频的事实信息，不取对方的语录、译文、文稿、主题标注
——那些是对方的编辑成果；而且「第三方文稿不进公开仓库」本来就是本站的规矩。
人物页上注明线索出处。

**只收官方频道。** 对方的索引里一大半是搬运号（马斯克 472/810，乔布斯 140/161），
出处不清、常有剪辑；用户 11-18 选了「加上演讲和发布会」，没选「加上搬运频道」。

**一集算不算某人的：两条路，都在构建时现算，不往集的数据里写字段。**
  ① 集的 youtube_id 在这个人的线索表里；
  ② 集里归给这个人的要点＋原话 ≥ PEER_MIN 条（排除「转述」）。只要提到一次不算——
     实测「Greg Jensen」会被「jensen」命中，Hard Fork 那集讨论黄仁勋但他本人没出场。
现算的理由：rescore/retitle 等脚本会整份重写集文件，存进去的标记迟早被冲掉，
而且名单一改，旧集要能自动跟着变。

    python3 pipeline/people.py --status
    python3 pipeline/people.py --next 4            # 本机日更每轮顺带做几条，最新的先做
    python3 pipeline/people.py --next 1 --dry-run
    python3 pipeline/people.py --import musk PATH  # 从对方页面的本地副本导入线索（只取事实字段）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PEOPLE = DATA / "people.json"

PEER_MIN = 3                 # 非线索表里的集：归给此人的要点＋原话至少这么多条
KEEP_KINDS = ("interview", "podcast", "presentation", "conference")
MIN_SECS = 900               # 15 分钟以下多是片段、快讯，写不出一篇深读
# 搬运号的视频（2026-11-18 用户：「搬运的里面有价值的长视频也需要」）门槛更高：
# 搬运号常把一场长谈切成段、配上标题党，20 分钟以上的才多半是整场。
MIN_SECS_REUPLOAD = 1200


def load(path: pathlib.Path = PEOPLE) -> dict:
    if not path.exists():
        return {"people": []}
    return json.loads(path.read_text())


def _speakers(ep: dict) -> list[str]:
    dg = ep.get("digest") or {}
    return [str(x.get("spk") or "") for k in ("quotes", "points")
            for x in (dg.get(k) or []) if isinstance(x, dict)]


def speaks(ep: dict, person: dict) -> int:
    """这一集里归给此人的要点＋原话条数。「转述某人」不算他本人在说。"""
    al = [a.lower() for a in person.get("aliases") or []]
    n = 0
    for s in _speakers(ep):
        low = s.lower()
        if "转述" in s or "paraphras" in low:
            continue
        if any(a in low for a in al):
            n += 1
    return n


def members(eps: list[dict], people: list[dict]) -> dict[str, list[dict]]:
    """每个人名下的集，保持 eps 原来的顺序（构建时是按时间倒序）。"""
    out: dict[str, list[dict]] = {p["id"]: [] for p in people}
    for p in people:
        ids = {v["yt"] for v in p.get("videos") or []}
        for x in eps:
            if (x.get("youtube_id") and x["youtube_id"] in ids) or speaks(x, p) >= PEER_MIN:
                out[p["id"]].append(x)
    return out


# ---------------------------------------------------------------- 补库


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:40] or "channel"


def channel_source(ch: str, person: dict, registered: list[dict]) -> dict:
    """集挂在**真实的频道**名下（原声站的规矩：读者要知道这段话是在哪儿说的）。

    频道已经是登记信源的，就用那个信源；否则给一个不登记的 id（ch-…）——
    build.py 会给未登记信源补页面，但它不进 sources.json，所以不会被日更去抓
    整个频道，也不会被 curate 当成停更源处理。

    **选题用人物频道自己的尺子**（rubric=people，见 lib/triage.PEOPLE_SYSTEM）：
    用户 11-18 定的标准是「视频里有没有值得学习的信息和内容」，和普通播客那把
    （一手、可反驳、机制）不一样。所以：
      · tier 一律当 3——tier 1/2 的「只拦广告、不看分」会让这把尺子形同虚设；
        登记过的频道（All-In 是 T1）也只在这一次处理里当 3，sources.json 不动；
      · kind 一律当 youtube——选题要喂字幕样本，不然只能按视频简介（多是链接）判。
    """
    want = ch.lower().strip()
    for s in registered:
        names = {str(s.get(k) or "").lower().strip() for k in ("name", "zh")}
        if want and want in names:
            return dict(s, tier=3, kind="youtube", residential=True,
                        rubric="people", person=_who(person))
    return {"id": "ch-" + _slug(ch), "name": ch, "zh": ch, "cat": person.get("cat", "ai"),
            "tier": 3, "lang": "en", "kind": "youtube", "residential": True, "desc": "",
            "rubric": "people", "person": _who(person)}


def _who(person: dict) -> str:
    zh, en = person.get("zh") or "", person.get("en") or ""
    return f"{zh}（{en}）" if zh and en else (zh or en)


MAX_TRIES = 3                # 视频本身的失败（成稿没过线、出错）累计这么多次就不再试
RETRY_DAYS = 7               # 视频本身失败过的，隔这么多天才再试一次
# **限流不是视频的错。** 11-19 第一轮：12 条里 4 条栽在 YouTube 限流（字幕取不到 →
# transcript-transient；选题只看到简介 → triage-unreliable），记账全是 soft。
# 原来 soft 和硬失败一起数到 3 就放弃——限流几轮，好视频就永久出局了。
TRANSIENT = ("transcript-transient", "triage-unreliable")
TRANSIENT_DAYS = 2
TRANSIENT_MAX = 8


def retry_ok(f: dict | None, today: dt.date | None = None) -> bool:
    """失败记录（state.fail）允不允许这一轮再试。

    为什么要单独管：run.process 的失败重试是给 feed 驱动的主轮设计的——主轮每天只看
    最近几天的集，过期自然不再碰。人物线索表是一张固定清单，失败的那条每轮都会被
    重新排到最前面，每次都重付一遍选题和取稿的钱。
    """
    if not f:
        return True
    hard, soft = int(f.get("n") or 0), int(f.get("soft") or 0)
    if f.get("why") == "too-long" or hard >= MAX_TRIES or soft >= TRANSIENT_MAX:
        return False
    transient = f.get("why") in TRANSIENT
    if not transient and hard + soft >= MAX_TRIES:
        return False
    at = str(f.get("at") or "")[:10]
    try:
        last = dt.date.fromisoformat(at)
    except ValueError:
        return True
    wait = TRANSIENT_DAYS if transient else RETRY_DAYS
    return ((today or dt.date.today()) - last).days >= wait


def _stale_off_brief(prior) -> bool:
    if not isinstance(prior, dict) or prior.get("skip") != "off-brief":
        return False
    from lib import triage
    return prior.get("rubric") != triage.rubric_id({"rubric": "people"})


def order(videos: list[dict]) -> list[dict]:
    """官方的在前，搬运的在后；各自最新的在前。出处清楚的先做。"""
    off = [v for v in videos if not v.get("re")]
    rep = [v for v in videos if v.get("re")]
    by_date = lambda xs: sorted(xs, key=lambda v: v.get("d") or "", reverse=True)
    return by_date(off) + by_date(rep)


def pending(blob: dict, state: dict, have_yt: set[str]) -> list[tuple[dict, dict]]:
    """还没做过的线索：官方在前、最新在前。三个人交替排，免得一个人占满每一轮。"""
    import run as R
    done_keys = set(state.get("done") or {})
    fails = state.get("fail") or {}
    queues = []
    for p in blob.get("people") or []:
        q = []
        for v in order(p.get("videos") or []):
            if v["yt"] in have_yt:
                continue
            key = R.eid(channel_source(v.get("ch") or "", p, _registered())["id"], v["yt"])
            # 发了、被选题拦了、片子太短、重复——都在 state.done 里，不再做。
            # 例外：被选题拦下、而判它的那把尺子已经改了的，重新判（和 run._verdict_is_stale 同理）。
            prior = (state.get("done") or {}).get(key)
            if key in done_keys and not _stale_off_brief(prior):
                continue
            if not retry_ok(fails.get(key)):
                continue
            q.append((p, v))
        queues.append(q)
    # 同一支视频可能在两个人的表里（马斯克和黄仁勋同台的那场）：只排一次
    out, seen = [], set()
    while any(queues):
        for q in queues:
            while q:
                p, v = q.pop(0)
                if v["yt"] not in seen:
                    seen.add(v["yt"])
                    out.append((p, v))
                    break
    return out


_REG: list[dict] = []


def _registered() -> list[dict]:
    if not _REG:
        _REG.extend(json.loads((DATA / "sources.json").read_text())["sources"])
    return _REG


def lead_meta(v: dict) -> dict | None:
    """线索表里已经有标题、时长、日期，就不去敲 YouTube 的 watch 页。

    addvideo.meta 每条都要读一次 watch 页——又一次 YouTube 请求，而补跑卡在 YouTube
    限流上（2026-11-19）。线索缺字段时才回落到 meta()。封面走 i.ytimg.com，不算在内。
    """
    if not (v.get("t") and v.get("sec")):
        return None
    from addvideo import yt_thumb
    return {"title": v["t"], "duration": int(v["sec"]), "published": "",
            "image": yt_thumb(v["yt"])}


def event_date(lead: str, upload: str) -> str:
    """这段话是哪天说的。线索里的日期是**事件日**，YouTube 的是**上传日**——
    乔布斯 2011-06-07 在库比蒂诺市议会的那次，上传日是 2012-02-02。
    线索日期完整就用它；只到月（「2026-11」）或年时，上传日落在同一个月／年里就用上传日，
    否则取那个月／年的第一天。"""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", lead):
        return lead
    up = upload[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", upload) else ""
    if re.fullmatch(r"\d{4}(-\d{2})?", lead):
        if up.startswith(lead):
            return up
        return lead + ("-01" if len(lead) == 7 else "-01-01")
    return up or dt.date.today().isoformat()


TRIES_PER_PUBLISH = 3        # 每轮最多试 n×3 条：选题拦下的很多（干跑 9 条过了 3 条），只试 n 条一轮只出一两篇


def _audio_mode(T, why: str) -> bool:
    T.hold_captions(12 * 3600)       # 整个进程不再要字幕
    from lib.util import log
    log(f"  {why}：后面的线索改走音频＋本机转写（不花 token、不再请求字幕）")
    return True


def run_next(n: int, dry: bool) -> int:
    """这一轮**发出** n 篇为止，最多试 n×TRIES_PER_PUBLISH 条。

    「每轮 4 篇」是用户定的产出，不是尝试数。被选题拦下的只花一次选题钱（几百 token）
    和一次字幕请求（或一次本机转写，见下面的 audio）；真正贵的深读和评审只发生在通过的那几条上。"""
    import run as R
    from addvideo import meta
    from lib.util import log
    from lib import transcript as T
    blob = load()
    have = set()
    for f in (DATA / "episodes").glob("*.json"):
        try:
            y = json.loads(f.read_text()).get("youtube_id")
        except Exception:
            continue
        if y:
            have.add(y)
    # **和 run.py 抢同一把跑批锁。** 补人物频道（scripts/backfill-people.sh）在两班日更之间连着跑，
    # 不拿锁的话会和日更同时写 state.json——并发写账本静默丢数据，出过不止一次。
    # 日更里这一步排在 run.py 退出之后，锁是空的；日更在跑时补跑拿不到，就让。
    if not dry and not R.acquire_run_lock():
        log("人物频道：跑批锁被占着（日更或补存量在跑），这一轮不做")
        return 0
    state = R.load_state()
    todo = pending(blob, state, have)
    cap = n * TRIES_PER_PUBLISH
    log(f"人物频道：待做 {len(todo)} 条线索，本轮发 {n} 篇为止（最多试 {cap} 条）")
    # **字幕一被限流就改走音频。** 字幕是 YouTube 最先限流的请求：2026-11-19 → 20 补跑一天一夜，
    # 几乎每批都是一两条之后就 429，熔断 30 分钟、歇 30–120 分钟，一共发了 2 篇。
    # 而同一时刻下音频照常（实测 15 分钟的音频 6 秒下完）。本机转写不花 token，一小时的视频约 5 分钟。
    # 先转写、落进 .cache/tr，选题样本和成稿都从那份缓存读，字幕一次都不再要。
    # PEOPLE_AUDIO_FIRST=1（补跑脚本设）从第一条就走音频：补跑量大，迟早会撞上限流。
    audio = (os.environ.get("PEOPLE_AUDIO_FIRST") == "1" and not dry
             and _audio_mode(T, "补跑：一开始就走音频"))
    published = tried = streak = 0
    results: dict[str, int] = {}
    stopped = ""
    t0 = iso_now()
    for p, v in todo:
        if published >= n or tried >= cap:
            break
        if streak >= THROTTLE_STOP:
            # 连着几条都是限流：再试只会继续撞墙、给更多好视频记上失败
            stopped = f"连续 {streak} 条限流，本轮提前停"
            log(f"  {stopped}")
            break
        if not audio and T.yt_cooling():
            audio = _audio_mode(T, "字幕被限流")
        tried += 1
        s = channel_source(v.get("ch") or "", p, _registered())
        m = lead_meta(v) or meta(v["yt"]) or {}
        title = m.get("title") or v.get("t")
        if not title:
            log(f"  {v['yt']} 拿不到标题，跳过")
            continue
        pub = event_date(v.get("d") or "", m.get("published") or "")
        ep = {"source_id": s["id"], "source": s["name"], "guid": v["yt"], "title": title,
              "link": "https://www.youtube.com/watch?v=" + v["yt"],
              "published": dt.datetime.fromisoformat(pub + "T12:00:00+00:00"),
              "duration": m.get("duration") or v.get("sec") or None,
              "audio": "", "image": m.get("image") or "", "notes": "",
              "episode_no": "", "explicit": False, "transcripts": [],
              "youtube_id": v["yt"], "feed_kind": "youtube",
              "_src": s, "_key": R.eid(s["id"], v["yt"])}
        log(f"  [{p['zh']}] {v.get('d')} · {s['name']} · {title[:60]}")
        if audio and not dry and not T.cached(ep):
            try:
                T.acquire(ep, s.get("lang") or "en", src=s, allow=("asr",),
                          asr_after_throttle=True)
            except Exception as ex:
                log(f"    音频转写失败：{type(ex).__name__}")
            # 拿不到也照常往下走：R.process 会按限流记软失败（字幕已停），连着三条就整轮停
        r = R.process(ep, state, dry=dry)
        log(f"    结果：{r}")
        results[r] = results.get(r, 0) + 1
        if r == "published":
            published += 1
        streak = streak + 1 if hit_throttle(state, ep["_key"], t0, r) else 0
        if not dry:
            R.save_state(state)
    log(f"人物频道：本轮试 {tried} 条，发布 {published} 篇")
    if not dry:
        record_run({"at": iso_now(), "tried": tried, "published": published,
                    "results": results, "pending": len(todo) - tried, "stopped": stopped})
    return 0


THROTTLE_STOP = 3


def hit_throttle(state: dict, key: str, since: str, result: str) -> bool:
    """这一条是不是**这一轮**撞上了限流。只看本轮写下的失败记录——上一轮留下的不算。"""
    if result == "published":
        return False
    f = (state.get("fail") or {}).get(key) or {}
    return f.get("why") in TRANSIENT and str(f.get("at") or "") >= since
RUNS = DATA / "people-runs.json"
KEEP_RUNS = 40               # 一天两轮，留 20 天


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_run(row: dict, path: pathlib.Path | None = None) -> None:
    """每轮留痕：体检读它判「人物频道在不在产出」（healthcheck.check_people_channel）。
    这一步在日更里是 `|| echo 不致命`，不留痕的话它坏了、或长期零产出，不会有任何输出提到。"""
    path = path or RUNS
    try:
        rows = json.loads(path.read_text()) if path.exists() else []
    except Exception:
        rows = []
    rows = (rows + [row])[-KEEP_RUNS:]
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n")


# ---------------------------------------------------------------- 导入线索


def _same_talk(a: dict, b: dict) -> bool:
    """同一场的两份上传：同一天、时长差 3% 以内（搬运号常整场转载，标题各起各的）。"""
    sa, sb = a.get("sec") or 0, b.get("sec") or 0
    return bool(a.get("d")) and a.get("d") == b.get("d") and sa and sb \
        and abs(sa - sb) <= 0.03 * max(sa, sb)


def pick(rows: list[dict]) -> list[dict]:
    """从对方索引里挑线索，只留事实字段。官方优先：同一场有官方版就不要搬运版。"""
    off, rep_ = [], []
    for v in rows:
        if v.get("p") != "yt" or not re.fullmatch(r"[\w-]{11}", str(v.get("id") or "")):
            continue
        if v.get("k") not in KEEP_KINDS:
            continue
        official = v.get("tier") == "officiel"
        if (v.get("sec") or 0) < (MIN_SECS if official else MIN_SECS_REUPLOAD):
            continue
        row = {"yt": v["id"], "t": v.get("t") or "", "ch": v.get("ch") or "",
               "d": v.get("d") or "", "sec": v.get("sec") or 0, "k": v.get("k")}
        if not official:
            row["re"] = 1
        (off if official else rep_).append(row)
    keep = list(off)
    for r in rep_:
        if not any(_same_talk(r, k) for k in keep):
            keep.append(r)
    return keep


def import_index(pid: str, html_path: str) -> int:
    """从对方页面的本地副本里读视频索引，只留事实字段。"""
    t = pathlib.Path(html_path).read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"window\.__V\s*=\s*(\[.*?\]);", t, re.S)
    if not m:
        print("页面里没有视频索引（window.__V）"); return 1
    rows = json.loads(m.group(1))
    keep = pick(rows)
    blob = load()
    for p in blob["people"]:
        if p["id"] == pid:
            old = {x["yt"] for x in p.get("videos") or []}
            p["videos"] = (p.get("videos") or []) + [x for x in keep if x["yt"] not in old]
            p["videos"].sort(key=lambda x: x.get("d") or "", reverse=True)
            break
    else:
        print(f"people.json 里没有 {pid}，先手写这个人的条目"); return 1
    PEOPLE.write_text(json.dumps(blob, ensure_ascii=False, indent=1) + "\n")
    n_re = sum(1 for x in keep if x.get("re"))
    print(f"{pid}：索引 {len(rows)} 条，保留 {len(keep)} 条（官方 {len(keep) - n_re}、搬运 {n_re}；"
          f"{'/'.join(KEEP_KINDS)}；官方 ≥{MIN_SECS // 60} 分钟、搬运 ≥{MIN_SECS_REUPLOAD // 60} 分钟）")
    return 0


def status() -> int:
    blob = load()
    eps = [json.loads(f.read_text()) for f in (DATA / "episodes").glob("*.json")]
    eps.sort(key=lambda x: x.get("published") or "", reverse=True)
    mem = members(eps, blob["people"])
    for p in blob["people"]:
        print(f"{p['zh']:<6} 线索 {len(p.get('videos') or [])} 条 · 站上 {len(mem[p['id']])} 篇")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--next", type=int, metavar="N")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--import", dest="imp", nargs=2, metavar=("PERSON", "HTML"))
    a = ap.parse_args()
    if a.imp:
        return import_index(*a.imp)
    if a.next:
        return run_next(a.next, a.dry_run)
    return status()


if __name__ == "__main__":
    raise SystemExit(main())
