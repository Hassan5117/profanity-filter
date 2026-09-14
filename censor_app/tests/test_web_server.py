import unittest
import io
import json
import os
import shutil
import tempfile
import subprocess
from pathlib import Path
from http import HTTPStatus
from censor_app.web.server import StudioRequestHandler, STATIC_DIR

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

        with open(self.srt_path, "w", encoding="utf-8") as f:
            f.write("1\n00:00:00,500 --> 00:00:01,500\nThis is a fucking test!\n")

    def tearDown(self):
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


if __name__ == "__main__":
    unittest.main()
