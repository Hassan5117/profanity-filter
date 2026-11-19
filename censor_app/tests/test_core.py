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

    def test_encoding_cp1252_and_utf16(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cp1252_path = os.path.join(tmpdir, "cp1252.srt")
            cp1252_bytes = "1\r\n00:00:01,000 --> 00:00:02,000\r\n\x93Holy shit!\x94\r\n".encode("latin-1")
            with open(cp1252_path, "wb") as f:
                f.write(cp1252_bytes)
            items_cp = parse_srt(cp1252_path)
            self.assertEqual(len(items_cp), 1)
            self.assertEqual(items_cp[0].clean_text, "“Holy shit!”")

            utf16_path = os.path.join(tmpdir, "utf16.srt")
            with open(utf16_path, "w", encoding="utf-16") as f:
                f.write("1\n00:00:01,000 --> 00:00:02,000\nWhat the fuck!\n")
            items_u16 = parse_srt(utf16_path)
            self.assertEqual(len(items_u16), 1)
            self.assertEqual(items_u16[0].clean_text, "What the fuck!")

    def test_whitespace_blank_lines_and_midword_tags(self):
        sample = (
            "1\n00:00:01,000 --> 00:00:02,000\n"
            "This is un<i>fucking</i>believable!\n"
            "   \n"
            "2\n00:00:03,000 --> 00:00:04,000\n"
            "Second cue.\n"
        )
        items = parse_srt_content(sample)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].clean_text, "This is unfuckingbelievable!")
        self.assertEqual(items[1].clean_text, "Second cue.")


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

    def test_masked_patterns_false_positives_and_full_tokens(self):
        pf = ProfanityFilter(preset="mild")
        benign = "John F. Kennedy met F. Scott Fitzgerald. Plan B... wait, a-- what? Exhibit A..."
        self.assertEqual(pf.find_matches(benign), [])

        text = (
            "What the f***, f**k, f*ck, f*cking, sh*t, s**t, "
            "b****, b*tch, a**hole, a$$hole, a$$, d*ck, c*nt, g*ddamn, d*mn, fuckin'!"
        )
        matches = pf.find_matches(text)
        found = [(m.word, m.matched_text) for m in matches]
        for expected in [
            ("fuck", "f***"),
            ("fuck", "f**k"),
            ("fuck", "f*ck"),
            ("fuck", "f*cking"),
            ("shit", "sh*t"),
            ("shit", "s**t"),
            ("bitch", "b****"),
            ("bitch", "b*tch"),
            ("asshole", "a**hole"),
            ("asshole", "a$$hole"),
            ("ass", "a$$"),
            ("dick", "d*ck"),
            ("cunt", "c*nt"),
            ("goddamn", "g*ddamn"),
            ("damn", "d*mn"),
            ("fuckin'", "fuckin'"),
        ]:
            self.assertIn(expected, found)

    def test_custom_preset_ignores_builtin_and_masked(self):
        pf = ProfanityFilter(preset="custom", custom_words=["darn"])
        matches = pf.find_matches("What the fuck f*** darn!")
        self.assertEqual([m.word for m in matches], ["darn"])


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

    def test_tightened_four_to_six_word_cues(self):
        pf = ProfanityFilter(preset="mild")
        cases = [
            ("Leave me alone, asshole.", 10.0, 12.2, "end"),
            ("Shit, they are right behind us!", 10.0, 12.2, "start"),
            ("Get out of my fucking house!", 10.0, 12.2, "middle"),
            ("- Are you ready? - Hell no, wait!", 10.0, 12.4, "middle"),
        ]
        for idx, (text, t0, t1, pos) in enumerate(cases, start=1):
            sub = SubtitleItem(index=idx, start_time=t0, end_time=t1, raw_text=text, clean_text=text)
            matches = pf.find_matches(text)
            self.assertEqual(len(matches), 1, f"Expected 1 match in {text!r}")
            s, e = calculate_word_interval(sub, matches[0], padding_before=0.18, padding_after=0.16, mode="word", lead_in=0.10)
            in_cue_ratio = (min(e, t1) - max(s, t0)) / (t1 - t0)
            self.assertLess(in_cue_ratio, 0.50, f"Cue over-muted ({in_cue_ratio:.2f}) for {text!r}: [{s:.2f}..{e:.2f}]")
            if pos == "start":
                self.assertLess(s, t0 + 0.1)
                self.assertLess(e, t0 + 0.9)
            elif pos == "end":
                self.assertGreater(s, t0 + 1.0)
            else:
                self.assertGreater(s, t0 + 0.7)
                self.assertLess(e, t1)

    def test_clause_pause_clamping_and_speaker_sdh_handling(self):
        pf = ProfanityFilter(preset="mild")
        sdh_text = "WALTER: [sighs] Get that shit out of here!"
        sub_sdh = SubtitleItem(index=1, start_time=10.0, end_time=13.0, raw_text=sdh_text, clean_text=sdh_text)
        m_sdh = pf.find_matches(sdh_text)[0]
        s1, e1 = calculate_word_interval(sub_sdh, m_sdh)
        # WALTER: and [sighs] should not make 'shit' look late; nor should leading pause shift it earlier than [10.45..12.1]
        self.assertGreaterEqual(s1, 10.45)
        self.assertLessEqual(e1, 12.15)
        self.assertLess(e1 - s1, 1.0)

        clause_text = "Listen carefully, asshole -- run!"
        sub_clause = SubtitleItem(index=2, start_time=10.0, end_time=13.0, raw_text=clause_text, clean_text=clause_text)
        m_clause = pf.find_matches(clause_text)[0]
        s2, e2 = calculate_word_interval(sub_clause, m_clause)
        # Comma before 'asshole' and dash after should keep 'Listen carefully' and 'run' clean
        self.assertGreaterEqual(s2, 11.15)
        self.assertLessEqual(e2, 12.50)


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

    def _measure_mean_volume(self, media_path: str, start_s: float, end_s: float) -> float:
        import re
        res = subprocess.run([
            "ffmpeg", "-hide_banner", "-i", media_path,
            "-af", f"atrim={start_s}:{end_s},volumedetect",
            "-f", "null", "-"
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        match = re.search(r"mean_volume:\s*([-\d\.]+)\s*dB", res.stderr)
        self.assertIsNotNone(match, f"volumedetect failed: {res.stderr}")
        return float(match.group(1))

    def test_bleep_multichunk_and_volume_preservation(self):
        sine_vid = os.path.join(self.test_dir, "sine10s.mp4")
        silent_vid = os.path.join(self.test_dir, "silent10s.mp4")
        sine_out = os.path.join(self.test_dir, "sine10s_bleep.mp4")
        silent_out = os.path.join(self.test_dir, "silent10s_bleep.mp4")

        for vid, audio_src in [
            (sine_vid, "sine=frequency=440:sample_rate=48000:duration=10"),
            (silent_vid, "anullsrc=r=48000:cl=stereo:d=10"),
        ]:
            subprocess.run([
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "testsrc=duration=10:size=160x120:rate=15",
                "-f", "lavfi", "-i", audio_src,
                "-c:v", "libx264", "-c:a", "aac", vid
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        # 60 intervals > CHUNK_SIZE (25) spanning 0.1s..6.05s
        intervals = [(round(0.1 + i * 0.1, 3), round(0.15 + i * 0.1, 3)) for i in range(60)]
        engine = FFmpegEngine()

        self.assertTrue(engine.run_censor(sine_vid, sine_out, intervals, mode="bleep"))
        orig_db = self._measure_mean_volume(sine_vid, 7.0, 9.0)
        bleep_unmuted_db = self._measure_mean_volume(sine_out, 7.0, 9.0)
        self.assertLess(abs(orig_db - bleep_unmuted_db), 1.0)

        self.assertTrue(engine.run_censor(silent_vid, silent_out, intervals, mode="bleep"))
        outside_db = self._measure_mean_volume(silent_out, 7.0, 9.0)
        self.assertLess(outside_db, -70.0)

    def test_surround_51_center_channel_mute_and_bleep(self):
        vid_51 = os.path.join(self.test_dir, "surround51.mp4")
        out_mute = os.path.join(self.test_dir, "surround51_mute.mp4")
        out_bleep = os.path.join(self.test_dir, "surround51_bleep.mp4")

        subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=15",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=5.1:d=2",
            "-c:v", "libx264", "-c:a", "eac3", "-b:a", "384k", vid_51
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        engine = FFmpegEngine()
        self.assertTrue(engine.run_censor(vid_51, out_mute, [(0.5, 1.2)], mode="mute", center_channel_only=True))
        self.assertTrue(engine.run_censor(vid_51, out_bleep, [(0.5, 1.2)], mode="bleep", center_channel_only=True))

        info = FFmpegProbe.probe_media(out_bleep)
        self.assertTrue(info["is_surround"])
        self.assertEqual(info["audio_channels"], 6)

    def test_webm_censor(self):
        in_webm = os.path.join(self.test_dir, "clip.webm")
        out_webm = os.path.join(self.test_dir, "clip.Cleaned.webm")
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=15",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
            "-c:v", "libvpx", "-c:a", "libopus", in_webm
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        engine = FFmpegEngine()
        self.assertTrue(engine.run_censor(in_webm, out_webm, [(0.5, 1.2)], mode="mute"))
        self.assertTrue(os.path.exists(out_webm))
        self.assertGreater(os.path.getsize(out_webm), 0)

    def test_reject_same_file_output(self):
        orig_size = os.path.getsize(self.video_path)
        engine = FFmpegEngine()
        success = engine.run_censor(self.video_path, self.video_path, [(0.5, 1.0)], mode="mute")
        self.assertFalse(success)
        self.assertTrue(bool(engine.last_error))
        self.assertEqual(os.path.getsize(self.video_path), orig_size)

    def test_audio_waveform_refinement_trims_hold_and_snaps_valleys(self):
        speech_vid = os.path.join(self.test_dir, "dialogue_hold.mp4")
        # 5s video: subtitle [1.00..4.00] has 0.25s pre-silence + 0.85s trailing hold silence
        # Word bursts: "Get" [1.25..1.48], "out" [1.55..1.76], "of" [1.82..1.96], "my" [2.02..2.18],
        # "fucking" [2.28..2.72], "house!" [2.82..3.15]
        bursts = [
            (1.25, 1.48), (1.55, 1.76), (1.82, 1.96),
            (2.02, 2.18), (2.28, 2.72), (2.82, 3.15)
        ]
        expr = "+".join(f"between(t,{s},{e})" for s, e in bursts)
        subprocess.run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=5:size=160x120:rate=15",
            "-f", "lavfi", "-i", "sine=frequency=450:sample_rate=48000:duration=5",
            "-af", f"volume='if({expr},0.9,0.005)':eval=frame",
            "-c:v", "libx264", "-c:a", "aac", speech_vid
        ], check=True)

        pf = ProfanityFilter(preset="moderate")
        subs = [SubtitleItem(
            index=1, start_time=1.0, end_time=4.0,
            raw_text="Get out of my fucking house!",
            clean_text="Get out of my fucking house!"
        )]
        unrefined = detect_profanities_in_subtitles(subs, pf, video_path=speech_vid, audio_refine=False)
        refined = detect_profanities_in_subtitles(subs, pf, video_path=speech_vid, audio_refine=True)
        self.assertEqual(len(refined), 1)
        self.assertEqual(len(unrefined), 1)

        r = refined[0]
        u = unrefined[0]
        # Refined brackets [2.28..2.72] tightly, preserving preceding clean words and ending near pause
        self.assertGreaterEqual(r.mute_start, 2.05)
        self.assertLessEqual(r.mute_start, 2.30)
        self.assertGreaterEqual(r.mute_end, 2.70)
        self.assertLessEqual(r.mute_end, 2.85)
        # Trimming 0.85s trailing hold shifts detection earlier toward true speech than unrefined
        self.assertLess(r.mute_end, u.mute_end)

    def test_audio_waveform_abstains_on_flat_tone(self):
        flat_vid = os.path.join(self.test_dir, "flat5s.mp4")
        subprocess.run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=5:size=160x120:rate=15",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=5",
            "-c:v", "libx264", "-c:a", "aac", flat_vid
        ], check=True)

        pf = ProfanityFilter(preset="moderate")
        subs = [SubtitleItem(
            index=1, start_time=1.0, end_time=4.0,
            raw_text="Get out of my fucking house!",
            clean_text="Get out of my fucking house!"
        )]
        unrefined = detect_profanities_in_subtitles(subs, pf, video_path=flat_vid, audio_refine=False)
        refined = detect_profanities_in_subtitles(subs, pf, video_path=flat_vid, audio_refine=True)
        self.assertAlmostEqual(refined[0].mute_start, unrefined[0].mute_start, places=4)
        self.assertAlmostEqual(refined[0].mute_end, unrefined[0].mute_end, places=4)


if __name__ == "__main__":
    unittest.main()
