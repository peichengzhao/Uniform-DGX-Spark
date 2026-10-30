from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

LOGGER = logging.getLogger(__name__)

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_QUICK_TUNNEL_URL = re.compile(
    r"https://[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.trycloudflare\.com",
    re.IGNORECASE,
)


class ExternalAccessUnavailable(RuntimeError):
    pass


class ExternalAccessStartError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ExternalAccessStatus:
    publication_id: str
    enabled: bool
    tunnel_state: str
    public_host: str | None
    public_base_url: str | None
    error: str | None


class ServiceExternalAccessManager:
    """Own one Cloudflare Quick Tunnel for authenticated publications."""

    def __init__(
        self,
        state_path: Path,
        *,
        enabled: bool = True,
        cloudflared_binary: str = "cloudflared",
        gateway_url: str = "http://127.0.0.1:18181",
        start_timeout_seconds: float = 30.0,
    ) -> None:
        self.state_path = state_path
        self.enabled = enabled
        self.cloudflared_binary = cloudflared_binary
        self.gateway_url = gateway_url.rstrip("/")
        self.start_timeout_seconds = start_timeout_seconds
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._subscriptions = self._load_subscriptions()
        self._process: subprocess.Popen[bytes] | None = None
        self._public_host: str | None = None
        self._tunnel_state = "stopped"
        self._error: str | None = None
        self._log_lines: deque[str] = deque(maxlen=40)

    def status(self, publication_id: str) -> ExternalAccessStatus:
        with self._lock:
            self._refresh_process_locked()
            subscribed = publication_id in self._subscriptions
            public_host = self._public_host if subscribed else None
            return ExternalAccessStatus(
                publication_id=publication_id,
                enabled=subscribed,
                tunnel_state=self._tunnel_state if subscribed else "stopped",
                public_host=public_host,
                public_base_url=self._public_base_url(publication_id, public_host),
                error=self._error if subscribed else None,
            )

    def restore_for_enabled_publications(self, publication_ids: set[str]) -> None:
        with self._lock:
            retained = self._subscriptions.intersection(publication_ids)
            if retained != self._subscriptions:
                self._subscriptions = retained
                self._persist_subscriptions_locked()
            if not self._subscriptions:
                return
            try:
                self._ensure_tunnel_locked()
            except (ExternalAccessUnavailable, ExternalAccessStartError):
                LOGGER.exception("could not restore managed Cloudflare Quick Tunnel")

    def start(self, publication_id: str) -> ExternalAccessStatus:
        if not self.enabled:
            raise ExternalAccessUnavailable("external model access is disabled")
        with self._lock:
            self._refresh_process_locked()
            was_subscribed = publication_id in self._subscriptions
            if not was_subscribed:
                self._subscriptions.add(publication_id)
                try:
                    self._persist_subscriptions_locked()
                except Exception:
                    self._subscriptions.remove(publication_id)
                    raise
            try:
                self._ensure_tunnel_locked()
            except (ExternalAccessUnavailable, ExternalAccessStartError):
                if not was_subscribed:
                    self._subscriptions.remove(publication_id)
                    try:
                        self._persist_subscriptions_locked()
                    except Exception:
                        LOGGER.exception("could not roll back external access subscription")
                raise
            return self.status(publication_id)

    def stop(self, publication_id: str) -> ExternalAccessStatus:
        with self._lock:
            if publication_id in self._subscriptions:
                self._subscriptions.remove(publication_id)
                self._persist_subscriptions_locked()
            if not self._subscriptions:
                self._stop_process_locked()
            return self.status(publication_id)

    def shutdown(self) -> None:
        with self._lock:
            self._stop_process_locked()

    def _ensure_tunnel_locked(self) -> None:
        if not self.enabled:
            raise ExternalAccessUnavailable("external model access is disabled")
        self._refresh_process_locked()
        if self._process is not None and self._process.poll() is None and self._public_host:
            return

        binary = self._resolve_binary()
        command = self._build_command(binary)
        self._public_host = None
        self._error = None
        self._tunnel_state = "starting"
        self._log_lines.clear()
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
                bufsize=0,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as error:
            self._tunnel_state = "error"
            self._error = f"could not start cloudflared: {error}"
            raise ExternalAccessStartError(self._error) from error
        self._process = process
        threading.Thread(
            target=self._consume_output,
            args=(process,),
            name="uniform-cloudflared-log",
            daemon=True,
        ).start()

        deadline = time.monotonic() + self.start_timeout_seconds
        while time.monotonic() < deadline:
            if self._public_host:
                self._tunnel_state = "running"
                return
            if process.poll() is not None:
                self._refresh_process_locked()
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            with self._condition:
                self._condition.wait(timeout=min(remaining, 0.25))
        if self._public_host:
            self._tunnel_state = "running"
            return

        process_error = self._error if self._tunnel_state == "error" else None
        detail = " | ".join(self._log_lines)
        self._stop_process_locked()
        self._tunnel_state = "error"
        self._error = process_error or (
            f"cloudflared did not provide a Quick Tunnel URL within "
            f"{self.start_timeout_seconds:g} seconds"
        )
        if detail:
            self._error = f"{self._error}: {detail[-600:]}"
        raise ExternalAccessStartError(self._error)

    def _build_command(self, binary: str) -> list[str]:
        return [
            binary,
            "tunnel",
            "--no-autoupdate",
            "--url",
            self.gateway_url,
        ]

    def _resolve_binary(self) -> str:
        configured = self.cloudflared_binary.strip()
        if not configured:
            raise ExternalAccessUnavailable("cloudflared binary is not configured")
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return str(candidate)
        resolved = shutil.which(configured)
        if resolved:
            return resolved
        project_local = Path.cwd() / ".runtime" / "bin" / "cloudflared"
        if project_local.is_file():
            return str(project_local)
        local_binary = Path.home() / ".local" / "bin" / "cloudflared"
        if local_binary.is_file():
            return str(local_binary)
        raise ExternalAccessUnavailable("cloudflared is not installed or is not available on PATH")

    def _consume_output(self, process: subprocess.Popen[bytes]) -> None:
        stream: BinaryIO | None = process.stdout
        if stream is None:
            return
        try:
            for raw_line in iter(stream.readline, b""):
                line = _ANSI_ESCAPE.sub("", raw_line.decode("utf-8", errors="replace")).strip()
                if not line:
                    continue
                host = self._extract_public_host(line)
                with self._condition:
                    self._log_lines.append(line)
                    if host and self._process is process:
                        self._public_host = host
                        self._tunnel_state = "running"
                        self._error = None
                        self._condition.notify_all()
        except OSError:
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass
            exit_code = process.poll()
            with self._condition:
                if self._process is process:
                    self._process = None
                    self._public_host = None
                    self._tunnel_state = "error"
                    self._error = (
                        "cloudflared stopped unexpectedly"
                        if exit_code is None
                        else f"cloudflared exited with code {exit_code}"
                    )
                    self._condition.notify_all()

    def _refresh_process_locked(self) -> None:
        process = self._process
        if process is None:
            return
        exit_code = process.poll()
        if exit_code is None:
            return
        self._process = None
        self._public_host = None
        self._tunnel_state = "error"
        self._error = f"cloudflared exited with code {exit_code}"
        self._condition.notify_all()

    def _stop_process_locked(self) -> None:
        process = self._process
        self._process = None
        self._public_host = None
        self._tunnel_state = "stopped"
        self._error = None
        self._condition.notify_all()
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                LOGGER.warning("cloudflared did not stop after SIGKILL")
        except OSError:
            LOGGER.exception("could not terminate cloudflared")

    def _load_subscriptions(self) -> set[str]:
        if not self.state_path.exists():
            return set()
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            publication_ids = payload.get("publication_ids", [])
            return {str(value) for value in publication_ids if isinstance(value, str) and value}
        except (OSError, ValueError, TypeError):
            LOGGER.exception("could not load external access state")
            return set()

    def _persist_subscriptions_locked(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(f".{self.state_path.name}.tmp")
        payload = {"publication_ids": sorted(self._subscriptions)}
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(self.state_path)

    @staticmethod
    def _extract_public_host(line: str) -> str | None:
        match = _QUICK_TUNNEL_URL.search(line)
        return match.group(0).rstrip("/") if match else None

    @staticmethod
    def _public_base_url(publication_id: str, public_host: str | None) -> str | None:
        if not public_host:
            return None
        return f"{public_host}/{publication_id}/v1"
