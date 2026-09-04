"""Synchronous stdio client for the local Codex app-server."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import IO, Protocol, cast

from code_mule.progress import (
    ProgressEvent,
    ProgressEventType,
    ProgressSink,
    resilient_progress_sink,
)

from .contracts import (
    CodexAppServerStartError,
    CodexApprovalRequired,
    CodexProtocolError,
    CodexTurnFailed,
    CodexTurnHardTimeout,
    CodexTurnInactivityTimeout,
    CodexTurnTimeout,
    CodexUserInputRequired,
    CodexWorkerConfig,
    WorkerTurnResult,
)
from .protocol import (
    APPROVAL_REQUEST_METHODS,
    INITIALIZED_METHOD,
    INITIALIZE_METHOD,
    ITEM_COMPLETED_METHOD,
    MessageKind,
    THREAD_START_METHOD,
    TURN_COMPLETED_METHOD,
    TURN_START_METHOD,
    USER_INPUT_REQUEST_METHODS,
    classify_message,
    notification_message,
    parse_worker_input_request,
    request_message,
    response_result,
)


class _Process(Protocol):
    stdin: IO[str] | None
    stdout: IO[str] | None
    stderr: IO[str] | None

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


_PopenFactory = Callable[..., _Process]
_EOF = object()


def _worker_environment() -> dict[str, str]:
    """Keep the local runtime usable without exposing parent-process secrets."""

    secret_segments = {
        "KEY",
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "CREDENTIAL",
        "CREDENTIALS",
    }
    return {
        key: value
        for key, value in os.environ.items()
        if secret_segments.isdisjoint(key.upper().split("_"))
    }


class CodexAppServerClient:
    """One local app-server process with one request in flight at a time."""

    def __init__(
        self,
        config: CodexWorkerConfig,
        *,
        popen_factory: _PopenFactory = subprocess.Popen,
        progress_sink: ProgressSink | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._popen_factory = popen_factory
        self._process: _Process | None = None
        self._stdout_queue: queue.Queue[object] = queue.Queue()
        self._pending_messages: deque[dict[str, object]] = deque()
        self._stderr_lines: deque[str] = deque(maxlen=100)
        self._reader_threads: list[threading.Thread] = []
        self._next_request_id = 1
        self._initialized = False
        self._progress = resilient_progress_sink(progress_sink)
        self._clock = clock
        self._monotonic = monotonic

    @property
    def stderr_tail(self) -> tuple[str, ...]:
        return tuple(self._stderr_lines)

    @property
    def progress_errors(self) -> tuple[BaseException, ...]:
        return self._progress.errors

    def start(self) -> None:
        if self._process is not None:
            return
        try:
            process = self._popen_factory(
                self._config.command,
                cwd=str(self._config.workspace),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
                shell=False,
                env=_worker_environment(),
            )
        except OSError as error:
            raise CodexAppServerStartError(
                "unable to start the local Codex app-server"
            ) from error
        if process.stdin is None or process.stdout is None or process.stderr is None:
            process.terminate()
            raise CodexAppServerStartError(
                "Codex app-server did not expose all required stdio pipes"
            )
        self._process = process
        stdout_thread = threading.Thread(
            target=self._read_stdout,
            args=(process.stdout,),
            name="code-mule-codex-stdout",
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._read_stderr,
            args=(process.stderr,),
            name="code-mule-codex-stderr",
            daemon=True,
        )
        self._reader_threads = [stdout_thread, stderr_thread]
        stdout_thread.start()
        stderr_thread.start()

    def initialize(self) -> None:
        self.start()
        result = self._request(
            INITIALIZE_METHOD,
            {
                "clientInfo": {
                    "name": "code-mule",
                    "title": "Code Mule",
                    "version": "0.1.0",
                }
            },
        )
        if not isinstance(result, dict):
            raise CodexProtocolError("initialize result must be an object")
        self._write_message(notification_message(INITIALIZED_METHOD))
        self._initialized = True

    def start_thread(self) -> str:
        self._require_initialized()
        result = self._request(
            THREAD_START_METHOD,
            {
                "cwd": str(self._config.workspace),
                "approvalPolicy": self._config.approval_policy,
                "sandbox": self._config.sandbox,
            },
        )
        thread = result.get("thread")
        if not isinstance(thread, dict):
            raise CodexProtocolError("thread/start result is missing thread")
        thread_id = thread.get("id")
        if not isinstance(thread_id, str) or thread_id == "":
            raise CodexProtocolError("thread/start result is missing thread.id")
        return thread_id

    def start_turn(
        self,
        thread_id: str,
        prompt: str,
        *,
        output_schema: dict[str, object] | None = None,
    ) -> str:
        self._require_initialized()
        if thread_id == "":
            raise ValueError("thread_id must not be empty")
        if prompt == "":
            raise ValueError("prompt must not be empty")
        params: dict[str, object] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
        }
        if output_schema is not None:
            params["outputSchema"] = output_schema
        result = self._request(TURN_START_METHOD, params)
        turn = result.get("turn")
        if not isinstance(turn, dict):
            raise CodexProtocolError("turn/start result is missing turn")
        turn_id = turn.get("id")
        if not isinstance(turn_id, str) or turn_id == "":
            raise CodexProtocolError("turn/start result is missing turn.id")
        return turn_id

    def wait_for_turn(self, thread_id: str, turn_id: str) -> WorkerTurnResult:
        if thread_id == "" or turn_id == "":
            raise ValueError("thread_id and turn_id must not be empty")
        started = self._monotonic()
        hard_deadline = started + self._config.max_turn_seconds
        inactivity_deadline = started + self._config.inactivity_timeout_seconds
        event_count = 0
        final_message: str | None = None
        issues: list[str] = []

        while True:
            try:
                message = self._next_turn_message(
                    hard_deadline, inactivity_deadline
                )
            except CodexTurnTimeout:
                self.close()
                raise
            kind = classify_message(message)
            if kind is MessageKind.RESPONSE:
                raise CodexProtocolError("unexpected response while waiting for turn")
            if kind is MessageKind.SERVER_REQUEST:
                event_count += 1
                self._raise_server_request(message)

            method = cast(str, message["method"])
            params = self._params(message, method)
            matches_thread = params.get("threadId") == thread_id
            matches_turn = params.get("turnId") == turn_id
            turn = params.get("turn")
            if isinstance(turn, dict) and turn.get("id") == turn_id:
                matches_turn = True

            if self._is_trusted_turn_activity(
                method, params, thread_id, turn_id
            ):
                inactivity_deadline = (
                    self._monotonic()
                    + self._config.inactivity_timeout_seconds
                )

            if matches_thread and matches_turn:
                self._project_activity(method, params)

            if method == ITEM_COMPLETED_METHOD:
                self._require_event_identity(params, method)
                if matches_thread and matches_turn:
                    event_count += 1
                    item = params.get("item")
                    if not isinstance(item, dict):
                        raise CodexProtocolError("item/completed is missing item")
                    if item.get("type") == "agentMessage":
                        text = item.get("text")
                        if not isinstance(text, str):
                            raise CodexProtocolError(
                                "completed agentMessage is missing text"
                            )
                        final_message = text
                continue

            if method == "error":
                self._require_event_identity(params, method)
                if matches_thread and matches_turn:
                    event_count += 1
                    issue = self._turn_error_message(params.get("error"))
                    issues.append(issue)
                    if params.get("willRetry") is not True:
                        raise CodexTurnFailed(issue)
                continue

            if method == TURN_COMPLETED_METHOD:
                if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                    raise CodexProtocolError("turn/completed is missing turn.id")
                if not isinstance(params.get("threadId"), str):
                    raise CodexProtocolError("turn/completed is missing threadId")
                if matches_thread and matches_turn:
                    event_count += 1
                    status = turn.get("status")
                    if status == "completed":
                        return WorkerTurnResult(
                            thread_id=thread_id,
                            turn_id=turn_id,
                            final_message=final_message,
                            completed=True,
                            event_count=event_count,
                            issues=tuple(issues),
                        )
                    if status in {"failed", "interrupted"}:
                        raise CodexTurnFailed(
                            self._turn_error_message(turn.get("error"), status)
                        )
                    raise CodexProtocolError(
                        f"turn/completed has invalid status {status!r}"
                    )
                continue

            if matches_thread and matches_turn:
                event_count += 1

    def _project_activity(
        self, method: str, params: dict[str, object]
    ) -> None:
        if self._clock is None:
            return
        message: str | None = None
        metadata: dict[str, str] = {}
        if method == "turn/started":
            message = "Codex turn started"
            metadata = {"activity": "turn.started"}
        elif method == "turn/completed":
            message = "Codex turn completed"
            metadata = {"activity": "turn.completed"}
        elif method in {"item/started", "item/completed"}:
            item = params.get("item")
            if not isinstance(item, dict):
                return
            item_type = item.get("type")
            phase = "started" if method == "item/started" else "completed"
            if item_type == "commandExecution":
                message = (
                    self._safe_command_message(item)
                    if phase == "started"
                    else "Command completed"
                )
                metadata = {"activity": f"command_execution.{phase}"}
            elif item_type == "fileChange":
                safe_path = self._safe_file_path(item)
                message = (
                    f"Editing {safe_path}"
                    if safe_path is not None and phase == "started"
                    else (
                        f"Updated {safe_path}"
                        if safe_path is not None
                        else "Updating file"
                    )
                )
                metadata = {"activity": f"file_change.{phase}"}
                if safe_path is not None:
                    metadata["path"] = safe_path
            elif item_type == "agentMessage":
                message = f"Codex response {phase}"
                metadata = {"activity": f"agent_message.{phase}"}
        if message is None:
            return
        self._progress.emit(
            ProgressEvent(
                type=ProgressEventType.WORKER_ACTIVITY,
                timestamp=self._clock(),
                project_id=None,
                task_id=None,
                attempt=None,
                message=message,
                metadata=metadata,
            )
        )

    @staticmethod
    def _safe_command_message(item: dict[str, object]) -> str:
        actions = item.get("commandActions")
        if isinstance(actions, list) and actions:
            action_types = {
                action.get("type")
                for action in actions
                if isinstance(action, dict)
            }
            if action_types and action_types <= {"read", "listFiles", "search"}:
                return "Inspecting repository"
        return "Running command"

    def _safe_file_path(self, item: dict[str, object]) -> str | None:
        changes = item.get("changes")
        if not isinstance(changes, list) or not changes:
            return None
        first = changes[0]
        if not isinstance(first, dict):
            return None
        raw_path = first.get("path")
        if not isinstance(raw_path, str) or raw_path == "":
            return None
        workspace = self._config.workspace.resolve()
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = workspace / candidate
        try:
            return candidate.resolve().relative_to(workspace).as_posix()
        except (OSError, ValueError):
            return None

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        self._process = None
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        for thread in self._reader_threads:
            thread.join(timeout=1.0)
        self._reader_threads.clear()
        self._initialized = False

    def __enter__(self) -> CodexAppServerClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        request_id = self._next_request_id
        self._next_request_id += 1
        self._write_message(request_message(request_id, method, params))
        deadline = self._monotonic() + self._config.inactivity_timeout_seconds
        while True:
            try:
                message = self._read_new_message(deadline)
            except CodexTurnTimeout:
                self.close()
                raise
            kind = classify_message(message)
            if kind is MessageKind.RESPONSE:
                return response_result(message, request_id)
            if kind is MessageKind.SERVER_REQUEST:
                self._raise_server_request(message)
            self._pending_messages.append(message)

    def _write_message(self, message: dict[str, object]) -> None:
        process = self._require_process()
        assert process.stdin is not None
        try:
            process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise CodexProtocolError("Codex app-server stdin is unavailable") from error

    def _next_message(self, deadline: float) -> dict[str, object]:
        if self._pending_messages:
            return self._pending_messages.popleft()
        return self._read_new_message(deadline)

    def _next_turn_message(
        self, hard_deadline: float, inactivity_deadline: float
    ) -> dict[str, object]:
        try:
            return self._next_message(min(hard_deadline, inactivity_deadline))
        except CodexTurnTimeout as error:
            if hard_deadline <= inactivity_deadline:
                raise CodexTurnHardTimeout(
                    "Codex turn reached its hard timeout"
                ) from error
            raise CodexTurnInactivityTimeout(
                "Codex turn reached its inactivity timeout"
            ) from error

    def _read_new_message(self, deadline: float) -> dict[str, object]:
        timeout = deadline - self._monotonic()
        if timeout <= 0:
            raise CodexTurnTimeout("timed out waiting for Codex app-server")
        try:
            item = self._stdout_queue.get(timeout=timeout)
        except queue.Empty as error:
            raise CodexTurnTimeout("timed out waiting for Codex app-server") from error
        if item is _EOF:
            process = self._process
            exit_code = process.poll() if process is not None else None
            raise CodexProtocolError(
                f"Codex app-server stdout closed unexpectedly (exit {exit_code!r})"
            )
        if isinstance(item, BaseException):
            raise CodexProtocolError("unable to read Codex app-server stdout") from item
        try:
            payload = json.loads(cast(str, item))
        except json.JSONDecodeError as error:
            raise CodexProtocolError("Codex app-server emitted malformed JSON") from error
        if not isinstance(payload, dict) or not all(
            isinstance(key, str) for key in payload
        ):
            raise CodexProtocolError("Codex app-server JSON must be an object")
        return cast(dict[str, object], payload)

    def _is_trusted_turn_activity(
        self,
        method: str,
        params: dict[str, object],
        thread_id: str,
        turn_id: str,
    ) -> bool:
        if method == "turn/started":
            turn = params.get("turn")
            if not isinstance(params.get("threadId"), str) or not isinstance(
                turn, dict
            ):
                raise CodexProtocolError("turn/started has invalid identity")
            identity = turn.get("id")
            if not isinstance(identity, str):
                raise CodexProtocolError("turn/started has invalid identity")
            return params["threadId"] == thread_id and identity == turn_id
        if method in {"item/started", "item/completed"}:
            self._require_event_identity(params, method)
            if not isinstance(params.get("item"), dict):
                raise CodexProtocolError(f"{method} is missing item")
            return (
                params["threadId"] == thread_id
                and params["turnId"] == turn_id
            )
        if method == "error":
            self._require_event_identity(params, method)
            return (
                params["threadId"] == thread_id
                and params["turnId"] == turn_id
                and params.get("willRetry") is True
            )
        return False

    def _read_stdout(self, stream: IO[str]) -> None:
        try:
            while True:
                line = stream.readline()
                if line == "":
                    self._stdout_queue.put(_EOF)
                    return
                self._stdout_queue.put(line)
        except BaseException as error:
            self._stdout_queue.put(error)

    def _read_stderr(self, stream: IO[str]) -> None:
        try:
            for line in stream:
                self._stderr_lines.append(line.rstrip("\r\n"))
        except (OSError, ValueError):
            return

    def _raise_server_request(self, message: dict[str, object]) -> None:
        method = cast(str, message["method"])
        if method in APPROVAL_REQUEST_METHODS:
            raise CodexApprovalRequired(
                f"Codex app-server requested approval via {method}"
            )
        if method in USER_INPUT_REQUEST_METHODS:
            raise CodexUserInputRequired(parse_worker_input_request(message))
        raise CodexProtocolError(f"unsupported app-server request method {method!r}")

    @staticmethod
    def _params(message: dict[str, object], method: str) -> dict[str, object]:
        params = message.get("params")
        if not isinstance(params, dict) or not all(
            isinstance(key, str) for key in params
        ):
            raise CodexProtocolError(f"{method} params must be an object")
        return cast(dict[str, object], params)

    @staticmethod
    def _require_event_identity(params: dict[str, object], method: str) -> None:
        if not isinstance(params.get("threadId"), str):
            raise CodexProtocolError(f"{method} is missing threadId")
        if not isinstance(params.get("turnId"), str):
            raise CodexProtocolError(f"{method} is missing turnId")

    @staticmethod
    def _turn_error_message(error: object, fallback: str = "Codex turn failed") -> str:
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                return message
        return fallback

    def _require_process(self) -> _Process:
        if self._process is None:
            raise CodexAppServerStartError("Codex app-server is not running")
        return self._process

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise CodexProtocolError("Codex app-server is not initialized")


__all__ = ["CodexAppServerClient"]
