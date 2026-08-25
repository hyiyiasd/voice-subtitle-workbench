import json
import os
from pathlib import Path

from voice_subtitle_translator.paths import AppPaths
from voice_subtitle_translator.settings import GlobalSettings, SettingsStore


def _paths(root: Path) -> AppPaths:
    data = root / "data"
    return AppPaths(
        root=root,
        data=data,
        config=data / "config",
        models=data / "models",
        cache=data / "cache",
        logs=data / "logs",
        temp=data / "temp",
        gpu_runtime=data / "gpu-runtime",
    )


def test_global_translation_default_is_false_and_persists(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.ensure()
    store = SettingsStore(paths)
    assert store.load().last_translation_enabled is False
    store.save(GlobalSettings(last_translation_enabled=True))
    assert store.load().last_translation_enabled is True


def test_translation_service_settings_persist_without_api_key(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.ensure()
    store = SettingsStore(paths)
    store.save(
        GlobalSettings(
            translation_provider="ollama",
            translation_base_url="http://127.0.0.1:11434/v1",
            translation_model="qwen2.5:7b",
        )
    )
    loaded = store.load()
    assert loaded.translation_provider == "ollama"
    assert loaded.translation_model == "qwen2.5:7b"
    assert "api_key" not in (paths.config / "settings.json").read_text(encoding="utf-8")


def test_legacy_cuda_setting_migrates_to_rtx50_profile(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.ensure()
    (paths.config / "settings.json").write_text(
        json.dumps({"asr_device": "cuda"}), encoding="utf-8"
    )
    loaded = SettingsStore(paths).load()
    assert loaded.asr_profile == "rtx50_int8_float16"
    assert loaded.asr_compute_type == "int8_float16"


def test_paths_only_create_inside_given_root(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.ensure()
    assert (paths.data / "subtitles" / "原文").is_dir()
    assert (paths.data / "subtitles" / "译文").is_dir()
    assert (paths.data / "subtitles" / "双语").is_dir()
    for path in (
        paths.data,
        paths.config,
        paths.models,
        paths.cache,
        paths.logs,
        paths.temp,
        paths.gpu_runtime,
    ):
        assert tmp_path.resolve() in path.resolve().parents or path.resolve() == tmp_path.resolve()


def test_development_discovery_uses_local_directory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VST_DEV_ROOT", str(tmp_path))
    monkeypatch.delenv("VST_DATA_ROOT", raising=False)
    paths = AppPaths.discover()
    assert paths.root == tmp_path.resolve()
    assert paths.data == (tmp_path / ".local").resolve()
    assert os.fspath(paths.models).startswith(os.fspath(paths.data))
