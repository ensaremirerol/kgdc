"""python -m kgdc ONTOLOGY SHAPES TEXT [-o out.ttl]"""
import argparse, json, sys
from pathlib import Path
from . import load, run

ap = argparse.ArgumentParser()
ap.add_argument("ontology"); ap.add_argument("shapes"); ap.add_argument("text")
ap.add_argument("-o", "--out", help="write final Turtle here (trace goes to <out>.trace.json)")
ap.add_argument("--workers", type=int, default=4)
a = ap.parse_args()
r = run(load(a.ontology, a.shapes), Path(a.text).read_text(), workers=a.workers)
if a.out:
    Path(a.out).write_text(r.ttl)
    Path(a.out + ".trace.json").write_text(json.dumps(r.__dict__, indent=1, default=str))
else:
    print(r.ttl)
print(f"conforms={r.conforms} segments={len(r.segments)} llm_calls={r.llm_calls} unresolved={len(r.unresolved)}", file=sys.stderr)
for u in r.unresolved: print("  " + u, file=sys.stderr)
if r.violations: print(r.violations, file=sys.stderr)
sys.exit(0 if r.conforms else 1)
