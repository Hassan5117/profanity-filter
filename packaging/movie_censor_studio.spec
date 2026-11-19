# -*- mode: python ; coding: utf-8 -*-
import os
import sys

project_root = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = [
    (
        os.path.join(project_root, "censor_app", "web", "static"),
        os.path.join("censor_app", "web", "static"),
    ),
]

binaries = []
bin_dir = os.environ.get("CENSOR_BUNDLE_BIN_DIR", "").strip()
if bin_dir and os.path.isdir(bin_dir):
    exe_suffix = ".exe" if sys.platform == "win32" else ""
    for tool in ("ffmpeg", "ffprobe"):
        tool_path = os.path.join(bin_dir, f"{tool}{exe_suffix}")
        if os.path.isfile(tool_path):
            binaries.append((tool_path, "bin"))

hiddenimports = [
    "censor_app",
    "censor_app.cli",
    "censor_app.core.audio_aligner",
    "censor_app.core.config",
    "censor_app.core.ffmpeg_engine",
    "censor_app.core.profanity_filter",
    "censor_app.core.srt_parser",
    "censor_app.core.subtitle_fetcher",
    "censor_app.core.subtitle_writer",
    "censor_app.core.timing",
    "censor_app.server.postprocess",
    "censor_app.server.watcher",
    "censor_app.web.server",
]

a = Analysis(
    [os.path.join(project_root, "app.py")],
    pathex=[project_root],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "test"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

if sys.platform == "darwin":
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="MovieCensorStudio",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="MovieCensorStudio",
    )
    app = BUNDLE(
        coll,
        name="Movie Censor Studio.app",
        icon=None,
        bundle_identifier="com.moviecensorstudio.app",
        info_plist={
            "CFBundleDisplayName": "Movie Censor Studio",
            "CFBundleName": "Movie Censor Studio",
            "CFBundleShortVersionString": "1.0.0",
            "CFBundleVersion": "1.0.0",
            "NSHighResolutionCapable": True,
        },
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="MovieCensorStudio",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=True,
        disable_windowed_traceback=False,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="MovieCensorStudio",
    )
