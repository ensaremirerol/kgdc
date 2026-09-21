"""Replace outputs whose Turtle does not parse, or that hold fewer than KGDC_MERGE_MIN_KEEP (0.5) of the union's
triples (a merge reply cut off by the output cap), with the union of the agent graphs rebuilt from the trace — what the pipeline now does itself. No LLM calls.

  python examples/chr/repair_truncated.py RUN_DIR      # keeps the original as <doc>.truncated.ttl"""
import json, sys
from pathlib import Path
from rdflib import Graph, URIRef
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import kgdc
from kgdc.pipeline import drop_redundant_types
from kgdc.validate import _BAD_IRI
HERE = Path(__file__).parent
schema = kgdc.load(HERE / "ontology.ttl", HERE / "shapes.ttl")
import os
MIN_KEEP = float(os.getenv("KGDC_MERGE_MIN_KEEP", "0.5"))
R = Path(sys.argv[1]); n_bad = n_fixed = 0
for f in sorted(R.glob("vignette_*.ttl")):
    if f.name.endswith(".truncated.ttl"): continue
    trace = f.with_name(f.name + ".trace.json")
    if not trace.exists(): print("no trace for", f.name); continue
    t = json.load(open(trace))
    g = Graph()
    for p, ns in schema.prefixes.items(): g.bind(p, ns)
    for seg in t["segments"]:
        try: g.parse(data=seg["ttl"], format="turtle")
        except Exception: pass  # noqa: BLE001 — that agent's output was unparsable already in the run
    for trip in [x for x in g if any(isinstance(y, URIRef) and _BAD_IRI.search(str(y)) for y in x)]: g.remove(trip)
    drop_redundant_types(g, schema)
    try:
        cur = Graph().parse(f.as_posix(), format="turtle")
        if len(cur) >= MIN_KEEP * len(g): continue          # parses and is not shrunken: keep it
        why = f"{len(cur)} triples vs union {len(g)}"
    except Exception as e:  # noqa: BLE001
        why = f"not parseable: {str(e)[:40]}"
    n_bad += 1; print(f"{f.name}: {why}")
    f.rename(f.with_name(f.stem + ".truncated.ttl"))
    f.write_text(g.serialize(format="turtle")); n_fixed += 1
print(f"unparsable outputs: {n_bad}, repaired: {n_fixed}")
