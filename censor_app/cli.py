import os
import sys
import argparse
import webbrowser
from pathlib import Path

from censor_app.core.config import Config
from censor_app.core.srt_parser import (
    parse_srt, probe_subtitle_streams, extract_embedded_subtitle, format_timestamp
)
from censor_app.core.profanity_filter import ProfanityFilter
from censor_app.core.timing import detect_profanities_in_subtitles, merge_intervals
from censor_app.core.subtitle_writer import generate_cleaned_srt
from censor_app.core.ffmpeg_engine import FFmpegProbe, FFmpegEngine
from censor_app.server.watcher import MediaWatcher


def run_scan_command(args):
    video_path = args.video
    if not os.path.exists(video_path):
        print(f"[Error] Video not found: {video_path}")
        sys.exit(1)

    srt_path = args.srt
    temp_srt = None
    if not srt_path or not os.path.exists(srt_path):
        candidate = Path(video_path).with_suffix(".srt")
        if candidate.exists():
            srt_path = str(candidate)
        else:
            streams = probe_subtitle_streams(video_path)
            if streams:
                print(f"[*] Extracting embedded subtitle track (codec: {streams[0]['codec']})...")
                temp_srt = extract_embedded_subtitle(video_path, stream_index=0)
                srt_path = temp_srt
            else:
                print("[Error] No external or embedded subtitles found. Please provide --srt.")
                sys.exit(1)

    try:
        subs = parse_srt(srt_path)
        pf = ProfanityFilter(preset=args.preset, custom_words=args.words or [])
        detections = detect_profanities_in_subtitles(
            subtitles=subs,
            profanity_filter=pf,
            timing_mode=args.timing_mode,
            padding_before=args.padding,
            padding_after=args.padding,
            lead_in=args.lead_in,
            sync_offset=args.sync_offset
        )

        media_info = FFmpegProbe.probe_media(video_path)
        print("\n" + "=" * 60)
        print(f"🎬 Movie: {os.path.basename(video_path)}")
        print(f"⏱️  Duration: {format_timestamp(media_info.get('duration', 0.0))}")
        print(f"🔊 Audio Layout: {media_info.get('channel_layout', 'stereo')} (Surround: {'Yes' if media_info.get('is_surround') else 'No'})")
        print(f"⚠️  Detections: {len(detections)} profanities found")
        print("=" * 60)

        if not detections:
            print("No profanities detected.")
            return

        print(f"{'TIMECODE':<14} | {'SEVERITY':<10} | {'WORD':<14} | {'CONTEXT'}")
        print("-" * 60)
        for d in detections:
            tc = format_timestamp(d.mute_start)
            print(f"{tc:<14} | {d.tier:<10} | {d.word:<14} | {d.context_text[:45]}...")

        intervals = [(d.mute_start, d.mute_end) for d in detections]
        merged = merge_intervals(intervals)
        total_mute_time = sum(e - s for s, e in merged)
        print("-" * 60)
        print(f"Total mute time across {len(merged)} combined intervals: {total_mute_time:.2f}s\n")
    finally:
        if temp_srt and os.path.exists(temp_srt):
            try:
                os.remove(temp_srt)
            except OSError:
                pass


def run_process_command(args):
    video_path = args.video
    if not os.path.exists(video_path):
        print(f"[Error] Video not found: {video_path}")
        sys.exit(1)

    out_video = args.output
    if not out_video:
        p = Path(video_path)
        out_video = str(p.parent / f"{p.stem}.Cleaned{p.suffix}")

    srt_path = args.srt
    temp_srt = None
    if not srt_path or not os.path.exists(srt_path):
        candidate = Path(video_path).with_suffix(".srt")
        if candidate.exists():
            srt_path = str(candidate)
        else:
            streams = probe_subtitle_streams(video_path)
            if streams:
                print(f"[*] Extracting embedded subtitle track (codec: {streams[0]['codec']})...")
                temp_srt = extract_embedded_subtitle(video_path, stream_index=0)
                srt_path = temp_srt
            else:
                print("[Error] No external or embedded subtitles found. Please provide --srt.")
                sys.exit(1)

    try:
        subs = parse_srt(srt_path)
        pf = ProfanityFilter(preset=args.preset, custom_words=args.words or [])
        detections = detect_profanities_in_subtitles(
            subtitles=subs,
            profanity_filter=pf,
            timing_mode=args.timing_mode,
            padding_before=args.padding,
            padding_after=args.padding,
            lead_in=args.lead_in,
            sync_offset=args.sync_offset
        )

        intervals = [(d.mute_start, d.mute_end) for d in detections]
        merged = merge_intervals(intervals)

        print(f"[*] Found {len(detections)} profanities ({len(merged)} mute segments).")
        print(f"[*] Censorship mode: {args.mode.upper()}")
        print(f"[*] Rendering to: {out_video}")

        def print_progress(p_data):
            pct = p_data.get("percent", 0.0)
            sys.stdout.write(f"\r[FFmpeg] Progress: {pct:.1f}% ({p_data.get('current_time', 0.0):.1f}s / {p_data.get('total_duration', 0.0):.1f}s)")
            sys.stdout.flush()

        engine = FFmpegEngine()
        success = engine.run_censor(
            input_video=video_path,
            output_video=out_video,
            intervals=merged,
            mode=args.mode,
            center_channel_only=not args.no_center_only,
            progress_callback=print_progress
        )

        print()  # Newline after progress bar
        if success:
            print(f"[✓] Success! Cleaned video saved to: {out_video}")
            if not args.no_clean_srt:
                out_srt = str(Path(out_video).with_suffix(".srt"))
                generate_cleaned_srt(subs, pf, out_srt)
                print(f"[✓] Cleaned subtitles saved to: {out_srt}")
        else:
            print("[Error] Processing failed.")
            sys.exit(1)
    finally:
        if temp_srt and os.path.exists(temp_srt):
            try:
                os.remove(temp_srt)
            except OSError:
                pass


def run_watch_command(args):
    watch_dir = args.directory
    output_dir = args.output_dir
    config = Config()
    if args.mode:
        config.set("mode", args.mode)
    if output_dir:
        config.set("output_dir", output_dir)

    watcher = MediaWatcher(watch_dir=watch_dir, output_dir=output_dir, config=config)
    try:
        watcher.run_forever()
    except KeyboardInterrupt:
        print("\n[MediaWatcher] Shutting down...")
        watcher.stop()


def run_studio_command(args):
    from censor_app.web.server import run_studio_server
    port = args.port
    host = args.host
    server = run_studio_server(host=host, port=port)
    url = f"http://{host}:{port}"
    print(f"\n=======================================================")
    print(f"🎬 Movie Censor Studio running at: {url}")
    print(f"Press Ctrl+C to stop the server.")
    print(f"=======================================================\n")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down Studio server...")
        server.shutdown()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="censor_app",
        description="Movie Censor Studio: Intelligent Profanity Muting & Media Server Automation."
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="Available subcommands")

    # studio
    studio_p = subparsers.add_parser("studio", help="Launch interactive Web Studio UI")
    studio_p.add_argument("--port", type=int, default=8000, help="Web server port (default: 8000)")
    studio_p.add_argument("--host", default="127.0.0.1", help="Web server host (default: 127.0.0.1)")
    studio_p.add_argument("--no-browser", action="store_true", help="Do not automatically open browser")

    # scan
    scan_p = subparsers.add_parser("scan", help="Scan a movie and print detected profanities")
    scan_p.add_argument("video", help="Input video file path")
    scan_p.add_argument("-s", "--srt", help="Input subtitle file path (optional if embedded)")
    scan_p.add_argument("--preset", choices=["strict", "moderate", "mild", "all"], default="moderate")
    scan_p.add_argument("-w", "--words", nargs="+", help="Additional custom words to detect")
    scan_p.add_argument("--timing-mode", choices=["word", "segment"], default="word")
    scan_p.add_argument("--padding", type=float, default=0.30, help="Padding in seconds around word (default: 0.30)")
    scan_p.add_argument("--lead-in", type=float, default=0.20, help="Mute onset lead-in in seconds (default: 0.20)")
    scan_p.add_argument("--sync-offset", type=float, default=0.0, help="Subtitle sync offset in seconds (+/-)")

    # process
    proc_p = subparsers.add_parser("process", help="Censor a movie and output cleaned video")
    proc_p.add_argument("video", help="Input video file path")
    proc_p.add_argument("-o", "--output", help="Output video file path (defaults to alongside with .Cleaned.mp4)")
    proc_p.add_argument("-s", "--srt", help="Input subtitle file path (optional if embedded)")
    proc_p.add_argument("--mode", choices=["mute", "bleep", "duck"], default="mute", help="Censorship mode (default: mute)")
    proc_p.add_argument("--preset", choices=["strict", "moderate", "mild", "all"], default="moderate")
    proc_p.add_argument("-w", "--words", nargs="+", help="Additional custom words to detect")
    proc_p.add_argument("--timing-mode", choices=["word", "segment"], default="word")
    proc_p.add_argument("--padding", type=float, default=0.30, help="Padding in seconds (default: 0.30)")
    proc_p.add_argument("--lead-in", type=float, default=0.20, help="Mute onset lead-in in seconds (default: 0.20)")
    proc_p.add_argument("--sync-offset", type=float, default=0.0, help="Subtitle sync offset in seconds (+/-)")
    proc_p.add_argument("--no-center-only", action="store_true", help="Do not isolate Center dialogue channel on 5.1 surround")
    proc_p.add_argument("--no-clean-srt", action="store_true", help="Do not generate sanitized .srt file")

    # watch
    watch_p = subparsers.add_parser("watch", help="Run folder watcher daemon for media server automation")
    watch_p.add_argument("directory", help="Directory path to monitor for incoming media")
    watch_p.add_argument("-o", "--output-dir", help="Separate destination directory for cleaned copies (leaves original untouched)")
    watch_p.add_argument("--mode", choices=["mute", "bleep", "duck"], help="Censorship mode override")

    return parser


def main():
    parser = build_parser()
    if len(sys.argv) == 1:
        # Default to studio if no args passed
        run_studio_command(argparse.Namespace(port=8000, host="127.0.0.1", no_browser=False))
        return

    args = parser.parse_args()
    if args.subcommand == "studio":
        run_studio_command(args)
    elif args.subcommand == "scan":
        run_scan_command(args)
    elif args.subcommand == "process":
        run_process_command(args)
    elif args.subcommand == "watch":
        run_watch_command(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
