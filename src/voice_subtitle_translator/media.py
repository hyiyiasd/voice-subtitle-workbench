from __future__ import annotations

import subprocess
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True, slots=True)
class SpeechRange:
    start_ms: int
    end_ms: int


def normalize_audio(ffmpeg: Path, source: Path, destination: Path) -> None:
    if not ffmpeg.is_file():
        _normalize_audio_with_pyav(source, destination)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(ffmpeg),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(destination),
    ]
    subprocess.run(command, check=True, creationflags=_no_window_flag())


def _normalize_audio_with_pyav(source: Path, destination: Path) -> None:
    """Decode common media through the PyAV runtime already bundled with faster-whisper."""
    from faster_whisper.audio import decode_audio

    samples = decode_audio(str(source), sampling_rate=16_000)
    pcm = (np.clip(samples, -1.0, 1.0) * 32_767).astype("<i2", copy=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(destination), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(pcm.tobytes())


def merge_speech_ranges(
    ranges: list[SpeechRange], *, max_duration_ms: int = 25_000, gap_ms: int = 350
) -> list[SpeechRange]:
    if not ranges:
        return []
    ordered = sorted(ranges, key=lambda value: (value.start_ms, value.end_ms))
    result: list[SpeechRange] = []
    current = ordered[0]
    for candidate in ordered[1:]:
        proposed_end = max(current.end_ms, candidate.end_ms)
        close_enough = candidate.start_ms - current.end_ms <= gap_ms
        within_limit = proposed_end - current.start_ms <= max_duration_ms
        if close_enough and within_limit:
            current = SpeechRange(current.start_ms, proposed_end)
        else:
            result.extend(_split_range(current, max_duration_ms))
            current = candidate
    result.extend(_split_range(current, max_duration_ms))
    return result


def _split_range(value: SpeechRange, maximum: int) -> list[SpeechRange]:
    result = []
    start = value.start_ms
    while value.end_ms - start > maximum:
        result.append(SpeechRange(start, start + maximum))
        start += maximum
    if value.end_ms > start:
        result.append(SpeechRange(start, value.end_ms))
    return result


def _no_window_flag() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def wav_duration_ms(path: Path) -> int:
    with wave.open(str(path), "rb") as source:
        return round(source.getnframes() / source.getframerate() * 1000)


def extract_wav_range(source_path: Path, destination: Path, value: SpeechRange) -> None:
    with wave.open(str(source_path), "rb") as source:
        rate = source.getframerate()
        start_frame = max(0, round(value.start_ms * rate / 1000))
        end_frame = min(source.getnframes(), round(value.end_ms * rate / 1000))
        source.setpos(start_frame)
        frames = source.readframes(max(0, end_frame - start_frame))
        destination.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(destination), "wb") as output:
            output.setparams(
                (
                    source.getnchannels(),
                    source.getsampwidth(),
                    rate,
                    0,
                    "NONE",
                    "not compressed",
                )
            )
            output.writeframes(frames)


def read_pcm16_mono(path: Path) -> tuple[list[float], int]:
    with wave.open(str(path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError("音频必须是 16-bit 单声道 WAV。")
        rate = source.getframerate()
        samples = array("h")
        samples.frombytes(source.readframes(source.getnframes()))
    return [sample / 32768.0 for sample in samples], rate
