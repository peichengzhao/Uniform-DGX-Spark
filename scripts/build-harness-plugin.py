#!/usr/bin/env python3
"""Build the standalone MCP plugin package from the project source of truth."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "packaging" / "uniform-platform-plugin"
HARNESS_SOURCE = ROOT / "src" / "uniform_dgxspark" / "harness_mcp.py"
REQUIRED_FILES = {
    "README.md",
    "install.py",
    "install.sh",
    "install.ps1",
    "manifest.json.in",
    "host/enable-lan-access.sh",
}
FORBIDDEN_PARTS = {".git", ".venv", ".runtime", "__pycache__", "data", "node_modules"}
EXECUTABLE_FILES = {"install.sh", "host/enable-lan-access.sh"}


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


def ignore_template(directory: str, names: list[str]) -> set[str]:
    ignored = {
        name for name in names if name in {"__pycache__", ".DS_Store"} or name.endswith(".pyc")
    }
    if Path(directory).resolve() == TEMPLATE_DIR.resolve():
        ignored.add("manifest.json")
    return ignored


def compile_python(path: Path) -> None:
    try:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    except (OSError, SyntaxError, ValueError) as error:
        raise BuildError(f"Python syntax check failed for {path.name}: {error}") from error


def stage_package(staging: Path, version: str) -> None:
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(TEMPLATE_DIR, staging, ignore=ignore_template, copy_function=shutil.copy2)
    shutil.copy2(HARNESS_SOURCE, staging / "uniform_harness_mcp.py")
    if not (staging / "uniform_harness_mcp.py").is_file():
        raise BuildError("harness_mcp.py copy failed")

    missing = sorted(item for item in REQUIRED_FILES if not (staging / item).is_file())
    if missing:
        raise BuildError(f"template is missing: {', '.join(missing)}")

    manifest_template = (staging / "manifest.json.in").read_text(encoding="utf-8")
    manifest_template = manifest_template.replace("__VERSION__", version)
    manifest = json.loads(manifest_template)
    if manifest.get("version") != version:
        raise BuildError("manifest version substitution failed")
    (staging / "manifest.json.in").unlink()

    files: dict[str, str] = {}
    for path in sorted(item for item in staging.rglob("*") if item.is_file()):
        relative = path.relative_to(staging).as_posix()
        parts = set(PurePosixPath(relative).parts)
        if parts & FORBIDDEN_PARTS:
            raise BuildError(f"forbidden path in package: {relative}")
        files[relative] = sha256(path)
    manifest["files"] = files
    manifest["generated_by"] = "scripts/build-harness-plugin.py"
    (staging / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    for relative in ("install.py", "uniform_harness_mcp.py"):
        compile_python(staging / relative)
    (staging / "install.py").chmod(0o755)
    for relative in EXECUTABLE_FILES:
        (staging / relative).chmod(0o755)


def archive_name(version: str) -> str:
    return f"uniform-platform-plugin-{version}"


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
            files = sorted(item for item in staging.rglob("*") if item.is_file())
            for path in files:
                relative = path.relative_to(staging).as_posix()
                info = zipfile.ZipInfo(f"{folder}/{relative}", date_time=(2020, 1, 1, 0, 0, 0))
                mode = path.stat().st_mode & 0o777
                info.external_attr = (mode & 0xFFFF) << 16
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes(), compresslevel=9)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_tar(staging: Path, destination: Path, folder: str) -> None:
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
                        info = archive.gettarinfo(str(path), arcname=f"{folder}/{relative}")
                        info.mtime = 0
                        info.uid = info.gid = 0
                        info.uname = info.gname = ""
                        with path.open("rb") as handle:
                            archive.addfile(info, handle)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "dist"), help="output directory")
    parser.add_argument(
        "--keep-staging", action="store_true", help="keep the unpacked build directory"
    )
    args = parser.parse_args(argv)

    version = read_version()
    folder = archive_name(version)
    output = Path(args.out).expanduser().resolve()
    build_root = ROOT / "build"
    build_root.mkdir(parents=True, exist_ok=True)
    staging = build_root / folder
    stage_package(staging, version)

    zip_path = output / f"{folder}.zip"
    tar_path = output / f"{folder}.tar.gz"
    write_zip(staging, zip_path, folder)
    write_tar(staging, tar_path, folder)
    checksums = [
        f"{sha256(zip_path)}  {zip_path.name}",
        f"{sha256(tar_path)}  {tar_path.name}",
    ]
    checksum_path = output / f"{folder}.sha256"
    checksum_path.write_text("\n".join(checksums) + "\n", encoding="utf-8")
    for path in (zip_path, tar_path, checksum_path):
        path.chmod(0o644)

    if not args.keep_staging:
        shutil.rmtree(staging)
    print(f"version: {version}")
    print(f"zip:     {zip_path} ({zip_path.stat().st_size} bytes)")
    print(f"tar.gz:  {tar_path} ({tar_path.stat().st_size} bytes)")
    print(f"sha256:  {checksum_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BuildError as error:
        print(f"build failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
