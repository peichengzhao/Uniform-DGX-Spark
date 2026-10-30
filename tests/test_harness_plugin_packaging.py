from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts" / "build-harness-plugin.py"


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
    completed = run([sys.executable, str(BUILDER), "--out", str(output)])
    assert completed.returncode == 0, completed.stderr
    zip_path = output / "uniform-platform-plugin-1.2.1.zip"
    tar_path = output / "uniform-platform-plugin-1.2.1.tar.gz"
    assert zip_path.is_file()
    assert tar_path.is_file()
    return zip_path, tar_path


def extract(zip_path: Path, tmp_path: Path) -> Path:
    destination = tmp_path / "unpacked"
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(destination)
    return destination / "uniform-platform-plugin-1.2.1"


def test_build_contains_manifest_and_hash_matches(tmp_path: Path) -> None:
    zip_path, tar_path = build(tmp_path)
    plugin = extract(zip_path, tmp_path)
    manifest = json.loads((plugin / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["version"] == "1.2.1"
    assert manifest["entrypoint"] == "uniform_harness_mcp.py"
    assert manifest["transport"] == "stdio"
    assert "manifest.json.in" not in manifest["files"]
    for relative, expected in manifest["files"].items():
        actual = hashlib.sha256((plugin / relative).read_bytes()).hexdigest()
        assert actual == expected, relative
    assert not any(
        part in {".git", ".venv", ".runtime", "__pycache__", "data"}
        for path in plugin.rglob("*")
        for part in path.parts
    )
    assert tar_path.stat().st_size > 0

    version = run([sys.executable, str(plugin / "uniform_harness_mcp.py"), "--version"])
    assert version.returncode == 0
    assert version.stdout.strip() == "1.2.1"


def test_build_is_reproducible(tmp_path: Path) -> None:
    first_zip, first_tar = build(tmp_path / "first")
    second_zip, second_tar = build(tmp_path / "second")
    assert first_zip.read_bytes() == second_zip.read_bytes()
    assert first_tar.read_bytes() == second_tar.read_bytes()


def test_installer_merges_json_and_toml_without_leaking_key(tmp_path: Path) -> None:
    zip_path, _ = build(tmp_path)
    plugin = extract(zip_path, tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    install_dir = home / "installed"
    key = "installer-secret-key"

    json_config = home / "mcp.json"
    json_config.write_text(
        json.dumps({"theme": "dark", "mcpServers": {"other": {"command": "other"}}}),
        encoding="utf-8",
    )
    completed = run(
        [
            sys.executable,
            str(plugin / "install.py"),
            "--url",
            "http://10.0.0.5:18180/",
            "--key",
            key,
            "--install-dir",
            str(install_dir),
            "--config",
            str(json_config),
            "--skip-check",
        ],
        env={"HOME": str(home), "XDG_DATA_HOME": str(home / "share")},
    )
    assert completed.returncode == 0, completed.stderr
    assert key not in completed.stdout
    assert key not in completed.stderr
    merged = json.loads(json_config.read_text(encoding="utf-8"))
    assert merged["theme"] == "dark"
    assert merged["mcpServers"]["other"]["command"] == "other"
    entry = merged["mcpServers"]["uniform-platform"]
    assert entry["env"]["UNIFORM_PLATFORM_URL"] == "http://10.0.0.5:18180"
    assert entry["env"]["UNIFORM_PLATFORM_API_KEY"] == key
    assert entry["args"] == [str(install_dir / "uniform_harness_mcp.py")]
    assert (install_dir / "uniform-harness-mcp.cmd").is_file()
    assert (install_dir / "uniform-harness-mcp.ps1").is_file()
    assert (install_dir / "uniform-harness-mcp").is_file()
    assert (install_dir / "mcp.json").stat().st_mode & 0o777 == 0o600
    assert (install_dir / "platform.env").stat().st_mode & 0o777 == 0o600

    toml_config = home / "mcp.toml"
    toml_config.write_text(
        '[ui]\ntheme = "dark"\n\n[mcp_servers.other]\ncommand = "other"\n',
        encoding="utf-8",
    )
    completed = run(
        [
            sys.executable,
            str(plugin / "install.py"),
            "--url",
            "http://10.0.0.5:18180",
            "--fallback-url",
            "https://uniform.example.com",
            "--key",
            key,
            "--install-dir",
            str(install_dir),
            "--config",
            str(toml_config),
            "--skip-check",
        ]
    )
    assert completed.returncode == 0, completed.stderr
    import tomllib

    merged_toml = tomllib.loads(toml_config.read_text(encoding="utf-8"))
    assert merged_toml["ui"]["theme"] == "dark"
    assert merged_toml["mcp_servers"]["other"]["command"] == "other"
    toml_entry = merged_toml["mcp_servers"]["uniform-platform"]
    assert toml_entry["env"]["UNIFORM_PLATFORM_FALLBACK_URL"] == "https://uniform.example.com"
    assert toml_entry["env"]["UNIFORM_PLATFORM_FALLBACK_API_KEY"] == key
    assert toml_config.read_text(encoding="utf-8").count("[mcp_servers.uniform-platform]") == 1


def test_print_only_redacts_key_and_writes_nothing(tmp_path: Path) -> None:
    zip_path, _ = build(tmp_path)
    plugin = extract(zip_path, tmp_path)
    install_dir = tmp_path / "never-created"
    key = "print-only-secret"
    completed = run(
        [
            sys.executable,
            str(plugin / "install.py"),
            "--url",
            "http://10.0.0.5:18180",
            "--key",
            key,
            "--install-dir",
            str(install_dir),
            "--print-only",
        ]
    )
    assert completed.returncode == 0, completed.stderr
    assert key not in completed.stdout
    assert "<redacted>" in completed.stdout
    assert not install_dir.exists()


@pytest.mark.parametrize(
    "url",
    ["ftp://host", "http://user:pass@host", "http://host/?token=1", "host:18180"],
)
def test_installer_rejects_unsafe_urls(tmp_path: Path, url: str) -> None:
    zip_path, _ = build(tmp_path)
    plugin = extract(zip_path, tmp_path)
    completed = run(
        [
            sys.executable,
            str(plugin / "install.py"),
            "--url",
            url,
            "--key",
            "key",
            "--install-dir",
            str(tmp_path / "install"),
            "--skip-check",
        ]
    )
    assert completed.returncode != 0
    assert "平台地址" in completed.stderr
