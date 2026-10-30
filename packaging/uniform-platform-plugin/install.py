#!/usr/bin/env python3
"""Install the Uniform platform MCP plugin into an MCP-capable AI harness.

The installer is deliberately dependency-free. It only copies the bundled
stdio MCP server, writes a private configuration snippet, and optionally merges
that entry into an existing JSON or TOML MCP configuration.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

MIN_PYTHON = (3, 10)
HERE = Path(__file__).resolve().parent
SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
TOML_HEADER_RE = re.compile(r"^\s*\[\[?\s*([^\[\]]+?)\s*\]\]?\s*(?:#.*)?$")


class InstallerError(RuntimeError):
    """A user-actionable installation error."""


def _die(message: str, code: int = 1) -> None:
    print(f"错误：{message}", file=sys.stderr)
    raise SystemExit(code)


def _read_manifest() -> dict[str, Any]:
    path = HERE / "manifest.json"
    if not path.is_file():
        raise InstallerError("安装包不完整：缺少 manifest.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise InstallerError(f"manifest.json 无法读取：{error}") from error
    if not isinstance(data, dict):
        raise InstallerError("manifest.json 格式无效")
    return data


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_bundle(manifest: dict[str, Any]) -> str:
    harness = HERE / "uniform_harness_mcp.py"
    if not harness.is_file():
        raise InstallerError("安装包不完整：缺少 uniform_harness_mcp.py")
    expected = (manifest.get("files") or {}).get("uniform_harness_mcp.py")
    if expected and _sha256(harness) != expected:
        raise InstallerError("插件文件校验失败，安装包可能被修改或损坏")
    version = str(manifest.get("version") or "").strip()
    if not version:
        raise InstallerError("manifest.json 缺少 version")
    try:
        completed = subprocess.run(
            [sys.executable, str(harness), "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise InstallerError(f"无法运行插件入口：{error}") from error
    if completed.returncode != 0 or completed.stdout.strip() != version:
        raise InstallerError("插件入口版本与 manifest.json 不一致")
    return version


def _normalize_url(value: str) -> str:
    value = value.strip()
    if not value:
        raise InstallerError("平台地址不能为空")
    if any(char.isspace() for char in value):
        raise InstallerError("平台地址不能包含空格")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise InstallerError("平台地址必须以 http:// 或 https:// 开头")
    if not parsed.hostname:
        raise InstallerError("平台地址缺少主机名")
    if parsed.username or parsed.password:
        raise InstallerError("平台地址不能包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise InstallerError("平台地址不能包含查询参数或片段")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _validate_secret(value: str, label: str) -> str:
    if "\n" in value or "\r" in value:
        raise InstallerError(f"{label}不能包含换行")
    return value


def _read_secret_file(path: str, label: str) -> str:
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError as error:
        raise InstallerError(f"无法读取{label}文件：{error}") from error
    return _validate_secret(text.rstrip("\r\n"), label)


def _default_install_dir() -> Path:
    override = os.environ.get("UNIFORM_HARNESS_HOME")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "uniform-harness-mcp"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "uniform-harness-mcp"


def _backup(path: Path, enabled: bool) -> None:
    if not enabled or not path.exists():
        return
    backup = path.with_name(path.name + ".bak")
    shutil.copy2(path, backup)
    _chmod_private(backup)


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        return


def _atomic_write(path: Path, text: str, *, private: bool = True) -> None:
    path = path.expanduser().resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if private:
            _chmod_private(temporary_path)
        os.replace(temporary_path, path)
        if private:
            _chmod_private(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _split_toml_key(value: str) -> list[str]:
    parts: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    escaped = False
    for char in value:
        if quote == '"' and escaped:
            buffer.append(char)
            escaped = False
            continue
        if quote == '"' and char == "\\":
            buffer.append(char)
            escaped = True
            continue
        if quote:
            buffer.append(char)
            if char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            buffer.append(char)
        elif char == ".":
            parts.append("".join(buffer).strip())
            buffer = []
        else:
            buffer.append(char)
    parts.append("".join(buffer).strip())
    result: list[str] = []
    for part in parts:
        if len(part) >= 2 and part[0] == part[-1] == '"':
            try:
                result.append(json.loads(part))
            except ValueError:
                result.append(part[1:-1])
        elif len(part) >= 2 and part[0] == part[-1] == "'":
            result.append(part[1:-1])
        else:
            result.append(part)
    return result


def _remove_toml_table(text: str, target: list[str]) -> str:
    kept: list[str] = []
    dropping = False
    for line in text.splitlines(keepends=True):
        match = TOML_HEADER_RE.match(line)
        if match:
            path = _split_toml_key(match.group(1))
            dropping = path[: len(target)] == target
            if dropping:
                continue
        if not dropping:
            kept.append(line)
    return "".join(kept)


def _merge_toml(path: Path, server_name: str, entry: dict[str, Any], backup: bool) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if re.search(r"(?m)^\s*mcp_servers\s*=", text):
        raise InstallerError(
            "配置使用了内联 mcp_servers 表，无法安全合并；请改为标准表或手动粘贴片段"
        )
    root = "mcp_servers"
    for line in text.splitlines():
        match = TOML_HEADER_RE.match(line)
        if not match:
            continue
        path_parts = _split_toml_key(match.group(1))
        if len(path_parts) >= 2 and path_parts[0] in {"mcp_servers", "mcpServers"}:
            root = path_parts[0]
            break
    target = [root, server_name]
    text = _remove_toml_table(text, target).rstrip()
    env = entry["env"]
    block = [
        f"[{root}.{server_name}]",
        f"command = {_toml_string(entry['command'])}",
        f"args = [{', '.join(_toml_string(value) for value in entry['args'])}]",
        "",
        f"[{root}.{server_name}.env]",
    ]
    block.extend(f"{key} = {_toml_string(value)}" for key, value in env.items())
    merged = (text + "\n\n" if text else "") + "\n".join(block) + "\n"
    try:
        import tomllib

        tomllib.loads(merged)
    except ImportError:
        pass
    except ValueError as error:
        raise InstallerError(f"合并后的 TOML 无法解析，已保持原文件不变：{error}") from error
    _backup(path, backup)
    _atomic_write(path, merged)


def _merge_json(path: Path, server_name: str, entry: dict[str, Any], backup: bool) -> None:
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError) as error:
            raise InstallerError(f"现有 JSON 配置无法解析，已保持原文件不变：{error}") from error
        if not isinstance(raw, dict):
            raise InstallerError("现有 JSON 配置的顶层必须是对象")
        existing = raw
    servers = existing.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise InstallerError("现有配置中的 mcpServers 必须是对象")
    servers[server_name] = entry
    _backup(path, backup)
    _atomic_write(path, json.dumps(existing, indent=2, ensure_ascii=False) + "\n")


def _merge_config(
    path: Path,
    server_name: str,
    entry: dict[str, Any],
    config_format: str,
    backup: bool,
) -> None:
    path = path.expanduser().resolve(strict=False)
    if config_format == "auto":
        suffix = path.suffix.lower()
        if suffix == ".json":
            config_format = "json"
        elif suffix == ".toml":
            config_format = "toml"
        else:
            raise InstallerError("无法判断配置文件格式，请使用 .json/.toml 后缀或 --config-format")
    if config_format == "json":
        _merge_json(path, server_name, entry, backup)
    else:
        _merge_toml(path, server_name, entry, backup)
    print(f"已合并 MCP 配置：{path}")


def _redacted_entry(entry: dict[str, Any]) -> dict[str, Any]:
    copied = json.loads(json.dumps(entry))
    for key in copied.get("env", {}):
        if "KEY" in key.upper() or "TOKEN" in key.upper():
            copied["env"][key] = "<redacted>"
    return copied


def _launcher_scripts(install_dir: Path) -> dict[str, str]:
    python = str(Path(sys.executable).resolve())
    launch_py = str(install_dir / "launch.py")
    posix = f"#!/bin/sh\nexec {shlex.quote(python)} {shlex.quote(launch_py)} \"$@\"\n"
    windows = f'@echo off\r\n"{python}" "{launch_py}" %*\r\nexit /b %ERRORLEVEL%\r\n'
    powershell = (
        "$ErrorActionPreference = 'Stop'\r\n"
        f"& '{python}' '{launch_py}' @args\r\n"
        "exit $LASTEXITCODE\r\n"
    )
    return {
        "uniform-harness-mcp": posix,
        "uniform-harness-mcp.cmd": windows,
        "uniform-harness-mcp.ps1": powershell,
    }


LAUNCH_PY = '''#!/usr/bin/env python3
"""Load the private plugin environment, then run the bundled MCP server."""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
env_path = root / "platform.env"
for raw in env_path.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    os.environ[key] = value

harness = root / "uniform_harness_mcp.py"
sys.argv = [str(harness), *sys.argv[1:]]
runpy.run_path(str(harness), run_name="__main__")
'''


def _write_env(path: Path, env: dict[str, str]) -> None:
    lines = []
    for key, value in env.items():
        if "\n" in value or "\r" in value:
            raise InstallerError(f"环境变量 {key} 不能包含换行")
        lines.append(f"{key}={value}")
    _atomic_write(path, "\n".join(lines) + "\n")


def _install_files(
    install_dir: Path,
    server_name: str,
    entry: dict[str, Any],
) -> None:
    install_dir.mkdir(parents=True, exist_ok=True)
    source = HERE / "uniform_harness_mcp.py"
    target = install_dir / "uniform_harness_mcp.py"
    if source.resolve() != target.resolve():
        shutil.copy2(source, target)
    try:
        os.chmod(target, 0o644)
    except OSError:
        pass
    _atomic_write(install_dir / "launch.py", LAUNCH_PY)
    _write_env(install_dir / "platform.env", entry["env"])
    for name, content in _launcher_scripts(install_dir).items():
        path = install_dir / name
        _atomic_write(path, content, private=False)
        if not name.endswith(('.cmd', '.ps1')):
            try:
                os.chmod(path, 0o755)
            except OSError:
                pass
    snippet = {"mcpServers": {server_name: entry}}
    _atomic_write(
        install_dir / "mcp.json",
        json.dumps(snippet, indent=2, ensure_ascii=False) + "\n",
    )


def _redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def _run_self_test(installed: Path, env: dict[str, str], secrets: list[str]) -> bool:
    merged_env = os.environ.copy()
    merged_env.update(env)
    try:
        completed = subprocess.run(
            [sys.executable, str(installed), "--self-test"],
            capture_output=True,
            text=True,
            timeout=30,
            env=merged_env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print("自检超时：30 秒内没有收到平台响应。", file=sys.stderr)
        return False
    except OSError as error:
        print(f"自检无法启动：{error}", file=sys.stderr)
        return False
    output = _redact((completed.stdout + "\n" + completed.stderr).strip(), secrets)
    if completed.returncode == 0:
        print("自检通过：插件已能访问平台。")
        return True
    print("自检未通过，请检查平台地址、API Key、防火墙和平台监听地址。", file=sys.stderr)
    if output:
        print(output[:1200], file=sys.stderr)
    return False


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="安装 Uniform DGX Spark 平台 MCP 插件（DeepSeek Harness 等 AI 工具通用）",
    )
    parser.add_argument("--url", help="平台地址，例如 http://10.0.0.5:18180")
    parser.add_argument("--fallback-url", help="可选备用地址，例如 https://uniform.example.com")
    parser.add_argument("--key", help="平台 API Key；建议改用 --key-file 或交互输入")
    parser.add_argument("--key-file", help="包含平台 API Key 的文件（读取第一行）")
    parser.add_argument("--fallback-key", help="备用地址的 API Key，默认与主地址相同")
    parser.add_argument("--fallback-key-file", help="包含备用地址 API Key 的文件")
    parser.add_argument("--config", help="要合并的 Harness MCP 配置（.json 或 .toml）")
    parser.add_argument(
        "--config-format",
        choices=("auto", "json", "toml"),
        default="auto",
        help="配置文件格式，默认按后缀判断",
    )
    parser.add_argument("--name", default="uniform-platform", help="MCP server 名称")
    parser.add_argument("--install-dir", help="插件安装目录")
    parser.add_argument("--print-only", action="store_true", help="只打印脱敏配置，不写文件")
    parser.add_argument("--skip-check", action="store_true", help="跳过安装后的连通性自检")
    parser.add_argument("--strict", action="store_true", help="自检失败时返回非零退出码")
    parser.add_argument("--no-backup", action="store_true", help="合并配置前不生成 .bak")
    parser.add_argument("--version", action="store_true", help="显示安装包版本")
    return parser.parse_args(argv)


def _pick_key(args: argparse.Namespace, print_only: bool) -> str:
    if args.key is not None and args.key_file:
        raise InstallerError("--key 与 --key-file 只能使用一个")
    if args.key_file:
        return _read_secret_file(args.key_file, "平台 API Key")
    if args.key is not None:
        return _validate_secret(args.key, "平台 API Key")
    if print_only:
        return ""
    print("平台 API Key（输入时不会显示；平台未启用鉴权可直接回车）：", end="", flush=True)
    return _validate_secret(getpass.getpass(""), "平台 API Key")


def _pick_fallback_key(args: argparse.Namespace, primary: str) -> str:
    if args.fallback_key is not None and args.fallback_key_file:
        raise InstallerError("--fallback-key 与 --fallback-key-file 只能使用一个")
    if args.fallback_key_file:
        return _read_secret_file(args.fallback_key_file, "备用地址 API Key")
    if args.fallback_key is not None:
        return _validate_secret(args.fallback_key, "备用地址 API Key")
    return primary


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < MIN_PYTHON:
        _die(f"需要 Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 或以上")
    args = _parse_args(argv or sys.argv[1:])
    manifest = _read_manifest()
    version = _verify_bundle(manifest)
    if args.version:
        print(version)
        return 0
    if not SERVER_NAME_RE.fullmatch(args.name):
        raise InstallerError("--name 只能包含字母、数字、点、下划线和短横线，且不超过 64 字符")

    url = args.url
    if not url:
        if args.print_only:
            url = "http://127.0.0.1:18180"
        else:
            url = input("平台地址（例如 http://10.0.0.5:18180）：").strip()
    url = _normalize_url(url)
    fallback_url = _normalize_url(args.fallback_url) if args.fallback_url else ""
    if fallback_url and fallback_url == url:
        raise InstallerError("备用地址不能与主地址相同")

    key = _pick_key(args, args.print_only)
    fallback_key = _pick_fallback_key(args, key) if fallback_url else ""
    # Adding the plugin is meaningful even for an unauthenticated platform.
    if not key and not args.print_only:
        print("提示：未提供 API Key；仅当平台未设置 UNIFORM_API_TOKEN 时才可访问。")

    install_dir = (
        Path(args.install_dir).expanduser() if args.install_dir else _default_install_dir()
    )
    install_dir = install_dir.resolve(strict=False)
    installed_harness = install_dir / "uniform_harness_mcp.py"
    env = {"UNIFORM_PLATFORM_URL": url, "UNIFORM_PLATFORM_API_KEY": key}
    if fallback_url:
        env["UNIFORM_PLATFORM_FALLBACK_URL"] = fallback_url
        env["UNIFORM_PLATFORM_FALLBACK_API_KEY"] = fallback_key
    entry = {
        "command": str(Path(sys.executable).resolve()),
        "args": [str(installed_harness)],
        "env": env,
    }

    if args.print_only:
        snippet = {"mcpServers": {args.name: _redacted_entry(entry)}}
        print(json.dumps(snippet, indent=2, ensure_ascii=False))
        print(
            "\n提示：上面已隐藏 API Key。去掉 --print-only 运行后，"
            "安装器会写入权限为 0600 的配置。"
        )
        return 0

    secrets = [key, fallback_key]
    try:
        _install_files(install_dir, args.name, entry)
    except OSError as error:
        raise InstallerError(f"写入插件文件失败：{error}") from error
    if args.config:
        try:
            _merge_config(
                Path(args.config),
                args.name,
                entry,
                args.config_format,
                not args.no_backup,
            )
        except OSError as error:
            raise InstallerError(f"写入 Harness 配置失败：{error}") from error

    passed = True
    if not args.skip_check:
        passed = _run_self_test(installed_harness, env, secrets)

    print("\n安装完成。")
    print(f"  插件版本：{version}")
    print(f"  插件目录：{install_dir}")
    print(f"  启动命令：{install_dir / 'uniform-harness-mcp'}")
    print(f"  配置片段：{install_dir / 'mcp.json'}（权限 0600，含 API Key）")
    print(f"  平台地址：{url}")
    if fallback_url:
        print(f"  备用地址：{fallback_url}")
    if args.config:
        print(f"  Harness 配置：{Path(args.config).expanduser().resolve(strict=False)}")
    else:
        print("  下一步：把 mcp.json 里的 mcpServers 条目加入 DeepSeek Harness 的 MCP 配置，")
        print("          或用 --config <配置文件> 重新运行以自动合并。")
    if not passed and args.strict:
        return 3
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except InstallerError as error:
        _die(str(error))
