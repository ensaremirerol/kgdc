"""segment -> N sub-agents (extract, validate, fix<=k) -> merge -> big-model pass -> validate."""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from rdflib import Graph

from . import llm, prompts
from .schema import Schema
from .validate import unresolved, validate

MAX_FIX = int(os.getenv("KGDC_MAX_FIX", "2"))
BIG_MODEL = os.getenv("LLM_MODEL_BIG") or None   # None -> LLM_MODEL


@dataclass
class Result:
    ttl: str
    conforms: bool
    violations: str
    unresolved: list[str]
    segments: list[dict] = field(default_factory=list)   # per-agent trace
    llm_calls: int = 0


def _segment(schema: Schema, text: str) -> tuple[str, list[dict]]:
    raw = llm.strip_fences(llm.chat(prompts.segment(schema, text)))
    data = json.loads(raw)
    segs = [s for s in data.get("segments", []) if s.get("text")]
    for s in segs:  # verbatim check — paraphrased spans are the first place facts get invented
        s["verbatim"] = s["text"].strip() in text
    return data.get("shared_context", ""), segs


def _agent(schema: Schema, context: str, seg: dict) -> dict:
    """One sub-agent: one-shot extraction, then up to MAX_FIX validator-guided fixes."""
    trace = {"id": seg["id"], "concepts": seg.get("concepts", []), "cycles": [], "llm_calls": 0}
    schema = schema.subset(trace["concepts"])   # only the relevant slice of the vocabulary goes in the prompt
    ttl = llm.strip_fences(llm.chat(prompts.extract(schema, context, seg["text"], trace["concepts"])))
    trace["llm_calls"] += 1
    prev = None
    for cycle in range(MAX_FIX + 1):
        ok, report, _ = validate(ttl, schema)
        trace["cycles"].append({"cycle": cycle, "conforms": ok, "violations": report[:2000]})
        if ok or cycle == MAX_FIX or report == prev:   # plateau: same report twice -> agent is (honestly) stuck
            break
        prev = report
        ttl = llm.strip_fences(llm.chat(prompts.fix(schema, ttl, report, context, seg["text"])))
        trace["llm_calls"] += 1
    trace["ttl"] = ttl
    trace["unresolved"] = unresolved(ttl)
    return trace


def _union(schema: Schema, ttls: list[str]) -> str:
    g = Graph()
    for p, ns in schema.prefixes.items():
        g.bind(p, ns)
    for t in ttls:
        try:
            g.parse(data=t, format="turtle")
        except Exception:  # noqa: BLE001 — unparseable sub-graph is dropped; its trace keeps the violation
            pass
    return g.serialize(format="turtle")


def run(schema: Schema, text: str, workers: int = 4) -> Result:
    context, segs = _segment(schema, text)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        traces = list(ex.map(lambda s: _agent(schema, context, s), segs))
    merged = _union(schema, [t["ttl"] for t in traces])
    _, report, _ = validate(merged, schema)
    unres = [u for t in traces for u in t["unresolved"]]
    used = sorted({c for t in traces for c in t["concepts"]})
    final = llm.strip_fences(llm.chat(prompts.merge(schema.subset(used), text, merged, report, unres), model=BIG_MODEL))
    ok, report, _ = validate(final, schema)
    return Result(
        ttl=final, conforms=ok, violations=report, unresolved=unresolved(final),
        segments=[{**t, "shared_context": context} for t in traces],
        llm_calls=1 + sum(t["llm_calls"] for t in traces) + 1,
    )
