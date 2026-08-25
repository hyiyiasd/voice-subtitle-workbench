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


def test_paths_only_create_inside_given_root(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.ensure()
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
