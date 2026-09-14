#!/usr/bin/env python3
"""
Movie Censor Studio - Quick Launcher
Run `python3 app.py` to start the interactive web application.
"""

import os
import sys
import argparse

current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from censor_app.cli import run_studio_command

def main():
    parser = argparse.ArgumentParser(description="Launch Movie Censor Studio Web App")
    parser.add_argument("--port", type=int, default=8000, help="Web server port (default: 8000)")
    parser.add_argument("--host", default="127.0.0.1", help="Web server host (default: 127.0.0.1)")
    parser.add_argument("--no-browser", action="store_true", help="Do not automatically open browser")
    args = parser.parse_args()

    run_studio_command(args)

if __name__ == "__main__":
    main()
