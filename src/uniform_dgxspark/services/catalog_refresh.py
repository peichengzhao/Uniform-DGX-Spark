from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from ..catalog import CatalogService


class CatalogRefreshWorker:
    def __init__(
        self,
        service: CatalogService,
        *,
        interval_seconds: int = 3600,
        limit: int = 50,
    ) -> None:
        self.service = service
        self.interval_seconds = max(60, interval_seconds)
        self.limit = min(500, max(1, limit))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._state: dict[str, object] = {
            "running": False,
            "last_started_at": None,
            "last_finished_at": None,
            "next_refresh_at": None,
            "sources": {},
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="uniform-catalog-refresh",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)

    def refresh_now(self) -> dict[str, object]:
        started = datetime.now(UTC)
        with self._lock:
            self._state["running"] = True
            self._state["last_started_at"] = started.isoformat()
        sources: dict[str, dict[str, object]] = {}
        for source, refresh in (
            ("huggingface", self.service.refresh_huggingface),
            ("modelscope", self.service.refresh_modelscope),
        ):
            try:
                sources[source] = {"ok": True, "refreshed": refresh(limit=self.limit)}
            except Exception as error:  # noqa: BLE001 - independent source boundary
                sources[source] = {"ok": False, "refreshed": 0, "error": str(error)}
        finished = datetime.now(UTC)
        with self._lock:
            self._state = {
                "running": False,
                "last_started_at": started.isoformat(),
                "last_finished_at": finished.isoformat(),
                "next_refresh_at": (
                    finished + timedelta(seconds=self.interval_seconds)
                ).isoformat(),
                "sources": sources,
            }
        return self.snapshot()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return dict(self._state) | {
                "interval_seconds": self.interval_seconds,
                "limit_per_source": self.limit,
            }

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.refresh_now()
            self._stop_event.wait(self.interval_seconds)
