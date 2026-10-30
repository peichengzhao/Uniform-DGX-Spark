from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from ..ai_store import AISessionStore
from ..schemas import (
    AIDeploymentResultRead,
    AIDeploymentTemplateCreate,
    AIDeploymentTemplateRead,
    AISessionRead,
    DeploymentRead,
)


class DeploymentTemplateInvalid(RuntimeError):
    pass


class DeploymentTemplateExecutionError(RuntimeError):
    def __init__(self, message: str, *, output: str = "") -> None:
        super().__init__(message)
        self.output = output[-16_000:]


class DeploymentTemplateBusy(DeploymentTemplateExecutionError):
    pass


_SHELL_VALUE = (
    r"(?:'[^'\r\n]*'|\"(?:\\.|[^\"\\\r\n])*\"|`[^`\r\n]*`|"
    r"\$\([^\r\n)]*\)|[^\s;&|]+)"
)
_SECRET_ASSIGNMENT = re.compile(
    rf"(?i)\b(?P<prefix>export\s+)?(?P<name>[A-Z_][A-Z0-9_]*)"
    rf"(?P<separator>\s*=\s*)(?P<value>{_SHELL_VALUE})"
)
_SECRET_FLAG = re.compile(
    rf"(?i)(?P<flag>--(?:[A-Z0-9]+[-_])*(?:api[-_]?key|access[-_]?token|"
    rf"auth[-_]?token|hf[-_]?token|token|password|secret|authorization|credential)"
    rf"(?:[-_](?:file|value))?)"
    rf"(?P<separator>=|\s+)(?P<value>{_SHELL_VALUE})"
)
_SHORT_SECRET_FLAG = re.compile(
    rf"(?i)(?P<prefix>\b(?:docker\s+login\b[^\r\n]*?\s-p|"
    rf"curl\b[^\r\n]*?\s-u)(?:=|\s+))(?P<value>{_SHELL_VALUE})"
)
_BEARER_SECRET = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_KNOWN_TOKEN = re.compile(
    r"(?i)\b(?:hf_[A-Za-z0-9]{8,}|sk-[A-Za-z0-9_-]{16,}|"
    r"github_pat_[A-Za-z0-9_]{16,})"
)
_URL_CREDENTIALS = re.compile(r"(?P<scheme>https?://)[^\s/@:]+:[^\s/@]+@", re.I)
_URL_QUERY_SECRET = re.compile(
    r"(?i)(?P<prefix>[?&](?:token|access_token|api_key|apikey|key|secret|password|"
    r"signature)=)(?P<value>[^&\s'\"]+)"
)
_RESIDUAL_SECRET = re.compile(
    r"(?i)(?:token|password|secret|api[-_]?key|authorization|auth[-_]?token|credential)"
    r"\s*(?:=|:)\s*(?:'[^'\r\n]+'|\"[^\"\r\n]+\"|[^\s;&|'\"\r\n]+)"
)


class AIDeploymentTemplateManager:
    FORMAT_VERSION = 3

    def __init__(
        self,
        store: AISessionStore,
        templates_dir: Path,
        *,
        docker_binary: str = "docker",
    ) -> None:
        self.store = store
        self.templates_dir = templates_dir.resolve()
        self.docker_binary = docker_binary
        self._operation_guard = threading.Lock()
        self._active_operations: set[str] = set()
        self.templates_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    @contextmanager
    def operation(self, template_id: str) -> Iterator[None]:
        with self._operation_guard:
            if template_id in self._active_operations:
                raise DeploymentTemplateBusy(
                    "a lifecycle operation is already running for this model service"
                )
            self._active_operations.add(template_id)
        try:
            yield
        finally:
            with self._operation_guard:
                self._active_operations.discard(template_id)

    def create(
        self,
        session: AISessionRead,
        result: AIDeploymentResultRead,
        *,
        deployment_context: dict[str, object],
        deployment: DeploymentRead | None = None,
        historical: bool = False,
    ) -> AIDeploymentTemplateRead:
        if result.status != "verified":
            raise DeploymentTemplateInvalid("only verified deployments can become templates")

        target = self._target(deployment_context)
        model_id = str(target.get("model_id") or session.target_model_id or "").strip()
        if not model_id:
            raise DeploymentTemplateInvalid("deployment target model is unavailable")
        local_path = self._optional_text(target.get("local_model_path"))
        source = str(target.get("source") or "").casefold()
        if local_path:
            source = "local"
        if source not in {"huggingface", "modelscope", "local"}:
            source = "unknown"
        topology = self._topology(deployment_context)

        safe_command, command_secrets = self._sanitize_text(result.launch_command)
        safe_image, image_secrets = self._sanitize_text(result.image)
        safe_container_name, container_secrets = self._sanitize_text(result.container_name)
        safe_service_url, service_url_secrets = self._sanitize_text(result.service_url)
        parameters: dict[str, object] = {
            "container_name": safe_container_name,
            "port": result.port,
            "service_url": safe_service_url,
        }
        parameter_secrets = container_secrets or service_url_secrets
        reuse_mode = "ai_session"
        artifact_verification_status = "legacy_unverified"
        if (
            deployment is not None
            and result.engine.casefold() == "vllm"
            and topology == "single_node"
        ):
            parameters, managed_parameter_secrets = self._safe_managed_parameters(deployment)
            parameter_secrets = parameter_secrets or managed_parameter_secrets
            if not (command_secrets or image_secrets or parameter_secrets):
                reuse_mode = "managed"
                artifact_verification_status = "not_required"

        references = [
            url for url in session.reference_urls if self._reference_has_no_credentials(url)
        ]
        template_dir = (self.templates_dir / session.id).resolve()
        if self.templates_dir not in template_dir.parents:
            raise DeploymentTemplateInvalid("invalid deployment template path")
        template_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        script_path = template_dir / "start-reusable.sh"
        stop_script_path = template_dir / "stop-reusable.sh"
        container_id = self._full_container_id(result.container_id)
        direct_candidate = (
            reuse_mode == "ai_session"
            and not historical
            and not (command_secrets or image_secrets or parameter_secrets)
            and topology == "single_node"
            and container_id is not None
            and self._valid_container_name(safe_container_name)
            and not self._looks_multi_node(safe_command)
        )
        if direct_candidate:
            artifact_verification_status = "pending"
        start_script = self._container_script(
            action="start",
            container_name=safe_container_name,
            container_id=container_id,
            image=safe_image,
        )
        stop_script = self._container_script(
            action="stop",
            container_name=safe_container_name,
            container_id=container_id,
            image=safe_image,
        )
        self._write_script(script_path, start_script)
        self._write_script(stop_script_path, stop_script)
        script_sha256 = hashlib.sha256(start_script.encode()).hexdigest()
        stop_script_sha256 = hashlib.sha256(stop_script.encode()).hexdigest()

        template = AIDeploymentTemplateCreate(
            format_version=self.FORMAT_VERSION,
            id=session.id,
            source_session_id=session.id,
            source_deployment_id=session.deployment_id,
            model_id=model_id,
            target_source=source,
            target_revision=self._optional_text(target.get("revision")),
            target_local_path=local_path,
            deployment_topology=topology,
            engine=result.engine,
            image=safe_image,
            port=result.port,
            container_name=safe_container_name,
            inference_endpoint=(
                "chat_completions"
                if "/chat/completions" in result.inference_test.url
                else "completions"
            ),
            reuse_mode=reuse_mode,
            parameters=parameters,
            reference_urls=references,
            launch_command_template=safe_command,
            script_path=str(script_path),
            script_sha256=script_sha256,
            stop_script_path=str(stop_script_path),
            stop_script_sha256=stop_script_sha256,
            container_id=container_id,
            artifact_verification_status=artifact_verification_status,
            requires_secret_values=(command_secrets or image_secrets or parameter_secrets),
        )
        manifest = template.model_dump(mode="json")
        self._write_manifest(template_dir, manifest)
        stored = self.store.upsert_template(template)
        self.store.add_audit(
            session.id,
            "deployment_template_created",
            "Created a credential-free reusable deployment template",
            {
                "template_id": stored.id,
                "reuse_mode": stored.reuse_mode,
                "script_sha256": stored.script_sha256,
                "stop_script_sha256": stored.stop_script_sha256,
                "artifact_verification_status": stored.artifact_verification_status,
                "requires_secret_values": stored.requires_secret_values,
            },
        )
        return stored

    def validate(self, template: AIDeploymentTemplateRead) -> None:
        self._validate_artifacts(template)
        if template.reuse_mode == "direct_script":
            if (
                template.format_version < self.FORMAT_VERSION
                or template.artifact_verification_status != "verified"
                or not template.stop_script_path
                or not template.stop_script_sha256
                or not template.container_id
            ):
                raise DeploymentTemplateInvalid(
                    "direct deployment artifacts are not fully verified"
                )
        if template.target_source == "local":
            if not template.target_local_path or not Path(template.target_local_path).is_dir():
                raise DeploymentTemplateInvalid("local model path is no longer available")

    def execute(
        self,
        template: AIDeploymentTemplateRead,
        action: str,
        *,
        allow_unverified: bool = False,
    ) -> str:
        if action not in {"start", "stop"}:
            raise ValueError("deployment artifact action must be start or stop")
        verification_allowed = allow_unverified and template.artifact_verification_status in {
            "pending",
            "verifying",
        }
        if template.reuse_mode != "direct_script" and not verification_allowed:
            raise DeploymentTemplateInvalid("deployment artifacts are not verified")
        self._validate_artifacts(template)
        if action == "start":
            path = Path(template.script_path)
            expected_hash = template.script_sha256
            timeout = 120
        else:
            if not template.stop_script_path or not template.stop_script_sha256:
                raise DeploymentTemplateInvalid("verified stop artifact is missing")
            path = Path(template.stop_script_path)
            expected_hash = template.stop_script_sha256
            timeout = 90
        script = self._validated_script(path, expected_hash)
        environment = {
            "PATH": os.environ.get(
                "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
            ),
            "HOME": os.environ.get("HOME", "/tmp"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }
        for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "XDG_RUNTIME_DIR"):
            if value := os.environ.get(key):
                environment[key] = value
        process = subprocess.Popen(
            ["/bin/bash", "--noprofile", "--norc"],
            cwd=str(path.parent),
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            output, _ = process.communicate(script, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                output, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                output, _ = process.communicate()
            raise DeploymentTemplateExecutionError(
                f"{action} artifact timed out after {timeout} seconds", output=output
            ) from error
        output = output[-16_000:]
        if process.returncode != 0:
            raise DeploymentTemplateExecutionError(
                f"{action} artifact exited with code {process.returncode}", output=output
            )
        return output

    def update_verification(
        self,
        template: AIDeploymentTemplateRead,
        status: str,
        *,
        error: str | None = None,
    ) -> AIDeploymentTemplateRead:
        if status not in {"pending", "verifying", "verified", "failed"}:
            raise ValueError("invalid artifact verification status")
        payload = template.model_dump(exclude={"created_at", "updated_at"})
        payload.update(
            {
                "reuse_mode": "direct_script" if status == "verified" else "ai_session",
                "artifact_verification_status": status,
                "artifact_verification_error": error[-8000:] if error else None,
                "artifact_verified_at": (datetime.now(UTC) if status == "verified" else None),
            }
        )
        updated = AIDeploymentTemplateCreate.model_validate(payload)
        template_dir = Path(updated.script_path).parent
        self._write_manifest(template_dir, updated.model_dump(mode="json"))
        return self.store.upsert_template(updated)

    def _validated_script(self, script_path: Path, expected_hash: str) -> str:
        try:
            resolved = script_path.resolve(strict=True)
        except OSError as error:
            raise DeploymentTemplateInvalid("reusable script is missing") from error
        if self.templates_dir not in resolved.parents or script_path.is_symlink():
            raise DeploymentTemplateInvalid("reusable script path is not trusted")
        try:
            script = resolved.read_bytes()
        except OSError as error:
            raise DeploymentTemplateInvalid("reusable script cannot be read") from error
        if hashlib.sha256(script).hexdigest() != expected_hash:
            raise DeploymentTemplateInvalid("reusable script integrity check failed")
        try:
            return script.decode("utf-8")
        except UnicodeDecodeError as error:
            raise DeploymentTemplateInvalid("reusable script is not UTF-8") from error

    def _validate_artifacts(self, template: AIDeploymentTemplateRead) -> None:
        self._validated_script(Path(template.script_path), template.script_sha256)
        if template.stop_script_path or template.stop_script_sha256:
            if not template.stop_script_path or not template.stop_script_sha256:
                raise DeploymentTemplateInvalid("incomplete stop artifact metadata")
            self._validated_script(Path(template.stop_script_path), template.stop_script_sha256)

    @staticmethod
    def _full_container_id(value: str | None) -> str | None:
        normalized = (value or "").strip().casefold()
        return normalized if re.fullmatch(r"[0-9a-f]{64}", normalized) else None

    @staticmethod
    def _valid_container_name(value: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value))

    @staticmethod
    def _looks_multi_node(command: str) -> bool:
        if re.search(r"(?i)(?:^|\s)--nnodes(?:=|\s+)(?:[2-9]|[1-9][0-9]+)(?:\s|$)", command):
            return True
        return bool(
            re.search(
                r"(?i)(?:^|[;&|]\s*|\s)(?:ssh|pdsh|mpirun|mpiexec|srun)(?:\s|$)|"
                r"(?:^|\s)--node-rank(?:=|\s+)",
                command,
            )
        )

    @staticmethod
    def _write_script(path: Path, content: str) -> None:
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.chmod(0o700)
        checked = subprocess.run(
            ["/bin/bash", "--noprofile", "--norc", "-n", str(temporary)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if checked.returncode != 0:
            temporary.unlink(missing_ok=True)
            raise DeploymentTemplateInvalid(
                f"generated lifecycle script is invalid: {checked.stderr.strip()}"
            )
        temporary.replace(path)

    @staticmethod
    def _write_manifest(template_dir: Path, manifest: dict[str, object]) -> None:
        path = template_dir / "deployment-template.json"
        temporary = template_dir / ".deployment-template.json.tmp"
        temporary.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(path)

    @staticmethod
    def _target(context: dict[str, object]) -> dict[str, object]:
        direct_target = context.get("target_model")
        if isinstance(direct_target, dict):
            return direct_target
        deployment = context.get("deployment")
        if isinstance(deployment, dict):
            target = deployment.get("target_model")
            if isinstance(target, dict):
                return target
        return {}

    @staticmethod
    def _topology(context: dict[str, object]) -> str:
        topology = context.get("deployment_topology")
        if isinstance(topology, dict) and topology.get("resolved") == "multi_node":
            return "multi_node"
        return "single_node"

    @staticmethod
    def _optional_text(value: object) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @classmethod
    def _safe_managed_parameters(cls, deployment: DeploymentRead) -> tuple[dict[str, object], bool]:
        raw = deployment.parameters.model_dump(mode="json")
        secrets_redacted = False
        extra_args: list[str] = []
        for argument in raw.get("extra_args") or []:
            safe_argument, changed = cls._sanitize_text(str(argument))
            extra_args.append(safe_argument)
            secrets_redacted = secrets_redacted or changed
        raw["extra_args"] = extra_args
        for key in ("image", "served_model_name", "local_model_path"):
            value = raw.get(key)
            if isinstance(value, str):
                raw[key], changed = cls._sanitize_text(value)
                secrets_redacted = secrets_redacted or changed
        return raw, secrets_redacted

    @classmethod
    def _sanitize_text(cls, value: str) -> tuple[str, bool]:
        if "\x00" in value:
            raise DeploymentTemplateInvalid("launch metadata contains a null byte")
        changed = False

        def replace_assignment(match: re.Match[str]) -> str:
            nonlocal changed
            name = match.group("name")
            if not cls._is_secret_name(name):
                return match.group(0)
            changed = True
            prefix = match.group("prefix") or ""
            return f'{prefix}{name}="${{{name}:?required at launch}}"'

        def replace_flag(match: re.Match[str]) -> str:
            nonlocal changed
            changed = True
            name = match.group("flag").removeprefix("--").replace("-", "_").upper()
            return f'{match.group("flag")}{match.group("separator")}"${{UNIFORM_{name}:?required}}"'

        def replace_short_flag(match: re.Match[str]) -> str:
            nonlocal changed
            changed = True
            return f'{match.group("prefix")}"${{UNIFORM_LOGIN_SECRET:?required}}"'

        sanitized = _SECRET_ASSIGNMENT.sub(replace_assignment, value)
        sanitized = _SECRET_FLAG.sub(replace_flag, sanitized)
        sanitized = _SHORT_SECRET_FLAG.sub(replace_short_flag, sanitized)
        for pattern, replacement in (
            (_BEARER_SECRET, 'Bearer "${UNIFORM_AUTH_TOKEN:?required}"'),
            (_KNOWN_TOKEN, '"${UNIFORM_REUSE_SECRET:?required}"'),
            (_URL_CREDENTIALS, r"\g<scheme><credentials-required>@"),
            (_URL_QUERY_SECRET, r"\g<prefix>${UNIFORM_URL_SECRET:?required}"),
        ):
            sanitized, count = pattern.subn(replacement, sanitized)
            changed = changed or count > 0
        residual_scan = re.sub(r"\$\{[^}\r\n]*\}", "", sanitized)
        if _RESIDUAL_SECRET.search(residual_scan):
            return "[REDACTED: credential-bearing value requires manual reconstruction]", True
        return sanitized, changed

    @staticmethod
    def _is_secret_name(name: str) -> bool:
        normalized = name.upper()
        parts = set(normalized.split("_"))
        if parts & {"TOKEN", "PASSWORD", "SECRET", "CREDENTIAL", "AUTHORIZATION"}:
            return True
        return any(
            marker in normalized
            for marker in ("API_KEY", "ACCESS_KEY", "PRIVATE_KEY", "AUTH_TOKEN")
        )

    @staticmethod
    def _reference_has_no_credentials(url: str) -> bool:
        parsed = urlsplit(url)
        if parsed.username is not None or parsed.password is not None:
            return False
        secret_keys = {
            "token",
            "access_token",
            "password",
            "secret",
            "api_key",
            "apikey",
            "key",
            "signature",
        }
        return not any(key.casefold() in secret_keys for key, _ in parse_qsl(parsed.query))

    def _container_script(
        self,
        *,
        action: str,
        container_name: str,
        container_id: str | None,
        image: str,
    ) -> str:
        header = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"# Uniform managed {action} artifact.\n"
            "# Container identity and image are checked before any lifecycle action.\n"
        )
        if container_id is None or not self._valid_container_name(container_name) or not image:
            return (
                f"{header}"
                "echo 'This historical deployment has no safe container binding.' >&2\n"
                "exit 64\n"
            )
        docker = shlex.quote(self.docker_binary)
        name = shlex.quote(container_name)
        expected_id = shlex.quote(container_id)
        expected_image = shlex.quote(image)
        common = (
            f"docker_bin={docker}\n"
            f"container_name={name}\n"
            f"expected_id={expected_id}\n"
            f"expected_image={expected_image}\n"
            'actual_id="$("${docker_bin}" inspect --format \'{{.Id}}\' '
            '"${container_name}")"\n'
            'actual_image="$("${docker_bin}" inspect --format \'{{.Config.Image}}\' '
            '"${container_name}")"\n'
            'auto_remove="$("${docker_bin}" inspect --format \'{{.HostConfig.AutoRemove}}\' '
            '"${container_name}")"\n'
            'if [[ "${actual_id}" != "${expected_id}" ]]; then\n'
            '  echo "Container identity changed; refusing lifecycle action." >&2\n'
            "  exit 65\n"
            "fi\n"
            'if [[ "${actual_image}" != "${expected_image}" ]]; then\n'
            '  echo "Container image changed; refusing lifecycle action." >&2\n'
            "  exit 66\n"
            "fi\n"
            'if [[ "${auto_remove}" != "false" ]]; then\n'
            '  echo "Auto-remove container cannot use reusable lifecycle artifacts." >&2\n'
            "  exit 67\n"
            "fi\n"
        )
        if action == "start":
            command = (
                'running="$("${docker_bin}" inspect --format \'{{.State.Running}}\' '
                '"${container_name}")"\n'
                'if [[ "${running}" == "true" ]]; then\n'
                '  echo "Container is already running."\n'
                "  exit 0\n"
                "fi\n"
                'exec "${docker_bin}" start "${container_name}"\n'
            )
        elif action == "stop":
            command = (
                'running="$("${docker_bin}" inspect --format \'{{.State.Running}}\' '
                '"${container_name}")"\n'
                'if [[ "${running}" != "true" ]]; then\n'
                '  echo "Container is already stopped."\n'
                "  exit 0\n"
                "fi\n"
                'exec "${docker_bin}" stop --time 30 "${container_name}"\n'
            )
        else:
            raise ValueError("deployment artifact action must be start or stop")
        return f"{header}{common}{command}"
