"""main 上 ci 连红要报出来（2026-11-25 起连红两天，体检全绿，没人知道）。"""
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import cistreak as C  # noqa: E402


def runs(*cs):
    return [{"conclusion": c, "created_at": f"2026-11-2{i}T00:00:00Z",
             "html_url": f"https://x/{i}"} for i, c in enumerate(cs)]


class RedStreakIsReported(unittest.TestCase):
    def test_three_reds_in_a_row_is_red_and_says_since_when(self):
        code, msg = C.verdict(runs("failure", "failure", "failure", "success"))
        self.assertEqual(code, 1)
        self.assertIn("连续 3 次", msg)
        self.assertIn("2026-11-22", msg, "要说出从哪一次开始红的")

    def test_one_or_two_reds_are_not_an_alarm(self):
        # 误报比漏报贵：单次抖一下就报，两周内这道闸就会被学会忽略
        self.assertEqual(C.verdict(runs("failure", "success"))[0], 0)
        self.assertEqual(C.verdict(runs("failure", "failure", "success"))[0], 0)

    def test_cancelled_runs_neither_break_nor_hide_a_streak(self):
        r = runs("cancelled", "failure", "skipped", "failure", "cancelled", "failure", "success")
        self.assertEqual(C.verdict(r)[0], 1)

    def test_green_after_red_is_green(self):
        self.assertEqual(C.verdict(runs("success", "failure", "failure", "failure"))[0], 0)

    def test_no_data_is_not_green(self):
        self.assertEqual(C.verdict([])[0], 2)
        self.assertEqual(C.verdict(runs("cancelled", "cancelled"))[0], 2)

    def test_whole_page_red_says_it_may_be_longer(self):
        self.assertIn("3+ 次", C.verdict(runs("failure", "failure", "failure"))[1])

    def test_cli_exit_codes(self):
        def cli(stdin):
            return subprocess.run([sys.executable, str(ROOT / "pipeline" / "cistreak.py")],
                                  input=stdin, capture_output=True, text=True).returncode
        self.assertEqual(cli(json.dumps({"workflow_runs": runs("failure") * 3})), 1)
        self.assertEqual(cli(json.dumps({"workflow_runs": runs("success")})), 0)
        self.assertEqual(cli(""), 2, "gh api 挂了（空输出）不能当成绿")


class WatchRunsIt(unittest.TestCase):
    def setUp(self):
        y = (ROOT / ".github" / "workflows" / "watch.yml").read_text()
        m = re.search(r"- name: main 上的 ci 还绿着吗\n(.*?)\n      - name:", y, re.S)
        self.assertIsNotNone(m, "watch.yml 里没有查 ci 连红的那一步")
        self.step, self.yml = m.group(1), y

    def test_runs_even_if_an_earlier_step_went_red(self):
        self.assertIn("if: always()", self.step)

    def test_asks_main_ci_and_feeds_the_script(self):
        self.assertIn("workflows/ci.yml/runs?branch=main&status=completed", self.step)
        self.assertIn("python pipeline/cistreak.py", self.step)
        self.assertIn("pipefail", self.step, "gh api 挂了要传到退出码上")

    def test_reason_lands_where_the_issue_and_the_phone_read_it(self):
        self.assertIn('"  坏了  ', self.step)
        self.assertIn('hc.txt"', self.step)
        self.assertIn("exit 1", self.step)

    def test_comes_before_the_issue_step(self):
        self.assertLess(self.yml.index("main 上的 ci 还绿着吗"), self.yml.index("坏了就开 issue"))


if __name__ == "__main__":
    unittest.main()
