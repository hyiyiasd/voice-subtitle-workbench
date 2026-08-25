from __future__ import annotations

import logging
import re
from pathlib import Path

SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)((?:api[-_ ]?key|token)\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
)


def redact(value: str) -> str:
    result = value
    for pattern in SECRET_PATTERNS:
        if pattern.groups:
            result = pattern.sub(r"\1***", result)
        else:
            result = pattern.sub("***", result)
    return result


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(str(record.msg))
        if record.args:
            record.args = tuple(redact(str(value)) for value in record.args)
        return True


def configure_logging(log_directory: Path) -> None:
    log_directory.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_directory / "application.log", encoding="utf-8")
    handler.addFilter(RedactingFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(handler)

