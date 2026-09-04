"""把 POSTMORTEM.md 里的教训变成可执行的检查。

这些不测业务逻辑，测的是"我上次是怎么犯错的"。每一条都对应 POSTMORTEM 里一条
真实事故；纯文档防不住重犯，能断言的就断言。
"""
import hashlib
import json
import os
import pathlib
import re
import subprocess
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
        # 判据是**意图**：试用标记必须和分数在同一个 span 里（.ev 是四列网格，
        # 多一个子元素会另起一行）。原来断言的是字面量 '分{flag}</span>'，
        # 而分数格式化在英文化时抽成了 i18n.score()——判据跟着搬，不是把改动退回去。
        self.assertRegex(seg, r'class="ev-score">\{sc\}\{flag\}</span>')

    def test_log_page_explains_what_probation_means(self):
        src = (ROOT / "pipeline" / "build.py").read_text()
        i = src.index("def log_page")
        # 这段文案在英文化时搬进了 i18n.UI（log_page 里现在是 T("LOG_LEDE_2B")）。
        # 判据跟着搬——原意是"页面上必须解释试用标记是什么"，那个意思没变。
        self.assertIn('T("LOG_LEDE_2B")', src[i:i + 5000],
                      "log 页必须解释试用标记")
        import sys as _s
        _s.path.insert(0, str(ROOT / "pipeline"))
        import i18n as _i
        self.assertIn("还没有任何一篇成稿走完四道闸门", _i.ZH.get("LOG_LEDE_2B", ""),
                      "试用标记的中文解释不见了")

    def test_artificial_keys_have_chinese_originals(self):
        """人造键（LOG_LEDE_1 这种长段落）必须在 i18n.ZH 里给出中文原文。
        不给的话，简体模式下 T() 的恒等行为会把**键名**直接印到页面上——
        线上真出过：日志页显示 "LOG_LEDE_1"，首页显示 "BLURB_HEADBLURB_TAIL"。
        是同伴写的一条守护抓到的，不是我自己发现的。"""
        import sys as _s
        _s.path.insert(0, str(ROOT / "pipeline"))
        import i18n as _i
        self.assertEqual(_i.unresolved_zh(), [],
                         "这些人造键会把键名印到简体页面上")

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

    def test_javascript_functions_referenced_are_defined(self):
        """同一个错误在 JS 上又犯了一次：范围替换把 loadDeep() 一起删掉，
        `node --check` 过（语法没错），但 run() 一调用就抛异常，**搜索静默失效**
        ——筛选永远走不到，255 篇全部显示为命中。"""
        js = (ROOT / "assets" / "site.js").read_text()
        defined = set(re.findall(r"function ([A-Za-z0-9_]+)", js))
        for name in ("run", "loadDeep", "loadPage", "loadAll", "maybeLoad",
                     "showCount", "pageUrl", "mountYouTube", "loadYTApi",
                     "seekVideo", "fmt", "copy", "toast", "track", "apply",
                     "current"):
            if name + "(" in js:
                self.assertIn(name, defined, f"site.js 调用了 {name}() 但没有定义")

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


class PageTierMustNotAcceptAListingPage(unittest.TestCase):
    """用户说"这几个优质源不要拦，很容易误判"。我去查了被拦的那 4 篇
    （Y Combinator，评分 3、3、4、4），结论是**评审没误判，它抓到的是上游 bug**：

    page 层把节目平台的分集列表页当成了逐字稿。抓下来 6700 字全是其他集的标题和
    导语——字数和语速检查都过（都是文字），`_is_this_episode` 也过（本集标题确实
    在页面上），于是"导语扩写"被当成深读生成出来。成稿评审的原话：
    "全部要点均来自节目导语，无任何访谈实质内容，所有要点均标注 [0:00]"。

    四次被拦全是这一个 bug。评审是当时唯一挡住它的东西——如果按"优质源免审"发出去，
    站上会多四篇导语扩写。"""

    def test_density_not_count(self):
        """第一版只数个数（≥3 就拒），误杀了 Dwarkesh：Substack 页面底部的
        "推荐文章"区块列着 7 篇，而主体是真逐字稿。"页面提到其他集"在 Substack、
        Libsyn 这类平台上普遍存在，不是列表页的证据。实测密度差一个数量级。"""
        sys.path.insert(0, str(ROOT / "pipeline"))
        from lib import transcript as T
        others = [f"Episode number {i} with a sufficiently long title" for i in range(25)]
        # 列表页：每 1600 字符一个标题
        listing = " ".join(others[:24]) + ("x" * 39000)
        self.assertIsNotNone(T._is_listing(listing, others))
        # 真逐字稿：13 万字符里 7 个（Dwarkesh 的实际比例）
        real = " ".join(others[:7]) + ("x" * 130000)
        self.assertIsNone(T._is_listing(real, others),
                          "误杀了带推荐区块的真逐字稿")

    def test_under_three_hits_is_never_a_listing(self):
        from lib import transcript as T
        others = [f"Some other episode title number {i} here" for i in range(10)]
        body = " ".join(others[:2]) + ("x" * 2000)
        self.assertIsNone(T._is_listing(body, others))

    def test_threshold_is_documented_with_measurements(self):
        src = (ROOT / "pipeline" / "lib" / "transcript.py").read_text()
        i = src.index("_LISTING_CHARS_PER_TITLE")
        body = src[i:i + 2200]
        # 阈值必须写清是怎么定的，否则下一个人只能猜
        self.assertIn("1,646", body)
        self.assertIn("18,584", body)

    def test_sibling_titles_failure_does_not_break_acquisition(self):
        # 取标题失败就少一道检查，不能让整条取稿路径挂掉
        from lib import transcript as T
        self.assertEqual(T._sibling_titles({"title": "x"}, None), [])
        self.assertEqual(T._sibling_titles({"title": "x"}, {"feed": "http://nope.invalid"}), [])


class LatencyIsAFeatureNotAnAccident(unittest.TestCase):
    """用户问"为啥不快速处理、快速发布"。实测常态延迟中位 15.1 小时
    （最近 10 天发布的 93 篇），因为日更只有三班（00:40 / 01:10 / 12:40 UTC）——
    早上发的节目要等到晚班。张小珺和曾鸣那期就是这样。

    加了快车道：每两小时只查 tier1 的 24 档，绝大多数轮次没有新集、几十秒退出。
    """

    def test_tier1_only_flag_exists_and_filters(self):
        src = (ROOT / "pipeline" / "run.py").read_text()
        self.assertIn("--tier1-only", src)
        self.assertIn('s.get("tier", 3) == 1', src)
        self.assertIn('_tier1["on"] = a.tier1_only', src)

    def test_fast_lane_shares_the_write_lock_with_daily(self):
        """两条线同时写 data/ 会撞在 state.json 上——必须共用一把 concurrency 锁。"""
        fast = (ROOT / ".github" / "workflows" / "fast.yml").read_text()
        daily = (ROOT / ".github" / "workflows" / "daily.yml").read_text()
        self.assertIn("group: podcast-write", fast)
        self.assertIn("group: podcast-write", daily)
        self.assertIn("cancel-in-progress: false", fast)

    def test_fast_lane_is_bounded(self):
        # 快车道的意义是"快"，不是"多"。历史内容交给日更和 backfill
        fast = (ROOT / ".github" / "workflows" / "fast.yml").read_text()
        self.assertIn("--tier1-only", fast)
        self.assertIn("--days 2", fast)
        self.assertIn("--per-source 1", fast)
        # 云端取不到的那批（Substack/YouTube）不该在这里白试
        self.assertIn("--skip-residential", fast)

    def test_fast_lane_writes_a_heartbeat(self):
        fast = (ROOT / ".github" / "workflows" / "fast.yml").read_text()
        self.assertIn("heartbeat.py cloud", fast)

    def test_frequency_tradeoff_is_documented(self):
        """daily.yml 里记过"高频自动化会招来账号停用"。加频次必须写清为什么这次
        可以、以及出问题怎么退——否则下一个人只能猜。"""
        fast = (ROOT / ".github" / "workflows" / "fast.yml").read_text()
        self.assertIn("账号停用", fast)
        self.assertIn("4 小时", fast)


class ShareSheetMustSendOneThingNotTwo(unittest.TestCase):
    """事故：分享到微信时多出一个 151 字节的文件（名字是一串哈希）。
    原因是 navigator.share 同时传了 text 和 url——微信把它当成两个条目：
    文本正常发出，URL 另存成临时文件跟着发过去。
    我们的文本末尾本来就带链接，少传一个字段反而干净。"""

    def test_share_passes_text_only(self):
        js = (ROOT / "assets" / "site.js").read_text()
        self.assertIn("navigator.share({ text: text })", js)
        self.assertNotIn("navigator.share({ title: title, text: text, url: url })", js)

    def test_the_text_still_carries_the_link(self):
        # 不传 url 的前提是文本里有链接，否则分享出去就没法点了
        sys.path.insert(0, str(ROOT / "pipeline"))
        import build
        for f in sorted((ROOT / "data" / "episodes").glob("*.json"))[:8]:
            ep = json.loads(f.read_text())
            self.assertIn(build.ep_url(ep), build.episode_share_text(ep))


class ShareCardImageMustBeSmall(unittest.TestCase):
    """事故：分享卡片上只有灰色占位符。张小珺那集的封面是 3000×3000 PNG、3.2 MB，
    微信抓图直接放弃。多数播客 CDN 支持缩略参数，实测 3.2 MB → 39 KB。"""

    def test_known_cdns_get_resized(self):
        import build
        self.assertIn("thumbnail/600x600",
                      build.og_image("https://image.xyzcdn.net/a.png"))
        self.assertIn("w=600",
                      build.og_image("https://megaphone.imgix.net/a.jpg"))

    def test_cdns_that_reject_params_are_left_alone(self):
        """omnycontent 加参数直接 HTTP 400——宁可慢，也不能让图整个挂掉。"""
        import build
        u = "https://www.omnycontent.com/d/playlist/x/image.jpg"
        self.assertEqual(build.og_image(u), u)
        u2 = "https://static.libsyn.com/p/assets/x.jpg"
        self.assertEqual(build.og_image(u2), u2)

    def test_urls_that_already_have_params_are_untouched(self):
        import build
        u = "https://image.xyzcdn.net/a.png?v=2"
        self.assertEqual(build.og_image(u), u)

    def test_dimensions_are_declared(self):
        # 微信和 Twitter 都用 og:image:width/height 决定要不要抓
        src = (ROOT / "pipeline" / "build.py").read_text()
        self.assertIn('og:image:width" content="600"', src)


class TitlesMustBeOneArguableClaim(unittest.TestCase):
    """用户："你的标题很烂，我没办法通过标题吸引我并理解核心内容是什么"。
    看了一批实际标题，好坏的结构差别很清楚：好的是一个可反驳的论断
    （"深科技公司败给组织系统，而不是技术失败"），坏的有三个毛病——
    两件不相干的事用逗号并列（"埃博拉持续感染脑神经元，鱼类黑色素瘤能传染"，
    像论文目录）、术语人名裸奔（"Fawcett 的 Z 城执念"、"盆岭省"）、
    用词有歧义（"AI没有安全巨头"，会被读成 security）。

    原判据只有一行"必须是一个判断或一个反常识结论"，管不住这三样。"""

    def _rules(self):
        sys.path.insert(0, str(ROOT / "pipeline"))
        from lib.digest import SYSTEM
        i = SYSTEM.index("标题怎么写")
        return SYSTEM[i:]

    def test_rubric_forbids_joining_two_things(self):
        r = self._rules()
        self.assertIn("只写一个论断", r)
        self.assertIn("不是这集的目录", r)
        # 反例必须是真出过的那两条，抽象的"别并列"没有约束力
        self.assertIn("埃博拉持续感染脑神经元，鱼类黑色素瘤能传染", r)
        # 第一次修完模型只是把两件事各说得更长，所以那个版本也要当反例钉住
        self.assertIn("把两件事都塞进去", r)

    def test_rubric_forbids_unexplained_names(self):
        r = self._rules()
        self.assertIn("需要背景才懂", r)
        self.assertIn("Fawcett", r)
        self.assertIn("盆岭省", r)

    def test_rubric_forbids_ambiguous_wording(self):
        r = self._rules()
        self.assertIn("歧义", r)
        self.assertIn("security", r)

    def test_rubric_gives_structures_and_a_self_check(self):
        r = self._rules()
        self.assertIn("不是 X，而是 Y", r)
        self.assertIn("有人会反对吗", r)
        self.assertIn("是不是把两件事并列了", r)

    def test_retitle_tool_exists_and_defaults_to_dry_run_safe(self):
        """改判据必须能便宜地验证：重跑一篇成稿要一次推理调用（1.5 万输出 token，
        八成是思考），只重出标题一集几百 token。"""
        src = (ROOT / "pipeline" / "retitle.py").read_text()
        self.assertIn("--dry-run", src)
        self.assertIn('role="review"', src)      # 便宜模型
        self.assertIn("一个字没写", src)


class HomepageMustNotShipEveryCard(unittest.TestCase):
    """用户："全部下拉内容太多了，改下拉成分页加载，网站打开会很慢"。
    实测 257 张卡片全内联：index.html 288 KB（gzip 109 KB），其中 69 KB 是
    每张卡片的内联搜索文本。改成首屏 24 张 + cards.json 按需补齐后
    56 KB（gzip 16 KB）。

    但**搜索和筛选必须覆盖全部**——只筛前 24 张会让用户以为站上没有那篇文章，
    那比慢更糟。所以一开始搜或筛就先补齐，补齐失败也要让用户看得见。"""

    def test_first_page_is_bounded(self):
        sys.path.insert(0, str(ROOT / "pipeline"))
        import build
        self.assertLessEqual(build.FIRST_PAGE, 40)
        html = (ROOT / "index.html").read_text()
        n = html.count('<a class="card')
        self.assertLessEqual(n, build.FIRST_PAGE + 1,
                             f"首页内联了 {n} 张卡片，分页没生效")

    def test_the_rest_is_available_as_json(self):
        pages = sorted(ROOT.glob("cards-*.json"))
        self.assertTrue(pages, "分页卡片文件没生成，剩下的卡片取不到")
        first = json.loads(pages[0].read_text())
        # 存的是整段 HTML：两份渲染逻辑迟早会长歪
        self.assertIn("data-card", first[0])
        self.assertIn("data-hay", first[0])
        n_eps = len(list((ROOT / "data" / "episodes").glob("*.json")))
        import build
        total = sum(len(json.loads(f.read_text())) for f in pages)
        self.assertEqual(total, max(0, n_eps - build.FIRST_PAGE))

    def test_search_and_filter_load_everything_first(self):
        js = (ROOT / "assets" / "site.js").read_text()
        i = js.index("function run()")
        body = js[i:i + 700]
        self.assertIn("pageState !== 'done'", body)
        self.assertIn("loadAll()", body)
        # 注释里要写清为什么：只筛前 24 张比慢更糟
        self.assertIn("以为站上没有", js)

    def test_load_failure_is_visible(self):
        js = (ROOT / "assets" / "site.js").read_text()
        self.assertIn("加载失败，点一下重试", js)

    def test_works_without_javascript(self):
        # 没有 JS 时只有 24 篇，必须告诉用户完整清单在哪
        html = (ROOT / "index.html").read_text()
        self.assertIn("<noscript>", html)
        self.assertIn("sitemap", html)


class QuoteAttributionIsBaselineAligned(unittest.TestCase):
    """用户指出金句署名那行"人名和时间没有底对齐"。原来是 align-items:center，
    而人名用 sans、时间戳用 mono，两种字体字形高度不同，居中之后基线是错开的。"""

    def test_baseline_not_center(self):
        css = (ROOT / "assets" / "site.css").read_text()
        i = css.index(".quote .attrib{")
        self.assertIn("align-items:baseline", css[i:i + 160])


class PaginationMustLoadOnePageAtATime(unittest.TestCase):
    """用户："加载更多样式好丑，另外也没真正实现下拉加载更多功能"。两条都对：
    第一版滚到底一次性把剩下 231 张全塞进来（那不是分页，是"晚一点的全量加载"），
    而且只靠 IntersectionObserver——**隐藏或后台标签页里 IO 不回调、rAF 也不执行**，
    滚动永远触发不了加载。查这个 bug 时的决定性证据：派发 scroll 事件毫无反应，
    点按钮却正常。"""

    def test_cards_are_split_into_pages(self):
        sys.path.insert(0, str(ROOT / "pipeline"))
        import build
        n_eps = len(list((ROOT / "data" / "episodes").glob("*.json")))
        expect = max(0, -(-(n_eps - build.FIRST_PAGE) // build.FIRST_PAGE))
        pages = sorted(ROOT.glob("cards-*.json"))
        self.assertEqual(len(pages), expect, "分页文件数不对")
        # 单文件版必须删掉，否则前端会取到过期数据
        self.assertFalse((ROOT / "cards.json").exists())
        first = json.loads(pages[0].read_text())
        self.assertLessEqual(len(first), build.FIRST_PAGE)

    def test_scroll_listener_is_the_primary_path(self):
        js = (ROOT / "assets" / "site.js").read_text()
        i = js.index("if (sentinel) {")
        body = js[i:i + 1400]
        self.assertIn("addEventListener('scroll'", body)
        # 节流不能用 rAF：隐藏标签页里它不执行。只看代码，注释里正好提到它。
        code = "\n".join(l for l in body.split("\n")
                         if "//" not in l and "*" not in l)
        self.assertNotIn("requestAnimationFrame", code)
        self.assertIn("Date.now()", body)
        self.assertIn("visibilitychange", body)

    def test_a_short_page_keeps_loading(self):
        # 一页装不满一屏时滚动条不动，再也触发不了——必须自己接着装
        js = (ROOT / "assets" / "site.js").read_text()
        self.assertIn("一页装不满一屏时继续装", js)

    def test_search_still_loads_every_page(self):
        js = (ROOT / "assets" / "site.js").read_text()
        i = js.index("function run()")
        self.assertIn("loadAll()", js[i:i + 600])


class InlinePlayerServesTheTimestamps(unittest.TestCase):
    """用户问视频放哪合适。放正文第一屏、占满正文列宽：这个站的前提是"每条判断都能
    跳回原声核对"，播放器是为时间戳服务的。侧栏只有 264px，视频小到没法看；
    放文末的话正文各处的时间戳都要往回滚很远。"""

    def test_player_is_in_the_article_not_the_aside(self):
        src = (ROOT / "pipeline" / "build.py").read_text()
        self.assertIn("def player_block", src)
        i = src.index('<div class="ep-meta">{tags}</div>')
        self.assertIn("player_block(ep)", src[i:i + 200])
        # 侧栏不能再有一个，否则一页两个播放器
        self.assertNotIn('{player}\n', src)

    def test_video_is_a_facade_not_an_iframe(self):
        """YouTube 的嵌入代码有 1 MB 以上的 JS，直接塞进页面会把首页刚压下来的
        体积又吃回去。"""
        src = (ROOT / "pipeline" / "build.py").read_text()
        i = src.index("def player_block")
        body = src[i:src.index("def episode_page")]
        self.assertIn("video-facade", body)
        self.assertNotIn("<iframe", body)
        self.assertIn("i.ytimg.com", body)      # 封面用 YouTube 自己的缩略图

    def test_timestamps_seek_the_video(self):
        """机制换了：以前自己拼 embed 的 src（每跳一次重载整个播放器，要等好
        几秒，还丢掉已缓冲的部分），现在用官方 API 的 seekTo 就地跳。"""
        js = (ROOT / "assets" / "site.js").read_text()
        self.assertIn("youtube-nocookie.com", js)
        self.assertIn("seekTo", js)
        self.assertIn("mountYouTube(t)", js)


class ChinesePunctuationMustBeFullWidth(unittest.TestCase):
    """用户："语录金句里的，都是半角不是全角"、"？问号也是半角"。
    归一化判据原来要求标点**两侧都是汉字**，于是句末和引号前的全漏了。
    改了两版：第二版右边放宽到「汉字／空白／句末／右引号」，还是漏了
    "机架架构,1兆瓦"（右边是数字）；第三版逗号类不限制右边，靠左边的
    lookbehind 保护 "1,200"。"""

    def _f(self):
        sys.path.insert(0, str(ROOT / "pipeline"))
        from lib.digest import _cn_punct
        return _cn_punct

    def test_terminal_and_pre_quote_punctuation(self):
        f = self._f()
        self.assertEqual(f("替代它?"), "替代它？")
        self.assertEqual(f("不一定成功."), "不一定成功。")
        self.assertEqual(f("「反共识的,但不一定成功.」"), "「反共识的，但不一定成功。」")
        self.assertEqual(f("机架架构,1兆瓦"), "机架架构，1兆瓦")

    def test_numbers_and_versions_are_untouched(self):
        f = self._f()
        self.assertEqual(f("营收 1,200 万美元"), "营收 1,200 万美元")
        self.assertEqual(f("用 gpt-4.1 跑"), "用 gpt-4.1 跑")
        self.assertEqual(f("中文.txt 这个文件"), "中文.txt 这个文件")
        self.assertEqual(f("型号 A,B 两种"), "型号 A,B 两种")

    def test_nothing_left_on_the_site(self):
        import re as _re
        bad = 0
        for f in (ROOT / "data" / "episodes").glob("*.json"):
            d = json.loads(f.read_text())["digest"]
            for q in (d.get("quotes") or []):
                if _re.search(r"[\u4e00-\u9fff][,;:!?]", q.get("zh") or ""):
                    bad += 1
        self.assertEqual(bad, 0, f"{bad} 条金句译文里还有半角标点")

    def test_verbatim_quotes_are_never_touched(self):
        """quotes[].raw 是逐字原文，改一个字符就通不过机械闸门的逐字校验。"""
        src = (ROOT / "pipeline" / "repunct.py").read_text()
        self.assertIn("raw 不动", src)
        i = src.index("for q in (g.get(\"quotes\")")
        self.assertNotIn('put(q, "raw")', src[i:i + 200])



class VideoTimeline(unittest.TestCase):
    """视频和音频得是同一条时间轴，否则每个时间戳都跳错。

    线上真实后果（用户问"为啥看不了视频只有音频"时查出来的）：55 篇挂着视频的里
    16 篇挂错了——80,000 Hours 挂了 176 秒的片花（音频 2968 秒）、Lenny's 挂了
    133 秒、Acquired 挂了 1674 秒对 14360 秒的正片。频道 Atom feed 不带时长，
    match_youtube 只能比标题，而片花的标题和正片几乎一样。
    """

    def setUp(self):
        import sys
        sys.path.insert(0, str(ROOT / "pipeline"))
        from lib import transcript as T
        self.T = T

    def test_clip_is_rejected(self):
        ok, why = self.T.video_aligned(
            {"youtube_id": "x", "duration": 2968}, False, known_len=176)
        self.assertFalse(ok)
        self.assertIn("不是同一个剪辑", why)

    def test_same_cut_passes(self):
        ok, _ = self.T.video_aligned(
            {"youtube_id": "x", "duration": 7952}, False, known_len=7952)
        self.assertTrue(ok)

    def test_youtube_tier_needs_no_check(self):
        """文稿就是这个视频的字幕，时间轴天然一致——不该去抓网页验证。"""
        ok, why = self.T.video_aligned({"youtube_id": "x"}, True, known_len=0)
        self.assertTrue(ok)
        self.assertIn("天然一致", why)

    def test_tolerance_stays_imperceptible(self):
        """承诺是"点一下回到它被说出的那一秒"，容差必须小到读者察觉不到。
        实测同剪辑的差 0～28 秒；差 84 秒那条是另一个版本，一分半的偏移已经
        跳到别的话上了。"""
        self.assertEqual(self.T.seek_tolerance(2691), 40)
        self.assertLessEqual(self.T.seek_tolerance(16000), 90)
        ok, _ = self.T.video_aligned(
            {"youtube_id": "x", "duration": 2691}, False, known_len=2775)
        self.assertFalse(ok, "差 84 秒不该放过")

    def test_search_is_the_only_path(self):
        """曾经有两条提前 return 造成静默的全面失效：没填频道 id 就 return
        （122 档信源从来没搜过），以及频道 feed 一个 HTTPError 就 return
        （一次限流等于这一集永远没视频）。全站只有 55/255 有视频，而用户看到的
        现象只是"只有音频"。

        那条"快路径"（feeds/videos.xml?channel_id=）本身已经废了——连确认存在的
        真实频道 id 也返回 404。它不该再回来：Atom feed 不带时长，只能比标题，
        而 dwarkesh 的 yt 指向的是 Dwarkesh Clips 剪辑号，挂上去全是片花。"""
        src = (ROOT / "pipeline" / "lib" / "transcript.py").read_text()
        i = src.index("def match_youtube")
        body = src[i:src.index("def _search_youtube", i)]
        self.assertIn("_search_youtube", body, "match_youtube 必须落到搜索")
        code = body[body.index('"""', body.index('"""') + 3) + 3:]   # 去掉文档串
        self.assertNotIn("feeds/videos.xml", code,
                         "频道 Atom feed 端点已废且不带时长，不该再当快路径")

    def test_search_does_not_hinge_on_show_name(self):
        """sources.json 里"张小珺"曾被拼成"张小珲"，一个错字让这档节目的视频发现
        完全归零，而且静默——搜索返回空和"这集真没视频"长得一模一样。"""
        src = (ROOT / "pipeline" / "lib" / "transcript.py").read_text()
        i = src.index("def _search_youtube")
        body = src[i:i + 1400]
        self.assertIn('for q in (f"{show} {title}", title)', body,
                      "搜索必须有一轮只用集标题，不能把节目名当单点")

    def test_unverifiable_is_not_the_same_as_wrong(self):
        """YouTube 会 429，而"限流"和"挂错了视频"在返回值上长得一样。把"查不出来"
        当成"对不上"，一次限流就会把好视频摘掉——临时故障造成永久损失。"""
        state, why = self.T.video_aligned(
            {"youtube_id": "x", "duration": 3000}, False, known_len=0)
        self.assertIsNone(state, "拿不到时长必须是 None，不能是 False")
        src = (ROOT / "pipeline" / "video.py").read_text()
        i = src.index("def audit")
        self.assertIn("if ok is None:", src[i:src.index("def find", i)],
                      "audit 必须把「查不出来」和「对不上」分开处理")

    def test_no_wrong_video_on_the_site(self):
        """线上不该再有挂错的视频。只查已知的失败形态：视频比音频短一大截 =
        片花。这条不联网，跑得起。"""
        bad = []
        for f in (ROOT / "data" / "episodes").glob("*.json"):
            d = json.loads(f.read_text())
            if not d.get("youtube_id"):
                continue
            q = (d.get("digest") or {}).get("quality") or {}
            if q.get("transcript_source") == "youtube":
                continue          # 文稿即字幕，时间轴天然一致
            if not d.get("video_len") or not d.get("duration"):
                continue
            if abs(d["video_len"] - d["duration"]) > self.T.seek_tolerance(d["duration"]):
                bad.append((d.get("source"), d["duration"], d["video_len"]))
        self.assertEqual(bad, [], f"{len(bad)} 篇挂着对不上的视频")


class PlayerNote(unittest.TestCase):
    def test_no_redundant_timestamp_hint(self):
        """「核心论点 · 点时间戳可跳到原声」那个小标题已经说了同一件事，
        播放器下面再解释一遍是噪音。"""
        src = (ROOT / "pipeline" / "build.py").read_text()
        i = src.index("def player_block")
        body = src[i:src.index("def episode_page", i)]
        marks = [ln for ln in body.split("\n")
                 if 'class="note"' in ln and "点时间戳" in ln]
        self.assertEqual(marks, [], f"播放器下面又出现了时间戳说明：{marks}")


class YouTubeChannelIds(unittest.TestCase):
    """信源清单里不许出现 YouTube 频道 id。

    这个字段的全部历史：43 档信源手填了 `yt="UC…"`，格式对、长度对、从没验证过。
    逐个拉频道页才发现它们**都存在但有几个指向错的频道**——dwarkesh 指的是
    **Dwarkesh Clips** 剪辑号，cogrev 指的是 Upstream with Erik Torenberg。
    挂上去的是 176 秒、1674 秒的片花，页面上每个时间戳都跳错。

    而它当年的用处（频道 Atom feed 快路径）已经彻底没了：那个端点对**任何** id
    都返回 404，包括确认存在的真实频道。删掉快路径之后没有任何代码再读这个字段。

    留一个没人读、又验证不了的外部标识符在配置里，唯一的用处是将来某天悄悄拿它
    抓错东西。要重新引入频道发现，必须连着验证工具一起来。
    """

    def test_no_hand_written_channel_ids(self):
        import re
        for name in ("resolve_sources.py", "extra_sources.py"):
            src = (ROOT / "pipeline" / name).read_text()
            found = re.findall(r'yt="[^"]*"', src)
            self.assertEqual(found, [],
                             f"{name} 里又出现了频道 id：{found}。"
                             f"没有代码读它，而手填的 id 曾指向剪辑号")

    def test_generated_list_has_none_either(self):
        srcs = json.loads((ROOT / "data" / "sources.json").read_text())["sources"]
        with_yt = [s["id"] for s in srcs if s.get("yt")]
        self.assertEqual(with_yt, [], f"sources.json 里还有 yt 字段：{with_yt}")


class Player(unittest.TestCase):
    """正文顶部那张播放器卡。这一类的 bug 全是"线上长得不对"，而本地看不出来，
    所以判据都盯着**已知的失效机制**，不是外观描述。
    """

    @staticmethod
    def _strip(text: str, kind: str) -> str:
        """把注释剥掉再判。不剥的话，"别再用 autoplay=1" 这句解释本身就会
        让"不许出现 autoplay=1"的检查失败——检查在读文档，不是在读代码。"""
        if kind == "py":
            text = re.sub(r'"""[\s\S]*?"""', "", text)
            return re.sub(r"(?m)^\s*#.*$", "", text)
        if kind == "css":
            return re.sub(r"/\*[\s\S]*?\*/", "", text)
        text = re.sub(r"/\*[\s\S]*?\*/", "", text)
        return re.sub(r"(?m)^\s*//.*$", "", text)

    def setUp(self):
        self.build = (ROOT / "pipeline" / "build.py").read_text()
        i = self.build.index("def player_block")
        self.fn = self._strip(
            self.build[i:self.build.index("def episode_page", i)], "py")
        self.css_raw = (ROOT / "assets" / "site.css").read_text()
        self.css = self._strip(self.css_raw, "css")
        self.js_raw = (ROOT / "assets" / "site.js").read_text()
        self.js = self._strip(self.js_raw, "js")

    # ---------------------------------------------------------- 有视频≠没音频
    def test_video_page_keeps_audio_in_the_dom(self):
        """产品决定：有视频时只显示视频（两个播放器摆在一张卡上，读者只会用
        一个，另一个是噪音）。但音频元素必须**留在 DOM 里**、默认 hidden——
        YouTube 放不出来（区域限制、嵌入被关、脚本被拦）时由脚本放出来。

        之前那版是干脆不输出音频，读者就停在一个黑框上，正文里几十个时间戳
        全都没处跳。"看起来干净"不能换来"走进死路"。"""
        sys.path.insert(0, str(ROOT / "pipeline"))
        import importlib
        build = importlib.import_module("build")
        html = build.player_block({"youtube_id": "abc12345678",
                                   "audio": "https://cdn.example/x.mp3",
                                   "duration": 3600})
        self.assertIn("video-facade", html)
        self.assertIn("<audio", html, "音频元素必须留在 DOM 里当兜底")
        self.assertRegex(html, r'data-audio-strip\s+hidden',
                         "有视频时音频条默认收起")

    def test_timestamps_do_not_seek_a_hidden_player(self):
        """音频条收起时时间戳不该去跳它——读者看不见的播放器动了等于什么都
        没发生。"""
        self.assertIn("!strip.hidden", self.js)

    def test_audio_only_still_gets_a_player(self):
        sys.path.insert(0, str(ROOT / "pipeline"))
        import importlib
        build = importlib.import_module("build")
        html = build.player_block({"audio": "https://cdn.example/x.mp3",
                                   "duration": 1800})
        self.assertIn("<audio", html)
        self.assertNotIn("video-facade", html)

    # -------------------------------------------------- 16:9 不能靠新语法
    def test_poster_has_no_height_attribute(self):
        """给 img 写 height="360" 属性等于指定了 height，两边都定死时 CSS 的
        aspect-ratio **不生效**——16:9 的框退回 4:3，露出 YouTube 缩略图自带的
        黑边。线上就是这么丑起来的。"""
        self.assertNotIn('height="', self.fn,
                         "封面图不能带 height 属性，它会把 aspect-ratio 废掉")

    def test_player_ratio_does_not_depend_on_aspect_ratio(self):
        """用户那台浏览器不认新语法：inset 不认就掉回静态位置（播放键跑到图片
        下面去了），aspect-ratio 不认就塌成 0 高。padding-top 百分比没有门槛。"""
        # 用分节标题定位，不用"页内播放器"这四个字——.cover 兜底那段的注释里
        # 也提到了它，按关键词找会定位到上千行之前。
        i = self.css_raw.index("------ 页内播放器 */")
        block = self._strip(
            self.css_raw[i:self.css_raw.index("加载更多", i)], "css")
        self.assertIn("padding-top:56.25%", block, "16:9 必须用 padding-top 撑")
        self.assertNotIn("aspect-ratio", block, "播放器不该再依赖 aspect-ratio")
        self.assertNotIn("inset:", block, "别用 inset 简写，写全四个方向")

    def test_card_covers_have_a_fallback_too(self):
        """首页卡片封面也用 aspect-ratio。同一类浏览器上它会塌成 0 高，
        补上同一个 padding-top 兜底。"""
        self.assertIn("@supports not (aspect-ratio:16/9)", self.css_raw)

    # ------------------------------------------------------- 播放要真能播
    def test_uses_the_official_iframe_api(self):
        """上一版点了假门就新建一个 `?autoplay=1` 的 iframe，而**在新建的
        iframe 上加 autoplay 不算用户手势**（iOS 尤其严），播放器加载了却不会动
        ——用户看到的就是"点播放没生效"。官方 API 的 playVideo() 在点击这条链路
        里调，算手势；seekTo 还能就地跳，不用换 src 重载。"""
        self.assertIn("iframe_api", self.js, "必须用官方 IFrame Player API")
        self.assertIn("playVideo", self.js)
        self.assertIn("seekTo", self.js)
        # autoplay=1 只许出现在兜底那条路上（API 脚本被拦时的普通 embed）。
        # 主路径必须走 API 的 playVideo()——在新建 iframe 上加 autoplay 不算
        # 用户手势，那正是"点播放没生效"的原因。兜底那条退化成"多点一下"，
        # 比跳出站好，所以允许。
        for m in re.finditer(r"autoplay=1", self.js):
            around = self.js[max(0, m.start() - 400):m.start()]
            self.assertIn("function plainSrc", around,
                          "autoplay=1 只许出现在 plainSrc 兜底里，不许在主路径")

    def test_api_script_loads_only_on_click(self):
        """API 脚本约 100 KB。不点视频的读者不该下载它——首屏刚从 109 KB
        压到 17 KB。"""
        i = self.js_raw.index("function loadYTApi")
        self.assertIn("iframe_api", self.js_raw[i:i + 1400])
        i2 = self.js_raw.index("function mountYouTube")
        self.assertIn("loadYTApi", self.js_raw[i2:i2 + 2200],
                      "loadYTApi 只该由 mountYouTube 触发，即点击之后")

    def test_embed_stays_on_the_nocookie_host(self):
        self.assertIn("youtube-nocookie.com", self.js)

    def test_dead_embed_gets_a_way_out(self):
        """放不了（区域限制、嵌入被关、脚本取不到）不能停在一个黑框上，也不能
        变成一张空卡。两种页面两种收场：
          · 有音频的 84 篇 → 拿掉视频框，放出音频条（它自己带一行说明）
          · 没音频的 21 篇 → 拿掉视频框，把封面放回来并改成 YouTube 外链
        没有第三种，所以 videoFailed 必须两条都覆盖。"""
        self.assertIn("function videoFailed", self.js)
        i = self.js.index("function videoFailed")
        body = self.js[i:i + 900]
        self.assertIn("removeChild", body, "失败时必须把视频框拿掉")
        self.assertIn("revealAudio()", body, "有音频就放出音频条")
        self.assertIn("facadeNode.hidden = false", body,
                      "没音频时必须把封面放回来，不然是一张空卡")
        self.assertIn("youtube.com/watch", body, "放回来的封面要能去 YouTube")
        # onError：101/150 是作者关掉了站外嵌入，换普通 iframe 也没用 → 给外链；
        # 其余码换普通 embed 再试。
        k = self.js.index("onError: function")
        blk = self.js[k:k + 700]
        self.assertIn("videoFailed", blk, "101/150 要能落到外链")
        self.assertIn("mountPlain", blk, "其余错误码要先试普通 embed")

        # API 脚本拿不到 → **不许**直接跳出站，要上普通 embed。
        # 内容拦截器常把 youtube.com/iframe_api 当追踪脚本拦掉，而 embed 本身
        # 是好的；我原来这条路直接给外链，用户报"非要让我跳出去看"。
        k2 = self.js.index("s.onerror")
        self.assertNotIn("videoFailed", self.js[k2:k2 + 200],
                         "API 脚本拿不到不等于不能内嵌播，别直接跳出站")

    def test_api_load_has_a_deadline(self):
        """拦截器有两种拦法：返回错误（onerror 触发），和**返回 200 但空 body**
        （onerror 不触发，window.YT 永远不出现，回调永远不跑）。第二种只能靠
        超时兜底——实测这台机器上取 iframe_api 就是 0 字节。"""
        i = self.js.index("function loadYTApi")
        body = self.js[i:i + 1200]
        self.assertIn("setTimeout", body, "loadYTApi 必须有截止时间")
        self.assertRegex(body, r"window\.YT && window\.YT\.Player\)\) no\(\)",
                         "超时时要走失败回调")

    def test_facade_is_hidden_not_replaced(self):
        """假门得藏起来、别替换掉——放不出来的时候要能把它放回来。
        replaceWith 之后节点就没了，没音频的 21 篇就只剩一张空卡。"""
        i = self.js.index("function mountYouTube")
        body = self.js[i:i + 1600]
        self.assertNotIn("facade.replaceWith", body)
        self.assertIn("facade.hidden = true", body)

    def test_no_redundant_failure_sentence(self):
        """用户："这段视频不能内嵌播放，去 YouTube 打开 ↗ 这句话去掉不需要"。
        有音频时音频条自己带说明，那句是重复的；没音频时封面上有 YouTube 角标。"""
        self.assertNotIn("不能内嵌播放", self.js)      # self.js 已剥注释
        self.assertNotIn("加载 YouTube 播放器失败", self.js)
        self.assertNotIn("video-fallback", self.js_raw)
        self.assertNotIn("vfail", (ROOT / "assets" / "site.css").read_text())

    # --------------------------------------------------- 音频控件是渐进增强
    def test_custom_audio_ui_degrades_to_native(self):
        """自己画的控件更好看，但脚本没跑（报错、被拦）时不能变成一个点不动的
        死条。所以 HTML 里 <audio> 带着 controls 出，自定义那层默认 hidden，
        脚本跑起来才对调。"""
        self.assertIn("controls", self.fn, "<audio> 必须带 controls 出")
        self.assertIn("hidden", self.fn, "自定义层必须默认 hidden")
        self.assertIn("removeAttribute('controls')", self.js)
        self.assertIn("ui.hidden = false", self.js)
        self.assertIn('.player .strip audio{', self.css_raw,
                      "原生兜底也得有样式，不然掉回去很难看")

    # ------------------------------------------------------------ 底部间距
    def test_last_card_is_not_flush_against_the_footer(self):
        """桌面端最后一块是 .prevnext，它自带 72px 下边距；单列时 aside 排到
        最后，谁都没给它下边距，于是"这篇是怎么来的"那张卡紧贴页脚的分隔线。"""
        self.assertRegex(
            self.css_raw,
            r"@media \(max-width:940px\)\{ \.ep\{padding-bottom:",
            "单列布局下 .ep 必须有下边距")


class AssetVersioning(unittest.TestCase):
    """CSS / JS 的 URL 必须带内容指纹。

    线上真实后果（用户连着三轮说"跟半成品一样"，我一直在改样式，其实样式早就
    对了）：GitHub Pages 给这些文件的是 max-age=600 而 URL 从不变，于是读者
    浏览器拿**缓存里的旧 CSS/旧 JS 配新 HTML**。新 HTML 有 .frame / .vdur /
    .aui 这些新结构，旧 CSS 里没有对应规则——播放器卡片没描边、时长掉到图片
    外面、播放圈完全看不见、自定义音频控件不显（旧 JS 不会去摘 controls）。
    看起来像交了个半成品，其实是两半不同版本拼在一起。

    指纹变了 URL 就变，浏览器必然重新取。靠调 max-age 只能缩短窗口，消不掉。
    """

    def test_html_references_hashed_assets(self):
        for page in ("index.html",
                     next(iter(sorted((ROOT / "p").iterdir())))/"index.html"):
            html = pathlib.Path(page).read_text()
            for m in re.findall(r'(?:href|src)="[^"]*site\.(?:css|js)[^"]*"', html):
                self.assertRegex(m, r"\?v=[0-9a-f]{6,}",
                                 f"{page} 里的资源引用没带指纹：{m}")

    def test_hash_follows_content(self):
        sys.path.insert(0, str(ROOT / "pipeline"))
        import importlib
        build = importlib.import_module("build")
        url = build.asset("assets/site.css")
        want = hashlib.sha256(
            (ROOT / "assets" / "site.css").read_bytes()).hexdigest()[:10]
        self.assertIn(f"?v={want}", url, "指纹必须来自文件内容，不是构建时间")

    def test_build_is_still_idempotent(self):
        """指纹是内容的函数，所以同样的内容必须给同样的 URL——否则每次构建都
        产生全站 diff，'构建是幂等的'那道闸门会红。"""
        sys.path.insert(0, str(ROOT / "pipeline"))
        import importlib
        build = importlib.import_module("build")
        self.assertEqual(build.asset("assets/site.js"),
                         build.asset("assets/site.js"))


class DegradesWithoutStylesheet(unittest.TestCase):
    """样式表没到位时，脚本不能把能用的控件换成看不见的东西。

    这是上面那次故障的第二半：脚本摘掉原生 audio controls、换上自己画的控件，
    而自己画的那套全靠 CSS。旧 CSS 里没有 .aui 的规则，于是读者手里既没有原生
    控件也没有新控件——页面上唯一能点的就剩下面那条，"点播放器只有声音没有视频"
    就是这么来的。
    """

    def test_script_checks_that_css_applied(self):
        css = (ROOT / "assets" / "site.css").read_text()
        js = (ROOT / "assets" / "site.js").read_text()
        self.assertIn("--css:ok", css, "site.css 必须定义探针 --css:ok")
        self.assertIn("getPropertyValue('--css')", js,
                      "脚本必须先确认样式表生效，再做依赖样式的替换")
        i = js.index("var ui = document.querySelector('[data-audio-ui]')")
        self.assertIn("cssOk", js[i:i + 200],
                      "摘掉原生 controls 之前必须过 cssOk")

    def test_play_button_is_inline_svg(self):
        """播放圈以前是个空 span，靠 CSS 画。CSS 没到位就彻底看不见——读者只看见
        一张静态图，看不出它能点。SVG 自带尺寸和颜色，零 CSS 也在。"""
        build = (ROOT / "pipeline" / "build.py").read_text()
        self.assertIn("PLAY_SVG", build)
        self.assertIn("<svg viewBox=", build)
        page = next(iter(sorted((ROOT / "p").iterdir()))) / "index.html"
        # 找一篇有视频的
        for d in sorted((ROOT / "p").iterdir()):
            h = (d / "index.html").read_text()
            if "video-facade" in h:
                self.assertIn("<svg viewBox=", h.split("video-facade")[1][:600],
                              "播放键里必须有内联 SVG")
                return
        del page


class DigestRubric(unittest.TestCase):
    """dek / 要点 / 金句 都得有判据，不能只有标题有。

    用户："我觉得我们文章内展示的重点和金句还不够核心"。查下来不是展示问题，
    是判据缺失：SYSTEM 里标题有一整套硬约束，dek、要点、金句一条都没有，
    只有机械规则（必须逐字、必须带时间戳、不许推测）。

    后果可量化：全站 2095 条要点小标题里，**只有 16% 带否定/转折/断言标记**，
    另外 84% 是名词短语式的话题名——「折旧与晶圆厂滞留风险」「与长鑫合作意义」
    「第一笔钱投向哪里」「2017年的转折」。读者扫一遍拿不到任何判断，等于把节目
    目录翻译了一遍。
    """

    def setUp(self):
        sys.path.insert(0, str(ROOT / "pipeline"))
        from lib.digest import SYSTEM
        self.sys = SYSTEM

    def test_has_a_rubric_for_each_part(self):
        for sec in ("一句话结论（dek）怎么写", "要点怎么选、怎么写",
                    "金句怎么选", "标题怎么写"):
            self.assertIn(sec, self.sys, f"SYSTEM 里少了「{sec}」这一节")

    def test_points_rubric_encodes_selection_not_just_style(self):
        """判据必须先解决"选什么"。原来的提示说了"找出最值钱的 5-8 处"，
        但从没定义什么叫值钱，于是要点顺着节目章节排。"""
        i = self.sys.index("要点怎么选、怎么写")
        blk = self.sys[i:self.sys.index("金句怎么选", i)]
        self.assertIn("不是这集的目录", blk)
        for k in ("会改变读者的判断", "给出了机制", "读者一周后还记得"):
            self.assertIn(k, blk, f"选择判据里少了「{k}」")
        self.assertIn("能被反对的话", blk, "小标题必须要求是论断")
        self.assertIn("具体画面", blk,
                      "要点必须要求留住说话人给的画面——那是让判断粘住的东西")

    def test_quote_rubric_is_about_load_bearing(self):
        i = self.sys.index("金句怎么选")
        blk = self.sys[i:]
        self.assertIn("承重句", blk, "金句判据必须说清它不是格言")

    def test_dek_rubric_forbids_stacking_claims(self):
        """dek 曾经把四个论断串成一句——正是标题判据明令禁止的并列，
        但 dek 没有对应的规则。"""
        i = self.sys.index("一句话结论（dek）怎么写")
        blk = self.sys[i:self.sys.index("要点怎么选", i)]
        self.assertIn("只承载一个论断", blk)

    def test_verbatim_rule_survives(self):
        """加判据不能把机械闸门那条挤掉——金句逐字复制是四道工序里的一道。"""
        self.assertIn("金句必须逐字复制原文", self.sys)
        self.assertIn("会被机器逐字比对", self.sys)


class YouTubeOnlyEpisodes(unittest.TestCase):
    """只在 YouTube 上发的节目也得能收。

    管线是 feed 驱动的，而有些节目**只在频道上发**。实测 Y Combinator：
    「Paul Graham On Startups, Ambition, and Great Founders」（5bxp78i96S8）
    在频道上是最新一条，336 集的 RSS feed 里根本没有它——全 feed 里和这个标题
    最相似的只有 0.18 分，是 5 月另一期 PG。这类内容我们以前一集都收不到，
    而且没有任何信号告诉我们漏了。
    """

    def test_tool_reuses_the_pipeline_gates(self):
        """收录一条视频不能自己另写一套发布逻辑——四道闸门必须原样跑。"""
        src = (ROOT / "pipeline" / "addvideo.py").read_text()
        self.assertIn("R.process(ep, state", src,
                      "必须走 run.process()，不能自己拼发布")
        self.assertNotIn("json.dumps(rec", src, "别自己写单集文件，那会绕过闸门")

    def test_dedups_against_the_feed_version(self):
        """频道版和 feed 版标题常常不一样（实测 Max Hodak 那集 feed 叫
        How Startups Build Speed、频道叫 Average Is Not Good Enough），
        只按标题查会把同一场对话发两遍。"""
        src = (ROOT / "pipeline" / "addvideo.py").read_text()
        self.assertIn("def dup_by_duration", src)
        self.assertIn("seek_tolerance", src, "时长判据要和时间戳容差用同一个数")
        self.assertIn("def already_have", src)

    def test_metadata_does_not_hinge_on_yt_dlp(self):
        """批量跑时 yt-dlp 会撞 bot 检查。watch 页不吃这个，必须是主路径。"""
        src = (ROOT / "pipeline" / "addvideo.py").read_text()
        body = src[src.index("def meta"):src.index("def already_have")]
        code = re.sub(r'"""[\s\S]*?"""', "", body)     # 文档串里也提 yt-dlp
        self.assertIn("net.get_text", code, "先读 watch 页")
        self.assertIn("yt-dlp", code, "yt-dlp 作为第二条路还是要有")
        self.assertLess(code.index("net.get_text"), code.index("yt-dlp"),
                        "watch 页必须在前：yt-dlp 只是补齐，不是主路径")


class BackfillToolsMustNotRenormalize(unittest.TestCase):
    """回填工具只许改它声明要改的字段，不许对已发布的数据整体 normalize。

    digest.normalize 是给**模型原始输出**用的：它把每个字段 str() 一遍，于是
    `t: None` 会变成 `""`。对已发布的数据调它，时间戳会被整片写成空串——而
    hhmmss("") 当时会抛 ValueError，把整站 267 篇的构建一起炸掉。

    我在写 repoint.py 时就这么干了一次，回填跑起来了才发现（幸好第一篇还没写盘）。
    retitle.py 和 repunct.py 一直是逐字段过 _cn_punct，正是因为这个。
    """

    def test_no_tool_calls_normalize_on_published_data(self):
        for name in ("repoint.py", "retitle.py", "repunct.py", "video.py"):
            f = ROOT / "pipeline" / name
            if not f.exists():
                continue
            src = f.read_text()
            code = re.sub(r'"""[\s\S]*?"""', "", src)
            self.assertNotIn("normalize(", code,
                             f"{name} 对已发布数据调了 normalize()，"
                             f"会把 t: None 写成空串")

    def test_hhmmss_survives_junk(self):
        """一个字段的坏值不该让 267 篇都发不出去。判据放在 normalize 那层，
        但格式化助手不能放大故障。"""
        sys.path.insert(0, str(ROOT / "pipeline"))
        from lib.util import hhmmss
        for junk in ("", None, "abc", [], {}):
            self.assertEqual(hhmmss(junk), "", f"hhmmss({junk!r}) 应该给空串")
        self.assertEqual(hhmmss(0), "0:00")
        self.assertEqual(hhmmss("125"), "2:05")
        self.assertEqual(hhmmss(3725), "1:02:05")

    def test_no_string_timestamps_on_disk(self):
        """线上不该有字符串型的时间戳。gate 会把它们规成 int | None，
        出现字符串就说明有工具绕过了 gate 直接写盘。"""
        bad = []
        for f in (ROOT / "data" / "episodes").glob("*.json"):
            d = json.loads(f.read_text()).get("digest") or {}
            for k in ("points", "quotes", "facts"):
                for it in (d.get(k) or []):
                    if isinstance(it.get("t"), str):
                        bad.append(f"{f.name}:{k}")
                        break
        self.assertEqual(bad, [], f"{len(set(bad))} 篇有字符串型时间戳")


class HiddenAttributeMustActuallyHide(unittest.TestCase):
    """带 hidden 出场（或被脚本藏起来）的元素，必须自己补一条 [hidden] 规则。

    浏览器默认表里有 `[hidden]{display:none}`，但**作者样式表排在 UA 表之后**，
    所以任何 `.foo{display:block}` 都会把 hidden 废掉。这不是特异性问题，
    加权重也没用，只能显式写 `.foo[hidden]{display:none}`。

    线上两处真实后果：
      · `.video-facade{display:block}` → 点播放之后假门根本没藏起来，.vwrap 加在
        它旁边，播放器卡片从 192px 变成 381px，整页往下跳 189px。用户报的
        「点播放按钮整个界面会跳一下」就是这个。
      · `.aui{display:flex}` → 它是带着 hidden 出场的（渐进增强），于是脚本跑
        起来之前，自绘控件和原生 controls 在 150 多个只有音频的页面上同时显示。

    这条检查是机械的：从 CSS 里收集"给某个类设了 display"的类名，从构建产物里
    收集"带 hidden 属性出场"和"被脚本 .hidden = true / .hidden=false 操作"的类名，
    两边一交叉，就必须有对应的 [hidden] 规则。
    """

    def setUp(self):
        self.css = (ROOT / "assets" / "site.css").read_text()
        self.js = (ROOT / "assets" / "site.js").read_text()

    def _classes_with_display(self):
        """CSS 里给某个类设了 display 的类名。"""
        body = re.sub(r"/\*[\s\S]*?\*/", "", self.css)
        out = set()
        for sel, decl in re.findall(r"([^{}]+)\{([^{}]*)\}", body):
            if not re.search(r"(^|;)\s*display\s*:", decl):
                continue
            if "[hidden]" in sel:
                continue
            for part in sel.split(","):
                # 只取选择器**最后一段**：display 设在后代上（`.empty b`、
                # `.player .strip .afallback`）跟祖先那个类没关系。第一版把整条
                # 选择器里所有类都收进来，误报了 .empty 和 .strip。
                last = re.split(r"[\s>+~]+", part.strip())[-1]
                for m in re.finditer(r"\.([A-Za-z][\w-]*)", last):
                    out.add(m.group(1))
        return out

    def _has_hidden_rule(self, cls):
        return bool(re.search(r"\.%s\[hidden\]\s*\{[^}]*display\s*:\s*none"
                              % re.escape(cls), self.css))

    def test_elements_that_ship_hidden(self):
        """构建产物里凡是带 hidden 属性出场的元素。"""
        with_display = self._classes_with_display()
        bad = []
        pages = [ROOT / "index.html"]
        pages += [d / "index.html" for d in sorted((ROOT / "p").iterdir())[:40]]
        for page in pages:
            if not page.exists():
                continue
            html = page.read_text()
            # `hidden` 必须是独立属性：\b 会把 aria-hidden="true" 也匹配上，
            # 因为连字符算词边界——我第一版就这么误报了 32 处。
            for tag in re.findall(r"<[a-z]+[^>]*(?<![-\w])hidden(?=[\s=>])[^>]*>",
                                  html):
                for cls in re.findall(r'class="([^"]*)"', tag):
                    for c in cls.split():
                        if c in with_display and not self._has_hidden_rule(c):
                            bad.append(f"{c} @ {page.parent.name}")
        self.assertEqual(sorted(set(bad)), [],
                         "这些元素带 hidden 出场，但 CSS 给它们设了 display，"
                         "hidden 会被废掉；补 .<类名>[hidden]{display:none}")

    def test_elements_the_script_hides(self):
        """脚本里被 `.hidden = ` 操作的元素。它们不带 hidden 出场，所以上一条
        查不到——假门正是这一类，也正是线上跳 189px 的那个。

        做法是两趟：先找出所有被赋值 .hidden 的变量名，再回去看那个变量是从哪个
        选择器来的。不列白名单——按名字列清单的检查只能靠白名单苟活。"""
        with_display = self._classes_with_display()
        hidden_vars = set(re.findall(r"\b([A-Za-z_$][\w$]*)\.hidden\s*=", self.js))
        bad = []
        for v in sorted(hidden_vars):
            # var v = document.querySelector('.cls') / ('[attr]')
            for m in re.finditer(r"\b%s\s*=\s*[^;\n]*querySelector\w*\("
                                 r"\s*'([^']+)'" % re.escape(v), self.js):
                for c in re.findall(r"\.([A-Za-z][\w-]*)", m.group(1)):
                    if c in with_display and not self._has_hidden_rule(c):
                        bad.append(f"{v} → .{c}")
        self.assertEqual(sorted(set(bad)), [],
                         "脚本把这些元素 .hidden 掉，但 CSS 给它们设了 display，"
                         f"藏不住：{sorted(set(bad))}")

    def test_the_two_known_ones_are_covered(self):
        for cls in ("video-facade", "aui"):
            self.assertTrue(self._has_hidden_rule(cls),
                            f".{cls}[hidden]{{display:none}} 不见了")


class ShareLinkCarriesAPreview(unittest.TestCase):
    """分享短链页必须自带完整的 og 标签。

    用户："分享 url 时没带预览图片"。分享按钮给出的是 /e/<id>/ 短链（正文 slug
    是中文，percent-encode 之后两百多字符，粘到朋友圈里链接比内容还长），而这个
    页面原来只有 title 和 canonical——**全站每一次分享都没有预览图**。

    根因是一个想当然：我以为 canonical 指回正文就够了。抓预览图的一方（微信、
    Twitter、Slack）只读它拿到的那个 URL 的 meta，**不跟 canonical、不跟
    http-equiv refresh、更不执行 JS**。canonical 是给搜索引擎的，两套机制。
    """

    def _alias_pages(self, n=6):
        d = ROOT / "e"
        out = []
        for sub in sorted(d.iterdir())[:n] if d.exists() else []:
            f = sub / "index.html"
            if f.exists():
                out.append((sub.name, f.read_text()))
        return out

    def test_every_alias_has_the_full_card(self):
        need = ["og:title", "og:description", "og:url", "og:type",
                "twitter:card", "twitter:title"]
        for name, html in self._alias_pages():
            for k in need:
                self.assertIn(k, html, f"/e/{name}/ 缺 {k}")

    def test_alias_has_an_image_when_the_episode_has_one(self):
        import json as _j
        by_id = {}
        for f in (ROOT / "data" / "episodes").glob("*.json"):
            d = _j.loads(f.read_text())
            if d.get("id"):
                by_id[d["id"]] = d
        missing = []
        for name, html in self._alias_pages(12):
            ep = by_id.get(name)
            if ep and ep.get("image") and "og:image" not in html:
                missing.append(name)
        self.assertEqual(missing, [],
                         f"这些短链页有封面却没写 og:image：{missing}")

    def test_no_json_escapes_left_in_urls(self):
        """封面 URL 曾经是从页面里的 JSON 串抠出来的，带着字面量 \\u0026，
        写进 og:image 就是个坏链接。图片地址一律按 id 构造，不从 JSON 里抠。"""
        import json as _j
        bad = []
        for f in (ROOT / "data" / "episodes").glob("*.json"):
            d = _j.loads(f.read_text())
            for k in ("image", "audio", "link", "transcript_url"):
                v = d.get(k) or ""
                if "\\u" in v or "\\/" in v:
                    bad.append(f"{d.get('slug','?')[:30]}:{k}")
        self.assertEqual(bad, [], f"这些 URL 里有 JSON 转义残留：{bad[:5]}")


class ProvenancePanelMustHaveContent(unittest.TestCase):
    """每篇的 digest.quality 必须齐全，页面上"这篇是怎么来的"那块不能是空的。

    用户报的："这篇是怎么来的，这个模块里面都是空的"。真因是 digest.normalize
    **从零重建 dict**——列举已知的文本字段重新组装，于是任何没被列举的字段静默
    消失。`quality` 是 gate 算出来放进 digest 的（文稿来源、字数、语速、逐字校验
    条数、质检剔除），normalize 不认识它，就把它扔了。

    这是同一个 normalize 误用的**第二处后果**。第一处（t: None → ""，
    hhmmss 抛异常炸掉整站构建）我修了，却没发现 quality 也没了——因为那次我只
    照着报错去查，没问一句"它还丢了什么"。所以这条检查查的是**字段齐全**，
    不是查某一个具体症状。
    """

    NEED = ("transcript_source", "words", "wpm", "verified_quotes",
            "grounded_facts", "points", "approx_timestamps")

    def test_every_episode_has_a_full_quality_block(self):
        bad = []
        for f in (ROOT / "data" / "episodes").glob("*.json"):
            d = json.loads(f.read_text())
            q = (d.get("digest") or {}).get("quality")
            if not q:
                bad.append(f"{d.get('slug','?')[:40]}: quality 整块缺失")
                continue
            miss = [k for k in self.NEED if q.get(k) is None]
            if miss:
                bad.append(f"{d.get('slug','?')[:40]}: 缺 {miss}")
        self.assertEqual(bad, [], f"{len(bad)} 篇的来源信息不全：{bad[:4]}")

    def test_normalize_carries_unknown_fields_through(self):
        """判据编的是失败机制本身：normalize 只负责规整文本，
        不认识的字段必须原样带过去。"""
        sys.path.insert(0, str(ROOT / "pipeline"))
        from lib.digest import normalize
        raw = {"title": "甲乙丙丁", "dek": "", "why": "", "who": "", "skip": "",
               "points": [{"t": 5, "h": "标题", "body": "正文"}],
               "quotes": [], "facts": [], "terms": [], "tags": [],
               "quality": {"words": 123}, "未来某个新字段": "别丢我"}
        out = normalize(raw)
        self.assertEqual(out.get("quality"), {"words": 123},
                         "normalize 把 quality 丢了")
        self.assertEqual(out.get("未来某个新字段"), "别丢我",
                         "normalize 会丢掉它不认识的字段——这就是 quality 消失的原因")

    def test_normalize_keeps_timestamps_numeric(self):
        """t 不是文本。str() 一遍会把 None 变成 ""，而 hhmmss("") 曾抛
        ValueError 把整站 267 篇的构建一起炸掉。"""
        sys.path.insert(0, str(ROOT / "pipeline"))
        from lib.digest import normalize
        out = normalize({"title": "甲乙丙丁", "dek": "", "why": "", "who": "",
                         "skip": "", "quotes": [], "facts": [], "terms": [],
                         "tags": [],
                         "points": [{"t": 5, "h": "a", "body": "b"},
                                    {"t": None, "h": "c", "body": "d"},
                                    {"t": "2:05", "h": "e", "body": "f"}]})
        got = [p["t"] for p in out["points"]]
        self.assertEqual(got, [5, None, 125], f"时间戳被改坏了：{got}")

    def test_the_panel_renders_something(self):
        """数据齐全不等于页面上渲染出来了。抽查构建产物里那块面板有内容。"""
        import re as _re
        for d in sorted((ROOT / "p").iterdir())[:8]:
            f = d / "index.html"
            if not f.exists():
                continue
            html = f.read_text()
            i = html.find("这篇是怎么来的")
            self.assertGreater(i, 0, f"{d.name} 没有来源面板")
            block = html[i:i + 2200]
            rows = _re.findall(r'class="row"', block)
            self.assertGreaterEqual(len(rows), 4,
                                    f"{d.name} 来源面板只有 {len(rows)} 行，是空的")


class EnglishEdition(unittest.TestCase):
    """英文版：内容层、文案层、和"不许中英混排"那条闸门。

    英文版和繁体版性质不同：繁体是 tw.py 对构建好的 HTML 做字形转换，翻译不成立。
    所以英文走两条独立的路——正文来自 data/en/<slug>.json，界面文案来自
    i18n.UI 那张人工审过的表。

    **最关键的一条：金句绝不回译。** 269 篇里 235 篇是英文源节目，它们的
    quotes[].raw 本来就是逐字英文原话。回译会毁掉这个站的前提（每句都能跳回原声
    核对），而英文读者**看不出那是译文**，比中文站上更糟。
    """

    def setUp(self):
        sys.path.insert(0, str(ROOT / "pipeline"))
        self.en_dir = ROOT / "data" / "en"

    def _records(self, n=40):
        out = []
        if not self.en_dir.exists():
            return out
        for f in sorted(self.en_dir.glob("*.json")):
            if f.name.startswith("_"):
                continue
            out.append(json.loads(f.read_text()))
            if len(out) >= n:
                break
        return out

    def test_quotes_from_english_shows_are_never_retranslated(self):
        """英文源节目的金句必须和中文稿里的 raw **逐字一致**。
        差一个字符就说明它被回译过了。"""
        by_slug = {}
        for f in (ROOT / "data" / "episodes").glob("*.json"):
            d = json.loads(f.read_text())
            if d.get("slug"):
                by_slug[d["slug"]] = d
        bad = []
        checked = 0
        for r in self._records(60):
            if r.get("source_lang") != "en":
                continue
            src = by_slug.get(r.get("slug"))
            if not src:
                continue
            raws = [q.get("raw") or "" for q in (src["digest"].get("quotes") or [])]
            for i, q in enumerate(r.get("quotes") or []):
                if i >= len(raws):
                    continue
                checked += 1
                if q.get("translated"):
                    bad.append(f"{r['slug'][:30]}[{i}] 标成了译文")
                elif q.get("text") != raws[i]:
                    bad.append(f"{r['slug'][:30]}[{i}] 和原话不一致")
        if checked:
            self.assertEqual(bad, [], f"{len(bad)} 条金句被回译了：{bad[:3]}")

    def test_chinese_source_quotes_are_marked_as_translations(self):
        for r in self._records(60):
            if r.get("source_lang") != "zh":
                continue
            for i, q in enumerate(r.get("quotes") or []):
                self.assertTrue(q.get("translated"),
                                f"{r['slug'][:30]}[{i}] 是中文源的金句，"
                                f"译文必须标 translated")

    def test_counts_line_up_with_the_source(self):
        """条数不齐会让时间戳和内容错位——译文的 points[i] 必须对应原稿的
        points[i]，因为时间戳只在原稿里。"""
        by_slug = {}
        for f in (ROOT / "data" / "episodes").glob("*.json"):
            d = json.loads(f.read_text())
            if d.get("slug"):
                by_slug[d["slug"]] = d
        for r in self._records(60):
            src = by_slug.get(r.get("slug"))
            if not src:
                continue
            for k in ("points", "terms", "facts", "tags"):
                self.assertEqual(
                    len(src["digest"].get(k) or []), len(r.get(k) or []),
                    f"{r['slug'][:30]} 的 {k} 条数和原稿不一致")

    def test_no_chinese_left_in_english_fields(self):
        """判据是**预算**，不是零。只有中文名的中国应用（懂车帝、玄界、朱雀三号）
        在英文正文里是合法专名，实测占比 0.3%；漏译一整句是 30% 以上。
        原来按"一个汉字都不许有"判，66/265 篇卡在这上面。"""
        import re as _re
        cjk = _re.compile(r"[一-鿿]")
        punct = _re.compile(r"[「」『』，。、；：？！（）《》【】〔〕]")
        bad = []

        def over(v):
            n = len(cjk.findall(v))
            return bool(n) and (n > max(8, len(v) * 0.04)
                                or not _re.search(r"[A-Za-z]", v))

        for r in self._records(60):
            for k in ("title", "dek", "why", "who", "skip"):
                v = r.get(k) or ""
                if over(v) or punct.search(v):
                    bad.append(f"{r['slug'][:26]}.{k}")
            for k in ("points", "terms", "facts"):
                for i, row in enumerate(r.get(k) or []):
                    for kk, vv in row.items():
                        if isinstance(vv, str) and (over(vv) or punct.search(vv)):
                            bad.append(f"{r['slug'][:26]}.{k}[{i}].{kk}")
        self.assertEqual(bad[:5], [], f"{len(bad)} 个英文字段里有中文残留")

    # ------------------------------------------------------- 文案层
    def test_zh_build_is_unaffected_by_the_locale_layer(self):
        """T() 在简体模式必须是恒等函数——给模板加 T() 不许改变简体站的输出。"""
        import i18n
        old = i18n.LANG
        i18n.LANG = "zh"
        try:
            for k in list(i18n.UI)[:20]:
                self.assertEqual(i18n.T(k), k)
            self.assertEqual(i18n.n(7, "quote"), "7 条")
            self.assertEqual(i18n.score(8.0), "8.0 分")
        finally:
            i18n.LANG = old

    def test_missing_ui_string_fails_the_build(self):
        """漏一条界面文案必须让构建失败，不能回落到中文——回落会让漏译静默
        变成中英混排，那正是要防的东西。"""
        src = (ROOT / "pipeline" / "build.py").read_text()
        self.assertIn("i18n.missed()", src)
        i = src.index("i18n.missed()")
        self.assertIn("return 1", src[i:i + 400],
                      "有没登记的文案时构建必须非零退出")

    def test_leak_gate_uses_a_parser_not_a_regex(self):
        """判据是"汉字只许在 lang=zh 里"。用 HTML 解析器，不用正则去标签——
        我写过两版正则，两版都误报（属性值里有 > 就提前收尾）。"""
        src = (ROOT / "pipeline" / "enscan.py").read_text()
        self.assertIn("HTMLParser", src)
        self.assertNotIn('re.sub(r"<[^>]+>"', src)

    def test_leak_gate_actually_catches(self):
        """反向验证：造一个漏译，闸门必须报。"""
        import enscan
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            (d / "leak.html").write_text(
                "<html><body><p>首页</p></body></html>")
            self.assertTrue(enscan.leaks(d), "闸门抓不到 body 里的漏译")
            (d / "leak.html").write_text(
                '<html><body><p lang="zh">科技这碗饭</p></body></html>')
            self.assertFalse(enscan.leaks(d), "lang=zh 的中文不该被当成漏译")

    def test_output_dirs_are_never_hardcoded_in_writers(self):
        """render_site 会被调两次（简体渲到根、英文渲到 en/），所以凡是写文件的
        函数都必须收输出目录。write_card_pages 原来直接写 ROOT，英文那一趟把
        **根目录**的 cards-*.json 覆盖成了带 /podcast/en/ 链接的版本——简体首页
        滚到第二页就全跳去英文站了。"""
        src = (ROOT / "pipeline" / "build.py").read_text()
        i = src.index("def write_card_pages")
        body = src[i:src.index("\ndef ", i + 10)]
        self.assertIn("out = out or ROOT", body, "必须收输出目录")
        # 写卡片的那几行必须用 out。（清理第一版遗留的 ROOT / "cards.json"
        # 是仓库根独有的一次性动作，不在此列。）
        for ln in body.split("\n"):
            if ".write_text(" in ln or ".unlink()" in ln:
                if 'cards.json"' in ln:      # 旧单文件，只在仓库根
                    continue
                self.assertNotIn("ROOT /", ln,
                                 f"这行还在写 ROOT：{ln.strip()[:70]}")

    def test_source_loop_does_not_shadow_the_output_root(self):
        """把 main() 里的 ROOT 机械替换成 out 时，源站页循环 `out = sdir / id`
        覆盖了函数参数，于是后面的 p/、e/、robots.txt 全写进了最后一个源的目录
        （仓库里留下过 s/tokcast/p/）。"""
        src = (ROOT / "pipeline" / "build.py").read_text()
        i = src.index("def render_site")
        import re as _re
        i2 = src.index("sdir / src[", i)
        line = src[src.rindex("\n", 0, i2) + 1:src.index("\n", i2)]
        # 用词边界匹配："out =" 是 "sout =" 的子串，直接 in 会误报
        self.assertIsNone(_re.search(r"\bout\s*=", line),
                          f"源站页循环不许用 out 当变量名：{line.strip()}")

    def test_no_stray_nested_page_dirs(self):
        """上面那个 bug 的产物长这样：s/<id>/p/。它在仓库里躺过一阵。"""
        s = ROOT / "s"
        bad = [d.name for d in s.iterdir()
               if d.is_dir() and (d / "p").exists()] if s.exists() else []
        self.assertEqual(bad, [], f"这些源站目录下有多余的 p/：{bad}")


class RenderSiteMustNotShadowItsOutputRoot(unittest.TestCase):
    """render_site(out, lang) 的函数体里不许再给 out 赋值。

    这是一次机械替换（把 main() 里的 ROOT 全换成 out）留下的坑，**三处**，
    每处症状都不一样，而且都不会让构建报错：

      · 源站页循环 `out = sdir / src["id"]` → 之后的 p/、e/、robots.txt、
        llms.txt 全写进了 s/<最后一个源>/ 下（仓库里留下了 s/tokcast/p/）
      · 短链循环 `out = edir / x["id"]` → 循环结束后 out 指向最后一集的目录
      · 正文页循环 `out = pdir / x["slug"]` → 同上

    症状是"少了两个短链页"、"多了个 s/tokcast/p 目录"这种，离原因很远。
    判据直接编在机制上：**函数体内对 out 的赋值一律禁止**。
    """

    def test_no_assignment_to_out_inside_render_site(self):
        src = (ROOT / "pipeline" / "build.py").read_text()
        i = src.index("def render_site")
        body = src[i:src.index("\ndef main", i)]
        bad = [ln.strip() for ln in body.split("\n")
               if re.match(r"\s*out\s*=\s*\S", ln)]
        self.assertEqual(bad, [],
                         f"render_site 里给 out 赋了值，会遮蔽输出根目录：{bad}")

    def test_alias_count_matches_episodes(self):
        n_eps = len(list((ROOT / "data" / "episodes").glob("*.json")))
        n_alias = len([d for d in (ROOT / "e").iterdir() if d.is_dir()]) \
            if (ROOT / "e").exists() else 0
        self.assertEqual(n_eps, n_alias, "短链页数量和篇数对不上")


class HreflangIsPerPage(unittest.TestCase):
    """只在这一页真有英文版时才声明 hreflang=en。

    译文是逐篇补的，没译的集不进 /en/。在它们的中文页上写 hreflang=en 等于把
    搜索引擎指到一个 404——而这类错误不会有任何症状，只会安静地损失收录。
    """

    def test_no_en_hreflang_without_an_english_page(self):
        en_p = ROOT / "en" / "p"
        if not en_p.exists():
            self.skipTest("英文站还没建")
        have = {d.name for d in en_p.iterdir() if d.is_dir()}
        bad = []
        for d in sorted((ROOT / "p").iterdir())[:120]:
            f = d / "index.html"
            if not f.exists():
                continue
            claims = 'hreflang="en"' in f.read_text()
            if claims != (d.name in have):
                bad.append(f"{d.name[:34]} claims={claims} has_en={d.name in have}")
        self.assertEqual(bad[:4], [], f"{len(bad)} 页的 hreflang=en 和实际不符")


class LanguageSwitchIsOneControl(unittest.TestCase):
    """三语共用一个下拉，而且不许把读者送到不存在的页面。

    用户："英和简繁放到同一个下拉菜单"。原来是"简繁一个按钮 + 英文一个链接"，
    对读者是同一件事却给了两种控件（同伴把三个站的这个控件统一过，这里只是把
    英文加进同一个下拉）。

    另一条更要紧：译文是逐篇补的，**这一页没有英文版就不许出现英文那一项**，
    否则读者点下去是 404。占位元素上的 data-en 说明有没有，JS 照它决定。
    """

    def test_single_control_no_separate_en_link(self):
        src = (ROOT / "pipeline" / "build.py").read_text()
        i = src.index("def lang_switch")
        body = src[i:src.index("\ndef masthead", i)]
        self.assertEqual(body.count('id="lang-toggle"'), 2,
                         "简体树和英文树各一个占位元素，不该有别的语言控件")
        self.assertNotIn('/en/">EN', body, "英文不该是另一个独立链接")

    def test_dropdown_offers_three_options(self):
        src = (ROOT / "pipeline" / "build.py").read_text()
        i = src.index("LANG_JS")
        blk = src[i:src.index("def _has_en", i)]
        for k in ("'sc'", "'tw'", "'en'"):
            self.assertIn(k, blk, f"下拉里少了 {k}")
        self.assertIn("if(hasEn)opts.push", blk,
                      "没有英文版时不许给英文那一项")

    def test_data_en_matches_reality(self):
        """data-en 必须和磁盘上真有没有英文页一致。"""
        import re as _re
        en_p = ROOT / "en" / "p"
        if not en_p.exists():
            self.skipTest("英文站还没建")
        have = {d.name for d in en_p.iterdir() if d.is_dir()}
        bad = []
        for d in sorted((ROOT / "p").iterdir())[:120]:
            f = d / "index.html"
            if not f.exists():
                continue
            m = _re.search(r'id="lang-toggle"[^>]*data-en="([^"]*)"', f.read_text())
            if not m:
                bad.append(f"{d.name[:30]} 没有 data-en")
                continue
            if bool(m.group(1)) != (d.name in have):
                bad.append(f"{d.name[:30]} data-en={m.group(1)!r} 但 en 页 "
                           f"{'有' if d.name in have else '没有'}")
        self.assertEqual(bad[:4], [], f"{len(bad)} 页的 data-en 和实际不符")

    def test_arrow_survives_the_pill_shorthand(self):
        """`.pill.ghost` 用 background 简写会把 background-image 一起重置成 none，
        而它（两个类）的特异性高过 select.pill——箭头就是这么消失的，
        整个过程没有报错、没有 404，computed 值直接是 none。

        另外那个 data URI **必须写成一行**：我第一版用反斜杠折行，CSS 解析不出来。
        """
        css = (ROOT / "assets" / "site.css").read_text()
        i = css.index(".pill.ghost{")
        self.assertIn("background-color:transparent", css[i:i + 60],
                      ".pill.ghost 不许用 background 简写")
        j = css.index("select.pill")
        blk = css[j:j + 1400]
        self.assertIn("background-image:url(", blk)
        # 折行会让它解析失败：url( 到 ) 之间不许有换行
        for m in re.finditer(r"background-image:url\(", blk):
            seg = blk[m.start():blk.index(")", m.start())]
            self.assertNotIn("\n", seg, "data URI 折行了，CSS 解析不出来")

    def test_tagline_fits_one_line_on_mobile(self):
        """用户："移动端 slogn 换行了"。英文那句 52 字符在 375px 上折两行。
        判据用字符数近似（真实换行由渲染层体检量）。"""
        sys.path.insert(0, str(ROOT / "pipeline"))
        import i18n
        self.assertLessEqual(len(i18n.TAGLINES["en"]), 44,
                             "英文口号太长，手机上会折行")

    def test_focus_ring_is_not_the_accent_colour(self):
        """用户："去掉下拉菜单选中时的红色外圈"。全局 :focus-visible 是
        2px accent（红）。**但不能直接删掉焦点提示**——键盘用户会不知道焦点在
        哪。换成描边变色 + 淡环。"""
        css = (ROOT / "assets" / "site.css").read_text()
        i = css.index("select.pill:focus-visible")
        blk = css[i:i + 220]
        self.assertIn("outline:none", blk)
        self.assertNotIn("var(--accent)", blk, "焦点环不该再用红色")
        self.assertIn("box-shadow", blk, "去掉红圈之后必须留一个看得见的焦点提示")


class BuildOutputMustNotDependOnEnvFlags(unittest.TestCase):
    """同样的数据必须产出同样的站。

    英文站原来由 PODCAST_EN / PODCAST_EN_LIVE 两个环境变量控制，后果是**同样的
    数据能产出两种 HTML**：裸跑 build.py 的中文页不声明英文版，工作流里带开关跑
    的声明——committed HTML 会在两次构建之间来回翻。而"构建是幂等的"那道闸门
    只在同一次调用里比两遍，抓不到这种跨调用的分叉。

    现在英文站由 data/en/ 里有没有译文决定。
    """

    def test_no_language_env_switches(self):
        for f in list((ROOT / ".github" / "workflows").glob("*.yml")) + \
                 list((ROOT / "scripts").glob("*.sh")) + \
                 [ROOT / "pipeline" / "build.py"]:
            src = re.sub(r"(?m)^\s*#.*$", "", f.read_text())
            for k in ("PODCAST_EN=", "PODCAST_EN_LIVE"):
                self.assertNotIn(k, src,
                                 f"{f.name} 里还有语言开关 {k}——"
                                 f"同样的数据会产出两种站")

    def test_en_live_comes_from_data(self):
        src = (ROOT / "pipeline" / "build.py").read_text()
        i = src.index("EN_LIVE =")
        self.assertIn('DATA / "en"', src[i:i + 220],
                      "EN_LIVE 必须由 data/en/ 决定")


class ChineseProperNounsInEnglishText(unittest.TestCase):
    """只有中文名的中国应用和公司，在英文正文里是合法的专名，不是漏译。

    实测：一篇中文源节目的译文里出现 懂车帝、幸福里、海豚股票 —— 占正文
    0.30%，而真正漏译一整句的话汉字占比在 30% 以上。原来的判据是"一个汉字
    都不许有"，把这类合法专名连着整篇一起打回，**66/265 篇卡在这上面**。

    现在两头都管住：
      · translate.py 给一个预算（≤4% 且必须含拉丁字母），超了才算漏译
      · build.py 渲染时把汉字连续段包进 <span lang="zh">，否则英文站的
        "零漏译"闸门会把合法专名报成漏译
    """

    def test_budget_allows_a_gloss_but_not_a_sentence(self):
        import importlib.util as iu
        sp = iu.spec_from_file_location("tr", ROOT / "pipeline" / "translate.py")
        m = iu.module_from_spec(sp)
        sp.loader.exec_module(m)
        base = {"digest": {"points": [], "terms": [], "facts": [], "tags": [],
                           "quotes": []}}
        gloss = ("Apps like 懂车帝 and 幸福里 took the traffic, which is why the "
                 "standalone app never needed its own brand at all here.")
        untranslated = "这一段完全没有翻译，整句都是中文，应该被判为漏译。"
        ok = m.check(base, {"title": "x" * 20, "dek": "y" * 40, "points": [],
                            "terms": [], "facts": [], "tags": [], "why": gloss}, True)
        self.assertEqual(ok, [], f"合法专名被误判成漏译：{ok}")
        bad = m.check(base, {"title": "x" * 20, "dek": "y" * 40, "points": [],
                             "terms": [], "facts": [], "tags": [],
                             "why": untranslated}, True)
        self.assertTrue(bad, "整句中文没被判为漏译")

    def test_renderer_marks_cjk_runs(self):
        sys.path.insert(0, str(ROOT / "pipeline"))
        import build
        old = build.LANG
        try:
            build.LANG = "en"
            got = build.mark_zh("apps like 懂车帝 and 幸福里 won")
            self.assertEqual(got.count('<span lang="zh">'), 2)
            build.LANG = "zh"
            self.assertEqual(build.mark_zh("要点 懂车帝"), "要点 懂车帝",
                             "简体模式下 mark_zh 必须是恒等函数")
        finally:
            build.LANG = old

    def test_marking_happens_after_escaping(self):
        """先包 span 再转义会把标签本身转掉。渲染处必须是 mark_zh(e(...))。"""
        src = (ROOT / "pipeline" / "build.py").read_text()
        self.assertNotIn("e(mark_zh(", src, "顺序反了：应该是 mark_zh(e(...))")
        self.assertIn("mark_zh(e(", src)

    def test_translate_logs_the_message_not_just_the_type(self):
        """60 篇连着因为"推理把 max_tokens 用光了"失败，而日志里只有一模一样的
        RuntimeError——看不出是同一个可修的原因。"""
        src = (ROOT / "pipeline" / "translate.py").read_text()
        i = src.index("译失败")
        self.assertIn("str(ex)", src[i - 200:i + 200],
                      "异常消息必须记下来，不能只记类型")

    def test_token_budget_scales_with_input(self):
        src = (ROOT / "pipeline" / "translate.py").read_text()
        self.assertIn("need = max(", src, "max_tokens 要按原稿长度给")
        self.assertNotIn("max_tokens=4000", src, "固定 4000 会让长稿子拿不到答案")


class EveryDigestTextGoesThroughMarkZh(unittest.TestCase):
    """英文页上凡是输出成稿文本的地方，都必须过 mark_zh。

    译文里会有合法的中文专名（Xuanjie (玄界)、Zhuque-3 (朱雀三号)）。漏一处
    不包，英文站的"零漏译"闸门就把它报成漏译——而这**不是**判据误报，是渲染处
    真的少标了一个 lang。

    我漏过三处：卡片的 dek、卡片的标题、单集页的 h1。所以判据不写成"记得包"，
    写成机械扫描：模板里凡是 `e(d.get(...))` / `e(p[...])` 这类输出成稿字段的，
    外面必须套 mark_zh。
    """

    # 这些字段是成稿正文，会带中文专名
    TEXT_FIELDS = ("title", "dek", "why", "who", "skip", "body", "h",
                   "term", "def", "k", "v")

    def test_all_digest_text_is_wrapped(self):
        src = (ROOT / "pipeline" / "build.py").read_text()
        # 只看渲染函数，跳过 D()、share_text 这些不产 HTML 的
        bad = []
        for fn in ("card", "episode_page"):
            i = src.index(f"def {fn}(")
            body = src[i:src.index("\ndef ", i + 8)]
            for m in re.finditer(r"\{e\((?:d\.get\(|p\[|f\[|t\[|t\.get\()"
                                 r"[\"']([\w]+)[\"']", body):
                if m.group(1) in self.TEXT_FIELDS:
                    line = body[body.rindex("\n", 0, m.start()) + 1:
                                body.index("\n", m.start())]
                    bad.append(f"{fn}: {line.strip()[:70]}")
        self.assertEqual(bad, [],
                         "这些成稿文本没过 mark_zh，英文页上的中文专名会被"
                         f"报成漏译：{bad}")


class EnglishTypography(unittest.TestCase):
    """英文版的字体：Playfair Display 做标题、Source Serif 4 做正文，**自托管**。

    原来英文页用的是中文 UI 字体的拉丁字形（标题 Songti SC、正文 PingFang SC）
    ——它们的西文是配角，小字号下不适合长读。用户："找到最合适的英文杂志字体，
    现在看起来很不适合阅读"，并指定了这两个。

    三条硬约束：
      · **只作用于英文树**，简体和繁体的输出一个字节都不能变
      · **不引第三方请求**：这个站在隐私上一直很克制（nocookie 域、点播放才向
        YouTube 请求），不该为字体在每页加一个 fonts.googleapis.com
      · 逐字引文走正文字体，不走 Playfair：Playfair 是展示字体，笔画反差大，
        排一到三句要细读的证据在 17.5px 和深色底上会发虚
    """

    def setUp(self):
        self.css = (ROOT / "assets" / "site.css").read_text()

    def test_fonts_are_self_hosted(self):
        self.assertNotIn("fonts.googleapis.com", self.css)
        self.assertNotIn("fonts.gstatic.com", self.css)
        d = ROOT / "assets" / "fonts"
        files = sorted(d.glob("*.woff2")) if d.exists() else []
        self.assertEqual(len(files), 2, f"应该正好两个字体文件，现在 {files}")
        for f in files:
            self.assertIn(f.name, self.css, f"{f.name} 没被 CSS 引用")
            # 文件名带内容指纹：CSS 里算不了指纹，只能放文件名上
            self.assertRegex(f.name, r"\.[0-9a-f]{8}\.woff2$",
                             "字体文件名要带内容指纹，否则改字体后浏览器不重取")

    def test_only_the_english_tree_is_affected(self):
        i = self.css.index('html[lang="en"]{')
        blk = self.css[i:self.css.index("}", i)]
        for k in ("--serif", "--text", "--sans"):
            self.assertIn(k, blk)
        # 简体的 :root 里不许出现这两个字体
        j = self.css.index(":root{")
        root = self.css[j:self.css.index("}", j)]
        for f in ("Playfair Display", "Source Serif 4"):
            self.assertNotIn(f, root, f"{f} 不该出现在简体的变量里")

    def test_only_one_axis_per_font(self):
        """Source Serif 4 带 opsz 轴的那份是 119 KB，只留 wght 是 49 KB。
        两个字体合计要压在 100 KB 以内——英文页要下载它们。"""
        d = ROOT / "assets" / "fonts"
        total = sum(f.stat().st_size for f in d.glob("*.woff2")) if d.exists() else 0
        self.assertLess(total, 100 * 1024,
                        f"字体合计 {total // 1024} KB，太大了")

    def test_quotes_use_the_text_face_not_the_display_face(self):
        i = self.css.index('html[lang="en"] .dek-lead')
        blk = self.css[i:self.css.index("{font-family:var(--text)}", i)]
        self.assertIn(".quote .raw", blk, "逐字引文必须走正文字体")


class NoUnevaluatedTemplateExpressions(unittest.TestCase):
    """页面里不许出现未求值的模板表达式，比如字面量 `{T("播放")}`。

    真实发生过三处，全是同一个原因：**嵌套字符串少了 f 前缀**。外层是 f-string，
    里面拼接的普通字符串写了 `{T("...")}`，Python 当字面量原样输出，于是页面上
    印的是代码而不是文案。

    这类错误没有任何症状——不报错、不 404、构建照样绿。第一处（"时间戳会跳到
    原节目对应位置。"）是英文站的零漏译闸门抓到的，而它**在简体站上也一直是错的**，
    只是没人扫过。所以这条检查同时管三棵树。

    要排除 <script>：内联 JS 里的 `{` 是合法的语法。
    """

    _SCRIPT = re.compile(r"<script[\s\S]*?</script>", re.I)
    # 模板里会用到的辅助函数名。写死这张表而不是通配 `{任意标识符(`：
    # 后者会把 JS 里的 `{location.replace(` 一起报进来。
    _PAT = re.compile(r"\{(?:T|e|i18n\.\w+|mark_zh|zh_attr|T_dict|hhmmss|"
                      r"asset|share_button|masthead|foot)\(")

    def _pages(self, n=40):
        out = [ROOT / "index.html", ROOT / "sources" / "index.html",
               ROOT / "log" / "index.html", ROOT / "404.html"]
        for sub in ("p", "en/p", "tw/p", "s", "en/s"):
            d = ROOT / sub
            if d.exists():
                out += [x / "index.html" for x in sorted(d.iterdir())[:n]
                        if x.is_dir()]
        return [f for f in out if f.exists()]

    def test_no_literal_template_calls_in_html(self):
        bad = []
        for f in self._pages():
            body = self._SCRIPT.sub(" ", f.read_text())
            for m in self._PAT.finditer(body):
                bad.append(f"{f.relative_to(ROOT)}: "
                           f"{body[m.start():m.start() + 40]!r}")
        self.assertEqual(sorted(set(b.split(': ', 1)[1] for b in bad)), [],
                         f"{len(bad)} 处未求值的模板表达式（少了 f 前缀）")

    def test_no_stray_braces_in_visible_text(self):
        """再宽一档：正文里不该出现 `{某个标识符}` 这种形状。
        （CSS/JS 已排除；真实文案里的花括号极少，出现就值得看一眼。）"""
        pat = re.compile(r"\{[a-z_][a-z0-9_.]{2,}\}")
        bad = []
        for f in self._pages(12):
            body = self._SCRIPT.sub(" ", f.read_text())
            body = re.sub(r"<style[\s\S]*?</style>", " ", body)
            for m in pat.finditer(body):
                bad.append(f"{f.relative_to(ROOT)}: {m.group(0)}")
        self.assertEqual(bad[:4], [], f"{len(bad)} 处可疑的花括号占位")
