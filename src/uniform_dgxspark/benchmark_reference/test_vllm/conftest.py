import time
import pytest
import subprocess
import os
import threading
import requests
import csv
from datetime import datetime


# =========================
# 基础工具函数
# =========================

def wait_for_server(host, port, timeout=180):
    url = f"http://{host}:{port}/health"
    start = time.time()

    while time.time() - start < timeout:
        try:
            r = requests.get(url, timeout=2)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)

    raise RuntimeError(f"vLLM server not ready on port {port}")


def pytest_addoption(parser):
    """添加命令行参数"""
    parser.addoption(
        "--existing-csv",
        action="store",
        default=None,
        help="Path to existing CSV file with previous test results"
    )


@pytest.fixture(scope="session")
def existing_test_results(request):
    """
    读取已存在的CSV测试结果，用于跳过已执行的测试用例

    Returns:
        set: 包含已执行测试组合的唯一标识集合
             标识格式为: "model|dtype|tp|context_length|random_ratio|input|output|concurrency"
    """
    existing_csv_path = request.config.getoption("--existing-csv")

    if not existing_csv_path or not os.path.exists(existing_csv_path):
        print(f"\n[INFO] No existing CSV file provided or file not found. All tests will run.")
        return set()

    executed_test_cases = set()

    try:
        with open(existing_csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)

            for row in reader:
                # 生成唯一标识
                test_case_key = (
                    f"{row.get('Model', '')}|"
                    f"{row.get('Dtype', '')}|"
                    f"{row.get('TP', '')}|"
                    f"{row.get('max-model-len', '')}|"
                    f"{row.get('Input', '')}|"
                    f"{row.get('Output', '')}|"
                    f"{row.get('Max-Concurrency', '')}"
                )
                executed_test_cases.add(test_case_key)

        print(f"\n[INFO] Loaded {len(executed_test_cases)} executed test cases from {existing_csv_path}")

    except Exception as e:
        print(f"\n[WARNING] Failed to read existing CSV file: {e}")
        return set()

    return executed_test_cases



def _kill_tmux_session(session_name):
    subprocess.run(
        ["tmux", "kill-session", "-t", session_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _tmux_has_session(session_name) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


# =========================
# tmux 启动 / 停止
# =========================

def _start_tmux_server(cmd, env, session_name, log_file):
    """
    正确 & 稳定地在 tmux session 中启动 vLLM
    """

    # # 只注入必要 env，避免 shell 解析问题
    # env_keys = [
    #     "TANG_VISIBLE_DEVICES",
    #     "SGL_PTPU_USE_PERFX_GEMM",
    #     "PTPU_DSV3S_DSK_GROUP_GEMM",
    #     "PTS_BLOCKED",
    #     "TORCH_CPP_LOG_LEVEL",
    #     "PYTHONUNBUFFERED",
    # ]
    #
    # env_prefix = " ".join(
    #     f'{k}="{env[k]}"' for k in env_keys if k in env
    # )
    #
    # full_cmd = f"{env_prefix} {' '.join(cmd)}"

    env_cmd = _build_env_cmd(env)
    full_cmd = f"{env_cmd} {' '.join(cmd)}"

    print(f"[DEBUG] tmux session: {session_name}")
    print(f"[DEBUG] cmd: {full_cmd}")

    # 启动前清理旧 session
    _kill_tmux_session(session_name)

    # 正确启动（command 必须是一个完整字符串）
    subprocess.run(
        [
            "tmux", "new-session",
            "-d",
            "-s", session_name,
            full_cmd,
        ],
        check=True,
    )

    # 确认 session 存活
    time.sleep(1)
    if not _tmux_has_session(session_name):
        raise RuntimeError(f"tmux session {session_name} not running")

    # 使用 pipe-pane 直接写日志（不会重复）
    subprocess.run(
        [
            "tmux", "pipe-pane",
            "-t", session_name,
            f"cat >> {log_file}",
        ],
        check=False,
    )

    print(f"[DEBUG] tmux session {session_name} started")


def _stop_tmux_server(session_name):
    _kill_tmux_session(session_name)


def _build_env_cmd(env: dict) -> str:
    parts = ["env", "-i"]   # 👈 关键在这里
    for k, v in env.items():
        if v is None:
            continue
        parts.append(f'{k}="{v}"')
    return " ".join(parts)



# =========================
# pytest fixtures
# =========================

@pytest.fixture(scope="class")
def vllm_qwen_server(request):
    cfg = request.param

    # ---------- log 目录 ----------
    log_dir = os.path.join(os.getcwd(), "log")
    os.makedirs(log_dir, exist_ok=True)

    model_name = os.path.basename(cfg["model"].rstrip("/"))
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(
        log_dir, f"vllm_{model_name}_port{cfg['port']}_{timestamp}.log"
    )

    session_name = f"vllm_{model_name}_p{cfg['port']}"

    # ---------- 启动命令 ----------
    cmd = [
        "vllm", "serve",
        cfg["model"],
        "--trust-remote-code",
        "--served-model-name", cfg["model"],
        "--port", str(cfg["port"]),
        "--tensor-parallel-size", str(cfg.get("tp", 1)),
        "--gpu-memory-utilization", str(cfg.get("gpu-memory-utilization", 0.6)),
        "--max-model-len", str(cfg.get("max-model-len", 32768)),
    ]

    if cfg.get("dtype"):
        cmd += ["--dtype", cfg["dtype"]]
    if cfg.get("no-enable-prefix-caching"):
        cmd += ["--no-enable-prefix-caching"]
    if cfg.get("compilation-config"):
        cmd += ["--compilation-config", "'" + cfg["compilation-config"] + "'"]

    # ---------- 环境变量 ----------
    env = os.environ.copy()
    env.update({
        "TANG_VISIBLE_DEVICES": cfg.get("TANG_VISIBLE_DEVICES", "0,1"),
        "SGL_PTPU_USE_PERFX_GEMM": "1",
        "PTPU_DSV3S_DSK_GROUP_GEMM": "1",
        "TORCH_PTPU_PCCL_SYNC": "1",
        "PTS_BLOCKED": "1",
        "TORCH_CPP_LOG_LEVEL": "INFO",
        "PYTHONUNBUFFERED": "1",
    })

    print("\n========== Launch vLLM (tmux) ==========")
    print("Model:", cfg["model"])
    print("Port:", cfg["port"])
    print("GPU:", env["TANG_VISIBLE_DEVICES"])
    print("tmux:", session_name)
    print("log :", log_file)
    print(f"tail -50f {log_file}")
    print(f"attach: tmux attach -t {session_name}")
    print("=======================================\n")

    # ---------- 启动 ----------
    with open(log_file, "a", buffering=1) as f:
        f.write(f"Start time: {datetime.now()}\n")
        f.write(f"Command: {' '.join(cmd)}\n\n")

    _start_tmux_server(cmd, env, session_name, log_file)

    try:
        wait_for_server("127.0.0.1", cfg["port"])
        print(f"[READY] vLLM ready on port {cfg['port']}")
        yield cfg
    finally:
        print(f"[STOP] stopping vLLM ({session_name})")
        _stop_tmux_server(session_name)
        time.sleep(300)


# =========================
# 结果收集 fixture（保持你原样）
# =========================

@pytest.fixture(scope="session")
def perf_qwen_result():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return (
        f"benchmark_results_{timestamp}.csv",
        f"benchmark_results_{timestamp}.json",
    )
