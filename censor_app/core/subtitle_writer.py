import os
from typing import List
from censor_app.core.srt_parser import SubtitleItem, format_timestamp
from censor_app.core.profanity_filter import ProfanityFilter

def generate_cleaned_srt(
    subtitles: List[SubtitleItem],
    profanity_filter: ProfanityFilter,
    output_path: str,
    mask_char: str = "*",
    preserve_first_letter: bool = True
) -> str:
    """
    Generate a sanitized SRT file where detected profanities are masked.
    Writes to output_path and returns the output_path.
    """
    lines: List[str] = []

    for item in subtitles:
        sanitized_text = profanity_filter.sanitize_text(
            item.clean_text,
            mask_char=mask_char,
            preserve_first_letter=preserve_first_letter
        )
        
        start_str = format_timestamp(item.start_time, ',')
        end_str = format_timestamp(item.end_time, ',')

        lines.append(str(item.index))
        lines.append(f"{start_str} --> {end_str}")
        lines.append(sanitized_text)
        lines.append("")  # Empty line separator

    content = "\n".join(lines)
    
    # Ensure directory exists
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)

    return output_path
