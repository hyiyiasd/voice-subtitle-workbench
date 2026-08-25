from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


class PortableDirectoryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AppPaths:
    root: Path
    data: Path
    config: Path
    models: Path
    cache: Path
    logs: Path
    temp: Path
    gpu_runtime: Path

    @classmethod
    def discover(cls) -> AppPaths:
        if getattr(sys, "frozen", False):
            root = Path(sys.executable).resolve().parent
            data = root / "data"
        elif value := os.environ.get("VST_DEV_ROOT"):
            root = Path(value).resolve()
            data = Path(os.environ.get("VST_DATA_ROOT", root / ".local")).resolve()
        else:
            root = Path(__file__).resolve().parents[2]
            data = root / ".local"
        return cls(
            root=root,
            data=data,
            config=data / "config",
            models=data / "models",
            cache=data / "cache",
            logs=data / "logs",
            temp=data / "temp",
            gpu_runtime=data / "gpu-runtime",
        )

    def ensure(self) -> None:
        for directory in (
            self.data,
            self.config,
            self.models,
            self.cache,
            self.logs,
            self.temp,
            self.gpu_runtime,
            self.data / "state",
            self.data / "subtitles" / "原文",
            self.data / "subtitles" / "中文",
            self.data / "subtitles" / "译文",
            self.data / "subtitles" / "双语",
        ):
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise PortableDirectoryError(
                    f"程序目录不可写：{directory}。请将程序移动到可写目录后重试。"
                ) from exc
        probe = self.temp / ".write-test"
        try:
            probe.write_bytes(b"ok")
            probe.unlink()
        except OSError as exc:
            raise PortableDirectoryError(
                f"程序目录不可写：{self.root}。请将程序移动到可写目录后重试。"
            ) from exc


def configure_library_environment(paths: AppPaths, *, offline: bool = False) -> None:
    """Keep model and library caches beside the portable application."""
    cache_values = {
        "HF_HOME": paths.cache / "huggingface",
        "HF_HUB_CACHE": paths.cache / "huggingface" / "hub",
        "TORCH_HOME": paths.cache / "torch",
        "XDG_CACHE_HOME": paths.cache / "xdg",
        "PIP_CACHE_DIR": paths.cache / "pip",
        "TEMP": paths.temp,
        "TMP": paths.temp,
        "TMPDIR": paths.temp,
    }
    for key, value in cache_values.items():
        os.environ[key] = str(value)
    os.environ["VST_DATA_ROOT"] = str(paths.data)
    if offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"


def bundled_resource(relative_path: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return base / relative_path
