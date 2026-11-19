#!/usr/bin/env python3
"""
Standalone executable builder for Movie Censor Studio (Windows & macOS).

Uses only Python standard library + PyInstaller to:
1. Optionally download verified static ffmpeg & ffprobe builds for the host OS.
2. Build a standalone bundle via packaging/movie_censor_studio.spec.
3. Smoke-test the built executable with a stripped PATH to confirm bundled FFmpeg works.
4. Optionally package the output into a distributable .zip archive.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = REPO_ROOT / "packaging" / "movie_censor_studio.spec"
VENDOR_ROOT = REPO_ROOT / "packaging" / "vendor"
DIST_DIR = REPO_ROOT / "dist"
BUILD_DIR = REPO_ROOT / "build"

WIN_FFMPEG_URLS = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
)


def _exe_suffix() -> str:
    return ".exe" if sys.platform == "win32" else ""


def _default_vendor_dir() -> Path:
    if sys.platform == "darwin":
        arch = platform.machine().lower()
        norm_arch = "arm64" if arch in ("arm64", "aarch64") else ("amd64" if arch in ("x86_64", "amd64") else arch)
        return VENDOR_ROOT / f"darwin-{norm_arch}"
    if sys.platform == "win32":
        return VENDOR_ROOT / "win64"
    return VENDOR_ROOT / f"{sys.platform}-{platform.machine().lower()}"


def _verify_binary_runs(binary_path: Path) -> bool:
    if not binary_path.is_file():
        return False
    try:
        res = subprocess.run(
            [str(binary_path), "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        return res.returncode == 0
    except Exception:
        return False


def _verify_macos_static(binary_path: Path) -> None:
    res = subprocess.run(
        ["otool", "-L", str(binary_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    for line in res.stdout.splitlines()[1:]:
        dep = line.strip().split(" ", 1)[0]
        if dep.startswith("/opt/homebrew/") or dep.startswith("/usr/local/opt/") or dep.startswith("/usr/local/Cellar/"):
            raise RuntimeError(
                f"{binary_path.name} is dynamically linked to {dep} instead of a portable static build."
            )


def _download_file(url: str, dest_path: Path) -> None:
    print(f"[download] Fetching {url} ...")
    req = urllib.request.Request(url, headers={"User-Agent": "MovieCensorStudio-Builder/1.0"})
    with urllib.request.urlopen(req, timeout=180) as resp, open(dest_path, "wb") as out_f:
        shutil.copyfileobj(resp, out_f)


def _extract_binaries_from_zip(zip_path: Path, dest_dir: Path, names: tuple[str, ...]) -> None:
    wanted = {n.lower(): n for n in names}
    found = set()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            base = os.path.basename(member.filename).lower()
            if base in wanted and base not in found:
                target = dest_dir / wanted[base]
                with zf.open(member, "r") as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                if sys.platform != "win32":
                    os.chmod(target, 0o755)
                found.add(base)
    missing = set(wanted.keys()) - found
    if missing:
        raise RuntimeError(f"Archive {zip_path.name} did not contain: {', '.join(sorted(missing))}")


def download_static_ffmpeg(dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    suffix = _exe_suffix()
    ffmpeg_bin = dest_dir / f"ffmpeg{suffix}"
    ffprobe_bin = dest_dir / f"ffprobe{suffix}"

    if _verify_binary_runs(ffmpeg_bin) and _verify_binary_runs(ffprobe_bin):
        if sys.platform == "darwin":
            _verify_macos_static(ffmpeg_bin)
            _verify_macos_static(ffprobe_bin)
        print(f"[ffmpeg] Using existing verified static binaries in {dest_dir}")
        return dest_dir

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        if sys.platform == "win32":
            last_err: Optional[Exception] = None
            zip_file = tmp_dir / "ffmpeg-win64.zip"
            for url in WIN_FFMPEG_URLS:
                try:
                    _download_file(url, zip_file)
                    _extract_binaries_from_zip(zip_file, dest_dir, ("ffmpeg.exe", "ffprobe.exe"))
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    print(f"[warn] Download failed from {url}: {e}")
            if last_err is not None:
                raise RuntimeError(f"Failed to download static Windows FFmpeg builds: {last_err}") from last_err
        elif sys.platform == "darwin":
            arch_raw = platform.machine().lower()
            arch = "arm64" if arch_raw in ("arm64", "aarch64") else "amd64"
            for tool in ("ffmpeg", "ffprobe"):
                url = f"https://ffmpeg.martin-riedl.de/redirect/latest/macos/{arch}/release/{tool}.zip"
                zip_file = tmp_dir / f"{tool}.zip"
                _download_file(url, zip_file)
                _extract_binaries_from_zip(zip_file, dest_dir, (tool,))
                _verify_macos_static(dest_dir / tool)
        else:
            raise RuntimeError(
                "Automatic static FFmpeg download is configured for Windows and macOS. "
                "On Linux, pass --ffmpeg-dir <dir> or --skip-ffmpeg."
            )

    for tool_path in (ffmpeg_bin, ffprobe_bin):
        if not _verify_binary_runs(tool_path):
            raise RuntimeError(f"Downloaded binary failed '-version' check: {tool_path}")

    print(f"[ffmpeg] Static binaries ready in {dest_dir}")
    return dest_dir


def _built_executable_path() -> Path:
    if sys.platform == "darwin":
        return DIST_DIR / "Movie Censor Studio.app" / "Contents" / "MacOS" / "MovieCensorStudio"
    if sys.platform == "win32":
        return DIST_DIR / "MovieCensorStudio" / "MovieCensorStudio.exe"
    return DIST_DIR / "MovieCensorStudio" / "MovieCensorStudio"


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def run_smoke_test(expect_bundled: bool) -> None:
    exe_path = _built_executable_path()
    if not exe_path.is_file():
        raise RuntimeError(f"Built executable not found at {exe_path}")

    port = _pick_free_port()
    env = os.environ.copy()
    if expect_bundled:
        if sys.platform == "win32":
            env["PATH"] = os.path.join(env.get("SystemRoot", r"C:\Windows"), "System32")
        else:
            env["PATH"] = "/usr/bin:/bin"

    print(f"[smoke] Launching {exe_path.name} on 127.0.0.1:{port} (expect_bundled={expect_bundled})...")
    proc = subprocess.Popen(
        [str(exe_path), "--no-browser", "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )

    try:
        health = None
        deadline = time.monotonic() + 20.0
        health_url = f"http://127.0.0.1:{port}/api/health"
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                raise RuntimeError(f"Executable exited early with code {proc.returncode}:\n{out}")
            try:
                with urllib.request.urlopen(health_url, timeout=1.5) as resp:
                    if resp.status == 200:
                        health = json.loads(resp.read().decode("utf-8"))
                        break
            except Exception:
                time.sleep(0.3)

        if not health:
            raise RuntimeError(f"Timed out waiting for {health_url}")

        if health.get("status") != "ok":
            raise RuntimeError(f"Unexpected /api/health payload: {health}")
        if expect_bundled:
            if not health.get("ffmpeg_ok"):
                raise RuntimeError(f"Bundled FFmpeg check failed: {health}")
            if not health.get("bundled"):
                raise RuntimeError(f"Expected bundled=True in /api/health, got: {health}")

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5.0) as resp:
            html = resp.read().decode("utf-8", errors="replace")
            if "Movie Censor Studio" not in html:
                raise RuntimeError("Root UI page did not render expected title.")

        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/shutdown",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            shutdown_data = json.loads(resp.read().decode("utf-8"))
            if shutdown_data.get("status") != "shutting_down":
                raise RuntimeError(f"Unexpected /api/shutdown response: {shutdown_data}")

        rc = proc.wait(timeout=10)
        if rc != 0:
            out = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"Executable exited with non-zero code {rc} on shutdown:\n{out}")
        print(f"[smoke] OK: /api/health={health}")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if proc.stdout:
            proc.stdout.close()


def create_archive() -> Path:
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        arch_raw = platform.machine().lower()
        arch = "arm64" if arch_raw in ("arm64", "aarch64") else arch_raw
        src_app = DIST_DIR / "Movie Censor Studio.app"
        zip_path = DIST_DIR / f"MovieCensorStudio-macOS-{arch}.zip"
        if zip_path.exists():
            zip_path.unlink()
        subprocess.run(
            ["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(src_app), str(zip_path)],
            check=True,
        )
        return zip_path

    if sys.platform == "win32":
        zip_path = DIST_DIR / "MovieCensorStudio-Windows-x64.zip"
    else:
        zip_path = DIST_DIR / f"MovieCensorStudio-Linux-{platform.machine().lower()}.zip"

    src_dir = DIST_DIR / "MovieCensorStudio"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(src_dir.rglob("*")):
            if file_path.is_file():
                arcname = Path("MovieCensorStudio") / file_path.relative_to(src_dir)
                zf.write(file_path, arcname=str(arcname))
    return zip_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Build standalone Movie Censor Studio executable.")
    parser.add_argument("--download-ffmpeg", action="store_true", help="Download static ffmpeg and ffprobe for the current OS.")
    parser.add_argument("--only-download-ffmpeg", action="store_true", help="Download static ffmpeg and ffprobe and exit without building.")
    parser.add_argument("--ffmpeg-dir", help="Directory containing ffmpeg and ffprobe binaries to bundle.")
    parser.add_argument("--skip-ffmpeg", action="store_true", help="Build without bundling ffmpeg/ffprobe.")
    parser.add_argument("--pyinstaller", help="Path to pyinstaller executable (default: python -m PyInstaller).")
    parser.add_argument("--zip", action="store_true", help="Create a distributable .zip archive in dist/.")
    parser.add_argument("--skip-smoke-test", action="store_true", help="Skip launching and testing the built executable.")
    args = parser.parse_args()

    bundle_dir: Optional[Path] = None
    if not args.skip_ffmpeg:
        if args.ffmpeg_dir:
            bundle_dir = Path(args.ffmpeg_dir).expanduser().resolve()
        else:
            bundle_dir = _default_vendor_dir()

        if args.download_ffmpeg or args.only_download_ffmpeg:
            download_static_ffmpeg(bundle_dir)
            if args.only_download_ffmpeg:
                return 0
        else:
            suffix = _exe_suffix()
            if not (bundle_dir / f"ffmpeg{suffix}").is_file() or not (bundle_dir / f"ffprobe{suffix}").is_file():
                raise SystemExit(
                    f"Missing ffmpeg/ffprobe in {bundle_dir}. Pass --download-ffmpeg or --skip-ffmpeg."
                )

    env = os.environ.copy()
    env.setdefault("PYINSTALLER_CONFIG_DIR", str(BUILD_DIR / "pyinstaller_cache"))
    if bundle_dir is not None:
        env["CENSOR_BUNDLE_BIN_DIR"] = str(bundle_dir)
    else:
        env.pop("CENSOR_BUNDLE_BIN_DIR", None)

    if args.pyinstaller:
        cmd = [args.pyinstaller]
    else:
        cmd = [sys.executable, "-m", "PyInstaller"]
    cmd.extend(["--noconfirm", "--clean", "--distpath", str(DIST_DIR), "--workpath", str(BUILD_DIR), str(SPEC_PATH)])

    print(f"[build] Running: {' '.join(cmd)}")
    subprocess.run(cmd, cwd=str(REPO_ROOT), env=env, check=True)

    if not args.skip_smoke_test:
        run_smoke_test(expect_bundled=(bundle_dir is not None))

    if args.zip:
        archive_path = create_archive()
        print(f"[zip] Created {archive_path} ({archive_path.stat().st_size / (1024 * 1024):.1f} MB)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
