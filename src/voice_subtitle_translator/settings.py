from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from .paths import AppPaths


@dataclass(slots=True)
class GlobalSettings:
    last_translation_enabled: bool = False
    window_geometry: str = ""
    last_project: str = ""
    asr_device: str = "cpu"
    asr_compute_type: str = "int8"
    asr_profile: str = "cpu_int8"
    translation_provider: str = ""
    translation_base_url: str = ""
    translation_model: str = ""
    translation_structured_output: bool = False


class SettingsStore:
    def __init__(self, paths: AppPaths) -> None:
        self._path = paths.config / "settings.json"

    def load(self) -> GlobalSettings:
        if not self._path.exists():
            return GlobalSettings()
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
            if "asr_profile" not in value:
                if value.get("asr_device") == "cuda":
                    value["asr_profile"] = "rtx50_int8_float16"
                    value["asr_compute_type"] = "int8_float16"
                else:
                    value["asr_profile"] = "cpu_int8"
                    value["asr_compute_type"] = "int8"
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
