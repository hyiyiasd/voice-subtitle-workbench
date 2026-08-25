from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from .domain import ASRCandidate, ProjectSettings, Segment, TaskKind, TaskStatus

SCHEMA_VERSION = 1
APPLICATION_ID = 0x56535450  # VSTP


SCHEMA = """
CREATE TABLE IF NOT EXISTS project_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_settings (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    value_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS media_assets (
    id TEXT PRIMARY KEY,
    absolute_path TEXT NOT NULL,
    relative_path TEXT,
    size_bytes INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    duration_ms INTEGER
);
CREATE TABLE IF NOT EXISTS segments (
    id TEXT PRIMARY KEY,
    order_key INTEGER NOT NULL,
    start_ms INTEGER NOT NULL CHECK (start_ms >= 0),
    end_ms INTEGER NOT NULL CHECK (end_ms > start_ms),
    language TEXT NOT NULL,
    source_text TEXT NOT NULL,
    translated_text TEXT,
    confidence REAL,
    quality_flags_json TEXT NOT NULL DEFAULT '[]',
    human_locked INTEGER NOT NULL DEFAULT 0,
    source_revision INTEGER NOT NULL DEFAULT 1,
    translation_source_revision INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_segments_order ON segments(order_key, start_ms, id);
CREATE TABLE IF NOT EXISTS segment_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id TEXT NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    source_text TEXT NOT NULL,
    translated_text TEXT,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS segment_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id TEXT REFERENCES segments(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    text TEXT NOT NULL,
    language TEXT NOT NULL,
    confidence REAL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS prompts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    content TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS glossary_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    UNIQUE(source, target)
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    total_batches INTEGER NOT NULL DEFAULT 0,
    completed_batches INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS task_batches (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    batch_index INTEGER NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT,
    PRIMARY KEY(task_id, batch_index)
);
CREATE TABLE IF NOT EXISTS translation_cache (
    cache_key TEXT PRIMARY KEY,
    segment_id TEXT NOT NULL,
    translated_text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    undone INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class InvalidProjectError(RuntimeError):
    pass


class Project(AbstractContextManager["Project"]):
    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self.path = path
        self.connection = connection
        self.connection.row_factory = sqlite3.Row

    @classmethod
    def create(cls, path: str | Path, settings: ProjectSettings | None = None) -> Project:
        target = Path(path).resolve()
        if target.suffix.lower() != ".vstproj":
            target = target.with_suffix(".vstproj")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise FileExistsError(target)
        connection = sqlite3.connect(target)
        project = cls(target, connection)
        try:
            project._initialize(settings or ProjectSettings())
        except Exception:
            connection.close()
            target.unlink(missing_ok=True)
            raise
        return project

    @classmethod
    def open(cls, path: str | Path) -> Project:
        target = Path(path).resolve()
        if not target.is_file():
            raise FileNotFoundError(target)
        connection = sqlite3.connect(target)
        project = cls(target, connection)
        app_id = connection.execute("PRAGMA application_id").fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if app_id != APPLICATION_ID or version > SCHEMA_VERSION:
            connection.close()
            raise InvalidProjectError(f"不是受支持的 .vstproj 项目：{target}")
        project._configure_connection()
        project.recover_interrupted_tasks()
        return project

    def _configure_connection(self) -> None:
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = DELETE")
        self.connection.execute("PRAGMA synchronous = FULL")
        self.connection.execute("PRAGMA busy_timeout = 5000")

    def _initialize(self, settings: ProjectSettings) -> None:
        self._configure_connection()
        with self.connection:
            self.connection.executescript(SCHEMA)
            self.connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
            self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self.connection.execute(
                "INSERT INTO project_meta(key, value) VALUES('project_id', ?)",
                (str(uuid4()),),
            )
            self.connection.execute(
                "INSERT INTO project_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self.connection.execute(
                "INSERT INTO project_settings(singleton, value_json) VALUES(1, ?)",
                (json.dumps(settings.to_dict(), ensure_ascii=False),),
            )

    def close(self) -> None:
        self.connection.commit()
        self.connection.close()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if exc_type is None:
            self.connection.commit()
        else:
            self.connection.rollback()
        self.connection.close()

    def get_settings(self) -> ProjectSettings:
        row = self.connection.execute(
            "SELECT value_json FROM project_settings WHERE singleton = 1"
        ).fetchone()
        return ProjectSettings.from_dict(json.loads(row[0]))

    def save_settings(self, settings: ProjectSettings) -> None:
        value = json.dumps(settings.to_dict(), ensure_ascii=False)
        with self.connection:
            self.connection.execute(
                "UPDATE project_settings SET value_json = ? WHERE singleton = 1", (value,)
            )

    def set_media(self, media_path: str | Path, duration_ms: int | None = None) -> str:
        source = Path(media_path).resolve()
        stat = source.stat()
        media_id = str(uuid4())
        try:
            relative_path = str(source.relative_to(self.path.parent))
        except ValueError:
            relative_path = ""
        fingerprint = quick_file_fingerprint(source)
        with self.connection:
            self.connection.execute("DELETE FROM media_assets")
            self.connection.execute(
                """INSERT INTO media_assets
                   (id, absolute_path, relative_path, size_bytes, fingerprint, duration_ms)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (media_id, str(source), relative_path, stat.st_size, fingerprint, duration_ms),
            )
        return media_id

    def resolve_media(self) -> Path | None:
        row = self.connection.execute(
            "SELECT absolute_path, relative_path, size_bytes FROM media_assets LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        candidates = [Path(row["absolute_path"])]
        if row["relative_path"]:
            candidates.append(self.path.parent / row["relative_path"])
        for candidate in candidates:
            if candidate.is_file() and candidate.stat().st_size == row["size_bytes"]:
                return candidate.resolve()
        return None

    def add_segment(self, segment: Segment, *, reason: str = "create") -> None:
        with self.connection:
            self._insert_segment(segment)
            self._save_revision(segment, reason)

    def add_segments(self, segments: Iterable[Segment], *, reason: str = "create") -> None:
        with self.connection:
            for segment in segments:
                self._insert_segment(segment)
                self._save_revision(segment, reason)

    def _insert_segment(self, segment: Segment) -> None:
        self.connection.execute(
            """INSERT INTO segments
               (id, order_key, start_ms, end_ms, language, source_text, translated_text,
                confidence, quality_flags_json, human_locked, source_revision,
                translation_source_revision)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                segment.id,
                segment.order_key,
                segment.start_ms,
                segment.end_ms,
                segment.language,
                segment.source_text,
                segment.translated_text,
                segment.confidence,
                json.dumps(sorted(segment.quality_flags), ensure_ascii=False),
                int(segment.human_locked),
                segment.source_revision,
                segment.translation_source_revision,
            ),
        )

    def _save_revision(self, segment: Segment, reason: str) -> None:
        self.connection.execute(
            """INSERT INTO segment_revisions
               (segment_id, revision, source_text, translated_text, reason)
               VALUES (?, ?, ?, ?, ?)""",
            (
                segment.id,
                segment.source_revision,
                segment.source_text,
                segment.translated_text,
                reason,
            ),
        )

    def list_segments(self) -> list[Segment]:
        rows = self.connection.execute(
            "SELECT * FROM segments ORDER BY order_key, start_ms, id"
        ).fetchall()
        return [self._row_to_segment(row) for row in rows]

    @staticmethod
    def _row_to_segment(row: sqlite3.Row) -> Segment:
        return Segment(
            id=row["id"],
            order_key=row["order_key"],
            start_ms=row["start_ms"],
            end_ms=row["end_ms"],
            language=row["language"],
            source_text=row["source_text"],
            translated_text=row["translated_text"],
            confidence=row["confidence"],
            quality_flags=set(json.loads(row["quality_flags_json"])),
            human_locked=bool(row["human_locked"]),
            source_revision=row["source_revision"],
            translation_source_revision=row["translation_source_revision"],
        )

    def get_segment(self, segment_id: str) -> Segment:
        row = self.connection.execute(
            "SELECT * FROM segments WHERE id = ?", (segment_id,)
        ).fetchone()
        if row is None:
            raise KeyError(segment_id)
        return self._row_to_segment(row)

    def update_source_text(self, segment_id: str, text: str, *, lock: bool = True) -> Segment:
        segment = self.get_segment(segment_id)
        if text == segment.source_text and lock == segment.human_locked:
            return segment
        segment.source_revision += 1
        segment.source_text = text
        segment.human_locked = lock
        with self.connection:
            self.connection.execute(
                """UPDATE segments SET source_text = ?, source_revision = ?, human_locked = ?,
                   updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (text, segment.source_revision, int(lock), segment_id),
            )
            self._save_revision(segment, "manual_edit")
            self.connection.execute(
                "INSERT INTO history(action, payload_json) VALUES('edit_source', ?)",
                (json.dumps(asdict(segment), ensure_ascii=False, default=list),),
            )
        return segment

    def set_human_locked(self, segment_id: str, locked: bool) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE segments SET human_locked = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (int(locked), segment_id),
            )

    def save_translation(self, segment_id: str, text: str, cache_key: str | None = None) -> None:
        segment = self.get_segment(segment_id)
        with self.connection:
            self.connection.execute(
                """UPDATE segments SET translated_text = ?, translation_source_revision = ?,
                   updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (text, segment.source_revision, segment_id),
            )
            if cache_key:
                self.connection.execute(
                    """INSERT INTO translation_cache(cache_key, segment_id, translated_text)
                       VALUES (?, ?, ?)
                       ON CONFLICT(cache_key) DO UPDATE SET
                       segment_id = excluded.segment_id,
                       translated_text = excluded.translated_text""",
                    (cache_key, segment_id, text),
                )

    def cached_translation(self, cache_key: str) -> str | None:
        row = self.connection.execute(
            "SELECT translated_text FROM translation_cache WHERE cache_key = ?", (cache_key,)
        ).fetchone()
        return None if row is None else str(row[0])

    def active_prompt(self) -> str:
        row = self.connection.execute(
            "SELECT content FROM prompts WHERE active = 1 ORDER BY rowid LIMIT 1"
        ).fetchone()
        return "" if row is None else str(row[0])

    def save_active_prompt(self, content: str) -> None:
        with self.connection:
            self.connection.execute("UPDATE prompts SET active = 0")
            self.connection.execute(
                """INSERT INTO prompts(id, name, content, active)
                   VALUES ('default', '默认提示词', ?, 1)
                   ON CONFLICT(id) DO UPDATE SET content = excluded.content, active = 1""",
                (content,),
            )

    def glossary(self) -> list[tuple[str, str]]:
        rows = self.connection.execute(
            "SELECT source, target FROM glossary_entries ORDER BY source, target"
        ).fetchall()
        return [(str(row[0]), str(row[1])) for row in rows]

    def save_glossary(self, entries: Iterable[tuple[str, str]]) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM glossary_entries")
            self.connection.executemany(
                "INSERT INTO glossary_entries(source, target) VALUES (?, ?)", list(entries)
            )

    def apply_asr_candidates(
        self,
        candidates: Iterable[ASRCandidate],
        *,
        provider: str,
        model: str,
        replace_segment_ids: Iterable[str] = (),
    ) -> list[Segment]:
        """Replace unlocked selected segments; preserve locked rows as comparison candidates."""
        replace_ids = list(replace_segment_ids)
        existing = {segment.id: segment for segment in self.list_segments()}
        unlocked_ids = [
            sid for sid in replace_ids if sid in existing and not existing[sid].human_locked
        ]
        locked_segments = [
            existing[sid] for sid in replace_ids if sid in existing and existing[sid].human_locked
        ]
        result: list[Segment] = []
        with self.connection:
            if unlocked_ids:
                placeholders = ",".join("?" for _ in unlocked_ids)
                self.connection.execute(
                    f"DELETE FROM segments WHERE id IN ({placeholders})", unlocked_ids
                )
            next_order = self.connection.execute(
                "SELECT COALESCE(MAX(order_key), -1) + 1 FROM segments"
            ).fetchone()[0]
            for candidate in candidates:
                overlapping = [
                    segment
                    for segment in locked_segments
                    if min(segment.end_ms, candidate.end_ms)
                    > max(segment.start_ms, candidate.start_ms)
                ]
                if overlapping:
                    segment_id = max(
                        overlapping,
                        key=lambda segment: min(segment.end_ms, candidate.end_ms)
                        - max(segment.start_ms, candidate.start_ms),
                    ).id
                    self.connection.execute(
                        """INSERT INTO segment_candidates
                           (segment_id, provider, model, start_ms, end_ms, text,
                            language, confidence)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            segment_id,
                            provider,
                            model,
                            candidate.start_ms,
                            candidate.end_ms,
                            candidate.text,
                            candidate.language,
                            candidate.confidence,
                        ),
                    )
                    continue
                segment = Segment(
                    order_key=next_order + len(result),
                    start_ms=candidate.start_ms,
                    end_ms=candidate.end_ms,
                    source_text=candidate.text,
                    language=candidate.language,
                    confidence=candidate.confidence,
                )
                self._insert_segment(segment)
                self._save_revision(segment, "asr")
                result.append(segment)
        return result

    def create_task(
        self, kind: TaskKind, *, total_batches: int, payload: dict | None = None
    ) -> str:
        task_id = str(uuid4())
        with self.connection:
            self.connection.execute(
                """INSERT INTO tasks(id, kind, status, total_batches, payload_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    task_id,
                    kind.value,
                    TaskStatus.QUEUED.value,
                    total_batches,
                    json.dumps(payload or {}, ensure_ascii=False),
                ),
            )
        return task_id

    def set_task_status(self, task_id: str, status: TaskStatus, error: str | None = None) -> None:
        with self.connection:
            self.connection.execute(
                """UPDATE tasks SET status = ?, error = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (status.value, error, task_id),
            )

    def complete_batch(self, task_id: str, batch_index: int, result: dict) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO task_batches(task_id, batch_index, status, result_json)
                   VALUES (?, ?, 'completed', ?)
                   ON CONFLICT(task_id, batch_index) DO NOTHING""",
                (task_id, batch_index, json.dumps(result, ensure_ascii=False)),
            )
            self.connection.execute(
                """UPDATE tasks SET completed_batches =
                   (SELECT COUNT(*) FROM task_batches
                    WHERE task_id = ? AND status = 'completed'),
                   updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (task_id, task_id),
            )

    def recover_interrupted_tasks(self) -> int:
        with self.connection:
            cursor = self.connection.execute(
                """UPDATE tasks SET status = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE status IN (?, ?)""",
                (
                    TaskStatus.INTERRUPTED.value,
                    TaskStatus.RUNNING.value,
                    TaskStatus.CANCEL_REQUESTED.value,
                ),
            )
        return cursor.rowcount

    def task(self, task_id: str) -> dict:
        row = self.connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result


def quick_file_fingerprint(path: Path, block_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    size = path.stat().st_size
    digest.update(str(size).encode("ascii"))
    with path.open("rb") as stream:
        digest.update(stream.read(block_size))
        if size > block_size:
            stream.seek(max(0, size - block_size))
            digest.update(stream.read(block_size))
    return digest.hexdigest()
