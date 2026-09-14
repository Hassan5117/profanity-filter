#!/usr/bin/env python3
"""
mute_audio_from_srt.py - Mute profanity in videos based on SRT subtitles.

Preserves 100% backward compatibility with original command syntax:
    python3 mute_audio_from_srt.py -i input.mp4 -s input.srt -o output.mp4 [-w word1 word2]

Powered by the robust censor_app engine:
- Smooth volume fades (eliminating audio pops/clicks)
- Optional 1000Hz bleep tone or audio ducking (--mode bleep|duck|mute)
- Smart word-level timing (doesn't wipe out whole sentences)
- 5.1 surround center-channel dialogue isolation
- Automatic sanitized subtitle (.Cleaned.srt) generation
"""

import os
import sys
import argparse
from pathlib import Path

# Ensure censor_app package can be imported even if script is invoked from another working directory
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from censor_app.core.srt_parser import parse_srt, extract_embedded_subtitle, probe_subtitle_streams
from censor_app.core.profanity_filter import ProfanityFilter, TIER_1_SEVERE, TIER_2_MODERATE
from censor_app.core.timing import detect_profanities_in_subtitles, merge_intervals
from censor_app.core.subtitle_writer import generate_cleaned_srt
from censor_app.core.ffmpeg_engine import FFmpegEngine, FFmpegProbe


DEFAULT_PROFANITY = TIER_1_SEVERE + TIER_2_MODERATE


def main():
    parser = argparse.ArgumentParser(
        description="Mute profanity in a video based on SRT subtitles (censor_app engine)."
    )
    parser.add_argument('-i', '--input', required=True, help="Input video file path")
    parser.add_argument('-s', '--srt', required=False, help="Input SRT file path (optional if embedded in video)")
    parser.add_argument('-o', '--output', required=True, help="Output video file path")
    parser.add_argument('-w', '--words', nargs='+', default=None,
                        help="List of words to mute. If not provided, standard profanity list is used.")
    parser.add_argument('--mode', choices=['mute', 'bleep', 'duck'], default='mute',
                        help="Censorship mode: mute (smooth silence), bleep (1000Hz tone), or duck (-24dB)")
    parser.add_argument('--preset', choices=['strict', 'moderate', 'mild', 'all'], default='moderate',
                        help="Profanity severity tier (default: moderate)")
    parser.add_argument('--timing-mode', choices=['word', 'segment'], default='word',
                        help="Targeted word timing vs full subtitle segment (default: word)")
    parser.add_argument('--padding', type=float, default=0.30,
                        help="Padding in seconds around word (default: 0.30)")
    parser.add_argument('--lead-in', type=float, default=0.20,
                        help="Mute onset lead-in in seconds (default: 0.20)")
    parser.add_argument('--sync-offset', type=float, default=0.0,
                        help="Global subtitle timing offset in seconds (+/-)")
    parser.add_argument('--no-clean-srt', action='store_true',
                        help="Skip generating sanitized .srt file")
    parser.add_argument('--no-center-only', action='store_true',
                        help="Do not isolate Center dialogue channel on 5.1 surround")

    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"Error: Input video '{args.input}' not found.")
        sys.exit(1)

    srt_path = args.srt
    temp_srt = None

    if not srt_path or not os.path.exists(srt_path):
        candidate = Path(args.input).with_suffix(".srt")
        if candidate.exists():
            srt_path = str(candidate)
            print(f"[*] Auto-detected matching subtitle file: {srt_path}")
        else:
            streams = probe_subtitle_streams(args.input)
            if streams:
                print(f"[*] Extracting embedded subtitle track (codec: {streams[0]['codec']})...")
                temp_srt = extract_embedded_subtitle(args.input, stream_index=0)
                srt_path = temp_srt
            else:
                print(f"Error: SRT file '{args.srt}' not found and no embedded subtitles detected in video.")
                sys.exit(1)

    try:
        # Step 1: Parse subtitles
        subs = parse_srt(srt_path)
        if not subs:
            print("[*] Subtitle file contains no valid entries. Copying video without changes.")
            engine = FFmpegEngine()
            engine.run_censor(args.input, args.output, intervals=[])
            return

        # Step 2: Configure filter
        custom_words = args.words if args.words else []
        pf = ProfanityFilter(preset=args.preset, custom_words=custom_words)

        detections = detect_profanities_in_subtitles(
            subtitles=subs,
            profanity_filter=pf,
            timing_mode=args.timing_mode,
            padding_before=args.padding,
            padding_after=args.padding,
            lead_in=args.lead_in,
            sync_offset=args.sync_offset
        )

        if not detections:
            print("No profanity found in the SRT file. No action needed.")
            engine = FFmpegEngine()
            engine.run_censor(args.input, args.output, intervals=[])
            return

        intervals = [(d.mute_start, d.mute_end) for d in detections]
        merged = merge_intervals(intervals)

        print(f"Found {len(detections)} profanities across {len(merged)} mute segments.")
        print(f"Censorship Mode: {args.mode.upper()}")

        def print_progress(p_data):
            pct = p_data.get("percent", 0.0)
            sys.stdout.write(f"\r[FFmpeg] Progress: {pct:.1f}% ({p_data.get('current_time', 0.0):.1f}s / {p_data.get('total_duration', 0.0):.1f}s)")
            sys.stdout.flush()

        engine = FFmpegEngine()
        success = engine.run_censor(
            input_video=args.input,
            output_video=args.output,
            intervals=merged,
            mode=args.mode,
            center_channel_only=not args.no_center_only,
            progress_callback=print_progress
        )

        print()  # Newline after progress
        if success:
            print(f"\nSuccess! Saved to: {args.output}")
            if not args.no_clean_srt:
                out_srt = str(Path(args.output).with_suffix(".srt"))
                generate_cleaned_srt(subs, pf, out_srt)
                print(f"Sanitized subtitles saved to: {out_srt}")
        else:
            print("\nAn error occurred while running FFmpeg.")
            sys.exit(1)

    finally:
        if temp_srt and os.path.exists(temp_srt):
            try:
                os.remove(temp_srt)
            except OSError:
                pass


if __name__ == "__main__":
    main()
