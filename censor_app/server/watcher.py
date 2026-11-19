import os
import re
import time
import threading
from pathlib import Path
from typing import Optional, Set, Callable, List, Dict

from censor_app.core.config import Config
from censor_app.core.srt_parser import (
    parse_srt, probe_subtitle_streams, extract_embedded_subtitle
)
from censor_app.core.profanity_filter import ProfanityFilter
from censor_app.core.timing import detect_profanities_in_subtitles, merge_intervals
from censor_app.core.subtitle_writer import generate_cleaned_srt
from censor_app.core.ffmpeg_engine import FFmpegEngine
from censor_app.core.subtitle_fetcher import SubtitleFetcher, normalize_language_codes

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}
INCOMPLETE_EXTENSIONS = {".part", ".crdownload", ".tmp", "!qb", ".downloading", ".incomplete"}

class MediaWatcher:
    def __init__(
        self,
        watch_dir: str,
        output_dir: Optional[str] = None,
        config: Optional[Config] = None,
        logger: Optional[Callable[[str], None]] = None,
        subtitle_fetcher: Optional[SubtitleFetcher] = None,
    ):
        self.watch_dir = Path(watch_dir).resolve()
        self.config = config or Config()
        out_cfg = output_dir or self.config.get("output_dir")
        self.output_dir = Path(out_cfg).resolve() if out_cfg else None
        self.logger = logger or print
        self._stop_event = threading.Event()
        self.processed_files: Set[str] = set()
        self._waiting_for_subs: Set[str] = set()
        self._online_sub_retry_after: Dict[str, float] = {}
        self.last_result: str = "idle"
        self._engine = FFmpegEngine()
        self._subtitle_fetcher = subtitle_fetcher or SubtitleFetcher(config=self.config, logger=self.log)

    def is_already_cleaned(self, video_path: Path) -> bool:
        """Check whether a video filename indicates it is already a cleaned/censored output."""
        clean_suffix = self.config.get("output_suffix", ".Cleaned")
        stem = video_path.stem
        if clean_suffix.lower() in stem.lower():
            return True
        if re.search(r'(?:^|[._\-\s])censored(?:$|[._\-\s])', stem, re.IGNORECASE):
            return True
        return False

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
        iso1, iso2 = normalize_language_codes(self.config.get("subtitle_language", "en"))
        candidates: List[Path] = []
        seen = set()
        for ext in (
            ".srt",
            f".{iso1}.srt",
            f".{iso2}.srt",
            ".en.srt",
            ".eng.srt",
            ".vtt",
            f".{iso1}.vtt",
            ".en.vtt",
        ):
            if ext not in seen:
                seen.add(ext)
                candidates.append(parent / f"{stem}{ext}")
        for candidate in candidates:
            if candidate.exists() and candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        return None

    def _try_auto_fetch_subtitles(self, video_path: Path) -> Optional[Path]:
        """Attempt online subtitle download once per retry cooldown window."""
        if not self.config.get("auto_download_subtitles", True):
            return None
        key = str(video_path)
        now = time.monotonic()
        if now < self._online_sub_retry_after.get(key, 0.0):
            return None

        lang = self.config.get("subtitle_language", "en") or "en"
        try:
            dl_res = self._subtitle_fetcher.auto_fetch_subtitle_for_video(str(video_path), language=lang)
        except Exception as e:
            self.log(f"Online subtitle lookup failed for {video_path.name}: {e}")
            dl_res = None

        if dl_res and Path(dl_res.srt_path).exists():
            self.log(
                f"Auto-downloaded subtitle for {video_path.name} from {dl_res.provider} "
                f"({dl_res.cue_count} cues) -> {Path(dl_res.srt_path).name}"
            )
            self._waiting_for_subs.discard(key)
            return Path(dl_res.srt_path)

        cooldown = float(self.config.get("subtitle_retry_cooldown_seconds", 3600) or 3600)
        self._online_sub_retry_after[key] = now + cooldown
        return None

    def process_single_video(self, video_path: Path) -> bool:
        """Process one video file, creating the .Cleaned media alongside."""
        self.last_result = "idle"
        clean_suffix = self.config.get("output_suffix", ".Cleaned")
        if self.is_already_cleaned(video_path):
            self.last_result = "skipped"
            return False

        if self.output_dir:
            try:
                rel_parent = video_path.parent.relative_to(self.watch_dir)
                dest_dir = self.output_dir / rel_parent
            except ValueError:
                dest_dir = self.output_dir
            dest_dir.mkdir(parents=True, exist_ok=True)
        else:
            dest_dir = video_path.parent

        output_video_path = dest_dir / f"{video_path.stem}{clean_suffix}{video_path.suffix}"
        if output_video_path.exists():
            self.log(f"Cleaned version already exists for {video_path.name}, skipping.")
            self.last_result = "skipped"
            return False

        # Step 1: Find, extract, or download subtitles
        srt_file = self.find_matching_subtitles(video_path)
        temp_extracted_srt = None
        active_srt_path: Optional[str] = None

        if srt_file:
            self.log(f"Detected new video: {video_path.name}")
            self.log(f"Found paired subtitle: {srt_file.name}")
            active_srt_path = str(srt_file)
        else:
            streams = probe_subtitle_streams(str(video_path))
            text_streams = [s for s in streams if s.get("is_text", True)] if streams else []
            if text_streams:
                # Prefer English text streams if available
                selected = text_streams[0]
                for s in text_streams:
                    if s.get("language", "").lower() in ("eng", "en"):
                        selected = s
                        break

                sub_idx = selected.get("sub_index", 0)
                self.log(f"Detected new video: {video_path.name}")
                self.log(f"Extracting embedded text subtitle track #{sub_idx} (codec: {selected['codec']}, lang: {selected.get('language', 'und')})...")
                try:
                    temp_extracted_srt = extract_embedded_subtitle(str(video_path), stream_index=sub_idx)
                    active_srt_path = temp_extracted_srt
                except Exception as e:
                    self.log(f"Error extracting embedded subtitle: {e}")
                    self.last_result = "failed"
                    return False
            else:
                downloaded = self._try_auto_fetch_subtitles(video_path)
                if downloaded:
                    self.log(f"Detected new video: {video_path.name}")
                    active_srt_path = str(downloaded)
                else:
                    if str(video_path) not in self._waiting_for_subs:
                        if streams:
                            codecs = ", ".join(set(s.get("codec", "unknown") for s in streams))
                            self.log(
                                f"Embedded subtitles in {video_path.name} are image-based ({codecs}), "
                                f"and online lookup returned no match. Waiting for an external .srt file..."
                            )
                        else:
                            self.log(
                                f"No subtitle tracks found for {video_path.name} locally or online. "
                                f"Waiting for external subtitles..."
                            )
                        self._waiting_for_subs.add(str(video_path))
                    self.last_result = "no_subtitles"
                    return False

        self._waiting_for_subs.discard(str(video_path))

        try:
            # Step 2: Parse subtitles and detect profanity
            subs = parse_srt(active_srt_path)
            if not subs:
                if str(video_path) not in self._waiting_for_subs:
                    self.log(f"Subtitle file contains no entries: {active_srt_path}")
                    self._waiting_for_subs.add(str(video_path))
                self.last_result = "no_subtitles"
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
                padding_before=self.config.get("padding_before", 0.18),
                padding_after=self.config.get("padding_after", 0.16),
                lead_in=self.config.get("lead_in", 0.10),
                sync_offset=self.config.get("sync_offset", 0.0),
                video_path=str(video_path),
                audio_refine=self.config.get("audio_refine", True),
                center_channel_only=self.config.get("center_channel_only", True),
            )

            intervals = [(d.mute_start, d.mute_end) for d in detections if d.enabled]
            merged = merge_intervals(intervals, gap_threshold=self.config.get("merge_gap", 0.25))

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
                bleep_freq=self.config.get("bleep_frequency", 1000),
                crossfade_duration=self.config.get("crossfade_duration", 0.04)
            )

            if success:
                self.last_result = "completed"
                self.log(f"Successfully generated cleaned video: {output_video_path.name}")
                # Step 4: Write cleaned subtitles if requested
                if self.config.get("generate_clean_srt", True):
                    out_srt = dest_dir / f"{video_path.stem}{clean_suffix}.srt"
                    generate_cleaned_srt(
                        subs,
                        pf,
                        str(out_srt),
                        mask_char=self.config.get("censor_char", "*")
                    )
                    self.log(f"Generated sanitized subtitles: {out_srt.name}")
                return True
            else:
                self.last_result = "failed"
                err = f" ({self._engine.last_error})" if self._engine.last_error else ""
                self.log(f"FFmpeg failed while processing {video_path.name}{err}")
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
            # Skip output directory if it is located inside watch_dir
            if self.output_dir:
                try:
                    if Path(root).resolve() == self.output_dir or self.output_dir in Path(root).resolve().parents:
                        dirs.clear()
                        continue
                except Exception:
                    pass

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

                if self.is_already_cleaned(p):
                    continue

                if self.is_file_complete_and_stable(p, wait_seconds=self.config.get("file_stability_wait_seconds", 3)):
                    ok = self.process_single_video(p)
                    if ok or self.last_result == "skipped":
                        self.processed_files.add(str(p))

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
