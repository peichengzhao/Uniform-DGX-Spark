from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = ROOT / ".runtime" / "node" / "bin" / "node"
NPM_CLI = ROOT / ".runtime" / "node" / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"


def test_repository_root_declares_dsh_bundle() -> None:
    manifest = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "uniform-dgxspark-platform"
    assert manifest["version"] == "1.2.3"
    assert manifest["dsh"]["bundle"]["patch"] == "./cordis.patch.yml"
    assert (ROOT / "cordis.patch.yml").is_file()

    yaml = pytest.importorskip("yaml")

    class Loader(yaml.SafeLoader):
        pass

    Loader.add_constructor(
        "tag:yaml.org,2002:js", lambda loader, node: loader.construct_scalar(node)
    )
    patch = yaml.load((ROOT / "cordis.patch.yml").read_text(encoding="utf-8"), Loader=Loader)
    row = patch[0]["insert"][0]
    assert row["id"] == "uniform-dgxspark-platform"
    assert row["name"] == "@deepseek-ai/dsh-mcp-client"
    assert row["config"]["args"] == [
        "node_modules/uniform-dgxspark-platform/packaging/"
        "uniform-dgxspark-dsh-plugin/launcher.mjs",
        "--stdio",
    ]


@pytest.mark.skipif(not NPM_CLI.is_file(), reason="bundled npm runtime is unavailable")
def test_npm_pack_includes_root_bundle_runtime(tmp_path: Path) -> None:
    cache = tmp_path / "npm-cache"
    cache.mkdir()
    environment = {
        **os.environ,
        "PATH": f"{NODE.parent}{os.pathsep}{os.environ.get('PATH', '')}",
        "npm_config_cache": str(cache),
    }
    completed = subprocess.run(
        [str(NODE), str(NPM_CLI), "pack", "--dry-run", "--json", "--ignore-scripts"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    [result] = json.loads(completed.stdout)
    paths = {entry["path"] for entry in result["files"]}
    assert {
        "package.json",
        "cordis.patch.yml",
        "packaging/uniform-dgxspark-dsh-plugin/launcher.mjs",
        "packaging/uniform-dgxspark-dsh-plugin/configure.mjs",
        "src/uniform_dgxspark/harness_mcp.py",
        "packaging/uniform-dgxspark-dsh-plugin/locale/zh.json",
    } <= paths
    assert not any("data/" in path or "__pycache__" in path for path in paths)


@pytest.mark.skipif(not NODE.is_file(), reason="bundled Node.js runtime is unavailable")
def test_repository_launcher_falls_back_to_source_runtime(tmp_path: Path) -> None:
    runtime = ROOT / "packaging" / "uniform-dgxspark-dsh-plugin" / "runtime"
    assert not (runtime / "uniform_harness_mcp.py").exists(), (
        "repository checkout should not contain a copied runtime"
    )
    config = tmp_path / "platform.env"
    config.write_text(
        "UNIFORM_PLATFORM_URL=http://127.0.0.1:9\n"
        "UNIFORM_PLATFORM_API_KEY=test-key\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [
            str(NODE),
            str(ROOT / "packaging" / "uniform-dgxspark-dsh-plugin" / "launcher.mjs"),
            "--stdio",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "UNIFORM_PLATFORM_CONFIG": str(config),
            "UNIFORM_PYTHON": sys.executable,
        },
    )
    request = json.dumps(
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
    ) + "\n"
    stdout, stderr = process.communicate(request, timeout=20)
    assert process.returncode == 0, stderr
    response = json.loads(stdout)
    assert response["result"]["serverInfo"]["name"] == "uniform_dgxspark"
