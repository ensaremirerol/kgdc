"""segment -> N sub-agents (extract, validate, fix<=k) -> merge -> big-model pass -> validate."""
from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from rdflib import Graph, URIRef

from . import llm, prompts
from .progress import log
from .schema import Schema
from .validate import _BAD_IRI, unresolved, validate

MAX_FIX = int(os.getenv("KGDC_MAX_FIX", "2"))
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


def _agent(schema: Schema, context: str, seg: dict, task: str, known: str = "", known_ttl: str = "") -> dict:
    """One sub-agent: one-shot extraction, then up to MAX_FIX validator-guided fixes.

    ``known`` (prompt block) / ``known_ttl`` (graph) carry entities built by
    earlier levels in ordered mode; validation runs on agent output + known
    graph so links to known IRIs satisfy range/class constraints."""
    trace = {"id": seg["id"], "concepts": seg.get("concepts", []), "text": seg["text"], "cycles": [], "llm_calls": 0}
    schema = schema.subset(trace["concepts"])   # only the relevant slice of the vocabulary goes in the prompt
    name = seg["id"].split(":")[-1]
    log(f"  {name}: extracting ({len(seg['text'])} chars, {len(schema.classes)} classes in view)")
    ttl = llm.strip_fences(llm.chat(prompts.extract(schema, context, seg["text"], trace["concepts"], task, known)))
    trace["llm_calls"] += 1
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
        ttl = llm.strip_fences(llm.chat(prompts.fix(schema, ttl, report, context, seg["text"], task, known)))
        trace["llm_calls"] += 1
    trace["ttl"] = ttl
    trace["unresolved"] = unresolved(ttl)
    if trace["unresolved"]:
        log(f"  {name}: {len(trace['unresolved'])} unresolved (honest gaps)")
    return trace


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


def _cap(s: str, n: int = MERGE_MAX_CHARS) -> str:
    return s if len(s) <= n else s[:n] + f"\n... [{len(s) - n} more characters omitted]"


def _final(schema: Schema, text: str, merged: str, report: str, unres: list[str], unparsed: list[str],
           task: str, concepts: list[str]) -> tuple[str, bool, str, str | None]:
    """Big-model merge pass with capped inputs; on failure keep the pre-merge graph."""
    prompt = prompts.merge(schema.subset(concepts), text, merged,
                           _cap(report), unres[:50], _cap("\n\n".join(unparsed)), task)
    try:
        final = llm.strip_fences(llm.chat(prompt, model=BIG_MODEL))
        err = None
    except Exception as e:  # noqa: BLE001 — e.g. context window exceeded: the union is still a result
        final, err = merged, f"merge pass failed: {str(e)[:300]}"
        log(f"merge FAILED ({str(e)[:80]}...) - returning the un-merged union")
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
            # still gets built — from the whole document, which its agent can afford here
            jobs.append({"id": cls, "concepts": [cls], "text": "\n\n".join(spans) or text})
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
