from __future__ import annotations

from uniform_dgxspark.services.catalog_refresh import CatalogRefreshWorker


class PartiallyFailingCatalogService:
    def refresh_huggingface(self, *, limit: int) -> int:
        return limit

    def refresh_modelscope(self, *, limit: int) -> int:
        del limit
        raise RuntimeError("temporary ModelScope error")


def test_background_refresh_keeps_source_results_independent() -> None:
    worker = CatalogRefreshWorker(
        PartiallyFailingCatalogService(),  # type: ignore[arg-type]
        interval_seconds=3600,
        limit=50,
    )

    state = worker.refresh_now()

    assert state["sources"]["huggingface"] == {"ok": True, "refreshed": 50}  # type: ignore[index]
    assert state["sources"]["modelscope"]["ok"] is False  # type: ignore[index]
