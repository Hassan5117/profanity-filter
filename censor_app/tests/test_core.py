import unittest
import tempfile
import os
import subprocess
from censor_app.core.srt_parser import (
    parse_timestamp, format_timestamp, strip_subtitle_tags, parse_srt_content,
    parse_srt
)
from censor_app.core.profanity_filter import ProfanityFilter, WHITELIST_WORDS
from censor_app.core.timing import (
    calculate_word_interval, merge_intervals, detect_profanities_in_subtitles,
    SubtitleItem, ProfanityMatch
)
from censor_app.core.subtitle_writer import generate_cleaned_srt
from censor_app.core.ffmpeg_engine import FFmpegProbe, FFmpegEngine, AudioFilterBuilder


class TestSRTParser(unittest.TestCase):
    def test_parse_timestamp(self):
        self.assertAlmostEqual(parse_timestamp("00:01:23,456"), 83.456)
        self.assertAlmostEqual(parse_timestamp("01:00:00.500"), 3600.500)
        self.assertAlmostEqual(parse_timestamp("02:15,100"), 135.100)

    def test_format_timestamp(self):
        self.assertEqual(format_timestamp(83.456, ','), "00:01:23,456")
        self.assertEqual(format_timestamp(3661.050, '.'), "01:01:01.050")

    def test_strip_tags(self):
        raw = "<i>Hello</i> <b>world</b>! <font color='red'>Don't touch that.</font>{\\an8}&quot;Quotes&quot;"
        clean = strip_subtitle_tags(raw)
        self.assertEqual(clean, "Hello world! Don't touch that. \"Quotes\"")

    def test_parse_srt_content(self):
        sample = """1
00:00:01,000 --> 00:00:04,500
Look at this <i>fucking</i> mess!

2
00:00:05,200 --> 00:00:08,000
Holy shit, we are in trouble.
"""
        items = parse_srt_content(sample)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].index, 1)
        self.assertAlmostEqual(items[0].start_time, 1.0)
        self.assertAlmostEqual(items[0].end_time, 4.5)
        self.assertEqual(items[0].clean_text, "Look at this fucking mess!")
        self.assertEqual(items[1].clean_text, "Holy shit, we are in trouble.")


class TestProfanityFilter(unittest.TestCase):
    def test_detection_and_tiers(self):
        pf = ProfanityFilter(preset="moderate")
        matches = pf.find_matches("This is fucking bullshit!")
        words = [m.word for m in matches]
        self.assertIn("fucking", words)
        self.assertIn("bullshit", words)

    def test_masked_and_censored_words(self):
        pf = ProfanityFilter(preset="moderate")
        matches = pf.find_matches("What the f*** is that sh*t, you b****?")
        words = [m.word for m in matches]
        self.assertIn("fuck", words)
        self.assertIn("shit", words)
        self.assertIn("bitch", words)

    def test_whitelist_and_false_positives(self):
        pf = ProfanityFilter(preset="mild")
        text = "The assassin sat in the cockpit drinking a cocktail with class."
        matches = pf.find_matches(text)
        self.assertEqual(len(matches), 0, f"False positives detected: {[m.word for m in matches]}")

    def test_sanitize_text(self):
        pf = ProfanityFilter(preset="moderate")
        sanitized = pf.sanitize_text("Holy shit, that was fucking insane!")
        self.assertIn("H********", sanitized)
        self.assertIn("f******", sanitized)
        self.assertNotIn("shit", sanitized)
        self.assertNotIn("fucking", sanitized)


class TestTiming(unittest.TestCase):
    def test_word_interpolation_long_sentence(self):
        # Sentence with > 5 words and duration > 2.5s
        text = "I told you that this was a completely fucking terrible and broken idea from the start"
        sub = SubtitleItem(
            index=1,
            start_time=10.0,
            end_time=20.0,
            raw_text=text,
            clean_text=text
        )
        match = ProfanityMatch(
            word="fucking",
            matched_text="fucking",
            start_char=text.index("fucking"),
            end_char=text.index("fucking") + len("fucking"),
            tier="strict"
        )
        start, end = calculate_word_interval(sub, match, padding_before=0.3, padding_after=0.3, mode="word", lead_in=0.2)
        # Verify mute starts early enough to prevent cutting in half
        self.assertTrue(13.5 <= start <= 15.0)
        self.assertTrue(15.0 <= end <= 17.5)

    def test_short_segment_auto_protection(self):
        # Short punchy line: <= 5 words
        text = "What the fuck!"
        sub = SubtitleItem(
            index=1,
            start_time=5.0,
            end_time=7.0,
            raw_text=text,
            clean_text=text
        )
        match = ProfanityMatch(
            word="fuck",
            matched_text="fuck",
            start_char=9,
            end_char=13,
            tier="strict"
        )
        # Should auto-mute full segment + padding
        start, end = calculate_word_interval(sub, match, padding_before=0.2, padding_after=0.2, mode="word")
        self.assertAlmostEqual(start, 4.8)
        self.assertAlmostEqual(end, 7.2)

    def test_sync_offset(self):
        text = "Holy shit!"
        sub = SubtitleItem(
            index=1,
            start_time=10.0,
            end_time=12.0,
            raw_text=text,
            clean_text=text
        )
        match = ProfanityMatch(
            word="shit",
            matched_text="shit",
            start_char=5,
            end_char=9,
            tier="moderate"
        )
        # Shift -0.5s earlier
        start, end = calculate_word_interval(sub, match, padding_before=0.2, padding_after=0.2, sync_offset=-0.5)
        self.assertAlmostEqual(start, 9.3)
        self.assertAlmostEqual(end, 11.7)

    def test_merge_intervals(self):
        intervals = [(1.0, 2.0), (2.1, 3.5), (5.0, 6.0)]
        merged = merge_intervals(intervals, gap_threshold=0.25)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0], (1.0, 3.5))
        self.assertEqual(merged[1], (5.0, 6.0))


class TestFFmpegPipeline(unittest.TestCase):
    def setUp(self):
        # Create a small 3-second synthetic video with 440Hz sine audio
        self.test_dir = tempfile.mkdtemp()
        self.video_path = os.path.join(self.test_dir, "synth.mp4")
        self.srt_path = os.path.join(self.test_dir, "synth.srt")
        self.out_video_path = os.path.join(self.test_dir, "synth_out.mp4")

        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=3:size=160x120:rate=15",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-c:v", "libx264", "-c:a", "aac",
            self.video_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        srt_content = """1
00:00:01,000 --> 00:00:02,000
This is a fucking test!
"""
        with open(self.srt_path, "w", encoding="utf-8") as f:
            f.write(srt_content)

    def tearDown(self):
        import shutil
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def test_e2e_censor(self):
        pf = ProfanityFilter(preset="moderate")
        subs = parse_srt(self.srt_path)
        detections = detect_profanities_in_subtitles(subs, pf, timing_mode="word")
        self.assertEqual(len(detections), 1)

        intervals = [(d.mute_start, d.mute_end) for d in detections if d.enabled]
        merged = merge_intervals(intervals)

        engine = FFmpegEngine()
        success = engine.run_censor(
            input_video=self.video_path,
            output_video=self.out_video_path,
            intervals=merged,
            mode="mute"
        )
        self.assertTrue(success)
        self.assertTrue(os.path.exists(self.out_video_path))
        self.assertGreater(os.path.getsize(self.out_video_path), 0)


if __name__ == "__main__":
    unittest.main()
