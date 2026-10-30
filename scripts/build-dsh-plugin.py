#!/usr/bin/env python3
"""Build a DeepSeek Harness bundle (.tgz) from the packaged MCP runtime."""

from __future__ import annotations

import argparse
import fnmatch
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "packaging" / "uniform-dgxspark-dsh-plugin"
HARNESS_SOURCE = ROOT / "src" / "uniform_dgxspark" / "harness_mcp.py"
HOST_SCRIPT = ROOT / "packaging" / "uniform-platform-plugin" / "host" / "enable-lan-access.sh"
FORBIDDEN_PARTS = {".git", ".venv", ".runtime", "__pycache__", "data", "node_modules"}
EXECUTABLES = {"configure.mjs", "launcher.mjs", "host/enable-lan-access.sh"}
REQUIRED_FILES = {
    "README.md",
    "README.zh.md",
    "package.json",
    "cordis.patch.yml",
    "config.mjs",
    "launcher.mjs",
    "configure.mjs",
    "runtime/uniform_harness_mcp.py",
    "locale/en.json",
    "locale/zh.json",
    "meta/detail.md",
    "meta/detail.zh.md",
    "icon.svg",
    "host/enable-lan-access.sh",
}


class BuildError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_version() -> str:
    match = re.search(
        r'^PLUGIN_VERSION\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?)"\s*$',
        HARNESS_SOURCE.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if not match:
        raise BuildError("cannot find PLUGIN_VERSION in harness_mcp.py")
    return match.group(1)


def ignore_template(_directory: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name in {"__pycache__", ".DS_Store"} or name.endswith((".pyc", ".tmp"))
    }


def compile_python(path: Path) -> None:
    try:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    except (OSError, SyntaxError, ValueError) as error:
        raise BuildError(f"Python syntax check failed for {path.name}: {error}") from error


def check_javascript(node: str | None, staging: Path) -> None:
    if not node:
        return
    command = Path(node).expanduser()
    for relative in ("config.mjs", "launcher.mjs", "configure.mjs"):
        completed = subprocess.run(
            [str(command), "--check", str(staging / relative)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise BuildError(
                f"JavaScript syntax check failed for {relative}: "
                f"{completed.stderr.strip() or completed.stdout.strip()}"
            )


def stage_package(staging: Path, version: str, node: str | None) -> dict[str, object]:
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(TEMPLATE_DIR, staging, ignore=ignore_template, copy_function=shutil.copy2)
    (staging / "runtime").mkdir(parents=True, exist_ok=True)
    (staging / "host").mkdir(parents=True, exist_ok=True)
    shutil.copy2(HARNESS_SOURCE, staging / "runtime" / "uniform_harness_mcp.py")
    shutil.copy2(HOST_SCRIPT, staging / "host" / "enable-lan-access.sh")

    manifest_template = (staging / "package.json.in").read_text(encoding="utf-8")
    manifest = json.loads(manifest_template.replace("__VERSION__", version))
    if manifest.get("version") != version:
        raise BuildError("package.json version substitution failed")
    (staging / "package.json.in").unlink()
    (staging / "package.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    missing = sorted(item for item in REQUIRED_FILES if not (staging / item).is_file())
    if missing:
        raise BuildError(f"bundle is missing: {', '.join(missing)}")
    if manifest.get("dsh", {}).get("bundle", {}).get("patch") != "./cordis.patch.yml":
        raise BuildError("package.json must declare dsh.bundle.patch = ./cordis.patch.yml")

    files = {
        path.relative_to(staging).as_posix(): path
        for path in staging.rglob("*")
        if path.is_file()
    }
    for relative in files:
        if set(PurePosixPath(relative).parts) & FORBIDDEN_PARTS:
            raise BuildError(f"forbidden path in bundle: {relative}")

    patterns = manifest.get("files")
    if not isinstance(patterns, list) or not all(isinstance(item, str) for item in patterns):
        raise BuildError("package.json files must be a list of path patterns")
    covered: set[str] = set()
    for pattern in patterns:
        matches = {
            relative
            for relative in files
            if fnmatch.fnmatchcase(relative, pattern)
            or relative.startswith(pattern.rstrip("/") + "/")
        }
        if not matches:
            raise BuildError(f"package.json files pattern matches nothing: {pattern}")
        covered.update(matches)
    uncovered = sorted(set(files) - covered - {"package.json"})
    if uncovered:
        raise BuildError(f"files are not declared in package.json: {', '.join(uncovered)}")

    compile_python(staging / "runtime" / "uniform_harness_mcp.py")
    check_javascript(node, staging)
    for relative in EXECUTABLES:
        (staging / relative).chmod(0o755)
    return manifest


def archive_name(version: str) -> str:
    return f"uniform-dgxspark-platform-{version}"


def write_tgz(staging: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as compressed:
                with tarfile.open(
                    fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT
                ) as archive:
                    for path in sorted(item for item in staging.rglob("*") if item.is_file()):
                        relative = path.relative_to(staging).as_posix()
                        data = path.read_bytes()
                        info = tarfile.TarInfo(f"package/{relative}")
                        info.size = len(data)
                        info.mode = 0o755 if relative in EXECUTABLES else 0o644
                        info.mtime = 0
                        info.uid = info.gid = 0
                        info.uname = info.gname = ""
                        import io

                        archive.addfile(info, io.BytesIO(data))
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_zip(staging: Path, destination: Path, folder: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for path in sorted(item for item in staging.rglob("*") if item.is_file()):
                relative = path.relative_to(staging).as_posix()
                info = zipfile.ZipInfo(f"{folder}/{relative}", date_time=(2020, 1, 1, 0, 0, 0))
                info.external_attr = ((path.stat().st_mode & 0o777) & 0xFFFF) << 16
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes(), compresslevel=9)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "dist"), help="output directory")
    parser.add_argument(
        "--node", default=os.environ.get("NODE"), help="Node.js executable for syntax checks"
    )
    parser.add_argument(
        "--keep-staging", action="store_true", help="keep the unpacked build directory"
    )
    args = parser.parse_args(argv)

    version = read_version()
    folder = archive_name(version)
    output = Path(args.out).expanduser().resolve()
    build_root = ROOT / "build" / "dsh-plugin"
    staging = build_root / folder
    build_root.mkdir(parents=True, exist_ok=True)
    stage_package(staging, version, args.node)

    tgz = output / f"{folder}.tgz"
    source_zip = output / f"{folder}-source.zip"
    write_tgz(staging, tgz)
    write_zip(staging, source_zip, folder)
    checksum = output / f"{folder}.sha256"
    checksum.write_text(
        f"{sha256(tgz)}  {tgz.name}\n{sha256(source_zip)}  {source_zip.name}\n",
        encoding="utf-8",
    )
    for path in (tgz, source_zip, checksum):
        path.chmod(0o644)
    if not args.keep_staging:
        shutil.rmtree(staging)
    print(f"version:         {version}")
    print(f"DSH bundle:      {tgz} ({tgz.stat().st_size} bytes)")
    print(f"source archive:  {source_zip} ({source_zip.stat().st_size} bytes)")
    print(f"sha256:          {checksum}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BuildError as error:
        print(f"build failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
