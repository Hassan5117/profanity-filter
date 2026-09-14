import re
import os
import subprocess
import html
import tempfile
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict, Any

@dataclass
class SubtitleItem:
    index: int
    start_time: float  # seconds
    end_time: float    # seconds
    raw_text: str      # original text
    clean_text: str    # text with styling tags and HTML entities stripped

    @property
    def duration(self) -> float:
        return max(0.0, self.end_time - self.start_time)


def parse_timestamp(time_str: str) -> float:
    """
    Parse timestamp string in formats like:
    HH:MM:SS,MMM or HH:MM:SS.MMM or H:MM:SS,MMM or MM:SS,MMM
    """
    time_str = time_str.strip().replace(',', '.')
    parts = time_str.split(':')
    
    if len(parts) == 3:
        hours = float(parts[0])
        minutes = float(parts[1])
        seconds = float(parts[2])
    elif len(parts) == 2:
        hours = 0.0
        minutes = float(parts[0])
        seconds = float(parts[1])
    elif len(parts) == 1:
        hours = 0.0
        minutes = 0.0
        seconds = float(parts[0])
    else:
        raise ValueError(f"Invalid timestamp format: {time_str}")

    return hours * 3600.0 + minutes * 60.0 + seconds


def format_timestamp(seconds: float, delimiter: str = ',') -> str:
    """Format seconds into HH:MM:SS,MMM format."""
    total_secs = max(0.0, seconds)
    hours = int(total_secs // 3600)
    remainder = total_secs % 3600
    minutes = int(remainder // 60)
    sec_float = remainder % 60
    secs = int(sec_float)
    millis = int(round((sec_float - secs) * 1000))
    if millis >= 1000:
        millis -= 1000
        secs += 1
    if secs >= 60:
        secs -= 60
        minutes += 1
    if minutes >= 60:
        minutes -= 60
        hours += 1
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{delimiter}{millis:03d}"


def strip_subtitle_tags(text: str) -> str:
    """
    Remove HTML tags (<i>, <b>, <font>, etc.) and SSA/ASS style tags ({\\an8}, etc.)
    and unescape HTML entities.
    """
    # Replace ASS/SSA curly bracket style tags e.g. {\an8}, {\c&HFFFFFF&} with a space
    text = re.sub(r'\{[^}]*\}', ' ', text)
    # Replace HTML tags e.g. <font color="...">, </i>, <br /> with a space
    text = re.sub(r'<[^>]+>', ' ', text)
    # Unescape HTML entities e.g. &amp;, &quot;, &#39;
    text = html.unescape(text)
    # Clean up whitespace before punctuation e.g. "world !" -> "world!"
    text = re.sub(r'\s+([.,!?;:])', r'\1', text)
    # Normalize whitespace
    text = re.sub(r'[ \t]+', ' ', text)
    return text.strip()


def read_subtitle_file_content(file_path: str) -> str:
    """Read subtitle file trying multiple encodings: utf-8-sig, utf-8, latin-1, cp1252."""
    encodings = ['utf-8-sig', 'utf-8', 'latin-1', 'cp1252', 'iso-8859-1']
    for enc in encodings:
        try:
            with open(file_path, 'r', encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    # Fallback with replacement
    with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
        return f.read()


def parse_srt_content(content: str) -> List[SubtitleItem]:
    """Parse raw SRT/VTT text content into SubtitleItem objects."""
    # Normalize line breaks
    content = content.replace('\r\n', '\n').replace('\r', '\n')
    
    # Strip WEBVTT header if present
    if content.startswith("WEBVTT"):
        content = re.sub(r'^WEBVTT[^\n]*\n', '', content)

    # Split on double (or more) newlines
    blocks = re.split(r'\n{2,}', content.strip())
    items: List[SubtitleItem] = []
    
    time_pattern = re.compile(
        r'((?:\d{1,2}:)?\d{2}:\d{2}[,\.]\d{3})\s*-->\s*((?:\d{1,2}:)?\d{2}:\d{2}[,\.]\d{3})'
    )

    item_index = 1
    for block in blocks:
        lines = [line.strip() for line in block.split('\n') if line.strip()]
        if not lines:
            continue

        time_line_idx = -1
        time_match = None

        for idx, line in enumerate(lines):
            match = time_pattern.search(line)
            if match:
                time_line_idx = idx
                time_match = match
                break

        if time_match is None or time_line_idx == -1:
            continue

        try:
            start_sec = parse_timestamp(time_match.group(1))
            end_sec = parse_timestamp(time_match.group(2))
        except ValueError:
            continue

        # Subtitle text consists of lines following the timestamp line
        text_lines = lines[time_line_idx + 1:]
        raw_text = "\n".join(text_lines)
        clean_text = strip_subtitle_tags(" ".join(text_lines))

        if not clean_text:
            continue

        items.append(SubtitleItem(
            index=item_index,
            start_time=start_sec,
            end_time=end_sec,
            raw_text=raw_text,
            clean_text=clean_text
        ))
        item_index += 1

    return items


def parse_srt(file_path: str) -> List[SubtitleItem]:
    """Parse an SRT or VTT file by path."""
    content = read_subtitle_file_content(file_path)
    return parse_srt_content(content)


def probe_subtitle_streams(video_path: str) -> List[Dict[str, Any]]:
    """
    Use ffprobe to detect embedded subtitle tracks in a video file.
    Returns list of dicts with stream index, codec, language, title.
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "s",
        "-show_entries", "stream=index,codec_name:stream_tags=language,title",
        "-of", "json",
        video_path
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        import json
        data = json.loads(res.stdout)
        streams = []
        for s in data.get("streams", []):
            tags = s.get("tags", {})
            streams.append({
                "stream_index": s.get("index"),
                "codec": s.get("codec_name", "unknown"),
                "language": tags.get("language", "und"),
                "title": tags.get("title", f"Track {s.get('index')}")
            })
        return streams
    except Exception:
        return []


def extract_embedded_subtitle(video_path: str, stream_index: Optional[int] = None, output_path: Optional[str] = None) -> str:
    """
    Extract embedded subtitle stream into an SRT file.
    If stream_index is None, extracts the first available subtitle stream.
    """
    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".srt", prefix="embedded_sub_")
        os.close(fd)

    stream_selector = f"0:s:{stream_index}" if stream_index is not None else "0:s:0"
    cmd = [
        "ffmpeg",
        "-y",
        "-i", video_path,
        "-map", stream_selector,
        "-c:s", "srt",
        output_path
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass
        raise RuntimeError(f"Failed to extract embedded subtitles: {res.stderr}")

    return output_path
