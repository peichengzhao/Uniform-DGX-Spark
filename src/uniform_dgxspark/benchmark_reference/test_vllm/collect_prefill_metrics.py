#!/usr/bin/env python3
"""Capture vLLM Prometheus counters and calculate per-case Prefill metrics."""

from __future__ import annotations

import argparse
import json
import math
import re
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

PREFILL_TIME = "vllm:request_prefill_time_seconds"
PREFILL_TOKENS = "vllm:request_prefill_kv_computed_tokens"
PROMPT_TOKENS = "vllm:prompt_tokens_total"
LINE_PATTERN = re.compile(
    r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([^}]*)\})?\s+"
    r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?|[-+]?Inf|NaN)$"
)
LE_PATTERN = re.compile(r'(?:^|,)le="([^"]+)"(?:,|$)')


def parse_prometheus(text: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for raw_line in text.splitlines():
        match = LINE_PATTERN.match(raw_line.strip())
        if match is None:
            continue
        name, labels, raw_value = match.groups()
        if name not in {
            f"{PREFILL_TIME}_sum",
            f"{PREFILL_TIME}_count",
            f"{PREFILL_TIME}_bucket",
            f"{PREFILL_TOKENS}_sum",
            f"{PREFILL_TOKENS}_count",
            PROMPT_TOKENS,
        }:
            continue
        key = name
        if name.endswith("_bucket"):
            le_match = LE_PATTERN.search(labels or "")
            if le_match is None:
                continue
            key = f"{name}|{le_match.group(1)}"
        value = float(raw_value)
        if math.isfinite(value):
            values[key] = values.get(key, 0.0) + value
    return values


def capture(url: str, timeout: float = 5.0) -> dict[str, object]:
    request = urllib.request.Request(url, headers={"Accept": "text/plain"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:  # noqa: S310
        body = response.read().decode("utf-8", errors="replace")
    return {
        "source_url": url,
        "collected_at": datetime.now(UTC).isoformat(),
        "metrics": parse_prometheus(body),
    }


def counter_delta(before: dict[str, float], after: dict[str, float], key: str) -> float | None:
    if key not in before or key not in after:
        return None
    value = after[key] - before[key]
    return value if value >= 0 else None


def histogram_quantile(
    before: dict[str, float], after: dict[str, float], quantile: float
) -> float | None:
    prefix = f"{PREFILL_TIME}_bucket|"
    buckets: list[tuple[float, float]] = []
    for key in after:
        if not key.startswith(prefix) or key not in before:
            continue
        raw_bound = key.removeprefix(prefix)
        bound = math.inf if raw_bound == "+Inf" else float(raw_bound)
        count = after[key] - before[key]
        if count < 0:
            return None
        buckets.append((bound, count))
    buckets.sort(key=lambda item: item[0])
    total = next((count for bound, count in buckets if math.isinf(bound)), None)
    if not total:
        return None
    rank = quantile * total
    previous_bound = 0.0
    previous_count = 0.0
    for bound, count in buckets:
        if count < rank:
            if math.isfinite(bound):
                previous_bound = bound
            previous_count = count
            continue
        if not math.isfinite(bound):
            return previous_bound
        bucket_count = count - previous_count
        if bucket_count <= 0:
            return bound
        fraction = (rank - previous_count) / bucket_count
        return previous_bound + (bound - previous_bound) * fraction
    return None


def calculate(before_doc: dict[str, object], after_doc: dict[str, object]) -> dict[str, object]:
    before = {key: float(value) for key, value in dict(before_doc["metrics"]).items()}
    after = {key: float(value) for key, value in dict(after_doc["metrics"]).items()}
    duration = counter_delta(before, after, f"{PREFILL_TIME}_sum")
    count = counter_delta(before, after, f"{PREFILL_TIME}_count")
    computed_tokens = counter_delta(before, after, f"{PREFILL_TOKENS}_sum")
    token_source = PREFILL_TOKENS
    if computed_tokens is None:
        computed_tokens = counter_delta(before, after, PROMPT_TOKENS)
        token_source = PROMPT_TOKENS
    available = duration is not None and duration > 0 and count is not None and count > 0
    result: dict[str, object] = {
        "available": available,
        "prefill_duration_seconds": duration,
        "prefill_observation_count": int(count) if count is not None else None,
        "prefill_computed_tokens": int(computed_tokens) if computed_tokens is not None else None,
        "mean_prefill_ms": duration / count * 1000 if available else None,
        "median_prefill_ms": None,
        "p95_prefill_ms": None,
        "p99_prefill_ms": None,
        "prefill_token_throughput": (
            computed_tokens / duration if available and computed_tokens is not None else None
        ),
        "prefill_measurement_source": f"{PREFILL_TIME}; {token_source}",
        "error": None,
    }
    if available:
        for field, quantile in (
            ("median_prefill_ms", 0.50),
            ("p95_prefill_ms", 0.95),
            ("p99_prefill_ms", 0.99),
        ):
            value = histogram_quantile(before, after, quantile)
            result[field] = value * 1000 if value is not None else None
    else:
        result["error"] = "native vLLM Prefill duration counters were unavailable or unchanged"
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot = subparsers.add_parser("snapshot")
    snapshot.add_argument("--url", required=True)
    snapshot.add_argument("--output", type=Path, required=True)
    delta = subparsers.add_parser("delta")
    delta.add_argument("--before", type=Path, required=True)
    delta.add_argument("--after", type=Path, required=True)
    delta.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "snapshot":
        document = capture(args.url)
    else:
        document = calculate(
            json.loads(args.before.read_text(encoding="utf-8")),
            json.loads(args.after.read_text(encoding="utf-8")),
        )
    args.output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
