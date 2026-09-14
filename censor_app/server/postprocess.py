#!/usr/bin/env python3
"""
Post-processing hook for media managers (Radarr, Sonarr, qBittorrent, manual scripts).
Reads target file path from environment variables or command-line arguments.
"""

import os
import sys
from pathlib import Path
from censor_app.server.watcher import MediaWatcher
from censor_app.core.config import Config

def get_target_media_path() -> Path:
    # 1. Check Radarr environment variable
    if os.environ.get("radarr_moviefile_path"):
        return Path(os.environ["radarr_moviefile_path"])
    
    # 2. Check Sonarr environment variable
    if os.environ.get("sonarr_episodefile_path"):
        return Path(os.environ["sonarr_episodefile_path"])

    # 3. Check CLI argument
    if len(sys.argv) > 1:
        return Path(sys.argv[1])

    return None

def main():
    target = get_target_media_path()
    if not target:
        print("Usage: python3 -m censor_app.server.postprocess <path-to-video-file>")
        print("Or run from Radarr/Sonarr custom script hook with environment variables set.")
        sys.exit(1)

    if not target.exists():
        print(f"[Error] Target file does not exist: {target}")
        sys.exit(1)

    print(f"[PostProcess] Initiating profanity censorship for: {target}")
    config = Config()
    watcher = MediaWatcher(watch_dir=str(target.parent), config=config)
    
    success = watcher.process_single_video(target)
    if success:
        print(f"[PostProcess] Success! Cleaned media created alongside original.")
        sys.exit(0)
    else:
        print(f"[PostProcess] Finished (no action required or error encountered).")
        sys.exit(0)

if __name__ == "__main__":
    main()
