from __future__ import annotations

import json
import sqlite3
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from scenic_pilot.agent.dependencies import RuntimeDependencies, default_dependencies
from scenic_pilot.agent.graph import build_graph
from scenic_pilot.persistence import (
    ExecutionStore,
    InMemoryExecutionStore,
    SQLiteExecutionStore,
    request_hash,
)


class ScenicPilotRuntime:
    def __init__(
        self,
        dependencies: RuntimeDependencies | None = None,
        *,
        checkpoint_path: Path | None = None,
        checkpointer: Any = None,
        execution_store: ExecutionStore | None = None,
    ) -> None:
        self.dependencies = dependencies or default_dependencies()
        self._checkpoint_connection: sqlite3.Connection | None = None
        self._graph_lock = threading.RLock()
        selected_path = checkpoint_path
        if dependencies is None and selected_path is None and checkpointer is None:
            selected_path = self.dependencies.settings.checkpoint_db_path
        if checkpointer is None and selected_path is not None:
            selected_path.parent.mkdir(parents=True, exist_ok=True)
            self._checkpoint_connection = sqlite3.connect(
                selected_path,
                check_same_thread=False,
            )
            checkpointer = SqliteSaver(self._checkpoint_connection)
            checkpointer.setup()
        elif checkpointer is None:
            checkpointer = InMemorySaver()
        if execution_store is not None:
            self.execution_store = execution_store
        elif selected_path is not None:
            self.execution_store = SQLiteExecutionStore(selected_path)
        else:
            self.execution_store = InMemoryExecutionStore()
        self.graph = build_graph(self.dependencies, checkpointer=checkpointer)

    def invoke(
        self,
        message: str,
        *,
        scenic_id: str | None = None,
        thread_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        envelope = self.start(
            message,
            scenic_id=scenic_id,
            thread_id=thread_id,
            idempotency_key=idempotency_key,
            require_approval=False,
        )
        if envelope["status"] != "completed" or envelope.get("result") is None:
            raise RuntimeError(f"graph did not complete: {envelope['status']}")
        return dict(envelope["result"])

    def start(
        self,
        message: str,
        *,
        scenic_id: str | None = None,
        thread_id: str | None = None,
        idempotency_key: str | None = None,
        require_approval: bool = False,
    ) -> dict[str, Any]:
        query = message.strip()
        if not query:
            raise ValueError("message cannot be empty")
        selected_thread = self._identifier(thread_id, prefix="thread")
        selected_key = self._identifier(idempotency_key, prefix="request")
        selected_scenic = scenic_id or self.dependencies.settings.scenic_id
        approval_required = self._approval_required(require_approval)
        payload = {
            "operation": "start",
            "message": query,
            "scenic_id": selected_scenic,
            "approval_required": approval_required,
        }
        existing = self.execution_store.claim(
            thread_id=selected_thread,
            idempotency_key=selected_key,
            operation="start",
            input_hash=request_hash(payload),
        )
        if existing is not None:
            return self._replayed_or_recovered(
                existing.response, selected_thread, selected_key
            )

        return self._run_claimed(
            graph_input={
                "query": query,
                "scenic_id": selected_scenic,
                "approval_required": approval_required,
                "trace": [],
            },
            thread_id=selected_thread,
            idempotency_key=selected_key,
        )

    def resume(
        self,
        *,
        thread_id: str,
        approved: bool,
        reason: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        selected_thread = self._identifier(thread_id, prefix="thread", generate=False)
        selected_key = self._identifier(idempotency_key, prefix="resume")
        payload = {
            "operation": "resume",
            "approved": bool(approved),
            "reason": reason[:500],
        }
        existing = self.execution_store.claim(
            thread_id=selected_thread,
            idempotency_key=selected_key,
            operation="resume",
            input_hash=request_hash(payload),
        )
        if existing is not None:
            return self._replayed_or_recovered(
                existing.response, selected_thread, selected_key
            )

        return self._run_claimed(
            graph_input=Command(
                resume={"approved": bool(approved), "reason": reason[:500]}
            ),
            thread_id=selected_thread,
            idempotency_key=selected_key,
        )

    def get_session(self, thread_id: str) -> dict[str, Any]:
        selected_thread = self._identifier(thread_id, prefix="thread", generate=False)
        with self._graph_lock:
            snapshot = self.graph.get_state(self._config(selected_thread))
        interrupts = [self._interrupt_value(item) for item in snapshot.interrupts]
        if interrupts:
            status = "awaiting_approval"
        elif snapshot.next:
            status = "running"
        elif snapshot.values:
            status = "completed"
        else:
            status = "not_found"
        return {
            "status": status,
            "thread_id": selected_thread,
            "next_nodes": list(snapshot.next),
            "interrupts": interrupts,
            "result": dict(snapshot.values) if snapshot.values else None,
            "checkpoint": {
                "created_at": snapshot.created_at,
                "checkpoint_id": snapshot.config.get("configurable", {}).get(
                    "checkpoint_id"
                ),
            },
        }

    def close(self) -> None:
        with self._graph_lock:
            self.execution_store.close()
            if self._checkpoint_connection is not None:
                self._checkpoint_connection.close()
                self._checkpoint_connection = None

    def _approval_required(self, requested: bool) -> bool:
        mode = self.dependencies.settings.approval_mode.strip().lower()
        if mode == "always":
            return True
        if mode == "on_request":
            return requested
        if mode in {"disabled", "never"}:
            return False
        raise ValueError(
            "SCENICPILOT_APPROVAL_MODE must be on_request, always, or disabled"
        )

    def _config(self, thread_id: str) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": self.dependencies.settings.execution_recursion_limit,
        }

    def _run_claimed(
        self,
        *,
        graph_input: dict[str, Any] | Command,
        thread_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        try:
            with self._graph_lock:
                result = self.graph.invoke(
                    graph_input,
                    config=self._config(thread_id),
                )
            envelope = self._envelope(result, thread_id, idempotency_key)
        except Exception as error:
            envelope = {
                "status": "failed",
                "thread_id": thread_id,
                "idempotency_key": idempotency_key,
                "idempotency_replayed": False,
                "interrupts": [],
                "result": None,
                "error": {
                    "code": "graph_execution_failed",
                    "type": type(error).__name__,
                    "message": str(error)[:500],
                },
            }
        envelope = json.loads(json.dumps(envelope, ensure_ascii=False))
        self.execution_store.finish(
            thread_id=thread_id,
            idempotency_key=idempotency_key,
            status=str(envelope["status"]),
            response=envelope,
        )
        return envelope

    @staticmethod
    def _identifier(
        value: str | None,
        *,
        prefix: str,
        generate: bool = True,
    ) -> str:
        selected = (value or "").strip()
        if not selected:
            if not generate:
                raise ValueError(f"{prefix}_id cannot be empty")
            return f"{prefix}-{uuid4()}"
        if len(selected) > 128:
            raise ValueError(f"{prefix}_id is too long")
        return selected

    @classmethod
    def _envelope(
        cls,
        graph_result: dict[str, Any],
        thread_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        interrupts = [
            cls._interrupt_value(item)
            for item in graph_result.get("__interrupt__", ())
        ]
        result = {
            key: value
            for key, value in graph_result.items()
            if key != "__interrupt__"
        }
        return {
            "status": "awaiting_approval" if interrupts else "completed",
            "thread_id": thread_id,
            "idempotency_key": idempotency_key,
            "idempotency_replayed": False,
            "interrupts": interrupts,
            "result": result,
        }

    @staticmethod
    def _interrupt_value(item: Any) -> dict[str, Any]:
        value = getattr(item, "value", item)
        interrupt_id = getattr(item, "id", None)
        if isinstance(value, dict):
            payload = dict(value)
        else:
            payload = {"value": value}
        if interrupt_id is not None:
            payload["interrupt_id"] = str(interrupt_id)
        return payload

    def _replayed_or_recovered(
        self,
        response: dict[str, Any] | None,
        thread_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if response is None:
            recovered = self.get_session(thread_id)
            if recovered["status"] == "not_found":
                return {
                    "status": "in_progress",
                    "thread_id": thread_id,
                    "idempotency_key": idempotency_key,
                    "idempotency_replayed": True,
                    "interrupts": [],
                    "result": None,
                }
            response = {
                "status": recovered["status"],
                "thread_id": thread_id,
                "idempotency_key": idempotency_key,
                "idempotency_replayed": True,
                "interrupts": recovered["interrupts"],
                "result": recovered["result"],
            }
            if response["status"] in {"completed", "awaiting_approval"}:
                self.execution_store.finish(
                    thread_id=thread_id,
                    idempotency_key=idempotency_key,
                    status=str(response["status"]),
                    response=response,
                )
            return response
        replayed = dict(response)
        replayed["idempotency_replayed"] = True
        return replayed


@lru_cache(maxsize=1)
def get_runtime() -> ScenicPilotRuntime:
    return ScenicPilotRuntime()
