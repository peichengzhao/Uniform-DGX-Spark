from __future__ import annotations

import hashlib
import json
from typing import Any

from .schemas import AccuracyResolvedConfig


def _digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def evaluation_fingerprint(config: AccuracyResolvedConfig, version: str, commit: str | None) -> str:
    """Identify evaluation settings, independently of the model being compared."""
    settings = config.model_dump(mode="json", exclude={"deployment_snapshot"})
    # Dataset/subset order does not change the selected samples or scoring contract.
    for dataset in settings["datasets"]:
        dataset["subset_list"] = sorted(dataset["subset_list"])
    settings["datasets"].sort(key=lambda item: item["name"])
    return _digest(
        {
            "identity_schema": 1,
            "report_schema": 2,
            "evalscope_version": version,
            "evalscope_commit": commit,
            "settings": settings,
        }
    )


def cache_fingerprint(fingerprint: str, model_name: str, snapshot: dict[str, Any]) -> str:
    """Cache reuse additionally requires the same registered model/deployment snapshot."""
    return _digest(
        {
            "evaluation_fingerprint": fingerprint,
            "model_name": model_name,
            "deployment_snapshot": snapshot,
        }
    )
