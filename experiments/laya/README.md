# Laya as a triple-support judge — benchmark harness

[Laya](https://github.com/NandhaKishorM/laya) (convaiinnovations, Apache-2.0) is a
non-autoregressive decision engine: a bidirectional encoder scores every option at its own
`[MASK]` token, so N typed questions are answered in one forward pass with no token
generation. Its `noul` primitive returns a calibrated binary probability.

Why it is here: `docs/related-work-small-models.md` evaluated the hosted Jev service for the
"does the text support this triple?" role and rejected it as proprietary, concluding that a
local small model under constrained yes/no output covers the same role. Laya is that, at
322M–421M parameters and open weights. kgdc's optional verifier pass (`KGDC_VERIFY=1`) spends
a full worker call per segment on this question and measured net negative (NOTES 39); a
dedicated 33 ms classifier is the obvious thing to try instead.

**Nothing here is wired into the pipeline.** This is a measurement harness only.

## Run

```bash
python -m venv .venv && . .venv/bin/activate
pip install torch --index-url https://download.pytorch.org/whl/cpu   # or a cuXXX wheel
pip install laya
python bench.py                                   # auto device, english checkpoint
python bench.py --device cuda --checkpoint typed-decisions
python bench.py --sizes 1,10,50 --reps 5 --skip-full
```

Regenerate the input from any kgdc run:

```bash
python prepare_data.py runs/chr-2026-09-21/vignette_065.ttl \
    examples/chr/docs/vignette_065.txt -o experiments/laya/vignette_065.json
```

Checkpoints: `english` (ModernBERT-large, 421M), `multilingual` (mmBERT-base, 322M),
`typed-decisions` (ModernBERT-large, 421M). Weights are fp16 on the hub, ~804 MB each; all
three preloaded is ~4.7 GB fp32 in memory.

## What is already known (2026-09-23)

- **Pascal will not work.** Laya's dependency floor is `torch` 2.14. PyTorch dropped Maxwell
  and Pascal from the cu128 builds of 2.8; sm_61 support ends at torch 2.6 / cu126, and CUDA
  13.0 starts at Turing. A GTX 1060 needs ONNX Runtime
  ([receptron/laya](https://github.com/receptron/laya)), a source build with
  `TORCH_CUDA_ARCH_LIST=6.1`, or a different GPU.
- **No CPU number was obtained.** On an i7-1165G7 (4 cores / 8 threads, AVX-512 VNNI) the
  sweep ran for one hour at ~290% CPU without completing a single reported row. Cold load was
  6.3 s and one isolated `predict()` with a single question took 2.06 s including lazy init.
  Treat the laptop as unusable for this and measure on a GPU.
- **512-token input.** A single-question call on a 3.1 kB vignette (~900 tokens) reported
  `usage.input_tokens == 512` exactly. If that is a hard cap rather than a tokenizer default,
  a triple about the fortieth measurement is being judged against a prefix that does not
  contain it, and per-triple judging on long clinical documents needs the note chunked around
  each triple. The benchmark warns when it sees exactly 512. **Verify this before trusting
  any p(supported) on a long document** — it is the single biggest threat to the idea.

## What would make this conclusive

The latency sweep answers "is it affordable". It does not answer "is it right". The next step
is accuracy: label each triple of a scored kgdc output as true positive or false positive
using `examples/chr/score.py`'s identity-hash alignment against the gold ABox, then measure
whether `p(supported)` separates them. kgdc's micro precision is 0.767, so a judge that drops
false positives without touching true positives is worth up to ~0.1 F1 — but only if it beats
the trivial baseline of keeping everything.
