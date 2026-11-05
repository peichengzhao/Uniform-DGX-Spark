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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlsplit

LOGGER = logging.getLogger(__name__)

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_QUICK_TUNNEL_URL = re.compile(
    r"https://[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.trycloudflare\.com",
    re.IGNORECASE,
)
_NAMED_TUNNEL_READY = re.compile(
    r"Registered tunnel connection|Connection\s+\S+\s+registered",
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
    mode: str = "quick"
    health_checked_at: str | None = None
    last_healthy_at: str | None = None
    consecutive_health_failures: int = 0
    restart_count: int = 0
    next_retry_at: str | None = None


class ServiceExternalAccessManager:
    """Own one Cloudflare Tunnel for authenticated publications.

    Quick Tunnel remains the zero-configuration default. A remotely managed
    tunnel token plus a configured public hostname provides a stable URL. Both
    modes have an independent public health probe so a live cloudflared process
    cannot hide a broken tunnel (for example Cloudflare error 1033).
    """

    def __init__(
        self,
        state_path: Path,
        *,
        enabled: bool = True,
        cloudflared_binary: str = "cloudflared",
        gateway_url: str = "http://127.0.0.1:18181",
        start_timeout_seconds: float = 30.0,
        tunnel_token: str | None = None,
        public_host: str | None = None,
        health_interval_seconds: float = 15.0,
        health_timeout_seconds: float = 8.0,
        health_failure_threshold: int = 2,
        restart_backoff_initial_seconds: float = 2.0,
        restart_backoff_max_seconds: float = 60.0,
    ) -> None:
        self.state_path = state_path
        self.enabled = enabled
        self.cloudflared_binary = cloudflared_binary
        self.gateway_url = gateway_url.rstrip("/")
        self.start_timeout_seconds = start_timeout_seconds
        self.tunnel_token = (tunnel_token or "").strip() or None
        self.public_host = self._normalize_public_host(public_host)
        self.health_interval_seconds = max(float(health_interval_seconds), 1.0)
        self.health_timeout_seconds = max(float(health_timeout_seconds), 1.0)
        self.health_failure_threshold = max(int(health_failure_threshold), 1)
        self.restart_backoff_initial_seconds = max(
            float(restart_backoff_initial_seconds), 0.1
        )
        self.restart_backoff_max_seconds = max(
            float(restart_backoff_max_seconds), self.restart_backoff_initial_seconds
        )
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._subscriptions = self._load_subscriptions()
        self._process: subprocess.Popen[bytes] | None = None
        self._public_host: str | None = None
        self._tunnel_state = "stopped"
        self._error: str | None = None
        self._log_lines: deque[str] = deque(maxlen=40)
        self._tunnel_connected = False
        self._health_checked_at: str | None = None
        self._last_healthy_at: str | None = None
        self._health_failures = 0
        self._restart_count = 0
        self._restart_attempts = 0
        self._next_restart_at = 0.0
        self._watchdog_stop = threading.Event()
        self._watchdog_thread: threading.Thread | None = None

    @property
    def mode(self) -> str:
        return "named" if self.tunnel_token else "quick"

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
                mode=self.mode,
                health_checked_at=self._health_checked_at if subscribed else None,
                last_healthy_at=self._last_healthy_at if subscribed else None,
                consecutive_health_failures=(
                    self._health_failures if subscribed else 0
                ),
                restart_count=self._restart_count if subscribed else 0,
                next_retry_at=(
                    self._iso_from_monotonic(self._next_restart_at)
                    if subscribed and self._next_restart_at > time.monotonic()
                    else None
                ),
            )

    def restore_for_enabled_publications(self, publication_ids: set[str]) -> None:
        with self._lock:
            retained = self._subscriptions.intersection(publication_ids)
            if retained != self._subscriptions:
                self._subscriptions = retained
                self._persist_subscriptions_locked()
            if not self._subscriptions:
                return
            self._ensure_watchdog_locked()
            try:
                self._ensure_tunnel_locked()
            except (ExternalAccessUnavailable, ExternalAccessStartError):
                LOGGER.exception("could not restore managed Cloudflare Tunnel")

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
            self._ensure_watchdog_locked()
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
        self._watchdog_stop.set()
        with self._condition:
            self._condition.notify_all()
        with self._lock:
            self._stop_process_locked()
        thread = self._watchdog_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)

    def _ensure_tunnel_locked(self) -> None:
        if not self.enabled:
            raise ExternalAccessUnavailable("external model access is disabled")
        if self.tunnel_token and not self.public_host:
            raise ExternalAccessUnavailable(
                "a named tunnel requires UNIFORM_EXTERNAL_PUBLIC_HOST, for example "
                "https://models.example.com"
            )
        self._refresh_process_locked()
        if (
            self._process is not None
            and self._process.poll() is None
            and self._public_host
            and self._tunnel_connected
        ):
            return

        binary = self._resolve_binary()
        command = self._build_command(binary)
        self._public_host = self.public_host if self.mode == "named" else None
        self._error = None
        self._tunnel_state = "starting"
        self._tunnel_connected = False
        self._log_lines.clear()
        environment = None
        if self.tunnel_token:
            environment = {**os.environ, "TUNNEL_TOKEN": self.tunnel_token}
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
                env=environment,
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
            if self._public_host and self._tunnel_connected:
                self._tunnel_state = "running"
                self._restart_attempts = 0
                self._health_failures = 0
                return
            if process.poll() is not None:
                self._refresh_process_locked()
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            with self._condition:
                self._condition.wait(timeout=min(remaining, 0.25))
        if self._public_host and self._tunnel_connected:
            self._tunnel_state = "running"
            self._restart_attempts = 0
            self._health_failures = 0
            return

        process_error = self._error if self._tunnel_state == "error" else None
        detail = " | ".join(self._log_lines)
        self._stop_process_locked()
        self._tunnel_state = "error"
        if self.mode == "named":
            self._error = process_error or (
                "cloudflared did not register the named tunnel within "
                f"{self.start_timeout_seconds:g} seconds"
            )
        else:
            self._error = process_error or (
                f"cloudflared did not provide a Quick Tunnel URL within "
                f"{self.start_timeout_seconds:g} seconds"
            )
        if detail:
            self._error = f"{self._error}: {detail[-600:]}"
        raise ExternalAccessStartError(self._error)

    def _build_command(self, binary: str) -> list[str]:
        if self.tunnel_token:
            return [
                binary,
                "tunnel",
                "--no-autoupdate",
                "run",
            ]
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
                if self.tunnel_token:
                    line = line.replace(self.tunnel_token, "[redacted]")
                host = self._extract_public_host(line)
                with self._condition:
                    self._log_lines.append(line)
                    if host and self._process is process:
                        self._public_host = host
                        self._tunnel_connected = True
                        self._tunnel_state = "running"
                        self._error = None
                        self._condition.notify_all()
                    elif (
                        self.mode == "named"
                        and self._process is process
                        and self._is_named_tunnel_ready(line)
                    ):
                        self._tunnel_connected = True
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
                    self._tunnel_connected = False
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
        self._tunnel_connected = False
        self._tunnel_state = "error"
        self._error = f"cloudflared exited with code {exit_code}"
        self._condition.notify_all()

    def _ensure_watchdog_locked(self) -> None:
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            return
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            name="uniform-tunnel-watchdog",
            daemon=True,
        )
        self._watchdog_thread.start()

    def _watchdog_loop(self) -> None:
        while not self._watchdog_stop.wait(self.health_interval_seconds):
            try:
                self._watchdog_tick()
            except Exception:  # noqa: BLE001 - watchdog must survive one bad probe
                LOGGER.exception("Cloudflare Tunnel watchdog iteration failed")

    def _watchdog_tick(self) -> None:
        with self._lock:
            if not self.enabled or not self._subscriptions:
                return
            self._refresh_process_locked()
            now = time.monotonic()
            if self._process is None:
                if now < self._next_restart_at:
                    return
                self._attempt_restart_locked("cloudflared is not running")
                return
            if not self._public_host or not self._tunnel_connected:
                return
            host = self._public_host

        healthy = self._probe_public_host(host)

        with self._lock:
            if self._public_host != host or self._process is None:
                return
            self._health_checked_at = self._now_iso()
            if healthy:
                self._health_failures = 0
                self._last_healthy_at = self._health_checked_at
                if self._tunnel_state == "error":
                    self._tunnel_state = "running"
                    self._error = None
                return
            self._health_failures += 1
            self._error = (
                f"public tunnel health check failed "
                f"({self._health_failures}/{self.health_failure_threshold}): {host}"
            )
            if self._health_failures >= self.health_failure_threshold:
                self._attempt_restart_locked(self._error)

    def _attempt_restart_locked(self, reason: str) -> None:
        now = time.monotonic()
        if now < self._next_restart_at:
            return
        self._restart_count += 1
        self._restart_attempts += 1
        delay = min(
            self.restart_backoff_initial_seconds
            * (2 ** max(self._restart_attempts - 1, 0)),
            self.restart_backoff_max_seconds,
        )
        self._stop_process_locked()
        self._tunnel_state = "starting"
        self._error = f"restarting tunnel after: {reason}"
        try:
            self._ensure_tunnel_locked()
        except (ExternalAccessUnavailable, ExternalAccessStartError) as error:
            self._tunnel_state = "error"
            self._error = str(error)
            self._next_restart_at = time.monotonic() + delay
            LOGGER.warning("could not restart Cloudflare Tunnel: %s", error)
            return
        self._next_restart_at = 0.0
        self._restart_attempts = 0
        self._health_failures = 0
        self._error = None
        LOGGER.info("Cloudflare Tunnel restarted successfully after: %s", reason)

    def _probe_public_host(self, host: str) -> bool:
        request = urllib_request.Request(
            f"{host.rstrip('/')}/__uniform_tunnel_health__",
            method="GET",
            headers={"User-Agent": "uniform-tunnel-health/1.0"},
        )
        try:
            with urllib_request.urlopen(
                request, timeout=self.health_timeout_seconds
            ) as response:
                return int(response.status) < 500
        except urllib_error.HTTPError as error:
            try:
                return int(error.code) < 500
            finally:
                error.close()
        except (OSError, TimeoutError, ValueError):
            return False

    @staticmethod
    def _is_named_tunnel_ready(line: str) -> bool:
        return _NAMED_TUNNEL_READY.search(line) is not None

    @staticmethod
    def _normalize_public_host(value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip().rstrip("/")
        if "://" not in normalized:
            normalized = f"https://{normalized}"
        parsed = urlsplit(normalized)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("external public host must be an https origin")
        return f"https://{parsed.netloc}"

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _iso_from_monotonic(value: float) -> str:
        remaining = max(value - time.monotonic(), 0.0)
        return (datetime.now(UTC) + timedelta(seconds=remaining)).isoformat()

    def _stop_process_locked(self) -> None:
        process = self._process
        self._process = None
        self._public_host = None
        self._tunnel_connected = False
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
