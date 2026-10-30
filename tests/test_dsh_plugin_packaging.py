from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts" / "build-dsh-plugin.py"
NODE = ROOT / ".runtime" / "node" / "bin" / "node"
VERSION = "1.2.1"


def run(
    command: list[str], *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


def build(tmp_path: Path) -> tuple[Path, Path]:
    output = tmp_path / "dist"
    command = [sys.executable, str(BUILDER), "--out", str(output)]
    if NODE.is_file():
        command.extend(["--node", str(NODE)])
    completed = run(command)
    assert completed.returncode == 0, completed.stderr
    tgz = output / f"uniform-dgxspark-platform-{VERSION}.tgz"
    source_zip = output / f"uniform-dgxspark-platform-{VERSION}-source.zip"
    assert tgz.is_file()
    assert source_zip.is_file()
    return tgz, source_zip


def unpack(tgz: Path, tmp_path: Path) -> Path:
    destination = tmp_path / "unpacked"
    destination.mkdir()
    with tarfile.open(tgz, "r:gz") as archive:
        members = archive.getmembers()
        assert members
        assert all(member.name.startswith("package/") for member in members)
        archive.extractall(destination, filter="data")
    return destination / "package"


def test_bundle_manifest_and_patch_are_installable(tmp_path: Path) -> None:
    tgz, _ = build(tmp_path)
    package = unpack(tgz, tmp_path)
    manifest = json.loads((package / "package.json").read_text(encoding="utf-8"))

    assert manifest["name"] == "uniform-dgxspark-platform"
    assert manifest["version"] == VERSION
    assert manifest["private"] is True
    assert manifest["dsh"]["bundle"]["patch"] == "./cordis.patch.yml"
    assert (package / "cordis.patch.yml").is_file()
    assert (package / "locale" / "zh.json").is_file()
    assert (package / "icon.svg").is_file()
    assert (package / "launcher.mjs").is_file()
    assert (package / "configure.mjs").is_file()
    assert (package / "runtime" / "uniform_harness_mcp.py").is_file()

    yaml = pytest.importorskip("yaml")

    class Loader(yaml.SafeLoader):
        pass

    Loader.add_constructor(
        "tag:yaml.org,2002:js", lambda loader, node: loader.construct_scalar(node)
    )
    patch = yaml.load((package / "cordis.patch.yml").read_text(encoding="utf-8"), Loader=Loader)
    row = patch[0]["insert"][0]
    assert row["id"] == "uniform-platform"
    assert row["name"] == "@deepseek-ai/dsh-mcp-client"
    config = row["config"]
    assert config["serverName"] == "uniform_platform"
    assert config["transport"] == "stdio"
    assert config["command"] == "process.execPath"
    assert config["cwd"] == "ctx.profileContext.dir"
    assert config["args"] == [
        "node_modules/uniform-dgxspark-platform/launcher.mjs",
        "--stdio",
    ]
    assert config["env"] == {"ELECTRON_RUN_AS_NODE": "1"}


def test_bundle_contains_no_secrets_or_development_files(tmp_path: Path) -> None:
    tgz, _ = build(tmp_path)
    package = unpack(tgz, tmp_path)
    relative_paths = [path.relative_to(package).as_posix() for path in package.rglob("*")]
    assert not any(
        part in {".git", ".venv", ".runtime", "__pycache__", "data", "node_modules"}
        for path in relative_paths
        for part in Path(path).parts
    )
    assert "platform.env" not in relative_paths
    assert not any(path.endswith((".pyc", ".key", ".pem")) for path in relative_paths)
    assert "API_KEY=" not in (package / "README.zh.md").read_text(encoding="utf-8")


def test_bundle_build_is_reproducible(tmp_path: Path) -> None:
    first_tgz, first_zip = build(tmp_path / "first")
    second_tgz, second_zip = build(tmp_path / "second")
    assert hashlib.sha256(first_tgz.read_bytes()).digest() == hashlib.sha256(
        second_tgz.read_bytes()
    ).digest()
    assert first_zip.read_bytes() == second_zip.read_bytes()


@pytest.mark.skipif(not NODE.is_file(), reason="bundled Node.js runtime is unavailable")
def test_launcher_starts_a_standard_mcp_server(tmp_path: Path) -> None:
    tgz, _ = build(tmp_path)
    package = unpack(tgz, tmp_path)
    config = tmp_path / "platform.env"
    config.write_text(
        "UNIFORM_PLATFORM_URL=http://127.0.0.1:9\n"
        "UNIFORM_PLATFORM_API_KEY=test-key\n",
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "UNIFORM_PLATFORM_CONFIG": str(config),
        "UNIFORM_PYTHON": sys.executable,
    }
    process = subprocess.Popen(
        [str(NODE), str(package / "launcher.mjs"), "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    requests = "\n".join(
        [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                }
            ),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
        ]
    ) + "\n"
    stdout, stderr = process.communicate(requests, timeout=20)
    assert process.returncode == 0, stderr
    responses = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    assert responses[0]["result"]["serverInfo"]["name"] == "uniform_platform"
    tools = {tool["name"] for tool in responses[1]["result"]["tools"]}
    assert {"platform_status", "deploy_model", "publish_service"} <= tools


@pytest.mark.skipif(not NODE.is_file(), reason="bundled Node.js runtime is unavailable")
def test_configure_writes_private_env_and_redacts_key(tmp_path: Path) -> None:
    tgz, _ = build(tmp_path)
    package = unpack(tgz, tmp_path)
    config = tmp_path / "config" / "platform.env"
    key_file = tmp_path / "uniform.key"
    key = "configure-test-secret"
    key_file.write_text(key + "\n", encoding="utf-8")

    completed = run(
        [
            str(NODE),
            str(package / "configure.mjs"),
            "--url",
            "http://10.0.0.5:18180/",
            "--fallback-url",
            "https://uniform.example.com",
            "--key-file",
            str(key_file),
            "--config",
            str(config),
            "--skip-check",
        ]
    )
    assert completed.returncode == 0, completed.stderr
    assert key not in completed.stdout
    assert key not in completed.stderr
    assert config.stat().st_mode & 0o777 == 0o600
    values = dict(
        line.split("=", 1)
        for line in config.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )
    assert values["UNIFORM_PLATFORM_URL"] == "http://10.0.0.5:18180"
    assert values["UNIFORM_PLATFORM_API_KEY"] == key
    assert values["UNIFORM_PLATFORM_FALLBACK_URL"] == "https://uniform.example.com"
    assert values["UNIFORM_PLATFORM_FALLBACK_API_KEY"] == key

    shown = run(
        [str(NODE), str(package / "configure.mjs"), "--show", "--config", str(config)]
    )
    assert shown.returncode == 0, shown.stderr
    assert key not in shown.stdout
    assert "<redacted>" in shown.stdout


@pytest.mark.skipif(not NODE.is_file(), reason="bundled Node.js runtime is unavailable")
def test_print_only_does_not_create_configuration(tmp_path: Path) -> None:
    tgz, _ = build(tmp_path)
    package = unpack(tgz, tmp_path)
    config = tmp_path / "not-created" / "platform.env"
    key = "print-only-secret"
    completed = run(
        [
            str(NODE),
            str(package / "configure.mjs"),
            "--url",
            "http://10.0.0.5:18180",
            "--key",
            key,
            "--config",
            str(config),
            "--print-only",
        ]
    )
    assert completed.returncode == 0, completed.stderr
    assert key not in completed.stdout
    assert "<redacted>" in completed.stdout
    assert not config.exists()


@pytest.mark.parametrize(
    "url",
    ["ftp://host", "http://user:pass@host", "http://host/?token=1", "host:18180"],
)
@pytest.mark.skipif(not NODE.is_file(), reason="bundled Node.js runtime is unavailable")
def test_configure_rejects_unsafe_urls(tmp_path: Path, url: str) -> None:
    tgz, _ = build(tmp_path)
    package = unpack(tgz, tmp_path)
    completed = run(
        [
            str(NODE),
            str(package / "configure.mjs"),
            "--url",
            url,
            "--key",
            "key",
            "--config",
            str(tmp_path / "config.env"),
            "--skip-check",
        ]
    )
    assert completed.returncode != 0
    assert "platform address" in completed.stderr or "Invalid URL" in completed.stderr
