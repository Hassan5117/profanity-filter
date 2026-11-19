import os
import signal
import sys
import argparse
import threading
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
from censor_app.core.subtitle_fetcher import (
    SubtitleFetcher,
    build_subtitle_output_path,
    normalize_language_codes,
    parse_media_query,
)
from censor_app.server.watcher import MediaWatcher


def _find_external_subtitle(video_path: str, language: str = "en"):
    p = Path(video_path)
    iso1, iso2 = normalize_language_codes(language)
    extensions = []
    for ext in (".srt", f".{iso1}.srt", f".{iso2}.srt", ".en.srt", ".eng.srt", ".vtt", f".{iso1}.vtt", ".en.vtt"):
        if ext not in extensions:
            extensions.append(ext)
    for ext in extensions:
        candidate = p.parent / f"{p.stem}{ext}"
        if candidate.exists() and candidate.is_file() and candidate.stat().st_size > 0:
            return str(candidate)
    return None


def _resolve_subtitle_for_video(
    video_path: str,
    explicit_srt: str = None,
    sub_lang: str = None,
    auto_download: bool = None,
):
    if explicit_srt and os.path.exists(explicit_srt):
        return explicit_srt, None
    cfg = Config()
    lang = sub_lang or cfg.get("subtitle_language", "en") or "en"
    external = _find_external_subtitle(video_path, language=lang)
    if external:
        return external, None
    streams = probe_subtitle_streams(video_path, text_only=True)
    if streams:
        sel = next((s for s in streams if s.get("language", "").lower() in ("eng", "en")), streams[0])
        print(f"[*] Extracting embedded text subtitle track #{sel['sub_index']} (codec: {sel['codec']}, lang: {sel.get('language', 'und')})...")
        temp_srt = extract_embedded_subtitle(video_path, stream_index=sel["sub_index"])
        return temp_srt, temp_srt

    should_download = cfg.get("auto_download_subtitles", True) if auto_download is None else auto_download
    if should_download:
        print(f"[*] No local or embedded text subtitles found; searching trusted online sources ({lang})...")
        fetcher = SubtitleFetcher(config=cfg, logger=print)
        result = fetcher.auto_fetch_subtitle_for_video(video_path, language=lang)
        if result:
            print(f"[*] Downloaded subtitle from {result.provider} ({result.cue_count} cues) -> {result.srt_path}")
            return result.srt_path, None

    return None, None


def run_scan_command(args):
    video_path = args.video
    if not os.path.exists(video_path):
        print(f"[Error] Video not found: {video_path}")
        sys.exit(1)

    srt_path, temp_srt = _resolve_subtitle_for_video(
        video_path,
        args.srt,
        sub_lang=getattr(args, "sub_lang", None),
        auto_download=False if getattr(args, "no_download_subs", False) else None,
    )
    if not srt_path:
        print("[Error] No external, embedded, or downloadable subtitles found. Please provide an external --srt file.")
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
            sync_offset=args.sync_offset,
            video_path=video_path,
            audio_refine=not args.no_audio_refine,
            center_channel_only=not args.no_center_only,
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

        print(f"{'TIMECODE':<21} | {'SEVERITY':<10} | {'WORD':<14} | {'CONTEXT'}")
        print("-" * 60)
        for d in detections:
            tc = f"{format_timestamp(d.mute_start):<12} ({d.mute_end - d.mute_start:.2f}s)"
            print(f"{tc:<21} | {d.tier:<10} | {d.word:<14} | {d.context_text[:45]}...")

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

    if Path(video_path).resolve() == Path(out_video).resolve():
        print("[Error] Output video path must be different from input video path.")
        sys.exit(1)

    srt_path, temp_srt = _resolve_subtitle_for_video(
        video_path,
        args.srt,
        sub_lang=getattr(args, "sub_lang", None),
        auto_download=False if getattr(args, "no_download_subs", False) else None,
    )
    if not srt_path:
        print("[Error] No external, embedded, or downloadable subtitles found. Please provide an external --srt file.")
        sys.exit(1)

    try:
        cfg = Config()
        subs = parse_srt(srt_path)
        pf = ProfanityFilter(preset=args.preset, custom_words=args.words or [])
        detections = detect_profanities_in_subtitles(
            subtitles=subs,
            profanity_filter=pf,
            timing_mode=args.timing_mode,
            padding_before=args.padding,
            padding_after=args.padding,
            lead_in=args.lead_in,
            sync_offset=args.sync_offset,
            video_path=video_path,
            audio_refine=not args.no_audio_refine,
            center_channel_only=not args.no_center_only,
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
            ducking_vol=cfg.get("ducking_volume", 0.06),
            bleep_freq=cfg.get("bleep_frequency", 1000),
            crossfade_duration=cfg.get("crossfade_duration", 0.04),
            progress_callback=print_progress
        )

        print()  # Newline after progress bar
        if success:
            print(f"[✓] Success! Cleaned video saved to: {out_video}")
            if not args.no_clean_srt:
                out_srt = str(Path(out_video).with_suffix(".srt"))
                generate_cleaned_srt(subs, pf, out_srt, mask_char=cfg.get("censor_char", "*"))
                print(f"[✓] Cleaned subtitles saved to: {out_srt}")
        else:
            err = f": {engine.last_error}" if engine.last_error else "."
            print(f"[Error] Processing failed{err}")
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
    deps = FFmpegProbe.check_dependencies()
    if not deps["ok"]:
        print(
            f"[Warning] FFmpeg / ffprobe not found in PATH. "
            f"Install it before scanning/exporting: {deps['install_hint']}"
        )
    port = args.port
    host = args.host
    server = run_studio_server(host=host, port=port)
    actual_port = server.server_port
    if actual_port != port:
        print(f"[Info] Port {port} was busy; using port {actual_port} instead.")
    url = f"http://{host}:{actual_port}"
    print(f"\n=======================================================")
    print(f"🎬 Movie Censor Studio running at: {url}")
    print(f"Close this window or click Stop Studio in the browser to quit.")
    print(f"=======================================================\n")

    def _handle_sigterm(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    try:
        signal.signal(signal.SIGTERM, _handle_sigterm)
    except (ValueError, OSError, AttributeError):
        pass

    if not args.no_browser:
        timer = threading.Timer(0.35, webbrowser.open, args=[url])
        timer.daemon = True
        timer.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down Studio server...")
        server.shutdown()
    finally:
        server.server_close()


def run_subtitles_command(args):
    if not args.video and not args.title and not args.imdb_id:
        print("[Error] Provide a video file path, --title, or --imdb-id to search subtitles.")
        sys.exit(1)

    cfg = Config()
    lang = args.lang or cfg.get("subtitle_language", "en") or "en"
    query = parse_media_query(
        video_path=args.video,
        title=args.title,
        year=args.year,
        season=args.season,
        episode=args.episode,
        imdb_id=args.imdb_id,
        language=lang,
        compute_hash=bool(args.video and os.path.isfile(args.video)),
    )

    print(
        f"[*] Searching trusted subtitle providers for '{query.title or query.imdb_id}' "
        f"(year={query.year or '-'}, lang={query.language}, hash={query.movie_hash or '-'})..."
    )
    fetcher = SubtitleFetcher(config=cfg, logger=print)
    candidates = fetcher.search(query, limit=15)
    if not candidates:
        print("[Error] No matching subtitles found from trusted providers.")
        sys.exit(1)

    print(f"\n{'#':<3} | {'PROVIDER':<21} | {'SCORE':<6} | {'LANG':<4} | {'HASH':<4} | {'DL':<7} | {'RELEASE'}")
    print("-" * 86)
    for idx, c in enumerate(candidates, 1):
        hash_flag = "YES" if c.hash_matched else "no"
        rel = (c.release_name or c.title or "-")[:38]
        print(f"{idx:<3} | {c.provider:<21} | {c.score:<6.1f} | {c.language:<4} | {hash_flag:<4} | {c.downloads:<7} | {rel}")

    if args.search_only:
        return

    pick_idx = max(1, int(args.pick or 1)) - 1
    if pick_idx >= len(candidates):
        print(f"[Error] Requested --pick {args.pick} exceeds {len(candidates)} results.")
        sys.exit(1)

    selected = candidates[pick_idx]
    if args.output:
        dest_path = args.output
    elif args.video:
        dest_path = str(build_subtitle_output_path(args.video, language=query.language))
    else:
        slug = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in (query.title or query.imdb_id or "subtitle"))
        dest_path = str(Path.cwd() / f"{slug}.{query.language}.srt")

    try:
        res = fetcher.download_candidate(selected, dest_srt_path=dest_path, query=query)
        print(f"\n[*] Saved {res.cue_count} cues from {res.provider} -> {res.srt_path}")
    except Exception as e:
        print(f"\n[Error] Failed to download selected subtitle: {e}")
        sys.exit(1)


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
    scan_p.add_argument("--preset", choices=["strict", "moderate", "mild", "all", "custom"], default="moderate")
    scan_p.add_argument("-w", "--words", nargs="+", help="Additional custom words to detect")
    scan_p.add_argument("--timing-mode", choices=["word", "smart", "segment"], default="word")
    scan_p.add_argument("--padding", type=float, default=0.18, help="Padding in seconds around word (default: 0.18)")
    scan_p.add_argument("--lead-in", type=float, default=0.10, help="Mute onset lead-in in seconds (default: 0.10)")
    scan_p.add_argument("--sync-offset", type=float, default=0.0, help="Subtitle sync offset in seconds (+/-)")
    scan_p.add_argument("--no-audio-refine", action="store_true", help="Disable acoustic waveform speech/pause alignment")
    scan_p.add_argument("--no-center-only", action="store_true", help="Do not isolate Center dialogue channel on 5.1 surround")
    scan_p.add_argument("--sub-lang", help="Preferred subtitle language for online fallback (default from config)")
    scan_p.add_argument("--no-download-subs", action="store_true", help="Disable automatic online subtitle download when missing")

    # process
    proc_p = subparsers.add_parser("process", help="Censor a movie and output cleaned video")
    proc_p.add_argument("video", help="Input video file path")
    proc_p.add_argument("-o", "--output", help="Output video file path (defaults to alongside with .Cleaned.mp4)")
    proc_p.add_argument("-s", "--srt", help="Input subtitle file path (optional if embedded)")
    proc_p.add_argument("--mode", choices=["mute", "bleep", "duck"], default="mute", help="Censorship mode (default: mute)")
    proc_p.add_argument("--preset", choices=["strict", "moderate", "mild", "all", "custom"], default="moderate")
    proc_p.add_argument("-w", "--words", nargs="+", help="Additional custom words to detect")
    proc_p.add_argument("--timing-mode", choices=["word", "smart", "segment"], default="word")
    proc_p.add_argument("--padding", type=float, default=0.18, help="Padding in seconds (default: 0.18)")
    proc_p.add_argument("--lead-in", type=float, default=0.10, help="Mute onset lead-in in seconds (default: 0.10)")
    proc_p.add_argument("--sync-offset", type=float, default=0.0, help="Subtitle sync offset in seconds (+/-)")
    proc_p.add_argument("--no-audio-refine", action="store_true", help="Disable acoustic waveform speech/pause alignment")
    proc_p.add_argument("--no-center-only", action="store_true", help="Do not isolate Center dialogue channel on 5.1 surround")
    proc_p.add_argument("--no-clean-srt", action="store_true", help="Do not generate sanitized .srt file")
    proc_p.add_argument("--sub-lang", help="Preferred subtitle language for online fallback (default from config)")
    proc_p.add_argument("--no-download-subs", action="store_true", help="Disable automatic online subtitle download when missing")

    # subtitles
    sub_p = subparsers.add_parser("subtitles", help="Search and download subtitles from trusted providers")
    sub_p.add_argument("video", nargs="?", help="Input video file path (optional if --title or --imdb-id given)")
    sub_p.add_argument("-t", "--title", help="Movie or TV show title override")
    sub_p.add_argument("--year", type=int, help="Release year")
    sub_p.add_argument("--season", type=int, help="TV season number")
    sub_p.add_argument("--episode", type=int, help="TV episode number")
    sub_p.add_argument("--imdb-id", help="IMDb ID (e.g. tt0133093)")
    sub_p.add_argument("--lang", help="Preferred subtitle language (e.g. en, es, fr)")
    sub_p.add_argument("-o", "--output", help="Destination .srt path (defaults alongside video)")
    sub_p.add_argument("--search-only", action="store_true", help="List matches without downloading")
    sub_p.add_argument("--pick", type=int, default=1, help="1-based ranked match to download (default: 1)")

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
    elif args.subcommand == "subtitles":
        run_subtitles_command(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
