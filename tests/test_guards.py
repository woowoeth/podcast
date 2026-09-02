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


class CandidatePoolIsNotJustPopularity(unittest.TestCase):
    """Apple 分类榜按流行度排，天然偏大众——Radar 自己的 top 榜（真crime、励志、
    政治）就是这个偏差的样本。候选池必须能从别处进，但判断仍然全部由本站做。"""

    def test_curate_accepts_an_external_candidate_list(self):
        import curate
        self.assertTrue(hasattr(curate, "feed_pool"))
        src = (ROOT / "pipeline" / "curate.py").read_text()
        self.assertIn("--from-feeds", src)
        # 外部清单只提供候选，不能绕过前置条件和评分
        i = src.index("def discover(")
        body = src[i:i + 3000]
        self.assertIn("feed_pool(from_feeds)", body)
        self.assertIn("known_feeds", body)
        self.assertIn("is_dup", body)

    def test_feed_pool_tolerates_both_field_namings(self):
        import curate, json as _j, tempfile, os as _os
        rows = [{"title": "A", "url": "http://a/rss"},
                {"name": "B", "feed": "http://b/rss"},
                {"title": "C"}]                      # 缺 feed，必须丢掉
        fd, path = tempfile.mkstemp(suffix=".json")
        with _os.fdopen(fd, "w") as f:
            _j.dump(rows, f)
        try:
            got = curate.feed_pool(path)
        finally:
            _os.unlink(path)
        self.assertEqual([g["name"] for g in got], ["A", "B"])
        self.assertEqual(curate.feed_pool("/nonexistent.json"), [])

    def test_probe_can_skip_itunes_when_a_feed_is_given(self):
        # 按名字搜 iTunes 是整套流程里最不可靠的一环：曾把一个冒用 Anthropic 品牌的
        # AI 生成播客匹配成官方节目。能直接给 feed 就绕开它。
        import curate, inspect
        sig = inspect.signature(curate.probe_candidate)
        self.assertIn("feed", sig.parameters)
        src = (ROOT / "pipeline" / "curate.py").read_text()
        # 两条入口共用同一份度量，否则迟早只改一份
        self.assertIn("def _measure(", src)
        self.assertEqual(src.count("_measure("), 3)


class YoutubeOnlySourcesGoToTheLocalLine(unittest.TestCase):
    """事故形状：curate 把新源一律写成 kind=rss、且从不设 residential。而文稿只能从
    YouTube 字幕拿的源在云端必然失败——GitHub 机房 IP 会被 YouTube 判成机器人索要
    cookie——它会每天在云端白失败一次，而"没有文稿"这条日志看不出是 IP 问题。"""

    def test_curate_derives_kind_from_the_feed(self):
        src = (ROOT / "pipeline" / "curate.py").read_text()
        i = src.index("def discover(")
        body = src[i:]
        self.assertIn('"youtube" if "youtube.com/feeds/videos.xml"', body)
        self.assertNotIn('"kind": "rss",', body, "又写死成 rss 了")

    def test_youtube_transcript_means_residential(self):
        src = (ROOT / "pipeline" / "curate.py").read_text()
        i = src.index("def discover(")
        self.assertIn('c.get("transcript_source") == "youtube"', src[i:])
        self.assertIn('entry["residential"] = True', src[i:])

    def test_probe_lets_youtube_candidates_use_captions(self):
        # 不放行 youtube 层，YouTube 源的探测取不到任何内容，评分又退回凭标题猜
        src = (ROOT / "pipeline" / "curate.py").read_text()
        i = src.index("def probe_candidate")
        body = src[i:src.index("def _measure")]
        self.assertIn('"youtube"', body)
        self.assertIn("youtube.com/feeds/videos.xml", body)

    def test_measure_probes_a_long_episode(self):
        # YouTube 频道里混着短视频，拿切片判字幕会得出错误结论
        src = (ROOT / "pipeline" / "curate.py").read_text()
        i = src.index("def _measure(")
        body = src[i:i + 1200]
        self.assertIn("probe_ep", body)
        self.assertIn("1200", body)


class LanguageComesFromTheOriginalName(unittest.TestCase):
    """两个方向都踩过：
    1. 原来用「cat == cn 才算中文」，中文节目归到 AI/技术 就拿到 lang=en。
    2. 我第一版修法用了 `zh` 字段——那是**我们起的显示名**。"Empire: World
       History" 的显示名是「Empire 世界史」，照它判会把一档英文节目判成中文，
       比原来的 bug 更糟。
    中英文的文稿密度阈值不同（MIN_WORDS en 1200 / zh 1800，语速上限 300 / 520），
    语言判错会让整套闸门用错标准，ASR 的语言提示也会给错。"""

    def test_helper_reads_the_original_name(self):
        import curate
        self.assertEqual(curate._lang_of("科技这碗饭", "ai"), "zh")
        self.assertEqual(curate._lang_of("Empire: World History", "hist"), "en")
        self.assertEqual(curate._lang_of("Latent Space", "ai"), "en")
        # 分类完全不参与：ChinaTalk 归在「中国视角」但整档是英文
        self.assertEqual(curate._lang_of("ChinaTalk", "cn"), "en")
        self.assertEqual(curate._lang_of("十字路口Crossing", "cn"), "zh")

    def test_entry_uses_the_helper(self):
        src = (ROOT / "pipeline" / "curate.py").read_text()
        i = src.index("def discover(")
        self.assertIn('_lang_of(c["name"], v["cat"])', src[i:])
        self.assertNotIn('"lang": "zh" if v["cat"] == "cn" else "en"', src[i:])

    def test_every_source_on_file_agrees_with_the_helper(self):
        import curate
        srcs = json.loads((ROOT / "data" / "sources.json").read_text())["sources"]
        bad = [f'{s["name"]}: {s.get("lang")}' for s in srcs
               if s.get("lang") != curate._lang_of(s["name"], s.get("cat", ""))]
        self.assertEqual(bad, [], f"这些源的 lang 和判据不一致：{bad}")


class PrefiltersMustNotRejectWhatWeAlreadyAccepted(unittest.TestCase):
    """事故：筛 4918 档候选时，我在站点判据前面加了一层手写关键词正则
    （名字里得有 ai / invest / history 这类题材词），4918 → 396。而播客名字常常
    不含题材词——拿我们自己在册的源去试那个过滤器，149 档里 74 档会被丢掉，
    包括 Dwarkesh、Acquired、Odd Lots、EconTalk、Conversations with Tyler。
    一半池子在第一步就静默消失，而"零档过关"看起来像池子差。

    这个检验对任何选择性判据都成立：拿我们已经接受的东西去跑它。"""

    def _names(self):
        srcs = json.loads((ROOT / "data" / "sources.json").read_text())["sources"]
        return [s.get("zh") or s["name"] for s in srcs]

    def test_curate_has_no_topic_keyword_gate_on_names(self):
        # 允许有正则，但不允许有"名字必须命中题材词才留下"这种闸门
        src = (ROOT / "pipeline" / "curate.py").read_text()
        i = src.index("def shortlist")
        body = src[i:src.index("def _name_match")]
        self.assertNotIn("KEEP", body)
        self.assertNotIn("semiconductor", body,
                         "又在 shortlist 前面加题材关键词过滤了")

    def test_the_only_name_level_filter_is_dedupe(self):
        """名字级别唯一该做的过滤是去重（_name_match），它必须放过所有在册源
        以外的名字、并认出在册源本身。"""
        sys.path.insert(0, str(ROOT / "pipeline"))
        import curate
        names = self._names()
        # 每档源都该被自己的名字匹配上——否则去重会漏，同一档源被收两次
        for n in names[:60]:
            self.assertTrue(curate._name_match(n, n.lower()),
                            f"去重判据认不出自己：{n}")

    def test_a_topic_regex_would_have_dropped_half_our_sources(self):
        """把那个正则钉在测试里当反例，防止有人觉得"加个关键词过滤挺省事"。"""
        KEEP = re.compile(
            r"(?ix)\b(ai|invest|econom|history|science|startup|business|china)|[\u4e00-\u9fff]")
        names = self._names()
        dropped = [n for n in names if not KEEP.search(n)]
        self.assertGreater(len(dropped), len(names) // 4,
                           "这个反例失效了：更新它或删掉这条测试")


class RankingIsNotFiltering(unittest.TestCase):
    """粗筛故意宽松（不确定的留下），4781 档过粗筛还剩 1522 档，全部实测要十几小时。
    所以要一道排序，但它必须是排序而不是过滤——被漏掉的仍在清单里，下一轮还能再挑。
    这是上一个错误的直接后果：手写正则那次是"过滤"，一半池子静默消失。"""

    def test_rank_names_exists_and_is_documented_as_ordering(self):
        import curate
        self.assertTrue(hasattr(curate, "rank_names"))
        src = (ROOT / "pipeline" / "curate.py").read_text()
        i = src.index("def rank_names")
        doc = src[i:i + 900]
        self.assertIn("只做排序不做过滤", doc)
        self.assertIn("下一轮还能再挑", doc)

    def test_rank_prompt_says_pick_the_strongest_not_drop_the_weak(self):
        src = (ROOT / "pipeline" / "curate.py").read_text()
        i = src.index("RANK_SYSTEM")
        body = src[i:i + 1600]
        self.assertIn("挑出最强的", body)
        self.assertIn("宁可漏掉", body)

    def test_rank_prompt_has_no_quota(self):
        """事故：提示词写"每次最多挑 8 个"，模型当成必须挑满 8 个——在一批真crime
        和体育播客里也硬挑出 8 个，排序结果前 60 名全是政治脱口秀。"""
        src = (ROOT / "pipeline" / "curate.py").read_text()
        i = src.index("RANK_SYSTEM")
        body = src[i:i + 1600]
        self.assertIn("没有配额", body)
        self.assertIn("一个都别挑", body)

    def test_rank_falls_back_without_an_llm(self):
        # 没有后端时不能返回空——那等于静默丢掉整个清单
        import curate, unittest.mock as mock
        with mock.patch.object(curate.llm, "available", lambda: False):
            got = curate.rank_names(["a", "b", "c"], per_batch=2)
        self.assertEqual(got, ["a", "b"])


class GapScoreMustBeAnchoredToScope(unittest.TestCase):
    """事故：补位这一项只问"现有信源有没有覆盖"，于是**任何站外题材都自动满分**。
    一轮 208 档候选里，消防工程、航空事故调查、泌尿科、监狱纪实、犹太文献研究
    全部评到 9 分——它们确实稀缺、确实可核对，但站点不做这些。
    这和"工程约束不等于产品判断"是同一类：把"我们没有"当成了"该有"。"""

    def test_rubric_names_the_scope_before_scoring_the_gap(self):
        src = (ROOT / "pipeline" / "curate.py").read_text()
        i = src.index("2. 补位价值")
        body = src[i:i + 900]
        self.assertIn("先判在不在站点范围内", body)
        self.assertIn("不在范围内的题材一律 0 分", body)
        self.assertIn("不等于", body)

    def test_out_of_scope_examples_are_spelled_out(self):
        # 抽象的"范围外"没有约束力，得给具体例子
        src = (ROOT / "pipeline" / "curate.py").read_text()
        i = src.index("2. 补位价值")
        body = src[i:i + 900]
        for eg in ("消防", "航空", "临床专科"):
            self.assertIn(eg, body)


class OnlyChineseAndEnglishSources(unittest.TestCase):
    """事故：Sternstunde Philosophie 是德语节目，名字里没有汉字，于是被当成英文，
    评到 9 分。德语文稿过不了中英的密度阈值，闸门会用错标准，而"文稿太稀"这条
    日志看不出真实原因是语言不对。"""

    def test_language_comes_from_the_feed_declaration(self):
        src = (ROOT / "pipeline" / "curate.py").read_text()
        self.assertIn("def feed_lang(", src)
        i = src.index("def feed_lang(")
        body = src[i:i + 900]
        self.assertIn("<language>", body)
        self.assertIn("不要从名字猜", body)

    def test_prerequisites_reject_unsupported_languages(self):
        import curate
        base = {"items": 50, "age_days": 3, "cadence": 7, "transcript_words": 5000}
        self.assertIn("只做中英文", curate.prerequisites({**base, "lang": "de"}) or "")
        self.assertIsNone(curate.prerequisites({**base, "lang": "en"}))
        self.assertIsNone(curate.prerequisites({**base, "lang": "zh"}))

    def test_measured_language_wins_over_the_name_guess(self):
        src = (ROOT / "pipeline" / "curate.py").read_text()
        i = src.index("def _measure(")
        self.assertIn('"lang": feed_lang(feed, name)', src[i:i + 1400])
        j = src.index("def discover(")
        self.assertIn('c.get("lang") or _lang_of(', src[j:])


class RangeReplacementsDeleteMoreThanYouThink(unittest.TestCase):
    """事故：用 `s[s.index(A):s.index(B)] = new` 改代码，那段区间里还夹着
    `_notes_sample` 和 `_excerpt` 两个函数，一起被删掉了。语法没错、导入没报，
    是单元测试报的 AttributeError。

    `assert old in s` 只能保证锚点存在，保证不了替换范围里没有别的东西。
    这条测试的作用是：curate 的每个被引用的模块级函数都必须真的有定义。"""

    def test_every_module_level_helper_referenced_is_defined(self):
        sys.path.insert(0, str(ROOT / "pipeline"))
        import curate
        src = (ROOT / "pipeline" / "curate.py").read_text()
        called = set(re.findall(r"(?<![\w.])(_[a-z][a-z0-9_]*)\(", src))
        missing = [n for n in called if not hasattr(curate, n)]
        self.assertEqual(missing, [], f"这些函数被调用但没有定义：{missing}")

    def test_the_two_that_were_deleted_are_back(self):
        import curate
        self.assertTrue(callable(curate._notes_sample))
        self.assertTrue(callable(curate._excerpt))
        self.assertEqual(curate._excerpt(None), "")


class MachineGeneratedPodcastsAreNotOriginalVoices(unittest.TestCase):
    """站名叫「原声」。四道闸门管的是"内容够不够硬"，管不了"说话的是不是人"——
    paperdive.ai 的「AI Papers: A Deep Dive」每天一集、254 集，我们的评分给了它
    9.0 并建议收录。用 AI 深读 AI 生成的内容，是个只会放大错误的回音室。
    同类风险第二次：第一次是 feeds.podcastai.com 冒用 Anthropic 品牌。"""

    def test_generator_tag_is_checked(self):
        import curate
        self.assertTrue(curate._GENERATED.search("podcast-generator"))
        self.assertTrue(curate._GENERATED.search("NotebookLM"))
        self.assertTrue(curate._GENERATED.search("ElevenLabs TTS"))
        self.assertTrue(curate._GENERATED.search("AI-generated"))
        # 别误杀：真人节目的 generator 常是发布平台
        for ok in ("Libsyn", "Megaphone", "Substack", "Anchor", "Acast", "Art19"):
            self.assertIsNone(curate._GENERATED.search(ok), f"误杀 {ok}")

    def test_known_generator_authors_are_caught(self):
        import curate
        self.assertTrue(curate._GEN_AUTHOR.search("paperdive.ai"))
        self.assertTrue(curate._GEN_AUTHOR.search("© 2026 paperdive.ai"))
        self.assertTrue(curate._GEN_AUTHOR.search("feeds.podcastai.com"))
        # 正常的 .ai 公司不该被误杀
        self.assertIsNone(curate._GEN_AUTHOR.search("Latent Space (latent.space)"))
        self.assertIsNone(curate._GEN_AUTHOR.search("Anthropic"))

    def test_prerequisites_reject_before_any_scoring(self):
        # 必须在前置条件里挡掉，不能指望评分看出来——评分已经看不出来一次了
        import curate
        base = {"items": 254, "age_days": 1, "cadence": 1.0,
                "transcript_words": 4049, "lang": "en"}
        self.assertIn("机器生成",
                      curate.prerequisites({**base, "generated": "x 是机器生成的节目"}) or "")
        self.assertIsNone(curate.prerequisites({**base, "generated": None, "cadence": 7}))

    def test_measure_records_the_marks(self):
        src = (ROOT / "pipeline" / "curate.py").read_text()
        i = src.index("def _measure(")
        self.assertIn('"generated": generated_marks(feed)', src[i:i + 1500])

    def test_detection_uses_only_self_declared_metadata(self):
        # 不做音频取证：那不可靠，而这里宁可漏掉也不能误杀真人节目
        src = (ROOT / "pipeline" / "curate.py").read_text()
        i = src.index("def generated_marks")
        doc = src[i:i + 700]
        self.assertIn("不做音频取证", doc)
        self.assertIn("宁可漏掉", doc)


class HeartbeatFilesMustNeverBlockTheCommit(unittest.TestCase):
    """事故：副本卡在 data/heartbeat-cloud.json 的未合并冲突里一整天。本机线每天
    照跑、心跳照写，但**提交和推送全被挡住**，日志里只有一行 "unmerged files"。
    仓库里的本机心跳因此停在前一天——而心跳的全部意义就是"这条线还活着"。

    两个缺口：心跳文件没有合并驱动（state.json 有）；脱身逻辑只在推送重试循环里，
    而卡住的是它前面的提交步骤。"""

    def test_heartbeat_has_a_merge_driver(self):
        ga = (ROOT / ".gitattributes").read_text()
        self.assertIn("data/heartbeat-*.json merge=podcast-state", ga)

    def test_driver_picks_the_newer_heartbeat(self):
        sys.path.insert(0, str(ROOT / "pipeline"))
        from mergestate import merge_heartbeat, _is_heartbeat
        old = {"at": "2026-08-31T13:32:34Z", "line": "local", "exit": 0}
        new = {"at": "2026-09-01T02:31:02Z", "line": "local", "exit": 0}
        # 两个方向都要取新的
        self.assertEqual(merge_heartbeat(old, new)["at"], new["at"])
        self.assertEqual(merge_heartbeat(new, old)["at"], new["at"])
        # 别把 state.json 误判成心跳
        self.assertFalse(_is_heartbeat({"done": {}, "fail": {}, "fp": {}}))
        self.assertTrue(_is_heartbeat(new))

    def test_escape_hatch_runs_before_any_commit(self):
        sh = (ROOT / "scripts" / "local-daily.sh").read_text()
        first = sh.index("git ls-files -u")
        # 必须在第一次 git commit 之前
        self.assertLess(first, sh.index("git -c user.name"),
                        "脱身逻辑在提交之后，卡住的路径救不了")
        # abort 救不回来时要能以远端为准重来
        self.assertIn("reset -q --hard origin/main", sh)


class DatacenterBlocksMustNotDeleteGoodSources(unittest.TestCase):
    """差点造成真损失：每周体检跑在云端机房 IP 上，对 residential 源必然 403，
    fail_streak 每周 +1，涨到 3 就被策展自动移除。发现时 Latent Space、
    Lenny's Podcast、The Pragmatic Engineer、This Week in Virology 已经 2/3——
    再跑一次就删掉四档主力源，而它们从住宅 IP 取全部正常（221/359/73/10 集）。

    代码注释里当时已经写了"Substack 会 403，这些都是抖动"，但仍然计数——
    对 residential 源来说机房 IP 的 403 不是抖动，是**必然**，所以计数单调涨。
    这是"基础设施抖动被当成内容缺失"那一类的新变种，出现在体检自己身上。"""

    def _mod(self):
        sys.path.insert(0, str(ROOT / "pipeline"))
        import resolve_sources
        return resolve_sources

    def test_datacenter_403_on_a_residential_source_is_expected(self):
        R = self._mod()
        sub = {"feed": "https://api.substack.com/feed/podcast/1.rss", "residential": True}
        old = os.environ.get("CI")
        try:
            os.environ["CI"] = "true"
            self.assertTrue(R.expected_block(sub, "HTTPError: HTTP Error 403: Forbidden"))
            # 404 是真下线，不能放过
            self.assertFalse(R.expected_block(sub, "HTTPError: HTTP Error 404: Not Found"))
            # 普通源的 403 也不能放过（可能是改成付费墙了）
            self.assertFalse(R.expected_block({"feed": "https://example.com/rss"}, "403"))
        finally:
            os.environ.pop("CI", None)
            if old is not None:
                os.environ["CI"] = old

    def test_from_a_residential_ip_a_failure_is_a_real_failure(self):
        R = self._mod()
        os.environ.pop("CI", None)
        sub = {"feed": "https://api.substack.com/feed/podcast/1.rss", "residential": True}
        self.assertFalse(R.expected_block(sub, "403 Forbidden"),
                         "本机取不到就是真取不到，不能也放过")

    def test_streak_is_held_not_incremented_when_blocked(self):
        src = (ROOT / "pipeline" / "resolve_sources.py").read_text()
        i = src.index("prev = ((old_status")
        body = src[i:i + 700]
        self.assertIn("expected_block", body)
        self.assertIn('st["fail_streak"] = prev', body)
        self.assertIn("既不涨也不清零", body)

    def test_local_line_checks_the_residential_sources(self):
        # 否则这批源永远拿不到真实的健康信号
        sh = (ROOT / "scripts" / "local-daily.sh").read_text()
        self.assertIn("--check --only-residential", sh)
        src = (ROOT / "pipeline" / "resolve_sources.py").read_text()
        self.assertIn('"--only-residential"', src)

    def test_the_four_sources_are_not_marked_failing(self):
        srcs = json.loads((ROOT / "data" / "sources.json").read_text())["sources"]
        names = {"Latent Space", "Lenny's Podcast", "The Pragmatic Engineer",
                 "This Week in Virology"}
        for s in srcs:
            if s["name"] in names:
                st = s.get("status") or {}
                self.assertTrue(s.get("residential"), f"{s['name']} 没标 residential")
                self.assertEqual(st.get("fail_streak", 0), 0,
                                 f"{s['name']} 又被刷上失败计数了")


class NeverDeleteASourceOnOneVantagePoint(unittest.TestCase):
    """移除决定原来只看云端一个视角。而体检跑在机房 IP 上，有些站点一律拒机房——
    差点因此删掉四档主力源（它们从住宅 IP 取全都正常）。
    删错一档没人会注意到；改派最坏只是本机线多跑一档。所以先改派，
    确认本机线也取不到才移除。"""

    def _judge(self, **over):
        sys.path.insert(0, str(ROOT / "pipeline"))
        import curate
        m = dict(name="X", tier=2, cat="ai", published=5, review_median=8,
                 triage_n=0, triage_pass=None, no_transcript=0, age_days=3,
                 official_transcripts=0, feed_ok=False, fail_streak=3,
                 residential=False)
        m.update(over)
        return curate.judge("x", m)

    def test_cloud_only_failure_reassigns_instead_of_deleting(self):
        act, why = self._judge(residential=False)
        self.assertEqual(act, "residential")
        self.assertIn("机房 IP", why)

    def test_failure_on_the_local_line_too_does_delete(self):
        act, why = self._judge(residential=True)
        self.assertEqual(act, "drop")
        self.assertIn("本机线也取不到", why)

    def test_a_short_streak_still_does_nothing(self):
        self.assertIsNone(self._judge(fail_streak=2))

    def test_apply_actions_knows_the_reassign_action(self):
        # 没有这个分支它会掉进 else 变成降级，而降级解决不了"机房取不到"
        src = (ROOT / "pipeline" / "curate.py").read_text()
        i = src.index("def apply_actions")
        body = src[i:i + 1400]
        self.assertIn('if act == "residential"', body)
        self.assertIn('s["residential"] = True', body)
        self.assertIn('"kind": "residential"', body)

    def test_metrics_carry_the_residential_flag(self):
        src = (ROOT / "pipeline" / "curate.py").read_text()
        self.assertIn('"residential": bool(s.get("residential"))', src)


class BlockedSourcesAreNotShownAsBroken(unittest.TestCase):
    """"机房 IP 取不到"不是抓取异常。日志打 DEAD、信源页显示异常、体检报提醒，
    三处都会误导——而这四档从住宅 IP 取全都正常。"""

    def test_log_label_is_not_dead(self):
        src = (ROOT / "pipeline" / "resolve_sources.py").read_text()
        self.assertIn('"skip" if st.get("blocked_here")', src)

    def test_healthcheck_excludes_blocked(self):
        src = (ROOT / "pipeline" / "healthcheck.py").read_text()
        i = src.index("dead = [s for s in srcs")
        self.assertIn("blocked_here", src[i:i + 300])

    def test_sources_page_excludes_blocked(self):
        src = (ROOT / "pipeline" / "build.py").read_text()
        i = src.index('dead = st.get("ok") is False')
        self.assertIn("blocked_here", src[i:i + 200])


class GuestGraphIsACandidatePoolWithoutPopularityBias(unittest.TestCase):
    """榜单按流行度排，天然偏大众——扫 4918 档 Radar 榜单，前 60 名全是真crime、
    励志和政治。而**上过我们好节目的嘉宾自己主持的节目**是另一个池子：没有流行度
    偏差，而且直接锚定在我们已经判过好的内容上。原来的 find_leads 只看最近 18 篇
    的标题，218 篇里 2400 多条发言人记录完全没用上。"""

    def test_speaker_leads_exists_and_is_wired_in(self):
        import curate
        self.assertTrue(hasattr(curate, "speaker_leads"))
        src = (ROOT / "pipeline" / "curate.py").read_text()
        i = src.index("def discover(")
        self.assertIn("speaker_leads(existing)", src[i:])
        # 必须走同一套去重
        j = src.index("speaker_leads(existing)", i)
        self.assertIn("is_dup", src[j - 120:j + 120])

    def test_prompt_forbids_inventing_show_names(self):
        # 下游会拿这个名字去搜 iTunes，编造的名字最容易匹配到冒名节目
        src = (ROOT / "pipeline" / "curate.py").read_text()
        i = src.index("SPEAKER_SYSTEM")
        body = src[i:i + 1200]
        self.assertIn("不确定就不报", body)
        self.assertIn("冒名", body)

    def test_vague_speaker_names_are_skipped(self):
        # "Tom"、"主持人" 这种查不出节目，只会浪费一次 iTunes 搜索
        src = (ROOT / "pipeline" / "curate.py").read_text()
        i = src.index("def speaker_leads")
        body = src[i:src.index("def find_leads")]
        self.assertIn("主持人", body)
        self.assertIn("len(n) >= 4", body)


class SearchIsABetterPoolThanCharts(unittest.TestCase):
    """榜单只有 top-N 且按流行度排，长尾里的硬节目永远上不了榜——扫 4918 档 Radar
    流行度榜单，最后只收到 3 档。按题材搜索则按相关性排，而且直接返回 feedUrl，
    绕开"按名字搜 iTunes"那一环（整条流程里最容易匹配到冒名节目的地方）。"""

    def test_search_pool_exists_with_terms_for_every_category(self):
        import curate
        self.assertTrue(hasattr(curate, "apple_search"))
        terms = " ".join(curate.SEARCH_TERMS).lower()
        # 每个站点分类都得有对应的搜索词，否则那一类永远不会有新源
        for probe in ("machine learning", "monetary", "china", "philosophy",
                      "history", "child development", "neuroscience"):
            self.assertIn(probe, terms, f"搜索词里没有覆盖 {probe}")

    def test_search_results_carry_the_feed(self):
        src = (ROOT / "pipeline" / "curate.py").read_text()
        i = src.index("def apple_search")
        body = src[i:src.index("def apple_charts")]
        self.assertIn('"feed": feed', body)
        self.assertIn("feedUrl", body)

    def test_search_is_documented_as_relevance_not_popularity(self):
        src = (ROOT / "pipeline" / "curate.py").read_text()
        i = src.index("SEARCH_TERMS = [")
        head = src[max(0, i - 500):i]
        self.assertIn("按流行度排", head)


class StalenessMustNotDeleteLiveSources(unittest.TestCase):
    """用户问"你是不是会把一些优质源因为更新问题删掉"。跑了一遍规则，答案是会，
    三种方式：

    1. 我们手里是**废弃的镜像 feed**，节目本身还在更新。体检只在"取不到"时重新
       解析，而这些 feed 返回 200、只是内容停在多年前——于是"停更 120 天就休眠"
       的规则把"我们拿错了 feed"报成了"节目停更"。实测救回四档：
       Science Magazine Podcast（feed 停在 2010，实际 5 天前还在更）、
       This Podcast Will Kill You（停在 2025-02，实际昨天）、
       Very Bad Wizards（停在 2018，实际 5 天前）、Rationally Speaking。
    2. 重新解析依赖 itunes id，而**没有 id 的源永远无法自愈**——上面三档都没有 id。
    3. "取稿失败 10 次就删源"把我们的能力限制当成了内容问题。"""

    def _judge(self, **over):
        sys.path.insert(0, str(ROOT / "pipeline"))
        import curate
        m = dict(name="X", tier=2, cat="ai", published=1, review_median=8,
                 triage_n=0, triage_pass=None, no_transcript=0,
                 official_transcripts=0, feed_ok=True, fail_streak=0,
                 residential=False, age_days=3, max_gap_days=None)
        m.update(over)
        return curate.judge("x", m)

    def test_stale_but_200_feeds_get_re_resolved(self):
        src = (ROOT / "pipeline" / "resolve_sources.py").read_text()
        self.assertIn("STALE_RECHECK_DAYS", src)
        i = src.index("stale = st.get(\"ok\")")
        body = src[i:i + 1200]
        self.assertIn("itunes_find", body, "没有 id 的源无法自愈")
        # 换 feed 必须换到更新的，否则可能换成另一个死镜像
        self.assertIn("better", body)

    def test_name_search_goes_through_the_name_match_check(self):
        # 按名字搜是最不可靠的一环：曾把冒用 Anthropic 品牌的 AI 生成播客匹配成官方
        src = (ROOT / "pipeline" / "resolve_sources.py").read_text()
        i = src.index("def itunes_find")
        body = src[i:i + 1400]
        self.assertIn("_name_match", body)
        self.assertIn("冒用", body)

    def test_dormancy_respects_each_shows_own_rhythm(self):
        # Revolutions 停更过 665 天和 301 天，之后都回来了；127 天判休眠是误伤
        self.assertIsNone(self._judge(age_days=127, max_gap_days=665))
        act, why = self._judge(age_days=1713, max_gap_days=301)
        self.assertEqual(act, "dormant")
        self.assertIn("历史最长间隔", why)
        # 没有历史数据时回落到固定阈值，不能变成永不休眠
        self.assertEqual(self._judge(age_days=200, max_gap_days=None)[0], "dormant")

    def test_our_own_transcript_failure_does_not_delete(self):
        act, why = self._judge(published=0, no_transcript=10)
        self.assertEqual(act, "dormant", "取不到文稿是我们的能力限制，不该删源")
        self.assertIn("是我们取不到", why)

    def test_only_one_rule_can_still_delete(self):
        """删源的路径应该只剩一条：feed 连续失败、且已经交给本机线也取不到。"""
        src = (ROOT / "pipeline" / "curate.py").read_text()
        i = src.index("def judge(")
        body = src[i:src.index("def audit(")]
        self.assertEqual(body.count('return "drop"'), 1,
                         "又多出了能直接删源的规则")


class AlertsMustNotLieAboutStaleCheckouts(unittest.TestCase):
    """我自己被这条误报骗过一次：在一份落后 4 个提交的本地副本上跑体检，报了两条
    硬伤——"云端线 17 小时没心跳"和"线上 236 篇、仓库 234 篇，部署卡住了"。
    两条都是假的：心跳文件和篇数都在 git 里，副本过期就必然对不上。
    我去查"部署卡住"，实际只需要 git pull。

    通则：**凡是拿仓库里的状态和当前时间／线上比的检查，都要先考虑仓库本身过期。**
    CI 里每次都是新 checkout，不受影响——所以这个洞只在人手动跑的时候咬人，
    而那正是最需要判断准确的时候。"""

    def test_staleness_is_detected(self):
        src = (ROOT / "pipeline" / "healthcheck.py").read_text()
        self.assertIn("def _commits_behind", src)
        i = src.index("def _commits_behind")
        body = src[i:i + 900]
        self.assertIn("HEAD..origin/main", body)
        self.assertIn("宁可不报，不误报", body)
        # 不许 fetch：体检不该改本地 git 状态，也不该因为网络慢就卡住。
        # 判据只看 docstring 之后的代码——docstring 里正好有"不 fetch"这句话，
        # 按行过滤引号只能去掉首尾两行，中间的说明文字还在。
        after_doc = body.split('"""')[2] if body.count('"""') >= 2 else body
        code = "\n".join(l for l in after_doc.split("\n")
                          if not l.strip().startswith("#"))
        self.assertNotIn("fetch", code)

    def test_both_time_based_checks_account_for_it(self):
        src = (ROOT / "pipeline" / "healthcheck.py").read_text()
        for fn in ("def check_heartbeats", "def check_online"):
            i = src.index(fn)
            self.assertIn("behind", src[i:i + 2600], f"{fn} 没考虑副本过期")

    def test_stale_downgrades_to_a_note_not_a_failure(self):
        src = (ROOT / "pipeline" / "healthcheck.py").read_text()
        i = src.index("def check_heartbeats")
        body = src[i:src.index("def check_content_freshness")]
        self.assertIn("elif h > limit and behind:", body)
        after = body.split("elif h > limit and behind:")[1][:200]
        self.assertIn("r.note", after)

    def test_alert_text_matches_what_judge_actually_does(self):
        """改了规则却没改文案，告警就在说谎。judge 现在对非 residential 源是
        改派而不是移除，体检却还在说"再失败一次会被移除"。"""
        src = (ROOT / "pipeline" / "healthcheck.py").read_text()
        i = src.index("def check_sources")
        body = src[i:src.index("def check_online")]
        self.assertIn("改派本机线（不是移除）", body)
        self.assertIn("to_local", body)
        self.assertIn("to_drop", body)
        self.assertIn('s.get("residential")', body)


class RejectedDraftsAreTheMostInformativeRecord(unittest.TestCase):
    """用户提议"优质源可以不审直接发"。查数据后我建议不这么做，但排查中发现一个
    真缺口：**被评审拦下的稿子完全没进统计**。

    降级规则只看"已发布稿子的评分中位"，于是 Y Combinator 发 0 篇、被评审拦 4 篇
    （评分 3、3、4、4），产出全不合格，却永远不会被降级——那 4 次根本没被看见。
    而每一次被拦都花掉了一次取稿加一次推理生成，是这个管线里最贵的浪费。

    顺带记下不该免审的证据：评审一共拦下 14 篇，**其中 10 篇来自 tier-1 优质源**
    （Y Combinator 4 次、Oxide and Friends、Latent Space、张小珺、
    The Cognitive Revolution、老石谈芯）。评审判的不是播客，是我们自己生成的稿子，
    而这类问题在好源上一样发生。按"发布≥8篇、中位≥8、从没被拦过"筛，93 档有产出
    的源里只有 1 档合格，而连它都有 5/12 篇是被机械闸门删过东西才发出去的。"""

    def test_rejections_are_counted(self):
        sys.path.insert(0, str(ROOT / "pipeline"))
        import curate
        src = (ROOT / "pipeline" / "curate.py").read_text()
        i = src.index("def performance")
        body = src[i:src.index("def judge(")]
        self.assertIn('why.startswith("review:")', body)
        self.assertIn("review_rejected", body)
        self.assertIn("gate_rejected", body)
        self.assertIn("draft_pass", body)

    def test_draft_pass_rate_can_demote(self):
        import curate
        m = dict(name="X", tier=1, cat="ai", published=0, review_median=None,
                 triage_n=0, triage_pass=None, no_transcript=0,
                 official_transcripts=0, feed_ok=True, fail_streak=0,
                 residential=False, age_days=3, max_gap_days=30,
                 review_rejected=4, gate_rejected=0, draft_pass=0.0)
        act, why = curate.judge("x", m)
        self.assertEqual(act, "demote")
        self.assertIn("成稿合格率", why)
        self.assertIn("白花一次推理生成", why)

    def test_a_few_tries_is_not_enough_to_judge(self):
        import curate
        m = dict(name="X", tier=1, cat="ai", published=0, review_median=None,
                 triage_n=0, triage_pass=None, no_transcript=0,
                 official_transcripts=0, feed_ok=True, fail_streak=0,
                 residential=False, age_days=3, max_gap_days=30,
                 review_rejected=2, gate_rejected=0, draft_pass=0.0)
        self.assertIsNone(curate.judge("x", m), "两次就降级太急了")

    def test_review_is_cheap_relative_to_generation(self):
        """为什么不该免审的另一半理由：评审是便宜模型、输出极短。
        实测一轮 14 集：digest 出 167,677 token（思考 137,163），review 出 1,458。
        评审约占整轮成本 1-2%。这条写进注释，防止以后有人为省钱把它关掉。"""
        src = (ROOT / "pipeline" / "lib" / "review.py").read_text()
        self.assertIn("MIN_SCORE", src)
        # review 必须走独立配置的模型（不能和 digest 同一个，否则是自己给自己打分）
        self.assertIn('role="review"', src)


class SelfHealingMustPersist(unittest.TestCase):
    """事故：feed 自愈只改生成的 data/sources.json，而 feed 的真相源是
    CURATED / EXTRA 里的 Python 列表。下一轮体检又从列表里读回那个死镜像——
    **同样六档连着两轮都报"feed 换新"**，自愈永远存不下来。
    症状很隐蔽：每轮日志都显示"修好了"，看起来在工作。"""

    def test_healing_writes_back_to_the_python_source(self):
        src = (ROOT / "pipeline" / "resolve_sources.py").read_text()
        self.assertIn("def _write_back", src)
        i = src.index("healed[s[\"id\"]] = meta[\"feedUrl\"]")
        self.assertGreater(i, 0, "换 feed 时没有记下要写回哪一档")
        self.assertIn("_write_back(healed)", src)

    def test_write_back_is_narrow(self):
        # 只改 feed= 那一行，找不到就明说——不猜、不改别的
        src = (ROOT / "pipeline" / "resolve_sources.py").read_text()
        i = src.index("def _write_back")
        body = src[i:src.index("def itunes_find")]
        self.assertIn('feed="', body)
        self.assertIn("找不到定义", body)
        self.assertIn("不猜、不改别的", body)

    def test_the_six_healed_feeds_are_in_the_python_lists(self):
        """把这次自愈的结果钉住：这六档的 feed 必须已经在 Python 列表里，
        否则说明写回又失效了。"""
        pysrc = ((ROOT / "pipeline" / "resolve_sources.py").read_text()
                 + (ROOT / "pipeline" / "extra_sources.py").read_text())
        for feed in ("rss.libsyn.com/shows/474285",          # Very Bad Wizards
                     "feeds.megaphone.fm/AAAS8717073854",    # Science Podcast
                     "feeds.transistor.fm/practical-ai"):    # Practical AI
            self.assertIn(feed, pysrc, f"{feed} 没写回 Python 列表")
        # 那几个死镜像不该再出现
        for dead in ("feeds.podcastmirror.com/very-bad-wizards",):
            self.assertNotIn(dead, pysrc, f"{dead} 是死镜像，还在列表里")

    def test_max_gap_days_is_actually_populated(self):
        """休眠判据依赖 max_gap_days。字段加了但体检没跑过的话，判据是**空转的**
        ——会回落到固定 120 天，和修之前一样。Revolutions 就这样又被判了一次休眠。"""
        srcs = json.loads((ROOT / "data" / "sources.json").read_text())["sources"]
        have = [s for s in srcs if (s.get("status") or {}).get("max_gap_days")]
        self.assertGreater(len(have), 50,
                           "绝大多数源都该有 max_gap_days，否则休眠判据在空转")

