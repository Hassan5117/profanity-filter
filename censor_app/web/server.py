import os
import sys
import json
import hashlib
import mimetypes
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Optional, Dict, Any, Set
from http import HTTPStatus
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

from censor_app.core.config import Config
from censor_app.core.srt_parser import (
    parse_srt, probe_subtitle_streams, extract_embedded_subtitle, SubtitleItem
)
from censor_app.core.profanity_filter import ProfanityFilter
from censor_app.core.timing import detect_profanities_in_subtitles, merge_intervals
from censor_app.core.subtitle_writer import generate_cleaned_srt
from censor_app.core.ffmpeg_engine import FFmpegProbe, FFmpegEngine
from censor_app.core.subtitle_fetcher import (
    SubtitleFetcher,
    SubtitleCandidate,
    build_subtitle_output_path,
    is_trusted_https_url,
    normalize_language_codes,
    parse_media_query,
)

STATIC_DIR = Path(__file__).parent / "static"
UPLOAD_DIR = Path.home() / ".cache" / "censor_app" / "uploads"

ALLOWED_VIDEO_EXTENSIONS: Set[str] = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}
ALLOWED_STREAM_EXTENSIONS: Set[str] = ALLOWED_VIDEO_EXTENSIONS | {
    ".srt", ".vtt", ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"
}

# Global state for active processing job
job_lock = threading.Lock()
active_engine: Optional[FFmpegEngine] = None
active_job_id: int = 0
current_job: Dict[str, Any] = {
    "status": "idle",       # "idle", "processing", "completed", "failed"
    "percent": 0.0,
    "current_time": 0.0,
    "total_duration": 0.0,
    "output_path": "",
    "cleaned_srt_path": "",
    "error": ""
}


def resolve_file_path(path_str: str) -> Optional[str]:
    if not path_str:
        return None
    p = Path(path_str).expanduser()
    if p.exists() and p.is_file():
        return str(p.resolve())

    # Try relative to CWD
    cwd_cand = Path.cwd() / path_str
    if cwd_cand.exists() and cwd_cand.is_file():
        return str(cwd_cand.resolve())

    # Check common user directories if only filename was provided by the browser
    filename = Path(path_str).name
    candidate_dirs = [
        Path.cwd(),
        Path.home() / "Downloads",
        Path.home() / "Movies",
        Path.home() / "Desktop",
        Path.home() / "Documents",
        UPLOAD_DIR,
        Path.home() / ".cache" / "censor_app" / "uploads",
    ]
    for d in candidate_dirs:
        try:
            cand = d / filename
            if cand.exists() and cand.is_file():
                return str(cand.resolve())
        except OSError:
            continue

    return None

class StudioRequestHandler(BaseHTTPRequestHandler):
    server_version = "CensorAppStudio/1.0"
    subtitle_fetcher_factory = staticmethod(SubtitleFetcher)
    _SECRET_CONFIG_KEYS = ("opensubtitles_api_key", "opensubtitles_password", "subdl_api_key")

    def _create_subtitle_fetcher(self, cfg: Config) -> SubtitleFetcher:
        factory = type(self).__dict__.get("subtitle_fetcher_factory")
        if factory is None:
            for cls in type(self).__mro__:
                if "subtitle_fetcher_factory" in cls.__dict__:
                    factory = cls.__dict__["subtitle_fetcher_factory"]
                    break
        if isinstance(factory, staticmethod):
            factory = factory.__func__
        if factory is None:
            factory = SubtitleFetcher
        return factory(config=cfg)

    def log_message(self, format, *args):
        # Keep console output clean
        pass

    def _get_validated_origin(self) -> Optional[str]:
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return None
        if origin == "null":
            return ""
        parsed_origin = urlparse(origin)
        origin_netloc = parsed_origin.netloc.lower()
        origin_host = (parsed_origin.hostname or "").lower()
        host_header = (self.headers.get("Host") or "").strip().lower()
        if host_header:
            return origin if origin_netloc == host_header else ""
        return origin if origin_host in ("127.0.0.1", "localhost", "::1") else ""

    def _send_security_headers(self, valid_origin: Optional[str] = None):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Vary", "Sec-Fetch-Dest, Sec-Fetch-Mode, Sec-Fetch-Site, Origin")
        if valid_origin:
            self.send_header("Access-Control-Allow-Origin", valid_origin)

    def _send_forbidden(self, message: str):
        body = json.dumps({"error": message}).encode("utf-8")
        self.send_response(HTTPStatus.FORBIDDEN)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _enforce_same_origin(self, is_navigation_get: bool = False) -> bool:
        sec_fetch_site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        sec_fetch_mode = (self.headers.get("Sec-Fetch-Mode") or "").strip().lower()
        if sec_fetch_site and sec_fetch_site not in ("same-origin", "none"):
            if not (is_navigation_get and sec_fetch_mode == "navigate"):
                self._send_forbidden("Cross-site request blocked")
                return False

        if self._get_validated_origin() == "":
            self._send_forbidden("Cross-origin request blocked")
            return False

        return True

    def do_OPTIONS(self):
        if not self._enforce_same_origin():
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_security_headers(self._get_validated_origin())
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Range")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        is_nav = path in ("/", "/index.html")
        if not self._enforce_same_origin(is_navigation_get=is_nav):
            return

        if path == "/" or path == "/index.html":
            self.serve_static_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif path == "/style.css":
            self.serve_static_file(STATIC_DIR / "style.css", "text/css; charset=utf-8")
        elif path == "/app.js":
            self.serve_static_file(STATIC_DIR / "app.js", "application/javascript; charset=utf-8")
        elif path == "/api/progress":
            self.handle_get_progress()
        elif path == "/api/config":
            self.handle_get_config()
        elif path == "/api/stream":
            self.handle_stream_media(query)
        elif path == "/api/browse":
            self.handle_browse(query)
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")

    def do_POST(self):
        if not self._enforce_same_origin(is_navigation_get=False):
            return

        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/upload":
            query = parse_qs(parsed.query)
            self.handle_upload(query)
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b"{}"
        
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            payload = {}

        if path == "/api/scan":
            self.handle_scan(payload)
        elif path == "/api/process":
            self.handle_process(payload)
        elif path == "/api/cancel":
            self.handle_cancel()
        elif path == "/api/reveal":
            self.handle_reveal(payload)
        elif path == "/api/config":
            self.handle_save_config(payload)
        elif path == "/api/subtitles/search":
            self.handle_subtitle_search(payload)
        elif path == "/api/subtitles/download":
            self.handle_subtitle_download(payload)
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "Endpoint not found")

    def handle_upload(self, query: dict):
        raw_filename = query.get("filename", ["uploaded_media"])[0]
        filename = os.path.basename(unquote(raw_filename))
        if not filename or filename in (".", ".."):
            filename = "uploaded_media.mp4"

        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        target_path = UPLOAD_DIR / filename

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length <= 0:
            self.send_json_response({"error": "Empty upload"}, status=HTTPStatus.BAD_REQUEST)
            return

        remaining = content_length
        chunk_size = 1024 * 1024
        try:
            with open(target_path, "wb") as f:
                while remaining > 0:
                    to_read = min(remaining, chunk_size)
                    chunk = self.rfile.read(to_read)
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)

            self.send_json_response({
                "status": "uploaded",
                "path": str(target_path.resolve()),
                "filename": filename,
                "size": target_path.stat().st_size
            })
        except Exception as e:
            self.send_json_response({"error": f"Upload failed: {e}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def serve_static_file(self, file_path: Path, content_type: str):
        if not file_path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "Static file not found")
            return
        content = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(content)

    def _public_config_dict(self, config: Config) -> Dict[str, Any]:
        data = dict(config.to_dict())
        for key in self._SECRET_CONFIG_KEYS:
            has_val = bool((data.get(key) or "").strip())
            data[key] = ""
            data[f"has_{key}"] = has_val
            data[f"{key}_from_env"] = config.is_from_env(key)
        data["opensubtitles_username_from_env"] = config.is_from_env("opensubtitles_username")
        return data

    def handle_get_config(self):
        config = Config()
        self.send_json_response(self._public_config_dict(config))

    def handle_save_config(self, payload: dict):
        config = Config()
        for k, v in payload.items():
            if k.startswith("clear_"):
                secret_name = k[len("clear_"):]
                if secret_name in self._SECRET_CONFIG_KEYS and v:
                    config.set(secret_name, "")
                continue
            if k in self._SECRET_CONFIG_KEYS:
                if isinstance(v, str) and v.strip() and not set(v.strip()) <= {"*"}:
                    config.set(k, v.strip())
                continue
            config.set(k, v)
        config.save()
        self.send_json_response({"status": "saved", "config": self._public_config_dict(config)})

    @staticmethod
    def _parse_optional_int(value: Any, field_name: str) -> Optional[int]:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as e:
            raise ValueError(f"Field '{field_name}' must be an integer") from e

    def handle_subtitle_search(self, payload: dict):
        raw_video = (payload.get("video_path") or "").strip()
        resolved_video = resolve_file_path(raw_video) if raw_video else None
        title = (payload.get("title") or "").strip() or None
        imdb_id = (payload.get("imdb_id") or "").strip() or None

        try:
            year = self._parse_optional_int(payload.get("year"), "year")
            season = self._parse_optional_int(payload.get("season"), "season")
            episode = self._parse_optional_int(payload.get("episode"), "episode")
        except ValueError as e:
            self.send_json_response({"error": str(e)}, status=HTTPStatus.BAD_REQUEST)
            return

        if not resolved_video and not title and not imdb_id:
            self.send_json_response({
                "error": "Provide a valid video file, movie/series title, or IMDb ID to search subtitles."
            }, status=HTTPStatus.BAD_REQUEST)
            return

        cfg = Config()
        lang = (payload.get("language") or cfg.get("subtitle_language", "en") or "en").strip()
        query = parse_media_query(
            video_path=resolved_video,
            title=title,
            year=year,
            season=season,
            episode=episode,
            imdb_id=imdb_id,
            language=lang,
            compute_hash=bool(resolved_video),
        )

        fetcher = self._create_subtitle_fetcher(cfg)
        candidates = fetcher.search(query, limit=25)
        self.send_json_response({
            "query": query.to_dict(),
            "candidates": [c.to_dict() for c in candidates],
        })

    def handle_subtitle_download(self, payload: dict):
        provider = (payload.get("provider") or "").strip()
        allowed_providers = {"opensubtitles", "subdl", "stremio_opensubtitles", "podnapisi"}
        if provider not in allowed_providers:
            self.send_json_response({"error": "Unsupported subtitle provider."}, status=HTTPStatus.BAD_REQUEST)
            return

        download_url = payload.get("download_url")
        download_id = payload.get("download_id")

        if download_url:
            if not is_trusted_https_url(str(download_url)):
                self.send_json_response({
                    "error": "Blocked untrusted or non-HTTPS subtitle download URL."
                }, status=HTTPStatus.BAD_REQUEST)
                return
        elif provider == "opensubtitles":
            if not download_id or not str(download_id).isdigit():
                self.send_json_response({
                    "error": "Missing numeric file_id for OpenSubtitles download."
                }, status=HTTPStatus.BAD_REQUEST)
                return
        else:
            self.send_json_response({
                "error": "Missing trusted subtitle download URL."
            }, status=HTTPStatus.BAD_REQUEST)
            return

        cfg = Config()
        language = (payload.get("language") or cfg.get("subtitle_language", "en") or "en").strip()
        iso1, _ = normalize_language_codes(language)
        raw_video = (payload.get("video_path") or "").strip()
        resolved_video = resolve_file_path(raw_video) if raw_video else None

        if resolved_video:
            dest_path = build_subtitle_output_path(resolved_video, language=iso1)
        else:
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            base_name = (payload.get("release_name") or payload.get("title") or "subtitle").strip()
            safe_stem = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in base_name)[:80].strip("._") or "subtitle"
            dest_path = UPLOAD_DIR / f"{safe_stem}.{iso1}.srt"

        query = parse_media_query(
            video_path=resolved_video,
            title=payload.get("title"),
            language=iso1,
            compute_hash=False,
        )
        candidate = SubtitleCandidate(
            provider=provider,
            subtitle_id=str(payload.get("subtitle_id") or download_id or "manual"),
            title=str(payload.get("title") or query.title or "Subtitle"),
            language=iso1,
            release_name=str(payload.get("release_name") or ""),
            download_url=str(download_url) if download_url else None,
            download_id=str(download_id) if download_id is not None else None,
            file_name=payload.get("file_name"),
        )

        fetcher = self._create_subtitle_fetcher(cfg)
        try:
            res = fetcher.download_candidate(candidate, dest_srt_path=str(dest_path), query=query)
            self.send_json_response({
                "status": "downloaded",
                "srt_path": res.srt_path,
                "provider": res.provider,
                "cue_count": res.cue_count,
                "converted_from_vtt": res.converted_from_vtt,
            })
        except ValueError as e:
            self.send_json_response({"error": str(e)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as e:
            self.send_json_response({"error": f"Subtitle download failed: {e}"}, status=HTTPStatus.BAD_GATEWAY)

    def handle_get_progress(self):
        with job_lock:
            state = current_job.copy()
        self.send_json_response(state)

    def handle_cancel(self):
        global active_engine, active_job_id
        with job_lock:
            active_job_id += 1
            if active_engine:
                active_engine.cancel()
            active_engine = None
            current_job["status"] = "idle"
            current_job["error"] = "Cancelled by user"
        self.send_json_response({"status": "cancelled"})

    def handle_reveal(self, payload: dict):
        path_str = payload.get("path", "")
        if path_str and os.path.exists(path_str):
            if sys.platform == "darwin":
                subprocess.run(["open", "-R", path_str])
            elif sys.platform == "win32":
                subprocess.run(["explorer", f"/select,{path_str}"])
            else:
                subprocess.run(["xdg-open", os.path.dirname(path_str)])
            self.send_json_response({"status": "revealed"})
        else:
            self.send_json_response({"error": "Path not found"}, status=HTTPStatus.NOT_FOUND)

    def handle_browse(self, query: dict):
        dir_param = query.get("dir", [str(Path.home())])[0]
        dir_path = Path(dir_param).resolve()
        if not dir_path.is_dir():
            dir_path = dir_path.parent

        items = []
        try:
            for entry in sorted(dir_path.iterdir()):
                if entry.name.startswith("."):
                    continue
                is_dir = entry.is_dir()
                is_video = entry.suffix.lower() in [".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"]
                is_sub = entry.suffix.lower() in [".srt", ".vtt"]
                if is_dir or is_video or is_sub:
                    items.append({
                        "name": entry.name,
                        "path": str(entry),
                        "is_dir": is_dir,
                        "is_video": is_video,
                        "is_sub": is_sub,
                        "size": entry.stat().st_size if not is_dir else 0
                    })
        except Exception as e:
            items = []

        bookmarks = [
            {"name": "Home", "path": str(Path.home())},
            {"name": "Downloads", "path": str(Path.home() / "Downloads")},
            {"name": "Movies", "path": str(Path.home() / "Movies")},
            {"name": "Workspace", "path": str(Path.cwd())}
        ]

        self.send_json_response({
            "current_dir": str(dir_path),
            "parent_dir": str(dir_path.parent) if dir_path.parent != dir_path else None,
            "bookmarks": bookmarks,
            "items": items
        })

    def handle_scan(self, payload: dict):
        video_path = payload.get("video_path", "").strip()
        srt_path = payload.get("srt_path", "").strip()
        preset = payload.get("preset", "moderate")
        custom_words = payload.get("custom_words", [])
        timing_mode = payload.get("timing_mode", "word")
        pad_before = float(payload.get("padding_before", 0.18))
        pad_after = float(payload.get("padding_after", 0.16))
        lead_in = float(payload.get("lead_in", 0.10))
        sync_offset = float(payload.get("sync_offset", 0.0))
        audio_refine = bool(payload.get("audio_refine", True))
        cfg = Config()
        sub_lang = (payload.get("sub_lang") or cfg.get("subtitle_language", "en") or "en").strip()
        auto_download_subs = bool(payload.get("auto_download_subs", cfg.get("auto_download_subtitles", True)))

        resolved_video = resolve_file_path(video_path)
        if not resolved_video:
            self.send_json_response({
                "error": f"Video file '{video_path}' could not be found. Please use the Browse (📁) button or ensure the file upload has finished."
            }, status=HTTPStatus.BAD_REQUEST)
            return
        video_path = resolved_video

        resolved_srt = resolve_file_path(srt_path)
        if resolved_srt:
            srt_path = resolved_srt

        media_info = FFmpegProbe.probe_media(video_path)
        embedded_streams = probe_subtitle_streams(video_path)

        # Subtitle resolution (external first, then embedded text track, then trusted online providers)
        active_srt = srt_path
        subtitle_downloaded = False
        subtitle_provider = None
        if not active_srt or not os.path.exists(active_srt):
            p = Path(video_path)
            iso1, iso2 = normalize_language_codes(sub_lang)
            exts = []
            for ext in (".srt", f".{iso1}.srt", f".{iso2}.srt", ".en.srt", ".eng.srt", ".vtt", f".{iso1}.vtt", ".en.vtt"):
                if ext not in exts:
                    exts.append(ext)
            for ext in exts:
                candidate = p.parent / f"{p.stem}{ext}"
                if candidate.exists() and candidate.is_file() and candidate.stat().st_size > 0:
                    active_srt = str(candidate)
                    break

        if not active_srt or not os.path.exists(active_srt):
            text_streams = [s for s in embedded_streams if s.get("is_text", False)]
            if text_streams:
                sel = next(
                    (s for s in text_streams if s.get("language", "").lower() in ("eng", "en")),
                    text_streams[0]
                )
                sub_cache_dir = Path(tempfile.gettempdir()) / "censor_app_subs"
                sub_cache_dir.mkdir(parents=True, exist_ok=True)
                vid_hash = hashlib.sha1(video_path.encode("utf-8")).hexdigest()[:12]
                cached_srt = sub_cache_dir / f"embedded_{vid_hash}_{sel['sub_index']}.srt"
                try:
                    active_srt = extract_embedded_subtitle(
                        video_path,
                        stream_index=sel["sub_index"],
                        output_path=str(cached_srt)
                    )
                except Exception as e:
                    self.send_json_response({"error": f"Failed to extract embedded subtitles: {e}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                    return

        if (not active_srt or not os.path.exists(active_srt)) and auto_download_subs:
            fetcher = self._create_subtitle_fetcher(cfg)
            dl_res = fetcher.auto_fetch_subtitle_for_video(video_path, language=sub_lang)
            if dl_res and os.path.exists(dl_res.srt_path):
                active_srt = dl_res.srt_path
                subtitle_downloaded = True
                subtitle_provider = dl_res.provider

        if not active_srt or not os.path.exists(active_srt):
            if embedded_streams:
                codecs = ", ".join(sorted(set(s.get("codec", "unknown") for s in embedded_streams)))
                self.send_json_response({
                    "error": f"Embedded subtitles are image-based ({codecs}), not text, and no online match was found. Please select or search for an external SRT/VTT file."
                }, status=HTTPStatus.BAD_REQUEST)
                return
            self.send_json_response({
                "error": "No external, embedded, or online subtitles found. Please select an SRT/VTT file or search online manually."
            }, status=HTTPStatus.BAD_REQUEST)
            return

        try:
            subs = parse_srt(active_srt)
            pf = ProfanityFilter(preset=preset, custom_words=custom_words)
            detections = detect_profanities_in_subtitles(
                subtitles=subs,
                profanity_filter=pf,
                timing_mode=timing_mode,
                padding_before=pad_before,
                padding_after=pad_after,
                lead_in=lead_in,
                sync_offset=sync_offset,
                video_path=video_path,
                audio_refine=audio_refine,
            )

            # Auto suggest output path alongside video
            v_p = Path(video_path)
            default_output = str(v_p.parent / f"{v_p.stem}.Cleaned{v_p.suffix}")

            self.send_json_response({
                "video_path": video_path,
                "srt_path": active_srt,
                "subtitle_downloaded": subtitle_downloaded,
                "subtitle_provider": subtitle_provider,
                "suggested_output": default_output,
                "duration": media_info.get("duration", 0.0),
                "is_surround": media_info.get("is_surround", False),
                "channel_layout": media_info.get("channel_layout", "stereo"),
                "audio_channels": media_info.get("audio_channels", 2),
                "embedded_tracks": embedded_streams,
                "detections": [d.to_dict() for d in detections]
            })
        except Exception as e:
            self.send_json_response({"error": f"Scanning failed: {str(e)}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_process(self, payload: dict):
        global active_engine, active_job_id
        video_path = payload.get("video_path")
        output_path = payload.get("output_path")
        srt_path = payload.get("srt_path")
        mode = payload.get("mode", "mute")
        preset = payload.get("preset", "moderate")
        custom_words = payload.get("custom_words", [])
        center_channel_only = payload.get("center_channel_only", True)
        generate_srt = payload.get("generate_clean_srt", True)
        detections_data = payload.get("detections", [])

        if not video_path or not os.path.exists(video_path):
            self.send_json_response({"error": "Invalid video path"}, status=HTTPStatus.BAD_REQUEST)
            return

        if not output_path or not isinstance(output_path, str) or not output_path.strip():
            self.send_json_response({"error": "Invalid output path"}, status=HTTPStatus.BAD_REQUEST)
            return
        output_path = output_path.strip()
        out_p = Path(output_path)
        if out_p.suffix.lower() not in ALLOWED_VIDEO_EXTENSIONS:
            self.send_json_response({
                "error": f"Unsupported output video extension: {out_p.suffix or '(none)'}"
            }, status=HTTPStatus.BAD_REQUEST)
            return
        if FFmpegEngine._is_same_file(video_path, output_path):
            self.send_json_response({"error": "Output path must be different from input video path"}, status=HTTPStatus.BAD_REQUEST)
            return

        cfg = Config()

        with job_lock:
            if current_job["status"] == "processing":
                self.send_json_response({"error": "A censorship job is already in progress"}, status=HTTPStatus.CONFLICT)
                return
            active_job_id += 1
            my_job_id = active_job_id
            current_job["status"] = "processing"
            current_job["percent"] = 0.0
            current_job["current_time"] = 0.0
            current_job["total_duration"] = 0.0
            current_job["output_path"] = output_path
            current_job["cleaned_srt_path"] = ""
            current_job["error"] = ""

        # Filter enabled intervals
        intervals = [
            (float(d["mute_start"]), float(d["mute_end"]))
            for d in detections_data
            if d.get("enabled", True)
        ]
        merged_intervals = merge_intervals(intervals)

        def worker():
            global active_engine
            engine = FFmpegEngine()
            with job_lock:
                if active_job_id != my_job_id or current_job["status"] != "processing":
                    return
                active_engine = engine

            def progress_hook(p_data):
                with job_lock:
                    if active_job_id != my_job_id or current_job["status"] != "processing":
                        return
                    current_job["percent"] = p_data.get("percent", 0.0)
                    current_job["current_time"] = p_data.get("current_time", 0.0)
                    current_job["total_duration"] = p_data.get("total_duration", 0.0)

            temp_srt_fallback = None
            try:
                success = engine.run_censor(
                    input_video=video_path,
                    output_video=output_path,
                    intervals=merged_intervals,
                    mode=mode,
                    center_channel_only=center_channel_only,
                    ducking_vol=cfg.get("ducking_volume", 0.06),
                    bleep_freq=cfg.get("bleep_frequency", 1000),
                    crossfade_duration=cfg.get("crossfade_duration", 0.04),
                    progress_callback=progress_hook
                )

                if success and generate_srt:
                    active_srt_for_clean = srt_path if (srt_path and os.path.exists(srt_path)) else None
                    if not active_srt_for_clean:
                        text_streams = probe_subtitle_streams(video_path, text_only=True)
                        if text_streams:
                            sel = next(
                                (s for s in text_streams if s.get("language", "").lower() in ("eng", "en")),
                                text_streams[0]
                            )
                            temp_srt_fallback = extract_embedded_subtitle(video_path, stream_index=sel["sub_index"])
                            active_srt_for_clean = temp_srt_fallback

                    if active_srt_for_clean and os.path.exists(active_srt_for_clean):
                        out_p_res = Path(output_path)
                        clean_srt_path = str(out_p_res.parent / f"{out_p_res.stem}.srt")
                        if FFmpegEngine._is_same_file(clean_srt_path, active_srt_for_clean):
                            clean_srt_path = str(out_p_res.parent / f"{out_p_res.stem}.Cleaned.srt")

                        subs = parse_srt(active_srt_for_clean)
                        pf = ProfanityFilter(preset=preset, custom_words=custom_words)
                        mask_char = cfg.get("censor_char", "*")

                        if detections_data:
                            enabled_sub_words = {
                                (int(d.get("subtitle_index", -1)), str(d.get("word", "")).lower())
                                for d in detections_data
                                if d.get("enabled", True)
                            }
                            cleaned_subs = []
                            for item in subs:
                                matches = [
                                    m for m in pf.find_matches(item.clean_text)
                                    if (item.index, m.word.lower()) in enabled_sub_words
                                ]
                                if matches:
                                    chars = list(item.clean_text)
                                    for m in reversed(matches):
                                        length = m.end_char - m.start_char
                                        repl = (
                                            item.clean_text[m.start_char] + (mask_char * (length - 1))
                                            if length > 1 else (mask_char * length)
                                        )
                                        chars[m.start_char:m.end_char] = list(repl)
                                    sanitized_text = "".join(chars)
                                else:
                                    sanitized_text = item.clean_text
                                cleaned_subs.append(SubtitleItem(
                                    index=item.index,
                                    start_time=item.start_time,
                                    end_time=item.end_time,
                                    raw_text=item.raw_text,
                                    clean_text=sanitized_text
                                ))
                            no_op_pf = ProfanityFilter(preset="custom", custom_words=[])
                            generate_cleaned_srt(cleaned_subs, no_op_pf, clean_srt_path, mask_char=mask_char)
                        else:
                            generate_cleaned_srt(subs, pf, clean_srt_path, mask_char=mask_char)

                        with job_lock:
                            if active_job_id == my_job_id and current_job["status"] == "processing":
                                current_job["cleaned_srt_path"] = clean_srt_path

                with job_lock:
                    if active_job_id == my_job_id and current_job["status"] == "processing":
                        current_job["status"] = "completed" if success else "failed"
                        if not success:
                            current_job["error"] = engine.last_error or "FFmpeg process returned non-zero exit code"
            except Exception as e:
                with job_lock:
                    if active_job_id == my_job_id and current_job["status"] == "processing":
                        current_job["status"] = "failed"
                        current_job["error"] = str(e)
            finally:
                if temp_srt_fallback and os.path.exists(temp_srt_fallback):
                    try:
                        os.remove(temp_srt_fallback)
                    except OSError:
                        pass
                with job_lock:
                    if active_job_id == my_job_id:
                        active_engine = None

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        self.send_json_response({"status": "started", "output_path": output_path})

    def handle_stream_media(self, query: dict):
        path_param = query.get("path", [""])[0]
        if not path_param:
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing path parameter")
            return

        resolved = resolve_file_path(unquote(path_param))
        if not resolved:
            self.send_error(HTTPStatus.NOT_FOUND, "Media file not found")
            return

        file_path = Path(resolved)
        if file_path.suffix.lower() not in ALLOWED_STREAM_EXTENSIONS:
            self._send_forbidden("Streaming non-media files is not permitted")
            return

        file_size = file_path.stat().st_size
        range_header = self.headers.get("Range")

        # Determine mime type
        mime_type, _ = mimetypes.guess_type(str(file_path))
        if not mime_type:
            mime_type = "video/mp4"

        if range_header:
            range_val = range_header.strip().lower()
            if not range_val.startswith("bytes="):
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self._send_security_headers()
                self.end_headers()
                return

            range_spec = range_val[6:].strip()
            if "," in range_spec or "-" not in range_spec or file_size == 0:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self._send_security_headers()
                self.end_headers()
                return

            start_str, end_str = range_spec.split("-", 1)
            try:
                if not start_str and not end_str:
                    raise ValueError("Empty range")
                if not start_str:
                    suffix_len = int(end_str)
                    if suffix_len <= 0:
                        raise ValueError("Invalid suffix length")
                    start = max(0, file_size - suffix_len)
                    end = file_size - 1
                else:
                    start = int(start_str)
                    end = int(end_str) if end_str else file_size - 1
            except ValueError:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self._send_security_headers()
                self.end_headers()
                return

            if start < 0 or start >= file_size or end < start:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self._send_security_headers()
                self.end_headers()
                return

            end = min(end, file_size - 1)

            # Cap single response chunk to 4MB for high responsiveness
            max_chunk = 4 * 1024 * 1024
            if end - start + 1 > max_chunk:
                end = start + max_chunk - 1

            chunk_len = end - start + 1

            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            self.send_header("Content-Length", str(chunk_len))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-cache")
            self._send_security_headers()
            self.end_headers()

            try:
                with open(file_path, "rb") as f:
                    f.seek(start)
                    remaining = chunk_len
                    buffer_size = 64 * 1024
                    while remaining > 0:
                        to_read = min(remaining, buffer_size)
                        buf = f.read(to_read)
                        if not buf:
                            break
                        self.wfile.write(buf)
                        remaining -= len(buf)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Length", str(file_size))
            self.send_header("Accept-Ranges", "bytes")
            self._send_security_headers()
            self.end_headers()

            try:
                with open(file_path, "rb") as f:
                    while True:
                        buf = f.read(64 * 1024)
                        if not buf:
                            break
                        self.wfile.write(buf)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def send_json_response(self, data: dict, status: HTTPStatus = HTTPStatus.OK):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers(self._get_validated_origin())
        self.end_headers()
        self.wfile.write(body)


def run_studio_server(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), StudioRequestHandler)
    return server
