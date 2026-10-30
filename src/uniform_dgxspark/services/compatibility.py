from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import psutil

KNOWN_VLLM_ARCHITECTURES = {
    "AquilaForCausalLM",
    "BaichuanForCausalLM",
    "BloomForCausalLM",
    "ChatGLMModel",
    "CohereForCausalLM",
    "DeepseekForCausalLM",
    "DeepseekV2ForCausalLM",
    "DeepseekV3ForCausalLM",
    "FalconForCausalLM",
    "GemmaForCausalLM",
    "Gemma2ForCausalLM",
    "Gemma3ForConditionalGeneration",
    "GPTBigCodeForCausalLM",
    "GPTJForCausalLM",
    "GPTNeoXForCausalLM",
    "InternLMForCausalLM",
    "InternLM2ForCausalLM",
    "LlamaForCausalLM",
    "MistralForCausalLM",
    "MixtralForCausalLM",
    "MPTForCausalLM",
    "PhiForCausalLM",
    "Phi3ForCausalLM",
    "Qwen2ForCausalLM",
    "Qwen2MoeForCausalLM",
    "Qwen2VLForConditionalGeneration",
    "Qwen3ForCausalLM",
    "Qwen3MoeForCausalLM",
    "Starcoder2ForCausalLM",
}


class CompatibilityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CompatibilityResult:
    architecture: str
    weight_bytes: int
    estimated_peak_bytes: int
    available_memory_bytes: int
    confidence: str
    quantization: str | None


def analyze_model_directory(path: Path) -> CompatibilityResult:
    try:
        config = json.loads((path / "config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CompatibilityError(f"cannot read model config.json: {error}") from error

    architectures = config.get("architectures") or []
    architecture = architectures[0] if architectures else config.get("model_type", "unknown")
    model_type = str(config.get("model_type", "")).lower()
    clearly_unsupported = any(
        keyword in str(architecture).lower()
        for keyword in ("diffusion", "classification", "segmentation", "detection")
    )
    if clearly_unsupported:
        raise CompatibilityError(f"architecture {architecture} is not a vLLM model")

    confidence = (
        "verified-architecture"
        if architecture in KNOWN_VLLM_ARCHITECTURES
        else "experimental-architecture"
    )
    weight_files = [*path.glob("*.safetensors"), *path.glob("*.bin")]
    weight_bytes = sum(file.stat().st_size for file in weight_files if file.is_file())
    if weight_bytes <= 0:
        raise CompatibilityError("no supported model weight files were found")

    quantization_config = config.get("quantization_config") or {}
    quantization = quantization_config.get("quant_method")
    estimated_peak = int(weight_bytes * 1.35) + (2 * 1024**3)
    available = psutil.virtual_memory().available
    if estimated_peak > available:
        raise CompatibilityError(
            "estimated model peak memory exceeds currently available unified memory: "
            f"required={estimated_peak} available={available}"
        )
    return CompatibilityResult(
        architecture=str(architecture or model_type or "unknown"),
        weight_bytes=weight_bytes,
        estimated_peak_bytes=estimated_peak,
        available_memory_bytes=available,
        confidence=confidence,
        quantization=str(quantization) if quantization else None,
    )
