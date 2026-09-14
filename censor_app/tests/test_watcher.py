import unittest
import tempfile
import os
import shutil
import subprocess
from pathlib import Path
from censor_app.server.watcher import MediaWatcher, VIDEO_EXTENSIONS, INCOMPLETE_EXTENSIONS
from censor_app.core.config import Config


class TestWatcher(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.watch_path = Path(self.test_dir)
        self.config = Config()
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


if __name__ == "__main__":
    unittest.main()
