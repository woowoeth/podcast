"""把 POSTMORTEM.md 里的教训变成可执行的检查。

这些不测业务逻辑，测的是"我上次是怎么犯错的"。每一条都对应 POSTMORTEM 里一条
真实事故；纯文档防不住重犯，能断言的就断言。
"""
import os
import pathlib
import re
import subprocess
import json
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))


class PushLoopsMustFailLoudly(unittest.TestCase):
    """事故：推送重试 8 次全失败，步骤仍报 success，11 篇内容随 runner 销毁。"""

    def _steps(self, name):
        return (ROOT / ".github/workflows" / name).read_text()

    def test_every_push_retry_loop_checks_its_result(self):
        for wf in ("daily.yml", "backfill.yml", "rescore.yml"):
            body = self._steps(wf)
            # 不去匹配 do...done 的配对：循环体里一旦出现嵌套的 while/for，
            # 非贪婪的 `done` 会提前收口，判据就自己失效了（真发生过）。
            # 改成从 `for` 往后取一段窗口，看结果检查在不在里面。
            for m in re.finditer(r"for i in \$\(seq 1 \d+\); do", body):
                win = body[m.start():m.start() + 1800]
                if "git push" not in win:
                    continue
                self.assertTrue(
                    re.search(r'\$ok"?\s*!?=\s*1|exit 1|::error::', win),
                    f"{wf} 里有个含 git push 的重试循环没有检查结果")

    def test_the_guard_survives_a_nested_loop(self):
        """这条守护自己踩过的坑：判据去配 do…done，循环体里加了个嵌套 while 之后
        非贪婪的 done 提前收口，含 git push 的循环被跳过，检查静默失效。"""
        body = self._steps("daily.yml")
        self.assertIn("while read -r f", body)          # 确实有嵌套循环
        self.assertRegex(body, r'\[ "\$ok" = 1 \] \|\| \{|if \[ "\$ok" != 1 \]')

    def test_workflows_do_not_swallow_push_failure_with_bare_break(self):
        for wf in ("daily.yml", "backfill.yml"):
            body = self._steps(wf)
            self.assertNotIn("git push && break", body,
                             f"{wf} 用了 `git push && break`，循环结束后无从判断是否成功")


class GatesFailInTheRightDirection(unittest.TestCase):
    """事故：评审不可用时放行，等于评审一坏闸门就不存在。"""

    def test_review_fails_closed(self):
        from lib import review
        self.assertFalse(review.passes(None), "评审不可用必须拦下")

    def test_triage_fails_open_on_purpose(self):
        # 反向的例外要显式固定：选题闸门只是省钱的预筛，失灵时放行，
        # 让稿子走到成稿评审那道真闸门去
        from lib import triage
        self.assertTrue(triage.passes(None))


class TransientFailuresDoNotBurnRetries(unittest.TestCase):
    """事故：YouTube 429 与 CI 机器人拦截被记成 no-transcript，把有字幕的集永久拉黑。"""

    def test_transient_signal_exists(self):
        from lib import transcript as T
        self.assertTrue(hasattr(T, "last_was_transient"))

    def test_run_distinguishes_soft_from_hard_failures(self):
        src = (ROOT / "pipeline/run.py").read_text()
        self.assertIn("last_was_transient", src)
        self.assertIn("MAX_SOFT_FAILS", src)

    def test_reasoning_models_get_a_bigger_budget(self):
        # 事故：推理 token 计入 max_tokens，8000 被思考用光，content 返回空
        from lib import llm
        self.assertTrue(llm._REASONING.search("deepseek-reasoner"))
        self.assertIsNone(llm._REASONING.search("deepseek-chat"))


class PageStructureStaysValid(unittest.TestCase):
    """事故：单集页两个 h1；改完之后信源页和 404 变成零个。"""

    def test_every_built_page_has_exactly_one_h1(self):
        pages = [ROOT / "index.html", ROOT / "404.html",
                 ROOT / "sources/index.html"]
        pages += list((ROOT / "p").glob("*/index.html"))[:6]
        pages += list((ROOT / "s").glob("*/index.html"))[:3]
        for f in pages:
            if not f.exists():
                continue
            n = len(re.findall(r"<h1[ >]", f.read_text()))
            self.assertEqual(n, 1, f"{f.relative_to(ROOT)} 有 {n} 个 h1")

    def test_no_class_name_collides_between_components(self):
        # 事故：masthead 的 slogan 和卡片标签胶囊都叫 .tag，slogan 因此套上了边框
        css = (ROOT / "assets/site.css").read_text()
        globals_ = set(re.findall(r"(?m)^\.([a-z][\w-]*)\s*[{,]", css))
        # 父选择器要整段抓：`.ev.add .ev-what` 里的父是 `.ev.add`，只抓最后一节
        # 会得到 "add"，同族判断就失效了。声明块也要抓——判据得看这条规则到底改了
        # 什么，见下面 layout_only。
        nested = set(re.findall(
            r"((?:\.[a-z][\w-]*)+)\s+\.([a-z][\w-]*)\s*\{([^}]*)\}", css))
        allowed = {("hero", "cover"), ("ep-head", "kicker"), ("brand", "slogan"),
                   ("brand", "wordmark"), ("guide", "k"), ("empty", "h1")}

        def same_family(parent: str, child: str) -> bool:
            """同族的作用域覆盖不算串味：`.ev.add .ev-what` 只是给 `.ev-what`
            换个颜色，这是正常的层叠。当初的事故是**跨组件**撞名——报头的
            slogan 撞上卡片的 `.tag`，两个毫无关系的东西共用一个末端类名。"""
            for cls in parent.strip(".").split("."):
                stem = cls.split("-")[0]
                if child == stem or child.startswith(stem + "-"):
                    return True
            return False

        # 容器给子组件定位，是标准写法，不可能"串味"：外观是组件自己的事，
        # 位置是容器的事。当年的事故是外观被串（slogan 套上了标签胶囊的边框和
        # 底色），所以判据要看这条规则动的是位置还是外观。
        LAYOUT = re.compile(
            r"^(margin|padding|top|right|bottom|left|inset|width|height|flex|grid|"
            r"order|align|justify|place|gap|position|z-index|display|float|clear|"
            r"transform|translate)")

        def layout_only(decls: str) -> bool:
            props = [d.split(":")[0].strip() for d in decls.split(";") if ":" in d]
            return bool(props) and all(LAYOUT.match(x) for x in props)

        for parent, child, decls in nested:
            last = parent.strip(".").split(".")[-1]
            if child in globals_ and (last, child) not in allowed \
                    and not same_family(parent, child) \
                    and not layout_only(decls):
                self.fail(f"{parent} .{child} 的末端类名同时是全局规则 .{child}，"
                          f"会被串味；确认无害后加进 allowed")

    def test_positioning_a_component_is_not_a_collision(self):
        """容器给子组件定位不算串味。判据必须能分清「改位置」和「改外观」，
        否则只能靠 allowlist 越堆越长，而 allowlist 迟早会把真的串味放过去。"""
        css = (ROOT / "assets/site.css").read_text()
        self.assertIn(".kicker .share-btn{margin-left:12px}", css)    # 只改位置，放过
        # 组件必须自己声明排版，否则会继承 .kicker 的 uppercase 和字距
        self.assertIn("text-transform:none; letter-spacing:normal", css)

    def test_family_scoping_is_not_reported_as_a_collision(self):
        # 判据要分清「跨组件撞名」和「同组件内作用域覆盖」，不然只能靠 allowlist
        # 越堆越长，而 allowlist 迟早会把真的串味也放过去。
        css = (ROOT / "assets/site.css").read_text()
        self.assertIn(".ev.add .ev-what", css)     # 同族，必须放过
        self.assertIn(".ev-what{", css.replace(" ", ""))


class ScriptedEditsMustAssertTheirAnchor(unittest.TestCase):
    """事故：改代码的脚本锚点没匹配上，str.replace 静默什么都不做。"""

    def test_cli_backend_accepts_a_model_argument(self):
        import inspect
        from lib import llm
        self.assertIn("model", inspect.signature(llm._cli).parameters)

    def test_every_page_kind_emits_structured_data(self):
        # 事故：首页 JSON-LD 的插入没匹配上，自查才发现首页没有结构化数据
        import json
        for rel in ("index.html", "sources/index.html"):
            f = ROOT / rel
            if not f.exists():
                continue
            blobs = re.findall(r'<script type="application/ld\+json">(.*?)</script>',
                               f.read_text(), re.S)
            self.assertTrue(blobs, f"{rel} 没有结构化数据")
            json.loads(blobs[0])


class CuratorCannotSilentlyRewriteState(unittest.TestCase):
    """事故：还在调的策展器每轮直接写 sources.json，10 档旧标准的候选进了清单
    （含衍生源「硅谷101|中国版」），而我汇报时没核对清单被改成了什么样。"""

    def test_curate_has_a_dry_run(self):
        sys.path.insert(0, str(ROOT / "pipeline"))
        src = (ROOT / "pipeline/curate.py").read_text()
        self.assertIn("--dry-run", src, "策展必须能只出建议不写文件")
        self.assertIn("dry", src)

    def test_source_ids_survive_non_ascii(self):
        """事故：slug_for 把非 ASCII 全删，「42章经」塌成 42、「脑放电波」塌成 src。"""
        sys.path.insert(0, str(ROOT / "pipeline"))
        import curate
        ids = {curate.slug_for(n, set()) for n in
               ("42章经", "脑放电波", "AI实话实说", "硅谷101|中国版", "乱翻书")}
        self.assertEqual(len(ids), 5, "中文名不该塌成同一批 id")
        for i in ids:
            self.assertGreaterEqual(len(i), 4, f"id {i!r} 太短，没有辨识度")
            self.assertNotIn(i, {"42", "101", "src", "ai"})

    def test_name_match_keeps_all_past_failures_passing(self):
        """字符串相似度改了四版，每版失败的用例都留在这里——修 A 不能打破 B。"""
        sys.path.insert(0, str(ROOT / "pipeline"))
        import curate
        cases = [
            ("Patrick", "Patrick Boyle", True),                    # 第一版栽在这
            ("Patrick", "Patrick Bet-David Podcast", False),
            ("Tomorrow Today", "The Tomorrow Show", False),        # 第二版栽在这
            ("This Week in Tech (Audio)", "This Week in Startups", False),  # 第三版
            ("硅谷101|中国版", "硅谷101", True),
            ("Money Stuff", "Money Stuff: The Podcast", True),
            ("刘洺堉", "Learn Persian with Chai", False),
        ]
        for want, got, expect in cases:
            self.assertEqual(curate._name_match(want, got), expect,
                             f"{want!r} vs {got!r}")


class ScoringUsesTheWholeRange(unittest.TestCase):
    """事故（2 次）：评分全挤在及格线上——成稿评审 16 篇全是 8，策展 7 档全是 8.0。"""

    def test_prompts_warn_against_anchoring(self):
        from lib import review
        self.assertIn("把整个 0-10 区间用起来", review.SYSTEM)
        src = (ROOT / "pipeline/curate.py").read_text()
        self.assertIn("把区间用起来", src)

    def test_inconsistent_scores_are_rejected_by_code(self):
        """不指望模型自觉：理由里写着"补位有限"却给过线分的，用代码拦。"""
        sys.path.insert(0, str(ROOT / "pipeline"))
        import curate
        self.assertTrue(any(w in curate.HEDGE for w in ("重叠", "补位有限", "偏泛")))


class ValueIsNotOnlyNumbers(unittest.TestCase):
    """事故：把工程约束（能验证什么）写成了产品判断（什么是好内容），
    用"无可核对数据"拒掉了一集严肃的投资哲学访谈。"""

    def test_triage_accepts_argument_driven_value(self):
        from lib import triage
        self.assertIn("判断与框架", triage.SYSTEM)
        self.assertIn("能不能被反驳", triage.SYSTEM)

    def test_review_specificity_accepts_theses(self):
        from lib import review
        self.assertIn("判断型", review.SYSTEM)

    def test_curation_score_excludes_obtainability(self):
        """文稿可得性决定"能不能做"，不该进"该不该做"的分数。"""
        src = (ROOT / "pipeline/curate.py").read_text()
        self.assertIn("只评内容价值", src)
        self.assertIn("def prerequisites", src)


class DailyUpdateStaysWired(unittest.TestCase):
    """确保每日更新真的还开着——cron 被注释掉过一次。"""

    def test_daily_cron_is_enabled(self):
        import yaml
        d = yaml.safe_load((ROOT / ".github/workflows/daily.yml").read_text())
        on = d[True] if True in d else d["on"]
        self.assertIn("schedule", on, "daily.yml 的 cron 不在了，站不会自己更新")
        self.assertGreaterEqual(len(on["schedule"]), 1)

    def test_cloud_run_skips_sources_it_cannot_reach(self):
        body = (ROOT / ".github/workflows/daily.yml").read_text()
        self.assertIn("--skip-residential", body,
                      "云端必须跳过住宅 IP 专属源，否则每天白试并污染失败计数")

    def test_curation_runs_on_a_schedule(self):
        """信源不该一次挑完就固定：节目会停更、会转向、会把长访谈换成短切片。"""
        import yaml
        d = yaml.safe_load((ROOT / ".github/workflows/curate.yml").read_text())
        on = d[True] if True in d else d["on"]
        self.assertIn("schedule", on, "策展没有定时，信源清单会僵住")

    def test_curation_thresholds_need_a_sample(self):
        """淘汰判据必须要求最小样本量，否则会在噪声上动刀。"""
        sys.path.insert(0, str(ROOT / "pipeline"))
        import curate
        self.assertGreaterEqual(curate.MIN_TRIAGE_EVALS, 5)
        self.assertGreaterEqual(curate.MIN_PUBLISHED_FOR_REVIEW, 3)

    def test_source_removal_needs_a_persistent_failure(self):
        """第五次踩同一个坑：抖动被当成永久失效。YouTube 会对密集请求返 404、
        Substack 会 403，一次失败就删源等于把好源误删。"""
        sys.path.insert(0, str(ROOT / "pipeline"))
        import curate
        self.assertGreaterEqual(curate.DEAD_STREAK, 3)
        m = {"feed_ok": False, "fail_streak": 1, "published": 0, "no_transcript": 0,
             "age_days": 3, "triage_n": 0, "triage_pass": None, "review_median": None}
        self.assertIsNone(curate.judge("x", m), "一次失败不该触发移除")
        m["fail_streak"] = 3
        self.assertIsNotNone(curate.judge("x", m), "连续失败应触发移除")

    def test_ci_does_not_build_before_rebasing(self):
        """事故：run.py 发布后自己 build，留下未跟踪的 p/ 与 s/，
        紧接着的 git pull --rebase 报 "could not detach HEAD"，日更整轮失败。"""
        for wf in ("daily.yml", "backfill.yml"):
            body = (ROOT / ".github/workflows" / wf).read_text()
            self.assertIn("--no-build", body,
                          f"{wf} 没让 run.py 跳过 build，rebase 会被未跟踪的生成产物挡住")
            self.assertIn("git clean -fdq p s", body,
                          f"{wf} 缺少 rebase 前清理生成产物的兜底")

    def test_local_runner_commits_and_pushes(self):
        # 事故：我直接跑 run.py 而不是 local-daily.sh，8 篇产出躺在本地没上线
        sh = (ROOT / "scripts/local-daily.sh").read_text()
        self.assertIn("git push", sh)
        self.assertRegex(sh, r"commit -q -m")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class ScoringNeedsEvidenceNotReputation(unittest.TestCase):
    """收不收一档节目，曾经只凭「节目名 + 最新一集标题」判断。同一档节目两次跑分
    差 2 分，通过与否取决于理由里有没有出现对冲词——那是措辞运气，不是标准。"""

    def test_probe_keeps_a_transcript_excerpt(self):
        # 实测已经把文稿抓下来了，却只把字数传给模型，正文扔了。
        src = (ROOT / "pipeline" / "curate.py").read_text()
        self.assertIn('"excerpt": _excerpt(tr)', src)
        self.assertIn('"titles":', src)

    def test_score_prompt_carries_the_excerpt(self):
        src = (ROOT / "pipeline" / "curate.py").read_text()
        i = src.index("def score_candidate")
        body = src[i:src.index("def slug_for")]
        self.assertIn('c.get("excerpt")', body)
        self.assertIn("等距抽样", body)
        # 三级证据梯子：文稿 → 分集说明 → 确实没有。中间那级是必须的：
        # 中文播客前三层本来就取不到文稿，少了它整条中文信源线会被永久判死。
        self.assertIn('c.get("notes_sample")', body)
        self.assertIn("打分要保守", body)

    def test_missing_transcript_is_not_a_scoring_penalty(self):
        # 曾经因为「无文稿难核」把所有需要转写的中文节目一律扣到 4 分
        src = (ROOT / "pipeline" / "curate.py").read_text()
        i = src.index("def score_candidate")
        body = src[i:src.index("def slug_for")]
        self.assertIn("不进你的打分", body)
        self.assertIn("不要因为", body)

    def test_notes_sample_spans_several_episodes(self):
        import curate
        head = [{"title": f"第{i}集", "notes": "<p>" + "实测细节" * 120 + "</p>"}
                for i in range(5)]
        nt = curate._notes_sample(head)
        self.assertGreaterEqual(nt.count("【"), 2, "只取了一集说明")
        self.assertLessEqual(len(nt), curate.NOTES_CHARS + 1000)
        self.assertNotIn("<p>", nt, "HTML 没剥掉")
        # 一句话的说明不算证据
        self.assertEqual(curate._notes_sample([{"title": "x", "notes": "短"}]), "")

    def test_excerpt_samples_the_whole_episode(self):
        # 只看开头会被漂亮的片头骗过去，判不了"密度稳不稳"
        import curate
        segs = [{"t": i * 30, "text": ("头" if i < 5 else "尾") * 300}
                for i in range(60)]
        ex = curate._excerpt({"segments": segs})
        self.assertIn("尾", ex, "抽样没覆盖到后半段")
        self.assertLessEqual(len(ex), curate.EXCERPT_CHARS + 260)

    def test_excerpt_survives_a_transcript_without_segments(self):
        import curate
        self.assertEqual(curate._excerpt(None), "")
        self.assertTrue(curate._excerpt({"text": "甲" * 9000}))

    def test_hedge_catches_the_phrase_that_slipped_through(self):
        # "密度略逊于顶级" 拿了 8.0 分并进了建议名单
        import curate
        why = "机制拆解扎实，论断清晰可反驳，补位学术视角，但密度略逊于顶级。"
        self.assertTrue([w for w in curate.HEDGE if w in why],
                        "对冲词表漏了「略逊」这类写法")


class DryRunMustNotLoseItsAdvice(unittest.TestCase):
    """建议只打在屏幕上等于没有：一次 tail 就丢了，再想看只能重跑，而重跑要花钱。"""

    def test_dry_run_persists_and_lists(self):
        src = (ROOT / "pipeline" / "curate.py").read_text()
        self.assertIn("curate-dry-run.json", src)
        self.assertIn("建议收录", src)


class NewSourcesStartOnProbation(unittest.TestCase):
    """新源的分是照着节目自己写的分集说明（宣传文案）打的，一篇成稿都没过闸门。
    模型却给了 tier 1，等于让它和跑了半年的源同权抢配额。"""

    def test_curate_ignores_the_model_tier_for_new_sources(self):
        import curate
        self.assertEqual(curate.PROBATION_TIER, 3)
        src = (ROOT / "pipeline" / "curate.py").read_text()
        i = src.index("def discover(")
        self.assertIn('"tier": PROBATION_TIER', src[i:])
        self.assertNotIn('"tier": v["tier"]', src[i:], "又用回了模型自报的 tier")


class PageTierKnowsSiblingTranscriptPaths(unittest.TestCase):
    """Darknet Diaries 每集都有官方逐字稿，但在 /transcript/N/ 而不是 /episode/N/，
    shownotes 里也不给链接。少这条规则，整档节目会白白走音频转写。"""

    def test_darknet_episode_url_yields_the_transcript_url(self):
        sys.path.insert(0, str(ROOT / "pipeline"))
        from lib import transcript as T
        alt = T._alt_urls("https://darknetdiaries.com/episode/178/")
        self.assertIn("https://darknetdiaries.com/transcript/178/", alt)

    def test_alt_urls_leaves_other_hosts_alone(self):
        from lib import transcript as T
        self.assertEqual(T._alt_urls("https://example.com/episode/178/"), [])

    def test_alt_url_is_tried_before_the_plain_link(self):
        src = (ROOT / "pipeline" / "lib" / "transcript.py").read_text()
        i = src.index("def from_page")
        self.assertIn('_alt_urls(ep["link"]) + [ep["link"]]', src[i:i + 900])


class ProbationIsVisibleToReaders(unittest.TestCase):
    """更新日志把「照分集说明打的 10.0 分」当结论公示，而那三档一篇都没跑出来。
    分数来自节目自己写的宣传文案，不标出来就是对读者的误导。"""

    def test_curate_marks_new_entries_as_probation(self):
        src = (ROOT / "pipeline" / "curate.py").read_text()
        self.assertIn('"probation": True', src)

    def test_log_page_renders_the_flag_inside_one_grid_cell(self):
        # .ev 是四列网格，多一个子元素会另起一行
        src = (ROOT / "pipeline" / "build.py").read_text()
        i = src.index("def log_page")
        seg = src[i:i + 3000]
        self.assertIn('r.get("probation")', seg)
        self.assertIn('分{flag}</span>', seg)

    def test_log_page_explains_what_probation_means(self):
        src = (ROOT / "pipeline" / "build.py").read_text()
        i = src.index("def log_page")
        self.assertIn("还没有任何一篇成稿走完四道闸门", src[i:i + 5000])

    def test_css_selectors_match_the_markup(self):
        # `.ev.add .what` 是上次把 .why 改成 .ev-why 时留下的死规则
        css = (ROOT / "assets" / "site.css").read_text()
        self.assertIn(".ev.add .ev-what", css)
        self.assertNotIn(".ev.add .what{", css)
        self.assertIn(".ev-flag{", css)


class SharingWorksWithoutAPlatformSDK(unittest.TestCase):
    """微信和朋友圈不给网页调起分享（要认证公众号 + JS 接口安全域名 + 服务端签名）。
    所以走复制粘贴这条路——它在任何地方都成立。这些检查盯的是几个真实的坑。"""

    def test_share_text_is_short_enough_for_a_moment_post(self):
        # 朋友圈超长会折叠，群里刷屏没人读
        sys.path.insert(0, str(ROOT / "pipeline"))
        import build
        for f in sorted((ROOT / "data" / "episodes").glob("*.json"))[:12]:
            ep = json.loads(f.read_text())
            t = build.episode_share_text(ep)
            self.assertLessEqual(len(t), 560, f"{ep['id']} 的分享文本 {len(t)} 字，太长")
            self.assertIn(build.ep_url(ep), t, "分享文本里没有链接")

    def test_share_link_is_pure_ascii(self):
        # 正文 slug 是中文，percent-encode 之后两百多字符——链接比内容还长
        import build
        for f in sorted((ROOT / "data" / "episodes").glob("*.json"))[:12]:
            ep = json.loads(f.read_text())
            url = build.ep_url(ep)
            self.assertTrue(url.isascii(), f"{url} 不是纯 ASCII")
            self.assertNotIn("%", url, "分享链接不该带 percent 转义")
            self.assertLess(len(url), 90, f"{url} 太长")

    def test_alias_page_points_back_and_stays_out_of_the_index(self):
        # 短链页不能和正文抢排名
        import build
        ep = json.loads(sorted((ROOT / "data" / "episodes").glob("*.json"))[0].read_text())
        h = build.alias_page(ep)
        self.assertIn('name="robots" content="noindex,follow"', h)
        self.assertIn('rel="canonical"', h)
        self.assertIn("location.replace", h)
        self.assertIn('http-equiv="refresh"', h)   # JS 关掉也要能跳

    def test_every_episode_has_an_alias_on_disk(self):
        n = len(list((ROOT / "data" / "episodes").glob("*.json")))
        built = len([d for d in (ROOT / "e").iterdir() if d.is_dir()]) \
            if (ROOT / "e").exists() else 0
        self.assertEqual(built, n, "短链页数量和篇数对不上")

    def test_newlines_survive_the_data_attribute(self):
        # 分享文本放在 data 属性里，换行必须写成 &#10;，否则粘出来是一整行
        import build
        ep = json.loads(sorted((ROOT / "data" / "episodes").glob("*.json"))[0].read_text())
        btn = build.share_button(build.episode_share_text(ep),
                                 url=build.ep_url(ep), title="x")
        self.assertIn("&#10;", btn)
        self.assertNotIn("\n", btn.split('data-share-text="')[1].split('"')[0])

    def test_wechat_branch_is_decided_at_click_time(self):
        # 载入时读一次 UA 的话，这条分支在开发时没法真验，只能靠肉眼读代码
        js = (ROOT / "assets" / "site.js").read_text()
        self.assertIn("function inWeChat()", js)
        self.assertIn("var wx = inWeChat();", js)
        # 微信里不许走系统面板：它宣称支持但拉起来常是空的
        self.assertIn("if (!wx && navigator.share)", js)
        # 用户取消分享面板不是失败，不该弹提示
        self.assertIn("AbortError", js)

    def test_copy_has_a_fallback_for_browsers_without_the_clipboard_api(self):
        js = (ROOT / "assets" / "site.js").read_text()
        self.assertIn("execCommand", js)
        self.assertIn("setSelectionRange", js)   # iOS 需要显式选区


class NoSymlinkPointsBackIntoTheRepo(unittest.TestCase):
    """事故：为了本地预览加了 .preview/podcast -> ..，一个指向仓库自身的符号链接。
    GitHub Pages 打包时顺着它无限递归，Upload artifact 连着两轮卡死 20 分钟，
    而站上什么都没变——看起来像"还没发布"，实际是打包永远走不完。"""

    def test_no_tracked_symlink_resolves_inside_the_repo(self):
        import subprocess
        out = subprocess.run(["git", "ls-files", "-s"], cwd=ROOT,
                             capture_output=True, text=True).stdout
        bad = []
        for line in out.splitlines():
            # git 里符号链接的 mode 是 120000
            if line.startswith("120000"):
                path = line.split("\t", 1)[1]
                target = (ROOT / path).parent / (ROOT / path).readlink()
                try:
                    target.resolve().relative_to(ROOT.resolve())
                    bad.append(f"{path} -> {target}")
                except ValueError:
                    pass          # 指向仓库外面，不会递归
        self.assertEqual(bad, [], f"入库的符号链接指回仓库内部，会让打包递归：{bad}")

    def test_preview_dir_is_ignored(self):
        gi = (ROOT / ".gitignore").read_text()
        self.assertIn(".preview/", gi)


class ScheduledJobsMustBeProvenNotAssumed(unittest.TestCase):
    """事故：plist 装好了、launchctl list 里也在，我就当它在跑。实际 runs = 0——
    仓库在 ~/Desktop（macOS TCC 保护目录），LaunchAgent 读它得到
    Operation not permitted，每天到点静默失败，而站上不更新没人会注意。"""

    TCC = ("/Desktop/", "/Documents/", "/Downloads/")

    def test_agent_working_copy_is_outside_tcc_protected_dirs(self):
        plist = (ROOT / "scripts" / "com.ourword.podcast.plist").read_text()
        for d in self.TCC:
            self.assertNotIn(d, plist,
                             f"plist 指向了 TCC 保护目录 {d}，LaunchAgent 读不了")

    def test_plist_is_a_template_not_someones_home_path(self):
        plist = (ROOT / "scripts" / "com.ourword.podcast.plist").read_text()
        self.assertIn("__DIR__", plist)
        self.assertNotIn("/Users/", plist, "模板里不该写死某台机器的家目录")

    def test_install_script_tells_you_to_run_it_once(self):
        # 从没跑过的定时任务等于没有
        sh = (ROOT / "scripts" / "install-agent.sh").read_text()
        self.assertIn("kickstart", sh)
        self.assertIn("launchd.err", sh, "没告诉人去哪看环境类失败")
        self.assertIn("TCC", sh, "没写清为什么工作副本不能放桌面")


class ReasoningBudgetIsSpentOnlyWhereItMatters(unittest.TestCase):
    """长集的 map 遍是纯抽取（从一段逐字稿里挑要点、原话、数字），没有判断可言。
    让推理模型干这个，一集能烧掉十几次 32000 token 的思考预算，产出却和便宜模型
    没区别。而推理预算被思考吃光、正文返空，是真实发生过的。"""

    def test_map_pass_does_not_use_the_digest_model(self):
        sys.path.insert(0, str(ROOT / "pipeline"))
        import importlib, os
        from lib import llm
        old = dict(os.environ)
        try:
            os.environ["LLM_MODEL"] = "deepseek-reasoner"
            os.environ["LLM_MODEL_TRIAGE"] = "deepseek-chat"
            os.environ.pop("LLM_MODEL_MAP", None)
            importlib.reload(llm)
            self.assertEqual(llm.model_name("map"), "deepseek-chat",
                             "map 没单独配置时该借用便宜模型，而不是掉到推理模型")
            self.assertEqual(llm.model_name("digest"), "deepseek-reasoner")
            # 显式配置优先
            os.environ["LLM_MODEL_MAP"] = "some-other"
            importlib.reload(llm)
            self.assertEqual(llm.model_name("map"), "some-other")
        finally:
            os.environ.clear(); os.environ.update(old)
            importlib.reload(llm)

    def test_map_call_site_passes_the_role(self):
        # 光加角色没用，调用处不传就还是走 digest 模型
        src = (ROOT / "pipeline" / "lib" / "digest.py").read_text()
        i = src.index("def _map_reduce")
        self.assertIn('role="map"', src[i:src.index("def _compose")])

    def test_budget_blowout_falls_back_instead_of_retrying_the_reasoner(self):
        # 预算不够是结构性的，不是抖动——同一个模型重试只会重复烧钱
        src = (ROOT / "pipeline" / "lib" / "digest.py").read_text()
        i = src.index("def _compose")
        body = src[i:src.index("def build")]
        self.assertIn("_BUDGET_BLOWN", body)
        self.assertIn('role="map"', body)
        # 两个模型相同就别兜圈子
        self.assertIn('cheap == llm.model_name("digest")', body)

    def test_both_compose_paths_go_through_the_fallback(self):
        src = (ROOT / "pipeline" / "lib" / "digest.py").read_text()
        # 单遍和 map-reduce 的合并遍都得走 _compose，漏一个就还有一条烧钱的路
        self.assertGreaterEqual(src.count("_compose("), 3)


class BotPushMustNotRevertSourceCode(unittest.TestCase):
    """事故：bot 的 "digest + build (local)" 提交删掉了 install-agent.sh、
    POSTMORTEM 的一整节、26 行守护检查，还把 plist 指回了受 TCC 保护的桌面路径。

    根因是推送重试里的 `git reset --soft origin/main`：--soft 只移动 HEAD，
    索引仍是旧基线的整棵树，于是下一个提交把这期间别人推上来的源码全部回退。
    这是 POSTMORTEM 七"git add -A 把源码扫进 bot 提交"的第二次，形状不同、
    后果一样：bot 有权限静默改源码。"""

    FILES = ("scripts/local-daily.sh", ".github/workflows/daily.yml",
             ".github/workflows/backfill.yml", ".github/workflows/curate.yml")

    def test_no_soft_reset_in_any_push_path(self):
        for f in self.FILES:
            src = (ROOT / f).read_text()
            self.assertNotIn("reset --soft", src, f"{f} 还在用 --soft")
            self.assertNotIn("reset -q --soft", src, f"{f} 还在用 --soft")

    def test_data_retry_restores_files_deleted_from_the_worktree(self):
        # 远端有而本机磁盘没有的数据文件，在 git 眼里是"被删除"；
        # 直接 git add data 会把别人刚发的内容删掉。
        for f in ("scripts/local-daily.sh", ".github/workflows/daily.yml",
                  ".github/workflows/backfill.yml"):
            src = (ROOT / f).read_text()
            self.assertIn("--diff-filter=D", src, f"{f} 没取回被删的数据文件")
            self.assertIn("--reconcile", src, f"{f} 没从磁盘重建 state.json")

    def test_reconcile_flag_exists_and_needs_no_llm(self):
        src = (ROOT / "pipeline" / "run.py").read_text()
        self.assertIn('"--reconcile"', src)
        # 必须在需要 LLM 后端的检查之前就返回，否则重试路径会因为没配 key 而失败
        i = src.index("if a.reconcile:")
        j = src.index("_ceiling[\"words\"] = a.max_words")
        self.assertLess(i, j, "--reconcile 的早退必须在其余初始化之前")


class TokenSpendMustBeMeasurable(unittest.TestCase):
    """"推理预算省着用"原来是一句没法核对的话——一处用量都没记。
    降没降、降在哪一步，必须能从跑批日志里直接看出来。"""

    def test_usage_is_recorded_per_role_and_model(self):
        sys.path.insert(0, str(ROOT / "pipeline"))
        from lib import llm
        llm._USE.clear()
        llm.note_usage("map", "cheap", {"prompt_tokens": 100, "completion_tokens": 20})
        llm.note_usage("map", "cheap", {"prompt_tokens": 100, "completion_tokens": 20})
        llm.note_usage("digest", "reasoner",
                       {"prompt_tokens": 50, "completion_tokens": 80,
                        "completion_tokens_details": {"reasoning_tokens": 60}})
        rep = "\n".join(llm.usage_report())
        llm._USE.clear()
        self.assertIn("map", rep)
        self.assertIn("digest", rep)
        self.assertIn("2 次", rep)
        self.assertIn("思考 60", rep, "思考 token 必须单列——那正是要省的东西")

    def test_missing_usage_block_does_not_crash(self):
        # 有些端点不返 usage，记账不能因此炸掉整轮
        from lib import llm
        llm._USE.clear()
        llm.note_usage("map", "cheap", None)
        llm.note_usage("map", "cheap", {})
        self.assertEqual(llm.usage_report(), [])

    def test_both_backends_record_usage(self):
        src = (ROOT / "pipeline" / "lib" / "llm.py").read_text()
        i = src.index("def call(")
        body = src[i:src.index("def _openai")]
        self.assertIn("note_usage", body, "anthropic 路径没记账")
        j = src.index("def _openai")
        self.assertIn("note_usage", src[j:j + 2600], "openai 路径没记账")

    def test_run_prints_the_report(self):
        src = (ROOT / "pipeline" / "run.py").read_text()
        self.assertIn("usage_report()", src)


class FailuresMustAnnounceThemselves(unittest.TestCase):
    """过去每一次故障都是被人问起才发现的：Pages 卡死 20 分钟、本机定时任务
    runs = 0、日更整轮失败、bot 回退源码。共同点不是难修，是**没有信号**。"""

    def test_healthcheck_covers_the_failures_that_actually_happened(self):
        src = (ROOT / "pipeline" / "healthcheck.py").read_text()
        for fn in ("check_heartbeats", "check_content_freshness",
                   "check_build_consistency", "check_online"):
            self.assertIn(f"def {fn}", src, f"体检少了 {fn}")

    def test_healthcheck_exits_nonzero_and_emits_error_annotations(self):
        src = (ROOT / "pipeline" / "healthcheck.py").read_text()
        self.assertIn("::error::", src, "坏了必须在 CI 里高亮")
        self.assertIn("return 1", src, "坏了必须非零退出")

    def test_online_check_compares_live_site_to_the_repo(self):
        # 「推上去了但没部署」只有对比线上和仓库才看得出来
        src = (ROOT / "pipeline" / "healthcheck.py").read_text()
        i = src.index("def check_online")
        body = src[i:src.index("def main")]
        self.assertIn("篇深读", body)
        self.assertIn("n_data", body)

    def test_both_lines_write_a_heartbeat(self):
        sh = (ROOT / "scripts" / "local-daily.sh").read_text()
        yml = (ROOT / ".github" / "workflows" / "daily.yml").read_text()
        # 文件名现在由 heartbeat.py 拼，两边只传线名
        self.assertIn("heartbeat.py local", sh)
        self.assertIn("heartbeat.py cloud", yml)
        # 空轮也要写心跳，否则"跑了但闸门全拦下"和"根本没跑"分不开
        self.assertIn("只推心跳", sh)
        # 云端跑批失败也要写，否则一失败就看起来像整条线死了
        self.assertIn("if: always()", yml)

    def test_watchdog_is_scheduled_and_opens_an_issue(self):
        yml = (ROOT / ".github" / "workflows" / "watch.yml").read_text()
        self.assertIn("cron:", yml)
        self.assertIn("issues: write", yml)
        self.assertIn("gh issue create", yml)
        # 复用同一个 issue，否则告警自己变成噪音
        self.assertIn("gh issue comment", yml)
        # 自愈之后要自己关掉
        self.assertIn("gh issue close", yml)
        # cron 被 GitHub 静默停用是真实风险，必须查
        self.assertIn("--jq .state", yml)
        # 没试过的告警等于没有告警：必须有办法真触发一次
        self.assertIn("selftest", yml)


class StateJsonHasAMergeDriver(unittest.TestCase):
    """事故：两条线同时跑，data/state.json 三方合并留下冲突标记，之后每次
    git pull --rebase 都报 unmerged files——8 次重试全撞在同一面墙上，整轮产出全废。"""

    def test_driver_registered_in_gitattributes(self):
        ga = (ROOT / ".gitattributes").read_text()
        self.assertIn("data/state.json merge=podcast-state", ga)

    def test_driver_configured_in_every_environment(self):
        # git config 是仓库本地设置，clone 不会带过来，每处都得配
        for f in (".github/workflows/daily.yml", ".github/workflows/backfill.yml",
                  ".github/workflows/curate.yml", "scripts/local-daily.sh",
                  "scripts/install-agent.sh"):
            self.assertIn("podcast-state", (ROOT / f).read_text(),
                          f"{f} 没配 state.json 的合并驱动")

    def test_merge_is_a_union_with_max_counters(self):
        sys.path.insert(0, str(ROOT / "pipeline"))
        from mergestate import merge
        m = merge({"done": {"a": {}}, "fail": {"x": {"n": 2, "soft": 1}},
                   "fp": {"f1": "a"}},
                  {"done": {"b": {}}, "fail": {"x": {"n": 1, "soft": 5}},
                   "fp": {"f2": "b"}})
        self.assertEqual(set(m["done"]), {"a", "b"})
        self.assertEqual(set(m["fp"]), {"f1", "f2"})
        # 计数取大：重试预算宁可少给不可多给
        self.assertEqual(m["fail"]["x"], {"n": 2, "soft": 5})

    def test_merge_survives_garbage_and_keeps_unknown_tables(self):
        from mergestate import merge, _load
        self.assertEqual(_load("/nonexistent/nope.json"), {})
        m = merge({"future_table": {"k": 1}}, {})
        self.assertEqual(m["future_table"], {"k": 1}, "以后加的表不该被静默抹掉")

    def test_retry_loops_can_escape_a_stuck_rebase(self):
        # 驱动只防能自动合的；真留下未合并文件时必须先脱身
        for f in (".github/workflows/daily.yml", "scripts/local-daily.sh"):
            src = (ROOT / f).read_text()
            self.assertIn("git ls-files -u", src, f"{f} 不会检测未合并状态")
            self.assertIn("rebase --abort", src, f"{f} 卡住之后没法脱身")


class LocalLineCommitsEverythingItBuilds(unittest.TestCase):
    """事故：本机脚本的 SITE_FILES 清单漏了 e（分享短链）和 log（更新日志），
    于是本机跑批**永远不提交这两样**——日志里躺着一堆未跟踪的 e/ 目录。
    云端用 git add -A 所以完全看不出来，只有本机线在悄悄少推东西。"""

    def test_site_files_covers_every_built_directory(self):
        sh = (ROOT / "scripts" / "local-daily.sh").read_text()
        i = sh.index("SITE_FILES=")
        decl = sh[i:i + 400]
        # build.py 会写这些目录，清单里必须都有
        for d in ("index.html", "sources", "s", "p", "e", "log",
                  "feed.xml", "sitemap.xml", "search.json",
                  "llms.txt", "llms-full.txt"):
            self.assertIn(d, decl, f"SITE_FILES 漏了 {d}")

    def test_heartbeat_is_a_script_not_a_heredoc(self):
        # heredoc 版嵌在被管道接走的花括号块里，单独跑正常、真跑批一声不响没写出来
        sh = (ROOT / "scripts" / "local-daily.sh").read_text()
        yml = (ROOT / ".github" / "workflows" / "daily.yml").read_text()
        self.assertIn("pipeline/heartbeat.py local", sh)
        self.assertIn("pipeline/heartbeat.py cloud", yml)
        self.assertNotIn("<<'HB'", sh, "又用回了 heredoc")
        self.assertNotIn("<<'HB'", yml, "又用回了 heredoc")

    def test_heartbeat_script_writes_and_says_so(self):
        sys.path.insert(0, str(ROOT / "pipeline"))
        import heartbeat
        src = (ROOT / "pipeline" / "heartbeat.py").read_text()
        self.assertIn("print(", src, "心跳必须自己出声——它静默失效过一次")
        self.assertTrue(hasattr(heartbeat, "write"))


class ChecksMustRunWithoutBeingRemembered(unittest.TestCase):
    """最根本的一条：这个仓库有 140 多项守护检查，但在 ci.yml 之前它们**从来没在
    CI 里跑过**——只在我记得跑的时候跑。于是接连出了几次"改完直接推、推完才发现"
    的事故。靠自觉记得跑检查不是流程，是运气。"""

    def test_ci_runs_the_test_suite_on_every_push(self):
        ci = ROOT / ".github" / "workflows" / "ci.yml"
        self.assertTrue(ci.exists(), "没有 CI——检查等于没有")
        src = ci.read_text()
        self.assertIn("unittest discover", src)
        self.assertIn("push:", src, "只在 PR 上跑不够：bot 直接推 main")

    def test_ci_covers_the_things_tests_cannot_see(self):
        src = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        # 递归符号链接炸的是部署，测试全绿也看不出来
        self.assertIn("120000", src, "没检查入库的符号链接")
        # 构建不幂等会让每轮跑批在生成产物上冲突
        self.assertIn("幂等", src)
        # --no-build 之后没重建，站上会缺页
        self.assertIn("git diff --quiet", src)

    def test_preflight_exists_and_mirrors_ci(self):
        pf = ROOT / "scripts" / "preflight.sh"
        self.assertTrue(pf.exists(), "本地没有一条命令能跑完全部检查")
        src = pf.read_text()
        for need in ("unittest discover", "bash -n", "120000",
                     "healthcheck.py", "build.py"):
            self.assertIn(need, src, f"preflight 少了 {need}")
        self.assertTrue(os.access(pf, os.X_OK), "preflight.sh 没有可执行位")

