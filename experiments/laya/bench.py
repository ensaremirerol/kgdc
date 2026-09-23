"""Benchmark Laya (convaiinnovations/laya) as a triple-support judge for kgdc output.

Laya is a non-autoregressive decision engine: a bidirectional encoder scores every
option at its own [MASK] token, so N typed questions are answered in one forward
pass. The `noul` primitive returns a calibrated probability, which is the shape of
"does the source text support this triple?" -- the question kgdc's verifier pass
(KGDC_VERIFY=1) currently spends a full worker call on, at a measured net loss.

What this measures:
  1. latency per predict() call as a function of questions per call, which is the
     number that decides whether per-triple judging is affordable at all;
  2. the same for the whole graph of one document in a single call;
  3. calibrated p(supported) for real triples of a real kgdc output, so the
     judgements can be eyeballed against the note.

Usage:
    python bench.py                          # auto device, english checkpoint
    python bench.py --device cuda --checkpoint typed-decisions
    python bench.py --sizes 1,10,50 --reps 5

Install (CPU):
    pip install torch --index-url https://download.pytorch.org/whl/cpu && pip install laya
Install (CUDA):
    follow pytorch.org for the cuXXX wheel matching your driver, then: pip install laya
    NOTE: Pascal (sm_61, GTX 10xx) is NOT supported by torch >= 2.8 wheels; Laya's
    dependency floor is torch 2.14, so you need Turing (sm_75) or newer.
"""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from pathlib import Path

import torch

import laya

HERE = Path(__file__).parent


def p(*a) -> None:
    """Unbuffered print. stdout redirected to a file is block-buffered otherwise,
    which hides all progress until the process exits."""
    print(*a, flush=True)


def noul_question(statement: str) -> dict:
    return {
        "type": "noul",
        "instructions": f"The clinical note is given. Is this statement supported by the note: {statement}",
        "criteria": {"true": "the note states this", "false": "the note does not state this"},
    }


def sync(device: str) -> None:
    if device == "cuda":
        torch.cuda.synchronize()


def timeit(fn, device: str, reps: int, warmup: int = 2) -> dict:
    for _ in range(warmup):
        fn()
    sync(device)
    ts = []
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        sync(device)
        ts.append((time.perf_counter() - t0) * 1000)
    return {
        "mean_ms": statistics.mean(ts),
        "median_ms": statistics.median(ts),
        "p90_ms": sorted(ts)[max(0, int(0.9 * len(ts)) - 1)],
        "min_ms": min(ts),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkpoint", default="english", choices=list(laya.DEFAULT_MODELS))
    ap.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    ap.add_argument("--reps", type=int, default=10)
    ap.add_argument("--sizes", default="1,5,10,25,50",
                    help="questions per predict() call, comma-separated")
    ap.add_argument("--data", default=str(HERE / "vignette_065.json"))
    ap.add_argument("--skip-full", action="store_true", help="skip the whole-graph call")
    args = ap.parse_args()

    device = args.device
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"

    p(f"# host      : {platform.processor() or platform.machine()}")
    p(f"# torch     : {torch.__version__}, device={device}, cpu threads={torch.get_num_threads()}")
    if device == "cuda":
        p(f"# gpu       : {torch.cuda.get_device_name(0)}, "
          f"cc={'.'.join(map(str, torch.cuda.get_device_capability(0)))}, "
          f"{torch.cuda.get_device_properties(0).total_memory / 2**30:.1f} GiB")
    repo, sub = laya.DEFAULT_MODELS[args.checkpoint]
    p(f"# checkpoint: {args.checkpoint} ({repo}{'/' + sub if sub else ''})")

    t0 = time.perf_counter()
    agent = laya.load(repo, subfolder=sub, device=device)
    p(f"# load      : {time.perf_counter() - t0:.1f} s")

    data = json.loads(Path(args.data).read_text())
    text, triples = data["text"], data["triples"]
    p(f"# document  : {len(text)} chars, {len(triples)} triples\n")

    sizes = [int(s) for s in args.sizes.split(",") if int(s) <= len(triples)]

    p("## latency by questions per call")
    p(f"{'questions':>10} {'mean ms':>10} {'median':>10} {'p90':>10} {'min':>10} {'ms/decision':>12}")
    for k in sizes:
        qs = {f"q{i}": noul_question(t) for i, t in enumerate(triples[:k])}
        r = timeit(lambda: agent.predict(text, qs), device, args.reps)
        p(f"{k:>10} {r['mean_ms']:>10.1f} {r['median_ms']:>10.1f} {r['p90_ms']:>10.1f} "
          f"{r['min_ms']:>10.1f} {r['mean_ms'] / k:>12.1f}")

    if not args.skip_full:
        p(f"\n## whole graph in one call ({len(triples)} triples)")
        qs = {f"q{i}": noul_question(t) for i, t in enumerate(triples)}
        r = timeit(lambda: agent.predict(text, qs), device, max(3, args.reps // 3))
        p(f"mean {r['mean_ms']:.0f} ms total, {r['mean_ms'] / len(triples):.1f} ms per triple")

    p("\n## triple-support judgements (first 15)")
    out = agent.predict(text, {f"q{i}": noul_question(t) for i, t in enumerate(triples[:15])})
    answers = out.get("answers", {})
    for i, t in enumerate(triples[:15]):
        a = answers.get(f"q{i}", {})
        p(f"  p(supported)={a.get('noul', float('nan')):.3f}  "
          f"conf={a.get('confidence', float('nan')):.3f}  {t}")

    usage = out.get("usage", {})
    p(f"\n# usage: {usage}")
    if usage.get("input_tokens") == 512:
        p("# WARNING: input_tokens == 512 exactly. The note is truncated to a 512-token")
        p("#          prefix, so triples about later parts of the document are judged")
        p("#          against text the model never saw. Check whether this is a hard cap")
        p("#          before drawing conclusions from p(supported) on long documents.")


if __name__ == "__main__":
    sys.exit(main())
