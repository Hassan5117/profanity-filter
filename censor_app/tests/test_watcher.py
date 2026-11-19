import unittest
import tempfile
import os
import sys
import shutil
import subprocess
from pathlib import Path
from censor_app.server.watcher import MediaWatcher, VIDEO_EXTENSIONS, INCOMPLETE_EXTENSIONS
from censor_app.core.config import Config
from censor_app.core.subtitle_fetcher import SubtitleFetcher


class TestWatcher(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.watch_path = Path(self.test_dir)
        self.config = Config(custom_path=str(self.watch_path / "test_cfg.json"), load_env=False)
        self.config.set("watch_interval_seconds", 1)
        self.config.set("file_stability_wait_seconds", 0.1)

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def test_filter_incomplete_and_cleaned(self):
        # Create an incomplete file
        part_file = self.watch_path / "movie.mp4.part"
        part_file.write_text("dummy")

        cleaned_file = self.watch_path / "movie.Cleaned.mp4"
        cleaned_file.write_text("dummy")

        watcher = MediaWatcher(watch_dir=str(self.watch_path), config=self.config)
        self.assertFalse(watcher.process_single_video(cleaned_file))

    def test_find_matching_subtitles(self):
        vid = self.watch_path / "IronMan.mp4"
        vid.write_text("video")
        srt = self.watch_path / "IronMan.en.srt"
        srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello")

        watcher = MediaWatcher(watch_dir=str(self.watch_path), config=self.config)
        paired = watcher.find_matching_subtitles(vid)
        self.assertIsNotNone(paired)
        self.assertEqual(paired.name, "IronMan.en.srt")

    def test_watcher_e2e_alongside_creation(self):
        # Generate a 2s video
        vid_path = self.watch_path / "ShortMovie.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=15",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-c:a", "aac",
            str(vid_path)
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        srt_path = self.watch_path / "ShortMovie.srt"
        srt_path.write_text("1\n00:00:00,500 --> 00:00:01,500\nWhat a fucking joke!\n")

        watcher = MediaWatcher(watch_dir=str(self.watch_path), config=self.config)
        watcher.scan_and_process_once()

        expected_cleaned_vid = self.watch_path / "ShortMovie.Cleaned.mp4"
        expected_cleaned_srt = self.watch_path / "ShortMovie.Cleaned.srt"

        self.assertTrue(expected_cleaned_vid.exists(), "Cleaned video should exist alongside original")
        self.assertTrue(expected_cleaned_srt.exists(), "Cleaned SRT should exist alongside original")
        self.assertGreater(expected_cleaned_vid.stat().st_size, 0)

    def test_watcher_separate_output_dir(self):
        output_dir = Path(tempfile.mkdtemp())
        try:
            vid_path = self.watch_path / "Subfolder" / "SeparateMovie.mp4"
            vid_path.parent.mkdir(parents=True, exist_ok=True)
            cmd = [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=15",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                "-c:v", "libx264", "-c:a", "aac",
                str(vid_path)
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

            srt_path = vid_path.parent / "SeparateMovie.srt"
            srt_path.write_text("1\n00:00:00,500 --> 00:00:01,500\nWhat bullshit!\n")

            watcher = MediaWatcher(watch_dir=str(self.watch_path), output_dir=str(output_dir), config=self.config)
            watcher.scan_and_process_once()

            # Original remains untouched in watch directory
            self.assertTrue(vid_path.exists())
            self.assertTrue(srt_path.exists())
            self.assertFalse((vid_path.parent / "SeparateMovie.Cleaned.mp4").exists())

            # Output created in separate output directory (preserving relative structure)
            expected_cleaned_vid = output_dir / "Subfolder" / "SeparateMovie.Cleaned.mp4"
            expected_cleaned_srt = output_dir / "Subfolder" / "SeparateMovie.Cleaned.srt"
            self.assertTrue(expected_cleaned_vid.exists(), "Cleaned video should exist in separate output dir")
            self.assertTrue(expected_cleaned_srt.exists(), "Cleaned SRT should exist in separate output dir")
        finally:
            if output_dir.exists():
                shutil.rmtree(output_dir)

    def test_video_waits_for_delayed_subtitles(self):
        vid_path = self.watch_path / "DelayedMovie.mp4"
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=15",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-c:a", "aac",
            str(vid_path)
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        watcher = MediaWatcher(watch_dir=str(self.watch_path), config=self.config)
        watcher.scan_and_process_once()

        cleaned_vid = self.watch_path / "DelayedMovie.Cleaned.mp4"
        self.assertFalse(cleaned_vid.exists())
        self.assertNotIn(str(vid_path.resolve()), watcher.processed_files)

        srt_path = self.watch_path / "DelayedMovie.srt"
        srt_path.write_text("1\n00:00:00,500 --> 00:00:01,500\nThis is fucking wild!\n")

        watcher.scan_and_process_once()
        self.assertTrue(cleaned_vid.exists())
        self.assertTrue((self.watch_path / "DelayedMovie.Cleaned.srt").exists())
        self.assertIn(str(vid_path.resolve()), watcher.processed_files)

    def test_uncensored_vs_censored_naming(self):
        uncensored = self.watch_path / "Movie.Uncensored.mp4"
        censored = self.watch_path / "Movie.Censored.mp4"
        cleaned = self.watch_path / "Movie.Cleaned.mp4"

        subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=15",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-c:a", "aac",
            str(uncensored)
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        shutil.copy2(uncensored, censored)
        shutil.copy2(uncensored, cleaned)

        (self.watch_path / "Movie.Uncensored.srt").write_text("1\n00:00:00,500 --> 00:00:01,500\nFuck!\n")
        (self.watch_path / "Movie.Censored.srt").write_text("1\n00:00:00,500 --> 00:00:01,500\nFuck!\n")

        watcher = MediaWatcher(watch_dir=str(self.watch_path), config=self.config)
        self.assertFalse(watcher.is_already_cleaned(uncensored))
        self.assertTrue(watcher.is_already_cleaned(censored))
        self.assertTrue(watcher.is_already_cleaned(cleaned))

        watcher.scan_and_process_once()
        self.assertTrue((self.watch_path / "Movie.Uncensored.Cleaned.mp4").exists())
        self.assertFalse((self.watch_path / "Movie.Censored.Cleaned.mp4").exists())

    def test_postprocess_outside_repo_and_radarr_test(self):
        postprocess_script = Path(__file__).resolve().parents[1] / "server" / "postprocess.py"
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env["radarr_eventtype"] = "Test"
        res = subprocess.run(
            [sys.executable, str(postprocess_script)],
            cwd=self.test_dir,
            env=env,
            capture_output=True,
            text=True
        )
        self.assertEqual(res.returncode, 0, f"postprocess failed: {res.stderr}")
        self.assertIn("test event succeeded", res.stdout.lower())

    def test_watcher_auto_downloads_missing_subtitles_and_cooldown(self):
        vid_path = self.watch_path / "AutoMovie.2022.1080p.WEB.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=15",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=2",
            "-c:v", "libx264", "-c:a", "aac", str(vid_path)
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        cfg = Config(custom_path=str(self.watch_path / "watcher_cfg.json"), load_env=False)
        cfg.set("file_stability_wait_seconds", 0.05)
        cfg.set("auto_download_subtitles", True)
        cfg.set("subtitle_retry_cooldown_seconds", 3600)

        empty_calls = 0

        def empty_http(url, method="GET", headers=None, body=None, timeout=10.0, max_bytes=10485760):
            nonlocal empty_calls
            empty_calls += 1
            return 404, {}, b""

        watcher_cooldown = MediaWatcher(
            watch_dir=str(self.watch_path),
            config=cfg,
            subtitle_fetcher=SubtitleFetcher(config=cfg, http_client=empty_http),
        )
        watcher_cooldown.scan_and_process_once()
        self.assertEqual(watcher_cooldown.last_result, "no_subtitles")
        first_calls = empty_calls
        self.assertGreater(first_calls, 0)

        # Second poll within cooldown must not re-query providers
        watcher_cooldown.scan_and_process_once()
        self.assertEqual(empty_calls, first_calls)
        self.assertEqual(watcher_cooldown.last_result, "no_subtitles")

        # Now provide a working trusted mock subtitle response
        def good_http(url, method="GET", headers=None, body=None, timeout=10.0, max_bytes=10485760):
            if "v3.sg.media-imdb.com/suggestion" in url:
                return 200, {}, b'{"d":[{"id":"tt9999999","l":"AutoMovie","y":2022,"qid":"movie"}]}'
            if "opensubtitles-v3.strem.io/subtitles/movie/tt9999999" in url:
                return 200, {}, b'{"subtitles":[{"id":"s1","url":"https://subs5.strem.io/s1.srt","lang":"eng"}]}'
            if url == "https://subs5.strem.io/s1.srt":
                return 200, {}, b"1\n00:00:00,400 --> 00:00:01,500\nWhat the shit!\n"
            return 404, {}, b""

        watcher_ok = MediaWatcher(
            watch_dir=str(self.watch_path),
            config=cfg,
            subtitle_fetcher=SubtitleFetcher(config=cfg, http_client=good_http),
        )
        watcher_ok.scan_and_process_once()

        self.assertEqual(watcher_ok.last_result, "completed")
        self.assertTrue((self.watch_path / "AutoMovie.2022.1080p.WEB.en.srt").exists())
        self.assertTrue((self.watch_path / "AutoMovie.2022.1080p.WEB.Cleaned.mp4").exists())


if __name__ == "__main__":
    unittest.main()
