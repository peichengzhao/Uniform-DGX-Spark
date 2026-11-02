from __future__ import annotations

import json
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from uniform_dgxspark.ai_conversation import AIConversationReader
from uniform_dgxspark.config import Settings
from uniform_dgxspark.main import create_app
from uniform_dgxspark.schemas import (
    AIConversationRead,
    AISessionCreate,
    AISessionRead,
)
from uniform_dgxspark.services import ai_terminal as ai_terminal_module
from uniform_dgxspark.services import dsh_native
from uniform_dgxspark.services.dsh_native import DSHNativeSession


class _FakeSession:
    def __init__(self, session_id: str) -> None:
        self.id = session_id
        self.prompts: list[str] = []

    def run(self, prompt: str, *, on_notification=None):
        self.prompts.append(prompt)
        if on_notification is not None:
            on_notification(
                SimpleNamespace(
                    method="session.event",
                    payload={
                        "sessionId": self.id,
                        "event": {
                            "type": "tool/call",
                            "data": {"name": "shell", "input": "printf native"},
                        },
                    },
                )
            )
        return SimpleNamespace(final_response=f"result: {prompt}", finish_reason="completed")


class _ReasoningFakeSession(_FakeSession):
    def run(self, prompt: str, *, on_notification=None):
        self.prompts.append(prompt)
        if on_notification is not None:
            for event in (
                {
                    "type": "assistant/live-chunk",
                    "data": {
                        "turn": 0,
                        "step": 0,
                        "chunk": {"type": "reasoning-delta", "index": 0, "text": "先核对数据"},
                    },
                },
                {
                    "type": "assistant/live-chunk",
                    "data": {
                        "turn": 0,
                        "step": 0,
                        "chunk": {"type": "reasoning-delta", "index": 0, "text": "集版本。"},
                    },
                },
                {
                    "type": "assistant/message",
                    "data": {
                        "turn": 0,
                        "step": 0,
                        "message": {
                            "content": [
                                {"type": "reasoning", "text": "先核对数据集版本。"},
                                {"type": "text", "text": f"result: {prompt}"},
                            ]
                        },
                        "stream": [
                            {
                                "type": "reasoning-chunks",
                                "index": 0,
                                "texts": ["先核对数据", "集版本。"],
                            }
                        ],
                    },
                },
            ):
                on_notification(
                    SimpleNamespace(
                        method="session.event",
                        payload={"sessionId": self.id, "event": event},
                    )
                )
        return SimpleNamespace(final_response=f"result: {prompt}", finish_reason="completed")


class _FakeHarness:
    instances: list[_FakeHarness] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.started: list[str] = []
        self.resumed: list[str] = []
        self.closed = False
        self.session: _FakeSession | None = None
        self.instances.append(self)

    def start_session(self, session_id: str) -> _FakeSession:
        self.started.append(session_id)
        self.session = _FakeSession(session_id)
        return self.session

    def resume_session(self, session_id: str) -> _FakeSession:
        self.resumed.append(session_id)
        self.session = _FakeSession(session_id)
        return self.session

    def close(self) -> None:
        self.closed = True


class _ErrorFakeSession(_FakeSession):
    def run(self, prompt: str, *, on_notification=None):
        self.prompts.append(prompt)
        return SimpleNamespace(
            final_response="",
            finish_reason="error",
            events=[
                {
                    "type": "turn/end",
                    "data": {
                        "reason": {
                            "kind": "error",
                            "error": {
                                "code": "MISSING_CREDENTIAL",
                                "message": "no API key for deepseek-official",
                            },
                        }
                    },
                }
            ],
        )


class _ErrorFakeHarness(_FakeHarness):
    def start_session(self, session_id: str) -> _ErrorFakeSession:
        self.started.append(session_id)
        self.session = _ErrorFakeSession(session_id)
        return self.session


class _ReasoningFakeHarness(_FakeHarness):
    def start_session(self, session_id: str) -> _ReasoningFakeSession:
        self.started.append(session_id)
        self.session = _ReasoningFakeSession(session_id)
        return self.session


def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("timed out waiting for the DSH native session")


def _native(
    workspace: Path,
    *,
    task_type: str = "dataset_integration",
    errors: list[BaseException | None] | None = None,
    harness_factory=None,
) -> DSHNativeSession:
    return DSHNativeSession(
        session_id="native-session-001",
        workspace=workspace,
        model="deepseek-v4-flash",
        reasoning_effort="high",
        execution_mode="interactive",
        task_type=task_type,
        environment={"LANG": "C.UTF-8"},
        capability_token="capability-secret",
        control_url="http://127.0.0.1:18180",
        on_exit=(errors.append if errors is not None else lambda _error: None),
        harness_factory=harness_factory or _FakeHarness,
    )


def test_dsh_native_session_writes_concise_messages_and_secure_patch(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        dsh_native,
        "runtime_status",
        lambda: (True, "/fake/dsh", "deepseek-harness-sdk 0.2.1rc2"),
    )
    _FakeHarness.instances.clear()
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    errors: list[BaseException | None] = []
    native = _native(workspace, errors=errors)

    native.start(prompt="inspect AMC", resume=False)
    conversation = workspace / "dsh-conversation.jsonl"
    _wait_for(lambda: conversation.read_text(encoding="utf-8").count("result: inspect AMC") == 1)

    records = [json.loads(line) for line in conversation.read_text(encoding="utf-8").splitlines()]
    assert [(record["role"], record["text"]) for record in records] == [
        ("user", "inspect AMC"),
        ("assistant", "result: inspect AMC"),
    ]
    harness = _FakeHarness.instances[-1]
    assert harness.kwargs["provider"] == "deepseek-official"
    assert harness.kwargs["model"] == "deepseek-v4-flash"
    assert harness.kwargs["reasoning_effort"] == "high"
    assert harness.kwargs["env"]["DSH_PERMISSION_MODE"] == "read-only"
    assert harness.kwargs["env"]["PKG_NATIVE_CACHE_PATH"] == str(
        workspace / ".dsh-cache" / "pkg-native"
    )
    patch_path = Path(harness.kwargs["patches"][0])
    assert patch_path.name == "dsh-native.patch.yml"
    assert stat.S_IMODE(patch_path.stat().st_mode) == 0o600
    patch = json.loads(patch_path.read_text(encoding="utf-8"))
    assert patch[0] == {"id": "approval", "config": {"policy": "never"}}
    inserted = [item for entry in patch for item in entry.get("insert", [])]
    reasoning_bridge = next(item for item in inserted if item["id"] == "uniform-reasoning-bridge")
    assert reasoning_bridge["name"].endswith("dsh_reasoning_bridge.mjs")
    assert reasoning_bridge["config"] == {
        "path": str(workspace / ".dsh-live-reasoning.jsonl"),
        "sessionId": "native-session-001",
    }
    mcp = next(item for item in inserted if item["id"] == "native-uniform_dataset")
    assert mcp["name"] == "@deepseek-ai/dsh-mcp-client"
    assert mcp["config"]["serverName"] == "uniform_dataset"
    assert mcp["config"]["env"]["UNIFORM_AI_CAPABILITY_TOKEN"] == "capability-secret"
    terminal = (workspace / "terminal.cast").read_text(encoding="utf-8")
    assert "DSH tool/call" in terminal
    assert "result: inspect AMC" in terminal
    conversation_read = AIConversationReader().read(
        AISessionRead(
            id="native-session-001",
            deployment_id=None,
            provider="dsh",
            task_type="dataset_integration",
            display_name="Native session",
            target_model_id=None,
            ai_model="deepseek-v4-flash",
            reasoning_effort="high",
            execution_mode="interactive",
            status="running",
            initial_prompt="inspect AMC",
            reference_urls=[],
            workspace_path=str(workspace),
            pid=None,
            exit_code=None,
            error_message=None,
            archived_at=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    assert conversation_read.source == "dsh_native"
    assert [message.text for message in conversation_read.messages] == [
        "inspect AMC",
        "result: inspect AMC",
    ]

    native.stop()
    native.join()
    assert errors == [None]


def test_dsh_native_session_resumes_the_persisted_harness_session(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        dsh_native,
        "runtime_status",
        lambda: (True, "/fake/dsh", "deepseek-harness-sdk 0.2.1rc2"),
    )
    _FakeHarness.instances.clear()
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)

    first = _native(workspace, task_type="general")
    first.start(prompt="first turn")
    _wait_for(
        lambda: "result: first turn"
        in (workspace / "dsh-conversation.jsonl").read_text(encoding="utf-8")
    )
    first.stop()
    first.join()

    resumed = _native(workspace, task_type="general")
    resumed.start(prompt="second turn", resume=True)
    _wait_for(
        lambda: "result: second turn"
        in (workspace / "dsh-conversation.jsonl").read_text(encoding="utf-8")
    )
    harness = _FakeHarness.instances[-1]
    assert harness.started == []
    assert harness.resumed == ["native-session-001"]
    resumed.stop()
    resumed.join()


def test_dsh_native_reasoning_is_upserted_and_folded_after_the_answer(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        dsh_native,
        "runtime_status",
        lambda: (True, "/fake/dsh", "deepseek-harness-sdk 0.2.1rc2"),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    native = _native(workspace, harness_factory=_ReasoningFakeHarness)

    native.start(prompt="check the dataset", resume=False)
    conversation_path = workspace / "dsh-conversation.jsonl"
    _wait_for(
        lambda: "result: check the dataset" in conversation_path.read_text(encoding="utf-8")
    )

    records = [
        json.loads(line) for line in conversation_path.read_text(encoding="utf-8").splitlines()
    ]
    reasoning = [record for record in records if record.get("id") == "dsh-reasoning-0-0-0"]
    assert reasoning[0]["text"] == "先核对数据"
    assert reasoning[-1]["text"] == "先核对数据集版本。"
    assert reasoning[-1]["phase"] == "reasoning_done"

    conversation = AIConversationReader().read(
        AISessionRead(
            id="native-session-001",
            deployment_id=None,
            provider="dsh",
            task_type="dataset_integration",
            display_name="Native reasoning session",
            target_model_id=None,
            ai_model="deepseek-v4-flash",
            reasoning_effort="high",
            execution_mode="interactive",
            status="running",
            initial_prompt="check the dataset",
            reference_urls=[],
            workspace_path=str(workspace),
            pid=None,
            exit_code=None,
            error_message=None,
            archived_at=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    assert [(message.role, message.phase) for message in conversation.messages] == [
        ("user", None),
        ("assistant", "reasoning_done"),
        ("assistant", None),
    ]
    assert conversation.messages[1].text == "先核对数据集版本。"
    assert conversation.messages[2].text == "result: check the dataset"

    native.stop()
    native.join()


def test_concise_chat_renders_dsh_reasoning_as_a_collapsible_card() -> None:
    web_dir = Path(__file__).parents[1] / "src" / "uniform_dgxspark" / "web"
    app_js = (web_dir / "app.js").read_text(encoding="utf-8")
    app_css = (web_dir / "app.css").read_text(encoding="utf-8")
    assert "function renderAiReasoningMessage" in app_js
    assert "function groupAiReasoningMessages" in app_js
    assert "dsh-reasoning-turn-" in app_js
    assert "data-ai-reasoning" in app_js
    assert "已深度思考" in app_js
    assert ".ai-chat-reasoning.active" in app_css


def test_dsh_reasoning_bridge_updates_live_then_folds_the_card(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    native = _native(workspace)
    native.reasoning_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {"type": "attempt-start", "attemptId": "attempt-1", "turn": 2, "step": 1}
                ),
                json.dumps(
                    {"attemptId": "attempt-1", "turn": 2, "step": 1, "index": 0, "text": "检查"}
                ),
                json.dumps(
                    {"attemptId": "attempt-1", "turn": 2, "step": 1, "index": 0, "text": "依赖。"}
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    native._drain_reasoning_file()

    session = AISessionRead(
        id="native-session-001",
        deployment_id=None,
        provider="dsh",
        task_type="general",
        display_name="Native bridge session",
        target_model_id=None,
        ai_model="deepseek-v4-flash",
        reasoning_effort=None,
        execution_mode="interactive",
        status="running",
        initial_prompt="check dependencies",
        reference_urls=[],
        workspace_path=str(workspace),
        pid=None,
        exit_code=None,
        error_message=None,
        archived_at=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    reader = AIConversationReader()
    live = reader.read(session)
    assert [(message.role, message.phase) for message in live.messages] == [
        ("user", None),
        ("assistant", "reasoning"),
    ]
    assert live.messages[1].text == "检查依赖。"

    native._flush_reasoning(done=True)
    folded = reader.read(session)
    assert [(message.role, message.phase) for message in folded.messages] == [
        ("user", None),
        ("assistant", "reasoning_done"),
    ]


def test_dsh_native_session_surfaces_turn_errors_in_conversation_and_terminal(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        dsh_native,
        "runtime_status",
        lambda: (True, "/fake/dsh", "deepseek-harness-sdk 0.2.1rc2"),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    native = DSHNativeSession(
        session_id="native-error-session",
        workspace=workspace,
        model="deepseek-v4-flash",
        reasoning_effort=None,
        execution_mode="interactive",
        task_type="general",
        environment={},
        capability_token=None,
        control_url="http://127.0.0.1:18180",
        on_exit=lambda _error: None,
        harness_factory=_ErrorFakeHarness,
    )

    native.start(prompt="hello", resume=False)
    _wait_for(
        lambda: "MISSING_CREDENTIAL"
        in (workspace / "dsh-conversation.jsonl").read_text(encoding="utf-8")
    )
    records = [
        json.loads(line)
        for line in (workspace / "dsh-conversation.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert records[-1]["role"] == "assistant"
    assert "MISSING_CREDENTIAL" in records[-1]["text"]
    terminal = (workspace / "terminal.cast").read_text(encoding="utf-8")
    assert "未检测到 DEEPSEEK_API_KEY" in terminal
    assert "no API key for deepseek-official" in terminal
    native.stop()
    native.join()


def test_dsh_provider_is_discoverable_and_native_conversation_source_is_valid(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        ai_terminal_module,
        "dsh_runtime_status",
        lambda: (True, "/opt/deepseek/dsh", "deepseek-harness-sdk 0.2.1rc2"),
    )
    app = create_app(
        Settings(
            data_dir=tmp_path,
            worker_enabled=False,
            codex_binary="/definitely/missing/codex",
        )
    )
    with TestClient(app) as client:
        providers = {item["provider"]: item for item in client.get("/api/v1/ai/providers").json()}
        page = client.get("/").text
    assert providers["dsh"] == {
        "provider": "dsh",
        "available": True,
        "executable": "/opt/deepseek/dsh",
        "version": "deepseek-harness-sdk 0.2.1rc2",
    }
    assert 'value="dsh"' in page
    assert "DSH 原生会话" in page
    conversation = AIConversationRead(
        session_id="native-session-001",
        provider="dsh",
        status="running",
        source="dsh_native",
        available=True,
        messages=[],
    )
    assert conversation.source == "dsh_native"


def test_api_creates_and_stops_a_dsh_native_session(tmp_path: Path, monkeypatch) -> None:
    created_kwargs: dict[str, object] = {}

    class FakeNative:
        def __init__(self, **kwargs) -> None:
            created_kwargs.update(kwargs)
            self.workspace = Path(kwargs["workspace"])
            self.stopped = False

        def start(self, *, prompt: str, resume: bool) -> None:
            created_kwargs["prompt"] = prompt
            created_kwargs["resume"] = resume
            record = {
                "type": "message",
                "id": "native-api-message",
                "role": "assistant",
                "text": "native answer",
                "created_at": datetime.now(UTC).isoformat(),
            }
            (self.workspace / "dsh-conversation.jsonl").write_text(
                json.dumps(record) + "\n",
                encoding="utf-8",
            )

        def stop(self) -> None:
            self.stopped = True

        def send(self, _prompt: str) -> None:
            pass

        def feed_terminal(self, _data: str) -> None:
            pass

    monkeypatch.setattr(
        ai_terminal_module,
        "dsh_runtime_status",
        lambda: (True, "/opt/deepseek/dsh", "deepseek-harness-sdk 0.2.1rc2"),
    )
    monkeypatch.setattr(ai_terminal_module, "DSHNativeSession", FakeNative)
    app = create_app(Settings(data_dir=tmp_path, worker_enabled=False))
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/ai/sessions",
            json={
                "provider": "dsh",
                "initial_prompt": "answer with the native session",
                "ai_model": "deepseek-v4-flash",
                "reasoning_effort": "high",
            },
        )
        assert response.status_code == 201, response.text
        session = response.json()
        assert session["provider"] == "dsh"
        assert session["status"] == "running"
        conversation = client.get(f"/api/v1/ai/sessions/{session['id']}/conversation").json()
        assert conversation["source"] == "dsh_native"
        assert [message["text"] for message in conversation["messages"]] == [
            "answer with the native session",
            "native answer",
        ]
        stopped = client.post(f"/api/v1/ai/sessions/{session['id']}/stop")
        assert stopped.status_code == 200
        assert stopped.json()["status"] == "stopped"

    assert created_kwargs["prompt"] == "answer with the native session"
    assert created_kwargs["resume"] is False
    assert created_kwargs["model"] == "deepseek-v4-flash"
    assert created_kwargs["reasoning_effort"] == "high"


def test_dsh_accepts_reasoning_effort_and_scoped_dataset_and_accuracy_tasks() -> None:
    dataset = AISessionCreate(
        provider="dsh",
        task_type="dataset_integration",
        execution_mode="interactive",
        accuracy_dataset_name="arc",
        reasoning_effort="high",
    )
    assert dataset.provider == "dsh"
    assert dataset.reasoning_effort == "high"

    accuracy = AISessionCreate(
        provider="dsh",
        task_type="accuracy_evaluation",
        accuracy_target_type="deployment",
        accuracy_target_id="deployment-001",
        reasoning_effort="medium",
    )
    assert accuracy.provider == "dsh"
    assert accuracy.accuracy_target_id == "deployment-001"
