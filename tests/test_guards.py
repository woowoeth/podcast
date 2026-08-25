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

    def test_local_runner_commits_and_pushes(self):
        # 事故：我直接跑 run.py 而不是 local-daily.sh，8 篇产出躺在本地没上线
        sh = (ROOT / "scripts/local-daily.sh").read_text()
        self.assertIn("git push", sh)
        self.assertRegex(sh, r"commit -q -m")


if __name__ == "__main__":
    unittest.main(verbosity=2)
