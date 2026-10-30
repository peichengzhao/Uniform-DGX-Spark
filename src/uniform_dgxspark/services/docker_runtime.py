from __future__ import annotations

import json
import re
import selectors
import shlex
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..schemas import DeploymentRead, LocalImageRead


class RuntimeErrorBase(RuntimeError):
    pass


class DockerCommandError(RuntimeErrorBase):
    def __init__(self, message: str, *, output: str = "") -> None:
        super().__init__(message)
        self.output = output


class DockerCommandCancelled(RuntimeErrorBase):
    pass


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class DiscoveredAIService:
    session_id: str
    display_name: str
    container_name: str
    container_id: str
    image: str
    engine: str
    port: int
    model_name: str
    launch_command: str


class CommandRunner(Protocol):
    def run(self, arguments: list[str], *, timeout: int = 120) -> CommandResult: ...


class SubprocessCommandRunner:
    def run(self, arguments: list[str], *, timeout: int = 120) -> CommandResult:
        try:
            completed = subprocess.run(
                arguments,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise DockerCommandError(
                f"command timed out after {timeout} seconds: {arguments[0]}"
            ) from error
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    def stream(
        self,
        arguments: list[str],
        *,
        timeout: int,
        output_callback: Callable[[str], None],
        cancel_check: Callable[[], bool],
    ) -> CommandResult:
        process = subprocess.Popen(
            arguments,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        if process.stdout is None:
            process.kill()
            raise DockerCommandError("failed to capture command output")
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        output: list[str] = []
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if cancel_check():
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise DockerCommandCancelled("command cancelled by user")
                if time.monotonic() >= deadline:
                    process.terminate()
                    raise DockerCommandError(f"command timed out after {timeout} seconds")
                for key, _ in selector.select(timeout=0.2):
                    line = key.fileobj.readline()
                    if line:
                        output.append(line)
                        output_callback(line.rstrip("\r\n"))
            remainder = process.stdout.read()
            if remainder:
                output.append(remainder)
                for line in remainder.splitlines():
                    output_callback(line)
            return CommandResult(process.returncode or 0, "".join(output), "")
        finally:
            selector.close()
            process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


class DockerRuntime:
    label_key = "com.uniform-dgxspark.deployment-id"

    def __init__(
        self,
        *,
        docker_binary: str = "docker",
        runner: CommandRunner | None = None,
    ) -> None:
        self.docker_binary = docker_binary
        self.runner = runner or SubprocessCommandRunner()
        self.local_http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @staticmethod
    def container_name(deployment_id: str) -> str:
        return f"uniform-vllm-{deployment_id.replace('-', '')[:16]}"

    def check_available(self) -> None:
        result = self._run(["version", "--format", "{{.Server.Version}}"])
        if result.returncode != 0:
            raise DockerCommandError("Docker daemon is unavailable", output=result.stderr)

    def ensure_port_available(self, port: int) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind(("127.0.0.1", port))
            except OSError as error:
                raise RuntimeErrorBase(f"port {port} is already in use") from error

    def image_exists(self, image: str) -> bool:
        return self._run(["image", "inspect", image]).returncode == 0

    def list_images(self) -> list[LocalImageRead]:
        result = self._run(["image", "ls", "--no-trunc", "--format", "{{json .}}"])
        if result.returncode != 0:
            raise DockerCommandError("failed to list local images", output=result.stderr)
        images: list[LocalImageRead] = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            repository = str(raw.get("Repository") or "<none>")
            tag = str(raw.get("Tag") or "<none>")
            reference = (
                f"{repository}:{tag}"
                if repository != "<none>" and tag != "<none>"
                else str(raw.get("ID") or "")
            )
            containers_raw = str(raw.get("Containers") or "")
            images.append(
                LocalImageRead(
                    image_id=str(raw.get("ID") or ""),
                    repository=repository,
                    tag=tag,
                    reference=reference,
                    digest=(
                        None if raw.get("Digest") in {None, "", "<none>"} else str(raw["Digest"])
                    ),
                    size=str(raw.get("Size") or "unknown"),
                    created_at=str(raw.get("CreatedAt") or ""),
                    created_since=str(raw.get("CreatedSince") or ""),
                    containers=(int(containers_raw) if containers_raw.isdigit() else None),
                    vllm_candidate="vllm" in f"{repository}:{tag}".lower(),
                )
            )
        return images

    def ensure_image(
        self,
        image: str,
        *,
        progress_callback: Callable[[str], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> None:
        if self.image_exists(image):
            return
        if progress_callback and cancel_check and isinstance(self.runner, SubprocessCommandRunner):
            result = self.runner.stream(
                [self.docker_binary, "pull", image],
                timeout=3600,
                output_callback=progress_callback,
                cancel_check=cancel_check,
            )
        else:
            result = self._run(["pull", image], timeout=3600)
        if result.returncode != 0:
            raise DockerCommandError(
                f"failed to pull image {image}", output=result.stderr or result.stdout
            )

    def start(self, deployment: DeploymentRead, model_path: Path) -> str:
        name = self.container_name(deployment.id)
        self.remove_if_managed(deployment.id)
        arguments = self._start_arguments(deployment, model_path)
        result = self._run(arguments, timeout=120)
        if result.returncode != 0:
            raise DockerCommandError("failed to start vLLM container", output=result.stderr)
        return name

    def container_metadata(self, deployment: DeploymentRead) -> dict[str, str | None]:
        name = self.container_name(deployment.id)
        result = self._run(["inspect", "--format", "{{json .}}", name])
        if result.returncode != 0:
            raise DockerCommandError("failed to inspect vLLM container", output=result.stderr)
        try:
            details = json.loads(result.stdout)
            model_mount = next(
                mount
                for mount in details.get("Mounts", [])
                if mount.get("Destination") == "/models/model"
            )
            model_path = Path(str(model_mount["Source"]))
        except (json.JSONDecodeError, KeyError, StopIteration, TypeError) as error:
            raise DockerCommandError("invalid Docker inspect response") from error
        arguments = self._start_arguments(deployment, model_path)
        return {
            "container_name": name,
            "container_id": str(details.get("Id") or "") or None,
            "launch_command": shlex.join([self.docker_binary, *arguments]),
        }

    def _start_arguments(
        self,
        deployment: DeploymentRead,
        model_path: Path,
    ) -> list[str]:
        parameters = deployment.parameters
        name = self.container_name(deployment.id)
        arguments = [
            "run",
            "-d",
            "--name",
            name,
            "--gpus",
            "all",
            "--ipc=host",
            "--label",
            f"{self.label_key}={deployment.id}",
            "-p",
            f"127.0.0.1:{parameters.port}:8000",
            "--mount",
            f"type=bind,src={model_path},dst=/models/model,readonly",
            "--entrypoint",
            "vllm",
            parameters.image,
            "serve",
            "/models/model",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
            "--served-model-name",
            parameters.served_model_name or deployment.model_id,
            "--gpu-memory-utilization",
            str(parameters.gpu_memory_utilization),
        ]
        if parameters.max_model_len is not None:
            arguments.extend(["--max-model-len", str(parameters.max_model_len)])
        if parameters.trust_remote_code:
            arguments.append("--trust-remote-code")
        for extra_argument in parameters.extra_args:
            arguments.extend(shlex.split(extra_argument))
        return arguments

    def is_running(self, deployment_id: str) -> bool:
        name = self.container_name(deployment_id)
        result = self._run(["inspect", "--format", "{{.State.Running}}", name])
        return result.returncode == 0 and result.stdout.strip() == "true"

    def is_named_container_running(self, container_name: str) -> bool:
        normalized = container_name.removeprefix("/")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", normalized):
            return False
        result = self._run(["inspect", "--format", "{{.State.Running}}", normalized])
        return result.returncode == 0 and result.stdout.strip() == "true"

    def named_container_identity(self, container_name: str) -> dict[str, str | bool] | None:
        normalized = container_name.removeprefix("/")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", normalized):
            return None
        result = self._run(["inspect", "--format", "{{json .}}", normalized], timeout=10)
        if result.returncode != 0:
            return None
        try:
            details = json.loads(result.stdout)
            container_id = str(details["Id"])
            image = str(details["Config"]["Image"])
            running = bool(details["State"]["Running"])
            auto_remove = details["HostConfig"]["AutoRemove"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return None
        if (
            not re.fullmatch(r"[0-9a-f]{64}", container_id.casefold())
            or not image
            or not isinstance(auto_remove, bool)
        ):
            return None
        return {
            "container_id": container_id.casefold(),
            "image": image,
            "running": running,
            "auto_remove": auto_remove,
        }

    def container_host_bindings(self, container_name: str, host_port: int) -> list[str]:
        """Return Docker host addresses explicitly publishing ``host_port``."""
        normalized = container_name.removeprefix("/")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", normalized):
            return []
        result = self._run(
            ["inspect", "--format", "{{json .NetworkSettings.Ports}}", normalized],
            timeout=5,
        )
        if result.returncode != 0:
            return []
        try:
            bindings = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            return []
        addresses: list[str] = []
        if not isinstance(bindings, dict):
            return addresses
        for values in bindings.values():
            if not isinstance(values, list):
                continue
            for value in values:
                if not isinstance(value, dict) or str(value.get("HostPort")) != str(host_port):
                    continue
                addresses.append(str(value.get("HostIp") or "0.0.0.0"))
        return addresses

    def discover_ai_services(self) -> list[DiscoveredAIService]:
        """Find running AI services created by an autonomous Uniform session.

        Both labels are required so an unrelated user container is never adopted merely
        because its name resembles one of ours. Rank workers are intentionally excluded;
        only the API-serving rank represents a service in the UI.
        """
        result = self._run(
            [
                "ps",
                "--filter",
                "label=uniform.session_id",
                "--filter",
                "label=uniform.deployment",
                "--format",
                "{{.ID}}",
            ]
        )
        if result.returncode != 0:
            return []
        services: list[DiscoveredAIService] = []
        for container_id in dict.fromkeys(result.stdout.split()):
            inspected = self._run(["inspect", "--format", "{{json .}}", container_id])
            if inspected.returncode != 0:
                continue
            try:
                details = json.loads(inspected.stdout)
                labels = details["Config"]["Labels"] or {}
                session_id = str(labels["uniform.session_id"]).strip()
                display_name = str(labels["uniform.deployment"]).strip()
                name = str(details["Name"]).removeprefix("/")
                image = str(details["Config"]["Image"])
                command = [
                    *[str(value) for value in details["Config"].get("Entrypoint") or []],
                    *[str(value) for value in details["Config"].get("Cmd") or []],
                ]
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
            if not (
                re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}", session_id)
                and display_name
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name)
            ):
                continue
            node_rank = self._command_option(command, "--node-rank")
            if node_rank not in {None, "0"}:
                continue
            port = self._published_host_port(details) or self._integer_command_option(
                command, "--port"
            )
            if port is None:
                continue
            model_name = self._command_option(command, "--served-model-name") or display_name
            command_text = " ".join(command).lower()
            engine = "sglang" if "sglang" in command_text else "vllm"
            services.append(
                DiscoveredAIService(
                    session_id=session_id,
                    display_name=display_name,
                    container_name=name,
                    container_id=str(details.get("Id") or container_id),
                    image=image,
                    engine=engine,
                    port=port,
                    model_name=model_name,
                    launch_command=shlex.join(command),
                )
            )
        return services

    @staticmethod
    def _command_option(command: list[str], option: str) -> str | None:
        for index, value in enumerate(command):
            if value == option and index + 1 < len(command):
                return command[index + 1]
            if value.startswith(f"{option}="):
                return value.split("=", 1)[1]
        return None

    @classmethod
    def _integer_command_option(cls, command: list[str], option: str) -> int | None:
        value = cls._command_option(command, option)
        try:
            port = int(value) if value is not None else None
        except ValueError:
            return None
        return port if port is not None and 1 <= port <= 65535 else None

    @staticmethod
    def _published_host_port(details: dict[str, object]) -> int | None:
        try:
            ports = details["NetworkSettings"]["Ports"]  # type: ignore[index]
            for bindings in ports.values():  # type: ignore[union-attr]
                for binding in bindings or []:
                    port = int(binding["HostPort"])
                    if 1 <= port <= 65535:
                        return port
        except (KeyError, TypeError, ValueError):
            return None
        return None

    def openai_models(self, port: int, *, timeout: float = 2) -> list[str]:
        try:
            with self.local_http.open(
                f"http://127.0.0.1:{port}/v1/models", timeout=timeout
            ) as response:
                body = json.load(response)
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            json.JSONDecodeError,
            TypeError,
        ):
            return []
        if response.status != 200 or not isinstance(body, dict):
            return []
        data = body.get("data")
        if not isinstance(data, list):
            return []
        return [str(item["id"]) for item in data if isinstance(item, dict) and item.get("id")]

    def smoke_test_service(
        self,
        port: int,
        *,
        model_name: str | None = None,
        endpoint: str = "completions",
        timeout: float = 30,
    ) -> str:
        models = self.openai_models(port, timeout=min(timeout, 10))
        if not models:
            raise RuntimeErrorBase("model discovery returned no served models")
        selected_model = model_name if model_name in models else models[0]
        if endpoint == "chat_completions":
            path = "/v1/chat/completions"
            request_body = {
                "model": selected_model,
                "messages": [{"role": "user", "content": "test"}],
                "max_tokens": 1,
                "temperature": 0,
            }
        else:
            path = "/v1/completions"
            request_body = {
                "model": selected_model,
                "prompt": "test",
                "max_tokens": 1,
                "temperature": 0,
            }
        payload = json.dumps(request_body).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.local_http.open(request, timeout=timeout) as response:
                body = json.load(response)
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            json.JSONDecodeError,
            TypeError,
        ) as error:
            raise RuntimeErrorBase("OpenAI-compatible inference smoke test failed") from error
        if response.status != 200 or not isinstance(body, dict) or not body.get("choices"):
            raise RuntimeErrorBase("inference smoke test returned an invalid response")
        return selected_model

    def exists(self, deployment_id: str) -> bool:
        return self._run(["inspect", self.container_name(deployment_id)]).returncode == 0

    def stop(self, deployment_id: str, *, remove: bool = False) -> None:
        name = self.container_name(deployment_id)
        if not self.exists(deployment_id):
            return
        self._assert_managed(deployment_id)
        stop_result = self._run(["stop", "--timeout", "20", name], timeout=40)
        if stop_result.returncode != 0 and "is not running" not in stop_result.stderr:
            raise DockerCommandError("failed to stop vLLM container", output=stop_result.stderr)
        if remove:
            remove_result = self._run(["rm", name])
            if remove_result.returncode != 0:
                raise DockerCommandError(
                    "failed to remove vLLM container", output=remove_result.stderr
                )

    def remove_if_managed(self, deployment_id: str) -> None:
        if not self.exists(deployment_id):
            return
        self._assert_managed(deployment_id)
        result = self._run(["rm", "-f", self.container_name(deployment_id)], timeout=40)
        if result.returncode != 0:
            raise DockerCommandError("failed to remove stale container", output=result.stderr)

    def logs(self, deployment_id: str, *, tail: int = 200) -> str:
        return self.logs_for_container(self.container_name(deployment_id), tail=tail)

    def logs_for_container(self, container_name: str, *, tail: int = 200) -> str:
        normalized = container_name.removeprefix("/")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", normalized):
            raise DockerCommandError("invalid container name")
        result = self._run(["logs", "--timestamps", "--tail", str(tail), normalized])
        if result.returncode != 0:
            raise DockerCommandError("failed to read container logs", output=result.stderr)
        return result.stdout + result.stderr

    def stats(self, deployment_id: str) -> dict[str, str]:
        if not self.exists(deployment_id):
            return {}
        result = self._run(
            [
                "stats",
                "--no-stream",
                "--format",
                "{{json .}}",
                self.container_name(deployment_id),
            ]
        )
        if result.returncode != 0:
            return {}
        try:
            raw = json.loads(result.stdout)
        except json.JSONDecodeError:
            return {}
        return {
            "cpu_percent": raw.get("CPUPerc", ""),
            "memory": raw.get("MemUsage", ""),
            "memory_percent": raw.get("MemPerc", ""),
            "network_io": raw.get("NetIO", ""),
            "block_io": raw.get("BlockIO", ""),
            "pids": raw.get("PIDs", ""),
        }

    def health(self, port: int, *, timeout: float = 2) -> bool:
        try:
            with self.local_http.open(
                f"http://127.0.0.1:{port}/health", timeout=timeout
            ) as response:
                return response.status == 200
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            return False

    def smoke_test(self, deployment: DeploymentRead) -> None:
        model_name = deployment.parameters.served_model_name or deployment.model_id
        payload = json.dumps(
            {
                "model": model_name,
                "prompt": "test",
                "max_tokens": 1,
                "temperature": 0,
            }
        ).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{deployment.parameters.port}/v1/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.local_http.open(request, timeout=30) as response:
                body = json.load(response)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            raise RuntimeErrorBase("vLLM smoke test request failed") from error
        if response.status != 200 or not body.get("choices"):
            raise RuntimeErrorBase("vLLM smoke test returned an invalid response")

    def _assert_managed(self, deployment_id: str) -> None:
        name = self.container_name(deployment_id)
        result = self._run(
            [
                "inspect",
                "--format",
                f'{{{{ index .Config.Labels "{self.label_key}" }}}}',
                name,
            ]
        )
        if result.returncode != 0 or result.stdout.strip() != deployment_id:
            raise DockerCommandError(
                f"refusing to modify unmanaged container {name}", output=result.stderr
            )

    def _run(self, arguments: list[str], *, timeout: int = 120) -> CommandResult:
        return self.runner.run([self.docker_binary, *arguments], timeout=timeout)
