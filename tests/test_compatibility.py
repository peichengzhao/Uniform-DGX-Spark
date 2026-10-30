from pathlib import Path
from types import SimpleNamespace

import pytest

from uniform_dgxspark.services.compatibility import (
    CompatibilityError,
    analyze_model_directory,
)


def test_known_architecture_and_weight_estimate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "uniform_dgxspark.services.compatibility.psutil.virtual_memory",
        lambda: SimpleNamespace(available=16 * 1024**3),
    )
    (tmp_path / "config.json").write_text(
        '{"architectures":["Qwen2ForCausalLM"],"model_type":"qwen2"}'
    )
    (tmp_path / "model.safetensors").write_bytes(b"weights")

    result = analyze_model_directory(tmp_path)

    assert result.architecture == "Qwen2ForCausalLM"
    assert result.confidence == "verified-architecture"
    assert result.weight_bytes == 7
    assert result.estimated_peak_bytes > result.weight_bytes


def test_non_vllm_architecture_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text(
        '{"architectures":["StableDiffusionPipeline"]}'
    )
    (tmp_path / "model.safetensors").write_bytes(b"weights")

    with pytest.raises(CompatibilityError, match="not a vLLM model"):
        analyze_model_directory(tmp_path)
