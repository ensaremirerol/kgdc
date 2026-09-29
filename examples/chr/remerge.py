"""Re-run only the final merge for notes whose merge failed in a run, from their traces. The agents are not
re-run: the union is rebuilt from the agent graphs in the trace exactly as run_ordered builds it, and
_final() is called with the same inputs. Use it when the original deployment's context was too small for
the merge prompt, with LLM_BIG_* exported (it overrides .env)
pointing at an endpoint with a larger context: prompts reach ~28k tokens plus the merged graph as output,
so use >= 64k context and raise LLM_BIG_MAX_TOKENS (default here 16000) if the endpoint allows.

  python examples/chr/remerge.py RUN_DIR OUT_DIR [--dry-run]

OUT_DIR gets <doc>.ttl, <doc>.ttl.trace.json (the original trace, with a "remerge" entry) and scores.txt
for every re-merged note. RUN_DIR is not modified. --dry-run builds each merge prompt and prints its size
without calling a model."""
import json, os, signal, sys, time
from pathlib import Path
from rdflib import Graph, URIRef
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import kgdc
from kgdc import llm, prompts
from kgdc.pipeline import _union, _final, _cap, flags, with_prefixes
from kgdc.validate import validate, _BAD_IRI

HERE = Path(__file__).parent
schema = kgdc.load(HERE / "ontology.ttl", HERE / "shapes.ttl")
task = (HERE / "context.md").read_text(encoding="utf-8")   # run_all.sh passes --context examples/chr/context.md


def merge_inputs(trace: dict) -> tuple[str, str, list[str], list[str], list[str]]:
    """(merged, report, unresolved, unparsed, present), as run_ordered computes them before calling _final."""
    built = Graph()
    for p, ns in schema.prefixes.items():
        built.bind(p, ns)
    for t in trace["segments"]:
        if t.get("unparsed"):
            continue
        # with_prefixes: the 09-21 traces still carry agent graphs with a mistyped CHR namespace (NOTES 60)
        for trip in Graph().parse(data=with_prefixes(t["ttl"], schema), format="turtle"):
            if not any(isinstance(x, URIRef) and _BAD_IRI.search(str(x)) for x in trip):
                built.add(trip)
    merged, _ = _union(schema, [built.serialize(format="turtle")])
    _, report, _ = validate(merged, schema)
    unres = [u for t in trace["segments"] for u in t["unresolved"]]
    unparsed = [t["ttl"] for t in trace["segments"] if t.get("unparsed")]
    present = sorted({c for level in trace["segments"][0]["build_order"] for c in level})
    return merged, report, unres, unparsed, present


WALL = int(os.getenv("REMERGE_WALL_SECONDS", "600"))


class _Hang(BaseException):   # not an Exception: llm.chat's retry loop must not swallow it
    pass


def walled(fn, *a, **k):
    """Hard wall-clock limit on one merge call. The HTTP timeout never fires while OpenRouter keeps the
    connection alive with keep-alive bytes, so a runaway generation could hold a call for half an hour."""
    def hang(*_):
        raise _Hang()
    old = signal.signal(signal.SIGALRM, hang)
    signal.alarm(WALL)
    try:
        return fn(*a, **k)
    except _Hang:
        raise RuntimeError(f"merge call exceeded {WALL} s wall clock") from None
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry = "--dry-run" in sys.argv
    run_dir, out_dir = Path(args[0]), Path(args[1])
    assert out_dir.resolve() != run_dir.resolve(), "OUT_DIR must differ from RUN_DIR"
    # run_all.sh exports 16000; without it the endpoint's default output cap truncates big merged graphs
    os.environ.setdefault("LLM_BIG_MAX_TOKENS", "16000")
    out_dir.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(HERE))
    from score import score
    todo = [tf for tf in sorted(run_dir.glob("vignette_*.ttl.trace.json")) if json.loads(tf.read_text(encoding="utf-8")).get("error")]
    stamp = lambda: time.strftime("%H:%M:%S")
    for k, tf in enumerate(todo, 1):
        trace = json.loads(tf.read_text(encoding="utf-8"))
        doc = tf.name.removesuffix(".ttl.trace.json")
        done = out_dir / f"{doc}.ttl.trace.json"   # resumable: skip only notes whose re-merge succeeded
        prev = json.loads(done.read_text(encoding="utf-8"))["remerge"]["error"] or "" if done.exists() else None
        if not dry and prev == "":
            continue
        if not dry and prev and "wall clock" in prev and os.getenv("REMERGE_SKIP_TIMEOUTS") == "1":
            continue   # hung last time: leave it for a final retry pass instead of stalling the run again
        text = (HERE / "docs" / f"{doc}.txt").read_text(encoding="utf-8")
        merged, report, unres, unparsed, present = merge_inputs(trace)
        if dry:
            prompt = prompts.merge(schema.subset(present), text, merged, _cap(report), unres[:50], _cap("\n\n".join(unparsed)), task)
            print(f"{doc}  prompt {len(prompt):7d} chars (~{len(prompt) // 4:6d} tokens)  union {len(Graph().parse(data=merged, format='turtle')):4d} triples  "
                  f"merge={flags()['merge']}  original error: {trace['error'][:60]}")
            continue
        t0, n_usage = time.monotonic(), len(llm.USAGE)
        print(f"[{stamp()}] START {doc} ({k}/{len(todo)})", flush=True)
        replies, chat = [], llm.chat   # keep the raw merge reply: a rejected one is otherwise lost
        saved = out_dir / f"{doc}.merge_reply.txt"   # a reply rejected before a parser fix: reuse it, no new call
        reused = saved.exists()
        call = (lambda *a, **k: saved.read_text(encoding="utf-8")) if reused else (lambda *a, **k: walled(chat, *a, **{**k, "attempts": 2}))
        llm.chat = lambda *a, **k: replies.append(call(*a, **k)) or replies[-1]
        try:
            final, ok, report, err = _final(schema, text, merged, report, unres, unparsed, task, present)
        finally:
            llm.chat = chat
        if err and replies:
            if reused:
                saved.unlink()   # still rejected with the current parser (e.g. a cut-off reply): call afresh next time
            else:
                saved.write_text(replies[-1], encoding="utf-8")
        elif saved.exists():
            saved.rename(saved.with_suffix(".reused.txt"))   # accepted now: keep it, but do not reuse it again
        out = out_dir / f"{doc}.ttl"
        out.write_text(final, encoding="utf-8")
        trace["ttl"] = final
        used = llm.USAGE[n_usage:]   # calls that returned a response; a call cut off by the wall clock returns none
        tok = {"prompt_tokens": sum(u["prompt_tokens"] for u in used), "completion_tokens": sum(u["completion_tokens"] for u in used),
               "calls": len(used), "reused_reply": reused}
        trace["remerge"] = {"conforms": ok, "error": err, "original_error": trace["error"], "usage": tok}
        (out_dir / f"{doc}.ttl.trace.json").write_text(json.dumps(trace, indent=1, default=str), encoding="utf-8")
        g, o, tp, p, r, f1 = score(out, HERE / "docs" / f"{doc}.gold.ttl")
        line = f"{doc:13s} gold={g:3d} out={o:3d} tp={tp:3d} P={p:.3f} R={r:.3f} F1={f1:.3f}"
        print(f"[{stamp()}] DONE {line}  {time.monotonic() - t0:.0f}s  tokens {tok['prompt_tokens']}/{tok['completion_tokens']}"
              + ("" if used or reused else " (no usage returned)") + (f"  MERGE ERROR: {err[:80]}" if err else ""), flush=True)
        with open(out_dir / "scores.txt", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
