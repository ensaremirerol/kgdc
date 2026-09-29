"""python -m kgdc ONTOLOGY SHAPES TEXT [-o out.ttl]"""
import argparse, json, os, sys
from pathlib import Path
from . import load, run, run_ordered, progress

ap = argparse.ArgumentParser()
ap.add_argument("ontology"); ap.add_argument("shapes"); ap.add_argument("text")
ap.add_argument("-o", "--out", help="write final Turtle here (trace goes to <out>.trace.json)")
ap.add_argument("--workers", type=int, default=4)
ap.add_argument("-q", "--quiet", action="store_true", help="no progress output on stderr")
ap.add_argument("--ordered", action="store_true", help="bottom-up: one agent per class per dependency level, later levels reuse earlier entities")
ap.add_argument("--mcp", action="store_true", help="tool-gated mode via kgdc-mcp (build it first): workers add triples through a vocabulary gate, orchestrator judges notes")
ap.add_argument("--context", help="text/markdown file with task-specific conventions, injected into every prompt")
ap.add_argument("--format", choices=["compact", "turtle"], help="graph text the agents read and write (default: KGDC_FORMAT, else compact)")
ap.add_argument("--shacl", choices=["rust", "pyshacl"], help="SHACL engine (default: KGDC_SHACL, else rust; pyshacl needs pip install kgdc[pyshacl])")
a = ap.parse_args()
progress.enabled = not a.quiet
if a.shacl:
    os.environ["KGDC_SHACL"] = a.shacl
if a.out:   # before any LLM call: a missing folder or a read-only mount must not cost a whole run
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
if a.format:
    os.environ["KGDC_FORMAT"] = a.format
from .mcp_pipeline import run_mcp
# UTF-8 explicitly: Windows defaults to cp1252 and would garble "µ" or "°" before the model sees them
r = (run_mcp if a.mcp else run_ordered if a.ordered else run)(load(a.ontology, a.shapes), Path(a.text).read_text(encoding="utf-8"), workers=a.workers,
        task=Path(a.context).read_text(encoding="utf-8") if a.context else "")
if a.out:
    Path(a.out).write_text(r.ttl, encoding="utf-8")
    Path(a.out + ".trace.json").write_text(json.dumps(r.__dict__, indent=1, default=str), encoding="utf-8")
else:
    print(r.ttl)
if a.mcp:
    print(f"conforms={r.conforms} tasks={len(r.tasks)} llm_calls={r.llm_calls} issues={len(r.issues)} decisions={[d.get('action') for d in r.decisions]}", file=sys.stderr)
    for t in r.tasks:
        if t["notes"]: print(f"  {t['class']} notes: {t['notes']}", file=sys.stderr)
    for i in r.issues: print("  ISSUE: " + i, file=sys.stderr)
else:
    if progress.enabled:   # the graph that came out, class by class
        from collections import Counter
        from rdflib import RDF, Graph
        g = Graph().parse(data=r.ttl, format="turtle")
        types = Counter(o.n3(g.namespace_manager) for o in g.objects(None, RDF.type))
        progress.stage("Final graph")
        print(f"{len(g)} statements, {len(set(g.subjects(RDF.type)))} entities"
              + (f", written to {a.out}" if a.out else " (Turtle on stdout)"), file=sys.stderr)
        for cls, n in types.most_common():
            print(f"  {n:4d}  {cls}", file=sys.stderr)
        print(("passes every SHACL constraint" if r.conforms else f"{r.violations.count('Constraint Violation')} SHACL violation(s) left")
              + (f"; merge step: {r.error}" if r.error else ""), file=sys.stderr)
    print(f"conforms={r.conforms} segments={len(r.segments)} llm_calls={r.llm_calls} unresolved={len(r.unresolved)}", file=sys.stderr)
for role, u in r.usage.items():
    cost = f"${u['cost']:.4f}" if u["cost"] is not None else "n/a"
    print(f"  {role:12s} {u['calls']:3d} calls  {u['prompt_tokens']:7d} in  {u['completion_tokens']:6d} out  cost {cost}  {u['model'] or ''}", file=sys.stderr)
if not a.mcp:
    for u in r.unresolved: print("  " + u, file=sys.stderr)
    if r.violations: print(r.violations, file=sys.stderr)
elif r.violations: print(json.dumps(r.violations[:20], indent=1), file=sys.stderr)
sys.exit(0 if r.conforms else 1)
