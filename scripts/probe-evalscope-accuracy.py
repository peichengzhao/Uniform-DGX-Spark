#!/usr/bin/env python3
"""Run the stage-36 EvalScope compatibility probe against a local model service."""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

EXPECTED_EVALSCOPE_VERSION = "0.0.0_dev"
EXPECTED_EVALSCOPE_COMMIT = "ccb9019ff56fd7dcc8602f4d17b706b31228edca"
EXPECTED_REPORT_SCHEMA_VERSION = 2
DATASET_NAME = "gsm8k"
SAMPLE_LIMIT = 2


class ProbeError(RuntimeError):
    """Raised when a compatibility requirement is not met."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ProbeError(message)


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    _require(isinstance(value, dict), f"{label} must be a JSON object")
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    _require(isinstance(value, list), f"{label} must be a JSON array")
    return value


def _require_int(value: Any, label: str) -> int:
    _require(isinstance(value, int) and not isinstance(value, bool), f"{label} must be an integer")
    return value


def _require_finite_number(value: Any, label: str) -> float:
    _require(
        isinstance(value, (int, float)) and not isinstance(value, bool), f"{label} must be numeric"
    )
    result = float(value)
    _require(math.isfinite(result), f"{label} must be finite")
    return result


def validate_local_base_url(base_url: str) -> str:
    """Return a normalized loopback OpenAI-compatible v1 URL."""
    parsed = urlparse(base_url)
    _require(parsed.scheme == "http", "the compatibility probe only accepts local http URLs")
    _require(
        parsed.username is None and parsed.password is None, "base URL must not contain credentials"
    )
    _require(
        parsed.query == "" and parsed.fragment == "",
        "base URL must not contain a query or fragment",
    )
    _require(parsed.port is not None, "base URL must include an explicit local port")
    hostname = parsed.hostname or ""
    if hostname != "localhost":
        try:
            _require(ipaddress.ip_address(hostname).is_loopback, "base URL host must be loopback")
        except ValueError as exc:
            raise ProbeError("base URL host must be localhost or a loopback IP address") from exc
    path = parsed.path.rstrip("/")
    _require(path in ("", "/v1"), "base URL path must be empty or /v1")
    return f"http://{parsed.netloc}/v1"


def _request_json(request: Request, timeout_seconds: float) -> dict[str, Any]:
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            payload = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise ProbeError(
            f"{request.method} {request.full_url} returned HTTP {exc.code}: {detail}"
        ) from exc
    except (URLError, TimeoutError) as exc:
        raise ProbeError(f"{request.method} {request.full_url} failed: {exc}") from exc
    try:
        return _require_mapping(json.loads(payload), f"{request.full_url} response")
    except json.JSONDecodeError as exc:
        raise ProbeError(f"{request.full_url} did not return valid JSON") from exc


def probe_openai_compatibility(
    base_url: str, model: str, timeout_seconds: float = 15.0
) -> dict[str, Any]:
    """Check the two OpenAI-compatible endpoints EvalScope needs."""
    normalized_url = validate_local_base_url(base_url)
    models = _request_json(Request(f"{normalized_url}/models", method="GET"), timeout_seconds)
    model_entries = _require_list(models.get("data"), "/v1/models.data")
    model_ids = [
        entry.get("id")
        for entry in model_entries
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    ]
    _require(model in model_ids, f"model {model!r} is not advertised by /v1/models")

    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "Return the number 4 and nothing else."}],
            "temperature": 0,
            "max_tokens": 16,
        }
    ).encode("utf-8")
    request = Request(
        f"{normalized_url}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer EMPTY"},
        method="POST",
    )
    completion = _request_json(request, timeout_seconds)
    choices = _require_list(completion.get("choices"), "/v1/chat/completions.choices")
    _require(bool(choices), "/v1/chat/completions.choices must not be empty")
    first_choice = _require_mapping(choices[0], "/v1/chat/completions.choices[0]")
    message = _require_mapping(
        first_choice.get("message"), "/v1/chat/completions.choices[0].message"
    )
    _require(isinstance(message.get("content"), str), "chat completion content must be text")
    return {
        "base_url": normalized_url,
        "model": model,
        "models_endpoint": "compatible",
        "chat_completions_endpoint": "compatible",
    }


def build_evalscope_command(
    evalscope_command: Path,
    base_url: str,
    model: str,
    work_dir: Path,
) -> list[str]:
    """Build the frozen stage-36 command without a shell or a real API key."""
    normalized_url = validate_local_base_url(base_url)
    _require(
        model.strip() == model and bool(model),
        "model name must be non-empty and have no outer whitespace",
    )
    return [
        str(evalscope_command),
        "eval",
        "--model",
        model,
        "--model-id",
        model,
        "--api-url",
        normalized_url,
        "--eval-type",
        "openai_api",
        "--datasets",
        DATASET_NAME,
        "--dataset-hub",
        "modelscope",
        "--dataset-dir",
        str(work_dir / "datasets"),
        "--dataset-args",
        json.dumps({DATASET_NAME: {"few_shot_num": 0}}, separators=(",", ":")),
        "--limit",
        str(SAMPLE_LIMIT),
        "--eval-batch-size",
        "1",
        "--generation-config",
        json.dumps({"temperature": 0, "max_tokens": 2048, "timeout": 300}, separators=(",", ":")),
        "--work-dir",
        str(work_dir),
        "--no-timestamp",
        "--enable-progress-tracker",
        "--no-collect-perf",
        "--seed",
        "42",
    ]


def build_evalscope_environment(work_dir: Path) -> dict[str, str]:
    """Keep every evaluation cache inside the stage-36 run directory."""
    environment = os.environ.copy()
    environment.update(
        {
            "MODELSCOPE_CACHE": str(work_dir / "modelscope-cache"),
            "EVALSCOPE_CACHE": str(work_dir / "evalscope-cache"),
            "HF_HOME": str(work_dir / "huggingface-cache"),
        }
    )
    return environment


def verify_evalscope_identity(evalscope_command: Path, evalscope_source: Path) -> dict[str, str]:
    """Verify the executable version and the pinned development source commit."""
    version_result = subprocess.run(
        [str(evalscope_command), "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    version_output = version_result.stdout.strip()
    _require(
        version_output == f"evalscope {EXPECTED_EVALSCOPE_VERSION}",
        f"expected evalscope {EXPECTED_EVALSCOPE_VERSION}, got {version_output!r}",
    )
    commit_result = subprocess.run(
        ["git", "-C", str(evalscope_source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    commit = commit_result.stdout.strip()
    _require(
        commit == EXPECTED_EVALSCOPE_COMMIT,
        f"expected EvalScope commit {EXPECTED_EVALSCOPE_COMMIT}, got {commit}",
    )
    return {"version": EXPECTED_EVALSCOPE_VERSION, "commit": commit}


def _identity_key(identity: dict[str, Any]) -> tuple[str, str, str]:
    name = identity.get("name")
    aggregation = identity.get("aggregation")
    dimensions = identity.get("dimensions")
    _require(isinstance(name, str), "metric identity.name must be text")
    _require(isinstance(aggregation, str), "metric identity.aggregation must be text")
    _require(isinstance(dimensions, dict), "metric identity.dimensions must be an object")
    return name, aggregation, json.dumps(dimensions, sort_keys=True, separators=(",", ":"))


def validate_evalscope_report(
    report: dict[str, Any], expected_model: str | None = None
) -> dict[str, Any]:
    """Validate the frozen minimum EvalScope v2 GSM8K report contract."""
    schema_version = _require_int(report.get("schema_version"), "schema_version")
    _require(
        schema_version == EXPECTED_REPORT_SCHEMA_VERSION,
        f"unsupported EvalScope report schema_version={schema_version}",
    )
    _require(
        report.get("dataset_name") == DATASET_NAME, f"report dataset_name must be {DATASET_NAME!r}"
    )
    model_name = report.get("model_name")
    _require(
        isinstance(model_name, str) and bool(model_name), "report model_name must be non-empty text"
    )
    if expected_model is not None:
        _require(
            model_name == expected_model,
            f"report model_name {model_name!r} does not match {expected_model!r}",
        )

    execution = _require_mapping(report.get("execution_summary"), "execution_summary")
    requested = _require_int(execution.get("requested"), "execution_summary.requested")
    succeeded = _require_int(execution.get("succeeded"), "execution_summary.succeeded")
    errored = _require_int(execution.get("errored"), "execution_summary.errored")
    incomplete = execution.get("incomplete")
    _require(isinstance(incomplete, bool), "execution_summary.incomplete must be boolean")
    _require(
        requested == SAMPLE_LIMIT, f"expected {SAMPLE_LIMIT} requested samples, got {requested}"
    )
    _require(
        succeeded == SAMPLE_LIMIT and errored == 0 and not incomplete,
        "both GSM8K samples must complete successfully",
    )

    primary_identity = _require_mapping(
        report.get("primary_metric_identity"), "primary_metric_identity"
    )
    primary_key = _identity_key(primary_identity)
    metrics = _require_list(report.get("metrics"), "metrics")
    matching_metrics = []
    for index, raw_metric in enumerate(metrics):
        metric = _require_mapping(raw_metric, f"metrics[{index}]")
        identity = _require_mapping(metric.get("identity"), f"metrics[{index}].identity")
        if _identity_key(identity) == primary_key:
            matching_metrics.append(metric)
    _require(len(matching_metrics) == 1, "primary_metric_identity must match exactly one metric")
    primary_metric = matching_metrics[0]
    _require(primary_identity.get("name") == "accuracy", "GSM8K primary metric must be accuracy")
    semantics = _require_mapping(primary_metric.get("semantics"), "primary metric semantics")
    _require(
        semantics.get("semantic_id") == "quality.accuracy.ratio", "unexpected Accuracy semantic_id"
    )
    _require(semantics.get("kind") == "quality", "Accuracy metric must be a quality metric")
    score = _require_finite_number(primary_metric.get("score"), "primary metric score")
    _require(0 <= score <= 1, "Accuracy must be in the [0, 1] range")
    metric_num = _require_int(primary_metric.get("num"), "primary metric num")
    _require(metric_num == SAMPLE_LIMIT, f"primary metric must contain {SAMPLE_LIMIT} samples")
    return {
        "schema_version": schema_version,
        "model_name": model_name,
        "dataset_name": DATASET_NAME,
        "metric": "Accuracy",
        "score": score,
        "requested": requested,
        "succeeded": succeeded,
        "errored": errored,
        "complete": not incomplete,
    }


def validate_progress(progress: dict[str, Any]) -> dict[str, Any]:
    """Validate the completed EvalScope progress snapshot."""
    _require(progress.get("status") == "completed", "progress status must be completed")
    _require(progress.get("pipeline") == "eval", "progress pipeline must be eval")
    total = _require_int(progress.get("total_count"), "progress.total_count")
    processed = _require_int(progress.get("processed_count"), "progress.processed_count")
    percent = _require_finite_number(progress.get("percent"), "progress.percent")
    _require(total > 0 and processed >= total, "progress must process its complete workload")
    _require(percent >= 100, "completed progress must be at least 100 percent")
    return {
        "status": "completed",
        "total_count": total,
        "processed_count": processed,
        "percent": percent,
    }


def load_json(path: Path) -> dict[str, Any]:
    """Load one JSON object from disk."""
    try:
        return _require_mapping(json.loads(path.read_text(encoding="utf-8")), str(path))
    except FileNotFoundError as exc:
        raise ProbeError(f"file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ProbeError(f"invalid JSON in {path}: {exc}") from exc


def find_report(work_dir: Path) -> Path:
    """Find the sole GSM8K report under an EvalScope run directory."""
    reports = sorted((work_dir / "reports").glob(f"**/{DATASET_NAME}.json"))
    _require(
        len(reports) == 1,
        f"expected one GSM8K report below {work_dir / 'reports'}, found {len(reports)}",
    )
    return reports[0]


def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    """Execute the live compatibility probe and return a sanitized result."""
    work_dir = args.work_dir.resolve()
    _require(
        not (work_dir / "reports").exists(),
        "work directory already contains reports; use a fresh directory",
    )
    work_dir.mkdir(parents=True, exist_ok=True)
    identity = verify_evalscope_identity(
        args.evalscope_command.resolve(), args.evalscope_source.resolve()
    )
    endpoint_result = probe_openai_compatibility(args.base_url, args.model, args.request_timeout)
    command = build_evalscope_command(
        args.evalscope_command.resolve(), args.base_url, args.model, work_dir
    )
    config_snapshot = {
        "evalscope": identity,
        "dataset": DATASET_NAME,
        "limit": SAMPLE_LIMIT,
        "few_shot_num": 0,
        "model": args.model,
        "base_url": validate_local_base_url(args.base_url),
        "generation_config": {"temperature": 0, "max_tokens": 2048, "timeout": 300},
        "eval_batch_size": 1,
        "seed": 42,
        "collect_perf": False,
        "cache_dirs": {
            "dataset": str(work_dir / "datasets"),
            "modelscope": str(work_dir / "modelscope-cache"),
            "evalscope": str(work_dir / "evalscope-cache"),
            "huggingface": str(work_dir / "huggingface-cache"),
        },
    }
    (work_dir / "uniform-probe-config.json").write_text(
        json.dumps(config_snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    log_path = work_dir / "evalscope.log"
    with log_path.open("w", encoding="utf-8") as log_file:
        completed = subprocess.run(
            command,
            check=False,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=args.eval_timeout,
            start_new_session=True,
            env=build_evalscope_environment(work_dir),
        )
    _require(
        completed.returncode == 0,
        f"EvalScope exited with code {completed.returncode}; inspect {log_path}",
    )
    progress_result = validate_progress(load_json(work_dir / "progress.json"))
    report_path = find_report(work_dir)
    report_result = validate_evalscope_report(load_json(report_path), expected_model=args.model)
    result = {
        "status": "passed",
        "evalscope": identity,
        "openai_compatibility": endpoint_result,
        "progress": progress_result,
        "report": report_result,
        "artifacts": {
            "config": str(work_dir / "uniform-probe-config.json"),
            "log": str(log_path),
            "progress": str(work_dir / "progress.json"),
            "report": str(report_path),
        },
    }
    result_path = work_dir / "uniform-probe-result.json"
    result_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return result


def validate_artifacts(args: argparse.Namespace) -> dict[str, Any]:
    """Validate saved report and optional progress artifacts without running inference."""
    result: dict[str, Any] = {
        "report": validate_evalscope_report(load_json(args.report), args.model)
    }
    if args.progress is not None:
        result["progress"] = validate_progress(load_json(args.progress))
    return result


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)

    validate_parser = subparsers.add_parser("validate", help="validate saved EvalScope artifacts")
    validate_parser.add_argument("--report", type=Path, required=True)
    validate_parser.add_argument("--progress", type=Path)
    validate_parser.add_argument("--model")

    run_parser = subparsers.add_parser("run", help="run the live GSM8K limit=2 probe")
    run_parser.add_argument(
        "--base-url", required=True, help="loopback OpenAI-compatible URL, ending in /v1"
    )
    run_parser.add_argument("--model", required=True, help="model id advertised by /v1/models")
    run_parser.add_argument(
        "--work-dir", type=Path, required=True, help="fresh output directory for this probe"
    )
    run_parser.add_argument("--evalscope-command", type=Path, required=True)
    run_parser.add_argument("--evalscope-source", type=Path, required=True)
    run_parser.add_argument("--request-timeout", type=float, default=300.0)
    run_parser.add_argument("--eval-timeout", type=float, default=1800.0)
    return parser


def main() -> int:
    """Run the selected probe action."""
    args = build_parser().parse_args()
    try:
        result = run_probe(args) if args.action == "run" else validate_artifacts(args)
    except (ProbeError, OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
