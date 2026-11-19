# Movie Censor Studio & Profanity Filter 🎬🔇

An intelligent, zero-dependency application and media server automation daemon for detecting and muting profanity in movies and videos based on subtitles.

Features an **interactive local Web Studio** (with real-time video streaming, detection timeline, and live audio preview), a **high-performance CLI**, and an **automated media server folder watcher daemon** (compatible with Plex, Jellyfin, Radarr, and Sonarr).

---

## ✨ Features

- **⚡ Zero External Pip Dependencies**: Built completely using the Python standard library and modern native browser technologies. Runs on any standard Python 3 installation without needing virtual environments or `pip install`.
- **🖥️ Interactive Web Studio (`python3 app.py`)**:
  - **HTTP 206 Byte-Range Video Streaming**: Scrub through multi-gigabyte local movie files smoothly in your browser with zero memory overhead.
  - **Detection Timeline**: Review all detected profanities in an interactive table with timestamp jump links that seek the video preview directly to that moment.
  - **🎧 Live Test Mute**: Audition each mute directly in your browser before exporting.
  - **Interactive Nudge Buttons**: Fine-tune word timing (`◀ Earlier` / `Later ▶`) with a single click.
  - **Drag & Drop & Local File Picker**: Upload files directly or pick from Mac bookmarks (**Home**, **Downloads**, **Movies**, **Workspace**).
  - **Live Encoding Progress**: Animated progress bar reporting percentage, elapsed time, and ETA.
- **🔊 Advanced Audio Censorship Engine**:
  - **Smooth Silence**: 40ms audio crossfades that eliminate digital pops and clicks.
  - **Bleep Tone (1000Hz)**: Mixes standard censorship sine beeps over muted intervals.
  - **Audio Ducking**: Lowers dialogue volume by -24dB so speech is unintelligible while preserving background music and score.
  - **5.1 Surround Center-Channel Filtering**: Selectively mutes *only* the Center dialogue channel (`FC`), leaving Left, Right, LFE subwoofer, and Surround channels untouched.
- **🎯 Precise Speech Timing**:
  - **Speech Onset Lead-In**: Anticipates speech onset earlier (100ms default in Smart Tight mode) to ensure initial consonants (*"f-"*, *"sh-"*, *"b-"*) are caught before sound reaches the speakers.
  - **Clause & Acoustic Waveform Alignment**: Breaks multi-clause lines at punctuation/conjunctions, tightens 4–6 word cues, and uses FFmpeg 8kHz audio envelope analysis to trim silence and snap mutes to inter-word pauses.
  - **Short-Segment Protection**: Short punchy lines (<= 2.5s or <= 5 words) are automatically protected with safety margins.
  - **Global Subtitle Sync Offset**: Compensate for out-of-sync subtitles with `+` / `-` second shifts.
- **🔍 Comprehensive Profanity Engine**:
  - **Pre-Censored Subtitle Support**: Detects masked and starred variants (`f***`, `f**k`, `sh*t`, `b****`, `a**hole`, `d*ck`, `c*nt`, `g*ddamn`, etc.).
  - **Phrases & Compounds**: Full support for multi-word expletives (`son of a bitch`, `piece of shit`, `holy shit`, `god dammit`) and hyphenated compounds (`dumb-ass`, `jack-ass`, `clusterfuck`).
  - **Scunthorpe Whitelist Protection**: Prevents false positives (e.g., *cockpit*, *assassin*, *class*, *pass*, *cocktail*).
- **🤖 Media Server Automation**:
  - **Folder Watcher Daemon**: Watches media folders for new downloads, verifies file stability (ignores `.part`, `.tmp`, `.crdownload`), and creates `Movie.Cleaned.mp4` and `Movie.Cleaned.srt` alongside the original without looping.
  - **Radarr / Sonarr / qBittorrent Post-Processing Hook**: Seamless integration as a post-download custom script.
- **📼 Embedded Subtitles**: Automatically extracts subtitle tracks from `.mkv` / `.mp4` containers if external `.srt` files are not present.
- **🌐 Trusted Online Subtitle Discovery**: When no local or embedded text subtitles exist, automatically searches and downloads matching subtitles from trusted providers (OpenSubtitles REST API v1, SubDL, Stremio OpenSubtitles v3 + IMDb, and Podnapisi) using 64-bit file hashing and title/episode parsing, or lets you search manually in Web Studio and CLI.
- **📜 Sanitized Subtitles**: Automatically generates matching `.Cleaned.srt` files with asterisks.

---

## 📋 Prerequisites

- **Python 3.8+** (tested on Python 3.8 through Python 3.14)
- **FFmpeg** installed and accessible in your `PATH`:
  - **macOS**: `brew install ffmpeg`
  - **Ubuntu / Debian**: `sudo apt install ffmpeg`
  - **Arch Linux**: `sudo pacman -S ffmpeg`

No `pip install` required!

---

## 🚀 Quick Start

### 1. Launch the Interactive Web Studio
```bash
python3 app.py
```
Opens `http://127.0.0.1:8000` in your default browser.

1. Drag and drop your movie file (or click **📁 Browse...** to pick from **Downloads** / **Movies**).
2. (Optional) Provide an `.srt` file, or let the app extract embedded subtitles automatically.
3. Click **⚡ Scan for Profanity**.
4. Review detected profanities in the timeline, preview audio clips with **🎧 Test Mute**, or nudge timing if needed.
5. Choose your censorship mode (**Smooth Silence**, **Bleep Tone**, or **Audio Ducking**) and click **🚀 Censor & Export Media**.

---

## 💻 Command Line Interface (CLI)

### Scan Media (Dry-Run)
Scan a video and print a table of detected profanities without modifying any files:
```bash
python3 -m censor_app.cli scan movie.mp4
# Or with an external subtitle file and severity preset:
python3 -m censor_app.cli scan movie.mp4 -s movie.srt --preset moderate
```

### Process Video
Censors audio and generates `movie.Cleaned.mp4` and `movie.Cleaned.srt` alongside the original:
```bash
# Smooth silence (default)
python3 -m censor_app.cli process movie.mp4

# Bleep tone (1000Hz)
python3 -m censor_app.cli process movie.mp4 --mode bleep

# Audio ducking (-24dB speech volume reduction)
python3 -m censor_app.cli process movie.mp4 --mode duck

# Compensate for subtitles that lag or lead audio by 0.3 seconds:
python3 -m censor_app.cli process movie.mp4 --sync-offset -0.3
```

### Backwards-Compatible Script
```bash
python3 mute_audio_from_srt.py -i movie.mp4 -s movie.srt -o movie_clean.mp4
```

### Search & Download Subtitles
```bash
# Automatically match by file hash and filename and save <movie>.<lang>.srt:
python3 -m censor_app.cli subtitles movie.mp4 --lang en

# Preview ranked matches without downloading:
python3 -m censor_app.cli subtitles --title "Inception" --year 2010 --search-only
```

#### Subtitle Provider API Keys (`.env` / Environment Variables)
You can store optional API credentials in a local `.env` file (gitignored so secrets are never committed) or in shell environment variables:
```bash
cp .env.example .env
# Edit .env and set:
#   OPENSUBTITLES_API_KEY=your_key_here
# Optional:
#   OPENSUBTITLES_USERNAME=your_username
#   OPENSUBTITLES_PASSWORD=your_password
#   SUBDL_API_KEY=your_subdl_key
```
Credential precedence: **shell environment variables (`os.environ`) > `.env` > `~/.config/censor_app/config.json`** (saving settings in Web Studio will not write `.env` / environment secrets into `config.json`).

---

## 🤖 Media Server Automation

### Run as a Folder Watcher Daemon
Continuously monitors a media directory (e.g. your movie download library):
```bash
# Output alongside original files:
python3 -m censor_app.cli watch /media/movies --mode mute

# Output to a separate folder (leaves original torrents completely untouched for seeding):
python3 -m censor_app.cli watch /mnt/torrents/completed -o /mnt/media/clean --mode mute
```
- Waits for downloads or transfers to complete (file stability check).
- Detects paired `.srt` files or extracts embedded subtitles from `.mkv`/`.mp4`.
- Generates `Movie.Cleaned.mp4` and `Movie.Cleaned.srt` alongside or in your designated `--output-dir`.
- Automatically skips already cleaned files to prevent loops.

### Radarr / Sonarr / qBittorrent Post-Processing Hook
In **Radarr** or **Sonarr**:
1. Navigate to **Settings** $\rightarrow$ **Connect** $\rightarrow$ **+** $\rightarrow$ **Custom Script**.
2. Set the path to:
   ```bash
   /usr/bin/python3 /path/to/profanity-filter/censor_app/server/postprocess.py
   ```
3. Check **On Download** and **On Upgrade**.

The script automatically detects `radarr_moviefile_path` or `sonarr_episodefile_path` environment variables and creates the cleaned version alongside the original upon download completion.

---

## 📦 Standalone Executables (Windows & macOS)

You can package Movie Censor Studio into a standalone desktop bundle (with static `ffmpeg` and `ffprobe` included—no Python or FFmpeg install needed by end users):

### Build Locally (macOS or Windows)
```bash
python3 -m venv .venv-build
.venv-build/bin/pip install pyinstaller   # Windows: .venv-build\Scripts\pip install pyinstaller
python3 packaging/build.py --download-ffmpeg --pyinstaller .venv-build/bin/pyinstaller --zip
```
Outputs are created in `dist/` (`Movie Censor Studio.app` / `MovieCensorStudio-macOS-arm64.zip` on Apple Silicon, or `MovieCensorStudio\MovieCensorStudio.exe` / `MovieCensorStudio-Windows-x64.zip` on Windows).

- **Optional `.env` placement:** Place a `.env` file beside `Movie Censor Studio.app` (or beside `MovieCensorStudio.exe` on Windows), or at `~/.config/censor_app/.env`, to auto-load subtitle API keys.
- **Unsigned macOS builds:** If Gatekeeper prompts when opening a downloaded unsigned `.app`, right-click **Open** once or run `xattr -dr com.apple.quarantine "Movie Censor Studio.app"`.

### GitHub Actions Builds
The [Build Executables](.github/workflows/build-executables.yml) workflow builds Windows and macOS zip bundles only when manually triggered (**Actions → Build Executables → Run workflow**, with an option to build `both`, `windows`, or `macos`) or when pushing a version tag (`git tag v1.0.0 && git push origin v1.0.0`, which attaches both archives to a GitHub Release).

---

## 🧪 Running Tests

The test suite runs with Python's built-in `unittest` runner:
```bash
python3 -m unittest discover -s censor_app/tests
```

All 68 unit and integration tests verify:
- Configuration loading, `.env` parsing, secret masking, and credential precedence
- Standalone packaging binary resolution, frozen `.env`/static asset discovery, and shutdown/health endpoints
- Subtitle parsing (SRT, WebVTT, encodings, HTML/SSA tags)
- Trusted online subtitle discovery, ranking, archive unpacking, and SSRF/XXE guards
- Profanity filtering & masked word detection
- Word-level timing, acoustic waveform alignment, lead-in, and sync offsets
- FFmpeg filter graphs (Smooth Mute, 1000Hz Bleep, Ducking, 5.1 Center-Channel split/join)
- Directory watcher daemon, auto-download cooldowns, and alongside file creation
- Web Studio HTTP Range streaming (HTTP 206), REST API, and subtitle search/download endpoints

---

## 📁 Repository Structure

```
profanity-filter/
├── censor_app/
│   ├── core/
│   │   ├── config.py           # Configuration management
│   │   ├── srt_parser.py       # Subtitle parser & embedded stream extractor
│   │   ├── profanity_filter.py # Dictionaries, masked patterns, and whitelist
│   │   ├── audio_aligner.py    # FFmpeg 8kHz waveform speech & pause alignment
│   │   ├── subtitle_fetcher.py # Trusted online subtitle search, ranking & download
│   │   ├── timing.py           # Speech onset lead-in, word timing & interval merging
│   │   ├── ffmpeg_engine.py    # FFmpeg filter builder & progress parser
│   │   └── subtitle_writer.py  # Sanitized subtitle generator (.Cleaned.srt)
│   ├── server/
│   │   ├── watcher.py          # Media server folder watcher daemon
│   │   └── postprocess.py      # Radarr / Sonarr / qBittorrent hook
│   ├── web/
│   │   ├── server.py           # Range-request streaming server & REST API
│   │   └── static/             # Single-page UI (HTML5, CSS3, ES6 JS)
│   │       ├── index.html
│   │       ├── style.css
│   │       └── app.js
│   ├── tests/                  # Automated test suite (68 unit & integration tests)
│   │   ├── test_config.py
│   │   ├── test_packaging.py
│   │   └── test_subtitle_fetcher.py
│   ├── cli.py                  # Unified CLI
│   └── main.py                 # Entrypoint
├── packaging/
│   ├── build.py                # Standalone bundle builder & static FFmpeg fetcher
│   └── movie_censor_studio.spec
├── .env.example                # Template for local subtitle API credentials
├── app.py                      # Quick Web Studio launcher
├── mute_audio_from_srt.py      # Standalone & backward-compatible wrapper
├── .gitignore
└── README.md
```

---

## 📄 License

MIT License. See LICENSE for details.
