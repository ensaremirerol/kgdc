"""Content-addressed IRIs: rename every ABox node by a hash of what it says.

Bottom-up: a node is ready when all its objects are literals, external IRIs
(classes, codes) or already-renamed nodes. Ready nodes get
``ex:<hash>`` from their sorted (predicate, object) pairs; that IRI then
counts as a literal for the nodes pointing at it. Repeat until nothing
changes. Two graphs describing the same thing then share IRIs exactly, so
triple F1 needs no alignment heuristics.

Two key modes:
  ``full``      hash every (predicate, object) pair — exact but brittle: one
                extra rdfs:label re-identifies the node and every ancestor.
  ``identity``  hash rdf:type plus the class's identity predicates only
                (``IDENTITY_KEYS``): a Measurement is its code+value+unit+date,
                a Person its label, a process its date+result, ... Extra or
                missing non-key properties stay ordinary FN/FP triples.
                Nodes of unknown type fall back to ``full``.

Usage: ``canonicalize(graph, keys="identity")``; run the file for the self-check.

Vendored verbatim from the CHR evaluation codebase this work compares against
(``pipeline/canonical_iri.py``); only the import of the normalisation helpers was
repointed at the local ``chr_norm`` module. This is the metric every committed
score in ``results/`` uses, so both sides of every published comparison are scored
by identical code.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from rdflib import BNode, Graph, Literal, URIRef, XSD

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chr_norm import (  # noqa: E402
    EX_PREFIX, _HAS_CODE_PRED, _HAS_UNIT_PRED, _normalize_datetime,
    _normalize_terminology_iri, _normalize_unit_iri,
)


_CHR = "https://w3id.org/shexmap/resource/ontology-schema/d285f599-dc2e-4bd0-83f3-df21defa8821/"
_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"

# class local name -> predicates that identify an instance (full IRIs).
IDENTITY_KEYS: dict[str, tuple[str, ...]] = {
    **{c: (_LABEL,) for c in ("Person", "CareUnit", "ProcessStatus", "Severity", "Device",
                              "AnatomicalStructure", "PharmaceuticalProduct",
                              "PharmaceuticalDoseForm", "PharmaceuticalDose", "Occupation")},
    "Unit":                     (_CHR + "hasCode",),   # compared via _unit_bare_key, not exact IRI
    "Measurement":              (_CHR + "hasCode", _CHR + "hasQuantityValue", _CHR + "hasMeasuredDate"),
    "ClinicalCondition":        (_CHR + "hasCode", _CHR + "hasRecordDate"),
    "DiagnosticStatement":      (_CHR + "hasCode",),
    "MeasurementProcess":       (_CHR + "hasResult", _CHR + "hasPerformedDate"),
    "EvaluationProcess":        (_CHR + "hasObservation", _CHR + "hasCondition", _CHR + "hasPerformedDate"),
    "MedicationAdministration": (_CHR + "hasPharmaceuticalProduct", _CHR + "hasPerformedDate"),
    "MedicalProcedure":         (_CHR + "hasPerformedDate", _LABEL),
    "ClinicalVisit":            (_CHR + "hasPatient", _CHR + "hasDate"),
    "CarePlan":                 (_CHR + "hasMedicalProcedure",),
    "TreatmentPlan":            (_LABEL,),
}


def _key_preds(g: Graph, n) -> set[str] | None:
    """Identity predicates for n's class, or None (= hash everything)."""
    for t in g.objects(n, URIRef(_TYPE)):
        preds = IDENTITY_KEYS.get(str(t).rsplit("/", 1)[-1])
        if preds:
            return set(preds) | {_TYPE}
    return None


def _literal_key(o) -> str:
    if isinstance(o, Literal):
        if o.datatype in (XSD.float, XSD.double, XSD.decimal, XSD.integer):
            try:
                return f"num:{float(o)}"
            except ValueError:
                pass
        return "lit:" + _normalize_datetime(str(o)).strip().lower()
    return "iri:" + _normalize_terminology_iri(str(o))


def canonicalize(g: Graph, prefix: str = EX_PREFIX, keys: str = "identity") -> Graph:
    """Return a copy of ``g`` with every ex:/blank subject renamed by content hash."""
    nodes = {s for s in g.subjects() if isinstance(s, BNode) or str(s).startswith(prefix)}
    new: dict = {}
    unresolved = set(nodes)
    while unresolved:
        progressed = False
        for n in sorted(unresolved, key=str):           # sorted: deterministic
            keep = _key_preds(g, n) if keys == "identity" else None
            pairs = []
            ready = True
            for p, o in g.predicate_objects(n):
                if keep is not None and str(p) not in keep:
                    continue
                if o in nodes:
                    if o not in new:
                        ready = False
                        break
                    pairs.append((str(p), "iri:" + str(new[o])))
                elif str(p) == _HAS_UNIT_PRED or (str(p) == _HAS_CODE_PRED and "ucum" in str(o)):
                    pairs.append((str(p), _normalize_unit_iri(str(o))))   # kg/m2 == kgperm2 == kg_m2
                else:
                    pairs.append((str(p), _literal_key(o)))
            if not ready:
                continue
            h = hashlib.sha1("\n".join(f"{p}\t{o}" for p, o in sorted(pairs)).encode()).hexdigest()[:16]
            new[n] = URIRef(f"{prefix}{h}")
            unresolved.discard(n)
            progressed = True
        if not progressed:
            # ponytail: cycle among the remaining nodes; hash them with their
            # own IRI as a stand-in. Upgrade to SCC hashing if cycles ever appear.
            for n in unresolved:
                new[n] = URIRef(f"{prefix}cyc_{hashlib.sha1(str(n).encode()).hexdigest()[:16]}")
            unresolved.clear()
    out = Graph()
    for pfx, ns in g.namespaces():
        out.bind(pfx, ns)
    for s, p, o in g:
        out.add((new.get(s, s), p, new.get(o, o)))
    return out


def canonical_triples(g: Graph, exclude_type: bool = True, keys: str = "identity") -> set[tuple[str, str, str]]:
    from rdflib import RDF
    return {(str(s), str(p), _literal_key(o) if not str(o).startswith(EX_PREFIX) else str(o))
            for s, p, o in canonicalize(g, keys=keys) if not (exclude_type and p == RDF.type)}


if __name__ == "__main__":
    ttl = """@prefix ex: <http://example.org/clinical/> . @prefix c: <urn:c#> .
    ex:{a} a c:Person ; c:label "Ann" .
    ex:{b} a c:Visit ; c:hasPatient ex:{a} ; c:date "2016-10-04T05:20:29+02:00" .
    """
    g1 = Graph().parse(data=ttl.format(a="p1", b="v1"), format="turtle")
    g2 = Graph().parse(data=ttl.format(a="patient_Ann", b="visit_x").replace("05:20:29+02:00", "05:20:00"),
                       format="turtle")
    assert canonical_triples(g1) == canonical_triples(g2), "same content, different IRIs must match"
    g3 = Graph().parse(data=ttl.format(a="p1", b="v1").replace('"Ann"', '"Bob"'), format="turtle")
    assert canonical_triples(g1) != canonical_triples(g3), "different leaf must propagate upward"
    # identity mode: an extra non-key property must not re-identify the node
    chr_ = "@prefix chr: <%s> . @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> . @prefix ex: <http://example.org/clinical/> .\n" % _CHR
    a = Graph().parse(data=chr_ + 'ex:p a chr:Person ; rdfs:label "Ann" . ex:m a chr:MeasurementProcess ; chr:hasPatient ex:p .', format="turtle")
    b = Graph().parse(data=chr_ + 'ex:x a chr:Person ; rdfs:label "Ann" . ex:y a chr:MeasurementProcess ; chr:hasPatient ex:x ; rdfs:label "extra" .', format="turtle")
    ta, tb = canonical_triples(a), canonical_triples(b)
    assert len(ta & tb) == 2 and len(tb - ta) == 1, (ta, tb)      # shared IRIs; only the label is FP
    assert not (canonical_triples(a, keys="full") & canonical_triples(b, keys="full")) - {t for t in ta if t[1] == _LABEL}
    print("ok")
