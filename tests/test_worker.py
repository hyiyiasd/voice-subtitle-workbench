from pathlib import Path

from voice_subtitle_translator.worker_client import WorkerClient


def test_worker_uses_request_ids_and_shuts_down(tmp_path: Path) -> None:
    with WorkerClient(cwd=tmp_path) as worker:
        assert worker.call("ping", {"worker": "asr"}) == {"alive": True, "worker": "asr"}

