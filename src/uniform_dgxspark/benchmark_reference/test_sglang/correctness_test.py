#!/usr/bin/env python3
"""Greedy-output correctness check: record outputs for fixed prompts.

Usage: python correctness_test.py <port> <outfile.json>
Compares token-identical outputs across servers (e.g. MTP vs non-MTP).
"""
import argparse
import json
import urllib.request


PROMPTS = [
    "The capital of France is",
    "Write a short poem about the ocean.",
    "The theory of general relativity was proposed by",
    "In computer science, a deadlock occurs when",
    "The mitochondria is the powerhouse of",
    "A brief history of the Roman Empire:",
]


def generate(base_url: str, prompt: str, max_tokens: int = 32, seed: int = 42) -> dict:
    payload = {
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "ignore_eos": True,
        "stream": False,
        "seed": seed,
        "top_k": 1,
    }
    req = urllib.request.Request(
        f"{base_url}/v1/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.loads(resp.read().decode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("port", type=int)
    ap.add_argument("outfile")
    ap.add_argument("--max-tokens", type=int, default=32)
    args = ap.parse_args()

    base = f"http://127.0.0.1:{args.port}"
    results = {}
    for prompt in PROMPTS:
        out = generate(base, prompt, args.max_tokens)
        results[prompt] = {
            "output": out["choices"][0]["text"],
            "usage": out.get("usage", {}),
        }
    with open(args.outfile, "w") as f:
        json.dump(results, f, indent=2)
    print(f"wrote {len(results)} prompts to {args.outfile}")


if __name__ == "__main__":
    main()
