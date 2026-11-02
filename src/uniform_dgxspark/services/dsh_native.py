from __future__ import annotations

import importlib.metadata
import json
import queue
import re
import shutil
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

_MAX_TERMINAL_DETAIL = 1_500
_SECRET_VALUE = re.compile(
    r"(?i)((?:api[_-]?key|token|password|secret)\s*[=:]\s*)([^\s,;]+)"
)


def runtime_status() -> tuple[bool, str | None, str | None]:
    """Return native SDK availability, a display path, and the installed version."""
    try:
        version = importlib.metadata.version("deepseek-harness-sdk")
        from deepseek_harness import DeepSeekHarness  # noqa: F401
    except (ImportError, importlib.metadata.PackageNotFoundError, OSError):
        return False, None, None
    try:
        from deepseek_harness_runtime import bundled_runtime_path

        executable = str(bundled_runtime_path())
    except (ImportError, FileNotFoundError, OSError):
        executable = shutil.which("dsh")
    if executable is None:
        return False, None, f"deepseek-harness-sdk {version}"
    return True, executable, f"deepseek-harness-sdk {version}"


def _redact(value: str) -> str:
    return _SECRET_VALUE.sub(r"\1<redacted>", value)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _text_from_blocks(blocks: Any) -> str:
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _reasoning_from_blocks(blocks: Any) -> list[tuple[int, str]]:
    if not isinstance(blocks, list):
        return []
    parts: list[tuple[int, str]] = []
    for index, block in enumerate(blocks):
        if not isinstance(block, dict) or block.get("type") not in {"reasoning", "thinking"}:
            continue
        text = block.get("text")
        if not isinstance(text, str):
            text = block.get("thinking")
        if isinstance(text, str) and text.strip():
            parts.append((index, text))
    return parts


class DSHNativeUnavailable(RuntimeError):
    pass


class DSHNativeSession:
    """Run one durable DeepSeek Harness Session through the official Python SDK."""

    def __init__(
        self,
        *,
        session_id: str,
        workspace: Path,
        model: str | None,
        reasoning_effort: str | None,
        execution_mode: str,
        task_type: str,
        environment: dict[str, str],
        capability_token: str | None,
        control_url: str,
        on_exit: Callable[[BaseException | None], None],
        harness_factory: Callable[..., Any] | None = None,
    ) -> None:
        available, _, version = runtime_status()
        if not available:
            raise DSHNativeUnavailable(
                "DeepSeek Harness Python SDK is not installed; install deepseek-harness-sdk"
            )
        self.session_id = session_id
        self.workspace = workspace.resolve()
        self.model = model or "deepseek-v4-flash"
        self.reasoning_effort = reasoning_effort
        self.execution_mode = execution_mode
        self.task_type = task_type
        self.environment = environment
        self.capability_token = capability_token
        self.control_url = control_url.rstrip("/")
        self.version = version
        self.on_exit = on_exit
        self.harness_factory = harness_factory or self._default_harness_factory
        self.dsh_session_id = session_id
        self.dsh_home = self.workspace / ".dsh-home"
        self.conversation_path = self.workspace / "dsh-conversation.jsonl"
        self.reasoning_path = self.workspace / ".dsh-live-reasoning.jsonl"
        self.transcript_path = self.workspace / "terminal.cast"
        self.state_path = self.workspace / "dsh-native-state.json"
        self._incoming: queue.Queue[str] = queue.Queue()
        self._stopped = threading.Event()
        self._ready = threading.Event()
        self._ready_error: BaseException | None = None
        self._harness: Any = None
        self._session: Any = None
        self._thread: threading.Thread | None = None
        self._reasoning_thread: threading.Thread | None = None
        self._write_lock = threading.RLock()
        self._reasoning_lock = threading.RLock()
        self._reasoning_file_lock = threading.Lock()
        self._reasoning_offset = 0
        self._reasoning_buffer = b""
        self._terminal_input = ""
        self._terminal_lock = threading.Lock()
        self._reasoning: dict[str, dict[str, Any]] = {}
        self._reasoning_last_write: dict[str, float] = {}
        self._reasoning_written: dict[str, tuple[str, str]] = {}
        self._active_reasoning_ids: set[str] = set()

    def _default_harness_factory(self, **kwargs: Any) -> Any:
        from deepseek_harness import DeepSeekHarness

        return DeepSeekHarness(**kwargs)

    def start(self, *, prompt: str = "", resume: bool = False) -> None:
        """Start the runtime and wait only for its handshake, not model completion."""
        if not resume and self.state_path.exists():
            self.dsh_session_id = str(uuid4())
        with self._write_lock:
            self.dsh_home.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.conversation_path.touch(mode=0o600, exist_ok=True)
            self.reasoning_path.write_text("", encoding="utf-8")
            self.reasoning_path.chmod(0o600)
            self.transcript_path.touch(mode=0o600, exist_ok=True)
            self._append_terminal(
                f"[DSH] 原生会话已启动 · {self.version or 'DeepSeek Harness'}\r\n"
            )
        self._reasoning_thread = threading.Thread(
            target=self._tail_reasoning,
            name=f"dsh-reasoning-{self.session_id[:8]}",
            daemon=True,
        )
        self._reasoning_thread.start()
        self._thread = threading.Thread(
            target=self._run,
            args=(prompt.strip(), resume),
            name=f"dsh-native-{self.session_id[:8]}",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=45):
            self.stop()
            raise TimeoutError("DeepSeek Harness native session initialization timed out")
        if self._ready_error is not None:
            raise DSHNativeUnavailable(str(self._ready_error))

    def send(self, prompt: str) -> None:
        normalized = prompt.strip()
        if not normalized:
            raise ValueError("instruction must not be empty")
        if not self._thread or not self._thread.is_alive():
            raise RuntimeError("DeepSeek Harness native session is not running")
        self._incoming.put(normalized)

    def feed_terminal(self, data: str) -> None:
        """Translate plain terminal lines into native prompts."""
        with self._terminal_lock:
            self._terminal_input += data.replace("\r\n", "\n")
            while True:
                positions = [
                    position
                    for marker in ("\r", "\n")
                    if (position := self._terminal_input.find(marker)) >= 0
                ]
                if not positions:
                    break
                index = min(positions)
                line = self._terminal_input[:index]
                self._terminal_input = self._terminal_input[index + 1 :]
                if line.strip():
                    self._incoming.put(line.strip())

    def stop(self) -> None:
        self._stopped.set()
        harness = self._harness
        if harness is not None:
            try:
                harness.close()
            except Exception:
                pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=10)
        if (
            self._reasoning_thread is not None
            and self._reasoning_thread is not threading.current_thread()
        ):
            self._reasoning_thread.join(timeout=5)

    def join(self, timeout: float = 10) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self, initial_prompt: str, resume: bool) -> None:
        error: BaseException | None = None
        try:
            env = dict(self.environment)
            if self.execution_mode == "full_access":
                permission_mode = "danger-full-access"
            elif self.task_type in {"dataset_integration", "accuracy_evaluation"}:
                permission_mode = "read-only"
            else:
                permission_mode = "workspace-write"
            env["DSH_PERMISSION_MODE"] = permission_mode
            env.setdefault("DSH_RESOURCE_CACHE", str(self.workspace / ".dsh-cache" / "resources"))
            env["PKG_NATIVE_CACHE_PATH"] = str(self.workspace / ".dsh-cache" / "pkg-native")
            if not env.get("DEEPSEEK_API_KEY", "").strip():
                self._append_terminal(
                    "\r\n[DSH] 未检测到 DEEPSEEK_API_KEY；模型回复会失败。"
                    "请为平台服务配置该凭证。\r\n"
                )
            patches = self._write_patch()
            harness = self.harness_factory(
                dsh_home=str(self.dsh_home),
                cwd=str(self.workspace),
                runtime_cwd=str(self.workspace),
                profile="sdk",
                provider="deepseek-official",
                model=self.model,
                reasoning_effort=self.reasoning_effort,
                patches=(str(patches),) if patches else (),
                env=env,
            )
            self._harness = harness
            session = self._open_session(harness, resume)
            self._session = session
            self._persist_state()
            self._ready.set()
            if initial_prompt:
                self._run_prompt(initial_prompt)
            while not self._stopped.is_set():
                try:
                    prompt = self._incoming.get(timeout=0.25)
                except queue.Empty:
                    continue
                if self._stopped.is_set():
                    break
                self._run_prompt(prompt)
        except BaseException as caught:
            error = caught
            if not self._ready.is_set():
                self._ready_error = caught
                self._ready.set()
            else:
                self._append_terminal(f"\r\n[DSH 错误] {_redact(str(caught))}\r\n")
        finally:
            self._stopped.set()
            self._ready.set()
            harness = self._harness
            self._harness = None
            if harness is not None:
                try:
                    harness.close()
                except Exception:
                    pass
            self.on_exit(error)

    def _open_session(self, harness: Any, resume: bool) -> Any:
        if resume:
            try:
                return harness.resume_session(self.dsh_session_id)
            except Exception as error:
                self._append_terminal(
                    f"\r\n[DSH] 无法恢复原会话，已新建会话：{_redact(str(error))}\r\n"
                )
        return harness.start_session(self.dsh_session_id)

    def _run_prompt(self, prompt: str) -> None:
        normalized = prompt.strip()
        if not normalized:
            return
        with self._reasoning_lock:
            self._active_reasoning_ids = set()
        self._write_message("user", normalized)
        self._append_terminal(f"\r\n\x1b[1m你 ›\x1b[0m {normalized}\r\n")
        try:
            result = self._session.run(normalized, on_notification=self._on_notification)
        except BaseException as error:
            self._drain_reasoning_file()
            self._flush_reasoning(done=True)
            self._append_terminal(f"\r\n[DSH 错误] {_redact(str(error))}\r\n")
            raise
        self._drain_reasoning_file()
        self._ingest_result_events(getattr(result, "events", None))
        response = str(getattr(result, "final_response", "") or "").strip()
        self._flush_reasoning(done=True)
        if response:
            self._write_message("assistant", response)
            self._append_terminal(f"\r\n\x1b[1mDSH ›\x1b[0m {response}\r\n")
        finish_reason = getattr(result, "finish_reason", None)
        if finish_reason and finish_reason != "completed":
            detail = self._result_error(result)
            message = f"本轮结束：{finish_reason}"
            if detail:
                message += f" · {detail}"
            self._append_terminal(f"\r\n[DSH 错误] {_redact(message)}\r\n")
            if finish_reason == "error" and not response:
                self._write_message("assistant", f"运行失败：{_redact(detail or message)}")

    @staticmethod
    def _result_error(result: Any) -> str:
        events = getattr(result, "events", None)
        if not isinstance(events, list):
            return ""
        for event in reversed(events):
            if not isinstance(event, dict) or event.get("type") != "turn/end":
                continue
            data = event.get("data")
            reason = data.get("reason") if isinstance(data, dict) else None
            error = reason.get("error") if isinstance(reason, dict) else None
            if not isinstance(error, dict):
                return ""
            text = error.get("message")
            code = error.get("code")
            if isinstance(text, str) and text.strip():
                return f"{code}: {text.strip()}" if code else text.strip()
            return str(code) if code else ""
        return ""

    def _on_notification(self, notification: Any) -> None:
        if getattr(notification, "method", "") != "session.event":
            return
        payload = getattr(notification, "payload", {})
        event = payload.get("event") if isinstance(payload, dict) else None
        if not isinstance(event, dict) or event.get("sessionId") not in {None, self.dsh_session_id}:
            return
        event_type = event.get("type")
        if not isinstance(event_type, str):
            return
        if event_type in {
            "assistant/attempt",
            "assistant/chunk",
            "assistant/live",
            "assistant/live-chunk",
            "assistant/message",
        }:
            self._consume_assistant_event(event_type, event.get("data"))
            return
        if event_type not in {
            "tool/call",
            "tool/result",
            "tool/error",
            "command/run",
            "command/done",
        }:
            return
        data = event.get("data")
        detail = self._notification_detail(data)
        if detail:
            self._append_terminal(f"\r\n[DSH {event_type}] {detail}\r\n")

    def _consume_assistant_event(self, event_type: str, data: Any) -> None:
        if not isinstance(data, dict):
            return
        if event_type in {"assistant/chunk", "assistant/live-chunk"}:
            self._consume_stream_chunk(data, data.get("chunk"))
            self._flush_reasoning()
            return
        if event_type == "assistant/live":
            chunks = data.get("chunks")
            if isinstance(chunks, list):
                for chunk in chunks:
                    self._consume_stream_chunk(data, chunk)
                self._flush_reasoning()
            return
        self._ingest_stream(data, data.get("stream"))
        message = data.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            for index, text in _reasoning_from_blocks(content):
                self._set_reasoning(data, index, text)
            self._flush_reasoning()
            return
        self._flush_reasoning()

    def _ingest_result_events(self, events: Any) -> None:
        if not isinstance(events, list):
            return
        for event in events:
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            if event_type in {
                "assistant/attempt",
                "assistant/chunk",
                "assistant/live",
                "assistant/live-chunk",
                "assistant/message",
            }:
                self._consume_assistant_event(event_type, event.get("data"))

    def _consume_stream_chunk(self, data: dict[str, Any], chunk: Any) -> None:
        if not isinstance(chunk, dict) or chunk.get("type") != "reasoning-delta":
            return
        text = chunk.get("text")
        index = chunk.get("index")
        if isinstance(text, str) and text and isinstance(index, int):
            self._append_reasoning(data, index, text)

    def _ingest_stream(self, data: dict[str, Any], stream: Any) -> None:
        if not isinstance(stream, list):
            return
        fragments: dict[int, list[str]] = {}
        for record in stream:
            if not isinstance(record, dict):
                continue
            record_type = record.get("type")
            if record_type == "chunk":
                chunk = record.get("chunk")
                if not isinstance(chunk, dict) or chunk.get("type") != "reasoning-delta":
                    continue
                index = chunk.get("index")
                text = chunk.get("text")
            elif record_type == "reasoning-chunks":
                index = record.get("index")
                texts = record.get("texts")
                text = (
                    "".join(item for item in texts if isinstance(item, str))
                    if isinstance(texts, list)
                    else None
                )
            else:
                continue
            if isinstance(index, int) and isinstance(text, str):
                fragments.setdefault(index, []).append(text)
        for index, parts in fragments.items():
            self._set_reasoning(data, index, "".join(parts))

    def _append_reasoning(self, data: dict[str, Any], index: int, text: str) -> None:
        with self._reasoning_lock:
            key = self._reasoning_id(data, index)
            state = self._reasoning.setdefault(
                key,
                {
                    "text": "",
                    "created_at": _timestamp(),
                    "phase": "reasoning",
                },
            )
            state["text"] = f"{state['text']}{text}"
            state["phase"] = "reasoning"
            self._active_reasoning_ids.add(key)

    def _set_reasoning(self, data: dict[str, Any], index: int, text: str) -> None:
        if not text.strip():
            return
        with self._reasoning_lock:
            key = self._reasoning_id(data, index)
            state = self._reasoning.setdefault(
                key,
                {
                    "text": "",
                    "created_at": _timestamp(),
                    "phase": "reasoning",
                },
            )
            if len(text) >= len(state["text"]):
                state["text"] = text
            state["phase"] = "reasoning"
            self._active_reasoning_ids.add(key)

    @staticmethod
    def _reasoning_coordinate(data: dict[str, Any]) -> str:
        turn = re.sub(r"[^0-9A-Za-z_.-]+", "-", str(data.get("turn", "0")))[:24] or "0"
        step = re.sub(r"[^0-9A-Za-z_.-]+", "-", str(data.get("step", "0")))[:24] or "0"
        return f"{turn}-{step}"

    @staticmethod
    def _reasoning_id(data: dict[str, Any], index: int) -> str:
        return f"dsh-reasoning-{DSHNativeSession._reasoning_coordinate(data)}-{index}"

    def _flush_reasoning(self, *, done: bool = False) -> None:
        with self._reasoning_lock:
            now = time.monotonic()
            for key in list(self._active_reasoning_ids):
                state = self._reasoning.get(key)
                if state is None:
                    continue
                text = str(state.get("text") or "").strip()
                if not text:
                    continue
                phase = "reasoning_done" if done else "reasoning"
                state["phase"] = phase
                if self._reasoning_written.get(key) == (text, phase):
                    continue
                last_write = self._reasoning_last_write.get(key, 0.0)
                if not done and last_write and now - last_write < 0.35:
                    continue
                self._write_message(
                    "assistant",
                    text,
                    message_id=key,
                    phase=phase,
                    created_at=str(state["created_at"]),
                )
                self._reasoning_last_write[key] = now
                self._reasoning_written[key] = (text, phase)

    def _tail_reasoning(self) -> None:
        while not self._stopped.wait(0.08):
            self._drain_reasoning_file()
        self._drain_reasoning_file()

    def _drain_reasoning_file(self) -> None:
        with self._reasoning_file_lock:
            try:
                with self.reasoning_path.open("rb") as stream:
                    stream.seek(self._reasoning_offset)
                    chunk = stream.read()
            except OSError:
                return
            if not chunk:
                return
            payload = self._reasoning_buffer + chunk
            boundary = payload.rfind(b"\n")
            if boundary < 0:
                self._reasoning_buffer = payload
                return
            complete = payload[: boundary + 1]
            self._reasoning_buffer = payload[boundary + 1 :]
            self._reasoning_offset += len(chunk)
        for raw_line in complete.splitlines():
            try:
                record = json.loads(raw_line)
            except (TypeError, ValueError):
                continue
            if not isinstance(record, dict):
                continue
            if record.get("type") == "attempt-start":
                self._reset_reasoning(record)
                continue
            text = record.get("text")
            index = record.get("index")
            if isinstance(text, str) and isinstance(index, int):
                self._append_reasoning(record, index, text)
        self._flush_reasoning()

    def _reset_reasoning(self, data: dict[str, Any]) -> None:
        prefix = f"dsh-reasoning-{self._reasoning_coordinate(data)}-"
        with self._reasoning_lock:
            for key in [key for key in self._reasoning if key.startswith(prefix)]:
                self._reasoning.pop(key, None)
                self._reasoning_last_write.pop(key, None)
                self._reasoning_written.pop(key, None)
                self._active_reasoning_ids.discard(key)

    @staticmethod
    def _notification_detail(data: Any) -> str:
        if not isinstance(data, dict):
            return ""
        for key in ("name", "toolName", "command", "input", "output", "error"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                text = value.strip().replace("\n", " ")
                return _redact(text[:_MAX_TERMINAL_DETAIL])
        return ""

    def _write_message(
        self,
        role: str,
        text: str,
        *,
        message_id: str | None = None,
        phase: str | None = None,
        created_at: str | None = None,
    ) -> None:
        record = {
            "type": "message",
            "id": message_id or str(uuid4()),
            "role": role,
            "text": text,
            "created_at": created_at or _timestamp(),
        }
        if phase is not None:
            record["phase"] = phase
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._write_lock:
            with self.conversation_path.open("a", encoding="utf-8") as stream:
                stream.write(line)
                stream.flush()

    def _append_terminal(self, text: str) -> None:
        with self._write_lock:
            with self.transcript_path.open("ab", buffering=0) as stream:
                stream.write(text.encode("utf-8", errors="replace"))

    def _write_patch(self) -> Path | None:
        entries: list[dict[str, Any]] = [
            {"id": "approval", "config": {"policy": "never"}},
        ]
        reasoning_bridge = Path(__file__).with_name("dsh_reasoning_bridge.mjs").resolve()
        if reasoning_bridge.is_file():
            entries.append(
                {
                    "insert": [
                        {
                            "id": "uniform-reasoning-bridge",
                            "name": str(reasoning_bridge),
                            "config": {
                                "path": str(self.reasoning_path),
                                "sessionId": self.dsh_session_id,
                            },
                        }
                    ]
                }
            )
        if self.task_type in {"dataset_integration", "accuracy_evaluation"}:
            module = (
                "uniform_dgxspark.dataset_integration_mcp"
                if self.task_type == "dataset_integration"
                else "uniform_dgxspark.accuracy_mcp"
            )
            server_name = (
                "uniform_dataset"
                if self.task_type == "dataset_integration"
                else "uniform_accuracy"
            )
            env = {
                "UNIFORM_CONTROL_URL": self.control_url,
                "UNIFORM_AI_SESSION_ID": self.session_id,
                "UNIFORM_AI_CAPABILITY_TOKEN": self.capability_token or "",
            }
            entries.append(
                {
                    "insert": [
                        {
                            "id": f"native-{server_name}",
                            "name": "@deepseek-ai/dsh-mcp-client",
                            "config": {
                                "serverName": server_name,
                                "transport": "stdio",
                                "command": str(Path(sys.executable)),
                                "args": ["-m", module],
                                "env": env,
                                "toolCallTimeoutMs": 120_000,
                                "failOnStartupError": True,
                                "reconnect": {
                                    "enabled": True,
                                    "maxAttempts": 10,
                                },
                            },
                        }
                    ]
                }
            )
        path = self.workspace / "dsh-native.patch.yml"
        path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        path.chmod(0o600)
        return path

    def _persist_state(self) -> None:
        payload = {
            "dsh_session_id": self.dsh_session_id,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "updated_at": _timestamp(),
        }
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.state_path)
