import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import censor_app.core.config as config_module
from censor_app.core.config import Config, ENV_VAR_MAP, parse_dotenv_file
from censor_app.core.subtitle_fetcher import MediaQuery, SubtitleFetcher


def _cleared_subtitle_env(**overrides: str) -> dict:
    env = {name: "" for name in ENV_VAR_MAP.values()}
    env.update(overrides)
    return env


class TestConfigAndDotenv(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temp_dir.name)
        self.cfg_path = self.tmp_path / "config.json"
        self.env_path = self.tmp_path / ".env"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_parse_dotenv_file_features(self):
        missing = self.tmp_path / "does_not_exist.env"
        self.assertEqual(parse_dotenv_file(missing), {})

        content = (
            "\ufeff"
            "# Leading comment\n"
            "   \n"
            "INVALID_LINE_NO_EQUALS\n"
            "=missing_key\n"
            "PLAIN_KEY=plain_value\n"
            "export EXPORTED_KEY=exported_value\n"
            "export   SPACED_KEY =  trimmed_val  # trailing inline comment\n"
            'DOUBLE_QUOTED="line1\\nline2\\t\\"quoted\\"\\\\slash#notcomment" # after quote\n'
            "SINGLE_QUOTED='raw\\n#literal' # after single quote\n"
            'UNCLOSED_DOUBLE="raw_unclosed\n'
            "UNCLOSED_SINGLE='raw_single_unclosed\n"
            "HASH_IN_PASSWORD=my#secret#pass\n"
            "EMPTY_KEY=\n"
        )
        self.env_path.write_text(content, encoding="utf-8")
        parsed = parse_dotenv_file(self.env_path)

        self.assertEqual(parsed["PLAIN_KEY"], "plain_value")
        self.assertEqual(parsed["EXPORTED_KEY"], "exported_value")
        self.assertEqual(parsed["SPACED_KEY"], "trimmed_val")
        self.assertEqual(parsed["DOUBLE_QUOTED"], 'line1\nline2\t"quoted"\\slash#notcomment')
        self.assertEqual(parsed["SINGLE_QUOTED"], "raw\\n#literal")
        self.assertEqual(parsed["UNCLOSED_DOUBLE"], '"raw_unclosed')
        self.assertEqual(parsed["UNCLOSED_SINGLE"], "'raw_single_unclosed")
        self.assertEqual(parsed["HASH_IN_PASSWORD"], "my#secret#pass")
        self.assertEqual(parsed["EMPTY_KEY"], "")
        self.assertNotIn("INVALID_LINE_NO_EQUALS", parsed)

    def test_precedence_defaults_json_dotenv_environ_set(self):
        with mock.patch.dict(os.environ, _cleared_subtitle_env(), clear=False):
            cfg = Config(custom_path=str(self.cfg_path), env_path=str(self.env_path))
            self.assertEqual(cfg.get("opensubtitles_api_key"), "")
            self.assertFalse(cfg.is_from_env("opensubtitles_api_key"))

            # config.json overrides defaults
            self.cfg_path.write_text(json.dumps({"opensubtitles_api_key": "from-json"}), encoding="utf-8")
            cfg.load()
            self.assertEqual(cfg.get("opensubtitles_api_key"), "from-json")
            self.assertFalse(cfg.is_from_env("opensubtitles_api_key"))

            # .env overrides config.json
            self.env_path.write_text("OPENSUBTITLES_API_KEY=from-dotenv\n", encoding="utf-8")
            cfg.load()
            self.assertEqual(cfg.get("opensubtitles_api_key"), "from-dotenv")
            self.assertEqual(cfg.to_dict()["opensubtitles_api_key"], "from-dotenv")
            self.assertTrue(cfg.is_from_env("opensubtitles_api_key"))

        # os.environ overrides .env and config.json
        with mock.patch.dict(os.environ, _cleared_subtitle_env(OPENSUBTITLES_API_KEY="from-environ"), clear=False):
            cfg.load()
            self.assertEqual(cfg.get("opensubtitles_api_key"), "from-environ")
            self.assertEqual(cfg.to_dict()["opensubtitles_api_key"], "from-environ")
            self.assertTrue(cfg.is_from_env("opensubtitles_api_key"))

            # Explicit config.set() overrides everything
            cfg.set("opensubtitles_api_key", "from-set")
            self.assertEqual(cfg.get("opensubtitles_api_key"), "from-set")
            self.assertEqual(cfg.to_dict()["opensubtitles_api_key"], "from-set")
            self.assertFalse(cfg.is_from_env("opensubtitles_api_key"))

    def test_blank_dotenv_and_environ_do_not_override_json(self):
        self.cfg_path.write_text(json.dumps({"opensubtitles_api_key": "saved-in-json"}), encoding="utf-8")
        self.env_path.write_text("OPENSUBTITLES_API_KEY=\nSUBDL_API_KEY=   \n", encoding="utf-8")

        with mock.patch.dict(os.environ, _cleared_subtitle_env(OPENSUBTITLES_API_KEY="   "), clear=False):
            cfg = Config(custom_path=str(self.cfg_path), env_path=str(self.env_path))
            self.assertEqual(cfg.get("opensubtitles_api_key"), "saved-in-json")
            self.assertFalse(cfg.is_from_env("opensubtitles_api_key"))

    def test_save_does_not_leak_env_secrets_and_sets_0600(self):
        self.env_path.write_text(
            "OPENSUBTITLES_API_KEY=env-secret\n"
            "SUBDL_API_KEY=subdl-secret\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, _cleared_subtitle_env(), clear=False):
            cfg = Config(custom_path=str(self.cfg_path), env_path=str(self.env_path))
            cfg.set("subtitle_language", "fr")
            cfg.save()

        raw_saved = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(raw_saved["subtitle_language"], "fr")
        self.assertEqual(raw_saved["opensubtitles_api_key"], "")
        self.assertEqual(raw_saved["subdl_api_key"], "")

        if os.name == "posix":
            self.assertEqual(self.cfg_path.stat().st_mode & 0o777, 0o600)

    def test_load_env_false_ignores_dotenv_and_environ(self):
        self.env_path.write_text("OPENSUBTITLES_API_KEY=from-dotenv\n", encoding="utf-8")
        with mock.patch.dict(os.environ, _cleared_subtitle_env(OPENSUBTITLES_API_KEY="from-environ"), clear=False):
            cfg = Config(custom_path=str(self.cfg_path), env_path=str(self.env_path), load_env=False)
            self.assertEqual(cfg.get("opensubtitles_api_key"), "")
            self.assertFalse(cfg.is_from_env("opensubtitles_api_key"))

    def test_is_from_env_transitions_with_set_and_load(self):
        self.env_path.write_text("OPENSUBTITLES_API_KEY=from-dotenv\n", encoding="utf-8")
        with mock.patch.dict(os.environ, _cleared_subtitle_env(), clear=False):
            cfg = Config(custom_path=str(self.cfg_path), env_path=str(self.env_path))
            self.assertTrue(cfg.is_from_env("opensubtitles_api_key"))

            cfg.set("opensubtitles_api_key", "manual-key")
            self.assertFalse(cfg.is_from_env("opensubtitles_api_key"))
            self.assertEqual(cfg.get("opensubtitles_api_key"), "manual-key")

            cfg.load()
            self.assertTrue(cfg.is_from_env("opensubtitles_api_key"))
            self.assertEqual(cfg.get("opensubtitles_api_key"), "from-dotenv")

    def test_default_env_discovery_project_root_and_cwd(self):
        root_dir = self.tmp_path / "proj_root"
        cwd_dir = self.tmp_path / "other_cwd"
        root_dir.mkdir()
        cwd_dir.mkdir()
        root_env = root_dir / ".env"
        cwd_env = cwd_dir / ".env"
        root_env.write_text(
            "OPENSUBTITLES_API_KEY=from-root\nSUBDL_API_KEY=root-subdl\n",
            encoding="utf-8",
        )
        cwd_env.write_text("OPENSUBTITLES_API_KEY=from-cwd\n", encoding="utf-8")

        with mock.patch.dict(os.environ, _cleared_subtitle_env(), clear=False):
            with mock.patch.object(config_module, "DEFAULT_ENV_FILE", root_env):
                with mock.patch("pathlib.Path.cwd", return_value=cwd_dir):
                    cfg = Config(custom_path=str(self.cfg_path))
                    self.assertEqual(cfg.get("opensubtitles_api_key"), "from-cwd")
                    self.assertEqual(cfg.get("subdl_api_key"), "root-subdl")

    def test_subtitle_fetcher_uses_opensubtitles_api_key_from_dotenv(self):
        self.env_path.write_text("OPENSUBTITLES_API_KEY=dotenv-os-key-123\n", encoding="utf-8")
        seen_headers = {}

        def mock_http(url, method="GET", headers=None, body=None, timeout=10.0, max_bytes=10485760):
            if "api.opensubtitles.com/api/v1/subtitles" in url:
                seen_headers.update(headers or {})
                return 200, {}, json.dumps({
                    "data": [{
                        "id": "101",
                        "attributes": {
                            "language": "en",
                            "release": "Inception.2010.1080p.BluRay",
                            "download_count": 500,
                            "moviehash_match": False,
                            "feature_details": {"movie_name": "Inception", "year": 2010},
                            "files": [{"file_id": 888, "file_name": "inception.srt"}],
                        },
                    }]
                }).encode("utf-8")
            return 404, {}, b""

        with mock.patch.dict(os.environ, _cleared_subtitle_env(), clear=False):
            cfg = Config(custom_path=str(self.cfg_path), env_path=str(self.env_path))
            fetcher = SubtitleFetcher(config=cfg, http_client=mock_http)
            candidates = fetcher.search(
                MediaQuery(title="Inception", year=2010, language="en"),
                providers=["opensubtitles"],
            )

        self.assertEqual(seen_headers.get("Api-Key"), "dotenv-os-key-123")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].provider, "opensubtitles")
        self.assertEqual(candidates[0].download_id, "888")


if __name__ == "__main__":
    unittest.main()
