"""Compact graph text: one line per individual, short handles instead of IRIs.

    n2 Measurement "Body temperature" hasCode=<https://loinc.org/8310-5>; hasQuantityValue=37.436; hasUnit=n4
    # UNRESOLVED: <what> — not stated in the text

The model reads and writes this; code turns it into triples. Half the tokens of Turtle for the same
graph (NOTES 63), and the model never spells an IRI: handles are resolved to known individuals or
minted here from the node's content, so the same thing written by two agents gets the same IRI.
Literal datatypes come from the vocabulary (property range, SHACL datatype), not from the model.

The parser is forgiving: vocabulary terms by local name or prefixed name, a line about an existing
handle without a class adds statements to it, and a line it cannot read is reported, not fatal."""
from __future__ import annotations

import hashlib
import re

from rdflib import RDF, RDFS, XSD, Graph, Literal, URIRef

from .schema import Schema
from .validate import _BAD_IRI

_HANDLE = re.compile(r"^[A-Za-z_][\w.:\-]*$")


class Vocab:
    """Local-name lookups, ranges and datatypes for one schema."""

    def __init__(self, schema: Schema):
        self.schema = schema
        self.ns = schema.prefixes
        self.ex = schema.prefixes.get("ex", "http://example.org/data/")
        vocab_ns = [ns for p, ns in schema.prefixes.items() if p not in ("ex", "rdf", "rdfs", "xsd")]
        self.default_ns = vocab_ns[0] if vocab_ns else self.ex
        self.by_local: dict[str, str] = {}
        for t in sorted(schema.terms):
            local = re.split(r"[/#]", t)[-1]
            self.by_local.setdefault(local, t)
        self.range: dict[str, str] = {}
        for q, d in schema.properties.items():
            self.range[self.iri(q)] = d.split("->", 1)[1].split(")", 1)[0].strip()
        for lines in schema.constraints.values():
            for l in lines:
                if "datatype " in l:   # "chr:hasQuantityValue: datatype xsd:float — ..."
                    q = l.split(": ", 1)[0].strip()
                    self.range[self.iri(q)] = l.split("datatype ", 1)[1].split(",")[0].split(" ")[0]

    def iri(self, qname: str) -> str:
        p, _, local = qname.partition(":")
        return self.ns[p] + local if p in self.ns else qname

    def term(self, name: str) -> str:
        """'Measurement', 'chr:Measurement' or a full IRI -> full IRI (unknown names land in the
        vocabulary namespace, so the validator reports them as undeclared terms)."""
        name = name.strip().strip("<>")
        if name.startswith("http"):
            return name
        if ":" in name and name.split(":", 1)[0] in self.ns:
            return self.iri(name)
        if name in ("label", "rdfs:label"):
            return str(RDFS.label)
        return self.by_local.get(name, self.default_ns + name)

    def local(self, iri: str) -> str:
        loc = re.split(r"[/#]", str(iri))[-1]
        return loc if self.by_local.get(loc) == str(iri) else self.q(iri)

    def q(self, iri: str) -> str:
        for p, ns in sorted(self.ns.items(), key=lambda kv: -len(kv[1])):
            if str(iri).startswith(ns):
                return f"{p}:{str(iri)[len(ns):]}"
        return f"<{iri}>"

    def literal(self, prop: str, value: str) -> Literal:
        rng = self.range.get(prop, "")
        if rng.startswith("xsd:"):
            return Literal(value, datatype=XSD[rng[4:]])
        return Literal(value)


# ------------------------------------------------------------------ writing
def _val(v: Vocab, o, handles: dict) -> str:
    if o in handles:
        return handles[o]
    if isinstance(o, Literal):
        s = str(o)
        return s if re.fullmatch(r"[\w.:+\-/%{}\[\]*]+", s) else '"' + s.replace('"', '\\"') + '"'
    return f"<{o}>"


def to_compact(g: Graph, schema: Schema, handles: dict, only: set | None = None, full: bool = True, vocab: Vocab | None = None) -> str:
    """One line per typed node of ``g`` (or of ``only``). ``handles`` maps node IRI -> handle and is
    extended with n1, n2, ... for nodes that have none. ``full=False`` writes type and label only."""
    v = vocab or Vocab(schema)
    nodes = sorted(only if only is not None else set(g.subjects(RDF.type, None)), key=str)
    for n in nodes:
        if n not in handles:
            handles[n] = f"n{len(handles) + 1}"
    out = []
    for n in nodes:
        types = ",".join(v.local(t) for t in sorted(g.objects(n, RDF.type), key=str))
        labels = sorted(str(l) for l in g.objects(n, RDFS.label))
        line = f"{handles[n]} {types}" + (' "' + labels[0].replace('"', '\\"') + '"' if labels else "")
        if full:
            po = [f"label={_val(v, Literal(l), handles)}" for l in labels[1:]]
            po += [f"{v.local(p)}={_val(v, o, handles)}" for p, o in sorted(g.predicate_objects(n), key=lambda x: (str(x[0]), str(x[1])))
                   if p not in (RDF.type, RDFS.label)]
            line += (" " + "; ".join(po)) if po else ""
        out.append(line)
    return "\n".join(out)


# ------------------------------------------------------------------ reading
def _split(s: str, sep: str) -> list[str]:
    """Split on ``sep`` outside double quotes and angle brackets."""
    out, cur, q, a = [], "", False, False
    for i, ch in enumerate(s):
        if ch == '"' and (i == 0 or s[i - 1] != "\\"):
            q = not q
        elif ch == "<" and not q:
            a = True
        elif ch == ">" and not q:
            a = False
        if ch == sep and not q and not a:
            out.append(cur); cur = ""
        else:
            cur += ch
    out.append(cur)
    return [x.strip() for x in out if x.strip()]


def _unquote(s: str) -> str:
    s = s.strip()
    return s[1:-1].replace('\\"', '"') if len(s) >= 2 and s[0] == s[-1] == '"' else s


def parse(text: str, schema: Schema, known: dict | None = None, vocab: Vocab | None = None, frozen: set | None = None):
    """Compact text -> (Graph, handle -> IRI, unresolved notes, problems).

    ``known`` maps handle -> IRI for individuals that already exist (and may also map their prefixed
    IRIs, so a model that writes ex:person_Ann still hits the known node). Lines about a handle in
    ``frozen`` are skipped: an agent describes its own class; what earlier agents built stays theirs."""
    v = vocab or Vocab(schema)
    known = dict(known or {})
    notes, problems, rows = [], [], []
    for raw in text.splitlines():
        line = raw.strip().strip("`")
        if not line:
            continue
        if line.startswith("#"):
            if "UNRESOLVED" in line:
                notes.append(line.lstrip("# ").strip())
            continue
        line = line.lstrip("-* ").rstrip(" .")
        m = re.match(r"^(\S+)\s*(.*)$", line)
        head, rest = m.group(1), m.group(2)
        if not _HANDLE.match(head):
            problems.append(f"cannot read: {raw.strip()[:120]}")
            continue
        if frozen and head in frozen:
            continue
        classes, label, props = [], None, rest
        mm = re.match(r'^([A-Za-z_][\w:,\-]*)(?=\s|$)(.*)$', rest)
        if mm and "=" not in mm.group(1):
            classes = [c for c in mm.group(1).split(",") if c]
            props = mm.group(2).strip()
        ml = re.match(r'^"((?:[^"\\]|\\.)*)"(.*)$', props)
        if ml:
            label, props = ml.group(1).replace('\\"', '"'), ml.group(2).strip()
        pairs = []
        for part in _split(props, ";"):
            if "=" not in part:
                problems.append(f"{head}: cannot read '{part[:80]}' (expected property=value)")
                continue
            k, val = part.split("=", 1)
            pairs.append((k.strip(), val.strip()))
        rows.append((head, classes, label, pairs))

    defined = {h for h, c, _, _ in rows if c and h not in known}
    triples, links = [], {}
    for h, classes, label, pairs in rows:
        if h not in known and h not in defined:
            problems.append(f"{h}: statements about a handle that is neither known nor declared with a class")
            defined.add(h)
        for c in classes:
            triples.append((h, str(RDF.type), URIRef(v.term(c))))
        if label is not None:
            triples.append((h, str(RDFS.label), Literal(label)))
        for k, val in pairs:
            p = v.term(k)
            items = _split(val, ",") if "," in val and all(_unquote(x) in known or _unquote(x) in defined for x in _split(val, ",")) else [val]
            for it in items:
                raw_v = _unquote(it) if it.startswith('"') else it
                if it.startswith("<") and it.endswith(">"):
                    if _BAD_IRI.search(it[1:-1]):   # rdflib reads it but cannot write it
                        problems.append(f"{h}: {k}=<{it[1:-1][:80]}> is not a valid IRI")
                        continue
                    o = URIRef(it[1:-1])
                elif not it.startswith('"') and (raw_v in known or raw_v in defined):
                    o = raw_v
                    links.setdefault(h, set()).add(raw_v)
                elif re.match(r"^https?://\S+$", raw_v):
                    o = URIRef(raw_v)
                elif p == str(RDFS.label):
                    o = Literal(raw_v)
                else:
                    o = v.literal(p, raw_v)
                triples.append((h, p, o))

    # mint IRIs for new handles from their content; a node is minted after the nodes it links to
    iri = {h: URIRef(known[h]) if not isinstance(known[h], URIRef) else known[h] for h in known}
    pending = sorted(defined)
    content = {h: sorted(f"{p}\t{o}" for s, p, o in triples if s == h and not (isinstance(o, str) and not isinstance(o, (URIRef, Literal)))) for h in pending}
    while pending:
        progress = False
        for h in list(pending):
            deps = links.get(h, set()) - {h}
            if any(d in pending for d in deps):
                continue
            iri[h] = _mint(v, h, content[h], sorted(str(iri[d]) for d in deps), triples)
            pending.remove(h); progress = True
        if not progress:   # a cycle among new nodes: mint the rest from their own content only
            for h in pending:
                iri[h] = _mint(v, h, content[h], [h], triples)
            pending = []
    g = Graph()
    for p_, ns in schema.prefixes.items():
        g.bind(p_, ns)
    for s, p, o in triples:
        g.add((iri[s], URIRef(p), iri[o] if isinstance(o, str) and not isinstance(o, (URIRef, Literal)) else o))
    return g, {h: iri[h] for h in iri}, notes, problems


def _slug(s: str, n: int = 40) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_-]", "_", s)).strip("_")[:n] or "x"


def _mint(v: Vocab, h: str, content: list[str], deps: list[str], triples) -> URIRef:
    cls = next((re.split(r"[/#]", str(o))[-1] for s, p, o in triples if s == h and p == str(RDF.type)), "thing")
    label = next((str(o) for s, p, o in triples if s == h and p == str(RDFS.label)), "")
    digest = hashlib.sha1("\n".join(content + deps).encode()).hexdigest()[:8]
    return URIRef(f"{v.ex}{_slug(cls).lower()}_{_slug(label) + '_' if label else ''}{digest}")


def to_turtle(text: str, schema: Schema, known: dict | None = None, vocab: Vocab | None = None,
              frozen: set | None = None) -> tuple[str, dict, list[str]]:
    """Compact reply -> (Turtle with prefixes and '# UNRESOLVED' comments, handle -> IRI, problems)."""
    g, h2i, notes, problems = parse(text, schema, known, vocab, frozen)
    ttl = g.serialize(format="turtle")
    if notes:
        ttl += "\n" + "\n".join("# " + n for n in notes) + "\n"
    return ttl, h2i, problems


# ------------------------------------------------------------------ prompt material
def _essentials(schema: Schema, cls: str) -> list[tuple[str, str]]:
    """(property qname, 'required' / 'IRI matching ...') from the SHACL slots of ``cls`` and its ancestors."""
    out = []
    for key in [cls] + sorted(schema.ancestors(cls) - {cls}):
        for l in schema.constraints.get(key, []):
            prop = l.split(": ", 1)[0].strip()
            bits = ["required"] if "MUST have 1" in l else []
            pm = re.search(r"pattern (\S+)", l)
            if pm:
                bits.append("IRI matching " + pm.group(1).replace("\\", ""))
            if bits:
                out.append((prop, ", ".join(bits)))
    return out


def hints(schema: Schema) -> str:
    """The SHACL essentials as short lines, for prompts that carry no SHACL block."""
    return "\n".join(f"- {cls} {p}: {b}" for cls in sorted(schema.classes) for p, b in _essentials(schema, cls))


def _handle_stem(cls: str, taken: dict) -> str:
    """CamelCase initials, unique per class: MeasurementProcess -> mp, Measurement -> m."""
    local = cls.split(":")[-1]
    stem = "".join(c for c in local if c.isupper()).lower() or local[:1].lower()
    while stem in taken.values() and taken.get(cls) != stem:
        stem = local[:len(stem) + 1].lower()
    taken[cls] = stem
    return stem


def template(schema: Schema, hints: bool = False, vocab: Vocab | None = None, built: set | None = None) -> str:
    """One template line per class: every slot its (inherited) properties allow, with a placeholder
    naming what goes there. ``hints`` adds the SHACL essentials (required slots, patterns) as a
    comment line, for prompts that carry no SHACL block. Classes in ``built`` (made by earlier
    agents, ordered mode) get no line of their own: a slot pointing at one takes a KNOWN ENTITY
    handle, which is what the scope filter enforces anyway."""
    v = vocab or Vocab(schema)
    built = built or set()
    stems: dict = {}
    for cls in sorted(schema.classes):
        _handle_stem(cls, stems)
    out = []
    for cls in sorted(schema.classes):
        if cls in built:
            continue
        doms = schema.ancestors(cls)
        slots, req = [], []
        for q, d in sorted(schema.properties.items()):
            if schema._domains(d) and not schema._domains(d) & doms:
                continue
            rng = v.range.get(v.iri(q), d.split("->", 1)[1].split(")", 1)[0].strip())
            ph = (f"<{rng[4:]}>" if rng.startswith("xsd:") else
                  "<KNOWN ENTITY handle>" if rng in built else
                  f"{stems[rng]}1" if rng in stems else "<IRI>")
            slots.append(f"{v.local(v.iri(q))}={ph}")
        if hints:
            req = [f"{v.local(v.iri(p))}: {b}" for p, b in _essentials(schema, cls)]
        line = f"{stems[cls]}1 {cls.split(':')[-1]} \"<label>\"" + (" " + "; ".join(slots) if slots else "")
        out.append(line + (f"\n#   {'; '.join(sorted(set(req)))}" if req else ""))
    return "\n".join(out)


def vocab_block(schema: Schema, vocab: Vocab | None = None) -> str:
    """Classes and properties by local name, with the ontology's own descriptions."""
    v = vocab or Vocab(schema)
    cl = [f"- {v.local(v.iri(c))}: {d}" for c, d in sorted(schema.classes.items())]
    pr = []
    for q, d in sorted(schema.properties.items()):
        dom, rng = d.split("->", 1)[0].strip("( "), d.split("->", 1)[1].split(")", 1)[0].strip()
        com = d.split("):", 1)[1].strip() if "):" in d else ""
        loc = lambda x: " | ".join(v.local(v.iri(y.strip())) if ":" in y and not y.strip().startswith("xsd:") else y.strip() for y in x.split("|"))
        pr.append(f"- {v.local(v.iri(q))} ({loc(dom)} -> {loc(rng)})" + (f": {com}" if com else ""))
    return "CLASSES:\n" + "\n".join(cl) + "\n\nPROPERTIES (domain -> range):\n" + "\n".join(pr)


def report(violations: str, handles: dict, vocab: Vocab) -> str:
    """SHACL / vocabulary report -> one short line per violation, IRIs shown as handles."""
    inv = {str(i): h for i, h in handles.items()}
    def show(x: str) -> str:
        x = x.strip()
        if x in inv:
            return inv[x]
        for p, ns in vocab.ns.items():
            if x.startswith(p + ":") and vocab.iri(x) in inv:
                return inv[vocab.iri(x)]
        return vocab.local(x) if x.startswith("http") else x
    out = []
    for blk in re.split(r"\n(?=Constraint )", violations):
        if not blk.startswith("Constraint"):
            continue
        f = re.search(r"Focus Node: (.*)", blk); val = re.search(r"Value Node: (.*)", blk)
        msg = re.search(r"Message: (.*)", blk); off = re.search(r"Offending line: (.*)", blk)
        m = msg.group(1).strip() if msg else blk.splitlines()[0]
        for iri_ in re.findall(r"https?://\S+", m):
            m = m.replace(iri_, show(iri_))
        who = show(f.group(1)) if f else ""
        extra = f" (value {show(val.group(1))})" if val and f and val.group(1).strip() != f.group(1).strip() else ""
        out.append(f"- {who + ': ' if who else ''}{m}{extra}" + (f" [line: {off.group(1).strip()[:100]}]" if off else ""))
    return "\n".join(dict.fromkeys(out))
