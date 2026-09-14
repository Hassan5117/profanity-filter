import re
from dataclasses import dataclass
from typing import List, Tuple, Optional
from censor_app.core.srt_parser import SubtitleItem
from censor_app.core.profanity_filter import ProfanityMatch, ProfanityFilter

@dataclass
class CensorDetection:
    id: str
    subtitle_index: int
    word: str
    context_text: str
    subtitle_start: float
    subtitle_end: float
    mute_start: float
    mute_end: float
    tier: str
    enabled: bool = True

    def to_dict(self):
        return {
            "id": self.id,
            "subtitle_index": self.subtitle_index,
            "word": self.word,
            "context_text": self.context_text,
            "subtitle_start": round(self.subtitle_start, 3),
            "subtitle_end": round(self.subtitle_end, 3),
            "mute_start": round(self.mute_start, 3),
            "mute_end": round(self.mute_end, 3),
            "tier": self.tier,
            "enabled": self.enabled
        }


def calculate_word_interval(
    sub: SubtitleItem,
    match: ProfanityMatch,
    padding_before: float = 0.30,
    padding_after: float = 0.30,
    mode: str = "word",
    lead_in: float = 0.20,
    sync_offset: float = 0.0,
    min_mute_duration: float = 0.55
) -> Tuple[float, float]:
    """
    Calculate start and end time (in seconds) for a detected profanity.
    
    Includes:
    - sync_offset: Shifts subtitle timestamps to compensate for out-of-sync SRTs.
    - lead_in: Starts the mute earlier (default 200ms) to ensure speech onset isn't clipped.
    - Smart segment muting: If subtitle duration is short (<= 2.5s) or has few words,
      mutes the entire segment to avoid chopping dialogue half-way.
    """
    adjusted_sub_start = max(0.0, sub.start_time + sync_offset)
    adjusted_sub_end = max(adjusted_sub_start + 0.1, sub.end_time + sync_offset)
    duration = adjusted_sub_end - adjusted_sub_start

    words = sub.clean_text.split()
    word_count = len(words)

    # If segment mode is requested, OR the subtitle line is short (<= 2.5s or <= 5 words),
    # mute the entire subtitle block to guarantee full word capture without cutting in half.
    if mode == "segment" or duration <= 2.5 or word_count <= 5:
        start = max(0.0, adjusted_sub_start - padding_before)
        end = adjusted_sub_end + padding_after
        return start, end

    # For longer sentences, calculate word-weighted timing
    text_len = len(sub.clean_text)
    if text_len == 0:
        return adjusted_sub_start, adjusted_sub_end

    # Word-level proportional interpolation with early lead-in
    char_start_ratio = match.start_char / text_len
    char_end_ratio = match.end_char / text_len

    estimated_start = adjusted_sub_start + (char_start_ratio * duration)
    estimated_end = adjusted_sub_start + (char_end_ratio * duration)

    # Apply lead-in (starts earlier to prevent clipping initial consonants) and padding
    mute_start = max(0.0, estimated_start - lead_in - padding_before)
    mute_end = estimated_end + padding_after

    # Clamp so mute doesn't drift uncontrollably beyond subtitle boundaries
    mute_start = max(0.0, min(mute_start, adjusted_sub_start + duration - 0.2))
    mute_end = max(mute_end, mute_start + min_mute_duration)

    return mute_start, mute_end


def merge_intervals(intervals: List[Tuple[float, float]], gap_threshold: float = 0.35) -> List[Tuple[float, float]]:
    """
    Merge overlapping intervals and intervals closer than gap_threshold seconds.
    Default 0.35s prevents jarring audio on/off stuttering when swear words are close together.
    """
    if not intervals:
        return []

    sorted_intervals = sorted(intervals, key=lambda x: x[0])
    merged = [sorted_intervals[0]]

    for current_start, current_end in sorted_intervals[1:]:
        last_start, last_end = merged[-1]
        if current_start <= last_end + gap_threshold:
            # Overlaps or is within gap threshold; extend the interval
            merged[-1] = (last_start, max(last_end, current_end))
        else:
            merged.append((current_start, current_end))

    return merged


def detect_profanities_in_subtitles(
    subtitles: List[SubtitleItem],
    profanity_filter: ProfanityFilter,
    timing_mode: str = "word",
    padding_before: float = 0.30,
    padding_after: float = 0.30,
    lead_in: float = 0.20,
    sync_offset: float = 0.0
) -> List[CensorDetection]:
    """Scan all subtitle items and generate CensorDetection objects."""
    detections: List[CensorDetection] = []
    det_counter = 1

    for sub in subtitles:
        matches = profanity_filter.find_matches(sub.clean_text)
        if not matches:
            continue

        for m in matches:
            mute_start, mute_end = calculate_word_interval(
                sub=sub,
                match=m,
                padding_before=padding_before,
                padding_after=padding_after,
                mode=timing_mode,
                lead_in=lead_in,
                sync_offset=sync_offset
            )
            detections.append(CensorDetection(
                id=f"det_{det_counter}",
                subtitle_index=sub.index,
                word=m.word,
                context_text=sub.clean_text,
                subtitle_start=max(0.0, sub.start_time + sync_offset),
                subtitle_end=max(0.0, sub.end_time + sync_offset),
                mute_start=mute_start,
                mute_end=mute_end,
                tier=m.tier,
                enabled=True
            ))
            det_counter += 1

    return detections
