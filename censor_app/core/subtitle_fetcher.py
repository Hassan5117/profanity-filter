"""
Trusted online subtitle discovery, ranking, and safe archive download/validation.

Supports:
- OpenSubtitles REST API v1 (optional API key / user login + 64-bit moviehash)
- SubDL REST API v1 (optional API key)
- Stremio OpenSubtitles v3 public addon + IMDb suggestion lookup (zero-config)
- Podnapisi public XML search & download (zero-config)

Pure Python standard library implementation with strict HTTPS trusted-domain
validation, bounded payload/archive extraction, and atomic disk writes.
"""

from __future__ import annotations

import gzip
import io
import json
import math
import os
import re
import struct
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from censor_app.core.config import Config
from censor_app.core.srt_parser import format_timestamp, parse_srt_content


TRUSTED_SUBTITLE_DOMAINS: Tuple[str, ...] = (
    "opensubtitles.com",
    "opensubtitles.org",
    "subdl.com",
    "podnapisi.net",
    "strem.io",
    "media-imdb.com",
)

MAX_DOWNLOAD_BYTES: int = 10 * 1024 * 1024  # 10 MB upper bound for HTTP/archive downloads
MAX_SUBTITLE_BYTES: int = 5 * 1024 * 1024   # 5 MB upper bound for a single extracted subtitle
DEFAULT_USER_AGENT: str = "ProfanityFilterStudio v1.0"

_LANGUAGE_ALIASES: Dict[str, Tuple[str, str]] = {
    "en": ("en", "eng"),
    "eng": ("en", "eng"),
    "english": ("en", "eng"),
    "es": ("es", "spa"),
    "spa": ("es", "spa"),
    "spanish": ("es", "spa"),
    "fr": ("fr", "fre"),
    "fre": ("fr", "fre"),
    "fra": ("fr", "fre"),
    "french": ("fr", "fre"),
    "de": ("de", "ger"),
    "ger": ("de", "ger"),
    "deu": ("de", "ger"),
    "german": ("de", "ger"),
    "it": ("it", "ita"),
    "ita": ("it", "ita"),
    "italian": ("it", "ita"),
    "pt": ("pt", "por"),
    "por": ("pt", "por"),
    "portuguese": ("pt", "por"),
    "nl": ("nl", "dut"),
    "dut": ("nl", "dut"),
    "nld": ("nl", "dut"),
    "dutch": ("nl", "dut"),
    "ru": ("ru", "rus"),
    "rus": ("ru", "rus"),
    "russian": ("ru", "rus"),
    "ja": ("ja", "jpn"),
    "jpn": ("ja", "jpn"),
    "japanese": ("ja", "jpn"),
    "ko": ("ko", "kor"),
    "kor": ("ko", "kor"),
    "korean": ("ko", "kor"),
    "zh": ("zh", "chi"),
    "chi": ("zh", "chi"),
    "zho": ("zh", "chi"),
    "chinese": ("zh", "chi"),
    "ar": ("ar", "ara"),
    "ara": ("ar", "ara"),
    "arabic": ("ar", "ara"),
    "hi": ("hi", "hin"),
    "hin": ("hi", "hin"),
    "hindi": ("hi", "hin"),
    "tr": ("tr", "tur"),
    "tur": ("tr", "tur"),
    "turkish": ("tr", "tur"),
    "pl": ("pl", "pol"),
    "pol": ("pl", "pol"),
    "polish": ("pl", "pol"),
    "sv": ("sv", "swe"),
    "swe": ("sv", "swe"),
    "swedish": ("sv", "swe"),
}

_RELEASE_NOISE_TOKENS = {
    "1080p", "720p", "480p", "2160p", "4k", "uhd",
    "bluray", "blu", "ray", "bdrip", "brrip", "remux",
    "web", "dl", "webdl", "webrip", "hdtv", "dvdrip", "hdrip", "cam", "ts",
    "x264", "x265", "h264", "h265", "hevc", "avc", "xvid", "divx",
    "aac", "ac3", "dts", "dd5", "ddp5", "atmos", "truehd", "flac", "mp3",
    "10bit", "8bit", "hdr", "hdr10", "dv", "dolby",
    "proper", "repack", "extended", "unrated", "directors", "cut",
    "yify", "yts", "rarbg", "sparks", "geckos", "fgt", "evo", "ntb", "nogrp",
}

_EPISODE_RE = re.compile(
    r"(?:^|[\s._\-\[\(])(?:S(\d{1,2})[\s._-]*E(\d{1,3})|(\d{1,2})x(\d{2,3})|Season[\s._-]*(\d{1,2})[\s._-]*Episode[\s._-]*(\d{1,3}))(?=$|[\s._\-\]\)])",
    re.IGNORECASE,
)
_IMDB_RE = re.compile(r"\b(tt\d{7,9})\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"(?:^|[\s._\(\[-])((?:19|20)\d{2})(?=$|[\s._\)\]-])")
_SRT_TIMECODE_RE = re.compile(
    r"\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,.]\d{3}"
)
_VTT_TIMECODE_RE = re.compile(
    r"^((?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3})\s*-->\s*((?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3})(?:\s+.*)?$"
)

HttpClient = Callable[
    [str, str, Optional[Dict[str, str]], Optional[bytes], float, int],
    Tuple[int, Dict[str, str], bytes],
]


def normalize_language_codes(language: Optional[str]) -> Tuple[str, str]:
    """Normalize a language string into (iso639_1, iso639_2_b) lowercase codes."""
    raw = (language or "en").strip().lower()
    if raw in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[raw]
    if len(raw) == 2 and raw.isalpha():
        return (raw, raw)
    if len(raw) == 3 and raw.isalpha():
        return (raw[:2], raw)
    return ("en", "eng")


def is_trusted_https_url(url: Optional[str]) -> bool:
    """Return True strictly for HTTPS URLs targeting allowlisted subtitle domains."""
    if not url or not isinstance(url, str):
        return False
    stripped = url.strip()
    try:
        parsed = urllib.parse.urlsplit(stripped)
        port = parsed.port
    except ValueError:
        return False

    if parsed.scheme.lower() != "https":
        return False
    if parsed.username or parsed.password:
        return False
    if port not in (None, 443):
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return False

    return any(host == base or host.endswith(f".{base}") for base in TRUSTED_SUBTITLE_DOMAINS)


class _SafeTrustedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that rejects untrusted redirects and strips auth across hosts."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        resolved = urllib.parse.urljoin(req.full_url, newurl)
        if not is_trusted_https_url(resolved):
            raise urllib.error.HTTPError(
                resolved, code, f"Blocked redirect to untrusted domain: {resolved}", headers, fp
            )
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is None:
            return None

        old_host = (urllib.parse.urlsplit(req.full_url).hostname or "").lower()
        new_host = (urllib.parse.urlsplit(new_req.full_url).hostname or "").lower()
        if old_host != new_host:
            for hdr_name in list(new_req.headers.keys()):
                if hdr_name.lower() in {"authorization", "api-key", "x-api-key"}:
                    new_req.remove_header(hdr_name)
        return new_req


def _default_http_client(
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    timeout: float = 10.0,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
) -> Tuple[int, Dict[str, str], bytes]:
    if not is_trusted_https_url(url):
        raise ValueError(f"Blocked untrusted subtitle URL: {url}")

    req_headers = {"User-Agent": DEFAULT_USER_AGENT}
    if headers:
        req_headers.update(headers)

    req = urllib.request.Request(url, data=body, headers=req_headers, method=method.upper())
    opener = urllib.request.build_opener(_SafeTrustedRedirectHandler)

    try:
        with opener.open(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode() or 200
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            data = resp.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError(f"Downloaded payload from {url} exceeded max size ({max_bytes} bytes)")
            return status, resp_headers, data
    except urllib.error.HTTPError as e:
        err_headers = {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}
        err_body = b""
        try:
            err_body = e.read(min(max_bytes, 65536))
        except Exception:
            pass
        return e.code, err_headers, err_body


@dataclass
class MediaQuery:
    title: str
    year: Optional[int] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    imdb_id: Optional[str] = None
    language: str = "en"
    movie_hash: Optional[str] = None
    movie_bytesize: Optional[int] = None
    release_name: Optional[str] = None
    media_type: str = "movie"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SubtitleCandidate:
    provider: str
    subtitle_id: str
    title: str
    language: str
    release_name: str
    year: Optional[int] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    downloads: int = 0
    hearing_impaired: bool = False
    foreign_parts_only: bool = False
    hash_matched: bool = False
    rating: float = 0.0
    score: float = 0.0
    download_url: Optional[str] = None
    download_id: Optional[str] = None
    file_name: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SubtitleDownloadResult:
    srt_path: str
    provider: str
    candidate: SubtitleCandidate
    cue_count: int
    converted_from_vtt: bool = False

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["candidate"] = self.candidate.to_dict()
        return data


def compute_opensubtitles_hash(file_path: str) -> Optional[Tuple[str, int]]:
    """
    Compute the standard 64-bit OpenSubtitles moviehash and file byte size.

    Returns ``(hex_hash_16_chars, filesize)`` or ``None`` if the file cannot be read
    or is smaller than 128 KB (131072 bytes).
    """
    chunk_size = 65536
    try:
        filesize = os.path.getsize(file_path)
        if filesize < chunk_size * 2:
            return None

        total = filesize
        fmt = "<" + "Q" * (chunk_size // 8)
        with open(file_path, "rb") as f:
            head = f.read(chunk_size)
            f.seek(max(0, filesize - chunk_size))
            tail = f.read(chunk_size)

        if len(head) < chunk_size or len(tail) < chunk_size:
            return None

        for val in struct.unpack(fmt, head):
            total = (total + val) & 0xFFFFFFFFFFFFFFFF
        for val in struct.unpack(fmt, tail):
            total = (total + val) & 0xFFFFFFFFFFFFFFFF
        return f"{total:016x}", filesize
    except OSError:
        return None


def _clean_title_tokens(raw_name: str) -> Tuple[str, Optional[int], Optional[int], Optional[int], Optional[str]]:
    """Extract (title, year, season, episode, imdb_id) from a video filename stem or title string."""
    imdb_match = _IMDB_RE.search(raw_name)
    imdb_id = imdb_match.group(1).lower() if imdb_match else None

    cleaned = raw_name
    if imdb_match:
        cleaned = cleaned[:imdb_match.start()] + " " + cleaned[imdb_match.end():]

    season: Optional[int] = None
    episode: Optional[int] = None
    ep_match = _EPISODE_RE.search(cleaned)
    if ep_match:
        s_str = ep_match.group(1) or ep_match.group(3) or ep_match.group(5)
        e_str = ep_match.group(2) or ep_match.group(4) or ep_match.group(6)
        season = int(s_str)
        episode = int(e_str)
        cleaned = cleaned[:ep_match.start()]

    # Remove bracketed release tags before scanning for year/title words
    cleaned_no_brackets = re.sub(r"[\[\{][^\]\}]*[\]\}]", " ", cleaned)
    raw_tokens = [t for t in re.split(r"[\s._\-()]+", cleaned_no_brackets) if t]

    # Drop everything from the first obvious technical release tag onward
    cutoff = len(raw_tokens)
    for idx, tok in enumerate(raw_tokens):
        if tok.lower() in _RELEASE_NOISE_TOKENS and idx > 0:
            cutoff = idx
            break
    candidate_tokens = raw_tokens[:cutoff]

    # Pick the rightmost 4-digit year only if at least one title token precedes it
    # so movies titled "1917" keep "1917" as title while "1917.2019..." parses year=2019.
    year: Optional[int] = None
    year_idx: Optional[int] = None
    for idx in range(len(candidate_tokens) - 1, 0, -1):
        tok = candidate_tokens[idx]
        if re.fullmatch(r"(?:19|20)\d{2}", tok):
            y_val = int(tok)
            if 1910 <= y_val <= 2035:
                year = y_val
                year_idx = idx
                break

    if year_idx is not None:
        title_tokens = candidate_tokens[:year_idx]
    else:
        title_tokens = candidate_tokens

    # Also strip trailing release noise if idx == 0 check kept a tag or fallback to raw_tokens
    title_str = " ".join(title_tokens).strip()
    if not title_str:
        # Try extracting year anywhere when string has e.g. "(2019)" only after title
        ym = _YEAR_RE.search(cleaned)
        if ym and ym.start() > 0:
            year = year or int(ym.group(1))
        title_str = " ".join(raw_tokens[:1]).strip()

    return title_str, year, season, episode, imdb_id


def parse_media_query(
    video_path: Optional[str] = None,
    title: Optional[str] = None,
    year: Optional[int] = None,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    imdb_id: Optional[str] = None,
    language: str = "en",
    compute_hash: bool = True,
) -> MediaQuery:
    """Build a structured MediaQuery from a video path, overrides, and optional Radarr/Sonarr env vars."""
    iso1, _ = normalize_language_codes(language)
    release_name = Path(video_path).stem if video_path else None

    parsed_title, parsed_year, parsed_season, parsed_episode, parsed_imdb = (
        _clean_title_tokens(release_name or (title or ""))
    )

    # Read Radarr/Sonarr webhook metadata only when matching the target video path
    if video_path:
        try:
            abs_video = os.path.abspath(video_path)
            radarr_path = os.environ.get("radarr_moviefile_path", "")
            sonarr_path = os.environ.get("sonarr_episodefile_path", "")
            if radarr_path and os.path.abspath(radarr_path) == abs_video:
                parsed_title = os.environ.get("radarr_movie_title") or parsed_title
                if not year and os.environ.get("radarr_movie_year", "").isdigit():
                    parsed_year = int(os.environ["radarr_movie_year"])
                parsed_imdb = os.environ.get("radarr_movie_imdbid") or parsed_imdb
            elif sonarr_path and os.path.abspath(sonarr_path) == abs_video:
                parsed_title = os.environ.get("sonarr_series_title") or parsed_title
                parsed_imdb = os.environ.get("sonarr_series_imdbid") or parsed_imdb
                s_env = os.environ.get("sonarr_episodefile_seasonnumber", "")
                e_env = os.environ.get("sonarr_episodefile_episodenumbers", "").split(",")[0].strip()
                if s_env.isdigit() and season is None:
                    parsed_season = int(s_env)
                if e_env.isdigit() and episode is None:
                    parsed_episode = int(e_env)
        except OSError:
            pass

    final_title = (title.strip() if title and title.strip() else parsed_title).strip()
    if title and title.strip() and year is None:
        # Also allow clean title strings like "The Matrix (1999)" when passed manually
        t_clean, t_year, t_season, t_ep, t_imdb = _clean_title_tokens(title)
        if t_clean:
            final_title = t_clean
        parsed_year = t_year or parsed_year
        if season is None:
            parsed_season = t_season or parsed_season
        if episode is None:
            parsed_episode = t_ep or parsed_episode
        parsed_imdb = parsed_imdb or t_imdb

    final_year = year if year is not None else parsed_year
    final_season = season if season is not None else parsed_season
    final_episode = episode if episode is not None else parsed_episode
    final_imdb = (imdb_id or parsed_imdb or None)
    if final_imdb:
        final_imdb = final_imdb.strip().lower()
        if final_imdb.isdigit():
            final_imdb = f"tt{final_imdb}"

    movie_hash: Optional[str] = None
    movie_bytesize: Optional[int] = None
    if compute_hash and video_path and os.path.isfile(video_path):
        hash_res = compute_opensubtitles_hash(video_path)
        if hash_res:
            movie_hash, movie_bytesize = hash_res

    media_type = "episode" if (final_season is not None or final_episode is not None) else "movie"

    return MediaQuery(
        title=final_title,
        year=final_year,
        season=final_season,
        episode=final_episode,
        imdb_id=final_imdb,
        language=iso1,
        movie_hash=movie_hash,
        movie_bytesize=movie_bytesize,
        release_name=release_name,
        media_type=media_type,
    )


def _normalize_vtt_timestamp(ts: str) -> str:
    """Normalize a VTT timestamp (MM:SS.mmm or HH:MM:SS.mmm) into SRT HH:MM:SS,mmm."""
    ts_clean = ts.strip().replace(".", ",")
    parts = ts_clean.split(":")
    if len(parts) == 2:
        return f"00:{parts[0].zfill(2)}:{parts[1]}"
    if len(parts) == 3:
        return f"{parts[0].zfill(2)}:{parts[1].zfill(2)}:{parts[2]}"
    return ts_clean


def convert_vtt_to_srt(vtt_text: str) -> str:
    """Convert WebVTT text into valid numbered SRT subtitle text."""
    text = vtt_text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", text.strip())
    srt_blocks: List[str] = []
    idx = 1

    for block in blocks:
        lines = [line.rstrip() for line in block.split("\n") if line.strip() != ""]
        if not lines:
            continue
        header = lines[0].strip()
        if header.startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue

        time_line_idx = None
        for i, line in enumerate(lines):
            if "-->" in line and _VTT_TIMECODE_RE.match(line.strip()):
                time_line_idx = i
                break
        if time_line_idx is None:
            continue

        m = _VTT_TIMECODE_RE.match(lines[time_line_idx].strip())
        if not m:
            continue
        start_srt = _normalize_vtt_timestamp(m.group(1))
        end_srt = _normalize_vtt_timestamp(m.group(2))
        cue_lines = lines[time_line_idx + 1:]
        if not cue_lines:
            continue

        srt_blocks.append(f"{idx}\n{start_srt} --> {end_srt}\n" + "\n".join(cue_lines))
        idx += 1

    if not srt_blocks:
        return ""
    return "\n\n".join(srt_blocks) + "\n"


def _decode_subtitle_bytes(raw_bytes: bytes) -> str:
    """Decode subtitle bytes trying common subtitle encodings cleanly."""
    if not raw_bytes:
        raise ValueError("Empty subtitle payload")
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def _score_archive_member(member_name: str, query: Optional[MediaQuery]) -> float:
    """Score an .srt/.vtt entry inside an archive to pick the best subtitle file."""
    lower = member_name.lower()
    score = 10.0 if lower.endswith(".srt") else 5.0
    if "sample" in lower or "trailer" in lower:
        score -= 50.0
    if "forced" in lower:
        score -= 20.0

    if query:
        if query.season is not None and query.episode is not None:
            ep_tag = f"s{query.season:02d}e{query.episode:02d}"
            ep_alt = f"{query.season}x{query.episode:02d}"
            if ep_tag in lower or ep_alt in lower:
                score += 80.0
        if query.release_name:
            rel_tokens = {
                t for t in re.split(r"[^a-z0-9]+", query.release_name.lower()) if len(t) >= 3
            }
            member_tokens = {
                t for t in re.split(r"[^a-z0-9]+", lower) if len(t) >= 3
            }
            score += min(40.0, len(rel_tokens & member_tokens) * 8.0)
    return score


def extract_and_validate_subtitle_bytes(
    raw_bytes: bytes,
    query: Optional[MediaQuery] = None,
    file_hint: Optional[str] = None,
) -> Tuple[str, int, bool]:
    """
    Safely unpack (if zip/gzip), normalize (VTT->SRT if needed), and validate subtitle payload.

    Returns ``(normalized_srt_text, cue_count, converted_from_vtt)``.
    Raises ``ValueError`` if the payload is unsafe, oversized, HTML/JSON error, or lacks valid cues.
    """
    if not raw_bytes:
        raise ValueError("Downloaded subtitle payload is empty")
    if len(raw_bytes) > MAX_DOWNLOAD_BYTES:
        raise ValueError("Downloaded subtitle payload exceeds maximum allowed size")

    candidate_blobs: List[Tuple[str, bytes]] = []

    if raw_bytes[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                entries = []
                total_uncompressed = 0
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    fname = info.filename
                    norm_parts = Path(fname).parts
                    if (
                        not fname
                        or fname.startswith(("/", "\\"))
                        or ".." in norm_parts
                        or fname.startswith("__MACOSX")
                    ):
                        continue
                    lower = fname.lower()
                    if not (lower.endswith(".srt") or lower.endswith(".vtt")):
                        continue
                    if info.file_size > MAX_SUBTITLE_BYTES:
                        continue
                    total_uncompressed += info.file_size
                    if total_uncompressed > MAX_DOWNLOAD_BYTES * 2:
                        raise ValueError("Zip archive uncompressed size exceeds safety limit")
                    entries.append(info)

                if not entries:
                    raise ValueError("Zip archive contains no valid .srt or .vtt subtitle files")

                entries.sort(key=lambda inf: _score_archive_member(inf.filename, query), reverse=True)
                for info in entries[:5]:
                    with zf.open(info, "r") as member_fp:
                        data = member_fp.read(MAX_SUBTITLE_BYTES + 1)
                    if len(data) <= MAX_SUBTITLE_BYTES:
                        candidate_blobs.append((info.filename, data))
        except zipfile.BadZipFile as e:
            raise ValueError(f"Corrupted zip subtitle archive: {e}") from e
    elif raw_bytes[:2] == b"\x1f\x8b":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw_bytes)) as gz:
                decompressed = gz.read(MAX_SUBTITLE_BYTES + 1)
        except OSError as e:
            raise ValueError(f"Corrupted gzip subtitle stream: {e}") from e
        if len(decompressed) > MAX_SUBTITLE_BYTES:
            raise ValueError("Decompressed gzip subtitle exceeds maximum allowed size")
        candidate_blobs.append((file_hint or "subtitle.srt", decompressed))
    else:
        if len(raw_bytes) > MAX_SUBTITLE_BYTES:
            raise ValueError("Subtitle file exceeds maximum allowed size")
        candidate_blobs.append((file_hint or "subtitle.srt", raw_bytes))

    last_err = "No parseable subtitle cues found"
    for name, blob in candidate_blobs:
        text = _decode_subtitle_bytes(blob)
        stripped_l = text.lstrip("\ufeff \t\r\n").lower()
        if not stripped_l:
            last_err = "Subtitle text is empty"
            continue
        if stripped_l.startswith(("<!doctype", "<html", "<head", "<error", "{", "[")):
            last_err = "Downloaded payload appears to be HTML/JSON/XML rather than a subtitle file"
            continue

        converted_vtt = False
        is_vtt = name.lower().endswith(".vtt") or stripped_l.startswith("webvtt")
        if is_vtt:
            srt_text = convert_vtt_to_srt(text)
            converted_vtt = True
        else:
            srt_text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")

        if not _SRT_TIMECODE_RE.search(srt_text):
            # Also try VTT converter as fallback when timestamps omit hours cleanly
            if "-->" in text:
                alt = convert_vtt_to_srt(text)
                if alt and _SRT_TIMECODE_RE.search(alt):
                    srt_text = alt
                    converted_vtt = True
                else:
                    last_err = "Payload lacks valid subtitle timestamps"
                    continue
            else:
                last_err = "Payload lacks valid subtitle timestamps"
                continue

        cues = parse_srt_content(srt_text)
        if not cues:
            last_err = "Subtitle file contains zero parseable dialogue cues"
            continue

        if not srt_text.endswith("\n"):
            srt_text += "\n"
        return srt_text, len(cues), converted_vtt

    raise ValueError(last_err)


def build_subtitle_output_path(video_path: str, language: Optional[str] = None) -> Path:
    """Return canonical external subtitle path ``<video_dir>/<stem>.<iso1>.srt``."""
    p = Path(video_path)
    iso1, _ = normalize_language_codes(language)
    return p.parent / f"{p.stem}.{iso1}.srt"


def _atomic_write_text(dest_path: Path, content: str) -> None:
    """Write text atomically alongside destination before replacing target path."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.with_name(f".{dest_path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, dest_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def score_candidate(candidate: SubtitleCandidate, query: MediaQuery) -> float:
    """Compute a deterministic relevance score favoring hash matches and exact release/episode matches."""
    score = 0.0
    q_iso1, q_iso2 = normalize_language_codes(query.language)
    c_iso1, c_iso2 = normalize_language_codes(candidate.language)

    if c_iso1 == q_iso1 or c_iso2 == q_iso2:
        score += 100.0
    else:
        score -= 100.0

    if candidate.hash_matched:
        score += 200.0

    rel_lower = f"{candidate.release_name} {candidate.file_name or ''} {candidate.title}".lower()

    if query.season is not None and query.episode is not None:
        ep_tag = f"s{query.season:02d}e{query.episode:02d}"
        ep_alt = f"{query.season}x{query.episode:02d}"
        if candidate.season == query.season and candidate.episode == query.episode:
            score += 80.0
        elif ep_tag in rel_lower or ep_alt in rel_lower:
            score += 75.0
        elif candidate.season is not None and candidate.episode is not None:
            score -= 150.0

    if query.year is not None and candidate.year is not None:
        if candidate.year == query.year:
            score += 40.0
        elif abs(candidate.year - query.year) > 1:
            score -= 60.0

    if query.release_name:
        q_tokens = {
            t for t in re.split(r"[^a-z0-9]+", query.release_name.lower()) if len(t) >= 3
        }
        r_tokens = {
            t for t in re.split(r"[^a-z0-9]+", rel_lower) if len(t) >= 3
        }
        overlap = len(q_tokens & r_tokens)
        score += min(60.0, overlap * 12.0)

    if query.title:
        title_tokens = {
            t for t in re.split(r"[^a-z0-9]+", query.title.lower()) if len(t) >= 2
        }
        c_title_tokens = {
            t for t in re.split(r"[^a-z0-9]+", candidate.title.lower()) if len(t) >= 2
        }
        if title_tokens and c_title_tokens:
            jaccard = len(title_tokens & c_title_tokens) / max(len(title_tokens), 1)
            score += jaccard * 40.0

    if candidate.foreign_parts_only or "forced" in rel_lower:
        score -= 80.0

    if candidate.downloads > 0:
        score += min(25.0, math.log10(candidate.downloads + 1) * 7.5)

    provider_weights = {
        "opensubtitles": 10.0,
        "subdl": 8.0,
        "stremio_opensubtitles": 6.0,
        "podnapisi": 4.0,
    }
    score += provider_weights.get(candidate.provider, 0.0)
    candidate.score = round(score, 2)
    return candidate.score


class SubtitleFetcher:
    """Multi-provider subtitle searcher and safe downloader."""

    def __init__(
        self,
        config: Optional[Config] = None,
        http_client: Optional[HttpClient] = None,
        logger: Optional[Callable[[str], None]] = None,
        timeout: float = 10.0,
    ):
        self.config = config or Config()
        self.http_client: HttpClient = http_client or _default_http_client
        self.logger = logger or (lambda _msg: None)
        self.timeout = timeout
        self._os_token: Optional[str] = None

    def _log(self, message: str) -> None:
        try:
            self.logger(message)
        except Exception:
            pass

    def _request(
        self,
        url: str,
        method: str = "GET",
        headers: Optional[Dict[str, str]] = None,
        body: Optional[bytes] = None,
        max_bytes: int = MAX_DOWNLOAD_BYTES,
    ) -> Tuple[int, Dict[str, str], bytes]:
        if not is_trusted_https_url(url):
            raise ValueError(f"Blocked untrusted URL: {url}")
        return self.http_client(url, method, headers, body, self.timeout, max_bytes)

    def _resolve_imdb_id(self, query: MediaQuery) -> Optional[str]:
        """Resolve an IMDb ID using the public IMDb suggestion endpoint when missing."""
        if query.imdb_id:
            return query.imdb_id
        if not query.title:
            return None

        clean_slug = re.sub(r"[^a-z0-9]+", "_", query.title.lower()).strip("_")
        if not clean_slug:
            return None
        first_char = clean_slug[0] if clean_slug[0].isalnum() else "x"
        url = (
            f"https://v3.sg.media-imdb.com/suggestion/{first_char}/"
            f"{urllib.parse.quote(clean_slug)}.json"
        )
        try:
            status, _hdrs, data = self._request(url, method="GET", headers={"Accept": "application/json"})
            if status != 200 or not data:
                return None
            parsed = json.loads(data.decode("utf-8"))
        except Exception:
            return None

        items = parsed.get("d") or []
        q_tokens = {t for t in re.split(r"[^a-z0-9]+", query.title.lower()) if t}
        best_id: Optional[str] = None
        best_score = -1.0

        for item in items:
            iid = str(item.get("id") or "")
            if not _IMDB_RE.fullmatch(iid):
                continue
            label = str(item.get("l") or "")
            l_tokens = {t for t in re.split(r"[^a-z0-9]+", label.lower()) if t}
            overlap = len(q_tokens & l_tokens) / max(len(q_tokens), 1) if q_tokens else 0.0
            if overlap <= 0.0 and query.title.lower() not in label.lower():
                continue

            item_score = overlap * 50.0
            if label.lower().strip() == query.title.lower().strip():
                item_score += 40.0

            qid = str(item.get("qid") or "").lower()
            if query.media_type == "episode" and qid in ("tvseries", "tvminiseries"):
                item_score += 20.0
            elif query.media_type == "movie" and qid in ("movie", "feature", "tvmovie"):
                item_score += 20.0

            item_year = item.get("y")
            if query.year and isinstance(item_year, int):
                if item_year == query.year:
                    item_score += 30.0
                elif abs(item_year - query.year) > 1:
                    item_score -= 20.0

            if item_score > best_score:
                best_score = item_score
                best_id = iid.lower()

        return best_id

    def _search_opensubtitles(self, query: MediaQuery) -> List[SubtitleCandidate]:
        api_key = (self.config.get("opensubtitles_api_key") or "").strip()
        if not api_key:
            return []

        iso1, _ = normalize_language_codes(query.language)
        params: Dict[str, Any] = {"languages": iso1}
        if query.movie_hash:
            params["moviehash"] = query.movie_hash
        if query.imdb_id and query.imdb_id.startswith("tt") and query.imdb_id[2:].isdigit():
            params["imdb_id"] = str(int(query.imdb_id[2:]))
        elif query.title:
            params["query"] = query.title
        if query.year is not None:
            params["year"] = str(query.year)
        if query.season is not None:
            params["season_number"] = str(query.season)
        if query.episode is not None:
            params["episode_number"] = str(query.episode)

        url = f"https://api.opensubtitles.com/api/v1/subtitles?{urllib.parse.urlencode(params)}"
        headers = {
            "Api-Key": api_key,
            "Accept": "application/json",
            "User-Agent": DEFAULT_USER_AGENT,
        }
        try:
            status, _hdrs, data = self._request(url, method="GET", headers=headers)
            if status != 200:
                return []
            payload = json.loads(data.decode("utf-8"))
        except Exception as e:
            self._log(f"OpenSubtitles search error: {e}")
            return []

        results: List[SubtitleCandidate] = []
        for entry in payload.get("data") or []:
            attrs = entry.get("attributes") or {}
            files = attrs.get("files") or []
            if not files:
                continue
            file_info = files[0] or {}
            file_id = file_info.get("file_id")
            if not file_id:
                continue
            feature = attrs.get("feature_details") or {}
            cand = SubtitleCandidate(
                provider="opensubtitles",
                subtitle_id=str(entry.get("id") or file_id),
                title=str(feature.get("movie_name") or feature.get("title") or query.title or "Unknown"),
                language=str(attrs.get("language") or iso1),
                release_name=str(attrs.get("release") or file_info.get("file_name") or ""),
                year=feature.get("year") if isinstance(feature.get("year"), int) else query.year,
                season=feature.get("season_number"),
                episode=feature.get("episode_number"),
                downloads=int(attrs.get("download_count") or 0),
                hearing_impaired=bool(attrs.get("hearing_impaired", False)),
                foreign_parts_only=bool(attrs.get("foreign_parts_only", False)),
                hash_matched=bool(attrs.get("moviehash_match", False)),
                rating=float(attrs.get("ratings") or 0.0),
                download_url=None,
                download_id=str(file_id),
                file_name=file_info.get("file_name"),
            )
            results.append(cand)
        return results

    def _search_subdl(self, query: MediaQuery) -> List[SubtitleCandidate]:
        api_key = (self.config.get("subdl_api_key") or "").strip()
        if not api_key:
            return []

        iso1, _ = normalize_language_codes(query.language)
        params: Dict[str, Any] = {
            "api_key": api_key,
            "languages": iso1.upper(),
            "subs_per_page": 20,
        }
        if query.imdb_id:
            params["imdb_id"] = query.imdb_id
        elif query.title:
            params["film_name"] = query.title
        if query.year is not None:
            params["year"] = str(query.year)
        if query.media_type == "episode":
            params["type"] = "tv"
            if query.season is not None:
                params["season_number"] = str(query.season)
            if query.episode is not None:
                params["episode_number"] = str(query.episode)
        else:
            params["type"] = "movie"

        url = f"https://api.subdl.com/api/v1/subtitles?{urllib.parse.urlencode(params)}"
        try:
            status, _hdrs, data = self._request(url, method="GET", headers={"Accept": "application/json"})
            if status != 200:
                return []
            payload = json.loads(data.decode("utf-8"))
        except Exception as e:
            self._log(f"SubDL search error: {e}")
            return []

        if not payload.get("status"):
            return []

        results: List[SubtitleCandidate] = []
        subtitles = payload.get("subtitles") or []
        for idx, sub in enumerate(subtitles):
            raw_dl = str(sub.get("url") or "")
            if not raw_dl:
                continue
            dl_url = f"https://dl.subdl.com{raw_dl}" if raw_dl.startswith("/") else raw_dl
            if not is_trusted_https_url(dl_url):
                continue
            release_name = str(sub.get("release_name") or sub.get("name") or "")
            cand = SubtitleCandidate(
                provider="subdl",
                subtitle_id=str(sub.get("sd_id") or f"subdl-{idx}"),
                title=str(sub.get("name") or query.title or "Unknown"),
                language=str(sub.get("lang") or sub.get("language") or iso1),
                release_name=release_name,
                year=query.year,
                season=sub.get("season") if isinstance(sub.get("season"), int) else query.season,
                episode=sub.get("episode") if isinstance(sub.get("episode"), int) else query.episode,
                downloads=int(sub.get("downloads") or 0),
                hearing_impaired=bool(sub.get("hi", False)),
                foreign_parts_only=False,
                hash_matched=False,
                download_url=dl_url,
                file_name=sub.get("release_name"),
            )
            results.append(cand)
        return results

    def _search_stremio_opensubtitles(self, query: MediaQuery) -> List[SubtitleCandidate]:
        imdb_id = query.imdb_id or self._resolve_imdb_id(query)
        if not imdb_id:
            return []

        iso1, iso2 = normalize_language_codes(query.language)
        if query.media_type == "episode" and query.season is not None and query.episode is not None:
            kind = "series"
            media_key = f"{imdb_id}:{query.season}:{query.episode}"
        else:
            kind = "movie"
            media_key = imdb_id

        urls: List[str] = []
        if query.movie_hash and query.movie_bytesize:
            urls.append(
                f"https://opensubtitles-v3.strem.io/subtitles/{kind}/{media_key}/"
                f"videoHash={query.movie_hash}&videoSize={query.movie_bytesize}.json"
            )
        urls.append(f"https://opensubtitles-v3.strem.io/subtitles/{kind}/{media_key}.json")

        seen_urls = set()
        results: List[SubtitleCandidate] = []
        for idx_url, url in enumerate(urls):
            is_hash_query = idx_url == 0 and bool(query.movie_hash and query.movie_bytesize)
            try:
                status, _hdrs, data = self._request(url, method="GET", headers={"Accept": "application/json"})
                if status != 200 or not data:
                    continue
                payload = json.loads(data.decode("utf-8"))
            except Exception as e:
                self._log(f"Stremio OpenSubtitles error: {e}")
                continue

            for sub in payload.get("subtitles") or []:
                dl_url = str(sub.get("url") or "")
                if not dl_url or dl_url in seen_urls or not is_trusted_https_url(dl_url):
                    continue
                sub_lang = str(sub.get("lang") or "").lower()
                s_iso1, s_iso2 = normalize_language_codes(sub_lang)
                if sub_lang and s_iso1 != iso1 and s_iso2 != iso2:
                    continue

                seen_urls.add(dl_url)
                sub_id = str(sub.get("id") or dl_url)
                results.append(
                    SubtitleCandidate(
                        provider="stremio_opensubtitles",
                        subtitle_id=sub_id,
                        title=query.title or imdb_id,
                        language=sub_lang or iso2,
                        release_name=str(sub.get("release") or sub_id),
                        year=query.year,
                        season=query.season,
                        episode=query.episode,
                        hash_matched=is_hash_query,
                        download_url=dl_url,
                    )
                )
            if results and is_hash_query:
                break
        return results

    def _search_podnapisi(self, query: MediaQuery) -> List[SubtitleCandidate]:
        if not query.title and not query.movie_hash:
            return []

        iso1, _ = normalize_language_codes(query.language)
        params: Dict[str, Any] = {"sXML": "1", "sL": iso1}
        if query.title:
            params["sK"] = query.title
        if query.year is not None:
            params["sY"] = str(query.year)
        if query.season is not None:
            params["sTS"] = str(query.season)
        if query.episode is not None:
            params["sTE"] = str(query.episode)
        if query.movie_hash:
            params["sMH"] = query.movie_hash

        url = f"https://www.podnapisi.net/subtitles/search/old?{urllib.parse.urlencode(params)}"
        try:
            status, _hdrs, data = self._request(url, method="GET")
            if status != 200 or not data:
                return []
            lower_head = data[:2048].lower()
            if b"<!doctype" in lower_head or b"<!entity" in lower_head:
                return []
            root = ET.fromstring(data.decode("utf-8", errors="replace"))
        except Exception as e:
            self._log(f"Podnapisi search error: {e}")
            return []

        results: List[SubtitleCandidate] = []
        for node in root.findall(".//subtitle"):
            pid = (node.findtext("pid") or "").strip()
            if not pid or not re.fullmatch(r"[A-Za-z0-9_-]+", pid):
                continue

            sub_title = (node.findtext("title") or query.title or "").strip()
            release_name = (node.findtext("release") or "").strip()
            if not release_name:
                rel_elems = [el.text.strip() for el in node.findall("./releases/release") if el.text]
                release_name = rel_elems[0] if rel_elems else sub_title

            year_txt = (node.findtext("year") or "").strip()
            dl_txt = (node.findtext("downloads") or "0").strip()
            flags = (node.findtext("flags") or "").lower()
            season_txt = (node.findtext("tvSeason") or "").strip()
            ep_txt = (node.findtext("tvEpisode") or "").strip()
            rating_txt = (node.findtext("rating") or "0").strip()

            dl_url = f"https://www.podnapisi.net/subtitles/{urllib.parse.quote(pid, safe='')}/download"
            try:
                downloads_val = int(dl_txt) if dl_txt.isdigit() else 0
                rating_val = float(rating_txt) if rating_txt else 0.0
            except ValueError:
                downloads_val = 0
                rating_val = 0.0

            results.append(
                SubtitleCandidate(
                    provider="podnapisi",
                    subtitle_id=pid,
                    title=sub_title,
                    language=iso1,
                    release_name=release_name,
                    year=int(year_txt) if year_txt.isdigit() else None,
                    season=int(season_txt) if season_txt.isdigit() else query.season,
                    episode=int(ep_txt) if ep_txt.isdigit() else query.episode,
                    downloads=downloads_val,
                    hearing_impaired=("n" in flags or "h" in flags),
                    foreign_parts_only=False,
                    hash_matched=False,
                    rating=rating_val,
                    download_url=dl_url,
                )
            )
        return results

    def search(
        self,
        query: MediaQuery,
        providers: Optional[Iterable[str]] = None,
        limit: int = 20,
    ) -> List[SubtitleCandidate]:
        """Query enabled trusted subtitle providers and return ranked candidates."""
        if providers is not None:
            provider_list = list(providers)
        else:
            provider_list = [
                "opensubtitles",
                "subdl",
                "stremio_opensubtitles",
                "podnapisi",
            ]

        all_candidates: List[SubtitleCandidate] = []
        search_fns = {
            "opensubtitles": self._search_opensubtitles,
            "subdl": self._search_subdl,
            "stremio_opensubtitles": self._search_stremio_opensubtitles,
            "podnapisi": self._search_podnapisi,
        }

        for pname in provider_list:
            fn = search_fns.get(pname)
            if fn is None:
                continue
            found = fn(query)
            all_candidates.extend(found)

        # Deduplicate while keeping highest-scoring candidate per (provider, download_target)
        dedup: Dict[Tuple[str, str], SubtitleCandidate] = {}
        for cand in all_candidates:
            score_candidate(cand, query)
            key = (cand.provider, cand.download_url or cand.download_id or cand.subtitle_id)
            if key not in dedup or cand.score > dedup[key].score:
                dedup[key] = cand

        ranked = sorted(
            dedup.values(),
            key=lambda c: (c.score, c.hash_matched, c.downloads),
            reverse=True,
        )
        return ranked[:limit]

    def _get_opensubtitles_token(self, api_key: str) -> Optional[str]:
        if self._os_token:
            return self._os_token
        username = (self.config.get("opensubtitles_username") or "").strip()
        password = (self.config.get("opensubtitles_password") or "").strip()
        if not username or not password:
            return None

        url = "https://api.opensubtitles.com/api/v1/login"
        headers = {
            "Api-Key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": DEFAULT_USER_AGENT,
        }
        body = json.dumps({"username": username, "password": password}).encode("utf-8")
        try:
            status, _hdrs, data = self._request(url, method="POST", headers=headers, body=body)
            if status == 200:
                parsed = json.loads(data.decode("utf-8"))
                token = parsed.get("token")
                if token:
                    self._os_token = str(token)
                    return self._os_token
        except Exception as e:
            self._log(f"OpenSubtitles login failed: {e}")
        return None

    def _resolve_opensubtitles_download_url(self, candidate: SubtitleCandidate) -> str:
        api_key = (self.config.get("opensubtitles_api_key") or "").strip()
        if not api_key:
            raise ValueError("OpenSubtitles API key is required to download via OpenSubtitles REST API")
        if not candidate.download_id or not str(candidate.download_id).isdigit():
            raise ValueError("OpenSubtitles candidate is missing numeric file_id")

        headers: Dict[str, str] = {
            "Api-Key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": DEFAULT_USER_AGENT,
        }
        token = self._get_opensubtitles_token(api_key)
        if token:
            headers["Authorization"] = f"Bearer {token}"

        url = "https://api.opensubtitles.com/api/v1/download"
        body = json.dumps({"file_id": int(candidate.download_id), "sub_format": "srt"}).encode("utf-8")
        status, _hdrs, data = self._request(url, method="POST", headers=headers, body=body)
        if status != 200:
            raise ValueError(f"OpenSubtitles download API returned HTTP {status}")

        parsed = json.loads(data.decode("utf-8"))
        link = str(parsed.get("link") or "")
        if not is_trusted_https_url(link):
            raise ValueError(f"OpenSubtitles returned untrusted download link: {link}")
        return link

    def download_candidate(
        self,
        candidate: SubtitleCandidate,
        dest_srt_path: str,
        query: Optional[MediaQuery] = None,
    ) -> SubtitleDownloadResult:
        """Download, validate, and atomically write a candidate subtitle to ``dest_srt_path``."""
        dl_url = candidate.download_url
        req_headers: Dict[str, str] = {}

        if candidate.provider == "opensubtitles" and not dl_url:
            dl_url = self._resolve_opensubtitles_download_url(candidate)
        elif candidate.provider == "subdl":
            subdl_key = (self.config.get("subdl_api_key") or "").strip()
            if subdl_key:
                req_headers["x-api-key"] = subdl_key

        if not dl_url or not is_trusted_https_url(dl_url):
            raise ValueError(f"Refused untrusted or missing subtitle download URL: {dl_url}")

        status, _hdrs, raw_bytes = self._request(dl_url, method="GET", headers=req_headers)
        if status != 200:
            raise ValueError(f"Subtitle download from {candidate.provider} returned HTTP {status}")

        srt_text, cue_count, converted_vtt = extract_and_validate_subtitle_bytes(
            raw_bytes,
            query=query,
            file_hint=candidate.file_name or candidate.release_name,
        )

        dest = Path(dest_srt_path)
        _atomic_write_text(dest, srt_text)
        return SubtitleDownloadResult(
            srt_path=str(dest),
            provider=candidate.provider,
            candidate=candidate,
            cue_count=cue_count,
            converted_from_vtt=converted_vtt,
        )

    def auto_fetch_subtitle_for_video(
        self,
        video_path: str,
        language: Optional[str] = None,
        dest_srt_path: Optional[str] = None,
        max_attempts: int = 5,
    ) -> Optional[SubtitleDownloadResult]:
        """Search trusted providers for ``video_path`` and download the highest-ranked valid subtitle."""
        lang = language or self.config.get("subtitle_language", "en") or "en"
        query = parse_media_query(video_path=video_path, language=lang, compute_hash=True)
        candidates = self.search(query, limit=max(max_attempts, 10))
        if not candidates:
            return None

        target_path = dest_srt_path or str(build_subtitle_output_path(video_path, language=query.language))
        for candidate in candidates[:max_attempts]:
            try:
                return self.download_candidate(candidate, dest_srt_path=target_path, query=query)
            except Exception as e:
                self._log(
                    f"Subtitle candidate {candidate.provider}/{candidate.subtitle_id} failed ({e}), trying next..."
                )
        return None
