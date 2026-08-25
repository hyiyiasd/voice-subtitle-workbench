from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4


class PipelineMode(StrEnum):
    TRANSCRIBE_ONLY = "transcribe_only"
    TRANSCRIBE_AND_TRANSLATE = "transcribe_and_translate"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELED = "canceled"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class TaskKind(StrEnum):
    PREPARE_MEDIA = "prepare_media"
    TRANSCRIBE = "transcribe"
    TRANSLATE = "translate"


@dataclass(slots=True)
class SubtitleFormatSettings:
    min_duration_ms: int = 1_000
    max_duration_ms: int = 7_000
    max_lines: int = 2
    cjk_chars_per_line: int = 20
    latin_chars_per_line: int = 42
    max_cjk_chars_per_second: float = 12.0
    max_latin_words_per_minute: float = 180.0


@dataclass(slots=True)
class ProjectSettings:
    translation_enabled: bool = False
    source_language: str = "ja"
    target_language: str = "zh-Hans"
    asr_model: str = "reazonspeech-k2-ja"
    translation_provider: str = "openai-compatible"
    translation_model: str = ""
    offline: bool = False
    subtitle: SubtitleFormatSettings = field(default_factory=SubtitleFormatSettings)

    @property
    def pipeline_mode(self) -> PipelineMode:
        if self.translation_enabled:
            return PipelineMode.TRANSCRIBE_AND_TRANSLATE
        return PipelineMode.TRANSCRIBE_ONLY

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ProjectSettings:
        data = dict(value)
        data["subtitle"] = SubtitleFormatSettings(**data.get("subtitle", {}))
        return cls(**data)


@dataclass(slots=True)
class Segment:
    start_ms: int
    end_ms: int
    source_text: str
    language: str = "ja"
    id: str = field(default_factory=lambda: str(uuid4()))
    order_key: int = 0
    translated_text: str | None = None
    confidence: float | None = None
    quality_flags: set[str] = field(default_factory=set)
    human_locked: bool = False
    source_revision: int = 1
    translation_source_revision: int | None = None

    @property
    def has_valid_translation(self) -> bool:
        return bool(self.translated_text) and (
            self.translation_source_revision == self.source_revision
        )


@dataclass(frozen=True, slots=True)
class AudioChunk:
    id: str
    path: str
    start_ms: int
    end_ms: int
    language_hint: str | None = None


@dataclass(frozen=True, slots=True)
class ASRCandidate:
    start_ms: int
    end_ms: int
    text: str
    language: str
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class TranslationItem:
    segment_id: str
    source_text: str


@dataclass(frozen=True, slots=True)
class TranslationResult:
    segment_id: str
    translated_text: str


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    structured_output: bool = False
    style_instructions: bool = True
    streaming: bool = False
    local: bool = False
    usage_reporting: bool = False


@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    id: str
    display_name: str
    source: str
    version: str
    license: str
    sha256: str
    size_bytes: int
    languages: tuple[str, ...]
    recommended_vram_mb: int
    runtime: str


@dataclass(frozen=True, slots=True)
class VoiceRenderRequest:
    segment_id: str
    text: str
    start_ms: int
    end_ms: int
    voice_id: str

