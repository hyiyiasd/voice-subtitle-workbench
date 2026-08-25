from pathlib import Path

from voice_subtitle_translator.worker_client import WorkerClient


def test_worker_uses_request_ids_and_shuts_down(tmp_path: Path) -> None:
    with WorkerClient(cwd=tmp_path) as worker:
        assert worker.call("ping", {"worker": "asr"}) == {"alive": True, "worker": "asr"}
        windows_path = r"F:\节目目录\第一话.mp3"
        assert worker.call("echo", {"path": windows_path}) == {"path": windows_path}
