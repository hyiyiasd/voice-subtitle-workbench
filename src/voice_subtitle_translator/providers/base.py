from __future__ import annotations

from typing import Protocol

from ..domain import (
    ASRCandidate,
    AudioChunk,
    ProviderCapabilities,
    TranslationItem,
    TranslationResult,
    VoiceRenderRequest,
)


class ASRProvider(Protocol):
    id: str

    def transcribe(self, chunk: AudioChunk, *, model: str) -> list[ASRCandidate]: ...

    def close(self) -> None: ...


class TranslationProvider(Protocol):
    id: str
    capabilities: ProviderCapabilities

    def translate(
        self,
        items: list[TranslationItem],
        *,
        target_language: str,
        model: str,
        context: str = "",
        glossary: list[tuple[str, str]] | None = None,
        style_instruction: str = "",
    ) -> list[TranslationResult]: ...

    def close(self) -> None: ...


class VoiceProvider(Protocol):
    id: str

    def render(self, request: VoiceRenderRequest) -> str: ...


class StemSeparationProvider(Protocol):
    id: str

    def separate(self, media_path: str, output_directory: str) -> dict[str, str]: ...

