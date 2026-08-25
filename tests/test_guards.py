"""把 POSTMORTEM.md 里的教训变成可执行的检查。

这些不测业务逻辑，测的是"我上次是怎么犯错的"。每一条都对应 POSTMORTEM 里一条
真实事故；纯文档防不住重犯，能断言的就断言。
"""
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
            for m in re.finditer(r"for i in \$\(seq 1 \d+\); do(.*?)done", body, re.S):
                blk = m.group(1)
                if "git push" not in blk:
                    continue
                tail = body[m.end():m.end() + 400]
                self.assertTrue(
                    re.search(r'\$ok"?\s*!?=\s*1|exit 1|::error::', tail + blk),
                    f"{wf} 里有个含 git push 的重试循环没有检查结果")

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
        nested = set(re.findall(r"\.([a-z][\w-]*)\s+\.([a-z][\w-]*)\s*\{", css))
        allowed = {("hero", "cover"), ("ep-head", "kicker"), ("brand", "slogan"),
                   ("brand", "wordmark"), ("guide", "k"), ("empty", "h1")}
        for parent, child in nested:
            if child in globals_ and (parent, child) not in allowed:
                self.fail(f".{parent} .{child} 的末端类名同时是全局规则 .{child}，"
                          f"会被串味；确认无害后加进 allowed")


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
