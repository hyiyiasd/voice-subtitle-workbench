from __future__ import annotations

import httpx

from ..domain import ProviderCapabilities, TranslationItem, TranslationResult
from .common import build_translation_messages, parse_translation_response
from .openai_compatible import OfflineModeError

TRANSLATION_SCHEMA = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
                "required": ["id", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["translations"],
    "additionalProperties": False,
}


class OpenAIResponsesProvider:
    id = "openai"
    capabilities = ProviderCapabilities(
        structured_output=True,
        style_instructions=True,
        streaming=False,
        local=False,
        usage_reporting=True,
    )

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.openai.com/v1",
        offline: bool = False,
        client: httpx.Client | None = None,
    ) -> None:
        self.offline = offline
        self._client = client or httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=120,
            trust_env=False,
        )

    def translate(
        self,
        items: list[TranslationItem],
        *,
        target_language: str,
        model: str,
        context: str = "",
        glossary: list[tuple[str, str]] | None = None,
        style_instruction: str = "",
    ) -> list[TranslationResult]:
        if self.offline:
            raise OfflineModeError("离线模式禁止访问 OpenAI。")
        if not items:
            return []
        messages = build_translation_messages(
            items,
            target_language=target_language,
            context=context,
            glossary=glossary or [],
            style_instruction=style_instruction,
        )
        response = self._client.post(
            "/responses",
            json={
                "model": model,
                "input": messages,
                "temperature": 0,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "subtitle_translations",
                        "strict": True,
                        "schema": TRANSLATION_SCHEMA,
                    }
                },
            },
        )
        response.raise_for_status()
        payload = response.json()
        raw = payload.get("output_text") or _extract_output_text(payload)
        return parse_translation_response(raw, [item.segment_id for item in items])

    def close(self) -> None:
        self._client.close()


def _extract_output_text(payload: dict) -> str:
    for item in payload.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                return content["text"]
    raise ValueError("OpenAI 响应中没有 output_text。")

