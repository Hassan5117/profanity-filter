import json
import os
from pathlib import Path
from typing import Dict, Any, List

DEFAULT_CONFIG: Dict[str, Any] = {
    "mode": "mute",  # "mute", "bleep", "duck"
    "preset": "moderate",  # "strict", "moderate", "mild", "custom"
    "custom_words": [],
    "whitelist_words": [],
    "timing_mode": "word",  # "word" (targeted timing) or "segment" (full subtitle block)
    "padding_before": 0.30,  # seconds of padding before word (prevents cutting half-way)
    "padding_after": 0.30,  # seconds of padding after word
    "lead_in": 0.20,  # seconds to anticipate speech onset earlier
    "sync_offset": 0.0,  # global subtitle offset in seconds (+/-)
    "crossfade_duration": 0.04,  # seconds of audio ramp in/out to avoid clicks
    "ducking_volume": 0.06,  # ~ -24dB volume when ducking
    "bleep_frequency": 1000,  # Hz sine wave
    "center_channel_only": True,  # If 5.1 surround detected, mute only Center dialogue channel
    "generate_clean_srt": True,  # Generate [name].Cleaned.srt alongside output
    "censor_char": "*",  # Character for subtitle masking
    "output_suffix": ".Cleaned",  # e.g. Movie.Cleaned.mp4
    "watch_interval_seconds": 10,
    "file_stability_wait_seconds": 5,
}

CONFIG_DIR = Path.home() / ".config" / "censor_app"
CONFIG_FILE = CONFIG_DIR / "config.json"

class Config:
    def __init__(self, custom_path: str = None):
        self.config_path = Path(custom_path) if custom_path else CONFIG_FILE
        self.data = DEFAULT_CONFIG.copy()
        self.load()

    def load(self):
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    self.data.update(loaded)
            except Exception as e:
                print(f"[Warning] Failed to load config from {self.config_path}: {e}")

    def save(self):
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
        except Exception as e:
            print(f"[Warning] Failed to save config to {self.config_path}: {e}")

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value: Any):
        self.data[key] = value

    def to_dict(self) -> Dict[str, Any]:
        return self.data.copy()
