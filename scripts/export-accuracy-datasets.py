"""Export a read-only, pinned EvalScope catalog without importing its adapters."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

from uniform_dgxspark.services.evalscope_accuracy import EvalScopeAccuracyAdapter


def build_catalog(source: Path) -> dict:
    commit = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    if commit != EvalScopeAccuracyAdapter.commit:
        raise ValueError("EvalScope source does not match the platform's pinned commit")
    dirty = subprocess.check_output(
        ["git", "-C", str(source), "status", "--porcelain", "--", "evalscope/benchmarks"],
        text=True,
    )
    if dirty:
        raise ValueError("EvalScope benchmark metadata has uncommitted changes")
    root = source / "evalscope" / "benchmarks"
    index_bytes = (root / "_index.json").read_bytes()
    index = json.loads(index_bytes)
    digest = hashlib.sha256(index_bytes)
    supported = EvalScopeAccuracyAdapter.dataset_catalog()
    if not set(supported) <= set(index):
        raise ValueError("A platform dataset is missing from the EvalScope index")
    items = []
    for name in sorted(index):
        raw = (root / "_meta" / f"{name}.json").read_bytes()
        digest.update(name.encode() + b"\0" + raw)
        document = json.loads(raw)
        meta = document["meta"]
        description = meta.get("description") or ""
        translated = document.get("readme", {}).get("zh") or ""
        overview = re.search(r"## 概述\s+(.+?)(?:\n## |$)", translated, re.S)
        english = re.search(r"## Overview\s+(.+?)(?:\n## |$)", description, re.S)
        summary = (overview or english).group(1).strip() if overview or english else ""
        # Retain descriptive sections only, not upstream CLI examples or templates.
        if overview:
            end = re.search(
                r"\n## (?:属性|Properties|数据集统计|统计信息|数据统计|Statistics)", translated
            )
            description = translated[overview.start() : end.start() if end else len(translated)]
            description = re.split(r"\n## (?:使用方法|Usage|提示模板|样本示例)", description)[0]
        metrics = meta.get("metrics") or []
        metric_names = [m if isinstance(m, str) else ", ".join(m) for m in metrics]
        items.append(
            {
                "name": name,
                "pretty_name": meta.get("pretty_name") or name,
                "dataset_id": meta.get("dataset_id") or "",
                "category": meta.get("category") or "other",
                "tags": meta.get("tags") or [],
                "summary": re.sub(r"\s+", " ", summary),
                "description": description,
                "subsets": meta.get("subset_list") or [],
                "metrics": metric_names,
                "platform": supported.get(name),
            }
        )
    return {
        "schema_version": 1,
        "source": {
            "project": "EvalScope",
            "version": EvalScopeAccuracyAdapter.version,
            "commit": commit,
            "metadata_sha256": digest.hexdigest(),
            "scope": "native benchmark registry; not additional backend plugins",
        },
        "items": items,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    catalog = build_catalog(args.source)
    rendered = json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"
    if args.check:
        if args.output.read_text(encoding="utf-8") != rendered:
            raise SystemExit("Catalog is out of date; regenerate it from the pinned source")
    else:
        args.output.write_text(rendered, encoding="utf-8")
    print(
        f"Validated {len(catalog['items'])} benchmarks; "
        f"{sum(item['platform'] is not None for item in catalog['items'])} platform integrations"
    )


if __name__ == "__main__":
    main()
