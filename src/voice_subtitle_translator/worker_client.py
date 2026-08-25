from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from .ipc import decode_message, encode_message


class WorkerCrashedError(RuntimeError):
    pass


class WorkerClient:
    def __init__(self, *, cwd: Path) -> None:
        if getattr(sys, "frozen", False):
            console_worker = Path(sys.executable).resolve().with_name("vst-cli.exe")
            executable = console_worker if console_worker.is_file() else Path(sys.executable)
            command = [str(executable), "--worker"]
        else:
            command = [sys.executable, "-m", "voice_subtitle_translator.worker"]
        data_root = Path(os.environ.get("VST_DATA_ROOT", cwd / "data"))
        log_directory = data_root / "logs"
        log_directory.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        gpu_bin = data_root / "gpu-runtime" / "cuda-12.9" / "bin"
        if gpu_bin.is_dir():
            environment["PATH"] = f"{gpu_bin}{os.pathsep}{environment.get('PATH', '')}"
        self._stderr = (log_directory / "worker-stderr.log").open("a", encoding="utf-8")
        self._process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            text=True,
            encoding="ascii",
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def call(self, method: str, params: dict | None = None) -> dict:
        if self._process.poll() is not None:
            raise WorkerCrashedError(f"模型工作进程已退出：{self._process.returncode}")
        request_id = str(uuid4())
        request = {"id": request_id, "method": method, "params": params or {}}
        assert self._process.stdin is not None
        assert self._process.stdout is not None
        self._process.stdin.write(encode_message(request) + "\n")
        self._process.stdin.flush()
        line = self._process.stdout.readline()
        if not line:
            raise WorkerCrashedError("模型工作进程未返回结果。")
        response = decode_message(line)
        if response.get("id") != request_id:
            raise WorkerCrashedError(
                f"模型工作进程返回了错误的请求 ID：期望 {request_id}，实际 {response.get('id')}。"
            )
        if error := response.get("error"):
            raise RuntimeError(error["message"])
        return response["result"]

    def close(self) -> None:
        if self._process.poll() is None:
            try:
                self.call("shutdown")
                self._process.wait(timeout=5)
            except Exception:
                self._process.kill()
        for stream in (self._process.stdin, self._process.stdout):
            if stream:
                stream.close()
        self._stderr.close()

    def terminate(self) -> None:
        """Immediately stop the model subprocess and unblock an active IPC call."""
        if self._process.poll() is None:
            self._process.kill()

    def __enter__(self) -> WorkerClient:
        return self

    def __exit__(self, *args) -> None:
        self.close()
