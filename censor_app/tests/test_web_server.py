import unittest
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import subprocess
from pathlib import Path
from http import HTTPStatus
from unittest import mock
import censor_app.web.server as server_module
from censor_app.web.server import StudioRequestHandler, STATIC_DIR
from censor_app.core.config import Config, ENV_VAR_MAP
from censor_app.core.subtitle_fetcher import SubtitleFetcher

class MockSocket:
    def __init__(self, request_bytes: bytes):
        self.rfile = io.BytesIO(request_bytes)
        self.wfile = io.BytesIO()

    def makefile(self, mode, *args, **kwargs):
        if 'r' in mode:
            return self.rfile
        elif 'w' in mode:
            return self.wfile
        return self.wfile

    def sendall(self, data):
        self.wfile.write(data)


class MockServer:
    def __init__(self):
        pass


class TestWebHandler(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.vid_path = os.path.join(self.test_dir, "web_test.mp4")
        self.srt_path = os.path.join(self.test_dir, "web_test.srt")

        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=15",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-c:a", "aac",
            self.vid_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        self.orig_upload_dir = server_module.UPLOAD_DIR
        server_module.UPLOAD_DIR = Path(self.test_dir) / "uploads"

        with open(self.srt_path, "w", encoding="utf-8") as f:
            f.write("1\n00:00:00,500 --> 00:00:01,500\nThis is a fucking test!\n")

    def tearDown(self):
        server_module.UPLOAD_DIR = self.orig_upload_dir
        with server_module.job_lock:
            if server_module.active_engine:
                server_module.active_engine.cancel()
            server_module.active_engine = None
            server_module.current_job.update({
                "status": "idle",
                "percent": 0.0,
                "current_time": 0.0,
                "total_duration": 0.0,
                "output_path": "",
                "cleaned_srt_path": "",
                "error": ""
            })
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def _execute_request(self, method: str, path: str, body: bytes = b"", headers: dict = None) -> tuple:
        if headers is None:
            headers = {}
        headers["Content-Length"] = str(len(body))

        header_lines = [f"{method} {path} HTTP/1.1"]
        for k, v in headers.items():
            header_lines.append(f"{k}: {v}")
        header_lines.append("")
        header_lines.append("")

        req_bytes = "\r\n".join(header_lines).encode("utf-8") + body
        mock_sock = MockSocket(req_bytes)

        # Instantiate handler with mock socket
        handler = StudioRequestHandler(mock_sock, ("127.0.0.1", 12345), MockServer())

        response_bytes = mock_sock.wfile.getvalue()
        # Parse status code from response
        status_line = response_bytes.split(b"\r\n")[0].decode("utf-8", errors="ignore")
        parts = status_line.split(" ", 2)
        status_code = int(parts[1]) if len(parts) > 1 else 500

        # Split headers and body
        if b"\r\n\r\n" in response_bytes:
            headers_raw, body_raw = response_bytes.split(b"\r\n\r\n", 1)
        else:
            headers_raw, body_raw = response_bytes, b""

        return status_code, headers_raw.decode("utf-8", errors="ignore"), body_raw

    def test_static_files(self):
        status, headers, body = self._execute_request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("Movie Censor Studio", body.decode("utf-8"))

        status, _, _ = self._execute_request("GET", "/style.css")
        self.assertEqual(status, 200)

        status, _, _ = self._execute_request("GET", "/app.js")
        self.assertEqual(status, 200)

    def test_api_browse(self):
        status, headers, body = self._execute_request("GET", f"/api/browse?dir={self.test_dir}")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertIn("items", data)
        names = [item["name"] for item in data["items"]]
        self.assertIn("web_test.mp4", names)

    def test_api_scan(self):
        payload = json.dumps({
            "video_path": self.vid_path,
            "srt_path": self.srt_path,
            "preset": "moderate"
        }).encode("utf-8")

        status, headers, body = self._execute_request("POST", "/api/scan", body=payload, headers={"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertIn("detections", data)
        self.assertEqual(len(data["detections"]), 1)
        self.assertEqual(data["detections"][0]["word"], "fucking")

    def test_range_stream(self):
        status, headers, body = self._execute_request(
            "GET",
            f"/api/stream?path={self.vid_path}",
            headers={"Range": "bytes=0-99"}
        )
        self.assertEqual(status, 206)
        self.assertEqual(len(body), 100)

    def test_api_upload(self):
        sample_payload = b"FAKE_VIDEO_CONTENT"
        status, headers, body = self._execute_request(
            "POST",
            "/api/upload?filename=test_clip.mp4",
            body=sample_payload
        )
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["status"], "uploaded")
        self.assertTrue(os.path.exists(data["path"]))
        self.assertEqual(data["size"], len(sample_payload))

    def test_api_cancel_before_job(self):
        status, headers, body = self._execute_request("POST", "/api/cancel", body=b"{}")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["status"], "cancelled")

    def test_range_suffix_and_invalid_ranges(self):
        file_bytes = Path(self.vid_path).read_bytes()
        status, headers, body = self._execute_request(
            "GET",
            f"/api/stream?path={self.vid_path}",
            headers={"Range": "bytes=-100"}
        )
        self.assertEqual(status, 206)
        self.assertEqual(len(body), 100)
        self.assertEqual(body, file_bytes[-100:])

        for bad_range in ("bytes=0-10,20-30", "bytes=abc-def"):
            status, _, _ = self._execute_request(
                "GET",
                f"/api/stream?path={self.vid_path}",
                headers={"Range": bad_range}
            )
            self.assertEqual(status, 416)

    def test_same_origin_and_stream_allowlist(self):
        status, _, _ = self._execute_request(
            "GET",
            f"/api/browse?dir={self.test_dir}",
            headers={"Origin": "https://evil.example", "Host": "127.0.0.1:8000"}
        )
        self.assertEqual(status, 403)

        status, _, _ = self._execute_request(
            "POST",
            "/api/scan",
            body=b"{}",
            headers={"Sec-Fetch-Site": "cross-site", "Content-Type": "application/json"}
        )
        self.assertEqual(status, 403)

        secret_json = os.path.join(self.test_dir, "secret.json")
        Path(secret_json).write_text('{"token":"abc"}', encoding="utf-8")
        status, _, _ = self._execute_request("GET", f"/api/stream?path={secret_json}")
        self.assertEqual(status, 403)

    def test_embedded_subtitle_scan_and_process_e2e(self):
        mkv_path = os.path.join(self.test_dir, "embedded.mkv")
        out_mkv = os.path.join(self.test_dir, "embedded.Cleaned.mkv")
        subprocess.run([
            "ffmpeg", "-y",
            "-i", self.vid_path,
            "-i", self.srt_path,
            "-map", "0:v", "-map", "0:a", "-map", "1:0",
            "-c:v", "copy", "-c:a", "copy", "-c:s", "srt",
            mkv_path
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        scan_payload = json.dumps({"video_path": mkv_path, "preset": "moderate"}).encode("utf-8")
        status, _, body = self._execute_request(
            "POST", "/api/scan", body=scan_payload, headers={"Content-Type": "application/json"}
        )
        self.assertEqual(status, 200)
        scan_data = json.loads(body.decode("utf-8"))
        self.assertEqual(len(scan_data["detections"]), 1)
        self.assertTrue(os.path.exists(scan_data["srt_path"]))

        proc_payload = json.dumps({
            "video_path": mkv_path,
            "output_path": out_mkv,
            "srt_path": scan_data["srt_path"],
            "mode": "mute",
            "preset": "moderate",
            "generate_clean_srt": True,
            "detections": scan_data["detections"]
        }).encode("utf-8")
        status, _, _ = self._execute_request(
            "POST", "/api/process", body=proc_payload, headers={"Content-Type": "application/json"}
        )
        self.assertEqual(status, 200)

        job_state = {}
        for _ in range(50):
            _, _, p_body = self._execute_request("GET", "/api/progress")
            job_state = json.loads(p_body.decode("utf-8"))
            if job_state.get("status") in ("completed", "failed"):
                break
            time.sleep(0.1)

        self.assertEqual(job_state.get("status"), "completed", f"Job failed: {job_state}")
        self.assertTrue(os.path.exists(out_mkv))
        cleaned_srt = job_state.get("cleaned_srt_path", "")
        self.assertTrue(cleaned_srt and os.path.exists(cleaned_srt))
        srt_text = Path(cleaned_srt).read_text(encoding="utf-8")
        self.assertIn("f******", srt_text)
        self.assertNotIn("fucking", srt_text)

        # Clean up cached embedded extraction in temp dir
        if os.path.exists(scan_data["srt_path"]):
            os.remove(scan_data["srt_path"])

    def test_api_scan_smart_audio_refine(self):
        speech_vid = os.path.join(self.test_dir, "web_speech.mp4")
        speech_srt = os.path.join(self.test_dir, "web_speech.srt")
        bursts = [
            (0.75, 0.98), (1.05, 1.26), (1.32, 1.46),
            (1.52, 1.68), (1.78, 2.22), (2.32, 2.65)
        ]
        expr = "+".join(f"between(t,{s},{e})" for s, e in bursts)
        subprocess.run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=4:size=160x120:rate=15",
            "-f", "lavfi", "-i", "sine=frequency=450:sample_rate=48000:duration=4",
            "-af", f"volume='if({expr},0.9,0.005)':eval=frame",
            "-c:v", "libx264", "-c:a", "aac", speech_vid
        ], check=True)
        Path(speech_srt).write_text("1\n00:00:00,500 --> 00:00:03,500\nGet out of my fucking house!\n", encoding="utf-8")

        base_req = {
            "video_path": speech_vid,
            "srt_path": speech_srt,
            "preset": "moderate",
            "timing_mode": "word",
            "padding_before": 0.18,
            "padding_after": 0.16,
            "lead_in": 0.10,
        }
        status, _, body_ref = self._execute_request(
            "POST", "/api/scan",
            body=json.dumps({**base_req, "audio_refine": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        self.assertEqual(status, 200)
        det_ref = json.loads(body_ref.decode("utf-8"))["detections"][0]

        status_un, _, body_un = self._execute_request(
            "POST", "/api/scan",
            body=json.dumps({**base_req, "audio_refine": False}).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        self.assertEqual(status_un, 200)
        det_un = json.loads(body_un.decode("utf-8"))["detections"][0]

        self.assertLess(det_ref["mute_end"] - det_ref["mute_start"], 1.2)
        self.assertLess(det_ref["mute_end"], det_un["mute_end"])

    def test_api_subtitles_search_download_ssrf_guard_and_auto_scan(self):
        sub_vid = os.path.join(self.test_dir, "TestAutoSub.2021.1080p.WEB.mp4")
        subprocess.run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=15",
            "-f", "lavfi", "-i", "sine=frequency=400:sample_rate=48000:duration=2",
            "-c:v", "libx264", "-c:a", "aac", sub_vid
        ], check=True)

        cfg_path = os.path.join(self.test_dir, "isolated_config.json")
        orig_config_cls = server_module.Config
        orig_factory = StudioRequestHandler.subtitle_fetcher_factory

        def mock_http(url, method="GET", headers=None, body=None, timeout=10.0, max_bytes=10485760):
            if "www.podnapisi.net/subtitles/search/old" in url:
                return 200, {}, (
                    '<?xml version="1.0"?><results><subtitle>'
                    '<pid>pod99</pid><title>TestAutoSub</title><year>2021</year>'
                    '<release>TestAutoSub.2021.1080p.WEB</release><downloads>42</downloads>'
                    '</subtitle></results>'
                ).encode("utf-8")
            if url == "https://www.podnapisi.net/subtitles/pod99/download":
                return 200, {}, b"1\n00:00:00,400 --> 00:00:01,500\nThis is fucking wild.\n"
            return 404, {}, b""

        try:
            server_module.Config = lambda custom_path=None: orig_config_cls(
                custom_path=custom_path or cfg_path,
                load_env=False,
            )
            StudioRequestHandler.subtitle_fetcher_factory = (
                lambda config=None: SubtitleFetcher(config=config, http_client=mock_http)
            )

            # 1. Config endpoint saves secrets and masks them on GET/POST responses
            s_cfg, _, b_cfg = self._execute_request(
                "POST", "/api/config",
                body=json.dumps({
                    "opensubtitles_api_key": "os-secret-key",
                    "opensubtitles_password": "os-secret-pw",
                    "opensubtitles_username": "demo_user",
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(s_cfg, 200)
            cfg_resp = json.loads(b_cfg.decode("utf-8"))["config"]
            self.assertEqual(cfg_resp["opensubtitles_api_key"], "")
            self.assertEqual(cfg_resp["opensubtitles_password"], "")
            self.assertTrue(cfg_resp["has_opensubtitles_api_key"])
            self.assertTrue(cfg_resp["has_opensubtitles_password"])
            self.assertEqual(cfg_resp["opensubtitles_username"], "demo_user")

            s_get, _, b_get = self._execute_request("GET", "/api/config")
            self.assertEqual(s_get, 200)
            self.assertEqual(json.loads(b_get.decode("utf-8"))["opensubtitles_api_key"], "")

            # 2. SSRF guard rejects localhost/http/untrusted domains
            for bad_url in ("http://127.0.0.1:8080/a.srt", "https://evil.example.com/a.srt"):
                s_bad, _, b_bad = self._execute_request(
                    "POST", "/api/subtitles/download",
                    body=json.dumps({
                        "provider": "podnapisi",
                        "download_url": bad_url,
                        "video_path": sub_vid,
                    }).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(s_bad, 400)
                self.assertIn("untrusted", json.loads(b_bad.decode("utf-8"))["error"].lower())

            # 3. Search & manual download work end-to-end
            s_search, _, b_search = self._execute_request(
                "POST", "/api/subtitles/search",
                body=json.dumps({"video_path": sub_vid, "language": "en"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(s_search, 200)
            candidates = json.loads(b_search.decode("utf-8"))["candidates"]
            self.assertGreaterEqual(len(candidates), 1)

            s_dl, _, b_dl = self._execute_request(
                "POST", "/api/subtitles/download",
                body=json.dumps({
                    "video_path": sub_vid,
                    "provider": candidates[0]["provider"],
                    "subtitle_id": candidates[0]["subtitle_id"],
                    "download_url": candidates[0]["download_url"],
                    "language": "en",
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(s_dl, 200)
            dl_info = json.loads(b_dl.decode("utf-8"))
            self.assertTrue(os.path.exists(dl_info["srt_path"]))
            self.assertEqual(dl_info["cue_count"], 1)

            # 4. Auto-fetch during /api/scan when external SRT is missing
            os.remove(dl_info["srt_path"])
            s_scan, _, b_scan = self._execute_request(
                "POST", "/api/scan",
                body=json.dumps({"video_path": sub_vid, "srt_path": ""}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(s_scan, 200)
            scan_data = json.loads(b_scan.decode("utf-8"))
            self.assertTrue(scan_data["subtitle_downloaded"])
            self.assertEqual(scan_data["subtitle_provider"], "podnapisi")
            self.assertEqual(len(scan_data["detections"]), 1)
        finally:
            server_module.Config = orig_config_cls
            StudioRequestHandler.subtitle_fetcher_factory = orig_factory

    def test_config_env_masking_and_non_persistence(self):
        cfg_path = os.path.join(self.test_dir, "env_isolated_config.json")
        env_path = os.path.join(self.test_dir, ".env")
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("OPENSUBTITLES_API_KEY=env-secret-key\n")
            f.write("OPENSUBTITLES_PASSWORD=env-secret-pw\n")

        orig_config_cls = server_module.Config
        cleared_env = {env_name: "" for env_name in ENV_VAR_MAP.values()}
        try:
            server_module.Config = lambda custom_path=None: orig_config_cls(
                custom_path=custom_path or cfg_path,
                env_path=env_path,
                load_env=True,
            )
            with mock.patch.dict(os.environ, cleared_env, clear=False):
                s_get, _, b_get = self._execute_request("GET", "/api/config")
                self.assertEqual(s_get, 200)
                get_data = json.loads(b_get.decode("utf-8"))
                self.assertEqual(get_data["opensubtitles_api_key"], "")
                self.assertTrue(get_data["has_opensubtitles_api_key"])
                self.assertTrue(get_data["opensubtitles_api_key_from_env"])
                self.assertEqual(get_data["opensubtitles_password"], "")
                self.assertTrue(get_data["has_opensubtitles_password"])
                self.assertTrue(get_data["opensubtitles_password_from_env"])

                s_post, _, b_post = self._execute_request(
                    "POST", "/api/config",
                    body=json.dumps({"subtitle_language": "es"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(s_post, 200)
                post_cfg = json.loads(b_post.decode("utf-8"))["config"]
                self.assertEqual(post_cfg["subtitle_language"], "es")
                self.assertEqual(post_cfg["opensubtitles_api_key"], "")
                self.assertTrue(post_cfg["has_opensubtitles_api_key"])
                self.assertTrue(post_cfg["opensubtitles_api_key_from_env"])

            with open(cfg_path, "r", encoding="utf-8") as f:
                saved_json = json.load(f)
            self.assertEqual(saved_json["subtitle_language"], "es")
            self.assertEqual(saved_json.get("opensubtitles_api_key", ""), "")
            self.assertEqual(saved_json.get("opensubtitles_password", ""), "")
        finally:
            server_module.Config = orig_config_cls

    def test_api_health_and_port_fallback(self):
        status, _, body = self._execute_request("GET", "/api/health")
        self.assertEqual(status, 200)
        health = json.loads(body.decode("utf-8"))
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["platform"], sys.platform)
        self.assertTrue(health["ffmpeg"])
        self.assertTrue(health["ffprobe"])
        self.assertTrue(health["ffmpeg_ok"])
        self.assertTrue(bool(health["install_hint"]))

        occupier = server_module.StudioHTTPServer(("127.0.0.1", 0), StudioRequestHandler)
        occupied_port = occupier.server_port
        fallback_srv = None
        try:
            fallback_srv = server_module.run_studio_server(
                host="127.0.0.1",
                port=occupied_port,
                max_port_attempts=10,
            )
            self.assertGreater(fallback_srv.server_port, occupied_port)
            self.assertLess(fallback_srv.server_port, occupied_port + 10)
        finally:
            if fallback_srv is not None:
                fallback_srv.server_close()
            occupier.server_close()

    def test_uploaded_video_export_path_tilde_resolution_and_custom_words(self):
        server_module.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        uploaded_vid = server_module.UPLOAD_DIR / "uploaded_movie.mp4"
        uploaded_srt = server_module.UPLOAD_DIR / "uploaded_movie.srt"
        shutil.copy2(self.vid_path, uploaded_vid)
        uploaded_srt.write_text(
            "1\n00:00:00,400 --> 00:00:01,500\nWhat the heck is that customswear?\n",
            encoding="utf-8",
        )

        with mock.patch.dict(os.environ, {"HOME": self.test_dir}, clear=False):
            s_scan, _, b_scan = self._execute_request(
                "POST", "/api/scan",
                body=json.dumps({
                    "video_path": "uploaded_movie.mp4",
                    "srt_path": "uploaded_movie.srt",
                    "preset": "moderate",
                    "custom_words": "heck, customswear",
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(s_scan, 200)
            scan_data = json.loads(b_scan.decode("utf-8"))
            self.assertTrue(scan_data["is_uploaded"])
            self.assertNotEqual(
                Path(scan_data["suggested_output"]).resolve().parent,
                server_module.UPLOAD_DIR.resolve(),
            )
            words = [d["word"].lower() for d in scan_data["detections"]]
            self.assertEqual(words, ["heck", "customswear"])
            self.assertTrue(all(d["tier"] == "custom" for d in scan_data["detections"]))

            s_proc, _, b_proc = self._execute_request(
                "POST", "/api/process",
                body=json.dumps({
                    "video_path": "uploaded_movie.mp4",
                    "output_path": "~/tilde_out.Cleaned.mp4",
                    "srt_path": "uploaded_movie.srt",
                    "mode": "mute",
                    "preset": "moderate",
                    "custom_words": ["heck", "customswear"],
                    "generate_clean_srt": True,
                    "detections": scan_data["detections"],
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(s_proc, 200, f"Unexpected process response: {b_proc}")
            expected_out = Path(self.test_dir) / "tilde_out.Cleaned.mp4"
            self.assertEqual(
                Path(json.loads(b_proc.decode("utf-8"))["output_path"]).resolve(),
                expected_out.resolve(),
            )

            job_state = {}
            for _ in range(50):
                _, _, p_body = self._execute_request("GET", "/api/progress")
                job_state = json.loads(p_body.decode("utf-8"))
                if job_state.get("status") in ("completed", "failed"):
                    break
                time.sleep(0.1)

            self.assertEqual(job_state.get("status"), "completed", f"Job failed: {job_state}")
            self.assertTrue(expected_out.exists())
            cleaned_srt = Path(job_state.get("cleaned_srt_path", ""))
            self.assertTrue(cleaned_srt.exists())
            srt_text = cleaned_srt.read_text(encoding="utf-8")
            self.assertNotIn("heck", srt_text.lower())
            self.assertNotIn("customswear", srt_text.lower())

    def test_missing_ffmpeg_returns_friendly_error(self):
        missing_deps = {
            "ffmpeg": False,
            "ffprobe": False,
            "ffmpeg_path": None,
            "ffprobe_path": None,
            "ok": False,
            "install_hint": "brew install ffmpeg",
        }
        with mock.patch.object(server_module.FFmpegProbe, "check_dependencies", return_value=missing_deps):
            s_scan, _, b_scan = self._execute_request(
                "POST", "/api/scan",
                body=json.dumps({"video_path": self.vid_path, "srt_path": self.srt_path}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(s_scan, 400)
            self.assertIn("brew install ffmpeg", json.loads(b_scan.decode("utf-8"))["error"])

            s_proc, _, b_proc = self._execute_request(
                "POST", "/api/process",
                body=json.dumps({
                    "video_path": self.vid_path,
                    "output_path": os.path.join(self.test_dir, "out.mp4"),
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(s_proc, 400)
            self.assertIn("brew install ffmpeg", json.loads(b_proc.decode("utf-8"))["error"])

    def test_ui_plug_and_play_markup_and_css(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")

        for expected_html in (
            '<meta name="color-scheme" content="dark">',
            '<dialog id="browserModal"',
            '<dialog id="progressModal"',
            '<dialog id="subtitleSearchDialog"',
            'id="customWordsInput"',
            'id="ffmpegWarningBanner"',
            'id="scanErrorBanner"',
            'id="playerCompatNotice"',
        ):
            self.assertIn(expected_html, html)

        for expected_css in (
            "color-scheme: dark;",
            'input[type="number"]',
            ".btn-close",
            ".btn-sm",
            ".error-msg",
            ".status-msg",
            ".browser-path-nav",
            ".browser-path-text",
            "dialog:modal",
            "dialog::backdrop",
            "overscroll-behavior: contain",
            "@media (max-width: 1024px)",
        ):
            self.assertIn(expected_css, css)


if __name__ == "__main__":
    unittest.main()
