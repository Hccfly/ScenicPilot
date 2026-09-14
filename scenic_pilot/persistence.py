from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol


class IdempotencyConflict(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    thread_id: str
    idempotency_key: str
    operation: str
    input_hash: str
    status: str
    response: dict[str, Any] | None


def request_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ExecutionStore(Protocol):
    def claim(
        self,
        *,
        thread_id: str,
        idempotency_key: str,
        operation: str,
        input_hash: str,
    ) -> ExecutionRecord | None: ...

    def finish(
        self,
        *,
        thread_id: str,
        idempotency_key: str,
        status: str,
        response: dict[str, Any],
    ) -> None: ...

    def close(self) -> None: ...


class InMemoryExecutionStore:
    def __init__(self) -> None:
        self._records: dict[tuple[str, str], ExecutionRecord] = {}
        self._lock = threading.RLock()

    def claim(
        self,
        *,
        thread_id: str,
        idempotency_key: str,
        operation: str,
        input_hash: str,
    ) -> ExecutionRecord | None:
        key = (thread_id, idempotency_key)
        with self._lock:
            existing = self._records.get(key)
            if existing is not None:
                _validate_record(existing, operation=operation, input_hash=input_hash)
                return existing
            self._records[key] = ExecutionRecord(
                thread_id=thread_id,
                idempotency_key=idempotency_key,
                operation=operation,
                input_hash=input_hash,
                status="running",
                response=None,
            )
            return None

    def finish(
        self,
        *,
        thread_id: str,
        idempotency_key: str,
        status: str,
        response: dict[str, Any],
    ) -> None:
        key = (thread_id, idempotency_key)
        with self._lock:
            current = self._records[key]
            self._records[key] = ExecutionRecord(
                thread_id=current.thread_id,
                idempotency_key=current.idempotency_key,
                operation=current.operation,
                input_hash=current.input_hash,
                status=status,
                response=response,
            )

    def close(self) -> None:
        return None


class SQLiteExecutionStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock, self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS scenicpilot_execution_requests (
                    thread_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    response_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (thread_id, idempotency_key)
                )
                """
            )

    def claim(
        self,
        *,
        thread_id: str,
        idempotency_key: str,
        operation: str,
        input_hash: str,
    ) -> ExecutionRecord | None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connection:
            row = self._connection.execute(
                """
                SELECT thread_id, idempotency_key, operation, input_hash,
                       status, response_json
                FROM scenicpilot_execution_requests
                WHERE thread_id = ? AND idempotency_key = ?
                """,
                (thread_id, idempotency_key),
            ).fetchone()
            if row is not None:
                record = _row_to_record(row)
                _validate_record(record, operation=operation, input_hash=input_hash)
                return record
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO scenicpilot_execution_requests (
                    thread_id, idempotency_key, operation, input_hash,
                    status, response_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'running', NULL, ?, ?)
                """,
                (thread_id, idempotency_key, operation, input_hash, now, now),
            )
            if cursor.rowcount == 0:
                row = self._connection.execute(
                    """
                    SELECT thread_id, idempotency_key, operation, input_hash,
                           status, response_json
                    FROM scenicpilot_execution_requests
                    WHERE thread_id = ? AND idempotency_key = ?
                    """,
                    (thread_id, idempotency_key),
                ).fetchone()
                if row is None:
                    raise RuntimeError("idempotency claim disappeared")
                record = _row_to_record(row)
                _validate_record(record, operation=operation, input_hash=input_hash)
                return record
        return None

    def finish(
        self,
        *,
        thread_id: str,
        idempotency_key: str,
        status: str,
        response: dict[str, Any],
    ) -> None:
        serialized = json.dumps(response, ensure_ascii=False, sort_keys=True)
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE scenicpilot_execution_requests
                SET status = ?, response_json = ?, updated_at = ?
                WHERE thread_id = ? AND idempotency_key = ?
                """,
                (
                    status,
                    serialized,
                    datetime.now(UTC).isoformat(),
                    thread_id,
                    idempotency_key,
                ),
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def _row_to_record(row: sqlite3.Row) -> ExecutionRecord:
    raw_response = row["response_json"]
    return ExecutionRecord(
        thread_id=str(row["thread_id"]),
        idempotency_key=str(row["idempotency_key"]),
        operation=str(row["operation"]),
        input_hash=str(row["input_hash"]),
        status=str(row["status"]),
        response=json.loads(raw_response) if raw_response else None,
    )


def _validate_record(
    record: ExecutionRecord,
    *,
    operation: str,
    input_hash: str,
) -> None:
    if record.operation != operation or record.input_hash != input_hash:
        raise IdempotencyConflict(
            "idempotency key was already used with a different request"
        )
