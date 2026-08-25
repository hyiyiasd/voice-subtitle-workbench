import json
from pathlib import Path

import pytest

from voice_subtitle_translator.domain import Segment
from voice_subtitle_translator.subtitles import (
    ExportContent,
    ExportFormat,
    TranslationUnavailableError,
    export_subtitles,
    parse_srt,
    render_ass,
    render_srt,
    render_vtt,
)


def _segments() -> list[Segment]:
    return [
        Segment(
            id="stable-a",
            order_key=0,
            start_ms=1_234,
            end_ms=3_456,
            source_text="こんにちは",
            translated_text="你好",
            translation_source_revision=1,
        ),
        Segment(id="stable-b", order_key=1, start_ms=4_000, end_ms=5_500, source_text="世界"),
    ]


def test_srt_has_stable_sequence_and_timestamp() -> None:
    rendered = render_srt(_segments(), ExportContent.SOURCE)
    assert "1\n00:00:01,234 --> 00:00:03,456\nこんにちは" in rendered
    assert "2\n00:00:04,000 --> 00:00:05,500\n世界" in rendered


def test_exported_srt_can_be_loaded_as_translation_source(tmp_path: Path) -> None:
    path = tmp_path / "原文.srt"
    export_subtitles(
        _segments(), path, output_format=ExportFormat.SRT, content=ExportContent.SOURCE
    )
    loaded = parse_srt(path, language="ja")
    assert [(item.start_ms, item.end_ms, item.source_text) for item in loaded] == [
        (1_234, 3_456, "こんにちは"),
        (4_000, 5_500, "世界"),
    ]
    assert all(item.language == "ja" for item in loaded)


def test_vtt_and_ass_render() -> None:
    segments = _segments()
    assert render_vtt(segments, ExportContent.SOURCE).startswith("WEBVTT")
    assert "Dialogue: 0,00:00:01.23,00:00:03.45" in render_ass(
        segments, ExportContent.SOURCE
    )


def test_translation_export_is_disabled_when_any_translation_missing(tmp_path: Path) -> None:
    with pytest.raises(TranslationUnavailableError, match="1 条"):
        export_subtitles(
            _segments(),
            tmp_path / "translated.srt",
            output_format=ExportFormat.SRT,
            content=ExportContent.TRANSLATION,
        )


def test_json_source_export_does_not_add_translation(tmp_path: Path) -> None:
    path = tmp_path / "segments.json"
    export_subtitles(
        _segments(), path, output_format=ExportFormat.JSON, content=ExportContent.SOURCE
    )
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    assert "translation" not in payload[0]
