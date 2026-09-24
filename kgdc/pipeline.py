"""segment -> N sub-agents (extract, validate, fix<=k) -> merge -> big-model pass -> validate."""
from __future__ import annotations

import collections
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from rdflib import RDF, RDFS, XSD, Graph, Literal, URIRef

from . import compact, llm, prompts
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


def json_call(prompt: str, model=None, attempts: int = 2, max_tokens: int | None = None):
    """chat() that must return JSON: strip fences, tolerate a truncated/degenerate tail, retry once."""
    for i in range(attempts):
        raw = llm.strip_fences(llm.chat(prompt, model=model, max_tokens=max_tokens))
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


def drop_dangling(g: Graph, ns: str) -> int:
    """Remove links to individuals (IRIs in the data namespace ``ns``) that the graph never declares.
    A full-graph merge rewrite used to drop them silently; with the union kept (a failed merge) or an
    edit-list merge they stayed: 2,533 links to undeclared handles in one ablation output (NOTES 64).
    Offline on the 210 ablation outputs this raised macro F1 by 0.01-0.03 in every variant."""
    typed = set(g.subjects(RDF.type, None))
    bad = [(s, p, o) for s, p, o in g if p != RDF.type and isinstance(o, URIRef) and str(o).startswith(ns) and o not in typed]
    for t in bad:
        g.remove(t)
    return len(bad)


def _drop_unwritable(g: Graph) -> int:
    """Remove triples with IRIs rdflib can read but not write (<.../ucum/{#}>): serialising them raises and
    took a whole document down (ablation, variant C, vignette_116). The validator reports them to the agent
    from its reply text before this runs."""
    bad = [t for t in g if any(isinstance(x, URIRef) and _BAD_IRI.search(str(x)) for x in t)]
    for t in bad:
        g.remove(t)
    return len(bad)


def scope_filter(ttl: str, schema: Schema, concepts: list[str], known_ttl: str, removed: list | None = None) -> tuple[str, int]:
    """Keep an agent to its scope. ``schema`` is the agent's slice of the vocabulary (its classes and the
    classes its properties point to). A *new* individual is dropped, with every triple about it, when
      - its class was already built by an earlier level (ordered mode: the visit agent re-creating the
        processes it should link), including super/subclasses, or
      - none of its classes is in the agent's slice, at any level: a ProcessStatus agent that wrote the
        whole note (visit, patient, process, measurement) made later agents' correct nodes look like
        re-creations (vignette_074, compact format, F1 0.93 -> 0.74; 437 such nodes in the 30-document
        Turtle ablation, NOTES 65).
    Links to dropped nodes stay and surface as violations for the repair round. Undeclared classes are left
    for the validator to report. Returns (ttl, dropped)."""
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
    in_slice = lambda t: t in qn and bool(schema.ancestors(qn[t]) & set(schema.classes))
    reason = {}
    for s, t in g.subject_objects(RDF.type):
        if s in known or t in mine:
            continue
        if t in built:
            reason[s] = "built"
    for s in set(g.subjects(RDF.type, None)) - known - set(reason):
        types = [t for t in g.objects(s, RDF.type) if t in qn]
        if types and not any(t in mine or in_slice(t) for t in types):
            reason[s] = "scope"
    bad = set(reason)
    # properties: an agent asserts only the properties of its slice. The ProcessStatus agent wrote
    # "status hasStatus ..." (hasStatus belongs to the process agent) and spent its repair rounds on the
    # domain violation. Undeclared properties stay: the validator reports them to the agent.
    allowed_p = {iri(q) for q in schema.properties} | {RDF.type, RDFS.label}
    declared = {str(t) for t in schema.terms}
    stray = [(s, p, o) for s, p, o in g if s not in bad and p not in allowed_p and str(p) in declared]
    for t in stray:
        g.remove(t)
    if not bad:
        if not stray:
            return ttl, 0
        _drop_unwritable(g)
        for p, ns in schema.prefixes.items():
            g.bind(p, ns)
        comments = "\n".join(l for l in ttl.splitlines() if l.strip().startswith("# UNRESOLVED"))
        return g.serialize(format="turtle") + ("\n" + comments if comments else ""), 0
    if removed is not None:   # (IRI, classes, reason) of what was dropped, for the agent's next repair prompt
        removed += [(s, [qn[t] for t in g.objects(s, RDF.type) if t in qn], reason[s]) for s in sorted(bad, key=str)]
    for s in bad:
        g.remove((s, None, None))
    _drop_unwritable(g)
    for p, ns in schema.prefixes.items():
        g.bind(p, ns)
    comments = "\n".join(l for l in ttl.splitlines() if l.strip().startswith("# UNRESOLVED"))
    return g.serialize(format="turtle") + ("\n" + comments if comments else ""), len(bad)


def scope_notes(removed: list, known_ttl: str, schema: Schema, name) -> list[str]:
    """Why the scope filter dropped a node, and what to link to instead: without this the repair prompt
    only says that a link points at nothing, about a node the model cannot see any more, and the model
    writes the same node again (NOTES 64). ``name`` renders a known IRI (handle or prefixed name)."""
    if not removed:
        return []
    kg = Graph().parse(data=known_ttl, format="turtle") if known_ttl.strip() else Graph()
    iri = lambda q: URIRef(next(ns for p, ns in schema.prefixes.items() if q.startswith(p + ":")) + q.split(":", 1)[1])
    out = []
    for node, classes, why in removed:
        cls = ", ".join(c.split(":")[-1] for c in classes)
        if why == "scope":
            out.append(f"{name(node)} ({cls}) was removed: other agents build {cls}. Describe only the classes you were asked "
                       f"for and leave out statements that need anything else.")
            continue
        family = {iri(x) for c in classes for x in schema.ancestors(c) | schema.descendants(c)}
        cands = sorted({s for s, t in kg.subject_objects(RDF.type) if t in family}, key=str)
        show = ", ".join(f'{name(s)} "{kg.value(s, RDFS.label) or ""}"' for s in cands[:12]) or "none"
        out.append(f"{name(node)} ({cls}) was removed: individuals of this class were "
                   f"built by earlier agents. Link to the matching KNOWN ENTITY instead of creating one: {show}")
    return out


def own_violations(report: str, ttl: str, known_ttl: str, schema: Schema) -> str:
    """Keep the violations this agent can act on. Validation runs on the agent's graph plus the known
    graph (so links to known IRIs type-check), which also reports what is wrong with an earlier agent's
    node: every later agent then spends its repair rounds on something outside its scope (NOTES 64).
    A violation on a known node stays only if this agent wrote statements about that node."""
    if not report or not known_ttl.strip():
        return report
    try:
        own = set(Graph().parse(data=ttl, format="turtle").subjects())
    except Exception:  # noqa: BLE001 — a syntax report is the agent's own
        return report
    kg = Graph().parse(data=known_ttl, format="turtle")
    names = set()
    for n in set(kg.subjects()) - own:
        names.add(str(n))
        names |= {f"{p}:{str(n)[len(ns):]}" for p, ns in schema.prefixes.items() if str(n).startswith(ns)}
    keep = []
    for blk in re.split(r"\n(?=Constraint )", report):
        f = re.search(r"Focus Node: (.*)", blk)
        if not (f and f.group(1).strip() in names):
            keep.append(blk)
    return "\n".join(keep).strip()


_PREFIX_LINE = re.compile(r"^\s*(@prefix|PREFIX)\s.*$", re.I | re.M)


def with_prefixes(ttl: str, schema: Schema) -> str:
    """Agents never see or write namespace IRIs (one mistyped character turned every term of a
    document into foreign vocabulary): drop any prefix declaration the model produced and put
    the canonical block in front."""
    return schema.prefix_block() + "\n\n" + _PREFIX_LINE.sub("", ttl).strip() + "\n"


def flags() -> dict:
    """Prompt and pipeline switches, read per call so one process can run one variant (examples/chr/ablation.py).

    KGDC_FORMAT         turtle | compact   graph text the model reads and writes (compact: kgdc/compact.py)
    KGDC_PROMPT_SHACL   1 | 0              SHACL block in the extraction prompt (0: only required slots and patterns)
    KGDC_PROMPT_NOFAB   1 | 0              the no-fabrication / UNRESOLVED rule in extraction and fix prompts
    KGDC_CONTEXT_FILTER 0 | 1              give each agent only the task-note bullets about its classes
    KGDC_SALVAGE        0 | 1              keep the statements of an unparseable Turtle reply that parse on their own
    KGDC_MERGE          rewrite | edits    final pass rewrites the graph, or returns edits (add / same / remove)"""
    return {"format": os.getenv("KGDC_FORMAT", "turtle"), "shacl": os.getenv("KGDC_PROMPT_SHACL", "1") == "1",
            "nofab": os.getenv("KGDC_PROMPT_NOFAB", "1") == "1", "task_filter": os.getenv("KGDC_CONTEXT_FILTER", "0") == "1",
            "salvage": os.getenv("KGDC_SALVAGE", "0") == "1", "merge": os.getenv("KGDC_MERGE", "rewrite")}


_STATEMENT_END = re.compile(r"(?<=\s\.)[ \t]*\n(?=\s*(?:[A-Za-z_][\w\-]*:|<|#|$))")


def salvage(ttl: str, schema: Schema) -> tuple[str, int, int]:
    """An unparseable reply (usually cut off at the output limit) -> the top-level statements that parse
    on their own, plus its UNRESOLVED notes. 49 broken Measurement replies of the 200-document batch held
    1,487 parseable statements out of 1,547 (NOTES 63). Returns (ttl, statements kept, statements seen)."""
    body = _PREFIX_LINE.sub("", ttl)
    g, kept, seen = Graph(), 0, 0
    for p_, ns in schema.prefixes.items():
        g.bind(p_, ns)
    for st in _STATEMENT_END.split(body):
        if not st.strip() or st.strip().startswith("#"):
            continue
        seen += 1
        try:   # parse on its own first: rdflib adds triples as it reads, so a failing statement would leave a fragment behind
            g += Graph().parse(data=schema.prefix_block() + "\n" + st, format="turtle")
            kept += 1
        except Exception:  # noqa: BLE001 — the cut-off statement, or a malformed one
            pass
    notes = "\n".join(l for l in ttl.splitlines() if l.strip().startswith("# UNRESOLVED"))
    _drop_unwritable(g)
    return with_prefixes(g.serialize(format="turtle"), schema) + ("\n" + notes if notes else ""), kept, seen


def _readable(ttl: str, schema: Schema, trace: dict, name: str) -> str:
    """KGDC_SALVAGE=1: a reply that does not parse is replaced by what can be salvaged from it."""
    if not flags()["salvage"]:
        return ttl
    try:
        Graph().parse(data=ttl, format="turtle")
        return ttl
    except Exception:  # noqa: BLE001
        out, kept, seen = salvage(ttl, schema)
        if not kept:
            return ttl
        trace["salvaged"] = trace.get("salvaged", 0) + kept
        log(f"  {name}: reply did not parse, salvaged {kept} of {seen} statements")
        return out


def _agent(schema: Schema, context: str, seg: dict, task: str, known: str = "", known_ttl: str = "") -> dict:
    """One sub-agent: one-shot extraction, then up to MAX_FIX validator-guided fixes.

    ``known`` (prompt block) / ``known_ttl`` (graph) carry entities built by
    earlier levels in ordered mode; validation runs on agent output + known
    graph so links to known IRIs satisfy range/class constraints."""
    F = flags()
    if F["format"] == "compact":
        return _agent_compact(schema, context, seg, task, known_ttl, F)
    trace = {"id": seg["id"], "concepts": seg.get("concepts", []), "text": seg["text"], "cycles": [], "llm_calls": 0}
    full, schema = schema, schema.subset(trace["concepts"])   # only the relevant slice of the vocabulary goes in the prompt
    if F["task_filter"]:
        task = prompts.filter_task(task, schema, full)
    name = seg["id"].split(":")[-1]
    log(f"  {name}: extracting ({len(seg['text'])} chars, {len(schema.classes)} classes in view)")
    try:
        ttl = with_prefixes(llm.strip_fences(llm.chat(prompts.extract(schema, context, seg["text"], trace["concepts"], task, known,
                                                                      shacl=F["shacl"], nofab=F["nofab"]))), schema)
        ttl = _readable(ttl, schema, trace, name)
    except Exception as e:  # noqa: BLE001 — one dead agent (context window, endpoint) must not take the document down
        log(f"  {name}: extraction FAILED ({str(e)[:100]})")
        trace.update(ttl="", unresolved=[f"UNRESOLVED: agent {name} failed: {str(e)[:200]}"], error=str(e)[:300])
        return trace
    trace["llm_calls"] += 1
    removed: list = []
    qname = lambda s: next((f"{p_}:{str(s)[len(ns):]}" for p_, ns in sorted(schema.prefixes.items(), key=lambda kv: -len(kv[1]))
                            if str(s).startswith(ns)), f"<{s}>")
    ttl, dropped = scope_filter(ttl, schema, trace["concepts"], known_ttl, removed)   # every level: an agent builds its own classes only
    if dropped:
        trace["scope_dropped"] = dropped
        log(f"  {name}: dropped {dropped} individual(s) outside its scope (built earlier, or another agent's class)")
    prev = None
    for cycle in range(MAX_FIX + 1):
        ok, report, _ = validate(ttl + "\n" + known_ttl if known_ttl else ttl, schema)
        report = own_violations(report, ttl, known_ttl, schema)
        ok = ok or not report
        notes = scope_notes(removed, known_ttl, schema, qname)
        removed.clear()
        if notes and not ok:   # say why a linked node vanished, else the repair writes it again
            report = "\n".join(f"Constraint Violation in ScopeFilter:\n\tSeverity: sh:Violation\n\tMessage: {n}" for n in notes) + "\n" + report
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
            ttl = with_prefixes(llm.strip_fences(llm.chat(prompts.fix(schema, ttl, _cap(report), context, seg["text"], task, known,
                                                                      nofab=F["nofab"]))), schema)   # a verbose SHACL report blew a 32k context
            ttl = _readable(ttl, schema, trace, name)
            # a fix round re-creates what the scope filter just dropped (dangling link -> "add the node"): filter again
            ttl, dropped = scope_filter(ttl, schema, trace["concepts"], known_ttl, removed)
            if dropped:
                trace["scope_dropped"] = trace.get("scope_dropped", 0) + dropped
                log(f"  {name}: fix round re-created {dropped} individual(s) outside its scope, dropped again")
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


def _agent_compact(full: Schema, context: str, seg: dict, task: str, known_ttl: str, F: dict) -> dict:
    """The agent loop of ``_agent`` with the compact graph format: the model reads and writes one line per
    individual with short handles (kgdc/compact.py); its reply becomes Turtle here, so validation, the
    scope filter and the union are the same as in Turtle mode. Lines the parser cannot read count as
    violations and go back to the model with the SHACL report, in handles instead of IRIs."""
    trace = {"id": seg["id"], "concepts": seg.get("concepts", []), "text": seg["text"], "cycles": [], "llm_calls": 0}
    schema = full.subset(trace["concepts"])
    if F["task_filter"]:
        task = prompts.filter_task(task, schema, full)
    V = compact.Vocab(full)
    name = seg["id"].split(":")[-1]
    kg = Graph().parse(data=known_ttl, format="turtle") if known_ttl.strip() else Graph()
    handles = {n: f"k{i + 1}" for i, n in enumerate(sorted(set(kg.subjects(RDF.type, None)), key=str))}   # IRI -> handle
    known_block = compact.to_compact(kg, full, dict(handles), full=False, vocab=V) if handles else ""
    known_map = {h: n for n, h in handles.items()} | {V.q(n): n for n in handles}   # also ex:person_Ann, if a model writes an IRI
    # classes earlier levels built (with super/subclasses, as the scope filter counts them): no template line of their own
    q = {URIRef(V.iri(c)): c for c in list(full.classes) + list(full.parents)}
    built = {x for t in kg.objects(None, RDF.type) if t in q for x in full.ancestors(q[t]) | full.descendants(q[t])}
    built -= set(trace["concepts"]) | {d for c in trace["concepts"] for d in full.descendants(c)}
    log(f"  {name}: extracting, compact ({len(seg['text'])} chars, {len(schema.classes)} classes in view)")

    def read(reply: str, extra: dict) -> tuple[str, dict, list[str]]:
        ttl_, h2i, problems = compact.to_turtle(llm.strip_fences(reply), full, known_map | extra, V, frozen=set(known_map))
        trace["reply"] = reply[:6000]
        return with_prefixes(ttl_, full), h2i, problems

    try:
        ttl, h2i, problems = read(llm.chat(prompts.extract_compact(schema, context, seg["text"], trace["concepts"], task, known_block,
                                                                   shacl=F["shacl"], nofab=F["nofab"], built=built)), {})
    except Exception as e:  # noqa: BLE001 — one dead agent must not take the document down
        log(f"  {name}: extraction FAILED ({str(e)[:100]})")
        trace.update(ttl="", unresolved=[f"UNRESOLVED: agent {name} failed: {str(e)[:200]}"], error=str(e)[:300])
        return trace
    trace["llm_calls"] += 1

    removed: list = []

    def filtered(ttl_: str) -> str:
        out, dropped = scope_filter(ttl_, schema, trace["concepts"], known_ttl, removed)
        if dropped:
            trace["scope_dropped"] = trace.get("scope_dropped", 0) + dropped
            log(f"  {name}: dropped {dropped} individual(s) outside its scope (built earlier, or another agent's class)")
        return out

    ttl, prev = filtered(ttl), None
    for cycle in range(MAX_FIX + 1):
        ok, report, _ = validate(ttl + "\n" + known_ttl if known_ttl else ttl, schema)
        report = own_violations(report, ttl, known_ttl, full)
        ok = ok or not report
        mine = {h: i for h, i in h2i.items() if h not in known_map}
        shown = {URIRef(i): h for h, i in mine.items()} | handles
        notes = scope_notes(removed, known_ttl, full, lambda s: shown.get(URIRef(s), V.q(s)))
        removed.clear()
        problems = problems + (notes if (notes and (not ok or problems)) else [])
        short = "\n".join(f"- {p}" for p in problems) + ("\n" if problems and report else "") + (compact.report(report, shown, V) if report else "")
        ok = ok and not problems
        trace["cycles"].append({"cycle": cycle, "conforms": ok, "violations": short[:2000]})
        n_viol = short.count("\n") + 1 if short else 0
        log(f"  {name}: cycle {cycle} -> {'conforms' if ok else f'{n_viol} violation(s)'}")
        if ok or cycle == MAX_FIX or short == prev:
            if not ok:
                log(f"  {name}: stopping ({'fix budget spent' if cycle == MAX_FIX else 'plateau'})")
            break
        prev = short
        log(f"  {name}: fixing ...")
        try:
            g = Graph().parse(data=ttl, format="turtle")
            own = {s for s in g.subjects(RDF.type, None) if s not in handles}
            graph_txt = compact.to_compact(g, full, dict(shown), only=own, vocab=V)
            notes = "\n".join(l.strip() for l in ttl.splitlines() if l.strip().startswith("# UNRESOLVED"))
            ttl, h2i, problems = read(llm.chat(prompts.fix_compact(schema, graph_txt + ("\n" + notes if notes else ""), _cap(short), context,
                                                                   seg["text"], task, known_block, nofab=F["nofab"])), mine)
            ttl = filtered(ttl)
        except Exception as e:  # noqa: BLE001 — keep the last graph; the merge pass still sees the violations
            log(f"  {name}: fix FAILED ({str(e)[:100]}), keeping the graph as is")
            trace["error"] = str(e)[:300]
            break
        trace["llm_calls"] += 1
    trace["unresolved"] = unresolved(ttl)
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
    n = drop_dangling(g, schema.prefixes.get("ex", "http://example.org/data/"))
    if n:
        log(f"union: dropped {n} link(s) to undeclared individuals")
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
    if flags()["merge"] == "edits":
        return _final_edits(schema, text, merged, report, unres, unparsed, task, concepts)
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
        drop_dangling(gf, schema.prefixes.get("ex", "http://example.org/data/"))
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


MERGE_EDIT_MAX_TOKENS = int(os.getenv("KGDC_MERGE_EDIT_MAX_TOKENS", "3000"))   # an edit list is short: the context is left to the input
MERGE_EDIT_LIMIT = 60   # edits per kind; each one is validated on its own


def _violation_set(report: str) -> set:
    """(focus node, message) of every violation in a report: an edit may not add one."""
    out = set()
    for blk in re.split(r"\n(?=Constraint )", report or ""):
        if blk.startswith("Constraint"):
            f = re.search(r"Focus Node: (.*)", blk); m = re.search(r"Message: (.*)", blk)
            out.add(((f.group(1).strip() if f else ""), (m.group(1).strip() if m else blk[:120])))
    return out


def apply_edits(g: Graph, edits: dict, handles: dict, schema: Schema, vocab: "compact.Vocab") -> dict:
    """Apply a merge-pass edit list to ``g`` in place. ``handles`` maps handle -> IRI of the graph the
    model saw. same: the second node's statements and incoming links move to the first. remove: the
    statements given as "<handle> <property>=<value>" (values compared as merge_identical compares them).
    add: graph lines; new handles are minted like any agent's. Returns counts of what was applied."""
    done = {"same": 0, "remove": 0, "add": 0, "unreadable": 0}
    for pair in edits.get("same") or []:
        if not (isinstance(pair, list) and len(pair) == 2 and pair[0] in handles and pair[1] in handles) or pair[0] == pair[1]:
            done["unreadable"] += 1
            continue
        keep, drop = URIRef(handles[pair[0]]), URIRef(handles[pair[1]])
        has = {(p, _value_key(o)) for p, o in g.predicate_objects(keep)}
        for p, o in list(g.predicate_objects(drop)):
            g.remove((drop, p, o))
            if (p, _value_key(o)) not in has:
                g.add((keep, p, o))
        for s, p in list(g.subject_predicates(drop)):
            g.remove((s, p, drop))
            g.add((s, p, keep))
        done["same"] += 1
    for line in edits.get("remove") or []:
        rg, _, _, problems = compact.parse(str(line), schema, known=handles, vocab=vocab)
        for s, p, o in rg:
            hit = [(s, p, x) for x in g.objects(s, p) if _value_key(x) == _value_key(o)]
            for t in hit:
                g.remove(t)
            done["remove"] += bool(hit)
        done["unreadable"] += len(problems)
    add = "\n".join(str(l) for l in edits.get("add") or [])
    if add.strip():
        ag, _, _, problems = compact.parse(add, schema, known=handles, vocab=vocab)
        g += ag
        done["add"] = len(ag)
        done["unreadable"] += len(problems)
    return done


def _final_edits(schema: Schema, text: str, merged: str, report: str, unres: list[str], unparsed: list[str],
                 task: str, concepts: list[str]) -> tuple[str, bool, str, str | None]:
    """KGDC_MERGE=edits: the model sees the graph in the compact format and returns edits (add / same /
    remove) instead of rewriting it. The reply is a few hundred tokens instead of the whole graph, so the
    input keeps most of the context (NOTES 63). The edited graph is kept only if it has no more
    violations than the unedited one."""
    V = compact.Vocab(schema)
    g = Graph().parse(data=merged, format="turtle")
    for p, ns in schema.prefixes.items():
        g.bind(p, ns)
    iri2h: dict = {}
    graph_txt = compact.to_compact(g, schema, iri2h, vocab=V)
    handles = {h: i for i, h in iri2h.items()}
    ok0, rep0, _ = validate(merged, schema)
    prompt = prompts.merge_edits(schema.subset(concepts), text, graph_txt, _cap(compact.report(rep0, iri2h, V)) if rep0 else "",
                                 unres[:50], _cap("\n\n".join(unparsed)), task, nofab=flags()["nofab"])
    try:
        edits = json_call(prompt, model=BIG_MODEL, max_tokens=MERGE_EDIT_MAX_TOKENS)
        if not isinstance(edits, dict):
            raise ValueError("edit list is not a JSON object")
    except Exception as e:  # noqa: BLE001 — the graph before the pass is still a result
        log(f"merge edits FAILED ({str(e)[:80]}) - returning the un-merged union")
        return merged, ok0, rep0, f"merge edits failed: {str(e)[:300]}"
    # "add" only links or annotates what exists: a line that declares a class, adds a label or brings a
    # new handle is refused. On vignette_024 the model reused process handles for new DiagnosticStatements,
    # which re-typed the processes without breaking a single shape (F1 0.88 -> 0.43, NOTES 64).
    add_lines, refused_lines = [], 0
    for line in [str(l) for l in edits.get("add") or [] if str(l).strip()][:MERGE_EDIT_LIMIT]:
        lg, minted, _, problems = compact.parse(line, schema, known=handles, vocab=V)
        head = line.split()[0] if line.split() else ""
        if (problems or head not in handles or set(minted) - set(handles) or (None, RDF.type, None) in lg
                or (None, RDFS.label, None) in lg or not len(lg)):
            refused_lines += 1
        else:
            add_lines.append(line)
    known_all = handles
    queue = [("same", e) for e in (edits.get("same") or [])[:MERGE_EDIT_LIMIT]] + [("add", e) for e in add_lines]
    def copy(src: Graph) -> Graph:   # same prefixes as the report of the union: violations compare as strings
        out = Graph()
        for p, ns in schema.prefixes.items():
            out.bind(p, ns)
        for t in src:
            out.add(t)
        return out

    cur = copy(g)
    base = _violation_set(validate(cur.serialize(format="turtle"), schema)[1])
    kept = collections.Counter(); refused = collections.Counter({"add": refused_lines, "remove": len(edits.get("remove") or [])})
    for kind, e in queue:   # one edit at a time: an edit that introduces a violation is refused, the others stay
        trial = copy(cur)
        done = apply_edits(trial, {kind: [e]}, known_all, schema, V)
        if not any(done[k] for k in ("same", "remove", "add")):
            refused[kind] += 1
            continue
        _, rep_t, _ = validate(trial.serialize(format="turtle"), schema)
        v = _violation_set(rep_t)
        if v <= base:
            cur, base = trial, v
            kept[kind] += 1
        else:
            refused[kind] += 1
    for p, ns in schema.prefixes.items():
        cur.bind(p, ns)
    drop_redundant_types(cur, schema)
    merge_identical(cur)
    drop_dangling(cur, schema.prefixes.get("ex", "http://example.org/data/"))
    final = cur.serialize(format="turtle")
    ok, rep, _ = validate(final, schema)
    log(f"merge edits: kept {dict(kept)}, refused {dict(refused)}; violations {rep0.count('Constraint Violation')} -> {rep.count('Constraint Violation')}")
    return final, ok, rep, None


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
