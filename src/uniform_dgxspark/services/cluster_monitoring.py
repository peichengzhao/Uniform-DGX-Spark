from __future__ import annotations

import copy
import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Protocol

from ..node_store import SSHNodeStore
from ..schemas import SSHNodeRead
from .node_probe import collect as collect_local


class SSHProbeError(RuntimeError):
    pass


class ProbeRunner(Protocol):
    def collect(self, node: SSHNodeRead) -> dict[str, Any]: ...


class SSHProbeRunner:
    def __init__(self, probe_path: Path | None = None) -> None:
        self.probe_path = probe_path or Path(__file__).with_name("node_probe.py")

    def collect(self, node: SSHNodeRead) -> dict[str, Any]:
        command = [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "ConnectionAttempts=1",
            "-p",
            str(node.port),
            f"{node.username}@{node.host}",
            "python3",
            "-",
        ]
        try:
            result = subprocess.run(
                command,
                input=self.probe_path.read_text(encoding="utf-8"),
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SSHProbeError(f"SSH probe failed: {error}") from error
        if result.returncode != 0:
            message = result.stderr.strip().splitlines()[-1:] or ["SSH probe failed"]
            raise SSHProbeError(message[0][:500])
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise SSHProbeError("remote probe returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise SSHProbeError("remote probe returned an invalid object")
        return payload


class ClusterMonitoringService:
    def __init__(
        self,
        store: SSHNodeStore,
        *,
        interval_seconds: float = 5,
        runner: ProbeRunner | None = None,
    ) -> None:
        self.store = store
        self.interval_seconds = interval_seconds
        self.runner = runner or SSHProbeRunner()
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._previous_counters: dict[tuple[str, str], tuple[float, int, int]] = {}
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self.refresh()
        self._thread = threading.Thread(
            target=self._loop,
            name="uniform-cluster-monitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread:
            self._thread.join(timeout=20)

    def wake(self) -> None:
        self._wake_event.set()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            items = copy.deepcopy(list(self._snapshots.values()))
        items.sort(key=lambda item: (item["id"] != "local", item.get("label") or ""))
        return {
            "items": items,
            "total": len(items),
            "refresh_interval_seconds": self.interval_seconds,
        }

    def refresh(self, *, wait: bool = False) -> dict[str, Any]:
        acquired = (
            self._refresh_lock.acquire(timeout=20)
            if wait
            else self._refresh_lock.acquire(blocking=False)
        )
        if not acquired:
            return self.snapshot()
        try:
            nodes = self.store.list()
            snapshots: dict[str, dict[str, Any]] = {}
            try:
                local = collect_local()
                snapshots["local"] = self._decorate(
                    "local",
                    local,
                    label="本机",
                    connection={"kind": "local"},
                )
            except Exception as error:  # noqa: BLE001 - probe boundary
                snapshots["local"] = self._offline("local", "本机", {"kind": "local"}, str(error))
            if nodes:
                with ThreadPoolExecutor(max_workers=min(8, len(nodes))) as executor:
                    futures = {executor.submit(self.runner.collect, node): node for node in nodes}
                    for future in as_completed(futures):
                        node = futures[future]
                        connection = {
                            "kind": "ssh",
                            "host": node.host,
                            "port": node.port,
                            "username": node.username,
                        }
                        try:
                            payload = future.result()
                            snapshots[node.id] = self._decorate(
                                node.id,
                                payload,
                                label=node.label or payload.get("hostname") or node.host,
                                connection=connection,
                            )
                        except Exception as error:  # noqa: BLE001 - remote boundary
                            snapshots[node.id] = self._offline(
                                node.id,
                                node.label or node.host,
                                connection,
                                str(error),
                            )
            valid_keys = {
                (node_id, str(interface.get("interface") or ""))
                for node_id, snapshot in snapshots.items()
                for interface in snapshot.get("rdma_interfaces", [])
            }
            self._previous_counters = {
                key: value for key, value in self._previous_counters.items() if key in valid_keys
            }
            with self._lock:
                self._snapshots = snapshots
            return self.snapshot()
        finally:
            self._refresh_lock.release()

    def _decorate(
        self,
        node_id: str,
        payload: dict[str, Any],
        *,
        label: str,
        connection: dict[str, Any],
    ) -> dict[str, Any]:
        observed_at = float(payload.get("observed_at") or 0)
        for interface in payload.get("rdma_interfaces", []):
            name = str(interface.get("interface") or "")
            key = (node_id, name)
            rx = int(interface.get("rx_bytes") or 0)
            tx = int(interface.get("tx_bytes") or 0)
            previous = self._previous_counters.get(key)
            rx_bps = 0.0
            tx_bps = 0.0
            if previous and observed_at > previous[0]:
                elapsed = observed_at - previous[0]
                rx_bps = max(0, rx - previous[1]) * 8 / elapsed
                tx_bps = max(0, tx - previous[2]) * 8 / elapsed
            interface["rx_bps"] = round(rx_bps, 1)
            interface["tx_bps"] = round(tx_bps, 1)
            self._previous_counters[key] = (observed_at, rx, tx)
        return {
            "id": node_id,
            "label": label,
            "online": True,
            "error": None,
            "connection": connection,
            **payload,
        }

    @staticmethod
    def _offline(
        node_id: str,
        label: str,
        connection: dict[str, Any],
        error: str,
    ) -> dict[str, Any]:
        return {
            "id": node_id,
            "label": label,
            "online": False,
            "observed_at": time.time(),
            "error": error[:500],
            "connection": connection,
            "addresses": [],
            "rdma_interfaces": [],
            "containers": [],
        }

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            self._wake_event.wait(self.interval_seconds)
            self._wake_event.clear()
            if not self._stop_event.is_set():
                self.refresh()
