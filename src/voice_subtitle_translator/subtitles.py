from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path

from .domain import Segment


class ExportContent(StrEnum):
    SOURCE = "source"
    TRANSLATION = "translation"
    BILINGUAL = "bilingual"


class ExportFormat(StrEnum):
    SRT = "srt"
    VTT = "vtt"
    ASS = "ass"
    TXT = "txt"
    JSON = "json"


class TranslationUnavailableError(ValueError):
    pass


def can_export(segments: list[Segment], content: ExportContent) -> tuple[bool, str]:
    if content is ExportContent.SOURCE:
        return True, ""
    if not segments:
        return False, "项目中没有字幕。"
    invalid = [segment for segment in segments if not segment.has_valid_translation]
    if invalid:
        return False, f"有 {len(invalid)} 条字幕没有有效译文。"
    return True, ""


def export_subtitles(
    segments: list[Segment],
    path: str | Path,
    *,
    output_format: ExportFormat,
    content: ExportContent = ExportContent.SOURCE,
) -> None:
    allowed, reason = can_export(segments, content)
    if not allowed:
        raise TranslationUnavailableError(reason)
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    renderers = {
        ExportFormat.SRT: render_srt,
        ExportFormat.VTT: render_vtt,
        ExportFormat.ASS: render_ass,
        ExportFormat.TXT: render_txt,
        ExportFormat.JSON: render_json,
    }
    output.write_text(renderers[output_format](segments, content), encoding="utf-8-sig")


def _text(segment: Segment, content: ExportContent, *, newline: str = "\n") -> str:
    if content is ExportContent.SOURCE:
        return segment.source_text
    if content is ExportContent.TRANSLATION:
        return segment.translated_text or ""
    return f"{segment.source_text}{newline}{segment.translated_text or ''}"


def _timestamp(milliseconds: int, separator: str = ",") -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{millis:03d}"


def render_srt(segments: list[Segment], content: ExportContent) -> str:
    blocks = []
    for index, segment in enumerate(segments, 1):
        blocks.append(
            f"{index}\n{_timestamp(segment.start_ms)} --> {_timestamp(segment.end_ms)}\n"
            f"{_text(segment, content)}"
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def render_vtt(segments: list[Segment], content: ExportContent) -> str:
    blocks = ["WEBVTT"]
    for segment in segments:
        blocks.append(
            f"{_timestamp(segment.start_ms, '.')} --> {_timestamp(segment.end_ms, '.')}\n"
            f"{_text(segment, content)}"
        )
    return "\n\n".join(blocks) + "\n"


def render_ass(segments: list[Segment], content: ExportContent) -> str:
    style_format = (
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
    )
    default_style = (
        "Style: Default,Microsoft YaHei UI,48,&H00FFFFFF,&H000000FF,&H00000000,"
        "&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,40,40,40,1"
    )
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\n\n"
        f"[V4+ Styles]\n{style_format}\n{default_style}\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = []
    for segment in segments:
        start = _timestamp(segment.start_ms, ".")[:-1]
        end = _timestamp(segment.end_ms, ".")[:-1]
        text = _text(segment, content, newline=r"\N").replace("\n", r"\N")
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")
    return header + "\n".join(lines) + ("\n" if lines else "")


def render_txt(segments: list[Segment], content: ExportContent) -> str:
    return "\n".join(_text(segment, content) for segment in segments) + ("\n" if segments else "")


def render_json(segments: list[Segment], content: ExportContent) -> str:
    values = []
    for index, segment in enumerate(segments, 1):
        item = {
            "index": index,
            "id": segment.id,
            "start_ms": segment.start_ms,
            "end_ms": segment.end_ms,
            "language": segment.language,
            "source": segment.source_text,
            "confidence": segment.confidence,
            "quality_flags": sorted(segment.quality_flags),
            "human_locked": segment.human_locked,
            "source_revision": segment.source_revision,
        }
        if content is not ExportContent.SOURCE:
            item["translation"] = segment.translated_text
        values.append(item)
    return json.dumps(values, ensure_ascii=False, indent=2) + "\n"
