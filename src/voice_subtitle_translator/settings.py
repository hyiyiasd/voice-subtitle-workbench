from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from .paths import AppPaths


@dataclass(slots=True)
class GlobalSettings:
    last_translation_enabled: bool = False
    window_geometry: str = ""
    last_project: str = ""


class SettingsStore:
    def __init__(self, paths: AppPaths) -> None:
        self._path = paths.config / "settings.json"

    def load(self) -> GlobalSettings:
        if not self._path.exists():
            return GlobalSettings()
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
            return GlobalSettings(**value)
        except (OSError, ValueError, TypeError):
            return GlobalSettings()

    def save(self, settings: GlobalSettings) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(asdict(settings), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self._path)

