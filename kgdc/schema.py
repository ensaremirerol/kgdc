"""Read any ontology (RDFS/OWL) + SHACL shapes into prompt-ready text.

Ontology  -> concepts (what exists, how to recognise it in text)  => segmentation.
Shapes    -> per-class slots with constraints (how to fill it)     => extraction.
Nothing here is specific to one vocabulary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from rdflib import Graph, Namespace, RDF, RDFS, OWL, URIRef

SH = Namespace("http://www.w3.org/ns/shacl#")
_CLASS_TYPES = (OWL.Class, RDFS.Class)
_PROP_TYPES = (RDF.Property, OWL.ObjectProperty, OWL.DatatypeProperty,
               URIRef("http://www.w3.org/2000/01/rdf-schema#Property"))
_META = ("http://www.w3.org/1999/02/22-rdf-syntax-ns#", "http://www.w3.org/2000/01/rdf-schema#",
         "http://www.w3.org/2002/07/owl#", "http://www.w3.org/2001/XMLSchema#", str(SH))


@dataclass
class Schema:
    ontology: Graph
    shapes: Graph
    ontology_path: Path
    shapes_path: Path
    classes: dict[str, str] = field(default_factory=dict)      # qname -> description
    properties: dict[str, str] = field(default_factory=dict)   # qname -> "domain -> range: comment"
    constraints: dict[str, list[str]] = field(default_factory=dict)  # class qname -> slot lines
    prefixes: dict[str, str] = field(default_factory=dict)
    terms: set[str] = field(default_factory=set)               # every declared class/property IRI
    parents: dict[str, list[str]] = field(default_factory=dict)  # class qname -> direct superclasses

    def ancestors(self, cls: str) -> set[str]:
        out, todo = set(), [cls]
        while todo:
            c = todo.pop()
            if c not in out:
                out.add(c); todo += self.parents.get(c, [])
        return out

    # ---- relevance ---------------------------------------------------------
    def subset(self, concepts: list[str]) -> "Schema":
        """View restricted to ``concepts``: those classes, the properties whose
        domain is one of them (or an ancestor), and the range classes reached in
        two hops (Process -> Measurement -> Unit) with their own properties.
        ``terms`` stays complete — validation is always against the whole vocabulary."""
        wanted = {c for c in concepts if c in self.classes}
        if not wanted:
            return self

        def props_of(classes: set[str]) -> dict[str, str]:
            doms = set().union(*(self.ancestors(c) for c in classes))
            return {q: d for q, d in self.properties.items()
                    if any(c in d.split("->")[0] for c in doms) or d.startswith("(? ->")}

        for _ in range(2):   # two hops: Process -> Measurement -> Unit; deeper is the merge pass's job
            for d in props_of(wanted).values():
                rng = d.split("->", 1)[1].split(")", 1)[0]
                wanted |= {c for c in self.classes if c in rng}
        props = props_of(wanted)
        return Schema(
            self.ontology, self.shapes, self.ontology_path, self.shapes_path,
            classes={q: d for q, d in self.classes.items() if q in wanted},
            properties=props,
            constraints={k: v for k, v in self.constraints.items() if k in wanted or k in props},
            prefixes=self.prefixes, terms=self.terms, parents=self.parents,
        )

    # ---- rendering ---------------------------------------------------------
    def prefix_block(self) -> str:
        return "\n".join(f"@prefix {p}: <{ns}> ." for p, ns in sorted(self.prefixes.items()))

    def concept_block(self) -> str:
        return "\n".join(f"- {q}: {d}" for q, d in sorted(self.classes.items()))

    def property_block(self) -> str:
        return "\n".join(f"- {q} {d}" for q, d in sorted(self.properties.items()))

    def constraint_block(self) -> str:
        out = []
        for cls, lines in sorted(self.constraints.items()):
            out.append(f"{cls}:")
            out += [f"    {l}" for l in lines]
        return "\n".join(out)


def load(ontology_path: str | Path, shapes_path: str | Path,
         example_ns: str = "http://example.org/data/") -> Schema:
    onto = Graph().parse(str(ontology_path))
    shapes = Graph().parse(str(shapes_path))
    s = Schema(onto, shapes, Path(ontology_path), Path(shapes_path))
    # Only prefixes whose namespace is actually used by a term in either file
    # (rdflib binds ~20 stock vocabularies we don't want in a prompt). A named
    # prefix wins over the empty default prefix for the same namespace.
    used = {str(t).rsplit("#", 1)[0] + "#" if "#" in str(t) else str(t).rsplit("/", 1)[0] + "/"
            for g in (onto, shapes) for t in g.all_nodes() if isinstance(t, URIRef)}
    bindings = [(p, str(ns)) for g in (onto, shapes) for p, ns in g.namespaces()
                if str(ns) not in _META and str(ns) in used]
    for p, ns in sorted(bindings, key=lambda b: not b[0]):      # named prefixes first
        if ns not in s.prefixes.values():
            s.prefixes[p or ns.rstrip("/#").rsplit("/", 1)[-1][:8].lower()] = ns
    s.prefixes.setdefault("ex", example_ns)
    s.prefixes.setdefault("rdfs", str(RDFS))
    s.prefixes.setdefault("xsd", "http://www.w3.org/2001/XMLSchema#")
    by_len = sorted(s.prefixes.items(), key=lambda kv: -len(kv[1]))

    def q(u) -> str:
        u = str(u)
        for p, ns in by_len:
            if u.startswith(ns):
                return f"{p}:{u[len(ns):]}"
        return f"<{u}>"

    for t in _CLASS_TYPES:
        for c in onto.subjects(RDF.type, t):
            if not isinstance(c, URIRef):
                continue
            s.terms.add(str(c))
            label = onto.value(c, RDFS.label) or c.split("/")[-1].split("#")[-1]
            comment = onto.value(c, RDFS.comment)
            parents = [q(p) for p in onto.objects(c, RDFS.subClassOf) if isinstance(p, URIRef)]
            desc = f"{label}" + (f" — {comment}" if comment else "") + (f" (subclass of {', '.join(parents)})" if parents else "")
            s.classes[q(c)] = desc
            s.parents[q(c)] = parents
    for t in _PROP_TYPES:
        for p in onto.subjects(RDF.type, t):
            if not isinstance(p, URIRef):
                continue
            s.terms.add(str(p))
            dom = [q(d) for d in onto.objects(p, RDFS.domain)]
            rng = [q(r) for r in onto.objects(p, RDFS.range)]
            comment = onto.value(p, RDFS.comment)
            s.properties[q(p)] = f"({' | '.join(dom) or '?'} -> {' | '.join(rng) or '?'})" + (f": {comment}" if comment else "")

    # SHACL: class-targeted property shapes -> slot lines; property-targeted shapes -> domain/range lines
    for shape in shapes.subjects(RDF.type, SH.NodeShape):
        target = shapes.value(shape, SH.targetClass)
        sev = shapes.value(shape, SH.severity)
        must = "SHOULD" if sev == SH.Warning else "MUST"
        if target is not None:
            for ps in shapes.objects(shape, SH.property):
                path = shapes.value(ps, SH.path)
                if not isinstance(path, URIRef):
                    continue  # sequence/alternative paths: skip, too vocabulary-specific to render generically
                s.terms.add(str(path))
                bits = []
                mn, mx = shapes.value(ps, SH.minCount), shapes.value(ps, SH.maxCount)
                if mn is not None or mx is not None:
                    bits.append(f"{must} have {mn or 0}..{mx or 'n'}")
                for k, lab in ((SH["class"], "class"), (SH.datatype, "datatype"), (SH.nodeKind, "kind"), (SH.pattern, "pattern")):
                    v = shapes.value(ps, k)
                    if v is not None:
                        bits.append(f"{lab} {q(v) if isinstance(v, URIRef) else v}")
                msg = shapes.value(ps, SH.message) or shapes.value(shape, SH.message)
                s.constraints.setdefault(q(target), []).append(
                    f"{q(path)}: {', '.join(bits)}" + (f" — {msg}" if msg else ""))
        for tgt, role in ((SH.targetSubjectsOf, "subject"), (SH.targetObjectsOf, "object")):
            prop = shapes.value(shape, tgt)
            if prop is None:
                continue
            cls = shapes.value(shape, SH["class"])
            msg = shapes.value(shape, SH.message)
            if cls is not None or msg:
                s.constraints.setdefault(q(prop), []).append(
                    f"{role} {must} be {q(cls) if cls is not None else '…'}" + (f" — {msg}" if msg else ""))
    return s
