from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from uniform_dgxspark.ai_conversation import AIConversationReader
from uniform_dgxspark.schemas import AISessionRead


def _session(workspace: Path, *, provider: str = "codex") -> AISessionRead:
    timestamp = datetime(2026, 10, 30, 8, 0, tzinfo=UTC)
    return AISessionRead(
        id="session-1",
        deployment_id=None,
        provider=provider,
        task_type="deployment",
        display_name="Qwen deployment",
        target_model_id=None,
        ai_model=None,
        reasoning_effort=None,
        execution_mode="autonomous",
        status="running",
        initial_prompt="部署 Qwen 模型",
        reference_urls=[],
        workspace_path=str(workspace),
        pid=123,
        exit_code=None,
        error_message=None,
        archived_at=None,
        created_at=timestamp,
        updated_at=timestamp,
    )


def _rollout(path: Path, workspace: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "type": "session_meta",
        "timestamp": "2026-10-30T08:00:00Z",
        "payload": {
            "id": "codex-session",
            "cwd": str(workspace),
            "source": "cli",
            "thread_source": "user",
        },
    }
    with path.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(meta, ensure_ascii=False) + "\n")
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def _message(role: str, text: str, *, phase: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "type": "message",
        "id": f"msg-{role}-{phase or 'plain'}-{len(text)}",
        "role": role,
        "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
    }
    if phase:
        payload["phase"] = phase
    return {
        "type": "response_item",
        "timestamp": "2026-10-30T08:01:00Z",
        "payload": payload,
    }


def test_codex_conversation_keeps_only_user_requests_and_final_answers(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    codex_home = tmp_path / "codex"
    rollout = codex_home / "sessions" / "2026" / "10" / "30" / "rollout-main.jsonl"
    _rollout(
        rollout,
        workspace,
        [
            _message("user", "<environment_context><cwd>secret</cwd></environment_context>"),
            _message(
                "user",
                "You are the deployment assistant. User request:\n部署 Qwen 模型并验证服务",
            ),
            _message("assistant", "我先检查运行环境。", phase="commentary"),
            {
                "type": "response_item",
                "timestamp": "2026-10-30T08:01:10Z",
                "payload": {"type": "custom_tool_call", "name": "exec_command"},
            },
            _message(
                "assistant",
                "部署完成。服务健康检查通过，真实推理返回 200。",
                phase="final_answer",
            ),
        ],
    )
    guardian = codex_home / "sessions" / "2026" / "10" / "30" / "rollout-guardian.jsonl"
    _rollout(
        guardian,
        workspace,
        [_message("assistant", "内部审查内容不应展示", phase="final_answer")],
    )
    guardian_record = json.loads(guardian.read_text(encoding="utf-8").splitlines()[0])
    guardian_record["payload"]["thread_source"] = "guardian_review"
    guardian_lines = guardian.read_text(encoding="utf-8").splitlines()
    guardian_lines[0] = json.dumps(guardian_record, ensure_ascii=False)
    guardian.write_text("\n".join(guardian_lines) + "\n", encoding="utf-8")

    conversation = AIConversationReader(codex_home=codex_home).read(_session(workspace))

    assert conversation.available is True
    assert conversation.source == "codex_rollout"
    assert [message.role for message in conversation.messages] == ["user", "assistant"]
    assert conversation.messages[0].text == "部署 Qwen 模型并验证服务"
    assert "健康检查通过" in conversation.messages[1].text
    assert "environment_context" not in conversation.messages[0].text
    assert all("内部审查" not in message.text for message in conversation.messages)
    assert all("我先检查运行环境" not in message.text for message in conversation.messages)


def test_codex_conversation_updates_after_final_answer_is_appended(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    codex_home = tmp_path / "codex"
    rollout = codex_home / "sessions" / "2026" / "10" / "30" / "rollout-live.jsonl"
    _rollout(
        rollout,
        workspace,
        [
            _message("user", "查看部署状态"),
            _message("assistant", "正在检查日志。", phase="commentary"),
        ],
    )
    reader = AIConversationReader(codex_home=codex_home)
    first = reader.read(_session(workspace))

    assert [message.role for message in first.messages] == ["user"]
    with rollout.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                _message("assistant", "服务运行正常，健康检查返回 200。", phase="final_answer"),
                ensure_ascii=False,
            )
            + "\n"
        )
    second = reader.read(_session(workspace))

    assert [message.role for message in second.messages] == ["user", "assistant"]
    assert second.messages[-1].text == "服务运行正常，健康检查返回 200。"


def test_claude_conversation_reads_text_content_only(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    claude_home = tmp_path / "claude"
    log = claude_home / "projects" / "project" / "session.jsonl"
    log.parent.mkdir(parents=True)
    records = [
        {
            "type": "user",
            "uuid": "user-1",
            "cwd": str(workspace),
            "timestamp": "2026-10-30T08:00:01Z",
            "message": {"role": "user", "content": "检查部署"},
        },
        {
            "type": "assistant",
            "uuid": "assistant-1",
            "cwd": str(workspace),
            "timestamp": "2026-10-30T08:00:02Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "部署正常。"},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "hidden"}},
                ],
            },
        },
    ]
    log.write_text("\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8")

    conversation = AIConversationReader(claude_home=claude_home).read(
        _session(workspace, provider="claude")
    )

    assert conversation.source == "claude_history"
    assert [message.text for message in conversation.messages] == ["检查部署", "部署正常。"]


def test_missing_structured_log_returns_actionable_notice(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    conversation = AIConversationReader(codex_home=tmp_path / "missing").read(
        _session(workspace)
    )

    assert conversation.available is False
    assert conversation.messages[0].text == "部署 Qwen 模型"
    assert conversation.notice is not None
    assert "原始终端" in conversation.notice
