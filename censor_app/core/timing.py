import re
from dataclasses import dataclass
from typing import List, Tuple, Optional

from censor_app.core.srt_parser import SubtitleItem
from censor_app.core.profanity_filter import ProfanityMatch, ProfanityFilter
from censor_app.core.audio_aligner import (
    AudioEnvelopeWindow,
    extract_speech_envelopes,
    find_covering_window,
    measure_cue_speech_bounds,
    snap_interval_to_valleys,
)

SHORT_FUNCTION_WORDS = {
    "a", "i", "to", "of", "the", "in", "on", "at", "it", "is",
    "or", "an", "as", "by", "my", "so", "we", "he", "me", "be", "do", "up", "no"
}

SPEAKER_PREFIX_RE = re.compile(r"(?:^|(?<=[.!?]\s)|(?<=-\s))[A-Z][A-Z0-9 _'\-]{1,18}:\s*")
SDH_BRACKET_RE = re.compile(r"\[[^\]]{1,45}\]|\([^\)]{1,45}\)")
WORD_TOKEN_RE = re.compile(
    r"[A-Za-z0-9]+(?:['\u2019\*#@\$_\-][A-Za-z0-9\*#@\$]+)*[\*#@\$'\u2019]*|[\*#@\$]{2,}"
)


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


def _estimate_word_weight(word: str) -> float:
    """Estimate relative spoken duration weight of a word based on English syllable cues."""
    w = re.sub(r"[^a-zA-Z]", "", word).lower()
    if not w:
        return 1.2
    if w in SHORT_FUNCTION_WORDS:
        return 0.65

    if len(w) > 5 and w.endswith("ely"):
        w = w[:-3] + "ly"
    elif len(w) > 4 and w.endswith("ed") and w[-3] not in "td":
        w = w[:-2]
    elif len(w) >= 3 and w.endswith("e") and not (
        len(w) >= 4 and w.endswith("le") and w[-3] in "bdfgkptvz"
    ):
        w = w[:-1]

    syl = max(1, len(re.findall(r"[aeiouy]+", w)))
    return 0.75 + 0.55 * syl


def _pause_weight(gap_text: str) -> float:
    """Estimate relative pause duration between adjacent tokens from punctuation."""
    if re.search(r"\.\.\.|…|--|—|[.!?]|(?:^|\s)-\s", gap_text):
        return 0.85
    if re.search(r"[,;:]", gap_text):
        return 0.55
    return 0.12


def _tokenize_spoken_words(
    text: str,
    protected_span: Optional[Tuple[int, int]] = None
) -> Tuple[List[Tuple[int, int, str, float]], List[Tuple[int, int]]]:
    """
    Tokenize spoken words outside SPEAKER: prefixes and [SDH]/(SDH) stage directions.
    Any non-spoken span overlapping protected_span is kept so the match itself isn't skipped.
    Returns (tokens, sdh_spans) where tokens are (start_char, end_char, word, weight).
    """
    ignored_spans: List[Tuple[int, int]] = []
    sdh_spans: List[Tuple[int, int]] = []

    for rx, kind in ((SPEAKER_PREFIX_RE, "speaker"), (SDH_BRACKET_RE, "sdh")):
        for m_ign in rx.finditer(text):
            si, ei = m_ign.span()
            if protected_span and si < protected_span[1] and ei > protected_span[0]:
                continue
            ignored_spans.append((si, ei))
            if kind == "sdh":
                sdh_spans.append((si, ei))

    tokens: List[Tuple[int, int, str, float]] = []
    for tm in WORD_TOKEN_RE.finditer(text):
        ts, te = tm.span()
        if any(ts >= si and te <= ei for si, ei in ignored_spans):
            continue
        tok = tm.group(0)
        tokens.append((ts, te, tok, _estimate_word_weight(tok)))

    return tokens, sdh_spans


def _count_spoken_words(text: str) -> int:
    tokens, _ = _tokenize_spoken_words(text)
    if tokens:
        return len(tokens)
    return max(1, len(text.split()))


def _calculate_word_interval_detailed(
    sub: SubtitleItem,
    match: ProfanityMatch,
    padding_before: float = 0.18,
    padding_after: float = 0.16,
    mode: str = "word",
    lead_in: float = 0.10,
    sync_offset: float = 0.0,
    min_mute_duration: float = 0.35,
    speech_bounds: Optional[Tuple[float, float]] = None,
) -> Tuple[float, float, float, float, bool]:
    """
    Calculate (mute_start, mute_end, est_word_start, est_word_end, is_full_segment)
    using syllable/pause-weighted timing and optional acoustic speech bounds.
    """
    adjusted_sub_start = max(0.0, sub.start_time + sync_offset)
    adjusted_sub_end = max(adjusted_sub_start + 0.1, sub.end_time + sync_offset)

    if mode == "segment":
        start = max(0.0, adjusted_sub_start - padding_before)
        end = adjusted_sub_end + padding_after
        return start, end, adjusted_sub_start, adjusted_sub_end, True

    if speech_bounds is not None:
        sp_start = max(0.0, speech_bounds[0])
        sp_end = max(sp_start + 0.1, speech_bounds[1])
    else:
        sp_start = adjusted_sub_start
        sp_end = adjusted_sub_end

    duration = sp_end - sp_start
    text = sub.clean_text
    if not text:
        return sp_start, sp_end, sp_start, sp_end, True

    tokens, sdh_spans = _tokenize_spoken_words(text, protected_span=(match.start_char, match.end_char))
    if not tokens:
        start = max(0.0, sp_start - padding_before)
        end = sp_end + padding_after
        return start, end, sp_start, sp_end, True

    spoken_word_count = len(tokens)
    gaps: List[float] = []
    for i in range(spoken_word_count - 1):
        gap_str = text[tokens[i][1]:tokens[i + 1][0]]
        pw = _pause_weight(gap_str)
        if any(si >= tokens[i][1] and ei <= tokens[i + 1][0] for si, ei in sdh_spans):
            pw = max(pw, 0.55)
        gaps.append(pw)

    has_clause_break = any(g >= 0.55 for g in gaps)

    hit_indices = [
        idx for idx, (ts, te, _, _) in enumerate(tokens)
        if ts < match.end_char and te > match.start_char
    ]
    if not hit_indices:
        text_len = max(1, len(text))
        est_start = sp_start + (match.start_char / text_len) * duration
        est_end = sp_start + (match.end_char / text_len) * duration
        mute_start = max(0.0, est_start - lead_in - padding_before)
        mute_end = max(est_end + padding_after, mute_start + min_mute_duration)
        return mute_start, mute_end, est_start, est_end, False

    first_idx, last_idx = hit_indices[0], hit_indices[-1]
    total_spoken_weight = sum(t[3] for t in tokens)
    matched_weight = sum(tokens[i][3] for i in range(first_idx, last_idx + 1))

    # Reserve full-segment protection strictly for ultra-short exclamations
    is_short_exclamation = (
        spoken_word_count <= 1
        or duration <= 1.15
        or (spoken_word_count == 2 and duration <= 2.5 and not has_clause_break)
        or (spoken_word_count == 3 and duration <= 2.2 and not has_clause_break)
        or (duration <= 2.2 and (matched_weight / max(0.1, total_spoken_weight)) >= 0.65 and not has_clause_break)
    )
    if is_short_exclamation:
        start = max(0.0, sp_start - padding_before)
        end = sp_end + padding_after
        return start, end, sp_start, sp_end, True

    lead_pause = 0.35 if any(ei <= tokens[0][0] for _, ei in sdh_spans) else 0.06
    trail_pause = 0.25 if any(si >= tokens[-1][1] for si, _ in sdh_spans) else 0.08

    word_unit_spans: List[Tuple[float, float]] = []
    cursor = lead_pause
    for i, (_, _, _, w_units) in enumerate(tokens):
        word_unit_spans.append((cursor, cursor + w_units))
        cursor += w_units
        if i < len(gaps):
            cursor += gaps[i]
    total_units = max(0.1, cursor + trail_pause)

    def u_to_t(u: float) -> float:
        return sp_start + (u / total_units) * duration

    est_start = u_to_t(word_unit_spans[first_idx][0])
    est_end = u_to_t(word_unit_spans[last_idx][1])

    raw_pre = lead_in + padding_before
    raw_post = padding_after
    avg_word_sec = duration / spoken_word_count

    # Start boundary clamping
    if first_idx == 0:
        if lead_pause >= 0.30:
            mute_start = max(0.0, est_start - min(raw_pre, 0.25))
        else:
            mute_start = max(0.0, sp_start - min(padding_before, 0.25))
    else:
        mute_start = est_start - raw_pre
        prev_start_t = u_to_t(word_unit_spans[first_idx - 1][0])
        prev_end_t = u_to_t(word_unit_spans[first_idx - 1][1])
        pre_gap = gaps[first_idx - 1]
        if pre_gap >= 0.55:
            pause_anchor = prev_end_t + 0.20 * (est_start - prev_end_t)
            safety_latest = est_start - max(0.11, min(0.18, raw_pre * 0.55))
            mute_start = min(safety_latest, max(mute_start, pause_anchor - 0.03))
        elif avg_word_sec < 0.48:
            prev_guard = prev_start_t + 0.35 * (prev_end_t - prev_start_t)
            safety_latest = est_start - max(0.14, min(0.20, raw_pre * 0.65))
            mute_start = min(safety_latest, max(mute_start, prev_guard))

    # End boundary clamping
    if last_idx == spoken_word_count - 1:
        if trail_pause >= 0.20:
            mute_end = est_end + min(raw_post, 0.25)
        else:
            mute_end = sp_end + min(padding_after, 0.25)
    else:
        mute_end = est_end + raw_post
        next_start_t = u_to_t(word_unit_spans[last_idx + 1][0])
        next_end_t = u_to_t(word_unit_spans[last_idx + 1][1])
        post_gap = gaps[last_idx]
        if post_gap >= 0.55:
            pause_end_anchor = next_start_t - 0.15 * (next_start_t - est_end)
            safety_earliest = est_end + max(0.10, min(0.16, raw_post * 0.55))
            mute_end = max(safety_earliest, min(mute_end, pause_end_anchor + 0.03))
        elif avg_word_sec < 0.48:
            next_guard = next_start_t + 0.65 * (next_end_t - next_start_t)
            safety_earliest = est_end + max(0.11, min(0.18, raw_post * 0.65))
            mute_end = max(safety_earliest, min(mute_end, next_guard))

    mute_start = max(0.0, min(mute_start, sp_end - 0.15))
    mute_end = max(mute_end, mute_start + min_mute_duration)
    return mute_start, mute_end, est_start, est_end, False


def calculate_word_interval(
    sub: SubtitleItem,
    match: ProfanityMatch,
    padding_before: float = 0.18,
    padding_after: float = 0.16,
    mode: str = "word",
    lead_in: float = 0.10,
    sync_offset: float = 0.0,
    min_mute_duration: float = 0.35,
    speech_bounds: Optional[Tuple[float, float]] = None,
) -> Tuple[float, float]:
    """
    Calculate tightened start and end time (in seconds) for a detected profanity.

    Includes:
    - sync_offset: Shifts subtitle timestamps to compensate for out-of-sync SRTs.
    - Syllable & punctuation-pause weighted interpolation (with speaker/SDH tag awareness).
    - Clause/word boundary clamping so adjacent clean words are preserved when safe.
    - Ultra-short exclamation protection (1-3 word non-paused exclamations or <= 1.15s cues).
    """
    mute_start, mute_end, _, _, _ = _calculate_word_interval_detailed(
        sub=sub,
        match=match,
        padding_before=padding_before,
        padding_after=padding_after,
        mode=mode,
        lead_in=lead_in,
        sync_offset=sync_offset,
        min_mute_duration=min_mute_duration,
        speech_bounds=speech_bounds,
    )
    return mute_start, mute_end


def merge_intervals(intervals: List[Tuple[float, float]], gap_threshold: float = 0.25) -> List[Tuple[float, float]]:
    """
    Merge overlapping intervals and intervals closer than gap_threshold seconds.
    Default 0.25s prevents jarring audio on/off stuttering while keeping clean words audible.
    """
    if not intervals:
        return []

    sorted_intervals = sorted(intervals, key=lambda x: x[0])
    merged = [sorted_intervals[0]]

    for current_start, current_end in sorted_intervals[1:]:
        last_start, last_end = merged[-1]
        if current_start <= last_end + gap_threshold:
            merged[-1] = (last_start, max(last_end, current_end))
        else:
            merged.append((current_start, current_end))

    return merged


def detect_profanities_in_subtitles(
    subtitles: List[SubtitleItem],
    profanity_filter: ProfanityFilter,
    timing_mode: str = "word",
    padding_before: float = 0.18,
    padding_after: float = 0.16,
    lead_in: float = 0.10,
    sync_offset: float = 0.0,
    min_mute_duration: float = 0.35,
    video_path: Optional[str] = None,
    audio_refine: bool = True,
    center_channel_only: bool = True,
) -> List[CensorDetection]:
    """Scan all subtitle items and generate tightened CensorDetection objects."""
    matched_subs: List[Tuple[SubtitleItem, List[ProfanityMatch]]] = []
    for sub in subtitles:
        matches = profanity_filter.find_matches(sub.clean_text)
        if matches:
            matched_subs.append((sub, matches))

    if not matched_subs:
        return []

    envelopes: List[AudioEnvelopeWindow] = []
    if audio_refine and video_path and timing_mode != "segment":
        cue_spans = [
            (
                max(0.0, s.start_time + sync_offset),
                max(max(0.0, s.start_time + sync_offset) + 0.1, s.end_time + sync_offset),
            )
            for s, _ in matched_subs
        ]
        envelopes = extract_speech_envelopes(
            video_path=video_path,
            cue_spans=cue_spans,
            center_channel_only=center_channel_only,
        )

    detections: List[CensorDetection] = []
    det_counter = 1

    for sub, matches in matched_subs:
        adj_start = max(0.0, sub.start_time + sync_offset)
        adj_end = max(adj_start + 0.1, sub.end_time + sync_offset)

        env = find_covering_window(envelopes, adj_start, adj_end) if envelopes else None
        speech_bounds = (
            measure_cue_speech_bounds(env, adj_start, adj_end, _count_spoken_words(sub.clean_text))
            if env is not None else None
        )

        for m in matches:
            mute_start, mute_end, est_start, est_end, is_full_seg = _calculate_word_interval_detailed(
                sub=sub,
                match=m,
                padding_before=padding_before,
                padding_after=padding_after,
                mode=timing_mode,
                lead_in=lead_in,
                sync_offset=sync_offset,
                min_mute_duration=min_mute_duration,
                speech_bounds=speech_bounds,
            )

            if env is not None and not is_full_seg:
                mute_start, mute_end = snap_interval_to_valleys(
                    env=env,
                    mute_start=mute_start,
                    mute_end=mute_end,
                    est_start=est_start,
                    est_end=est_end,
                    sub_start=adj_start,
                    sub_end=adj_end,
                    min_mute_duration=min_mute_duration,
                )

            detections.append(CensorDetection(
                id=f"det_{det_counter}",
                subtitle_index=sub.index,
                word=m.word,
                context_text=sub.clean_text,
                subtitle_start=adj_start,
                subtitle_end=max(0.0, sub.end_time + sync_offset),
                mute_start=mute_start,
                mute_end=mute_end,
                tier=m.tier,
                enabled=True
            ))
            det_counter += 1

    return detections
