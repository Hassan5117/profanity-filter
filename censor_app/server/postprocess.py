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
        print("Usage: python3 -m censor_app.server.postprocess <path-to-video-file> [output-directory]")
        print("Or run from Radarr/Sonarr custom script hook with environment variables set.")
        sys.exit(1)

    if not target.exists():
        print(f"[Error] Target file does not exist: {target}")
        sys.exit(1)

    output_dir = os.environ.get("CENSOR_OUTPUT_DIR") or (sys.argv[2] if len(sys.argv) > 2 else None)

    print(f"[PostProcess] Initiating profanity censorship for: {target}")
    config = Config()
    if output_dir:
        config.set("output_dir", output_dir)
    watcher = MediaWatcher(watch_dir=str(target.parent), output_dir=output_dir, config=config)
    
    success = watcher.process_single_video(target)
    if success:
        dest_msg = f"in {output_dir}" if output_dir else "alongside original"
        print(f"[PostProcess] Success! Cleaned media created {dest_msg}.")
        sys.exit(0)
    else:
        print(f"[PostProcess] Finished (no action required or error encountered).")
        sys.exit(0)

if __name__ == "__main__":
    main()
