"""Unit tests for the parts that decide what gets published.

Run: python3 -m unittest discover -s tests -v

These cover the pure functions only — no network, no model calls. The cases are
the ones that actually bit during development: spoken numbers being deleted as
"ungrounded", YouTube's rolling captions doubling every sentence, chapter lists
masquerading as transcripts, and the same episode arriving twice.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "pipeline"))

from lib import gate, transcript as T                     # noqa: E402
from lib.digest import _clean_title, _cn_punct, normalize  # noqa: E402
from lib.util import fingerprint, norm_title, parse_duration, parse_ts, hhmmss  # noqa: E402


class Numbers(unittest.TestCase):
    """A transcript says "twenty fourteen"; the digest says "2014". Both are the
    same fact, and deleting it would be a false positive."""

    HAY = ("It came out in twenty fourteen and I had been working on it for six years. "
           "you would pay a doctor four hundred dollars for the consultation. "
           "inventions we would make if we had twenty thousand years. "
           "there's a one percent a year chance they blow up the firm. "
           "it thinks it has a five percent chance of succeeding. "
           "revenue could reach one hundred billion dollars next year. "
           "we raised 200 million at a 2 billion valuation.")

    def test_spelled_out_numbers_count_as_grounded(self):
        for v in ["2014 年出版", "此前写了 6 年", "约 400 美元", "2 万年",
                  "每年 1% 概率", "5% 成功率"]:
            self.assertTrue(gate._num_in(v, self.HAY), v)

    def test_scale_is_normalised_across_languages(self):
        # transcript says "one hundred billion"; the digest wrote 1000 亿
        self.assertTrue(gate._num_in("1000 亿美元", self.HAY))
        self.assertTrue(gate._num_in("2 billion 估值", self.HAY))

    def test_digits_still_match_digits(self):
        self.assertTrue(gate._num_in("200 million 融资", self.HAY))

    def test_absent_numbers_are_rejected(self):
        for v in ["估值 2 万亿美元", "准确率 87%", "共 45 万用户"]:
            self.assertFalse(gate._num_in(v, self.HAY), v)

    # 下面这些字符串是 Odd Lots《Nigerian Industrial Behemoth》逐字稿的原句。
    # 这一集的四条数字曾被全部误删——口语里数字是念出来的，不是写出来的。
    SPOKEN = ("africa as a continent now has one point five billion people. that's about "
              "seven times more than after the second world war when africa had two hundred "
              "and twenty million people. nigeria is about two hundred and fifty people per "
              "square kilometer. china put twelve and a half billion dollars into "
              "manufacturing in africa. india's grown at four point two percent year "
              "average. an economy that grew for thirty years at ten percent a year.")

    def test_decimals_are_spoken_not_written(self):
        # "1.5" 是念成 "one point five" 的
        self.assertTrue(gate._num_in("15亿 vs 2.2亿", self.SPOKEN))
        self.assertTrue(gate._num_in("年均 4.2%", self.SPOKEN))

    def test_hundreds_take_an_and(self):
        # 只生成 "two hundred fifty" 会漏掉 "two hundred and fifty"
        self.assertTrue(gate._num_in("约 250 人/平方公里", self.SPOKEN))

    def test_fraction_idioms(self):
        # 125亿 念成 "twelve and a half billion"
        self.assertTrue(gate._num_in("125 亿美元", self.SPOKEN))

    def test_float_noise_does_not_break_integer_scaling(self):
        # 2.2 * 1e8 / 1e6 == 220.00000000000003，精确比较会把整数误判成小数，
        # 于是 "two hundred and twenty million" 永远匹配不上
        self.assertTrue(gate._num_in("2.2 亿人", self.SPOKEN))

    def test_a_year_the_transcript_never_states_is_still_rejected(self):
        # 逐字稿只说 "since india's reforms began"，没说 1991——
        # 那是模型的外部知识，该删
        self.assertFalse(gate._num_in("印度 1991 年以来年均 4.2%", self.SPOKEN))

    def test_qualitative_facts_need_no_number(self):
        self.assertTrue(gate._num_in("没有任何数字的定性判断", self.HAY))


class Quotes(unittest.TestCase):
    HAY = gate._norm("The models we're trying to create are models that are as dumb "
                     "as I am. The mistakes that I would make, the model has to make "
                     "the same mistakes.")

    def test_verbatim_quote_passes(self):
        self.assertTrue(gate._quote_in(
            "The models we're trying to create are models that are as dumb as I am.", self.HAY))

    def test_smart_quotes_and_case_do_not_matter(self):
        self.assertTrue(gate._quote_in(
            "the models we’re trying to create are models that are as dumb as I am",
            self.HAY))

    def test_elision_is_allowed_when_each_kept_run_is_present(self):
        self.assertTrue(gate._quote_in(
            "The models we're trying to create … the model has to make the same mistakes.",
            self.HAY))

    def test_invented_quote_is_rejected(self):
        self.assertFalse(gate._quote_in(
            "So market research is a hundred billion dollar industry.", self.HAY))

    def test_partially_invented_quote_is_rejected(self):
        self.assertFalse(gate._quote_in(
            "The models we're trying to create are models that reason better than humans.",
            self.HAY))


class Captions(unittest.TestCase):
    VTT = """WEBVTT

00:00:01.000 --> 00:00:03.000 align:start position:0%
we need to talk about

00:00:03.000 --> 00:00:05.000 align:start position:0%
we need to talk about the cost of compute

00:00:05.000 --> 00:00:07.000 align:start position:0%
the cost of compute because it keeps rising
"""

    def test_rolling_captions_are_not_doubled(self):
        segs = T._vtt_srt(self.VTT)
        text = " ".join(s["text"] for s in segs)
        self.assertEqual(text, "we need to talk about the cost of compute "
                               "because it keeps rising")

    def test_cue_settings_are_stripped(self):
        self.assertNotIn("align:start", " ".join(s["text"] for s in T._vtt_srt(self.VTT)))

    def test_timestamps_are_seconds(self):
        self.assertEqual([s["t"] for s in T._vtt_srt(self.VTT)], [1, 3, 5])

    def test_srt_comma_decimals(self):
        srt = "1\n00:01:02,500 --> 00:01:04,000\nhello there\n"
        self.assertEqual(T._vtt_srt(srt), [{"t": 62, "text": "hello there"}])


class NotesTranscripts(unittest.TestCase):
    def test_speaker_prefixed_lines_carry_attribution(self):
        body = "\n".join(f"Joon [00:0{i}:00]: sentence number {i} with enough words here"
                        for i in range(1, 8))
        segs = T._timestamped_notes(body)
        self.assertEqual(len(segs), 7)
        self.assertEqual(segs[0]["spk"], "Joon")
        self.assertEqual(segs[2]["t"], 180)

    def test_a_chapter_list_is_not_mistaken_for_a_transcript(self):
        # Timestamped lines that cover a tiny fraction of a long document are a
        # chapter list; treating them as the transcript yielded 105 "words".
        body = ("00:01 Intro\n02:00 The thesis\n05:00 Data\n09:00 Scaling\n"
                "14:00 Objections\n20:00 Wrap\n\n" + ("filler prose. " * 900))
        self.assertEqual(T._timestamped_notes(body), [])

    def test_chapters_are_extracted_and_sorted(self):
        ep = {"notes": "05:00 Later thing\n01:30 Earlier thing\n09:00 Last thing",
              "duration": 1200}
        ch = T.chapters(ep)
        self.assertEqual([c["t"] for c in ch], [90, 300, 540])

    def test_chapters_beyond_the_duration_are_dropped(self):
        ep = {"notes": "01:00 a\n02:00 b\n03:00 c\n99:00 bogus", "duration": 300}
        self.assertNotIn(5940, [c["t"] for c in T.chapters(ep)])


class Belonging(unittest.TestCase):
    """文稿到手后要验证它属于这一集。两个真实事故各占一个方向。"""

    def test_wrong_episode_is_caught(self):
        # 真实事故：YouTube 匹配把 Bezos 访谈配给了 Whatnot CPO 那一集，
        # 整篇深读基于错的内容写成、署着错的嘉宾发出去了
        self.assertFalse(T.belongs_to(
            "the advantage of compromise as a resolution mechanism is that it's low energy",
            "This CPO regrets that product management exists | Tom Verrilli (CPO of Whatnot)"))

    def test_chinese_title_is_not_falsely_rejected(self):
        # 真实事故：中文标题被贪婪匹配切成整句（「这是你该知道的一切」），
        # 逐字稿里不会有这种标题式说法，于是一份正确的 80 分钟转写被误杀
        self.assertTrue(T.belongs_to(
            "今天我们请到了理想汽车的CTO谢炎，聊理想为什么要自己造芯片",
            "关于理想造芯，这是你该知道的一切：对话理想汽车CTO谢炎"))

    def test_latin_tokens_carry_mixed_titles(self):
        # 中英混排的技术标题，真信号在拉丁词上
        self.assertTrue(T.belongs_to(
            "欢迎收听硅谷101，今天聊 OpenClaw 和 Hermes，还有 Token 经济的转点",
            "E249｜Token经济转点：OpenClaw、Hermes到本地自研的Agent进化之路"))

    def test_unverifiable_title_makes_no_judgement(self):
        self.assertIsNone(T.belongs_to("some transcript text here", "第五期"))


class Dedupe(unittest.TestCase):
    def test_same_episode_from_two_sources_shares_a_fingerprint(self):
        # Identical wording, different punctuation and a few seconds of intro:
        # this is what the same episode looks like in RSS vs on YouTube.
        rss = "Simulation: the new Scaling Law — Joon Sung Park, Simile AI"
        yt = "Simulation: The New Scaling Law - Joon Sung Park, Simile AI"
        self.assertEqual(fingerprint(rss, 4178), fingerprint(yt, 4183))

    def test_a_differing_trailing_show_name_is_a_known_miss(self):
        # Documented limitation: when one side appends the show name the
        # fingerprints diverge, so the episode may publish twice. Merging on
        # partial titles instead would risk dropping distinct episodes, which
        # is the worse failure.
        self.assertNotEqual(fingerprint("Great Episode", 3600),
                            fingerprint("Great Episode | The Show", 3600))

    def test_episode_numbering_does_not_split_the_fingerprint(self):
        self.assertEqual(norm_title("#430 How to Write Ads That Sell"),
                         norm_title("430 - How to Write Ads That Sell"))

    def test_different_episodes_do_not_collide(self):
        self.assertNotEqual(fingerprint("Nick Bostrom on utopia", 3300),
                            fingerprint("Jasmine Sun on the backlash", 3104))

    def test_a_long_duration_gap_separates_a_rerun(self):
        self.assertNotEqual(fingerprint("Same Title Here", 600),
                            fingerprint("Same Title Here", 3600))


class TimeParsing(unittest.TestCase):
    def test_duration_formats(self):
        self.assertEqual(parse_duration("3104"), 3104)
        self.assertEqual(parse_duration("51:44"), 3104)
        self.assertEqual(parse_duration("1:09:38"), 4178)
        self.assertIsNone(parse_duration(""))
        self.assertIsNone(parse_duration("nonsense"))

    def test_timestamp_formats(self):
        self.assertEqual(parse_ts("1:23"), 83)
        self.assertEqual(parse_ts("01:02:03"), 3723)
        self.assertEqual(parse_ts("1h2m3s"), 3723)
        self.assertEqual(parse_ts(90), 90)

    def test_hhmmss_roundtrip(self):
        for s in (0, 59, 60, 3599, 3600, 4178):
            self.assertEqual(parse_ts(hhmmss(s)), s)


class GateOutcomes(unittest.TestCase):
    def _tr(self):
        return {"segments": [{"t": 0, "text": "we are building models that are as dumb as I am "
                                              "and the revenue reached one hundred billion "
                                              "dollars this year"}],
                "source": "feed", "detail": "text/vtt", "words": 20, "wpm": 150, "timed": True}

    def _digest(self, **over):
        d = {"title": "一个足够长的中文标题", "dek": "这是一句足够长的中文导语，用来通过长度检查。",
             "why": "值得听", "who": "谁该听", "skip": "",
             "points": [{"t": 10 * i, "h": f"小标题{i}",
                         "body": "这是一段长度接近真实产出的正文，说明论证链条而不只是给结论。"
                                 "质检要求正文至少六十个字符，因为更短的段落通常只是把小标题"
                                 "换了个说法，读者拿不到任何新增信息。", "spk": ""}
                        for i in range(6)],
             "quotes": [{"t": 0, "spk": "A", "raw": "models that are as dumb as I am", "zh": "译"},
                        {"t": 0, "spk": "A", "raw": "the revenue reached one hundred billion dollars",
                         "zh": "译"}],
             "facts": [{"k": "收入", "v": "1000 亿美元", "t": 0}],
             "terms": [], "tags": ["a"]}
        d.update(over)
        return d

    def test_a_clean_digest_passes(self):
        ok, probs, out = gate.check(self._digest(), self._tr(), {"duration": 600})
        self.assertTrue(ok, probs)
        self.assertEqual(out["quality"]["verified_quotes"], 2)
        self.assertEqual(out["quality"]["grounded_facts"], 1)

    def test_pipeline_leakage_rejects_the_whole_episode(self):
        d = self._digest(dek="由于本机 yt-dlp 字幕脚本环境异常，本篇根据公开信息整理。")
        ok, probs, _ = gate.check(d, self._tr(), {"duration": 600})
        self.assertFalse(ok)
        self.assertIn("leakage", probs[0])

    def test_invented_quotes_are_pruned_and_may_fail_the_episode(self):
        d = self._digest(quotes=[{"t": 0, "spk": "A", "raw": "this sentence was never said aloud",
                                  "zh": "译"}])
        ok, probs, out = gate.check(d, self._tr(), {"duration": 600})
        self.assertEqual(out["quotes"], [])
        self.assertFalse(ok)
        self.assertTrue(any("verifiable quotes" in p for p in probs))

    def test_timestamps_past_the_end_are_dropped(self):
        pts = self._digest()["points"]
        pts[0] = dict(pts[0], t=99999)
        ok, probs, out = gate.check(self._digest(points=pts), self._tr(), {"duration": 600})
        self.assertEqual(len(out["points"]), 5)
        self.assertTrue(any("out of range" in p for p in probs))

    def test_an_english_only_title_is_rejected(self):
        ok, probs, _ = gate.check(self._digest(title="A Perfectly Fine English Title"),
                                  self._tr(), {"duration": 600})
        self.assertFalse(ok)

    def test_too_few_points_is_rejected(self):
        d = self._digest()
        ok, probs, _ = gate.check(self._digest(points=d["points"][:3]), self._tr(),
                                  {"duration": 600})
        self.assertFalse(ok)


class Anchoring(unittest.TestCase):
    """A "deep read" whose points all sit at one timestamp is a paraphrase of one
    passage. This is how a 185-word clip got a six-point page."""

    def _tr(self):
        return {"segments": [{"t": 0, "text": "the incentive structure is what produces "
                                             "the cheating, not the model's character"}],
                "source": "youtube", "detail": "en", "words": 13, "wpm": 13, "timed": True}

    def _digest(self, stamps):
        return {"title": "一个足够长的中文标题", "dek": "这是一句足够长的中文导语，用来通过长度检查。",
                "why": "", "who": "", "skip": "",
                "points": [{"t": t, "h": f"小标题{i}",
                            "body": "这是一段长度接近真实产出的正文，说明论证链条而不只是给结论。"
                                    "质检要求正文至少六十个字符，因为更短的段落通常只是把小标题"
                                    "换了个说法，读者拿不到任何新增信息。", "spk": ""}
                           for i, t in enumerate(stamps)],
                "quotes": [{"t": 0, "spk": "A",
                            "raw": "the incentive structure is what produces the cheating",
                            "zh": "译"},
                           {"t": 0, "spk": "A",
                            "raw": "not the model's character", "zh": "译"}],
                "facts": [], "terms": [], "tags": []}

    def test_all_points_at_the_same_timestamp_is_rejected(self):
        ok, probs, _ = gate.check(self._digest([0] * 6), self._tr(), {"duration": 60})
        self.assertFalse(ok)
        self.assertTrue(any("not anchored" in p for p in probs), probs)

    def test_points_crammed_into_one_stretch_of_a_long_episode_is_rejected(self):
        ok, probs, _ = gate.check(self._digest([10, 20, 30, 40, 50, 60]),
                                  self._tr(), {"duration": 3600})
        self.assertFalse(ok)
        self.assertTrue(any("one passage" in p for p in probs), probs)

    def test_an_untimed_transcript_is_exempt_from_the_spread_rule(self):
        # A newsletter read aloud has no cue timings; its anchors are estimated
        # from position in the text. Demanding spread would reject it for a
        # property the source cannot provide.
        tr = dict(self._tr(), timed=False)
        ok, probs, out = gate.check(self._digest([0] * 6), tr, {"duration": 900})
        self.assertTrue(ok, probs)
        self.assertTrue(out["quality"]["approx_timestamps"])

    def test_a_timed_transcript_records_exact_timestamps(self):
        ok, _, out = gate.check(self._digest([60, 600, 1200, 1800, 2400, 3000]),
                                self._tr(), {"duration": 3600})
        self.assertTrue(ok)
        self.assertFalse(out["quality"]["approx_timestamps"])

    def test_points_spread_across_a_long_episode_pass(self):
        ok, probs, _ = gate.check(self._digest([60, 600, 1200, 1800, 2400, 3000]),
                                  self._tr(), {"duration": 3600})
        self.assertTrue(ok, probs)


class Gates(unittest.TestCase):
    """闸门失灵时的方向：必须拦下，不能放行。"""

    def test_review_unavailable_does_not_pass(self):
        # 原来是失灵即放行，等于评审一坏闸门就不存在了
        from lib import review
        self.assertFalse(review.passes(None))
        self.assertTrue(review.passes({"score": 7.0}))    # 及格线 7
        self.assertFalse(review.passes({"score": 6.0}))

    def test_triage_unavailable_does_pass(self):
        # 选题闸门方向相反：它只是省钱的预筛，失灵时放行让稿子走到成稿评分那道
        # 真闸门去，不该因为预筛坏了就整体空转
        from lib import triage
        self.assertTrue(triage.passes(None))
        self.assertFalse(triage.passes({"score": 6.0}))
        self.assertTrue(triage.passes({"score": 8.0}))

    def test_reasoning_models_are_detected(self):
        # 推理模型的思考 token 算进 max_tokens；不识别就会拿到空 content，
        # 而报错只会说"没返回 JSON"，查不到真因（实测 12/12 全挂）
        from lib import llm
        for m in ("deepseek-reasoner", "qwen3-235b-thinking", "o3", "glm-4.7-r1"):
            self.assertTrue(llm._REASONING.search(m), m)
        for m in ("deepseek-chat", "gpt-4o-mini", "claude-sonnet-5", "glm-4.7"):
            self.assertIsNone(llm._REASONING.search(m), m)

    def test_cli_backend_accepts_a_model(self):
        # 加分角色模型时签名替换的锚点没匹配上，静默失败；CI 走 API 路径所以
        # 只在本机炸，10 篇补评全废
        import inspect
        from lib import llm
        self.assertIn("model", inspect.signature(llm._cli).parameters)


class Titles(unittest.TestCase):
    def test_a_fully_quoted_title_is_unwrapped(self):
        self.assertEqual(_clean_title("「整个标题都被引号包住」"), "整个标题都被引号包住")
        self.assertEqual(_clean_title('"A fully quoted title"'), "A fully quoted title")

    def test_an_internal_quotation_is_left_alone(self):
        # Stripping the leading 「 here leaves a dangling 」 — worse than the
        # thing it was trying to fix.
        t = "「大脑是计算机」不是哲学立场，是绕开生物学的商业路线"
        self.assertEqual(_clean_title(t), t)

    def test_a_trailing_comma_is_removed(self):
        self.assertEqual(_clean_title("结尾多了个逗号，"), "结尾多了个逗号")

    def test_internal_punctuation_survives(self):
        t = "ChatGPT Work 与 Codex 是同一个 harness，差的只是 UX"
        self.assertEqual(_clean_title(t), t)

    def test_half_width_punctuation_between_cjk_is_widened(self):
        # 国产模型稳定地在中文里写半角逗号，中文站上很扎眼
        self.assertEqual(_cn_punct("非洲工业化不缺资源,缺的是人口密度"),
                         "非洲工业化不缺资源，缺的是人口密度")
        self.assertEqual(_cn_punct("第一点:成本下降了"), "第一点：成本下降了")
        self.assertEqual(_cn_punct("真的吗?我不信"), "真的吗？我不信")
        self.assertEqual(_cn_punct("这句话结束了."), "这句话结束了。")

    def test_numbers_and_latin_keep_half_width(self):
        for t in ("我们融了200,000美元", "用的是gpt-4.1模型",
                  "Anthropic 的 ARR 是 1.2 billion", "E249｜Token经济转点"):
            self.assertEqual(_cn_punct(t), t)

    def test_verbatim_quote_is_never_rewritten(self):
        # quotes.raw 要逐字校验，改一个字符就通不过
        out = normalize({"title": "标题",
                         "quotes": [{"t": "0:10", "spk": "A",
                                     "raw": "他说,这是原话.", "zh": "译文,如此."}]})
        self.assertEqual(out["quotes"][0]["raw"], "他说,这是原话.")
        self.assertEqual(out["quotes"][0]["zh"], "译文，如此。")

    def test_normalize_drops_malformed_rows(self):
        out = normalize({"title": "标题", "points": ["not a dict", {"h": "有内容"}],
                         "tags": ["a", "", "b"], "quotes": None})
        self.assertEqual(len(out["points"]), 1)
        self.assertEqual(out["tags"], ["a", "b"])
        self.assertEqual(out["quotes"], [])


class Density(unittest.TestCase):
    def test_word_counting_handles_chinese(self):
        self.assertEqual(T._count("hello world", "en"), 2)
        self.assertGreater(T._count("这是一段中文文本用来测试计数", "zh"), 10)

    def test_there_is_an_absolute_word_floor(self):
        # The ratio check alone is useless when the feed omits duration: dur_min
        # falls back to 1 minute and a 185-word clip reads as "185 wpm".
        self.assertGreaterEqual(T.MIN_WORDS["en"], 1000)
        self.assertGreaterEqual(T.MIN_WORDS["zh"], 1500)

    def test_the_wpm_window_brackets_real_speech(self):
        self.assertLess(T.MIN_WPM["en"], 130)     # real English conversation
        self.assertGreater(T.MAX_WPM["en"], 200)  # fast talkers still fit
        self.assertLess(T.MAX_WPM["en"], 400)     # but a wrong document does not


if __name__ == "__main__":
    unittest.main(verbosity=2)
