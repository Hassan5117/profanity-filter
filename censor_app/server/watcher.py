import os
import time
import threading
from pathlib import Path
from typing import Optional, Set, Callable, List

from censor_app.core.config import Config
from censor_app.core.srt_parser import (
    parse_srt, probe_subtitle_streams, extract_embedded_subtitle
)
from censor_app.core.profanity_filter import ProfanityFilter
from censor_app.core.timing import detect_profanities_in_subtitles, merge_intervals
from censor_app.core.subtitle_writer import generate_cleaned_srt
from censor_app.core.ffmpeg_engine import FFmpegEngine

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}
INCOMPLETE_EXTENSIONS = {".part", ".crdownload", ".tmp", "!qb", ".downloading", ".incomplete"}

class MediaWatcher:
    def __init__(self, watch_dir: str, config: Optional[Config] = None, logger: Optional[Callable[[str], None]] = None):
        self.watch_dir = Path(watch_dir).resolve()
        self.config = config or Config()
        self.logger = logger or print
        self._stop_event = threading.Event()
        self.processed_files: Set[str] = set()
        self._engine = FFmpegEngine()

    def log(self, message: str):
        self.logger(f"[MediaWatcher] {message}")

    def stop(self):
        self._stop_event.set()
        self._engine.cancel()

    def is_file_complete_and_stable(self, file_path: Path, wait_seconds: float = 3.0) -> bool:
        """Verify the file is not currently being written by checking size stability."""
        try:
            initial_size = file_path.stat().st_size
            if initial_size == 0:
                return False
            time.sleep(wait_seconds)
            return file_path.stat().st_size == initial_size
        except OSError:
            return False

    def find_matching_subtitles(self, video_path: Path) -> Optional[Path]:
        """Look for an external subtitle file in the same directory."""
        stem = video_path.stem
        parent = video_path.parent
        candidates = [
            parent / f"{stem}.srt",
            parent / f"{stem}.en.srt",
            parent / f"{stem}.eng.srt",
            parent / f"{stem}.vtt",
            parent / f"{stem}.en.vtt",
        ]
        for candidate in candidates:
            if candidate.exists() and candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        return None

    def process_single_video(self, video_path: Path) -> bool:
        """Process one video file, creating the .Cleaned media alongside."""
        clean_suffix = self.config.get("output_suffix", ".Cleaned")
        if clean_suffix.lower() in video_path.stem.lower() or "censored" in video_path.stem.lower():
            return False

        output_video_path = video_path.parent / f"{video_path.stem}{clean_suffix}{video_path.suffix}"
        if output_video_path.exists():
            self.log(f"Cleaned version already exists for {video_path.name}, skipping.")
            return False

        self.log(f"Detected new video: {video_path.name}")
        
        # Step 1: Find or extract subtitles
        srt_file = self.find_matching_subtitles(video_path)
        temp_extracted_srt = None

        if srt_file:
            self.log(f"Found paired subtitle: {srt_file.name}")
            active_srt_path = str(srt_file)
        else:
            self.log(f"No external subtitle found. Checking embedded tracks in {video_path.name}...")
            streams = probe_subtitle_streams(str(video_path))
            if not streams:
                self.log(f"No subtitle tracks found for {video_path.name}. Cannot detect profanity.")
                return False
            self.log(f"Extracting embedded subtitle track (codec: {streams[0]['codec']})...")
            try:
                temp_extracted_srt = extract_embedded_subtitle(str(video_path), stream_index=0)
                active_srt_path = temp_extracted_srt
            except Exception as e:
                self.log(f"Error extracting embedded subtitle: {e}")
                return False

        try:
            # Step 2: Parse subtitles and detect profanity
            subs = parse_srt(active_srt_path)
            if not subs:
                self.log(f"Subtitle file contains no entries: {active_srt_path}")
                return False

            pf = ProfanityFilter(
                preset=self.config.get("preset", "moderate"),
                custom_words=self.config.get("custom_words", []),
                whitelist=set(self.config.get("whitelist_words", []))
            )

            detections = detect_profanities_in_subtitles(
                subtitles=subs,
                profanity_filter=pf,
                timing_mode=self.config.get("timing_mode", "word"),
                padding_before=self.config.get("padding_before", 0.15),
                padding_after=self.config.get("padding_after", 0.15)
            )

            intervals = [(d.mute_start, d.mute_end) for d in detections if d.enabled]
            merged = merge_intervals(intervals)

            self.log(f"Found {len(detections)} profanities ({len(merged)} mute segments) in {video_path.name}")

            # Step 3: Run FFmpeg censorship
            mode = self.config.get("mode", "mute")
            center_only = self.config.get("center_channel_only", True)

            success = self._engine.run_censor(
                input_video=str(video_path),
                output_video=str(output_video_path),
                intervals=merged,
                mode=mode,
                center_channel_only=center_only,
                ducking_vol=self.config.get("ducking_volume", 0.06),
                bleep_freq=self.config.get("bleep_frequency", 1000)
            )

            if success:
                self.log(f"Successfully generated cleaned video: {output_video_path.name}")
                # Step 4: Write cleaned subtitles if requested
                if self.config.get("generate_clean_srt", True):
                    out_srt = video_path.parent / f"{video_path.stem}{clean_suffix}.srt"
                    generate_cleaned_srt(subs, pf, str(out_srt))
                    self.log(f"Generated sanitized subtitles: {out_srt.name}")
                return True
            else:
                self.log(f"FFmpeg failed while processing {video_path.name}")
                return False

        finally:
            if temp_extracted_srt and os.path.exists(temp_extracted_srt):
                try:
                    os.remove(temp_extracted_srt)
                except OSError:
                    pass

    def scan_and_process_once(self):
        """Single scan pass through the directory."""
        if not self.watch_dir.exists():
            return

        for root, dirs, files in os.walk(self.watch_dir):
            for file_name in files:
                p = Path(root) / file_name

                # Skip non-video extensions and temporary files
                if p.suffix.lower() not in VIDEO_EXTENSIONS:
                    continue
                if any(p.name.endswith(ext) for ext in INCOMPLETE_EXTENSIONS):
                    continue
                if p.name.startswith("."):
                    continue
                if str(p) in self.processed_files:
                    continue

                clean_suffix = self.config.get("output_suffix", ".Cleaned")
                if clean_suffix.lower() in p.stem.lower():
                    continue

                if self.is_file_complete_and_stable(p, wait_seconds=self.config.get("file_stability_wait_seconds", 3)):
                    self.processed_files.add(str(p))
                    self.process_single_video(p)

    def run_forever(self):
        """Continuous watching loop."""
        interval = self.config.get("watch_interval_seconds", 10)
        self.log(f"Starting watcher daemon on: {self.watch_dir} (interval: {interval}s)")
        while not self._stop_event.is_set():
            try:
                self.scan_and_process_once()
            except Exception as e:
                self.log(f"Error in scan loop: {e}")
            
            # Sleep in short increments to allow fast shutdown
            for _ in range(int(interval * 2)):
                if self._stop_event.is_set():
                    break
                time.sleep(0.5)
        self.log("Watcher daemon stopped.")
