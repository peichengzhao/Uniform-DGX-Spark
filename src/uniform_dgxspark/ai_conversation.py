from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .schemas import AIConversationMessage, AIConversationRead, AISessionRead

_MAX_MESSAGES = 300
_MAX_MESSAGE_CHARS = 40_000
_APPROVAL_MARKERS = (
    ">>> APPROVAL REQUEST",
    ">>> APPROVAL REQUEST END",
    ">>> USER MESSAGE",
)


@dataclass(slots=True)
class _MessageRecord:
    key: str
    role: str
    text: str
    created_at: datetime | None
    phase: str | None = None
    order: int = 0


@dataclass(slots=True)
class _LogCursor:
    path: Path
    offset: int = 0
    inode: int = 0
    messages: list[_MessageRecord] = field(default_factory=list)
    seen_keys: set[str] = field(default_factory=set)
    seen_signatures: set[tuple[str, str, str | None]] = field(default_factory=set)
    meta: dict[str, Any] | None = None

    def refresh(self, provider: str) -> None:
        try:
            stat = self.path.stat()
        except OSError:
            return
        if stat.st_ino != self.inode or stat.st_size < self.offset:
            self.offset = 0
            self.messages.clear()
            self.seen_keys.clear()
            self.seen_signatures.clear()
            self.meta = None
        self.inode = stat.st_ino
        if stat.st_size <= self.offset:
            return
        try:
            with self.path.open("rb") as stream:
                stream.seek(self.offset)
                chunk = stream.read(stat.st_size - self.offset)
        except OSError:
            return
        boundary = chunk.rfind(b"\n")
        if boundary < 0:
            return
        complete = chunk[: boundary + 1]
        self.offset += len(complete)
        for raw_line in complete.splitlines():
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
            except (TypeError, ValueError):
                continue
            if not isinstance(record, dict):
                continue
            if provider == "codex":
                self._consume_codex(record)
            else:
                self._consume_claude(record)

    def _append(
        self,
        *,
        key: str,
        role: str,
        text: str,
        created_at: datetime | None,
        phase: str | None,
    ) -> None:
        normalized = text.strip()
        if not normalized:
            return
        if len(normalized) > _MAX_MESSAGE_CHARS:
            normalized = normalized[:_MAX_MESSAGE_CHARS].rstrip() + "\n\n…（内容过长，已截断）"
        signature = (
            role,
            normalized,
            created_at.isoformat() if created_at else None,
        )
        if key in self.seen_keys or signature in self.seen_signatures:
            return
        self.seen_keys.add(key)
        self.seen_signatures.add(signature)
        self.messages.append(
            _MessageRecord(
                key=key,
                role=role,
                text=normalized,
                created_at=created_at,
                phase=phase,
                order=len(self.messages),
            )
        )

    def _consume_codex(self, record: dict[str, Any]) -> None:
        record_type = record.get("type")
        payload = record.get("payload")
        if record_type == "session_meta" and isinstance(payload, dict):
            self.meta = payload
            return
        if record_type != "response_item" or not isinstance(payload, dict):
            return
        if payload.get("type") != "message":
            return
        role = payload.get("role")
        if role not in {"user", "assistant"}:
            return
        text = _content_text(payload.get("content"))
        if not text:
            return
        phase = _optional_text(payload.get("phase"))
        if role == "assistant" and phase not in {None, "final_answer"}:
            return
        if role == "user":
            text = _clean_user_text(text)
            if not text:
                return
        else:
            text = text.strip()
            if not text:
                return
        timestamp = _parse_timestamp(record.get("timestamp"))
        message_id = _optional_text(payload.get("id"))
        key = message_id or (
            f"codex:{self.path}:{record.get('ordinal', self.offset)}:{role}:{timestamp}"
        )
        self._append(
            key=key,
            role=role,
            text=text,
            created_at=timestamp,
            phase=phase,
        )

    def _consume_claude(self, record: dict[str, Any]) -> None:
        record_type = record.get("type")
        message = record.get("message")
        if record_type not in {"user", "assistant"} or not isinstance(message, dict):
            return
        role = message.get("role") or record_type
        if role not in {"user", "assistant"}:
            return
        text = _content_text(message.get("content"))
        if not text:
            return
        if role == "user":
            text = _clean_user_text(text)
        else:
            text = text.strip()
        if not text:
            return
        timestamp = _parse_timestamp(record.get("timestamp"))
        message_id = _optional_text(record.get("uuid")) or _optional_text(message.get("id"))
        key = message_id or f"claude:{self.path}:{self.offset}:{role}:{timestamp}"
        self._append(
            key=key,
            role=role,
            text=text,
            created_at=timestamp,
            phase=None,
        )


class AIConversationReader:
    """Read only the human-readable messages a local AI CLI writes to disk."""

    def __init__(
        self,
        *,
        codex_home: Path | None = None,
        claude_home: Path | None = None,
    ) -> None:
        self.codex_home = codex_home
        self.claude_home = claude_home
        self._cursors: dict[Path, _LogCursor] = {}
        self._codex_meta: dict[Path, dict[str, Any] | None] = {}
        self._claude_meta: dict[Path, str | None] = {}
        self._lock = threading.Lock()

    def read(self, session: AISessionRead) -> AIConversationRead:
        with self._lock:
            if session.provider == "codex":
                records, found = self._read_codex(Path(session.workspace_path))
                source = "codex_rollout"
                unsupported_notice = None
            elif session.provider == "claude":
                records, found = self._read_claude(Path(session.workspace_path))
                source = "claude_history"
                unsupported_notice = None
            else:
                records, found = [], False
                source = "none"
                unsupported_notice = (
                    "DeepSeek Harness 暂未提供可读取的结构化对话格式；"
                    "请切换到“原始终端”查看完整执行过程。"
                )

        if not any(record.role == "user" for record in records) and session.initial_prompt.strip():
            records.insert(
                0,
                _MessageRecord(
                    key=f"session:{session.id}:initial-prompt",
                    role="user",
                    text=session.initial_prompt.strip(),
                    created_at=session.created_at,
                    phase=None,
                    order=-1,
                ),
            )
        messages = [
            AIConversationMessage(
                id=record.key,
                role=record.role,
                text=record.text,
                created_at=record.created_at,
                phase=record.phase,
            )
            for record in records[-_MAX_MESSAGES:]
        ]
        notice = unsupported_notice
        if notice is None and not found:
            notice = (
                "还没有找到这个会话的结构化对话记录。会话刚启动时请稍等片刻，"
                "或切换到“原始终端”查看实时进度。"
            )
        return AIConversationRead(
            session_id=session.id,
            provider=session.provider,
            status=session.status,
            source=source,
            available=found,
            messages=messages,
            notice=notice,
        )

    def _read_codex(self, workspace: Path) -> tuple[list[_MessageRecord], bool]:
        home = self._codex_home()
        sessions_dir = home / "sessions"
        if not sessions_dir.is_dir():
            return [], False
        workspace_text = _resolved_text(workspace)
        candidates: list[Path] = []
        for path in sessions_dir.rglob("rollout-*.jsonl"):
            meta = self._codex_meta.get(path)
            if path not in self._codex_meta:
                record = _read_first_json_object(path)
                payload = record.get("payload") if isinstance(record, dict) else None
                self._codex_meta[path] = payload if isinstance(payload, dict) else None
                meta = payload
            if not isinstance(meta, dict):
                continue
            if _resolved_text(_optional_text(meta.get("cwd"))) != workspace_text:
                continue
            thread_source = meta.get("thread_source")
            if thread_source not in {None, "user"}:
                continue
            if isinstance(meta.get("source"), dict):
                continue
            candidates.append(path)
        records: list[_MessageRecord] = []
        for path in sorted(candidates):
            cursor = self._cursors.get(path)
            if cursor is None:
                cursor = _LogCursor(path=path)
                self._cursors[path] = cursor
            cursor.refresh("codex")
            records.extend(cursor.messages)
        return _sort_and_dedupe(records), bool(candidates)

    def _read_claude(self, workspace: Path) -> tuple[list[_MessageRecord], bool]:
        home = self._claude_home()
        projects = home / "projects"
        if not projects.is_dir():
            return [], False
        workspace_text = _resolved_text(workspace)
        candidates: list[Path] = []
        for path in projects.rglob("*.jsonl"):
            cached_cwd = self._claude_meta.get(path)
            if path not in self._claude_meta:
                cached_cwd = _read_claude_cwd(path)
                self._claude_meta[path] = cached_cwd
            if _resolved_text(cached_cwd) != workspace_text:
                continue
            candidates.append(path)
        records: list[_MessageRecord] = []
        for path in sorted(candidates):
            cursor = self._cursors.get(path)
            if cursor is None:
                cursor = _LogCursor(path=path)
                self._cursors[path] = cursor
            cursor.refresh("claude")
            records.extend(cursor.messages)
        return _sort_and_dedupe(records), bool(candidates)

    def _codex_home(self) -> Path:
        if self.codex_home is not None:
            return self.codex_home.expanduser()
        configured = os.getenv("CODEX_HOME")
        return Path(configured).expanduser() if configured else Path.home() / ".codex"

    def _claude_home(self) -> Path:
        if self.claude_home is not None:
            return self.claude_home.expanduser()
        configured = os.getenv("CLAUDE_CONFIG_DIR")
        return Path(configured).expanduser() if configured else Path.home() / ".claude"


def _sort_and_dedupe(records: Iterable[_MessageRecord]) -> list[_MessageRecord]:
    ordered = sorted(
        records,
        key=lambda record: (
            record.created_at or datetime.min.replace(tzinfo=UTC),
            record.order,
            record.text,
        ),
    )
    result: list[_MessageRecord] = []
    seen_ids: set[str] = set()
    seen_signatures: set[tuple[str, str, str | None]] = set()
    for record in ordered:
        signature = (
            record.role,
            record.text,
            record.created_at.isoformat() if record.created_at else None,
        )
        if record.key in seen_ids or signature in seen_signatures:
            continue
        seen_ids.add(record.key)
        seen_signatures.add(signature)
        result.append(record)
    return result


def _read_first_json_object(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as stream:
            line = stream.readline()
    except OSError:
        return None
    try:
        record = json.loads(line)
    except (TypeError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _read_claude_cwd(path: Path) -> str | None:
    try:
        with path.open("rb") as stream:
            for _ in range(20):
                line = stream.readline()
                if not line:
                    break
                try:
                    record = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(record, dict) and isinstance(record.get("cwd"), str):
                    return str(record["cwd"])
    except OSError:
        return None
    return None


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type not in {None, "text", "input_text", "output_text"}:
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts).strip()


def _clean_user_text(text: str) -> str:
    normalized = text.strip()
    if not normalized:
        return ""
    reply_match = re.search(
        r"<send_user_message_question_reply>\s*(.*?)\s*</send_user_message_question_reply>",
        normalized,
        flags=re.DOTALL,
    )
    if reply_match:
        try:
            answers = json.loads(reply_match.group(1))
        except (TypeError, ValueError):
            answers = None
        if isinstance(answers, list):
            rendered = [
                str(item.get("answer", "")).strip()
                for item in answers
                if isinstance(item, dict) and str(item.get("answer", "")).strip()
            ]
            if rendered:
                return "\n".join(rendered)
    marker = re.search(r"(?:^|\n)##\s*My request:\s*\n?", normalized)
    if marker:
        normalized = normalized[marker.end() :]
    else:
        marker = re.search(r"(?:^|\n|\.\s+)User request:\s*\n?", normalized)
        if marker:
            normalized = normalized[marker.end() :]
    normalized = re.sub(
        r"\n?---\s*\n?<image\s+name=.*?</image>\s*",
        "",
        normalized,
        flags=re.DOTALL,
    )
    normalized = re.sub(r"<image\s+name=.*?</image>", "", normalized, flags=re.DOTALL)
    normalized = normalized.strip()
    if not normalized:
        return ""
    ignored_prefixes = (
        "<environment_context>",
        "# AGENTS.md instructions",
        ">_ OpenAI Codex",
        "<turn_aborted>",
        "<user_message",
    )
    if normalized.startswith(ignored_prefixes):
        return ""
    if any(marker in normalized for marker in _APPROVAL_MARKERS):
        return ""
    return normalized


def _optional_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _resolved_text(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(Path(value).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        return value
