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
    unlinkable: list[str] = field(default_factory=list)          # declared but unreachable by any property

    @staticmethod
    def _domains(desc: str) -> set[str]:
        """'(A | B -> C): ...' -> {'A', 'B'}; '?' (undeclared) -> set()"""
        dom = desc.split("->", 1)[0].strip("( ")
        return set() if dom == "?" else {d.strip() for d in dom.split("|")}

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
                    if not self._domains(d) or self._domains(d) & doms}

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

    def skeleton_block(self) -> str:
        """One Turtle template per class, generated from the vocabulary: every
        property whose domain is the class (or an ancestor), with a placeholder
        typed by its range / SHACL datatype. Shows *how to fill* a class."""
        dt = {}   # property -> datatype named by a SHACL slot
        for lines in self.constraints.values():
            for l in lines:
                if "datatype " in l:
                    dt[l.split(":", 1)[0].strip()] = l.split("datatype ", 1)[1].split(",")[0].split(" ")[0]
        out = []
        for cls in sorted(self.classes):
            doms = self.ancestors(cls)
            slots = []
            for q, d in sorted(self.properties.items()):
                rng = d.split("->", 1)[1].split(")", 1)[0].strip()
                if self._domains(d) and not self._domains(d) & doms:
                    continue
                if q in dt:
                    ph = f'"..."^^{dt[q]}'
                elif rng.startswith("xsd:"):
                    ph = f'"..."^^{rng}'
                elif rng in self.classes:
                    ph = f"ex:{rng.split(':')[-1]}_1"
                else:
                    ph = "<https://example.org/replace-with-the-real-iri>"
                slots.append(f"    {q} {ph} ;")
            if not slots:
                continue
            # Placeholders are syntactically valid Turtle (ex:Class_1) on purpose:
            # small models copy templates literally, and `ex:<Class-iri>` copied
            # literally is a parse error with no subject.
            out.append(f"ex:{cls.split(':')[-1]}_1 a {cls} ;\n    rdfs:label \"...\" ;\n" + "\n".join(slots)[:-1] + ".")
        return "\n\n".join(out)

    def descendants(self, cls: str) -> set[str]:
        return {c for c in self.classes if cls in self.ancestors(c)}

    def dependencies(self, cls: str) -> set[str]:
        """Classes an instance of ``cls`` may link to: ranges of its (inherited)
        properties, plus their subclasses (a range of MedicalProcedure means the
        link may point at a MeasurementProcess)."""
        doms = self.ancestors(cls)
        out = set()
        for d in self.properties.values():
            if self._domains(d) and self._domains(d) & doms:
                rng = {r.strip() for r in d.split("->", 1)[1].split(")", 1)[0].split("|")}
                for r in rng & set(self.classes):
                    out |= self.descendants(r)
        return out - {cls}

    def build_order(self, classes: list[str] | None = None) -> list[list[str]]:
        """Levels, leaves first: a class appears after every class it links to.
        Only ``classes`` are ordered; a dependency outside that set is left to
        whichever agent needs it (it creates the individual if absent)."""
        from graphlib import CycleError, TopologicalSorter
        wanted = set(classes or self.classes)
        deps = {c: self.dependencies(c) & wanted for c in wanted}
        # ponytail: a cycle (A links to B, B to A) gets both in the last level;
        # split by strongly connected component if a vocabulary ever needs it.
        while True:
            ts = TopologicalSorter(deps)
            try:
                ts.prepare(); break
            except CycleError as e:
                cyc = set(e.args[1])
                deps = {c: (d - cyc if c in cyc else d) for c, d in deps.items()}
        levels = []
        while ts.is_active():
            ready = sorted(ts.get_ready())
            levels.append(ready); ts.done(*ready)
        return levels

    # ---- rendering ---------------------------------------------------------
    def prefix_block(self) -> str:
        return "\n".join(f"@prefix {p}: <{ns}> ." for p, ns in sorted(self.prefixes.items()))

    def prefix_names(self) -> str:
        """`chr:, ex:, rdf:, rdfs:, xsd:` — what an agent may use; the IRIs stay out of every prompt."""
        return ", ".join(f"{p}:" for p in sorted(self.prefixes))

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
    s.prefixes.setdefault("rdf", str(RDF))     # models mistype this one when left to declare it themselves
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
        # property shapes hang off a class-targeted shape (slot of that class)
        # or off a property-targeted one (rule about that property's values)
        key = target if target is not None else (shapes.value(shape, SH.targetSubjectsOf) or shapes.value(shape, SH.targetObjectsOf))
        if key is not None:
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
                s.constraints.setdefault(q(key), []).append(
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

    # Classes that no property can reach or leave (never a domain or range,
    # nor a subclass of one) cannot be linked into a graph: abstract roots,
    # leftovers from another modelling pattern. Keep them valid (``terms``)
    # but never offer them to an agent.
    linked = set()
    for d in s.properties.values():
        rng = {r.strip() for r in d.split("->", 1)[1].split(")", 1)[0].split("|")}
        for c in s._domains(d) | (rng & set(s.classes)):
            linked |= s.descendants(c)
    s.unlinkable = sorted(set(s.classes) - linked)
    for c in s.unlinkable:
        s.classes.pop(c)
    return s
