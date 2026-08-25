from __future__ import annotations

import re
from collections import Counter

from .domain import Segment, SubtitleFormatSettings

CJK_RE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)?")


def inspect_segments(
    segments: list[Segment], settings: SubtitleFormatSettings
) -> dict[str, set[str]]:
    flags: dict[str, set[str]] = {segment.id: set() for segment in segments}
    normalized = [re.sub(r"\s+", "", segment.source_text) for segment in segments]
    repeated = {text for text, count in Counter(normalized).items() if text and count >= 3}
    for index, segment in enumerate(segments):
        duration_ms = segment.end_ms - segment.start_ms
        if duration_ms < settings.min_duration_ms:
            flags[segment.id].add("duration_too_short")
        if duration_ms > settings.max_duration_ms:
            flags[segment.id].add("duration_too_long")
        if index and segment.start_ms < segments[index - 1].end_ms:
            flags[segment.id].add("timeline_overlap")
            flags[segments[index - 1].id].add("timeline_overlap")
        if not segment.source_text.strip():
            flags[segment.id].add("speech_without_text")
            continue
        if normalized[index] in repeated:
            flags[segment.id].add("possible_repetition")
        cjk_count = len(CJK_RE.findall(segment.source_text))
        latin_words = len(WORD_RE.findall(segment.source_text))
        seconds = max(duration_ms / 1000, 0.001)
        if cjk_count and cjk_count / seconds > settings.max_cjk_chars_per_second:
            flags[segment.id].add("reading_speed_high")
        if latin_words and latin_words / seconds * 60 > settings.max_latin_words_per_minute:
            flags[segment.id].add("reading_speed_high")
        chars_per_line = (
            settings.cjk_chars_per_line
            if cjk_count >= latin_words
            else settings.latin_chars_per_line
        )
        max_chars = chars_per_line * settings.max_lines
        if len(segment.source_text) > max_chars:
            flags[segment.id].add("subtitle_too_long")
    return flags


def apply_quality_flags(segments: list[Segment], settings: SubtitleFormatSettings) -> None:
    result = inspect_segments(segments, settings)
    for segment in segments:
        segment.quality_flags = result[segment.id]
