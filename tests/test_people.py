"""人物频道（pipeline/people.py + build.py 的人物页）。

测的是会骗人的那几处：
  · 归属：提到一次不算本人出场；「转述某人」不算；同姓的别人（Greg Jensen）不算；
  · 补库：失败的线索不许每轮重排到最前面重付钱；事件日不能被上传日顶掉；
  · 产物：盘上的 people/ 和数据现算的结果一致，首页挂着入口（不挂就是孤儿页），sitemap 里有。
"""
import datetime as dt
import json
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))

import people as P  # noqa: E402

MUSK = {"id": "musk", "zh": "马斯克", "en": "Elon Musk", "aliases": ["Elon Musk", "马斯克"],
        "videos": [{"yt": "AAAAAAAAAAA", "d": "2026-10-21", "ch": "Some Channel"}]}
JENSEN = {"id": "jensen", "zh": "黄仁勋", "en": "Jensen Huang",
          "aliases": ["Jensen Huang", "黄仁勋"], "videos": []}


def ep(slug, spk=(), yt=None):
    return {"slug": slug, "youtube_id": yt,
            "digest": {"quotes": [{"spk": s} for s in spk], "points": []}}


class WhoCountsAsSpeaking(unittest.TestCase):
    def test_lead_list_hit_counts_even_without_speaker_tags(self):
        m = P.members([ep("a", yt="AAAAAAAAAAA")], [MUSK])
        self.assertEqual([x["slug"] for x in m["musk"]], ["a"])

    def test_enough_own_lines_count(self):
        m = P.members([ep("a", ["Elon Musk"] * P.PEER_MIN)], [MUSK])
        self.assertEqual(len(m["musk"]), 1)

    def test_a_mention_is_not_an_appearance(self):
        # Hard Fork 那集讨论黄仁勋，归给他的只有 1 条
        m = P.members([ep("a", ["Jensen Huang"] + ["Kevin Roose"] * 10)], [JENSEN])
        self.assertEqual(m["jensen"], [])

    def test_paraphrase_is_not_the_person_talking(self):
        m = P.members([ep("a", ["Nathan Lambert（转述Jensen Huang）"] * 5)], [JENSEN])
        self.assertEqual(m["jensen"], [])

    def test_another_person_with_the_same_surname_is_not_him(self):
        # 实测：Odd Lots 里 16 条「Greg Jensen」被裸的 "jensen" 命中
        m = P.members([ep("a", ["Greg Jensen"] * 16)], [JENSEN])
        self.assertEqual(m["jensen"], [])

    def test_order_follows_the_input(self):
        eps = [ep(s, yt="AAAAAAAAAAA" if s == "x" else None, spk=["Elon Musk"] * 3)
               for s in ("x", "y", "z")]
        self.assertEqual([x["slug"] for x in P.members(eps, [MUSK])["musk"]], ["x", "y", "z"])


class FailedLeadsDoNotEatEveryRound(unittest.TestCase):
    T = dt.date(2026, 11, 18)

    def test_untried_is_ok(self):
        self.assertTrue(P.retry_ok(None, self.T))

    def test_recent_failure_waits(self):
        self.assertFalse(P.retry_ok({"n": 1, "at": "2026-11-15T00:00:00Z"}, self.T))

    def test_old_failure_gets_another_go(self):
        self.assertTrue(P.retry_ok({"n": 1, "at": "2026-11-01T00:00:00Z"}, self.T))

    def test_gives_up_after_max_tries(self):
        self.assertFalse(P.retry_ok({"n": 2, "soft": 1, "at": "2026-01-01T00:00:00Z"}, self.T))

    def test_too_long_is_not_retried(self):
        self.assertFalse(P.retry_ok({"n": 0, "why": "too-long", "at": "2026-01-01"}, self.T))


class PendingQueue(unittest.TestCase):
    def setUp(self):
        import run as R
        self.R = R
        self.a = {"id": "a", "zh": "甲", "aliases": [], "videos": [
            {"yt": "a1", "d": "2026-01-01", "ch": "X"}, {"yt": "a2", "d": "2026-06-01", "ch": "X"},
            {"yt": "a3", "d": "2026-03-01", "ch": "X"}]}
        self.b = {"id": "b", "zh": "乙", "aliases": [], "videos": [
            {"yt": "b1", "d": "2025-01-01", "ch": "Y"}]}

    def key(self, p, yt):
        return self.R.eid(P.channel_source("X" if p is self.a else "Y", p, [])["id"], yt)

    def test_newest_first_and_people_take_turns(self):
        q = P.pending({"people": [self.a, self.b]}, {"done": {}, "fail": {}}, set())
        self.assertEqual([v["yt"] for _, v in q], ["a2", "b1", "a3", "a1"])

    def test_done_published_and_recently_failed_are_skipped(self):
        from lib import triage
        st = {"done": {self.key(self.a, "a2"): {"skip": "off-brief",
                                                "rubric": triage.rubric_id({"rubric": "people"})}},
              "fail": {self.key(self.a, "a3"): {"n": 1, "at": dt.date.today().isoformat()}}}
        q = P.pending({"people": [self.a, self.b]}, st, {"b1"})
        self.assertEqual([v["yt"] for _, v in q], ["a1"])


class PeopleUseTheirOwnRubric(unittest.TestCase):
    """用户 11-18：人物视频的标准是「有没有值得学习的信息和内容」，和普通播客不同。"""

    def test_every_people_source_uses_the_people_rubric_and_the_score_gate(self):
        import run as R
        reg = [{"id": "allin", "name": "All-In Podcast", "tier": 1, "kind": "rss"}]
        for s in (P.channel_source("All-In Podcast", MUSK, reg),
                  P.channel_source("Unknown Channel", MUSK, reg)):
            self.assertEqual(s["rubric"], "people")
            # tier 1/2 只拦广告、不看分 —— 那样这把尺子就形同虚设
            self.assertNotIn(s["tier"], R.NO_FILTER_TIERS)
            self.assertEqual(s["kind"], "youtube")       # 选题要喂字幕样本
            self.assertIn("Elon Musk", s["person"])
        self.assertEqual(reg[0]["tier"], 1, "登记信源本身不许被改")

    def test_triage_picks_the_rubric_by_source(self):
        from lib import triage
        self.assertIs(triage._system_for({"rubric": "people"}), triage.PEOPLE_SYSTEM)
        self.assertIs(triage._system_for({"id": "x"}), triage.SYSTEM)
        self.assertNotEqual(triage.rubric_id({"rubric": "people"}), triage.rubric_id())

    def test_the_regular_rubric_fingerprint_did_not_move(self):
        # 改了 SYSTEM 的指纹 = 账本里几百条「不做」全部重开、重付一遍选题钱
        from lib import triage
        self.assertEqual(triage.rubric_id(), "3960d9cba5")

    def test_a_skip_judged_by_an_old_people_rubric_is_reopened(self):
        import run as R
        from lib import triage
        a = {"id": "a", "zh": "甲", "aliases": [], "videos": [{"yt": "a1", "d": "2026-01-01", "ch": "X"}]}
        key = R.eid(P.channel_source("X", a, [])["id"], "a1")
        old = {"done": {key: {"skip": "off-brief", "rubric": "oldoldold0"}}, "fail": {}}
        cur = {"done": {key: {"skip": "off-brief", "rubric": triage.rubric_id({"rubric": "people"})}},
               "fail": {}}
        self.assertEqual(len(P.pending({"people": [a]}, old, set())), 1)
        self.assertEqual(len(P.pending({"people": [a]}, cur, set())), 0)


class ReuploadLeads(unittest.TestCase):
    """用户 11-18：「搬运的里面有价值的长视频也需要」。"""

    def row(self, i, tier="officiel", sec=3600, d="2026-01-01", k="interview"):
        return {"p": "yt", "id": f"{i:011d}", "tier": tier, "sec": sec, "d": d, "k": k,
                "t": "x", "ch": "c", "quotes": ["对方的编辑成果"]}

    def test_long_reuploads_are_kept_and_marked(self):
        got = P.pick([self.row(1, tier="réupload", sec=P.MIN_SECS_REUPLOAD)])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["re"], 1)
        self.assertNotIn("quotes", got[0])

    def test_short_reuploads_are_dropped_but_short_officials_are_not(self):
        sec = P.MIN_SECS_REUPLOAD - 60
        self.assertEqual(P.pick([self.row(1, tier="réupload", sec=sec)]), [])
        self.assertEqual(len(P.pick([self.row(2, sec=sec)])), 1)

    def test_a_reupload_of_an_official_talk_is_dropped(self):
        got = P.pick([self.row(1, tier="réupload", sec=3610), self.row(2, sec=3600)])
        self.assertEqual([x["yt"] for x in got], ["00000000002"])

    def test_the_same_day_but_a_different_talk_is_kept(self):
        got = P.pick([self.row(1, tier="réupload", sec=1800), self.row(2, sec=3600)])
        self.assertEqual(len(got), 2)

    def test_officials_go_first_then_newest(self):
        vs = [{"yt": "r_new", "d": "2026-10-01", "re": 1}, {"yt": "o_old", "d": "2020-01-01"},
              {"yt": "o_new", "d": "2026-01-01"}]
        self.assertEqual([v["yt"] for v in P.order(vs)], ["o_new", "o_old", "r_new"])


class EventDateNotUploadDate(unittest.TestCase):
    def test_full_lead_date_wins(self):
        self.assertEqual(P.event_date("2011-06-07", "2012-02-02"), "2011-06-07")

    def test_month_only_lead_uses_upload_inside_that_month(self):
        self.assertEqual(P.event_date("2026-11", "2026-11-10"), "2026-11-10")

    def test_month_only_lead_ignores_a_later_upload(self):
        self.assertEqual(P.event_date("1996-05", "2012-02-02"), "1996-05-01")

    def test_year_only(self):
        self.assertEqual(P.event_date("1980", ""), "1980-01-01")


class ChannelAttribution(unittest.TestCase):
    def test_registered_show_keeps_its_own_page(self):
        reg = [{"id": "allin", "name": "All-In Podcast", "zh": "All-In"}]
        self.assertEqual(P.channel_source("All-In Podcast", MUSK, reg)["id"], "allin")

    def test_unregistered_channel_gets_a_page_but_no_feed(self):
        s = P.channel_source("The Builders with Walter Isaacson", MUSK, [])
        self.assertEqual(s["id"], "ch-the-builders-with-walter-isaacson")
        self.assertNotIn("feed", s)
        self.assertTrue(s["residential"])


class LeadsCarryFactsOnly(unittest.TestCase):
    """线索表只许有事实字段。对方的语录、译文、文稿、主题标注是对方的编辑成果，不进本站仓库。"""

    def test_only_fact_fields(self):
        blob = P.load()
        allowed = {"yt", "t", "ch", "d", "sec", "k", "re"}
        for p in blob["people"]:
            for v in p.get("videos") or []:
                self.assertLessEqual(set(v), allowed, f"{p['id']} {v.get('yt')} 多了字段")
                self.assertRegex(v["yt"], r"^[\w-]{11}$")

    def test_credit_is_shown(self):
        self.assertTrue(P.load().get("credit", {}).get("url"))


class BuiltPeoplePages(unittest.TestCase):
    """盘上的产物和数据现算的一致（仓库里提交的是构建结果，CI 也会再构建一次比对）。"""

    @classmethod
    def setUpClass(cls):
        eps = [json.loads(f.read_text()) for f in (ROOT / "data" / "episodes").glob("*.json")]
        cls.rows = [(p["id"], len(r)) for p, r in
                    zip(P.load()["people"], P.members(eps, P.load()["people"]).values()) if r]

    def test_every_person_with_reads_has_a_page_and_no_extra_pages(self):
        want = {pid for pid, _ in self.rows}
        d = ROOT / "people"
        have = {x.name for x in d.iterdir() if x.is_dir()} if d.exists() else set()
        self.assertEqual(have, want)

    def test_home_links_every_person_page(self):
        home = (ROOT / "index.html").read_text()
        for pid, n in self.rows:
            m = re.search(rf'href="/podcast/people/{pid}/">[^<]+<span class="n">(\d+)</span>', home)
            self.assertIsNotNone(m, f"首页没有 {pid} 的入口")
            self.assertEqual(int(m.group(1)), n, f"首页上 {pid} 的篇数和数据对不上")

    def test_person_page_lists_exactly_its_reads(self):
        for pid, n in self.rows:
            html = (ROOT / "people" / pid / "index.html").read_text()
            # 只数卡片本身：class="card" 或 "card hero"；card-body / card-foot 是卡片里的块
            self.assertEqual(len(re.findall(r'class="card(?:"| )', html)), n, pid)

    def test_sitemap_has_the_people_pages(self):
        sm = (ROOT / "sitemap.xml").read_text()
        for pid, _ in self.rows:
            self.assertIn(f"/podcast/people/{pid}/</loc>", sm)


class DailyRunWiring(unittest.TestCase):
    def test_people_step_runs_after_catchup_and_before_the_build(self):
        sh = (ROOT / "scripts" / "local-daily.sh").read_text()
        i_catch = sh.find("pipeline/run.py --catchup")
        i_people = sh.find("pipeline/people.py --next")
        i_build = sh.find("python3 pipeline/build.py\n", i_catch)
        self.assertTrue(0 < i_catch < i_people < i_build, (i_catch, i_people, i_build))

    def test_people_step_is_skipped_for_a_manual_single_source_run(self):
        sh = (ROOT / "scripts" / "local-daily.sh").read_text()
        block = sh[sh.find('PEOPLE_LIMIT:=4'):][:400]
        self.assertIn('[ -z "$ONLY" ]', block)


class ThrottleIsNotTheVideosFault(unittest.TestCase):
    """11-19 第一轮：12 条里 4 条栽在 YouTube 限流。限流不该让好视频永久出局。"""
    T = dt.date(2026, 11, 19)

    def test_transient_failure_waits_two_days_not_seven(self):
        f = {"n": 0, "soft": 1, "why": "transcript-transient", "at": "2026-11-18T00:00:00Z"}
        self.assertFalse(P.retry_ok(f, self.T))
        self.assertTrue(P.retry_ok(dict(f, at="2026-11-17T00:00:00Z"), self.T))

    def test_three_throttles_do_not_kill_a_lead(self):
        f = {"n": 0, "soft": 3, "why": "triage-unreliable", "at": "2026-11-01T00:00:00Z"}
        self.assertTrue(P.retry_ok(f, self.T))

    def test_but_endless_throttles_eventually_stop(self):
        f = {"n": 0, "soft": P.TRANSIENT_MAX, "why": "triage-unreliable", "at": "2026-01-01"}
        self.assertFalse(P.retry_ok(f, self.T))

    def test_hard_failures_still_give_up_at_three(self):
        f = {"n": 3, "soft": 0, "why": "transcript-transient", "at": "2026-01-01"}
        self.assertFalse(P.retry_ok(f, self.T))

    def test_only_this_rounds_records_count_toward_the_streak(self):
        st = {"fail": {"k": {"why": "transcript-transient", "at": "2026-11-18T00:00:00Z"}}}
        self.assertFalse(P.hit_throttle(st, "k", "2026-11-19T03:00:00Z", "off-brief"))
        st["fail"]["k"]["at"] = "2026-11-19T03:05:00Z"
        self.assertTrue(P.hit_throttle(st, "k", "2026-11-19T03:00:00Z", "no-transcript"))
        self.assertFalse(P.hit_throttle(st, "k", "2026-11-19T03:00:00Z", "published"))


class SharedVideoQueuedOnce(unittest.TestCase):
    def test_a_video_in_two_peoples_lists_is_tried_once(self):
        # 实测：America.Gov 那场同时在马斯克和黄仁勋的表里，一轮试了两次
        a = {"id": "a", "zh": "甲", "aliases": [], "videos": [{"yt": "same", "d": "2026-10-01", "ch": "X"}]}
        b = {"id": "b", "zh": "乙", "aliases": [], "videos": [{"yt": "same", "d": "2026-10-01", "ch": "X"},
                                                            {"yt": "b2", "d": "2026-09-01", "ch": "X"}]}
        q = P.pending({"people": [a, b]}, {"done": {}, "fail": {}}, set())
        self.assertEqual([v["yt"] for _, v in q], ["same", "b2"])


class RunsAreLoggedForTheHealthcheck(unittest.TestCase):
    def test_log_keeps_the_last_runs(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            f = pathlib.Path(d) / "runs.json"
            for i in range(P.KEEP_RUNS + 5):
                P.record_run({"at": str(i)}, f)
            rows = json.loads(f.read_text())
            self.assertEqual(len(rows), P.KEEP_RUNS)
            self.assertEqual(rows[-1]["at"], str(P.KEEP_RUNS + 4))

    def run_check(self, runs, now="2026-11-19T12:00:00"):
        import tempfile
        import healthcheck as H
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            (d / "people.json").write_text(json.dumps({"people": [{"videos": [{"yt": "x"}]}]}))
            if runs is not None:
                (d / "people-runs.json").write_text(json.dumps(runs))
            old, H.DATA = H.DATA, d
            try:
                r = H.Report()
                H.check_people_channel(r, dt.datetime.fromisoformat(now).replace(tzinfo=dt.timezone.utc))
            finally:
                H.DATA = old
        return r

    def test_stalled_step_is_a_hard_failure(self):
        r = self.run_check([{"at": "2026-11-16T02:00:00Z", "tried": 4, "published": 1, "pending": 700}])
        self.assertTrue(r.bad, r.warn)

    def test_four_empty_rounds_warn(self):
        row = {"at": "2026-11-19T02:00:00Z", "tried": 12, "published": 0, "pending": 700,
               "results": {"off-brief": 12}}
        r = self.run_check([row] * 4)
        self.assertFalse(r.bad)
        self.assertTrue(any("一篇没发" in w for w in r.warn), r.warn)

    def test_a_healthy_round_is_green(self):
        r = self.run_check([{"at": "2026-11-19T02:00:00Z", "tried": 12, "published": 3, "pending": 700}])
        self.assertFalse(r.bad)
        self.assertFalse(r.warn)
        self.assertTrue(r.ok)

    def test_never_ran_is_a_note_not_a_failure(self):
        r = self.run_check(None)
        self.assertFalse(r.bad)
        self.assertTrue(r.warn)


class ArticlesLinkToTheirPerson(unittest.TestCase):
    def test_every_member_article_links_its_person_page(self):
        eps = [json.loads(f.read_text()) for f in (ROOT / "data" / "episodes").glob("*.json")]
        ps = P.load()["people"]
        for p, rows in zip(ps, P.members(eps, ps).values()):
            for x in rows:
                html = (ROOT / "p" / x["slug"] / "index.html").read_text()
                self.assertIn(f'href="/podcast/people/{p["id"]}/"', html, x["slug"])


class PeopleVerdictsAreNotCoreVerdicts(unittest.TestCase):
    """2026-11-19 CI 红：Lex（优质源）的夜市那集按人物尺子判了 4 分，账本 src=lex，
    两条「优质源只拦广告」的守护把它当成错拦。人物尺子判的要能被认出来——而且只认它。"""

    def setUp(self):
        import run as R
        from lib import triage
        self.R, self.t = R, triage

    def v(self, rubric):
        return {"skip": "off-brief", "src": "lex", "tier": 3, "score": 4.0, "kind": "闲聊",
                "rubric": rubric}

    def test_people_rubric_verdict_is_recognised(self):
        self.assertTrue(self.R.is_people_verdict(self.v(self.t.rubric_id({"rubric": "people"}))))

    def test_regular_verdicts_are_not_excused(self):
        self.assertFalse(self.R.is_people_verdict(self.v(self.t.rubric_id())))
        self.assertFalse(self.R.is_people_verdict(self.v("oldoldold0")))
        self.assertFalse(self.R.is_people_verdict(self.v(None)))

    def test_the_feed_side_does_not_reopen_people_verdicts(self):
        v = self.v(self.t.rubric_id({"rubric": "people"}))
        self.assertFalse(self.R._verdict_is_stale(v, {"tier": 1}, self.t.rubric_id()))


class YoutubeCoversAreRealImages(unittest.TestCase):
    """2026-11-19：乔布斯斯坦福演讲的封面是 YouTube 的 404 灰色占位图。"""

    def test_thumb_falls_back_to_a_size_that_exists(self):
        import subprocess
        import addvideo as A
        seen = []

        def fake(cmd, **kw):
            seen.append(cmd[-1])
            code = "200" if cmd[-1].endswith("hqdefault.jpg") else "404"
            return subprocess.CompletedProcess(cmd, 0, stdout=code, stderr="")
        old, A.subprocess.run = A.subprocess.run, fake
        try:
            got = A.yt_thumb("Hd_ptbiPoXM")
        finally:
            A.subprocess.run = old
        self.assertTrue(got.endswith("/hqdefault.jpg"), got)
        self.assertTrue(seen[0].endswith("/maxresdefault.jpg"), "先试最大的那档")

    def test_cover_cache_refuses_an_http_error_body(self):
        import subprocess
        import cache_covers as C
        argv = []

        def boom(*a, **k):
            raise OSError("python 的证书库不认本机代理")

        def fake(cmd, **kw):
            argv.append(cmd)
            # curl -f 遇到 404：退出码 22、没有正文
            fail = "-f" in "".join(x for x in cmd if x.startswith("-") and not x.startswith("--"))
            return subprocess.CompletedProcess(cmd, 22 if fail else 0,
                                               stdout=b"" if fail else b"GIF89a-placeholder", stderr=b"404")
        old_u, old_r = C.urllib.request.urlopen, subprocess.run
        C.urllib.request.urlopen, subprocess.run = boom, fake
        try:
            with self.assertRaises(Exception):
                C._fetch("https://i.ytimg.com/vi/x/maxresdefault.jpg")
        finally:
            C.urllib.request.urlopen, subprocess.run = old_u, old_r
        self.assertTrue(argv, "没走到 curl 那条路")


if __name__ == "__main__":
    unittest.main()
