"""segment -> N sub-agents (extract, validate, fix<=k) -> merge -> big-model pass -> validate."""
from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from rdflib import RDF, RDFS, XSD, Graph, Literal, URIRef

from . import llm, prompts
from .progress import log
from .schema import Schema
from .validate import _BAD_IRI, unresolved, validate

MAX_FIX = int(os.getenv("KGDC_MAX_FIX", "2"))
VERIFY = os.getenv("KGDC_VERIFY", "0") == "1"   # one audit call per segment before the union: drop unsupported triples, note missing facts
BIG_MODEL = llm.BIG   # orchestrator role: LLM_BIG_* env, falls back to LLM_*


@dataclass
class Result:
    ttl: str
    conforms: bool
    violations: str
    unresolved: list[str]
    segments: list[dict] = field(default_factory=list)   # per-agent trace
    llm_calls: int = 0
    usage: dict = field(default_factory=dict)             # tokens/cost per role (llm.usage_summary)
    error: str | None = None                              # set when the merge pass failed (graph = un-merged union)


def json_call(prompt: str, model=None, attempts: int = 2):
    """chat() that must return JSON: strip fences, tolerate a truncated/degenerate tail, retry once."""
    for i in range(attempts):
        raw = llm.strip_fences(llm.chat(prompt, model=model))
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        # salvage: a list -> the longest prefix of well-formed elements; an object -> longest valid prefix
        a = raw.find("[")
        if a >= 0 and (raw.find("{") < 0 or a < raw.find("{")):
            dec, items, pos = json.JSONDecoder(), [], a + 1
            while True:
                while pos < len(raw) and raw[pos] in " \n\r\t,":
                    pos += 1
                if pos >= len(raw) or raw[pos] == "]":
                    break
                try:
                    obj, end = dec.raw_decode(raw, pos)
                except json.JSONDecodeError:
                    nxt = raw.find("{", pos + 1)   # skip the malformed element, resync at the next one
                    if nxt < 0:
                        break
                    pos = nxt
                    continue
                items.append(obj)
                pos = end
            if items:
                log(f"orchestrator JSON salvaged: {len(items)} element(s)")
                return items
        b = raw.find("{")
        if b >= 0:
            try:
                obj, _ = json.JSONDecoder().raw_decode(raw, b)
                return obj
            except json.JSONDecodeError:
                pass
        log(f"orchestrator returned invalid JSON (attempt {i + 1}); retrying")
        prompt += "\n\nYour previous answer was not valid JSON. Return ONLY the JSON, nothing else."
    raise ValueError("orchestrator did not return valid JSON")


def _segment(schema: Schema, text: str, task: str) -> tuple[str, list[dict]]:
    log("segmenting (orchestrator) ...")
    data = json_call(prompts.segment(schema, text, task), model=BIG_MODEL)   # planning: big model
    if not isinstance(data, dict):
        data = {}
    segs = [s for s in data.get("segments", []) if isinstance(s, dict) and s.get("text")]
    if not segs:   # degraded orchestrator reply: treat the whole document as one segment
        log("segmentation unusable; using the whole document as one segment")
        segs = [{"id": "s1", "concepts": list(schema.classes), "text": text}]
    log(f"{len(segs)} segments: " + ", ".join(f"{s['id']}[{','.join(c.split(':')[-1] for c in s.get('concepts', []))}]" for s in segs))
    for s in segs:  # verbatim check — paraphrased spans are the first place facts get invented
        s["verbatim"] = s["text"].strip() in text
        if s["verbatim"]:
            s["text"] = _whole_sentences(text, s["text"].strip())
    return data.get("shared_context", ""), segs


_BOUNDARY = re.compile(r"(?<=[.!?])\s+|\n\s*\n")


def _whole_sentences(text: str, span: str) -> str:
    """Widen a verbatim span to the full sentence(s) it sits in.

    Segmenters like to drop leading clauses ("On <date>, a Body Height ...")
    that carry exactly the facts a constraint will later ask for.
    """
    i = text.find(span)
    j = i + len(span)
    starts = [0] + [m.end() for m in _BOUNDARY.finditer(text)]
    start = max(b for b in starts if b <= i)
    ends = [m.start() for m in _BOUNDARY.finditer(text)] + [len(text)]
    end = min(b for b in ends if b >= j)
    return text[start:end].strip()


def scope_filter(ttl: str, schema: Schema, concepts: list[str], known_ttl: str) -> tuple[str, int]:
    """Ordered mode: a *new* individual typed with a class that an earlier level already built is a
    duplicate (the visit agent re-creating the processes it should link). Drop such individuals and
    every triple about them; the links to them stay and surface as SHACL violations for the fix round.
    Classes no earlier level built (a Unit no segment named) may still be created. Returns (ttl, dropped)."""
    try:
        g = Graph().parse(data=ttl, format="turtle")
    except Exception:  # noqa: BLE001 — unparsable output is handled downstream
        return ttl, 0
    kg = Graph().parse(data=known_ttl, format="turtle") if known_ttl.strip() else Graph()
    known = set(kg.subjects(RDF.type, None))
    iri = lambda q: URIRef(next(ns for p, ns in schema.prefixes.items() if q.startswith(p + ":")) + q.split(":", 1)[1])
    qn = {iri(q): q for q in schema.classes} | {iri(q): q for q in schema.parents}
    # off limits: classes an earlier level built, plus their super/subclasses (the visit agent types the
    # processes it re-creates with the range class MedicalProcedure, not MeasurementProcess)
    built_q = {qn[t] for t in kg.objects(None, RDF.type) if t in qn}
    built = {iri(x) for q in built_q for x in schema.ancestors(q) | schema.descendants(q)}
    mine = {iri(c) for c in concepts if ":" in c} | {iri(d) for c in concepts for d in schema.descendants(c)}
    bad = {s for s, t in g.subject_objects(RDF.type) if s not in known and t not in mine and t in built}
    if not bad:
        return ttl, 0
    for s in bad:
        g.remove((s, None, None))
    for p, ns in schema.prefixes.items():
        g.bind(p, ns)
    comments = "\n".join(l for l in ttl.splitlines() if l.strip().startswith("# UNRESOLVED"))
    return g.serialize(format="turtle") + ("\n" + comments if comments else ""), len(bad)


_PREFIX_LINE = re.compile(r"^\s*(@prefix|PREFIX)\s.*$", re.I | re.M)


def with_prefixes(ttl: str, schema: Schema) -> str:
    """Agents never see or write namespace IRIs (one mistyped character turned every term of a
    document into foreign vocabulary): drop any prefix declaration the model produced and put
    the canonical block in front."""
    return schema.prefix_block() + "\n\n" + _PREFIX_LINE.sub("", ttl).strip() + "\n"


def _agent(schema: Schema, context: str, seg: dict, task: str, known: str = "", known_ttl: str = "") -> dict:
    """One sub-agent: one-shot extraction, then up to MAX_FIX validator-guided fixes.

    ``known`` (prompt block) / ``known_ttl`` (graph) carry entities built by
    earlier levels in ordered mode; validation runs on agent output + known
    graph so links to known IRIs satisfy range/class constraints."""
    trace = {"id": seg["id"], "concepts": seg.get("concepts", []), "text": seg["text"], "cycles": [], "llm_calls": 0}
    schema = schema.subset(trace["concepts"])   # only the relevant slice of the vocabulary goes in the prompt
    name = seg["id"].split(":")[-1]
    log(f"  {name}: extracting ({len(seg['text'])} chars, {len(schema.classes)} classes in view)")
    try:
        ttl = with_prefixes(llm.strip_fences(llm.chat(prompts.extract(schema, context, seg["text"], trace["concepts"], task, known))), schema)
    except Exception as e:  # noqa: BLE001 — one dead agent (context window, endpoint) must not take the document down
        log(f"  {name}: extraction FAILED ({str(e)[:100]})")
        trace.update(ttl="", unresolved=[f"UNRESOLVED: agent {name} failed: {str(e)[:200]}"], error=str(e)[:300])
        return trace
    trace["llm_calls"] += 1
    if known_ttl:   # ordered mode, level >= 1: only individuals of this agent's class are new; the rest exist already
        ttl, dropped = scope_filter(ttl, schema, trace["concepts"], known_ttl)
        if dropped:
            trace["scope_dropped"] = dropped
            log(f"  {name}: dropped {dropped} individual(s) of other classes (already built by earlier levels)")
    prev = None
    for cycle in range(MAX_FIX + 1):
        ok, report, _ = validate(ttl + "\n" + known_ttl if known_ttl else ttl, schema)
        trace["cycles"].append({"cycle": cycle, "conforms": ok, "violations": report[:2000]})
        n_viol = report.count("Constraint Violation")
        log(f"  {name}: cycle {cycle} -> {'conforms' if ok else f'{n_viol} violation(s)'}")
        if ok or cycle == MAX_FIX or report == prev:   # plateau: same report twice -> agent is (honestly) stuck
            if not ok:
                log(f"  {name}: stopping ({'fix budget spent' if cycle == MAX_FIX else 'plateau'})")
            break
        prev = report
        log(f"  {name}: fixing ...")
        try:
            ttl = with_prefixes(llm.strip_fences(llm.chat(prompts.fix(schema, ttl, _cap(report), context, seg["text"], task, known))), schema)   # a verbose SHACL report blew a 32k context
            if known_ttl:   # a fix round re-creates what the scope filter just dropped (dangling link -> "add the node"): filter again
                ttl, dropped = scope_filter(ttl, schema, trace["concepts"], known_ttl)
                if dropped:
                    trace["scope_dropped"] = trace.get("scope_dropped", 0) + dropped
                    log(f"  {name}: fix round re-created {dropped} individual(s) of other classes, dropped again")
        except Exception as e:  # noqa: BLE001 — keep the last graph; the merge pass still sees the violations
            log(f"  {name}: fix FAILED ({str(e)[:100]}), keeping the graph as is")
            trace["error"] = str(e)[:300]
            break
        trace["llm_calls"] += 1
    trace["unresolved"] = unresolved(ttl)   # before the verifier re-serialises (comments do not survive rdflib)
    if VERIFY:
        ttl, trace["verifier"] = _verify(schema, ttl, context, seg["text"], task)
        trace["llm_calls"] += 1
        trace["unresolved"] += [f"MISSING (verifier): {m}" for m in trace["verifier"].get("missing", [])]
        log(f"  {name}: verifier dropped {len(trace['verifier'].get('dropped', []))}, missing {len(trace['verifier'].get('missing', []))}")
    trace["ttl"] = ttl
    if trace["unresolved"]:
        log(f"  {name}: {len(trace['unresolved'])} unresolved (honest gaps)")
    return trace


def _verify(schema: Schema, ttl: str, context: str, segment_text: str, task: str) -> tuple[str, dict]:
    """One worker call over the numbered triples: drop what the text does not support (by index,
    so the verifier can only remove, never invent), and list stated facts the graph lacks —
    those go to the merge pass, which sees the whole document."""
    try:
        g = Graph().parse(data=ttl, format="turtle")
    except Exception:  # noqa: BLE001 — unparsed output is the merge pass's problem, not the verifier's
        return ttl, {"skipped": "not valid Turtle"}
    for p, ns in schema.prefixes.items():
        g.bind(p, ns)
    triples = sorted(g)
    lines = [f"{i}: " + " ".join(x.n3(g.namespace_manager) for x in t) for i, t in enumerate(triples)]
    try:
        v = json_call(prompts.verify(lines, context, segment_text, task))
    except (ValueError, TypeError):
        return ttl, {"skipped": "verifier returned no JSON"}
    v = v if isinstance(v, dict) else {}
    drop = {int(i) for i in v.get("drop", []) if str(i).isdigit() and int(i) < len(triples)}
    gone = {triples[i][0] for i in drop if triples[i][1] == RDF.type}   # an unsupported individual goes entirely
    for i, t in enumerate(triples):
        if i in drop or t[0] in gone:
            g.remove(t)
    return g.serialize(format="turtle"), {"dropped": [lines[i] for i in sorted(drop)], "missing": [str(m) for m in v.get("missing", []) if m]}


def drop_redundant_types(g: Graph, schema: Schema) -> int:
    """Remove `x a Super` where g also says `x a Sub` and Sub ⊑ Super in the ontology. Models add
    the range class next to the real one (`a chr:MeasurementProcess, chr:MedicalProcedure`); it is
    entailed anyway and re-keys the node for identity-hash scoring. Returns the number removed."""
    iri = {q: next(ns for p, ns in schema.prefixes.items() if q.startswith(p + ":")) + q.split(":", 1)[1] for q in schema.parents}
    qn = {v: k for k, v in iri.items()}
    n = 0
    for s in set(g.subjects(RDF.type, None)):
        types = [t for t in g.objects(s, RDF.type) if str(t) in qn]
        for t in types:
            if any(qn[str(t)] in schema.ancestors(qn[str(u)]) - {qn[str(u)]} for u in types if u != t):
                g.remove((s, RDF.type, t)); n += 1
    return n


_NUM = {XSD.float, XSD.double, XSD.decimal, XSD.integer, XSD.int, XSD.long}


def _value_key(o):
    """Literals compare by value (1.0 == "1.0"^^xsd:float, case-insensitive text); IRIs as they are."""
    if isinstance(o, Literal):
        if o.datatype in _NUM:
            try:
                return ("num", float(o))
            except ValueError:
                pass
        return ("lit", str(o).strip().lower())
    return o


def merge_identical(g: Graph) -> int:
    """Merge individuals that say the same thing: same rdf:types and the same non-label statements
    (or, with no statements, the same labels). Agents re-create what another agent built (38
    Measurements written twice by two chunk agents, one "completed" ProcessStatus per process);
    the merge pass that should dedupe them is the call that overflows on big documents. The copy
    others link to survives, every label is kept, a value it already has is not copied twice.
    Repeats until nothing changes: merging two Units makes their Measurements identical. Returns
    the number of nodes merged away."""
    merged = 0
    while True:
        groups: dict = {}
        for s in set(g.subjects(RDF.type, None)):
            if not isinstance(s, URIRef):
                continue
            facts = frozenset((p, _value_key(o)) for p, o in g.predicate_objects(s) if p not in (RDF.type, RDFS.label))
            ident = facts or frozenset(str(l).strip().lower() for l in g.objects(s, RDFS.label))
            if ident:   # a bare typed node with no label identifies nothing
                groups.setdefault((frozenset(g.objects(s, RDF.type)), bool(facts), ident), []).append(s)
        dups = [v for v in groups.values() if len(v) > 1]
        if not dups:
            return merged
        for v in dups:
            keep, *rest = sorted(v, key=lambda s: (-len(set(g.subjects(None, s))), str(s)))
            for d in rest:
                has = {(p, _value_key(o)) for p, o in g.predicate_objects(keep)}
                for p, o in list(g.predicate_objects(d)):
                    g.remove((d, p, o))
                    if (p, _value_key(o)) not in has:   # a second hasQuantityValue re-keys the node
                        g.add((keep, p, o))
                for s, p in list(g.subject_predicates(d)):
                    g.remove((s, p, d))
                    g.add((s, p, keep))
                merged += 1


def _union(schema: Schema, ttls: list[str]) -> tuple[str, list[str]]:
    """Union of the parseable sub-graphs, plus the raw text of those that were not."""
    g = Graph()
    unparsed = []
    for p, ns in schema.prefixes.items():
        g.bind(p, ns)
    for t in ttls:
        try:
            g.parse(data=t, format="turtle")
        except Exception:  # noqa: BLE001 — the merge pass gets it as text; facts must not vanish silently
            unparsed.append(t)
    drop_redundant_types(g, schema)
    n = merge_identical(g)
    if n:
        log(f"union: merged {n} identical individual(s)")
    # rdflib parses IRIs with illegal characters but cannot serialise them; such
    # triples already fail the IRI check in the agent's trace, so drop them here.
    for trip in [t for t in g if any(isinstance(x, URIRef) and _BAD_IRI.search(str(x)) for x in t)]:
        g.remove(trip)
    return g.serialize(format="turtle"), unparsed


def _known_block(g: Graph, schema: Schema) -> str:
    """IRI, type and label of every individual in g — what later agents may link to."""
    from rdflib import RDF, RDFS
    lines = []
    for s_, t in sorted(g.subject_objects(RDF.type), key=lambda x: str(x[0])):
        lbl = g.value(s_, RDFS.label)
        lines.append(f"  {g.namespace_manager.normalizeUri(s_)} a {g.namespace_manager.normalizeUri(t)}"
                     + (f' ; rdfs:label "{lbl}"' if lbl else ""))
    return "\n".join(lines)


MERGE_MAX_CHARS = int(os.getenv("KGDC_MERGE_MAX_CHARS", "12000"))   # per section of the merge prompt
MERGE_MIN_KEEP = float(os.getenv("KGDC_MERGE_MIN_KEEP", "0.5"))   # merge output smaller than this fraction of the union is a cut-off reply
MAX_SEGS_PER_AGENT = int(os.getenv("KGDC_MAX_SEGS_PER_AGENT", "12"))   # a class with more segments gets several agents: one reply must fit the output cap


def _cap(s: str, n: int = MERGE_MAX_CHARS) -> str:
    return s if len(s) <= n else s[:n] + f"\n... [{len(s) - n} more characters omitted]"


def _final(schema: Schema, text: str, merged: str, report: str, unres: list[str], unparsed: list[str],
           task: str, concepts: list[str]) -> tuple[str, bool, str, str | None]:
    """Big-model merge pass with capped inputs; on failure keep the pre-merge graph."""
    prompt = prompts.merge(schema.subset(concepts), text, merged,
                           _cap(report), unres[:50], _cap("\n\n".join(unparsed)), task)
    try:
        final = with_prefixes(llm.strip_fences(llm.chat(prompt, model=BIG_MODEL)), schema)
        err = None
    except Exception as e:  # noqa: BLE001 — e.g. context window exceeded: the union is still a result
        final, err = merged, f"merge pass failed: {str(e)[:300]}"
        log(f"merge FAILED ({str(e)[:80]}...) - returning the un-merged union")
    try:
        gf = Graph().parse(data=final, format="turtle")
        drop_redundant_types(gf, schema)
        merge_identical(gf)
        n_union = len(Graph().parse(data=merged, format="turtle"))
        if len(gf) < MERGE_MIN_KEEP * n_union:   # a merge dedupes, it does not lose half the graph: the reply was cut short
            raise ValueError(f"merge output has {len(gf)} triples, union has {n_union}")
        for p, ns in schema.prefixes.items():
            gf.bind(p, ns)
        final = gf.serialize(format="turtle")
    except Exception as e:  # noqa: BLE001 — a truncated/garbled merge output loses facts: keep the union instead
        final, err = merged, f"merge output rejected ({str(e)[:100]}): kept the un-merged union"
        log(f"merge output rejected ({str(e)[:60]}) - returning the un-merged union")
    ok, report, _ = validate(final, schema)
    return final, ok, report, err


def run_ordered(schema: Schema, text: str, workers: int = 4, task: str = "") -> Result:
    """Bottom-up: one agent per class per dependency level; later levels see earlier entities."""
    context, segs = _segment(schema, text, task)
    present = sorted({c for s in segs for c in s.get("concepts", []) if c in schema.classes})
    levels = schema.build_order(present)
    log("build order: " + " -> ".join("[" + ", ".join(c.split(":")[-1] for c in l) + "]" for l in levels))
    built = Graph()
    for p_, ns in schema.prefixes.items():
        built.bind(p_, ns)
    traces = []
    for level in levels:
        known, known_ttl = _known_block(built, schema), built.serialize(format="turtle")
        log(f"level {levels.index(level)}: {len(level)} agent(s), {len(known.splitlines()) if known else 0} known entities")
        jobs = []
        for cls in level:
            spans = [s["text"] for s in segs if cls in s.get("concepts", [])]
            # a class no segment names (e.g. the encounter itself, folded into the header)
            # still gets built — from the whole document, which its agent can afford here.
            # The last level holds the containers (visit, plan): their links to everything
            # built before are stated all over the document, not in their own segment.
            whole = level is levels[-1] and len(levels) > 1
            if whole or len(spans) <= MAX_SEGS_PER_AGENT:
                jobs.append({"id": cls, "concepts": [cls], "text": text if whole else "\n\n".join(spans) or text})
            else:   # divide: 40 measurement sentences would need a reply beyond any output cap
                for i in range(0, len(spans), MAX_SEGS_PER_AGENT):
                    jobs.append({"id": f"{cls}#{i // MAX_SEGS_PER_AGENT + 1}", "concepts": [cls], "text": "\n\n".join(spans[i:i + MAX_SEGS_PER_AGENT])})
        with ThreadPoolExecutor(max_workers=workers) as ex:
            level_traces = list(ex.map(lambda j: _agent(schema, context, j, task, known, known_ttl), jobs))
        for t in level_traces:
            t["level"] = levels.index(level)
            try:
                g = Graph().parse(data=t["ttl"], format="turtle")
            except Exception:  # noqa: BLE001 — kept in the trace; merge pass gets it as text
                t["unparsed"] = True
                continue
            for trip in g:   # rdflib parses illegal IRIs but cannot serialise them; keep the graph serialisable
                if not any(isinstance(x, URIRef) and _BAD_IRI.search(str(x)) for x in trip):
                    built.add(trip)
        traces += level_traces
    merged, _ = _union(schema, [built.serialize(format="turtle")])
    unparsed = [t["ttl"] for t in traces if t.get("unparsed")]
    _, report, _ = validate(merged, schema)
    unres = [u for t in traces for u in t["unresolved"]]
    log(f"merging (orchestrator): {report.count('Constraint Violation')} violation(s), {len(unres)} unresolved, {len(unparsed)} unparsed")
    final, ok, report, err = _final(schema, text, merged, report, unres, unparsed, task, present)
    log(f"final: {'conforms' if ok else str(report.count('Constraint Violation')) + ' violation(s)'}")
    return Result(ttl=final, conforms=ok, violations=report, unresolved=unresolved(final),
                  segments=[{**t, "shared_context": context, "build_order": levels} for t in traces],
                  llm_calls=1 + sum(t["llm_calls"] for t in traces) + 1, usage=llm.usage_summary(), error=err)


def run(schema: Schema, text: str, workers: int = 4, task: str = "") -> Result:
    """``task``: free-text domain conventions injected into every prompt (see README)."""
    context, segs = _segment(schema, text, task)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        traces = list(ex.map(lambda s: _agent(schema, context, s, task), segs))
    merged, unparsed = _union(schema, [t["ttl"] for t in traces])
    _, report, _ = validate(merged, schema)
    unres = [u for t in traces for u in t["unresolved"]]
    used = sorted({c for t in traces for c in t["concepts"]})
    log(f"merging (orchestrator): {report.count('Constraint Violation')} violation(s), {len(unres)} unresolved, {len(unparsed)} unparsed")
    final, ok, report, err = _final(schema, text, merged, report, unres, unparsed, task, used)
    log(f"final: {'conforms' if ok else str(report.count('Constraint Violation')) + ' violation(s)'}")
    return Result(
        ttl=final, conforms=ok, violations=report, unresolved=unresolved(final),
        segments=[{**t, "shared_context": context} for t in traces],
        llm_calls=1 + sum(t["llm_calls"] for t in traces) + 1, usage=llm.usage_summary(), error=err,
    )
