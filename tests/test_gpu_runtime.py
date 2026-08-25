from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import voice_subtitle_translator.gpu_runtime as gpu_runtime
from voice_subtitle_translator.gpu_runtime import (
    GPU_PROFILE_BY_ID,
    GPURuntimeManager,
    RuntimeArtifact,
)
from voice_subtitle_translator.paths import AppPaths


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


def test_rtx50_profile_has_explanation_and_mixed_precision() -> None:
    profile = GPU_PROFILE_BY_ID["rtx50_int8_float16"]
    assert profile.device == "cuda"
    assert profile.compute_type == "int8_float16"
    assert "Blackwell" in profile.description
    assert "5070" in profile.recommendation


def test_green_runtime_download_is_verified_and_extracted(tmp_path: Path, monkeypatch) -> None:
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        archive.writestr("nvidia/test/bin/cublas64_12.dll", b"verified-dll")
        archive.writestr("nvidia/test/License.txt", b"test-license")
    payload = archive_bytes.getvalue()
    artifact = RuntimeArtifact(
        name="test-runtime",
        url="https://example.invalid/runtime.whl",
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )

    class Response:
        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self):
            yield payload

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    class Client:
        def __init__(self, **_kwargs) -> None:
            pass

        def stream(self, _method: str, _url: str) -> Response:
            return Response()

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    monkeypatch.setattr(gpu_runtime, "RUNTIME_ARTIFACTS", (artifact,))
    monkeypatch.setattr(gpu_runtime, "REQUIRED_DLLS", {"cublas64_12.dll"})
    monkeypatch.setattr(gpu_runtime.httpx, "Client", Client)
    paths = _paths(tmp_path)
    paths.ensure()
    manager = GPURuntimeManager(paths)
    manager.install()
    assert manager.is_installed()
    assert (manager.bin_dir / "cublas64_12.dll").read_bytes() == b"verified-dll"
    assert manager.manifest_path.is_file()


def test_gpu_runtime_uses_domestic_mirrors_before_official_source() -> None:
    artifact = gpu_runtime.RUNTIME_ARTIFACTS[0]
    urls = gpu_runtime._runtime_download_urls(artifact)
    assert urls[0].startswith("https://pypi.tuna.tsinghua.edu.cn/")
    assert urls[1].startswith("https://mirrors.aliyun.com/pypi/")
    assert urls[-1] == artifact.url


def test_gpu_runtime_status_always_contains_absolute_directory(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    manager = GPURuntimeManager(paths)
    assert str(manager.bin_dir.resolve()) in manager.status_text()
    assert str(manager.bin_dir.resolve()) in manager.manual_install_text()
