from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from .domain import ModelDescriptor
from .paths import AppPaths


class ModelIntegrityError(RuntimeError):
    pass


class ModelUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ModelArtifact:
    url: str
    relative_path: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ManagedModel:
    descriptor: ModelDescriptor
    artifacts: tuple[ModelArtifact, ...]
    downloadable: bool
    recommendation: str = ""
    description: str = ""
    note: str = ""


class ModelManager:
    def __init__(self, paths: AppPaths, manifest_path: Path) -> None:
        self.paths = paths
        self.manifest_path = manifest_path
        self.models = self._load_manifest(manifest_path)

    @staticmethod
    def _load_manifest(path: Path) -> dict[str, ManagedModel]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        result = {}
        for value in payload["models"]:
            descriptor = ModelDescriptor(
                id=value["id"],
                display_name=value["display_name"],
                source=value["source"],
                version=value["version"],
                license=value["license"],
                sha256=value.get("sha256", ""),
                size_bytes=value["size_bytes"],
                languages=tuple(value["languages"]),
                recommended_vram_mb=value["recommended_vram_mb"],
                runtime=value["runtime"],
            )
            artifacts = tuple(ModelArtifact(**artifact) for artifact in value.get("artifacts", []))
            result[descriptor.id] = ManagedModel(
                descriptor=descriptor,
                artifacts=artifacts,
                downloadable=bool(value.get("downloadable", False)),
                recommendation=value.get("recommendation", ""),
                description=value.get("description", ""),
                note=value.get("note", ""),
            )
        return result

    def model_path(self, model_id: str) -> Path:
        if model_id not in self.models:
            raise KeyError(model_id)
        return self.paths.models / model_id

    def is_installed(self, model_id: str) -> bool:
        model = self.models[model_id]
        root = self.model_path(model_id)
        return bool(model.artifacts) and all(
            (root / artifact.relative_path).is_file() for artifact in model.artifacts
        )

    def verify(self, model_id: str) -> None:
        model = self.models[model_id]
        root = self.model_path(model_id)
        if not model.artifacts:
            raise ModelUnavailableError(model.note or "模型清单尚未提供可下载文件。")
        for artifact in model.artifacts:
            target = _safe_child(root, artifact.relative_path)
            if not target.is_file() or target.stat().st_size != artifact.size_bytes:
                raise ModelIntegrityError(f"模型文件缺失或大小不符：{target.name}")
            if file_sha256(target) != artifact.sha256.lower():
                raise ModelIntegrityError(f"模型文件 SHA-256 不匹配：{target.name}")

    def download(
        self,
        model_id: str,
        *,
        offline: bool = False,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> None:
        model = self.models[model_id]
        if offline:
            raise ModelUnavailableError("离线模式禁止下载模型。")
        if not model.downloadable or not model.artifacts:
            raise ModelUnavailableError(model.note or "此模型暂未开放自动下载。")
        root = self.model_path(model_id)
        root.mkdir(parents=True, exist_ok=True)
        completed_bytes = 0
        total_bytes = sum(artifact.size_bytes for artifact in model.artifacts)
        if on_progress:
            on_progress(0, total_bytes)
        # Respect the user's HTTPS proxy when Hugging Face is not directly reachable.
        # Offline mode still returns above before any client or request is created.
        with httpx.Client(timeout=300, follow_redirects=True, trust_env=True) as client:
            for artifact in model.artifacts:
                target = _safe_child(root, artifact.relative_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                if (
                    target.is_file()
                    and target.stat().st_size == artifact.size_bytes
                    and file_sha256(target) == artifact.sha256.lower()
                ):
                    completed_bytes += artifact.size_bytes
                    if on_progress:
                        on_progress(completed_bytes, total_bytes)
                    continue
                temporary = self.paths.temp / f"{model_id}-{target.name}.part"
                temporary.unlink(missing_ok=True)
                with client.stream("GET", artifact.url) as response:
                    response.raise_for_status()
                    with temporary.open("wb") as output:
                        for chunk in response.iter_bytes():
                            output.write(chunk)
                            if on_progress:
                                on_progress(completed_bytes + output.tell(), total_bytes)
                if temporary.stat().st_size != artifact.size_bytes:
                    temporary.unlink(missing_ok=True)
                    raise ModelIntegrityError(f"下载大小不符：{artifact.relative_path}")
                if file_sha256(temporary) != artifact.sha256.lower():
                    temporary.unlink(missing_ok=True)
                    raise ModelIntegrityError(f"下载校验失败：{artifact.relative_path}")
                temporary.replace(target)
                completed_bytes += artifact.size_bytes
                if on_progress:
                    on_progress(completed_bytes, total_bytes)
        self.verify(model_id)

    def delete(self, model_id: str) -> None:
        root = self.model_path(model_id).resolve()
        models_root = self.paths.models.resolve()
        if root.parent != models_root:
            raise ValueError("拒绝删除模型目录之外的路径。")
        if root.exists():
            shutil.rmtree(root)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_child(root: Path, relative_path: str) -> Path:
    target = (root / relative_path).resolve()
    resolved_root = root.resolve()
    if target != resolved_root and resolved_root not in target.parents:
        raise ValueError("模型清单包含越界路径。")
    return target
