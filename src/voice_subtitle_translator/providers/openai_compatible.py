from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from ..domain import ProviderCapabilities, TranslationItem, TranslationResult
from .common import build_translation_messages, parse_translation_response


class OfflineModeError(RuntimeError):
    pass


class ProviderRequestError(RuntimeError):
    """A provider error safe to display without exposing request headers."""


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    id: str = "openai-compatible"
    base_url: str = "http://127.0.0.1:11434/v1"
    api_key: str = ""
    timeout_seconds: float = 120.0
    extra_headers: dict[str, str] = field(default_factory=dict)
    structured_output: bool = False
    offline: bool = False


class OpenAICompatibleProvider:
    def __init__(self, config: ProviderConfig, client: httpx.Client | None = None) -> None:
        self.config = config
        self.id = config.id
        self.capabilities = ProviderCapabilities(
            structured_output=config.structured_output,
            style_instructions=True,
            streaming=False,
            local=config.base_url.startswith(("http://127.0.0.1", "http://localhost")),
            usage_reporting=True,
        )
        headers = {"Content-Type": "application/json", **config.extra_headers}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        self._client = client or httpx.Client(
            base_url=config.base_url.rstrip("/"),
            headers=headers,
            timeout=config.timeout_seconds,
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
        if self.config.offline:
            raise OfflineModeError("离线模式禁止所有 HTTP 翻译请求，包括本地 HTTP 服务。")
        if not items:
            return []
        messages = build_translation_messages(
            items,
            target_language=target_language,
            context=context,
            glossary=glossary or [],
            style_instruction=style_instruction,
        )
        body: dict = {"model": model, "messages": messages, "temperature": 0}
        if self.config.id == "deepseek":
            body["thinking"] = {"type": "disabled"}
        if self.config.structured_output or self.config.id == "deepseek":
            body["response_format"] = {"type": "json_object"}
        payload = self._post_chat(body)
        raw = payload["choices"][0]["message"]["content"]
        return parse_translation_response(raw, [item.segment_id for item in items])

    def test_connection(self, *, text: str, model: str) -> str:
        """Send a small real request and return its plain-text response."""
        if self.config.offline:
            raise OfflineModeError("离线模式禁止所有 HTTP 翻译请求，包括接口测试。")
        body = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": "请简洁回答用户的测试内容。这是连通性测试。",
                },
                {"role": "user", "content": text},
            ],
            "temperature": 0,
        }
        if self.config.id == "deepseek":
            body["thinking"] = {"type": "disabled"}
        payload = self._post_chat(body)
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderRequestError("接口返回成功，但响应中没有可显示的文本。") from exc
        if not isinstance(content, str) or not content.strip():
            raise ProviderRequestError("接口返回成功，但模型输出为空。")
        return content.strip()

    def _post_chat(self, body: dict) -> dict:
        try:
            response = self._client.post("/chat/completions", json=body)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text.strip()[:2000]
            suffix = f"\n服务返回：{detail}" if detail else ""
            raise ProviderRequestError(
                f"接口请求失败（HTTP {exc.response.status_code}）。{suffix}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise ProviderRequestError(
                f"接口请求超时（{self.config.timeout_seconds:g} 秒）。"
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderRequestError(f"无法连接翻译接口：{exc}") from exc
        except ValueError as exc:
            raise ProviderRequestError("接口没有返回有效 JSON。") from exc
        if not isinstance(payload, dict):
            raise ProviderRequestError("接口返回的 JSON 不是对象。")
        return payload

    def close(self) -> None:
        self._client.close()


PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "deepseek": {"base_url": "https://api.deepseek.com"},
    "zhipu": {"base_url": "https://open.bigmodel.cn/api/paas/v4"},
    "ollama": {"base_url": "http://127.0.0.1:11434/v1"},
    "lm-studio": {"base_url": "http://127.0.0.1:1234/v1"},
}
