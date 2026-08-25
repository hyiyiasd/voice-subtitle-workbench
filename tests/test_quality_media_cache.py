import wave
from pathlib import Path

from voice_subtitle_translator.cache import translation_cache_key
from voice_subtitle_translator.domain import Segment, SubtitleFormatSettings
from voice_subtitle_translator.media import (
    SpeechRange,
    merge_speech_ranges,
    normalize_audio,
    wav_duration_ms,
)
from voice_subtitle_translator.quality import inspect_segments


def test_vad_ranges_merge_and_split_at_25_seconds() -> None:
    result = merge_speech_ranges(
        [SpeechRange(0, 10_000), SpeechRange(10_200, 28_000)], max_duration_ms=25_000
    )
    assert result == [SpeechRange(0, 10_000), SpeechRange(10_200, 28_000)]
    assert merge_speech_ranges([SpeechRange(0, 60_000)]) == [
        SpeechRange(0, 25_000),
        SpeechRange(25_000, 50_000),
        SpeechRange(50_000, 60_000),
    ]


def test_quality_detects_overlap_repetition_and_speed() -> None:
    segments = [
        Segment(start_ms=0, end_ms=1_000, source_text="很长很长很长很长很长很长很长很长"),
        Segment(start_ms=900, end_ms=1_200, source_text="重复"),
        Segment(start_ms=1_300, end_ms=1_600, source_text="重复"),
        Segment(start_ms=1_700, end_ms=2_000, source_text="重复"),
    ]
    flags = inspect_segments(segments, SubtitleFormatSettings())
    assert "timeline_overlap" in flags[segments[0].id]
    assert "reading_speed_high" in flags[segments[0].id]
    assert "possible_repetition" in flags[segments[1].id]


def test_cache_key_changes_with_glossary_and_revision() -> None:
    base = dict(
        source_text="test",
        source_revision=1,
        model="m",
        target_language="zh-Hans",
        prompt="p",
        glossary=[],
        provider_parameters={"temperature": 0},
    )
    first = translation_cache_key(**base)
    assert first == translation_cache_key(**base)
    assert first != translation_cache_key(**{**base, "source_revision": 2})
    assert first != translation_cache_key(**{**base, "glossary": [("test", "测试")]})


def test_media_normalization_falls_back_to_bundled_pyav(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    destination = tmp_path / "normalized.wav"
    with wave.open(str(source), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(b"\0\0\0\0" * 4_000)
    normalize_audio(tmp_path / "missing-ffmpeg.exe", source, destination)
    with wave.open(str(destination), "rb") as normalized:
        assert normalized.getnchannels() == 1
        assert normalized.getsampwidth() == 2
        assert normalized.getframerate() == 16_000
    assert 490 <= wav_duration_ms(destination) <= 510
