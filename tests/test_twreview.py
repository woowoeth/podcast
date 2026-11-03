#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""繁体歧义字日常审阅（pipeline/twreview.py）的判据。

test_tw.test_4 原来见到待审就红，正常发稿后必红，19 天没人看。改成日更里由模型审、
只在「审阅这一步坏了」时红之后，要盯住的是这一步自己会怎么骗人：
  · 模型的改法动了别的字、换成表外的字 —— 不许收（它会直接改站上的字）；
  · 模型没回、回了一半 —— 没回的不许算「审过」，last_ok 不许更新；
  · 过期判据 —— 新来的不红，搁久了、或者这一步停了要红。
"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import tw as TW  # noqa: E402
import twreview as R  # noqa: E402


class _Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = (TW.REVIEW, R.REVIEW)
        TW.REVIEW = R.REVIEW = os.path.join(self.tmp, "tw-review.json")
        TW.reset_review()

    def tearDown(self):
        TW.REVIEW, R.REVIEW = self.saved
        TW.reset_review()

    def page(self, text):
        d = os.path.join(self.tmp, "tw", "p", "x")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as fh:
            fh.write(f"<html><body><p>{text}</p></body></html>")
        return os.path.join(self.tmp, "tw")


class TwReview(_Sandbox):
    def test_a_fix_may_only_swap_the_middle_char_for_a_listed_variant(self):
        self.assertTrue(R.valid_fix("和麵板", "和面板"))
        self.assertFalse(R.valid_fix("持有隻", "持有只"))   # 歧义字不在中间
        self.assertFalse(R.valid_fix("和麵板", "和麪板"))   # 麪 不在 ALT 表里
        self.assertFalse(R.valid_fix("和麵板", "和面版"))   # 动了别的字
        self.assertFalse(R.valid_fix("和麵板", "面板"))     # 长度变了
        self.assertFalse(R.valid_fix("　麵板", "　面板"))   # 句首占位：页面源码里匹配不到
        self.assertFalse(R.valid_fix("和麵板", None))

    def test_b_review_sorts_ok_fix_and_leaves_the_rest_pending(self):
        found = {"和麵板": ["TCL 併購失敗和麵板豪賭"], "頭髮長": ["她頭髮長"],
                 "這隻佔": ["但這隻佔很小"], "那隻說": ["那隻說明"], "一隻貓": ["一隻貓"]}
        d = R.load()

        def ask(chunk):
            idx = {c: n for n, (c, _) in enumerate(chunk, 1)}
            return [{"i": idx["和麵板"], "ok": False, "fix": "和面板"},
                    {"i": idx["頭髮長"], "ok": True},
                    {"i": idx["這隻佔"], "unsure": True},
                    {"i": idx["那隻說"], "ok": False, "fix": "那只話"}]   # 动了别的字，不收
                    # 一隻貓：模型漏回
        n_ok, n_fix, n_left = R.review(found, d, ask=ask)
        self.assertEqual((n_ok, n_fix, n_left), (1, 1, 3))
        self.assertEqual(set(d["ok"]), {"頭髮長"})
        self.assertEqual(d["fix"]["和麵板"]["to"], "和面板")
        self.assertEqual(set(d["pending"]), {"這隻佔", "那隻說", "一隻貓"})
        self.assertTrue(d["last_ok"])

    def test_c_a_failed_model_call_does_not_count_as_reviewed(self):
        d = R.load()

        def boom(chunk):
            raise RuntimeError("額度用完")
        with self.assertRaises(RuntimeError):
            R.review({"和麵板": ["x"]}, d, ask=boom)
        self.assertEqual(d.get("last_ok", ""), "")
        self.assertEqual(d["ok"], {})

    def test_d_overdue_only_when_the_step_is_broken(self):
        now = dt.date(2026, 11, 10)
        found = {"那隻說": ["那隻說明"]}
        fresh = {"ok": {}, "fix": {}, "pending": {}, "last_ok": "2026-11-09T21:40:00+08:00"}
        self.assertEqual([], R.overdue(fresh, found, now), "新来的、审阅昨天还跑成过：不该红")
        old = dict(fresh, pending={"那隻說": "2026-11-07"})
        self.assertTrue(R.overdue(old, found, now), "待审搁了 3 天：要红")
        stopped = dict(fresh, last_ok="2026-11-07T21:40:00+08:00")
        self.assertTrue(R.overdue(stopped, found, now), "审阅 3 天没跑成：要红")
        never = dict(fresh, last_ok="")
        self.assertTrue(R.overdue(never, found, now), "从没跑成过：要红")
        self.assertEqual([], R.overdue(never, {}, now), "没有待审：没什么可红的")

    def test_e_convert_applies_the_recorded_fix_and_allow_reads_ok(self):
        with open(R.REVIEW, "w", encoding="utf-8") as fh:
            json.dump({"ok": {"頭髮長": "2026-11-03"},
                       "fix": {"理髮店": {"to": "理發店", "on": "2026-11-03", "eg": ""}},
                       "pending": {}, "last_ok": ""}, fh, ensure_ascii=False)
        TW.reset_review()
        self.assertEqual(TW.convert("理发店"), "理發店")
        self.assertIn("頭髮長", TW.load_allow())

    def test_f_unreviewed_scans_pages_and_skips_what_is_recorded(self):
        twdir = self.page("貓貓髮怪事和鬆餅")
        got = R.unreviewed(twdir)
        self.assertIn("貓髮怪", got)
        self.assertIn("貓貓髮怪事和鬆餅"[:12], got["貓髮怪"][0])
        R.save({"ok": {"貓髮怪": "2026-11-03"}, "fix": {}, "pending": {}, "last_ok": ""})
        self.assertNotIn("貓髮怪", R.unreviewed(twdir))


if __name__ == "__main__":
    unittest.main()
