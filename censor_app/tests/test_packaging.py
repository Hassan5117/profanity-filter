import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import censor_app.core.config as config_module
import censor_app.core.ffmpeg_engine as ffmpeg_engine_module
import censor_app.web.server as server_module
from censor_app.core.config import Config, ENV_VAR_MAP
from censor_app.core.ffmpeg_engine import is_bundled_binary, resolve_media_binary
from censor_app.tests.test_web_server import MockServer, MockSocket
from censor_app.web.server import StudioRequestHandler


class TestPackagingSupport(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temp_dir.name)
        self.exe_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_resolve_media_binary_precedence(self):
        env_bin = self.tmp_path / "env_override" / self.exe_name
        env_bin.parent.mkdir(parents=True)
        env_bin.write_text("stub", encoding="utf-8")

        meipass_dir = self.tmp_path / "meipass"
        meipass_bin = meipass_dir / "bin" / self.exe_name
        meipass_bin.parent.mkdir(parents=True)
        meipass_bin.write_text("stub", encoding="utf-8")

        app_contents = self.tmp_path / "Movie Censor Studio.app" / "Contents"
        macos_exe = app_contents / "MacOS" / "MovieCensorStudio"
        macos_exe.parent.mkdir(parents=True)
        macos_exe.write_text("exe", encoding="utf-8")
        exe_dir_bin = macos_exe.parent / "bin" / self.exe_name
        exe_dir_bin.parent.mkdir(parents=True)
        exe_dir_bin.write_text("stub", encoding="utf-8")
        resources_bin = app_contents / "Resources" / "bin" / self.exe_name
        resources_bin.parent.mkdir(parents=True)
        resources_bin.write_text("stub", encoding="utf-8")

        # 1. CENSOR_FFMPEG_PATH wins over everything
        with mock.patch.dict(os.environ, {"CENSOR_FFMPEG_PATH": str(env_bin)}, clear=False):
            with mock.patch.object(ffmpeg_engine_module.sys, "frozen", True, create=True):
                with mock.patch.object(ffmpeg_engine_module.sys, "_MEIPASS", str(meipass_dir), create=True):
                    self.assertEqual(resolve_media_binary("ffmpeg"), str(env_bin))

        # 2. Frozen sys._MEIPASS/bin wins over exe_dir/bin and PATH
        with mock.patch.dict(os.environ, {"CENSOR_FFMPEG_PATH": ""}, clear=False):
            with mock.patch.object(ffmpeg_engine_module.sys, "frozen", True, create=True):
                with mock.patch.object(ffmpeg_engine_module.sys, "_MEIPASS", str(meipass_dir), create=True):
                    with mock.patch.object(ffmpeg_engine_module.sys, "executable", str(macos_exe)):
                        resolved = resolve_media_binary("ffmpeg")
                        self.assertEqual(Path(resolved).resolve(), meipass_bin.resolve())
                        self.assertTrue(is_bundled_binary(resolved))

        # 3. Frozen exe_dir/bin works when _MEIPASS has no binary
        with mock.patch.dict(os.environ, {"CENSOR_FFMPEG_PATH": ""}, clear=False):
            with mock.patch.object(ffmpeg_engine_module.sys, "frozen", True, create=True):
                with mock.patch.object(ffmpeg_engine_module.sys, "_MEIPASS", str(self.tmp_path / "empty_mp"), create=True):
                    with mock.patch.object(ffmpeg_engine_module.sys, "executable", str(macos_exe)):
                        resolved = resolve_media_binary("ffmpeg")
                        self.assertEqual(Path(resolved).resolve(), exe_dir_bin.resolve())
                        self.assertTrue(is_bundled_binary(resolved))

        # 4. macOS Contents/Resources/bin fallback when exe_dir/bin is absent
        exe_dir_bin.unlink()
        with mock.patch.dict(os.environ, {"CENSOR_FFMPEG_PATH": ""}, clear=False):
            with mock.patch.object(ffmpeg_engine_module.sys, "frozen", True, create=True):
                with mock.patch.object(ffmpeg_engine_module.sys, "_MEIPASS", str(self.tmp_path / "empty_mp"), create=True):
                    with mock.patch.object(ffmpeg_engine_module.sys, "executable", str(macos_exe)):
                        resolved = resolve_media_binary("ffmpeg")
                        self.assertEqual(Path(resolved).resolve(), resources_bin.resolve())
                        self.assertTrue(is_bundled_binary(resolved))

        # 5. System PATH fallback & is_bundled_binary == False outside bundle
        with mock.patch.dict(os.environ, {"CENSOR_FFMPEG_PATH": ""}, clear=False):
            with mock.patch.object(ffmpeg_engine_module.sys, "frozen", False, create=True):
                with mock.patch.object(ffmpeg_engine_module.shutil, "which", return_value="/usr/local/bin/ffmpeg"):
                    self.assertEqual(resolve_media_binary("ffmpeg"), "/usr/local/bin/ffmpeg")
                    self.assertFalse(is_bundled_binary("/usr/local/bin/ffmpeg"))

        with mock.patch.object(ffmpeg_engine_module.sys, "frozen", True, create=True):
            with mock.patch.object(ffmpeg_engine_module.sys, "_MEIPASS", str(meipass_dir), create=True):
                with mock.patch.object(ffmpeg_engine_module.sys, "executable", str(macos_exe)):
                    self.assertFalse(is_bundled_binary("/usr/local/bin/ffmpeg"))

    def test_resolve_static_dir_frozen_and_source(self):
        repo_static = Path(server_module.__file__).resolve().parent / "static"
        with mock.patch.object(server_module.sys, "frozen", False, create=True):
            self.assertEqual(server_module._resolve_static_dir(), repo_static)

        meipass_dir = self.tmp_path / "meipass_static"
        frozen_static = meipass_dir / "censor_app" / "web" / "static"
        frozen_static.mkdir(parents=True)
        with mock.patch.object(server_module.sys, "frozen", True, create=True):
            with mock.patch.object(server_module.sys, "_MEIPASS", str(meipass_dir), create=True):
                self.assertEqual(server_module._resolve_static_dir(), frozen_static)

    def test_frozen_dotenv_discovery_for_macos_app_and_windows_exe(self):
        cleared_env = {name: "" for name in ENV_VAR_MAP.values()}
        cfg_dir = self.tmp_path / "user_cfg"
        cfg_file = cfg_dir / "config.json"

        # macOS .app: .env placed beside "Movie Censor Studio.app"
        app_parent = self.tmp_path / "Applications"
        macos_exe = app_parent / "Movie Censor Studio.app" / "Contents" / "MacOS" / "MovieCensorStudio"
        macos_exe.parent.mkdir(parents=True)
        macos_exe.write_text("exe", encoding="utf-8")
        (app_parent / ".env").write_text("OPENSUBTITLES_API_KEY=mac-bundle-key\n", encoding="utf-8")

        # Windows .exe: .env placed beside MovieCensorStudio.exe
        win_dir = self.tmp_path / "WinDist"
        win_exe = win_dir / "MovieCensorStudio.exe"
        win_dir.mkdir(parents=True)
        win_exe.write_text("exe", encoding="utf-8")
        (win_dir / ".env").write_text("OPENSUBTITLES_API_KEY=win-exe-key\n", encoding="utf-8")

        empty_cwd = self.tmp_path / "empty_cwd"
        empty_cwd.mkdir()

        with mock.patch.dict(os.environ, cleared_env, clear=False):
            with mock.patch.object(config_module, "CONFIG_DIR", cfg_dir):
                with mock.patch.object(config_module.sys, "frozen", True, create=True):
                    with mock.patch("pathlib.Path.cwd", return_value=empty_cwd):
                        with mock.patch.object(config_module.sys, "executable", str(macos_exe)):
                            cfg_mac = Config(custom_path=str(cfg_file))
                            self.assertEqual(cfg_mac.get("opensubtitles_api_key"), "mac-bundle-key")

                        with mock.patch.object(config_module.sys, "executable", str(win_exe)):
                            cfg_win = Config(custom_path=str(cfg_file))
                            self.assertEqual(cfg_win.get("opensubtitles_api_key"), "win-exe-key")

    def test_health_includes_bundled_and_shutdown_endpoint_works(self):
        def _exec(method: str, path: str, body: bytes = b""):
            req = (
                f"{method} {path} HTTP/1.1\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Content-Type: application/json\r\n\r\n"
            ).encode("utf-8") + body
            sock = MockSocket(req)
            StudioRequestHandler(sock, ("127.0.0.1", 12345), MockServer())
            raw = sock.wfile.getvalue()
            status = int(raw.split(b"\r\n")[0].decode("utf-8").split(" ", 2)[1])
            payload = json.loads(raw.split(b"\r\n\r\n", 1)[1].decode("utf-8"))
            return status, payload

        s_health, health = _exec("GET", "/api/health")
        self.assertEqual(s_health, 200)
        self.assertIn("bundled", health)
        self.assertIsInstance(health["bundled"], bool)

        s_shut, shut = _exec("POST", "/api/shutdown", b"{}")
        self.assertEqual(s_shut, 200)
        self.assertEqual(shut["status"], "shutting_down")


if __name__ == "__main__":
    unittest.main()
