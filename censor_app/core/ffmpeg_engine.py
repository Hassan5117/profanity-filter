import os
import json
import shutil
import subprocess
import sys
import tempfile
import threading
from collections import deque
from pathlib import Path
from typing import List, Tuple, Optional, Callable, Dict, Any


def subprocess_hidden_kwargs() -> Dict[str, Any]:
    """Suppress transient console windows when spawning helper executables on Windows."""
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def resolve_media_binary(name: str) -> Optional[str]:
    """Locate ffmpeg/ffprobe across env override, PyInstaller bundle, PATH, and standard macOS paths."""
    exe_name = f"{name}.exe" if sys.platform == "win32" else name
    env_override = (os.environ.get(f"CENSOR_{name.upper()}_PATH") or "").strip()
    if env_override:
        cand = Path(env_override).expanduser()
        if cand.exists() and cand.is_file():
            return str(cand)

    if getattr(sys, "frozen", False):
        bundle_candidates: List[Path] = []
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            mp_dir = Path(meipass)
            bundle_candidates.extend([mp_dir / "bin" / exe_name, mp_dir / exe_name])
        exe_dir = Path(sys.executable).resolve().parent
        bundle_candidates.extend([
            exe_dir / "bin" / exe_name,
            exe_dir / exe_name,
            exe_dir.parent / "Resources" / "bin" / exe_name,
            exe_dir.parent / "Resources" / exe_name,
        ])
        for cand in bundle_candidates:
            try:
                if cand.exists() and cand.is_file():
                    return str(cand)
            except OSError:
                continue

    which_hit = shutil.which(name)
    if which_hit:
        return which_hit

    if sys.platform == "darwin":
        for mac_dir in (Path("/opt/homebrew/bin"), Path("/usr/local/bin")):
            cand = mac_dir / name
            if cand.exists() and cand.is_file():
                return str(cand)

    return None


def is_bundled_binary(binary_path: Optional[str]) -> bool:
    """Return True if binary_path points inside the active PyInstaller bundle."""
    if not binary_path or not getattr(sys, "frozen", False):
        return False
    try:
        resolved = Path(binary_path).resolve()
    except OSError:
        return False

    bundle_roots: List[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        try:
            bundle_roots.append(Path(meipass).resolve())
        except OSError:
            pass
    try:
        exe_dir = Path(sys.executable).resolve().parent
        bundle_roots.append(exe_dir)
        if exe_dir.name == "MacOS" and exe_dir.parent.name == "Contents":
            bundle_roots.append(exe_dir.parent)
    except OSError:
        pass

    for root in bundle_roots:
        if resolved == root or root in resolved.parents:
            return True
    return False


def get_ffmpeg_path() -> str:
    return resolve_media_binary("ffmpeg") or "ffmpeg"


def get_ffprobe_path() -> str:
    return resolve_media_binary("ffprobe") or "ffprobe"


class FFmpegProbe:
    @staticmethod
    def probe_media(file_path: str) -> Dict[str, Any]:
        """Probe media file and extract duration, audio layout, and stream info."""
        cmd = [
            get_ffprobe_path(),
            "-v", "error",
            "-show_entries",
            "format=duration,size,bit_rate:stream=index,codec_type,codec_name,codec_tag_string,channels,channel_layout,sample_rate,width,height,disposition",
            "-of", "json",
            file_path
        ]
        try:
            res = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
                **subprocess_hidden_kwargs(),
            )
            data = json.loads(res.stdout)

            duration = 0.0
            raw_duration = data.get("format", {}).get("duration")
            if raw_duration is not None:
                try:
                    duration = float(raw_duration)
                except (ValueError, TypeError):
                    duration = 0.0

            audio_channels = 2
            channel_layout = "stereo"
            sample_rate = 48000
            first_audio_seen = False
            audio_streams = []
            video_streams = []

            for s in data.get("streams", []):
                ctype = s.get("codec_type")
                if ctype == "audio":
                    audio_streams.append(s)
                    if not first_audio_seen:
                        first_audio_seen = True
                        if "channels" in s:
                            try:
                                audio_channels = int(s["channels"])
                            except (ValueError, TypeError):
                                audio_channels = 2
                        channel_layout = s.get("channel_layout") or ("mono" if audio_channels == 1 else "stereo")
                        if "sample_rate" in s:
                            try:
                                sample_rate = int(s["sample_rate"])
                            except (ValueError, TypeError):
                                sample_rate = 48000
                elif ctype == "video":
                    video_streams.append(s)

            primary_video = next(
                (vs for vs in video_streams if not vs.get("disposition", {}).get("attached_pic")),
                video_streams[0] if video_streams else {}
            )
            video_codec = primary_video.get("codec_name", "")
            video_codec_tag = primary_video.get("codec_tag_string", "")

            return {
                "duration": duration,
                "audio_channels": audio_channels,
                "channel_layout": channel_layout,
                "sample_rate": sample_rate,
                "video_codec": video_codec,
                "video_codec_tag": video_codec_tag,
                "audio_streams": audio_streams,
                "video_streams": video_streams,
                "is_surround": audio_channels >= 6 or "5.1" in str(channel_layout) or "7.1" in str(channel_layout)
            }
        except Exception as e:
            return {
                "duration": 0.0,
                "audio_channels": 2,
                "channel_layout": "stereo",
                "sample_rate": 48000,
                "video_codec": "",
                "video_codec_tag": "",
                "audio_streams": [],
                "video_streams": [],
                "is_surround": False,
                "error": str(e)
            }

    @staticmethod
    def check_dependencies() -> Dict[str, Any]:
        """Check availability of bundled or system ffmpeg and ffprobe executables."""
        ffmpeg_path = resolve_media_binary("ffmpeg")
        ffprobe_path = resolve_media_binary("ffprobe")
        if sys.platform == "darwin":
            install_hint = "brew install ffmpeg"
        elif sys.platform == "win32":
            install_hint = "winget install Gyan.FFmpeg"
        else:
            install_hint = "sudo apt install ffmpeg"
        return {
            "ffmpeg": bool(ffmpeg_path),
            "ffprobe": bool(ffprobe_path),
            "ffmpeg_path": ffmpeg_path,
            "ffprobe_path": ffprobe_path,
            "bundled": is_bundled_binary(ffmpeg_path) and is_bundled_binary(ffprobe_path),
            "ok": bool(ffmpeg_path and ffprobe_path),
            "install_hint": install_hint,
        }


class AudioFilterBuilder:
    CHUNK_SIZE = 25

    @staticmethod
    def build_enable_expression(intervals: List[Tuple[float, float]]) -> str:
        """Create FFmpeg between expression for intervals."""
        parts = [f"between(t,{round(s, 3)},{round(e, 3)})" for s, e in intervals]
        return "+".join(parts)

    @staticmethod
    def build_smooth_envelope_sum(intervals: List[Tuple[float, float]], crossfade: float = 0.04) -> str:
        """Build an expression summing smooth [0, 1] trapezoidal fade envelopes for intervals."""
        if crossfade <= 0:
            terms = [f"between(t,{round(s, 3)},{round(e, 3)})" for s, e in intervals if e > s]
            return "+".join(terms) if terms else "0"

        terms = []
        for s, e in intervals:
            if e <= s:
                continue
            fade = round(min(crossfade, max(0.001, (e - s) / 2.0)), 4)
            terms.append(f"clip(min(t-{round(s, 3)},{round(e, 3)}-t)/{fade},0,1)")
        return "+".join(terms) if terms else "0"

    @classmethod
    def build_chunked_volume(
        cls,
        intervals: List[Tuple[float, float]],
        vol: Any,
        crossfade: float = 0.04,
        include_asetnsamples: bool = True
    ) -> str:
        """Split intervals into chunks to avoid FFmpeg eval.c recursion limits and apply smooth ramps."""
        chunks = [intervals[i:i + cls.CHUNK_SIZE] for i in range(0, len(intervals), cls.CHUNK_SIZE)]
        filters = []
        for c in chunks:
            env = cls.build_smooth_envelope_sum(c, crossfade)
            if vol == 0:
                filters.append(f"volume='1-min(1,{env})':eval=frame")
            else:
                filters.append(f"volume='1-(1-{vol})*min(1,{env})':eval=frame")
        chain = ",".join(filters)
        if include_asetnsamples and chain:
            return f"asetnsamples=n=256:p=1,{chain}"
        return chain

    @classmethod
    def build_beep_subgraph(
        cls,
        intervals: List[Tuple[float, float]],
        active_vol: float,
        bleep_freq: int = 1000,
        dur_arg: str = "",
        crossfade: float = 0.04
    ) -> str:
        """Generate and smoothly gate a sine wave across arbitrary intervals without eval depth overflow."""
        chunks = [intervals[i:i + cls.CHUNK_SIZE] for i in range(0, len(intervals), cls.CHUNK_SIZE)]
        base = f"sine=f={bleep_freq}:r=48000{dur_arg},asetnsamples=n=256:p=1"
        if len(chunks) == 1:
            env = cls.build_smooth_envelope_sum(chunks[0], crossfade)
            return f"{base},volume='{active_vol}*min(1,{env})':eval=frame[beep]"

        split_out = "".join(f"[b{i}]" for i in range(len(chunks)))
        branches = []
        for i, c in enumerate(chunks):
            env = cls.build_smooth_envelope_sum(c, crossfade)
            branches.append(f"[b{i}]volume='{active_vol}*min(1,{env})':eval=frame[bg{i}]")
        bg_inputs = "".join(f"[bg{i}]" for i in range(len(chunks)))
        return (
            f"{base},asplit={len(chunks)}{split_out}; "
            + "; ".join(branches)
            + f"; {bg_inputs}amix=inputs={len(chunks)}:normalize=0:dropout_transition=0[beep]"
        )

    @classmethod
    def build_filter(
        cls,
        intervals: List[Tuple[float, float]],
        mode: str = "mute",
        is_surround_51: bool = False,
        center_channel_only: bool = True,
        ducking_vol: float = 0.06,
        bleep_freq: int = 1000,
        total_duration: float = 0.0,
        crossfade_duration: float = 0.04
    ) -> Tuple[str, bool]:
        """
        Builds FFmpeg filter string.
        Returns (filter_string, is_filter_complex: bool).
        """
        if not intervals:
            return "", False

        target_vol = ducking_vol if mode == "duck" else 0
        dur_arg = f":d={total_duration}" if total_duration > 0 else ""

        # Case 1: 5.1 Center-channel isolation requested
        if is_surround_51 and center_channel_only:
            core_fc = cls.build_chunked_volume(
                intervals,
                target_vol,
                crossfade=crossfade_duration,
                include_asetnsamples=False
            )
            if mode == "bleep":
                beep_sub = cls.build_beep_subgraph(
                    intervals,
                    0.8,
                    bleep_freq=bleep_freq,
                    dur_arg=dur_arg,
                    crossfade=crossfade_duration
                )
                filter_str = (
                    f"{beep_sub}; "
                    f"[0:a:0]aformat=sample_rates=48000:channel_layouts=5.1,asetnsamples=n=256:p=1,channelsplit=channel_layout=5.1[fl][fr][fc][lfe][bl][br]; "
                    f"[fc]{core_fc}[fc_muted]; "
                    f"[fc_muted][beep]amix=inputs=2:duration=first:normalize=0:dropout_transition=0[fc_out]; "
                    f"[fl][fr][fc_out][lfe][bl][br]join=inputs=6:channel_layout=5.1[aout]"
                )
                return filter_str, True
            else:
                filter_str = (
                    f"[0:a:0]aformat=channel_layouts=5.1,asetnsamples=n=256:p=1,channelsplit=channel_layout=5.1[fl][fr][fc][lfe][bl][br]; "
                    f"[fc]{core_fc}[fc_clean]; "
                    f"[fl][fr][fc_clean][lfe][bl][br]join=inputs=6:channel_layout=5.1[aout]"
                )
                return filter_str, True

        # Case 2: Standard stereo / all-channel bleep
        if mode == "bleep":
            core_action = cls.build_chunked_volume(
                intervals,
                0,
                crossfade=crossfade_duration,
                include_asetnsamples=True
            )
            beep_sub = cls.build_beep_subgraph(
                intervals,
                0.7,
                bleep_freq=bleep_freq,
                dur_arg=dur_arg,
                crossfade=crossfade_duration
            )
            filter_str = (
                f"{beep_sub}; "
                f"[0:a:0]aformat=sample_rates=48000,{core_action}[a_muted]; "
                f"[a_muted][beep]amix=inputs=2:duration=first:normalize=0:dropout_transition=0[aout]"
            )
            return filter_str, True

        # Case 3: Standard audio filter (mute / duck)
        core_action = cls.build_chunked_volume(
            intervals,
            target_vol,
            crossfade=crossfade_duration,
            include_asetnsamples=True
        )
        return core_action, False


class FFmpegEngine:
    def __init__(self):
        self._current_process: Optional[subprocess.Popen] = None
        self._is_cancelled = False
        self.last_error: str = ""

    def cancel(self):
        self._is_cancelled = True
        if self._current_process:
            try:
                self._current_process.terminate()
            except OSError:
                pass

    @staticmethod
    def _is_same_file(path_a: str, path_b: str) -> bool:
        try:
            if Path(path_a).resolve() == Path(path_b).resolve():
                return True
            if os.path.exists(path_a) and os.path.exists(path_b):
                return os.path.samefile(path_a, path_b)
        except OSError:
            pass
        return False

    def run_censor(
        self,
        input_video: str,
        output_video: str,
        intervals: List[Tuple[float, float]],
        mode: str = "mute",
        center_channel_only: bool = True,
        ducking_vol: float = 0.06,
        bleep_freq: int = 1000,
        crossfade_duration: float = 0.04,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None
    ) -> bool:
        """
        Execute FFmpeg to censor audio with selected intervals and options.
        Streams progress events via progress_callback.
        """
        self._is_cancelled = False
        self.last_error = ""

        if self._is_same_file(input_video, output_video):
            self.last_error = "Input and output video paths must be different."
            if progress_callback:
                progress_callback({"status": "failed", "percent": 0.0, "error": self.last_error})
            return False

        out_path = Path(output_video).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        temp_output = str(out_path.parent / f".{out_path.stem}.tmp_{os.getpid()}_{threading.get_ident()}{out_path.suffix}")

        media_info = FFmpegProbe.probe_media(input_video)
        duration = media_info.get("duration", 0.0)
        is_surround = media_info.get("is_surround", False)
        out_ext = out_path.suffix.lower()

        if not intervals:
            try:
                cmd = [get_ffmpeg_path(), "-y", "-nostdin", "-i", input_video, "-c", "copy", temp_output]
                res = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    **subprocess_hidden_kwargs(),
                )
                success = (res.returncode == 0) and not self._is_cancelled
                if success:
                    os.replace(temp_output, str(out_path))
                else:
                    self.last_error = res.stderr.strip()[-1500:] if res.stderr else "FFmpeg copy failed"
                if progress_callback:
                    progress_callback({
                        "status": "completed" if success else "failed",
                        "percent": 100.0 if success else 0.0,
                        "returncode": res.returncode,
                        "error": self.last_error
                    })
                return success
            finally:
                if os.path.exists(temp_output):
                    try:
                        os.remove(temp_output)
                    except OSError:
                        pass

        filter_str, is_complex = AudioFilterBuilder.build_filter(
            intervals=intervals,
            mode=mode,
            is_surround_51=is_surround,
            center_channel_only=center_channel_only,
            ducking_vol=ducking_vol,
            bleep_freq=bleep_freq,
            total_duration=duration,
            crossfade_duration=crossfade_duration
        )

        # Write filter string to temporary script file to prevent command line length overflow
        filter_script_fd, filter_script_path = tempfile.mkstemp(suffix=".txt", prefix="censor_filter_")
        with os.fdopen(filter_script_fd, "w", encoding="utf-8") as f:
            f.write(filter_str)

        stderr_lines: deque = deque(maxlen=40)
        stderr_thread: Optional[threading.Thread] = None

        try:
            cmd = [get_ffmpeg_path(), "-y", "-nostdin", "-i", input_video]

            if is_complex:
                cmd.extend(["-filter_complex_script", filter_script_path, "-map", "0:v:0?", "-map", "[aout]"])
            else:
                cmd.extend(["-filter_script:a", filter_script_path, "-map", "0:v:0?", "-map", "0:a:0"])

            # Copy video stream losslessly; only add hvc1 tag for non-tagged HEVC in MP4/MOV containers
            cmd.extend(["-strict", "unofficial", "-c:v", "copy"])
            vcodec = media_info.get("video_codec", "").lower()
            vtag = media_info.get("video_codec_tag", "").lower()
            if out_ext in (".mp4", ".m4v", ".mov") and vcodec == "hevc" and vtag not in ("hvc1", "dvh1", "dvhe"):
                cmd.extend(["-tag:v", "hvc1"])

            # Select container-compatible audio codec
            if out_ext == ".webm":
                if is_surround:
                    cmd.extend(["-c:a", "libopus", "-mapping_family", "1", "-b:a", "256k"])
                else:
                    cmd.extend(["-c:a", "libopus", "-b:a", "192k"])
            elif is_surround and out_ext == ".avi":
                cmd.extend(["-c:a", "ac3", "-b:a", "640k"])
            elif is_surround:
                cmd.extend(["-c:a", "eac3", "-b:a", "640k"])
            else:
                cmd.extend(["-c:a", "aac", "-b:a", "320k"])

            # Progress pipe
            cmd.extend(["-progress", "pipe:1", "-nostats", temp_output])

            self._current_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                **subprocess_hidden_kwargs(),
            )

            def _drain_stderr(pipe):
                try:
                    for err_line in iter(pipe.readline, ''):
                        stderr_lines.append(err_line.rstrip())
                except Exception:
                    pass

            if self._current_process.stderr:
                stderr_thread = threading.Thread(
                    target=_drain_stderr,
                    args=(self._current_process.stderr,),
                    daemon=True
                )
                stderr_thread.start()

            # Monitor stdout for progress key-value pairs
            if self._current_process.stdout:
                for line in iter(self._current_process.stdout.readline, ''):
                    if self._is_cancelled:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        if k == "out_time_us":
                            try:
                                current_sec = int(v) / 1_000_000.0
                                pct = min(100.0, (current_sec / duration * 100.0)) if duration > 0 else 0.0
                                if progress_callback:
                                    progress_callback({
                                        "status": "processing",
                                        "percent": round(pct, 1),
                                        "current_time": round(current_sec, 2),
                                        "total_duration": round(duration, 2)
                                    })
                            except (ValueError, TypeError):
                                pass

            if self._current_process.stdout:
                self._current_process.stdout.close()
            self._current_process.wait()
            if stderr_thread:
                stderr_thread.join(timeout=2.0)
            if self._current_process.stderr:
                self._current_process.stderr.close()

            success = (self._current_process.returncode == 0) and not self._is_cancelled
            if success:
                os.replace(temp_output, str(out_path))
            elif not self._is_cancelled:
                self.last_error = "\n".join(stderr_lines).strip()

            if progress_callback:
                progress_callback({
                    "status": "completed" if success else "failed",
                    "percent": 100.0 if success else 0.0,
                    "returncode": self._current_process.returncode,
                    "error": self.last_error
                })

            return success
        finally:
            if self._current_process:
                if self._current_process.stdout:
                    try:
                        self._current_process.stdout.close()
                    except Exception:
                        pass
                if self._current_process.stderr:
                    try:
                        self._current_process.stderr.close()
                    except Exception:
                        pass
            if os.path.exists(temp_output):
                try:
                    os.remove(temp_output)
                except OSError:
                    pass
            if os.path.exists(filter_script_path):
                try:
                    os.remove(filter_script_path)
                except OSError:
                    pass
            self._current_process = None
