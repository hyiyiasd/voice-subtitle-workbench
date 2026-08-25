from __future__ import annotations

import hashlib
import json


def translation_cache_key(
    *,
    source_text: str,
    source_revision: int,
    model: str,
    target_language: str,
    prompt: str,
    glossary: list[tuple[str, str]],
    provider_parameters: dict,
) -> str:
    canonical = json.dumps(
        {
            "source_text": source_text,
            "source_revision": source_revision,
            "model": model,
            "target_language": target_language,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "glossary_sha256": hashlib.sha256(
                json.dumps(glossary, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "provider_parameters": provider_parameters,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

