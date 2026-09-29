"""Parse -> vocabulary membership -> SHACL. Returns (ok, report, graph|None)."""
from __future__ import annotations

import os
import re
import threading
from functools import lru_cache

from rdflib import Graph, RDF, URIRef

from .schema import Schema, _META

_BAD_IRI = re.compile(r'[\s<>"{}|\\^`\x00-\x20]|%(?![0-9A-Fa-f]{2})')   # incl. a bare '%' (not percent-encoding)
_LOCK = threading.Lock()
_ALLOWED_PREDS = {str(RDF.type), "http://www.w3.org/2000/01/rdf-schema#label", "http://www.w3.org/2000/01/rdf-schema#comment"}


def _block(kind: str, msg: str, node: str = "") -> str:
    return f"Constraint Violation in {kind}:\n\tSeverity: sh:Violation\n" + (f"\tFocus Node: {node}\n" if node else "") + f"\tMessage: {msg}"


def vocabulary_violations(g: Graph, schema: Schema) -> list[str]:
    """Closed world over the declared terms: every class and predicate must be in the ontology/shapes."""
    out = []
    for s, p, o in g:
        for iri in (s, p, o):
            if isinstance(iri, URIRef) and _BAD_IRI.search(str(iri)):
                out.append(_block("IRISyntax", f"IRI contains characters not allowed in an IRI: {iri}"))
        if str(p) not in schema.terms and str(p) not in _ALLOWED_PREDS:
            out.append(_block("UndeclaredProperty", f"{p} is not a property of the vocabulary", str(s)))
        if p == RDF.type and str(o) not in schema.terms:
            out.append(_block("UndeclaredClass", f"{o} is not a class of the vocabulary", str(s)))
    return sorted(set(out))


def validate(ttl: str, schema: Schema) -> tuple[bool, str, Graph | None]:
    try:
        g = Graph().parse(data=ttl, format="turtle")
    except Exception as e:  # noqa: BLE001
        m = re.search(r"line (\d+)", str(e))
        line = ttl.splitlines()[int(m.group(1)) - 1] if m and int(m.group(1)) <= len(ttl.splitlines()) else ""
        return False, ("Constraint Violation in TurtleSyntax:\n\tSeverity: sh:Violation\n"
                       f"\tMessage: not valid Turtle: {str(e).splitlines()[0]}\n\tOffending line: {line.strip()}\n"
                       "\tHint: every statement starts with a subject IRI (ex:name a chr:Class ; ...). "
                       "IRI local names may only contain letters, digits, '_' and '-'; "
                       "put dates/codes with other characters in literals, not in IRIs."), None
    vocab = vocabulary_violations(g, schema)
    blocks = vocab + (_pyshacl(g, schema) if os.getenv("KGDC_SHACL", "rust") == "pyshacl" else _shacl_rust(ttl, g, schema))
    if not blocks:
        return True, "", g
    return False, "\n".join(blocks), g


def _pyshacl(g: Graph, schema: Schema) -> list[str]:
    import pyshacl   # optional: pip install kgdc[pyshacl]
    with _LOCK:   # rdflib's SPARQL parser (used for SPARQL targets) is not thread-safe
        _, _, report = pyshacl.validate(g, shacl_graph=schema.shapes, ont_graph=schema.ontology,
                                        inference="none", advanced=True)
    return [b for b in re.split(r"\n(?=Constraint )", report) if b.startswith("Constraint") and "sh:Warning" not in b and "sh:Info" not in b]


@lru_cache(maxsize=8)
def _turtle(g: Graph) -> str:
    return g.serialize(format="turtle")


def _shacl_rust(ttl: str, g: Graph, schema: Schema) -> list[str]:
    """Same blocks as pySHACL's text report, so the prompts and the Focus Node / Value Node readers are unchanged.
    The ontology goes into the data graph (pySHACL's ont_graph does the same): the class checks walk rdfs:subClassOf."""
    import shacl_rust
    report = shacl_rust.validate(ttl + "\n" + _turtle(schema.ontology), _turtle(schema.shapes))
    def show(t):   # schema prefixes first, so own_violations() can match "chr:X" (a data graph may bind ":" instead)
        if not (t and t.startswith("<") and t.endswith(">")):
            return t
        iri = t[1:-1]
        ns = max((n for n in schema.prefixes.values() if iri.startswith(n)), key=len, default=None)
        return next(f"{k}:{iri[len(ns):]}" for k, v in schema.prefixes.items() if v == ns) if ns else URIRef(iri).n3(g.namespace_manager)
    out = []
    for r in report["results"]:
        if not r.get("severity", "").endswith("#Violation>"):
            continue
        comp = r.get("sourceConstraintComponent", "").strip("<>")
        msgs = r.get("messages") or [""]
        lines = [f"Constraint Violation in {comp.rsplit('#', 1)[-1]} ({comp}):", "\tSeverity: sh:Violation",
                 f"\tFocus Node: {show(r.get('focusNode'))}"]
        if r.get("value"):
            lines.append(f"\tValue Node: {show(r['value'])}")
        if r.get("resultPath"):
            lines.append(f"\tResult Path: {show(r['resultPath'])}")
        lines.append(f"\tMessage: {' '.join(msgs[1:]) or msgs[0]}")   # the shape's sh:message when it has one, like pySHACL
        out.append("\n".join(lines))
    return out


def unresolved(ttl: str) -> list[str]:
    return [l.strip()[2:] for l in ttl.splitlines() if l.strip().startswith("# UNRESOLVED")]
