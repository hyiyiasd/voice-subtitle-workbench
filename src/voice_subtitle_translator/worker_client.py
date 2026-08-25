from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4


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
        self._stderr = (log_directory / "worker-stderr.log").open(
            "a", encoding="utf-8"
        )
        self._process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            text=True,
            encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def call(self, method: str, params: dict | None = None) -> dict:
        if self._process.poll() is not None:
            raise WorkerCrashedError(f"模型工作进程已退出：{self._process.returncode}")
        request_id = str(uuid4())
        request = {"id": request_id, "method": method, "params": params or {}}
        assert self._process.stdin is not None
        assert self._process.stdout is not None
        self._process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        self._process.stdin.flush()
        line = self._process.stdout.readline()
        if not line:
            raise WorkerCrashedError("模型工作进程未返回结果。")
        response = json.loads(line)
        if response.get("id") != request_id:
            raise WorkerCrashedError("模型工作进程返回了错误的请求 ID。")
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

    def __enter__(self) -> WorkerClient:
        return self

    def __exit__(self, *args) -> None:
        self.close()
