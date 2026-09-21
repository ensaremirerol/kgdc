"""ADE corpus v2 (Gurulingappa et al. 2012; DRUG-AE.rel + DRUG-DOSE.rel) -> one document per case report.

  python examples/ade/prepare.py examples/ade/docs [--n 20] [--min-triples 3]

A document is the annotated sentences of one PubMed ID in order of first appearance; the gold is
every (drug | causes | adverse effect) and (drug | dosage | dose) pair annotated in them, in the
same JSON shape as the WebNLG docs so `examples/webnlg/score.py` scores it."""
import argparse, json
from collections import OrderedDict
from pathlib import Path

ap = argparse.ArgumentParser(); ap.add_argument("out"); ap.add_argument("--n", type=int, default=20); ap.add_argument("--min-triples", type=int, default=3)
a = ap.parse_args()
here = Path(__file__).parent
docs: dict[str, dict] = OrderedDict()
for fname, pred, oi in (("DRUG-AE.rel", "causes", 2), ("DRUG-DOSE.rel", "dosage", 2)):
    for line in (here / fname).read_text(encoding="utf-8").splitlines():
        f = line.split("|")
        if len(f) < 8: continue
        pmid, sent, obj, drug = f[0], f[1].strip(), f[oi].strip(), f[5].strip()
        d = docs.setdefault(pmid, {"sentences": OrderedDict(), "triples": OrderedDict()})
        d["sentences"][sent] = None
        d["triples"][f"{drug} | {pred} | {obj}"] = None
out = Path(a.out); out.mkdir(parents=True, exist_ok=True); n = 0
for pmid, d in docs.items():
    if len(d["triples"]) < a.min_triples or len(d["sentences"]) < 2: continue
    n += 1; stem = f"ade{n:02d}"
    (out / f"{stem}.txt").write_text(" ".join(d["sentences"]) + "\n", encoding="utf-8")
    (out / f"{stem}.gold.json").write_text(json.dumps({"pmid": pmid, "triples": list(d["triples"])}, indent=1, ensure_ascii=False), encoding="utf-8")
    if n >= a.n: break
print(f"{n} documents in {out} (from {len(docs)} case reports)")
