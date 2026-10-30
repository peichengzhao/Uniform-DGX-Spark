"""
vLLM 性能基准测试工具
兼容 pytest 调用 & vLLM bench serve 输出
"""
import subprocess
import threading
import time
import re
import json
import csv
import logging
import os
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from itertools import product
import selectors

# ================= 日志 =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("benchmark.log")]
)
logger = logging.getLogger(__name__)


@dataclass
class BenchmarkConfig:
    input: int
    output: int
    concurrency: int
    num: int
    rate: Optional[int] = None


class BenchmarkRunner:

    def __init__(
        self,
        host: str,
        port: int,
        model: str,
        dataset_path: str,
        csv_path=None,
        output_file=None,
        tang_visible_devices=None,
        dtype=None,
        tp=None,
        max_model_len=None,
        tokenizer=None,
    ):
        self.host = host
        self.port = port
        self.model = model
        self.dataset_path = dataset_path
        self.dtype = dtype
        self.tp = tp
        self.max_model_len = max_model_len
        self.tokenizer = tokenizer
        self.results: Dict[str, Dict] = {}
        self.output_file = output_file

        if csv_path:
            self.csv_file = csv_path
            if not os.path.exists(self.csv_file):
                self._create_csv_file()

    # ================= CSV =================
    def _create_csv_file(self):
        headers = [
            "Model", "Dtype", "TP", "max-model-len",
            "Input", "Output", "Max-Concurrency", "Num",
            "Successful Req",
            "Duration(s)",
            "Total input tokens",
            "Total generated tokens",
            "Req/s",
            "Output Tok/s",
            "Total Tok/s",
            "Mean TTFT(ms)",
            "Median TTFT(ms)",
            "P99 TTFT(ms)",
            "Mean ITL(ms)",
            "Median ITL(ms)",
            "P95 ITL(ms)",
            "P99 ITL(ms)",
            "Mean TPOT (ms)",
            "Median TPOT(ms)",
            "P95 TPOT(ms)",
            "P99 TPOT(ms)",
        ]
        with open(self.csv_file, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(headers)

    def _append_to_csv(self, data: Dict):
        row = [
            self.model,
            self.dtype or "",
            self.tp or "",
            self.max_model_len or "",
        ]

        field_mapping = [
            ("Input", "Input", int),
            ("Output", "Output", int),
            ("Max-Concurrency", "Max-Concurrency", int),
            ("Num", "Num", int),
            ("Successful Req", "Successful requests", int),
            ("Duration(s)", "Benchmark duration (s)", float),
            ("Total input tokens", "Total input tokens", int),
            ("Total generated tokens", "Total generated tokens", int),
            ("Req/s", "Request throughput (req/s)", float),
            ("Output Tok/s", "Output token throughput (tok/s)", float),
            ("Total Tok/s", "Total Token throughput (tok/s)", float),
            ("Mean TTFT(ms)", "Mean TTFT (ms)", float),
            ("Median TTFT(ms)", "Median TTFT (ms)", float),
            ("P99 TTFT(ms)", "P99 TTFT (ms)", float),
            ("Mean ITL(ms)", "Mean ITL (ms)", float),
            ("Median ITL(ms)", "Median ITL (ms)", float),
            ("P95 ITL(ms)", "P95 ITL (ms)", float),
            ("P99 ITL(ms)", "P99 ITL (ms)", float),
            ("Mean TPOT (ms)", "Mean TPOT (ms)", float),
            ("Median TPOT(ms)", "Median TPOT (ms)", float),
            ("P95 TPOT(ms)", "P95 TPOT (ms)", float),
            ("P99 TPOT(ms)", "P99 TPOT (ms)", float),
        ]

        for _, key, tp in field_mapping:
            val = data.get(key, "")
            if val == "":
                row.append("")
            else:
                try:
                    row.append(int(float(val)) if tp == int else f"{float(val):.2f}")
                except Exception:
                    row.append("")

        with open(self.csv_file, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(row)

    # ================= 命令 =================
    def generate_commands_from_combinations(
        self,
        input_lengths: List[int],
        output_lengths: List[int],
        concurrencies: List[int],
        num_prompts: List[int],
    ):
        cmds = []
        for i, o, c, n in product(
            input_lengths, output_lengths, concurrencies, num_prompts
        ):
            cmds.append(
                f"vllm bench serve "
                f"--backend openai "
                f"--model {self.model} "
                f"--base-url http://{self.host}:{self.port} "
                f"--dataset-name random "
                f"--served-model-name {self.model} "
                f"--request-rate inf "
                f"--metric_percentiles 50,90,95,99 "
                f"--random-input-len {i} "
                f"--random-output-len {o} "
                f"--max-concurrency {c} "
                f"--num-prompts {n} "
                f"--ignore-eos"
                + (f" --tokenizer {self.tokenizer}" if self.tokenizer else "")
            )
        return cmds

    def run_commands(self, commands, timeout, cooldown):
        for idx, cmd in enumerate(commands):
            logger.info(f"[{idx+1}/{len(commands)}] {cmd}")
            stdout, _, rc = self._execute_command(cmd, timeout)
            if rc == 0:
                data = self._parse_output("\n".join(stdout))
                data.update(self._parse_command_params(cmd))
                self._append_to_csv(data)
                self.results[cmd] = data
            time.sleep(cooldown)

        if self.output_file:
            with open(self.output_file, "w", encoding="utf-8") as f:
                json.dump(self.results, f, indent=2, ensure_ascii=False)

    def _execute_command(self, command, timeout):
        p = subprocess.Popen(
            command, shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        stdout = []
        for line in p.stdout:
            line = line.strip()
            stdout.append(line)
            logger.info(f"[stdout] {line}")
        rc = p.wait(timeout=timeout)
        return stdout, [], rc

    def _parse_output(self, output: str):
        patterns = {
            "Successful requests": r"Successful requests:\s+(\S+)",
            "Benchmark duration (s)": r"Benchmark duration \(s\):\s+(\S+)",
            "Total input tokens": r"Total input tokens:\s+(\S+)",
            "Total generated tokens": r"Total generated tokens:\s+(\S+)",
            "Request throughput (req/s)": r"Request throughput \(req/s\):\s+(\S+)",
            "Output token throughput (tok/s)": r"Output token throughput \(tok/s\):\s+(\S+)",
            "Total Token throughput (tok/s)": r"Total token throughput \(tok/s\):\s+(\S+)",
            "Mean TTFT (ms)": r"Mean TTFT \(ms\):\s+(\S+)",
            "Median TTFT (ms)": r"Median TTFT \(ms\):\s+(\S+)",
            "P99 TTFT (ms)": r"P99 TTFT \(ms\):\s+(\S+)",
            "Mean ITL (ms)": r"Mean ITL \(ms\):\s+(\S+)",
            "Median ITL (ms)": r"Median ITL \(ms\):\s+(\S+)",
            "P95 ITL (ms)": r"P95 ITL \(ms\):\s+(\S+)",
            "P99 ITL (ms)": r"P99 ITL \(ms\):\s+(\S+)",
            "Mean TPOT (ms)": r"Mean TPOT \(ms\):\s+(\S+)",
            "Median TPOT (ms)": r"Median TPOT \(ms\):\s+(\S+)",
            "P95 TPOT (ms)": r"P95 TPOT \(ms\):\s+(\S+)",
            "P99 TPOT (ms)": r"P99 TPOT \(ms\):\s+(\S+)",
        }
        return {k: re.search(v, output).group(1)
                for k, v in patterns.items() if re.search(v, output)}

    def _parse_command_params(self, command):
        return {
            "Input": int(re.search(r"--random-input-len (\d+)", command).group(1)),
            "Output": int(re.search(r"--random-output-len (\d+)", command).group(1)),
            "Max-Concurrency": int(re.search(r"--max-concurrency (\d+)", command).group(1)),
            "Num": int(re.search(r"--num-prompts (\d+)", command).group(1)),
        }

    def generate_markdown_table(self) -> str:
        """生成 Markdown 格式的性能结果表格"""
        headers = [
            "Model", "Dtype", "TP", "max-model-len",
            "Input", "Output", "Max-Concurrency", "Num",
            "Successful Req",
            "Duration(s)",
            "Total input tokens",
            "Total generated tokens",
            "Req/s",
            "Output Tok/s",
            "Total Tok/s",
            "Mean TTFT(ms)",
            "Median TTFT(ms)",
            "P99 TTFT(ms)",
            "Mean ITL(ms)",
            "Median ITL(ms)",
            "P95 ITL(ms)",
            "P99 ITL(ms)",
            "Mean TPOT (ms)",
            "Median TPOT(ms)",
            "P95 TPOT(ms)",
            "P99 TPOT(ms)",
        ]

        rows = []

        for _, data in self.results.items():
            row = [
                self.model,
                self.dtype or "",
                self.tp or "",
                self.max_model_len or "",

                data.get("Input", ""),
                data.get("Output", ""),
                data.get("Max-Concurrency", ""),
                data.get("Num", ""),

                data.get("Successful requests", ""),
                data.get("Benchmark duration (s)", ""),
                data.get("Total input tokens", ""),
                data.get("Total generated tokens", ""),

                data.get("Request throughput (req/s)", ""),
                data.get("Output token throughput (tok/s)", ""),
                data.get("Total Token throughput (tok/s)", ""),

                data.get("Mean TTFT (ms)", ""),
                data.get("Median TTFT (ms)", ""),
                data.get("P99 TTFT (ms)", ""),

                data.get("Mean ITL (ms)", ""),
                data.get("Median ITL (ms)", ""),
                data.get("P95 ITL (ms)", ""),
                data.get("P99 ITL (ms)", ""),

                data.get("Mean TPOT (ms)", ""),
                data.get("Median TPOT (ms)", ""),
                data.get("P95 TPOT (ms)", ""),
                data.get("P99 TPOT (ms)", ""),
            ]

            formatted = []
            for v in row:
                try:
                    formatted.append(f"{float(v):.2f}")
                except Exception:
                    formatted.append(str(v))

            rows.append(formatted)

        # 组装 markdown
        table = []
        table.append("| " + " | ".join(headers) + " |")
        table.append("|" + "|".join(["---"] * len(headers)) + "|")

        for r in rows:
            table.append("| " + " | ".join(r) + " |")

        return "\n".join(table)

    def print_summary(self):
        """打印测试摘要"""
        print("\n" + "="*80)
        print("性能测试完成!")
        print("="*80)
        markdown_table = self.generate_markdown_table()
        print("\n" + markdown_table + "\n")
        logger.info(f"所有结果已保存:")
        logger.info(f"  - CSV文件: {self.csv_file}")
        logger.info(f"  - JSON文件: {self.output_file}")

# ================= main =================
def main():
    import argparse
    parser = argparse.ArgumentParser()

    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=30001)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--input", type=int, nargs="+", required=True)
    parser.add_argument("--output", type=int, nargs="+", required=True)
    parser.add_argument("--concurrency", type=int, nargs="+", required=True)
    parser.add_argument("--num", type=int, nargs="+", required=True)

    # ✅ 保持你原来的参数
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--cooldown", type=int, default=10)
    parser.add_argument("--csv-path", required=True)
    parser.add_argument("--output_file")

    parser.add_argument("--dtype")
    parser.add_argument("--tp", type=int)
    parser.add_argument("--max-model-len", type=int)
    parser.add_argument("--tokenizer", default=None,
                        help="Tokenizer path or name for vllm bench serve")

    args = parser.parse_args()

    runner = BenchmarkRunner(
        args.host, args.port, args.model, args.dataset,
        csv_path=args.csv_path,
        output_file=args.output_file,
        dtype=args.dtype,
        tp=args.tp,
        max_model_len=args.max_model_len,
        tokenizer=args.tokenizer,
    )

    cmds = runner.generate_commands_from_combinations(
        args.input, args.output, args.concurrency, args.num
    )
    runner.run_commands(cmds, args.timeout, args.cooldown)
    runner.print_summary()

if __name__ == "__main__":
    main()
