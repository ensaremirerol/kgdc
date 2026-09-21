"""Identity-hash triple F1 of a kgdc output vs a CHR gold ABox (the Thesis scorer, pipeline/canonical_iri.py).

  python examples/chr/score.py OUT.ttl GOLD.ttl [--exact]

Default normalises both sides first: honorifics stripped from labels (the gold has "Ms. X", the text
has "X") and redundant supertypes dropped (`x a MeasurementProcess, MedicalProcedure`). --exact skips
both. THESIS_DIR points at the Thesis checkout (default ~/workspace/03_ids/Thesis)."""
import os, re, sys
from pathlib import Path
from rdflib import Graph, RDFS, Literal
sys.path.insert(0, str(Path(os.getenv("THESIS_DIR", Path.home() / "workspace/03_ids/Thesis")) / "pipeline"))
from canonical_iri import canonical_triples  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import kgdc  # noqa: E402
from kgdc.pipeline import drop_redundant_types  # noqa: E402

HERE = Path(__file__).parent
_schema = None
def schema():
    global _schema
    if _schema is None:
        _schema = kgdc.load(HERE / "ontology.ttl", HERE / "shapes.ttl", example_ns="http://example.org/clinical/")
    return _schema

def prep(g: Graph, exact: bool) -> Graph:
    if exact: return g
    for s, o in list(g.subject_objects(RDFS.label)):
        n = re.sub(r"^(Ms|Mr|Mrs|Dr|Miss)\.?\s+", "", str(o)).strip()
        if n != str(o): g.remove((s, RDFS.label, o)); g.add((s, RDFS.label, Literal(n)))
    drop_redundant_types(g, schema()); return g

def score(out_path, gold_path, exact=False):
    gold = prep(Graph().parse(str(gold_path)), exact)
    out = prep(Graph().parse(data=Path(out_path).read_text().replace("http://example.org/data/", "http://example.org/clinical/"), format="turtle"), exact)
    G, O = canonical_triples(gold), canonical_triples(out)
    tp = len(G & O); p = tp / len(O) if O else 0; r = tp / len(G) if G else 0
    return len(G), len(O), tp, p, r, (2 * p * r / (p + r) if p + r else 0)

if __name__ == "__main__":
    exact = "--exact" in sys.argv; a = [x for x in sys.argv[1:] if x != "--exact"]
    g, o, tp, p, r, f1 = score(a[0], a[1], exact)
    print(f"{Path(a[0]).stem:13s} gold={g:3d} out={o:3d} tp={tp:3d} P={p:.3f} R={r:.3f} F1={f1:.3f}{' (exact)' if exact else ''}")
