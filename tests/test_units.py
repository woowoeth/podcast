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
