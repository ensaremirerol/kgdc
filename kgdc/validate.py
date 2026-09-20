"""Parse -> vocabulary membership -> SHACL. Returns (ok, report, graph|None)."""
from __future__ import annotations

import re
import threading

import pyshacl
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
    with _LOCK:   # rdflib's SPARQL parser (used for SPARQL targets) is not thread-safe
        ok, _, report = pyshacl.validate(g, shacl_graph=schema.shapes, ont_graph=schema.ontology,
                                         inference="none", advanced=True)
    blocks = vocab + [b for b in re.split(r"\n(?=Constraint )", report) if b.startswith("Constraint") and "sh:Warning" not in b and "sh:Info" not in b]
    if not blocks:
        return True, "", g
    return False, "\n".join(blocks), g


def unresolved(ttl: str) -> list[str]:
    return [l.strip()[2:] for l in ttl.splitlines() if l.strip().startswith("# UNRESOLVED")]
