#!/usr/bin/env python3
"""Probe determinism of the first generated token across repeated requests."""
import json
import sys
import urllib.request

BASE = f"http://127.0.0.1:{sys.argv[1]}"
PROMPT = "The capital of France is"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 10


def one():
    payload = {
        "prompt": PROMPT,
        "max_tokens": 1,
        "temperature": 0.0,
        "top_k": 1,
        "ignore_eos": True,
        "seed": 42,
        "stream": False,
    }
    req = urllib.request.Request(
        f"{BASE}/v1/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())["choices"][0]["text"]


outs = [one() for _ in range(N)]
from collections import Counter

c = Counter(outs)
print("unique first tokens:", len(c), "/", N)
for tok, cnt in c.most_common():
    print(f"  {cnt:2d}x {tok!r}")
