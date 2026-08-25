from __future__ import annotations

import hashlib
import json
import os
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import httpx

from .paths import AppPaths


@dataclass(frozen=True, slots=True)
class GPUProfile:
    id: str
    name: str
    device: str
    compute_type: str
    recommendation: str
    description: str


GPU_PROFILES = (
    GPUProfile(
        id="rtx50_int8_float16",
        name="RTX 50 系极速模式（推荐 5070）",
        device="cuda",
        compute_type="int8_float16",
        recommendation="RTX 5060/5070/5080/5090 首选",
        description=(
            "针对 Blackwell RTX 50 系。权重使用 INT8，其他计算使用 FP16，"
            "通常是速度、显存和识别精度之间最实用的选择。"
            "建议 CUDA 12.8 或更新的运行库。"
        ),
    ),
    GPUProfile(
        id="rtx20_40_int8_float16",
        name="RTX 20/30/40 系均衡模式",
        device="cuda",
        compute_type="int8_float16",
        recommendation="RTX 20、30、40 系日常识别",
        description=(
            "使用 INT8 权重和 FP16 计算，显存占用较低，速度通常比纯 FP16 更快。"
            "适合 Turing、Ampere 和 Ada 架构的 RTX 显卡。"
        ),
    ),
    GPUProfile(
        id="cuda_float16",
        name="GPU FP16 高精度模式",
        device="cuda",
        compute_type="float16",
        recommendation="显存充足、希望尽量保留模型精度",
        description=(
            "权重和计算均以 FP16 为主，通常占用更多显存和存储带宽。"
            "与 INT8+FP16 相比，某些较难音频可能更稳，但速度不一定更快。"
        ),
    ),
    GPUProfile(
        id="cuda_bfloat16",
        name="GPU BF16 新架构实验模式",
        device="cuda",
        compute_type="bfloat16",
        recommendation="RTX 30 系及更新显卡的兼容性对比",
        description=(
            "BF16 动态范围较大，适合支持 BF16 的新显卡。"
            "这是对比或排查用选项，Whisper 常规使用仍建议优先 INT8+FP16。"
        ),
    ),
    GPUProfile(
        id="cpu_int8",
        name="CPU INT8 兼容模式",
        device="cpu",
        compute_type="int8",
        recommendation="没有 NVIDIA GPU 或不希望安装 CUDA",
        description="不需要 CUDA，显存占用为零，但长音频识别速度通常明显慢于 GPU。",
    ),
)

GPU_PROFILE_BY_ID = {profile.id: profile for profile in GPU_PROFILES}
DEFAULT_GPU_PROFILE = "rtx50_int8_float16"


@dataclass(frozen=True, slots=True)
class RuntimeArtifact:
    name: str
    url: str
    sha256: str
    size_bytes: int


RUNTIME_ARTIFACTS = (
    RuntimeArtifact(
        name="nvidia-cublas-cu12-12.9.2.10",
        url=(
            "https://files.pythonhosted.org/packages/20/e2/"
            "fc9a0e985249d873150276d5afb02e39a66817fedbf1a385724393e505ed/"
            "nvidia_cublas_cu12-12.9.2.10-py3-none-win_amd64.whl"
        ),
        sha256="623f43027d40d44ceadf0043f002bd25cf353e8f13ce90b9a87057019f560661",
        size_bytes=553_162_896,
    ),
    RuntimeArtifact(
        name="nvidia-cudnn-cu12-9.24.0.43",
        url=(
            "https://files.pythonhosted.org/packages/29/28/"
            "2c9a2a97a8b3fedcf74a14f38fd5edfae12274380a829fdc6b16ce29be4c/"
            "nvidia_cudnn_cu12-9.24.0.43-py3-none-win_amd64.whl"
        ),
        sha256="cbd41a0ab084422c936dc9fb2fc89be5ea9a85bc421c6f23d0243bdfc945fbef",
        size_bytes=737_103_728,
    ),
    RuntimeArtifact(
        name="nvidia-cuda-nvrtc-cu12-12.9.86",
        url=(
            "https://files.pythonhosted.org/packages/52/de/"
            "823919be3b9d0ccbf1f784035423c5f18f4267fb0123558d58b813c6ec86/"
            "nvidia_cuda_nvrtc_cu12-12.9.86-py3-none-win_amd64.whl"
        ),
        sha256="72972ebdcf504d69462d3bcd67e7b81edd25d0fb85a2c46d3ea3517666636349",
        size_bytes=76_408_187,
    ),
)

REQUIRED_DLLS = {
    "cublas64_12.dll",
    "cublasLt64_12.dll",
    "cudnn_adv64_9.dll",
    "cudnn_cnn64_9.dll",
    "cudnn_graph64_9.dll",
    "cudnn_ops64_9.dll",
    "nvrtc64_120_0.dll",
    "nvrtc-builtins64_129.dll",
}


class GPURuntimeManager:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self.root = paths.gpu_runtime / "cuda-12.9"
        self.bin_dir = self.root / "bin"
        self.manifest_path = self.root / "installed.json"

    @property
    def download_size(self) -> int:
        return sum(artifact.size_bytes for artifact in RUNTIME_ARTIFACTS)

    def is_installed(self) -> bool:
        return all((self.bin_dir / name).is_file() for name in REQUIRED_DLLS)

    def status_text(self) -> str:
        if self.is_installed():
            return f"已安装：{self.bin_dir}"
        return f"未安装：需下载约 {self.download_size / 1024**3:.2f} GB"

    def install(
        self,
        *,
        offline: bool = False,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> None:
        if offline:
            raise RuntimeError("离线模式禁止下载 GPU 运行库。")
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        licenses_dir = self.root / "licenses"
        licenses_dir.mkdir(parents=True, exist_ok=True)
        self.paths.temp.mkdir(parents=True, exist_ok=True)
        completed = 0
        total = self.download_size
        if on_progress:
            on_progress(0, total)
        installed_hashes: dict[str, str] = {}
        with httpx.Client(timeout=600, follow_redirects=True, trust_env=True) as client:
            for artifact in RUNTIME_ARTIFACTS:
                wheel = self.paths.temp / f"{artifact.name}.whl.part"
                wheel.unlink(missing_ok=True)
                try:
                    with client.stream("GET", artifact.url) as response:
                        response.raise_for_status()
                        with wheel.open("wb") as output:
                            for chunk in response.iter_bytes():
                                output.write(chunk)
                                if on_progress:
                                    on_progress(completed + output.tell(), total)
                    if wheel.stat().st_size != artifact.size_bytes:
                        raise RuntimeError(f"GPU 运行库大小不符：{artifact.name}")
                    if _file_sha256(wheel) != artifact.sha256:
                        raise RuntimeError(f"GPU 运行库 SHA-256 不匹配：{artifact.name}")
                    with zipfile.ZipFile(wheel) as archive:
                        for member in archive.infolist():
                            name = PurePosixPath(member.filename).name
                            if name in REQUIRED_DLLS:
                                target = self.bin_dir / name
                            elif name.lower() in {"license.txt", "license"}:
                                target = licenses_dir / f"{artifact.name}-{name}"
                            else:
                                continue
                            with archive.open(member) as source, target.open("wb") as output:
                                while chunk := source.read(1024 * 1024):
                                    output.write(chunk)
                            if target.parent == self.bin_dir:
                                installed_hashes[name] = _file_sha256(target)
                finally:
                    wheel.unlink(missing_ok=True)
                completed += artifact.size_bytes
                if on_progress:
                    on_progress(completed, total)
        if not self.is_installed():
            missing = sorted(name for name in REQUIRED_DLLS if not (self.bin_dir / name).is_file())
            raise RuntimeError(f"GPU 运行库解压不完整：{', '.join(missing)}")
        self.manifest_path.write_text(
            json.dumps(
                {
                    "runtime": "CUDA 12.9 / cuBLAS 12.9 / cuDNN 9.24",
                    "source": "NVIDIA packages published on PyPI",
                    "license": "NVIDIA Proprietary Software",
                    "files": installed_hashes,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def worker_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        if self.bin_dir.is_dir():
            environment["PATH"] = f"{self.bin_dir}{os.pathsep}{environment.get('PATH', '')}"
        return environment


def selected_profile(profile_id: str) -> GPUProfile:
    return GPU_PROFILE_BY_ID.get(profile_id, GPU_PROFILE_BY_ID[DEFAULT_GPU_PROFILE])


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
