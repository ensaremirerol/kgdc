"""Label-level triple F1 of a kgdc Turtle output vs a WebNLG gold triple set: python examples/webnlg/score.py OUT.ttl GOLD.json
Nodes are replaced by their normalised rdfs:label (accents/underscores/dashes/case folded); literals normalised the same way.
The property is compared by local name, so the ontology must keep the DBpedia property names."""
import json, re, sys, unicodedata
from rdflib import Graph, RDF, RDFS, Literal
def _date(x: str):
    """'30 march 2007', 'march 30th, 2007', '1st of june 2009', '2009-06-01' -> '2009-06-01'; None if not a date."""
    if not re.search(r"\b(19|20)\d\d\b", x) or len(x) > 30: return None
    from datetime import datetime
    y = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", x).replace(" of ", " ").strip()
    for f in ("%Y-%m-%d", "%d/%m/%Y", "%d %B %Y", "%B %d %Y", "%d %b %Y", "%b %d %Y", "%B %Y", "%Y"):   # ponytail: the WebNLG forms; add a format when one shows up
        try:
            return datetime.strptime(y, f).date().isoformat()
        except ValueError:
            pass
    return None


def norm(x: str) -> str:
    x = str(x).replace("–", "-").replace("—", "-")   # dashes first: the ASCII fold below would delete them
    x = unicodedata.normalize("NFKD", x).encode("ascii", "ignore").decode()
    x = x.strip().strip('"').replace("_", " ").lower().replace("&", " and ")
    x = re.sub(r"^the\s+", "", x)                              # articles, punctuation and spacing are not facts
    x = re.sub(r"[.,'\"()]", " ", x)
    x = re.sub(r"\s+", " ", x).strip()
    d = _date(x)
    if d: return d
    if re.fullmatch(r"-?\d+(\.\d+)?", x): x = str(float(x)).rstrip("0").rstrip(".") if "." in x else x   # 3500.0 == 3500
    return x
def gold_set(path):
    return {tuple(norm(x) if i != 1 else x for i, x in enumerate(t.split(" | "))) for t in json.load(open(path))["triples"]}
def out_set(path, all_labels=False):
    """Triples over node labels. With all_labels, a node with several rdfs:labels (aliases such as
    "MTX" and "methotrexate") yields one triple per label combination."""
    g = Graph().parse(path, format="turtle")
    labels = {s: sorted({norm(l) for l in g.objects(s, RDFS.label)}) for s in g.subjects(RDF.type)}
    labels = {s: (ls if all_labels else ls[:1]) for s, ls in labels.items() if ls}
    out = set()
    for s, p, o in g:
        if p in (RDF.type, RDFS.label) or s not in labels: continue
        objs = [norm(o)] if isinstance(o, Literal) else labels.get(o, [norm(str(o).rsplit("/", 1)[-1])])
        pl = str(p).rsplit("/", 1)[-1].rsplit("#", 1)[-1]
        out |= {(sl, pl, ol) for sl in labels[s] for ol in objs}
    return out


def _arg_match(a: str, b: str) -> bool:
    """lenient: equal, or one label contains the other (span boundaries: 'hives' vs 'occasional hives')."""
    return a == b or (min(len(a), len(b)) >= 4 and (a in b or b in a))


def lenient_counts(O, G):
    """One-to-one greedy matching of output triples to gold triples under _arg_match. Returns tp."""
    free, tp = set(G), 0
    for s, p, o in sorted(O):
        m = next((g for g in sorted(free) if g[1] == p and _arg_match(s, g[0]) and _arg_match(o, g[2])), None)
        if m: free.discard(m); tp += 1
    return tp
if __name__ == "__main__":
    lenient = "--lenient" in sys.argv
    args = [x for x in sys.argv[1:] if x != "--lenient"]
    O, G = out_set(args[0], all_labels=lenient), gold_set(args[1])
    tp = lenient_counts(O, G) if lenient else len(O & G)
    n_out = len({(s, p, o) for s, p, o in O}) if not lenient else len(out_set(args[0]))   # lenient: count nodes once, not per alias
    P = tp / n_out if n_out else 0; R = tp / len(G) if G else 0
    print(f"{args[0].rsplit('/',1)[-1]:10s} gold={len(G)} out={n_out} tp={tp} P={P:.2f} R={R:.2f} F1={2*P*R/(P+R) if P+R else 0:.3f}{' (lenient)' if lenient else ''}")
    for t in sorted(G - O): print("   FN", t)
    for t in sorted(O - G): print("   FP", t)
