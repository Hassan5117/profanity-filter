import json
import os
import re
from pathlib import Path
from typing import Dict, Any, Optional, Set

DEFAULT_CONFIG: Dict[str, Any] = {
    "mode": "mute",  # "mute", "bleep", "duck"
    "preset": "moderate",  # "strict", "moderate", "mild", "custom"
    "custom_words": [],
    "whitelist_words": [],
    "timing_mode": "word",  # "word" / "smart" (targeted timing) or "segment" (full subtitle block)
    "padding_before": 0.18,  # seconds of padding before word (prevents cutting half-way)
    "padding_after": 0.16,  # seconds of padding after word
    "lead_in": 0.10,  # seconds to anticipate speech onset earlier
    "audio_refine": True,  # Use FFmpeg waveform energy to trim silence & snap to pauses
    "merge_gap": 0.25,  # seconds gap below which adjacent mute intervals are merged
    "sync_offset": 0.0,  # global subtitle offset in seconds (+/-)
    "crossfade_duration": 0.04,  # seconds of audio ramp in/out to avoid clicks
    "ducking_volume": 0.06,  # ~ -24dB volume when ducking
    "bleep_frequency": 1000,  # Hz sine wave
    "center_channel_only": True,  # If 5.1 surround detected, mute only Center dialogue channel
    "generate_clean_srt": True,  # Generate [name].Cleaned.srt alongside output
    "censor_char": "*",  # Character for subtitle masking
    "output_suffix": ".Cleaned",  # e.g. Movie.Cleaned.mp4
    "output_dir": None,  # Optional separate destination directory for cleaned media
    "watch_interval_seconds": 10,
    "file_stability_wait_seconds": 5,
    "auto_download_subtitles": True,  # Search trusted online subtitle providers when missing locally
    "subtitle_language": "en",  # Preferred ISO-639 language code for online subtitle search
    "opensubtitles_api_key": "",  # Optional OpenSubtitles REST API key
    "opensubtitles_username": "",  # Optional OpenSubtitles account username (for download quota/JWT)
    "opensubtitles_password": "",  # Optional OpenSubtitles account password
    "subdl_api_key": "",  # Optional SubDL API key
    "subtitle_retry_cooldown_seconds": 3600,  # Cooldown before re-querying online subtitles for same file
}

CONFIG_DIR = Path.home() / ".config" / "censor_app"
CONFIG_FILE = CONFIG_DIR / "config.json"

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"

ENV_VAR_MAP: Dict[str, str] = {
    "opensubtitles_api_key": "OPENSUBTITLES_API_KEY",
    "opensubtitles_username": "OPENSUBTITLES_USERNAME",
    "opensubtitles_password": "OPENSUBTITLES_PASSWORD",
    "subdl_api_key": "SUBDL_API_KEY",
}

_DOUBLE_QUOTE_ESCAPES: Dict[str, str] = {
    "\\": "\\",
    '"': '"',
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


def parse_dotenv_file(env_path: Path) -> Dict[str, str]:
    """Parse key=value pairs from a .env file using only Python stdlib."""
    if not env_path.exists() or not env_path.is_file():
        return {}

    try:
        content = env_path.read_text(encoding="utf-8-sig")
    except OSError as e:
        print(f"[Warning] Failed to read .env file {env_path}: {e}")
        return {}

    parsed: Dict[str, str] = {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        stripped = re.sub(r"^export\s+", "", stripped)
        if "=" not in stripped:
            continue

        key, raw_val = stripped.split("=", 1)
        key = key.strip()
        if not key:
            continue

        raw_val = raw_val.strip()
        if raw_val.startswith('"'):
            chars = []
            i = 1
            closed = False
            while i < len(raw_val):
                ch = raw_val[i]
                if ch == "\\":
                    if i + 1 < len(raw_val):
                        nxt = raw_val[i + 1]
                        if nxt in _DOUBLE_QUOTE_ESCAPES:
                            chars.append(_DOUBLE_QUOTE_ESCAPES[nxt])
                        else:
                            chars.append("\\" + nxt)
                        i += 2
                        continue
                    chars.append("\\")
                    i += 1
                    continue
                if ch == '"':
                    closed = True
                    break
                chars.append(ch)
                i += 1
            val = "".join(chars) if closed else raw_val
        elif raw_val.startswith("'"):
            end_idx = raw_val.find("'", 1)
            val = raw_val[1:end_idx] if end_idx != -1 else raw_val
        else:
            val = re.split(r"\s+#", raw_val, maxsplit=1)[0].strip()

        parsed[key] = val

    return parsed


class Config:
    def __init__(
        self,
        custom_path: Optional[str] = None,
        env_path: Optional[str] = None,
        load_env: bool = True,
    ):
        self.config_path = Path(custom_path) if custom_path else CONFIG_FILE
        self.env_path = Path(env_path) if env_path else None
        self.load_env = load_env
        self.data: Dict[str, Any] = DEFAULT_CONFIG.copy()
        self._env_values: Dict[str, str] = {}
        self._explicit_overrides: Set[str] = set()
        self.load()

    def load(self):
        self.data = DEFAULT_CONFIG.copy()
        self._env_values = {}
        self._explicit_overrides = set()
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        self.data.update(loaded)
            except Exception as e:
                print(f"[Warning] Failed to load config from {self.config_path}: {e}")
        if self.load_env:
            self._load_env_overrides()

    def _load_env_overrides(self):
        dotenv_vals: Dict[str, str] = {}
        if self.env_path is not None:
            for k, v in parse_dotenv_file(self.env_path).items():
                if v.strip():
                    dotenv_vals[k] = v.strip()
        else:
            candidates = [DEFAULT_ENV_FILE]
            try:
                cwd_env = Path.cwd() / ".env"
                if cwd_env.resolve() != DEFAULT_ENV_FILE.resolve():
                    candidates.append(cwd_env)
            except OSError:
                pass
            for candidate in candidates:
                for k, v in parse_dotenv_file(candidate).items():
                    if v.strip():
                        dotenv_vals[k] = v.strip()

        for cfg_key, env_name in ENV_VAR_MAP.items():
            os_val = os.environ.get(env_name)
            if os_val is not None and os_val.strip():
                self._env_values[cfg_key] = os_val.strip()
            elif env_name in dotenv_vals and dotenv_vals[env_name].strip():
                self._env_values[cfg_key] = dotenv_vals[env_name].strip()

    def save(self):
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            try:
                os.chmod(self.config_path, 0o600)
            except OSError:
                pass
        except Exception as e:
            print(f"[Warning] Failed to save config to {self.config_path}: {e}")

    def get(self, key: str, default=None):
        if key not in self._explicit_overrides and key in self._env_values:
            return self._env_values[key]
        return self.data.get(key, default)

    def set(self, key: str, value: Any):
        self.data[key] = value
        self._explicit_overrides.add(key)

    def is_from_env(self, key: str) -> bool:
        return key not in self._explicit_overrides and key in self._env_values

    def to_dict(self) -> Dict[str, Any]:
        merged = self.data.copy()
        for key, value in self._env_values.items():
            if key not in self._explicit_overrides:
                merged[key] = value
        return merged
