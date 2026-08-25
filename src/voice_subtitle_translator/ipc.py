from __future__ import annotations

import base64
import json
from typing import Any

PROTOCOL_PREFIX = "vst1:"


def encode_message(value: dict[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return PROTOCOL_PREFIX + base64.b64encode(payload).decode("ascii")


def decode_message(line: str) -> dict[str, Any]:
    payload = line.strip()
    if payload.startswith(PROTOCOL_PREFIX):
        encoded = payload.removeprefix(PROTOCOL_PREFIX)
        payload = base64.b64decode(encoded, validate=True).decode("utf-8")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise TypeError("IPC 消息必须是 JSON 对象。")
    return value
