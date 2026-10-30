from __future__ import annotations

from uniform_dgxspark.benchmark_reference.test_vllm.collect_prefill_metrics import (
    calculate,
    parse_prometheus,
)


def snapshot(
    *, duration: float, count: int, tokens: int, buckets: dict[str, int]
) -> dict[str, object]:
    lines = [
        f'vllm:request_prefill_time_seconds_sum{{engine="0"}} {duration}',
        f'vllm:request_prefill_time_seconds_count{{engine="0"}} {count}',
        f'vllm:request_prefill_kv_computed_tokens_sum{{engine="0"}} {tokens}',
    ]
    lines.extend(
        f'vllm:request_prefill_time_seconds_bucket{{engine="0",le="{bound}"}} {value}'
        for bound, value in buckets.items()
    )
    return {"metrics": parse_prometheus("\n".join(lines))}


def test_prefill_metrics_are_calculated_from_native_counter_deltas() -> None:
    before = snapshot(
        duration=10,
        count=10,
        tokens=1_000,
        buckets={"0.1": 2, "0.5": 8, "1.0": 10, "+Inf": 10},
    )
    after = snapshot(
        duration=12,
        count=14,
        tokens=5_000,
        buckets={"0.1": 2, "0.5": 10, "1.0": 14, "+Inf": 14},
    )

    result = calculate(before, after)

    assert result["available"] is True
    assert result["prefill_duration_seconds"] == 2
    assert result["prefill_observation_count"] == 4
    assert result["prefill_computed_tokens"] == 4_000
    assert result["mean_prefill_ms"] == 500
    assert result["median_prefill_ms"] == 500
    assert result["p95_prefill_ms"] == 950
    assert result["p99_prefill_ms"] == 990
    assert result["prefill_token_throughput"] == 2_000


def test_prefill_metrics_remain_unavailable_without_native_timer() -> None:
    before = {"metrics": {"vllm:prompt_tokens_total": 100}}
    after = {"metrics": {"vllm:prompt_tokens_total": 300}}

    result = calculate(before, after)

    assert result["available"] is False
    assert result["prefill_duration_seconds"] is None
    assert result["prefill_token_throughput"] is None
    assert result["error"]
