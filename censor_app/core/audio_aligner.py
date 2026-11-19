import array
import math
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Optional, Tuple

from censor_app.core.ffmpeg_engine import (
    FFmpegProbe,
    get_ffmpeg_path,
    subprocess_hidden_kwargs,
)

SAMPLE_RATE = 8000
FRAME_SAMPLES = 80
FRAME_SEC = FRAME_SAMPLES / SAMPLE_RATE  # 0.01s (10ms frames)


@dataclass
class AudioEnvelopeWindow:
    start_time: float
    frame_sec: float
    rms: List[float]
    smooth: List[float]

    @property
    def end_time(self) -> float:
        return self.start_time + len(self.smooth) * self.frame_sec

    def slice_frames(self, t0: float, t1: float) -> Tuple[float, List[float]]:
        """Return (actual_t0, smooth_subslice) covering [t0, t1] within this window."""
        if not self.smooth or t1 <= self.start_time or t0 >= self.end_time:
            return self.start_time, []
        idx0 = max(0, int(math.floor((t0 - self.start_time) / self.frame_sec)))
        idx1 = min(len(self.smooth), int(math.ceil((t1 - self.start_time) / self.frame_sec)))
        if idx1 <= idx0:
            return self.start_time + idx0 * self.frame_sec, []
        return self.start_time + idx0 * self.frame_sec, self.smooth[idx0:idx1]


def _pcm_to_envelope(raw_pcm: bytes, start_time: float) -> Optional[AudioEnvelopeWindow]:
    usable = len(raw_pcm) - (len(raw_pcm) % 2)
    if usable < FRAME_SAMPLES * 2:
        return None

    samples = array.array("h")
    samples.frombytes(raw_pcm[:usable])
    if sys.byteorder != "little":
        samples.byteswap()

    rms: List[float] = []
    n_samples = len(samples)
    for i in range(0, n_samples - FRAME_SAMPLES + 1, FRAME_SAMPLES):
        chunk = samples[i:i + FRAME_SAMPLES]
        energy = sum(x * x for x in chunk)
        rms.append(math.sqrt(energy / FRAME_SAMPLES))

    if not rms:
        return None

    n_rms = len(rms)
    smooth: List[float] = []
    for i in range(n_rms):
        lo = max(0, i - 1)
        hi = min(n_rms, i + 2)
        smooth.append(sum(rms[lo:hi]) / (hi - lo))

    return AudioEnvelopeWindow(
        start_time=start_time,
        frame_sec=FRAME_SEC,
        rms=rms,
        smooth=smooth,
    )


def _decode_spans(
    video_path: str,
    decode_spans: List[Tuple[float, float]],
    audio_filter: str,
) -> Tuple[List[AudioEnvelopeWindow], bool]:
    """Decode audio spans with FFmpeg into 10ms RMS envelopes. Returns (windows, any_failed)."""
    windows: List[AudioEnvelopeWindow] = []
    any_failed = False

    for s, e in decode_spans:
        if e <= s + 0.05:
            continue
        cmd = [
            get_ffmpeg_path(), "-nostdin", "-hide_banner", "-loglevel", "error",
            "-ss", f"{s:.3f}", "-to", f"{e:.3f}",
            "-i", video_path,
            "-vn", "-sn", "-dn",
            "-af", audio_filter,
            "-ac", "1", "-ar", str(SAMPLE_RATE),
            "-f", "s16le", "-",
        ]
        try:
            res = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                **subprocess_hidden_kwargs(),
            )
            if res.returncode != 0:
                any_failed = True
                continue
            win = _pcm_to_envelope(res.stdout, start_time=s)
            if win is not None:
                windows.append(win)
        except Exception:
            any_failed = True

    return windows, any_failed


def extract_speech_envelopes(
    video_path: str,
    cue_spans: List[Tuple[float, float]],
    center_channel_only: bool = True,
) -> List[AudioEnvelopeWindow]:
    """
    Extract 10ms speech-band RMS energy envelopes covering the provided subtitle cue spans.
    Clusters nearby cues to minimize FFmpeg invocations, or decodes one pass when dense.
    """
    if not cue_spans or not video_path or not os.path.exists(video_path):
        return []

    try:
        media_info = FFmpegProbe.probe_media(video_path)
        if not media_info.get("audio_streams") or media_info.get("audio_channels", 0) <= 0:
            return []

        padded: List[Tuple[float, float]] = []
        for s, e in cue_spans:
            ps = max(0.0, float(s) - 0.40)
            pe = max(ps + 0.1, float(e) + 0.40)
            padded.append((ps, pe))
        padded.sort(key=lambda x: x[0])

        clusters: List[Tuple[float, float]] = [padded[0]]
        for s, e in padded[1:]:
            prev_s, prev_e = clusters[-1]
            if s <= prev_e + 20.0:
                clusters[-1] = (prev_s, max(prev_e, e))
            else:
                clusters.append((s, e))

        full_start = clusters[0][0]
        full_end = clusters[-1][1]
        full_span = full_end - full_start
        cluster_sum = sum(e - s for s, e in clusters)

        if (len(clusters) > 12 or cluster_sum >= 0.45 * max(0.1, full_span)) and full_span <= 10800.0:
            decode_spans = [(full_start, full_end)]
        else:
            decode_spans = clusters

        use_center = bool(media_info.get("is_surround") and center_channel_only)
        base_af = "highpass=f=120,lowpass=f=3600"
        af = f"pan=mono|c0=FC,{base_af}" if use_center else base_af

        windows, any_failed = _decode_spans(video_path, decode_spans, af)

        if use_center:
            peak_energy = max((max(w.smooth, default=0.0) for w in windows), default=0.0)
            if any_failed or not windows or peak_energy < 80.0:
                fallback_windows, _ = _decode_spans(video_path, decode_spans, base_af)
                if fallback_windows:
                    return fallback_windows

        return windows
    except Exception:
        return []


def find_covering_window(
    windows: List[AudioEnvelopeWindow],
    t0: float,
    t1: float,
) -> Optional[AudioEnvelopeWindow]:
    """Find an extracted envelope window that covers [t0, t1] within 0.1s tolerance."""
    if not windows:
        return None
    req_start = max(0.0, t0)
    req_end = max(req_start + 0.05, t1)
    for w in windows:
        if w.start_time <= req_start + 0.1 and w.end_time >= req_end - 0.1:
            return w
    return None


def measure_cue_speech_bounds(
    env: AudioEnvelopeWindow,
    sub_start: float,
    sub_end: float,
    spoken_word_count: int,
) -> Optional[Tuple[float, float]]:
    """
    Detect actual active speech onset and offset inside [sub_start, sub_end] to trim
    leading dead air and trailing subtitle hold time before word-level interpolation.
    Returns None when dynamic contrast is too flat (e.g., loud music or flat test tone).
    """
    win_s = max(0.0, sub_start - 0.05)
    win_e = max(win_s + 0.2, sub_end + 0.05)
    slice_t0, frames = env.slice_frames(win_s, win_e)
    if len(frames) < 20:
        return None

    sv = sorted(frames)
    p15 = sv[int(0.15 * len(sv))]
    p90 = sv[min(len(sv) - 1, int(0.90 * len(sv)))]
    if p90 < 100.0 or (p90 / max(1.0, p15)) < 1.8:
        return None

    thresh = max(90.0, p15 + 0.18 * (p90 - p15))
    active_runs = [
        idx for idx in range(len(frames) - 2)
        if frames[idx] >= thresh and frames[idx + 1] >= thresh and frames[idx + 2] >= thresh
    ]
    if not active_runs:
        return None

    det_start = slice_t0 + active_runs[0] * env.frame_sec - 0.02
    det_end = slice_t0 + (active_runs[-1] + 3) * env.frame_sec + 0.03

    det_start = max(max(0.0, sub_start - 0.05), min(det_start, sub_start + 0.65))
    det_end = min(sub_end + 0.05, max(det_end, sub_end - 1.20))

    min_span = max(0.35, 0.14 * max(1, spoken_word_count))
    if det_end - det_start < min_span:
        return None

    return det_start, det_end


def snap_interval_to_valleys(
    env: AudioEnvelopeWindow,
    mute_start: float,
    mute_end: float,
    est_start: float,
    est_end: float,
    sub_start: float,
    sub_end: float,
    min_mute_duration: float = 0.35,
) -> Tuple[float, float]:
    """
    Snap mute_start and mute_end toward nearby inter-word energy valleys (pauses/consonant dips)
    around [est_start, est_end] so adjacent clean words are not clipped.
    """
    _, cue_frames = env.slice_frames(max(0.0, sub_start - 0.05), max(sub_start + 0.1, sub_end + 0.05))
    if len(cue_frames) < 10:
        return mute_start, mute_end

    sv = sorted(cue_frames)
    p15 = sv[int(0.15 * len(sv))]
    p90 = sv[min(len(sv) - 1, int(0.90 * len(sv)))]
    if p90 < 100.0 or (p90 / max(1.0, p15)) < 1.8:
        return mute_start, mute_end

    _, word_frames = env.slice_frames(est_start, max(est_start + 0.05, est_end))
    word_peak = max(max(word_frames, default=p90), 0.7 * p90)
    valley_ceil = p15 + 0.32 * (word_peak - p15)
    deep_ceil = p15 + 0.20 * (word_peak - p15)

    new_start = mute_start
    pre_lo = max(0.0, mute_start - 0.06)
    pre_hi = est_start - 0.03
    if pre_hi > pre_lo:
        pre_t0, pre_frames = env.slice_frames(pre_lo, pre_hi)
        if len(pre_frames) >= 3:
            pre_valleys = [
                i for i in range(len(pre_frames))
                if pre_frames[i] <= valley_ceil
                and (i == 0 or pre_frames[i] <= pre_frames[i - 1])
                and (i == len(pre_frames) - 1 or pre_frames[i] <= pre_frames[i + 1])
            ]
            if pre_valleys:
                best_i = pre_valleys[-1]
                new_start = max(0.0, pre_t0 + best_i * env.frame_sec - 0.025)

    new_end = mute_end
    post_lo = est_end + 0.03
    post_hi = mute_end + 0.06
    found_post = False
    if post_hi > post_lo:
        post_t0, post_frames = env.slice_frames(post_lo, post_hi)
        if len(post_frames) >= 3:
            post_valleys = [
                i for i in range(len(post_frames))
                if post_frames[i] <= valley_ceil
                and (i == 0 or post_frames[i] <= post_frames[i - 1])
                and (i == len(post_frames) - 1 or post_frames[i] <= post_frames[i + 1])
            ]
            if post_valleys:
                best_i = post_valleys[0]
                new_end = post_t0 + best_i * env.frame_sec + 0.025
                found_post = True

    if not found_post:
        # Handle slight est_end overshoot past a deep >=40ms inter-word pause right before est_end
        back_lo = max(new_start + min_mute_duration - 0.025, est_end - 0.12)
        back_hi = est_end + 0.03
        if back_hi > back_lo:
            back_t0, back_frames = env.slice_frames(back_lo, back_hi)
            if len(back_frames) >= 4:
                deep_runs = [
                    i for i in range(len(back_frames) - 3)
                    if all(back_frames[i + k] <= deep_ceil for k in range(4))
                ]
                if deep_runs:
                    mid_i = deep_runs[0] + 1
                    new_end = back_t0 + mid_i * env.frame_sec + 0.025

    new_start = max(0.0, new_start)
    new_end = max(new_end, new_start + min_mute_duration)
    return new_start, new_end
