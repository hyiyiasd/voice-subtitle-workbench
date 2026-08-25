from __future__ import annotations

import json
import sys
import traceback
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .asr import FasterWhisperProvider, ReazonSpeechProvider
from .domain import AudioChunk
from .vad import SileroVAD


def _ping(params: dict[str, Any]) -> dict[str, Any]:
    return {"alive": True, "worker": params.get("worker", "generic")}


def _echo(params: dict[str, Any]) -> dict[str, Any]:
    return params


_ASR_PROVIDERS: dict[str, Any] = {}


def _asr_transcribe(params: dict[str, Any]) -> dict[str, Any]:
    provider_type = params["provider"]
    model_path = params["model_path"]
    language = params.get("language", "auto")
    device = params.get("device", "cpu")
    compute_type = params.get("compute_type", "int8")
    provider_key = f"{provider_type}|{model_path}|{language}|{device}|{compute_type}"
    provider = _ASR_PROVIDERS.get(provider_key)
    if provider is None:
        if provider_type == "faster-whisper":
            provider = FasterWhisperProvider(
                Path(model_path), device=device, compute_type=compute_type
            )
        elif provider_type == "reazonspeech-k2":
            provider = ReazonSpeechProvider(Path(model_path), language=language, device=device)
        else:
            raise ValueError(f"未知 ASR Provider：{provider_type}")
        _ASR_PROVIDERS[provider_key] = provider
    chunk = AudioChunk(**params["chunk"])
    candidates = provider.transcribe(chunk, model=params["model_id"])
    return {"candidates": [asdict(candidate) for candidate in candidates]}


def _vad_detect(params: dict[str, Any]) -> dict[str, Any]:
    ranges = SileroVAD(Path(params["model_path"])).speech_ranges(Path(params["wav_path"]))
    return {"ranges": [asdict(value) for value in ranges]}


def _close_providers() -> None:
    for provider in _ASR_PROVIDERS.values():
        provider.close()
    _ASR_PROVIDERS.clear()


METHODS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "ping": _ping,
    "echo": _echo,
    "vad.detect": _vad_detect,
    "asr.transcribe": _asr_transcribe,
}


def handle_request(request: dict[str, Any]) -> dict[str, Any]:
    request_id = request.get("id")
    method = request.get("method")
    if method == "shutdown":
        _close_providers()
        return {"id": request_id, "result": {"shutdown": True}}
    if not isinstance(method, str) or method not in METHODS:
        return {"id": request_id, "error": {"code": "method_not_found", "message": str(method)}}
    try:
        result = METHODS[method](request.get("params") or {})
        return {"id": request_id, "result": result}
    except Exception as exc:
        return {
            "id": request_id,
            "error": {
                "code": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            },
        }


def main() -> int:
    for line in sys.stdin:
        try:
            request = json.loads(line)
            response = handle_request(request)
        except Exception as exc:
            response = {"id": None, "error": {"code": type(exc).__name__, "message": str(exc)}}
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        if response.get("result", {}).get("shutdown"):
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
