from __future__ import annotations

import ipaddress
import json
import os
import platform
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

MODEL_PROCESS_MARKERS = (
    "sglang::",
    "vllm",
    "tritonserver",
    "tensorrt",
    "llama",
    "ollama",
    "text-generation",
)


def _run(arguments: list[str], timeout: int = 5) -> str:
    try:
        result = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout if result.returncode == 0 else ""


def _json_command(arguments: list[str]) -> Any:
    try:
        return json.loads(_run(arguments) or "[]")
    except json.JSONDecodeError:
        return []


def _model_gpu_processes(output: str) -> list[dict[str, Any]]:
    processes: list[dict[str, Any]] = []
    for line in output.splitlines():
        parts = [part.strip() for part in line.split(",", 2)]
        if len(parts) != 3:
            continue
        try:
            pid = int(parts[0])
            used_mib = float(parts[2])
        except ValueError:
            continue
        processes.append(
            {
                "pid": pid,
                "process_name": parts[1],
                "used_memory_mib": used_mib,
                "used_memory_bytes": int(used_mib * 1024 * 1024),
            }
        )
    model_processes = [
        process
        for process in processes
        if any(marker in process["process_name"].lower() for marker in MODEL_PROCESS_MARKERS)
    ]
    return model_processes or (
        [max(processes, key=lambda item: item["used_memory_mib"])] if processes else []
    )


def _read_int(path: Path) -> int | None:
    try:
        value = int(path.read_text(encoding="utf-8").strip())
        return value if value >= 0 else None
    except (OSError, ValueError):
        return None


def _link_status(
    rdma_state: str,
    physical_state: str,
    operstate: str,
    addresses: list[str],
) -> dict[str, bool | str | None]:
    physical_link_up = (
        rdma_state == "ACTIVE" and physical_state == "LINK_UP" and operstate.upper() == "UP"
    )
    usable_addresses: list[str] = []
    for value in addresses:
        try:
            address = ipaddress.ip_address(value.split("%", 1)[0])
        except ValueError:
            continue
        if not (address.is_loopback or address.is_link_local or address.is_unspecified):
            usable_addresses.append(value)
    network_configured = physical_link_up and bool(usable_addresses)
    if network_configured:
        status = "network_configured"
    elif physical_link_up:
        status = "physical_only"
    else:
        status = "down"
    return {
        "physical_link_up": physical_link_up,
        "network_configured": network_configured,
        "peer_reachable": None,
        "connection_status": status,
    }


def _memory() -> dict[str, int | float]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, _, raw = line.partition(":")
            parts = raw.strip().split()
            if parts and parts[0].isdigit():
                values[key] = int(parts[0]) * 1024
    except OSError:
        pass
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    used = max(0, total - available)
    return {
        "total_bytes": total,
        "used_bytes": used,
        "available_bytes": available,
        "percent": round((used / total * 100), 1) if total else 0,
    }


def _cpu() -> dict[str, Any]:
    def counters() -> tuple[int, int] | None:
        try:
            fields = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()
            values = [int(value) for value in fields[1:]]
        except (OSError, ValueError, IndexError):
            return None
        if len(values) < 4:
            return None
        return sum(values), values[3] + (values[4] if len(values) > 4 else 0)

    before = counters()
    time.sleep(0.1)
    after = counters()
    percent: float | None = None
    if before and after and after[0] > before[0]:
        total_delta = after[0] - before[0]
        idle_delta = max(0, after[1] - before[1])
        percent = round(max(0.0, min(100.0, (total_delta - idle_delta) / total_delta * 100)), 1)
    try:
        load_average = list(os.getloadavg())
    except OSError:
        load_average = []
    return {
        "logical_count": os.cpu_count(),
        "utilization_percent": percent,
        "load_average": load_average,
    }


def _gpu(memory: dict[str, int | float]) -> dict[str, Any]:
    fields = [
        "name",
        "uuid",
        "utilization.gpu",
        "utilization.memory",
        "temperature.gpu",
        "power.draw",
        "memory.total",
        "memory.used",
        "memory.free",
    ]
    output = _run(
        [
            "nvidia-smi",
            f"--query-gpu={','.join(fields)}",
            "--format=csv,noheader,nounits",
        ]
    ).strip()
    if not output:
        return {"available": False}
    parts = [part.strip() for part in output.splitlines()[0].split(",")]

    def number(index: int) -> float | None:
        try:
            return float(parts[index])
        except (IndexError, ValueError):
            return None

    dedicated_total_mib = number(6)
    dedicated_used_mib = number(7)
    dedicated_free_mib = number(8)
    dedicated_available = bool(dedicated_total_mib and dedicated_total_mib > 0)
    mib = 1024 * 1024
    process_output = _run(
        [
            "nvidia-smi",
            "--query-compute-apps=pid,process_name,used_gpu_memory",
            "--format=csv,noheader,nounits",
        ]
    )
    model_processes = _model_gpu_processes(process_output)
    model_used_mib = sum(item["used_memory_mib"] for item in model_processes)
    if dedicated_available:
        memory_kind = "dedicated"
        memory_total_bytes = int(dedicated_total_mib * mib)
        memory_used_bytes = int((dedicated_used_mib or 0) * mib)
        memory_available_bytes = int((dedicated_free_mib or 0) * mib)
        memory_allocation_visible = True
    else:
        memory_kind = "unified"
        memory_total_bytes = int(memory.get("total_bytes") or 0)
        memory_used_bytes = int(model_used_mib * mib)
        memory_available_bytes = max(0, memory_total_bytes - memory_used_bytes)
        memory_allocation_visible = bool(model_processes)
    model_memory_percent = (
        round(int(model_used_mib * mib) / memory_total_bytes * 100, 1)
        if model_processes and memory_total_bytes > 0
        else None
    )

    return {
        "available": True,
        "name": parts[0] if parts else "NVIDIA GPU",
        "uuid": parts[1] if len(parts) > 1 else None,
        "utilization_percent": number(2),
        "memory_controller_utilization_percent": number(3),
        "temperature_c": number(4),
        "power_watts": number(5),
        "memory_kind": memory_kind,
        "memory_total_bytes": memory_total_bytes,
        "memory_used_bytes": memory_used_bytes,
        "memory_available_bytes": memory_available_bytes,
        "memory_allocation_visible": memory_allocation_visible,
        "memory_scope": "model_compute_process_allocation",
        "model_memory_total_bytes": memory_total_bytes,
        "model_memory_percent": model_memory_percent,
        "model_memory_used_mib": model_used_mib,
        "model_memory_used_bytes": int(model_used_mib * mib),
        "model_process_count": len(model_processes),
        "model_processes": model_processes,
        "memory_note": (
            "Dedicated GPU memory reported by nvidia-smi"
            if dedicated_available
            else "Model GPU allocation reported by nvidia-smi compute processes"
            if model_processes
            else "No model GPU allocation reported by nvidia-smi"
        ),
    }


def _interfaces() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    addresses_raw = _json_command(["ip", "-j", "address", "show"])
    links_raw = _json_command(["ip", "-s", "-j", "link", "show"])
    rdma_raw = _json_command(["rdma", "-j", "link"])
    links = {item.get("ifname"): item for item in links_raw if item.get("ifname")}
    addresses: list[dict[str, Any]] = []
    address_map: dict[str, list[str]] = {}
    for interface in addresses_raw:
        name = str(interface.get("ifname") or "")
        if not name:
            continue
        values = [
            str(item.get("local"))
            for item in interface.get("addr_info", [])
            if item.get("local") and item.get("scope") != "host"
        ]
        address_map[name] = values
        if name != "lo":
            addresses.append(
                {
                    "interface": name,
                    "operstate": interface.get("operstate", "UNKNOWN"),
                    "addresses": values,
                }
            )

    rdma_interfaces: list[dict[str, Any]] = []
    for rdma in rdma_raw:
        netdev = str(rdma.get("netdev") or "")
        rdma_device = str(rdma.get("ifname") or "")
        rdma_port = int(rdma.get("port") or 1)
        if not netdev:
            continue
        link = links.get(netdev, {})
        stats = link.get("stats64", {})
        rx = stats.get("rx", {})
        tx = stats.get("tx", {})
        sys_net = Path("/sys/class/net") / netdev
        driver = None
        try:
            driver = (sys_net / "device/driver").resolve().name
        except OSError:
            pass
        statistic_output = _run(["rdma", "statistic", "show", "link", f"{rdma_device}/{rdma_port}"])
        statistic_parts = statistic_output.split()
        statistics: dict[str, int] = {}
        for index in range(2, len(statistic_parts) - 1, 2):
            try:
                statistics[statistic_parts[index]] = int(statistic_parts[index + 1])
            except ValueError:
                continue
        error_keys = {
            "out_of_buffer",
            "out_of_sequence",
            "rnr_nak_retry_err",
            "packet_seq_err",
            "local_ack_timeout_err",
            "resp_local_length_error",
            "resp_cqe_error",
            "req_cqe_error",
            "req_remote_invalid_request",
            "req_remote_access_errors",
            "resp_remote_access_errors",
            "resp_cqe_flush_error",
            "req_cqe_flush_error",
            "req_transport_retries_exceeded",
            "req_rnr_retries_exceeded",
        }
        retransmit_keys = {
            "roce_adp_retrans",
            "roce_adp_retrans_to",
            "roce_slow_restart",
            "roce_slow_restart_trans",
        }
        interface_addresses = address_map.get(netdev, [])
        operstate = str(link.get("operstate", "UNKNOWN"))
        rdma_interfaces.append(
            {
                "rdma_device": rdma_device,
                "rdma_port": rdma_port,
                "state": rdma.get("state", "UNKNOWN"),
                "physical_state": rdma.get("physical_state", "UNKNOWN"),
                "interface": netdev,
                "operstate": operstate,
                "addresses": interface_addresses,
                **_link_status(
                    str(rdma.get("state", "UNKNOWN")),
                    str(rdma.get("physical_state", "UNKNOWN")),
                    operstate,
                    interface_addresses,
                ),
                "mtu": link.get("mtu"),
                "speed_mbps": _read_int(sys_net / "speed"),
                "driver": driver,
                "rx_bytes": int(rx.get("bytes") or 0),
                "tx_bytes": int(tx.get("bytes") or 0),
                "rx_errors": int(rx.get("errors") or 0),
                "tx_errors": int(tx.get("errors") or 0),
                "rx_dropped": int(rx.get("dropped") or 0),
                "tx_dropped": int(tx.get("dropped") or 0),
                "rdma_error_total": sum(statistics.get(key, 0) for key in error_keys),
                "rdma_retransmit_total": sum(statistics.get(key, 0) for key in retransmit_keys),
            }
        )
    return addresses, rdma_interfaces


def _containers() -> list[dict[str, Any]]:
    output = _run(["docker", "ps", "--no-trunc", "--format", "{{json .}}"])
    stats_output = _run(["docker", "stats", "--no-stream", "--format", "{{json .}}"])
    stats_by_key: dict[str, dict[str, str]] = {}
    for line in stats_output.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        values = {
            "cpu_percent": str(item.get("CPUPerc") or ""),
            "memory_usage": str(item.get("MemUsage") or ""),
            "memory_percent": str(item.get("MemPerc") or ""),
            "net_io": str(item.get("NetIO") or ""),
            "block_io": str(item.get("BlockIO") or ""),
            "pids": str(item.get("PIDs") or ""),
        }
        for key in (item.get("ID"), item.get("Container"), item.get("Name")):
            if key:
                stats_by_key[str(key)] = values
    containers: list[dict[str, Any]] = []
    for line in output.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        container_id = str(item.get("ID") or "")
        name = str(item.get("Names") or "")
        stats = (
            stats_by_key.get(container_id)
            or stats_by_key.get(container_id[:12])
            or stats_by_key.get(name)
            or {}
        )
        containers.append(
            {
                "id": container_id,
                "name": name,
                "image": str(item.get("Image") or ""),
                "state": str(item.get("State") or ""),
                "status": str(item.get("Status") or ""),
                "ports": str(item.get("Ports") or ""),
                "network": str(item.get("Networks") or ""),
                **stats,
            }
        )
    return containers


def collect() -> dict[str, Any]:
    addresses, rdma_interfaces = _interfaces()
    memory = _memory()
    disk = shutil.disk_usage("/")
    try:
        uptime_seconds = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        uptime_seconds = 0
    return {
        "observed_at": time.time(),
        "hostname": socket.gethostname(),
        "architecture": platform.machine(),
        "uptime_seconds": uptime_seconds,
        "cpu": _cpu(),
        "memory": memory,
        "disk": {
            "total_bytes": disk.total,
            "used_bytes": disk.used,
            "free_bytes": disk.free,
            "percent": round(disk.used / disk.total * 100, 1) if disk.total else 0,
        },
        "gpu": _gpu(memory),
        "addresses": addresses,
        "rdma_interfaces": rdma_interfaces,
        "containers": _containers(),
    }


def main() -> None:
    print(json.dumps(collect(), ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
