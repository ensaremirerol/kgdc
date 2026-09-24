"""Prompt / format / merge ablation on a CHR sample: same documents, same model, one switch at a time.

  python -u examples/chr/ablation.py OUT_DIR [--docs 30] [--parallel 4] [--variants A,B,C,D,E,F]

Every (variant, document) is one `python -m kgdc ... --ordered` run with the switches of kgdc.pipeline.flags()
set in its environment; finished outputs are skipped, so the run can be resumed. Then every output is
scored (identity-hash F1, examples/chr/score.py) and OUT_DIR/summary.md compares the variants."""
import argparse, csv, json, os, re, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from statistics import mean

HERE = Path(__file__).parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

VARIANTS = {   # name: (description, env)
    "A": ("Turtle, full prompts (today)", {}),
    "B": ("A without the SHACL block (required slots and patterns kept)", {"KGDC_PROMPT_SHACL": "0"}),
    "C": ("A without the no-fabrication rule", {"KGDC_PROMPT_NOFAB": "0"}),
    "D": ("compact graph format, full prompts", {"KGDC_FORMAT": "compact"}),
    "E": ("D without SHACL block and no-fabrication rule, task notes filtered per agent",
          {"KGDC_FORMAT": "compact", "KGDC_PROMPT_SHACL": "0", "KGDC_PROMPT_NOFAB": "0", "KGDC_CONTEXT_FILTER": "1"}),
    "F": ("E with the edit-list merge", {"KGDC_FORMAT": "compact", "KGDC_PROMPT_SHACL": "0", "KGDC_PROMPT_NOFAB": "0",
                                         "KGDC_CONTEXT_FILTER": "1", "KGDC_MERGE": "edits"}),
}


def sample(n: int) -> list[str]:
    """n documents spread evenly over the gold-size range (smallest to largest)."""
    sizes = []
    for g in sorted((HERE / "docs").glob("vignette_*.gold.ttl")):
        sizes.append((sum(1 for l in g.read_text(encoding="utf-8").splitlines() if l.strip().endswith((";", ".", ","))), g.name[:12]))
    sizes.sort()
    step = (len(sizes) - 1) / (n - 1)
    return sorted({sizes[round(i * step)][1] for i in range(n)})


def run_one(out: Path, variant: str, doc: str) -> str:
    vd = out / variant; vd.mkdir(parents=True, exist_ok=True)
    ttl = vd / f"{doc}.ttl"
    if ttl.exists() and ttl.stat().st_size:
        return f"{variant} {doc} skipped (done)"
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1", "LLM_TIMEOUT": os.getenv("LLM_TIMEOUT", "600"),
           "LLM_BIG_MAX_TOKENS": os.getenv("LLM_BIG_MAX_TOKENS", "16000"), **VARIANTS[variant][1]}
    t = time.time()
    with open(vd / f"{doc}.log", "w", encoding="utf-8") as log:
        r = subprocess.run([sys.executable, "-m", "kgdc", "examples/chr/ontology.ttl", "examples/chr/shapes.ttl", f"examples/chr/docs/{doc}.txt",
                            "--ordered", "--context", "examples/chr/context.md", "-o", str(ttl)], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        log.write(f"\nexit={r.returncode} seconds={time.time() - t:.0f}\n")
    return f"{variant} {doc} exit={r.returncode} {time.time() - t:.0f}s"


def collect(out: Path, docs: list[str], variants: list[str]) -> list[dict]:
    from score import score
    rows = []
    for v in variants:
        for d in docs:
            ttl, tr, lg = out / v / f"{d}.ttl", out / v / f"{d}.ttl.trace.json", out / v / f"{d}.log"
            if not (ttl.exists() and tr.exists()):
                continue
            g, o, tp, p, r, f1 = score(ttl, HERE / "docs" / f"{d}.gold.ttl")
            t = json.loads(tr.read_text(encoding="utf-8"))
            u = t.get("usage", {}).get("total", {})
            agents = t.get("segments", [])
            log = lg.read_text(encoding="utf-8") if lg.exists() else ""
            sec = re.search(r"seconds=(\d+)", log)
            rows.append({"variant": v, "doc": d, "gold": g, "out": o, "tp": tp, "P": round(p, 4), "R": round(r, 4), "F1": round(f1, 4),
                         "calls": t.get("llm_calls", 0), "repairs": sum(max(0, a.get("llm_calls", 0) - 1) for a in agents),
                         "prompt_tokens": u.get("prompt_tokens", 0), "completion_tokens": u.get("completion_tokens", 0),
                         "unresolved": len(t.get("unresolved") or []), "unparsed": sum(bool(a.get("unparsed")) for a in agents),
                         "salvaged": sum(a.get("salvaged", 0) for a in agents), "merge_error": bool(t.get("error")),
                         "conforms": bool(t.get("conforms")), "seconds": int(sec.group(1)) if sec else 0})
    return rows


def summary(rows: list[dict], docs: list[str], variants: list[str]) -> str:
    done = {v: {r["doc"] for r in rows if r["variant"] == v} for v in variants}
    common = sorted(set(docs).intersection(*done.values())) if done else []
    lines = [f"# Ablation, {len(common)} documents finished by every variant\n",
             "| variant | what | macro F1 | micro F1 | P | R | calls | repair calls | prompt tokens | output tokens | merge errors | unresolved | min/doc |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    base = None
    for v in variants:
        rs = [r for r in rows if r["variant"] == v and r["doc"] in common]
        if not rs:
            continue
        G = sum(r["gold"] for r in rs); O = sum(r["out"] for r in rs); T = sum(r["tp"] for r in rs)
        P, R = T / O if O else 0, T / G if G else 0
        tot = lambda k: sum(r[k] for r in rs)
        base = base or {k: tot(k) for k in ("prompt_tokens", "completion_tokens", "calls")}
        rel = lambda k: f"{tot(k):,} ({tot(k) / base[k]:.0%})" if base[k] else f"{tot(k):,}"
        lines.append(f"| {v} | {VARIANTS[v][0]} | {mean(r['F1'] for r in rs):.4f} | {2 * P * R / (P + R) if P + R else 0:.4f} | {P:.3f} | {R:.3f} | "
                     f"{rel('calls')} | {tot('repairs')} | {rel('prompt_tokens')} | {rel('completion_tokens')} | {tot('merge_error')} | "
                     f"{tot('unresolved')} | {mean(r['seconds'] for r in rs) / 60:.1f} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out"); ap.add_argument("--docs", type=int, default=30); ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--variants", default="A,B,C,D,E,F"); ap.add_argument("--score-only", action="store_true")
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    variants = a.variants.split(",")
    docs = sample(a.docs)
    (out / "docs.txt").write_text("\n".join(docs) + "\n", encoding="utf-8")
    print(f"{len(docs)} documents x {len(variants)} variants -> {out}", flush=True)
    if not a.score_only:
        jobs = [(v, d) for d in docs for v in variants]   # document-major: an early stop still compares every variant
        with ThreadPoolExecutor(a.parallel) as ex:
            futures = [ex.submit(run_one, out, *j) for j in jobs]
            for i, f in enumerate(as_completed(futures), 1):
                print(f"[{i}/{len(jobs)}] {f.result()}", flush=True)
    rows = collect(out, docs, variants)
    if rows:
        with open(out / "results.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    s = summary(rows, docs, variants)
    (out / "summary.md").write_text(s, encoding="utf-8")
    print(s, flush=True)
