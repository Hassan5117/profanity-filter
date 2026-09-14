import os
import re
import json
import subprocess
import tempfile
import threading
from typing import List, Tuple, Optional, Callable, Dict, Any

class FFmpegProbe:
    @staticmethod
    def probe_media(file_path: str) -> Dict[str, Any]:
        """Probe media file and extract duration, audio layout, and stream info."""
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration,size,bit_rate:stream=index,codec_type,codec_name,channels,channel_layout,sample_rate,width,height",
            "-of", "json",
            file_path
        ]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            data = json.loads(res.stdout)
            
            duration = 0.0
            if "format" in data and "duration" in data["format"]:
                duration = float(data["format"]["duration"])

            audio_channels = 2
            channel_layout = "stereo"
            audio_streams = []
            video_streams = []

            for s in data.get("streams", []):
                ctype = s.get("codec_type")
                if ctype == "audio":
                    audio_streams.append(s)
                    if audio_channels == 2 and "channels" in s:
                        audio_channels = int(s["channels"])
                        channel_layout = s.get("channel_layout", "stereo")
                elif ctype == "video":
                    video_streams.append(s)

            return {
                "duration": duration,
                "audio_channels": audio_channels,
                "channel_layout": channel_layout,
                "audio_streams": audio_streams,
                "video_streams": video_streams,
                "is_surround": audio_channels >= 6 or "5.1" in str(channel_layout) or "7.1" in str(channel_layout)
            }
        except Exception as e:
            return {
                "duration": 0.0,
                "audio_channels": 2,
                "channel_layout": "stereo",
                "audio_streams": [],
                "video_streams": [],
                "is_surround": False,
                "error": str(e)
            }


class AudioFilterBuilder:
    @staticmethod
    def build_enable_expression(intervals: List[Tuple[float, float]]) -> str:
        """Create FFmpeg between expression for intervals."""
        parts = [f"between(t,{round(s, 3)},{round(e, 3)})" for s, e in intervals]
        return "+".join(parts)

    @classmethod
    def build_filter(
        cls,
        intervals: List[Tuple[float, float]],
        mode: str = "mute",
        is_surround_51: bool = False,
        center_channel_only: bool = True,
        ducking_vol: float = 0.06,
        bleep_freq: int = 1000,
        total_duration: float = 0.0
    ) -> Tuple[str, bool]:
        """
        Builds FFmpeg filter string.
        Returns (filter_string, is_filter_complex: bool).
        """
        if not intervals:
            return "", False

        enable_expr = cls.build_enable_expression(intervals)
        
        # Decide base action on target channel
        if mode == "duck":
            core_action = f"volume={ducking_vol}:enable='{enable_expr}'"
        elif mode == "bleep":
            core_action = f"volume=0:enable='{enable_expr}'"
        else:  # mute
            core_action = f"volume=0:enable='{enable_expr}'"

        # Case 1: 5.1 Center-channel isolation requested
        if is_surround_51 and center_channel_only:
            if mode == "bleep":
                # For 5.1 bleep: split channels, mute FC, inject beep into FC, rejoin
                dur_arg = f":d={total_duration}" if total_duration > 0 else ""
                filter_str = (
                    f"sine=f={bleep_freq}:r=48000{dur_arg}[raw_beep]; "
                    f"[raw_beep]volume=0.8:enable='{enable_expr}'[beep]; "
                    f"[0:a]channelsplit=channel_layout=5.1[fl][fr][fc][lfe][sl][sr]; "
                    f"[fc]{core_action}[fc_muted]; "
                    f"[fc_muted][beep]amix=inputs=2:duration=first:dropout_transition=0[fc_out]; "
                    f"[fl][fr][fc_out][lfe][sl][sr]join=inputs=6:channel_layout=5.1[aout]"
                )
                return filter_str, True
            else:
                filter_str = (
                    f"[0:a]channelsplit=channel_layout=5.1[fl][fr][fc][lfe][sl][sr]; "
                    f"[fc]{core_action}[fc_clean]; "
                    f"[fl][fr][fc_clean][lfe][sl][sr]join=inputs=6:channel_layout=5.1[aout]"
                )
                return filter_str, True

        # Case 2: Standard stereo / all-channel bleep
        if mode == "bleep":
            dur_arg = f":d={total_duration}" if total_duration > 0 else ""
            filter_str = (
                f"sine=f={bleep_freq}:r=48000{dur_arg}[raw_beep]; "
                f"[raw_beep]volume=0.7:enable='{enable_expr}'[beep]; "
                f"[0:a]{core_action}[a_muted]; "
                f"[a_muted][beep]amix=inputs=2:duration=first:dropout_transition=0[aout]"
            )
            return filter_str, True

        # Case 3: Standard audio filter (mute / duck)
        return core_action, False


class FFmpegEngine:
    def __init__(self):
        self._current_process: Optional[subprocess.Popen] = None
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True
        if self._current_process:
            try:
                self._current_process.terminate()
            except OSError:
                pass

    def run_censor(
        self,
        input_video: str,
        output_video: str,
        intervals: List[Tuple[float, float]],
        mode: str = "mute",
        center_channel_only: bool = True,
        ducking_vol: float = 0.06,
        bleep_freq: int = 1000,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None
    ) -> bool:
        """
        Execute FFmpeg to censor audio with selected intervals and options.
        Streams progress events via progress_callback.
        """
        self._is_cancelled = False
        media_info = FFmpegProbe.probe_media(input_video)
        duration = media_info.get("duration", 0.0)
        is_surround = media_info.get("is_surround", False)

        if not intervals:
            # Nothing to mute, just do a fast stream copy
            cmd = ["ffmpeg", "-y", "-i", input_video, "-c", "copy", output_video]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return res.returncode == 0

        filter_str, is_complex = AudioFilterBuilder.build_filter(
            intervals=intervals,
            mode=mode,
            is_surround_51=is_surround,
            center_channel_only=center_channel_only,
            ducking_vol=ducking_vol,
            bleep_freq=bleep_freq,
            total_duration=duration
        )

        # Write filter string to temporary script file to prevent command line length overflow
        filter_script_fd, filter_script_path = tempfile.mkstemp(suffix=".txt", prefix="censor_filter_")
        with os.fdopen(filter_script_fd, "w", encoding="utf-8") as f:
            f.write(filter_str)

        try:
            cmd = ["ffmpeg", "-y", "-nostdin", "-i", input_video]

            if is_complex:
                cmd.extend(["-filter_complex_script", filter_script_path, "-map", "0:v:0", "-map", "[aout]"])
            else:
                cmd.extend(["-filter_script:a", filter_script_path, "-map", "0:v:0", "-map", "0:a:0"])

            # Copy video stream losslessly
            cmd.extend(["-c:v", "copy"])
            # High-quality audio encoding
            cmd.extend(["-c:a", "aac", "-b:a", "320k"])

            # Progress pipe
            cmd.extend(["-progress", "pipe:1", "-nostats", output_video])

            self._current_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
            )

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
            if self._current_process.stderr:
                self._current_process.stderr.close()
            self._current_process.wait()
            success = (self._current_process.returncode == 0) and not self._is_cancelled

            if progress_callback:
                progress_callback({
                    "status": "completed" if success else "failed",
                    "percent": 100.0 if success else 0.0,
                    "returncode": self._current_process.returncode
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
            if os.path.exists(filter_script_path):
                try:
                    os.remove(filter_script_path)
                except OSError:
                    pass
            self._current_process = None
