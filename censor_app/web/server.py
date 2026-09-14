import os
import sys
import json
import mimetypes
import subprocess
import threading
from pathlib import Path
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

STATIC_DIR = Path(__file__).parent / "static"

# Global state for active processing job
job_lock = threading.Lock()
current_job = {
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
        Path.home() / ".cache" / "censor_app" / "uploads"
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

    def log_message(self, format, *args):
        # Keep console output clean
        pass

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Range")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

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
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "Endpoint not found")

    def handle_upload(self, query: dict):
        raw_filename = query.get("filename", ["uploaded_media"])[0]
        filename = os.path.basename(unquote(raw_filename))
        if not filename:
            filename = "uploaded_media.mp4"

        upload_dir = Path.home() / ".cache" / "censor_app" / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        target_path = upload_dir / filename

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
        self.end_headers()
        self.wfile.write(content)

    def handle_get_config(self):
        config = Config()
        self.send_json_response(config.to_dict())

    def handle_save_config(self, payload: dict):
        config = Config()
        for k, v in payload.items():
            config.set(k, v)
        config.save()
        self.send_json_response({"status": "saved", "config": config.to_dict()})

    def handle_get_progress(self):
        with job_lock:
            state = current_job.copy()
        self.send_json_response(state)

    def handle_cancel(self):
        global active_engine
        with job_lock:
            if active_engine:
                active_engine.cancel()
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
        pad_before = float(payload.get("padding_before", 0.30))
        pad_after = float(payload.get("padding_after", 0.30))
        lead_in = float(payload.get("lead_in", 0.20))
        sync_offset = float(payload.get("sync_offset", 0.0))

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

        # Subtitle resolution
        temp_extracted_srt = None
        active_srt = srt_path
        if not active_srt or not os.path.exists(active_srt):
            # Check for same-named srt
            p = Path(video_path)
            candidate = p.parent / f"{p.stem}.srt"
            if candidate.exists():
                active_srt = str(candidate)
            elif embedded_streams:
                try:
                    temp_extracted_srt = extract_embedded_subtitle(video_path, stream_index=0)
                    active_srt = temp_extracted_srt
                except Exception as e:
                    self.send_json_response({"error": f"Failed to extract embedded subtitles: {e}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
            else:
                self.send_json_response({
                    "error": "No external or embedded subtitles found. Please select an SRT/VTT file."
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
                sync_offset=sync_offset
            )

            # Auto suggest output path alongside video
            v_p = Path(video_path)
            default_output = str(v_p.parent / f"{v_p.stem}.Cleaned{v_p.suffix}")

            self.send_json_response({
                "video_path": video_path,
                "srt_path": active_srt,
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
        finally:
            if temp_extracted_srt and os.path.exists(temp_extracted_srt):
                try:
                    os.remove(temp_extracted_srt)
                except OSError:
                    pass

    def handle_process(self, payload: dict):
        global active_engine
        video_path = payload.get("video_path")
        output_path = payload.get("output_path")
        srt_path = payload.get("srt_path")
        mode = payload.get("mode", "mute")
        center_channel_only = payload.get("center_channel_only", True)
        generate_srt = payload.get("generate_clean_srt", True)
        detections_data = payload.get("detections", [])

        if not video_path or not os.path.exists(video_path):
            self.send_json_response({"error": "Invalid video path"}, status=HTTPStatus.BAD_REQUEST)
            return

        with job_lock:
            if current_job["status"] == "processing":
                self.send_json_response({"error": "A censorship job is already in progress"}, status=HTTPStatus.CONFLICT)
                return
            current_job["status"] = "processing"
            current_job["percent"] = 0.0
            current_job["current_time"] = 0.0
            current_job["output_path"] = output_path
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
            active_engine = engine

            def progress_hook(p_data):
                with job_lock:
                    current_job["percent"] = p_data.get("percent", 0.0)
                    current_job["current_time"] = p_data.get("current_time", 0.0)
                    current_job["total_duration"] = p_data.get("total_duration", 0.0)

            try:
                success = engine.run_censor(
                    input_video=video_path,
                    output_video=output_path,
                    intervals=merged_intervals,
                    mode=mode,
                    center_channel_only=center_channel_only,
                    progress_callback=progress_hook
                )

                if success and generate_srt and srt_path and os.path.exists(srt_path):
                    # Write cleaned srt alongside
                    out_p = Path(output_path)
                    clean_srt_path = str(out_p.parent / f"{out_p.stem}.srt")
                    subs = parse_srt(srt_path)
                    pf = ProfanityFilter(preset="mild")  # Mask all matching words
                    generate_cleaned_srt(subs, pf, clean_srt_path)
                    with job_lock:
                        current_job["cleaned_srt_path"] = clean_srt_path

                with job_lock:
                    current_job["status"] = "completed" if success else "failed"
                    if not success and not current_job.get("error"):
                        current_job["error"] = "FFmpeg process returned non-zero exit code"
            except Exception as e:
                with job_lock:
                    current_job["status"] = "failed"
                    current_job["error"] = str(e)
            finally:
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
        file_size = file_path.stat().st_size
        range_header = self.headers.get("Range")

        # Determine mime type
        mime_type, _ = mimetypes.guess_type(str(file_path))
        if not mime_type:
            mime_type = "video/mp4"

        if range_header:
            # Parse Range e.g. "bytes=0-1024" or "bytes=1024-"
            range_val = range_header.strip().lower()
            if not range_val.startswith("bytes="):
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                return

            range_spec = range_val[6:]
            parts = range_spec.split("-")
            start = int(parts[0]) if parts[0] else 0
            end = int(parts[1]) if len(parts) > 1 and parts[1] else file_size - 1

            if start >= file_size or end >= file_size or start > end:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.end_headers()
                return

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
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


def run_studio_server(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), StudioRequestHandler)
    return server
