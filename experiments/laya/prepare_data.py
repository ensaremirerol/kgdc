"""Render a kgdc output graph into the (text, triples) JSON the benchmark consumes.

    python prepare_data.py runs/chr-2026-09-21/vignette_065.ttl \
        examples/chr/docs/vignette_065.txt -o experiments/laya/vignette_065.json

Triples are flattened to "subject label -- property -- object label" so the
decision engine is asked about a fact, not about Turtle syntax. rdf:type and
rdfs:label are dropped: labels are the identity the rendering already uses, and
type assertions are not the part a text-support judge can usefully rule on.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from rdflib import RDF, RDFS, Graph


def render(ttl_path: Path, text_path: Path) -> dict:
    g = Graph().parse(ttl_path.as_posix(), format="turtle")
    label = {s: str(o) for s, o in g.subject_objects(RDFS.label)}

    def name(node) -> str:
        return label.get(node, str(node).rsplit("/", 1)[-1].rsplit("#", 1)[-1])

    rows = []
    for s, pred, o in g:
        if pred in (RDF.type, RDFS.label):
            continue
        local = str(pred).rsplit("/", 1)[-1].rsplit("#", 1)[-1]
        words = "".join(" " + c.lower() if c.isupper() else c for c in local)
        rows.append(f"{name(s)} — {words.replace('has ', '').strip()} — {name(o)}")
    return {"text": text_path.read_text(), "triples": sorted(set(rows))}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ttl", type=Path, help="kgdc output graph")
    ap.add_argument("text", type=Path, help="source document")
    ap.add_argument("-o", "--out", type=Path, required=True)
    args = ap.parse_args()

    data = render(args.ttl, args.text)
    args.out.write_text(json.dumps(data, indent=1))
    print(f"{args.out}: {len(data['triples'])} triples, {len(data['text'])} chars of text")


if __name__ == "__main__":
    main()
