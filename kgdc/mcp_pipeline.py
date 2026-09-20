"""Tool-gated mode: orchestrator (big model) plans and judges, workers (small model) build via kgdc-mcp tools.

  segment (big) -> build order (server) -> per level: create_task + worker tool loop, in parallel
  -> orchestrator reads status/notes and decides per task: accept | rerun(hint) | issue
  -> export.  No text merge pass: the shared graph is the result.
"""
from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import llm, prompts
from .mcp_client import Mcp
from .pipeline import _segment
from .progress import log
from .schema import Schema

AGENT_TOOLS = ["set", "set_many", "validate", "finalize"]   # no Turtle: typed values on pre-minted targets
MAX_TURNS = int(os.getenv("KGDC_AGENT_TURNS", "40"))   # one individual per call: a 10-measurement task needs ~15
MAX_RERUNS = int(os.getenv("KGDC_MAX_RERUNS", "1"))


@dataclass
class McpResult:
    ttl: str
    conforms: bool
    violations: list[dict]
    tasks: list[dict]
    issues: list[str]
    decisions: list[dict] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)
    llm_calls: int = 0
    usage: dict = field(default_factory=dict)


def salvage_args(raw: str) -> dict | None:
    """Parse tool-call arguments, tolerating a degenerate tail (nano emits '}]}]}]...' after
    the real content until the token cap) or truncation. Returns None if nothing usable."""
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        pass
    try:
        obj, _ = json.JSONDecoder().raw_decode(raw)   # valid object followed by junk
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    out: dict = {}
    for key in ("lines", "notes"):   # array fields: bracket-aware scan for the matching ']'
        i = raw.find(f'"{key}"')
        if i < 0:
            continue
        j = raw.find("[", i)
        depth, in_str, esc = 0, False, False
        for k in range(j, len(raw)):
            ch = raw[k]
            if in_str:
                esc = (ch == "\\") and not esc
                if ch == '"' and not esc:
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    try:
                        out[key] = json.loads(raw[j:k + 1])
                    except json.JSONDecodeError:
                        pass
                    break
    for key in ("subject", "predicate", "object", "query", "class", "turtle"):   # scalar string fields
        m = re.search(r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)"' % key, raw) or re.search(r'"%s"\s*:\s*"([^"\']*)' % key, raw)
        if m and m.group(1):
            out[key] = m.group(1)
    return out or None


def _known_block(mcp: Mcp) -> str:
    known = mcp.call("lookup")
    if not known:
        return ""
    return ("\nKNOWN ENTITIES already in the graph — link to these exact IRIs, never invent a new IRI for the same thing:\n"
            + "\n".join(f"  {k}" for k in known[:80]) + "\n")


def _worker_system(task: dict, vocab: str, ctx_notes: str, known: str = "") -> str:
    targets = "\n".join(f"  {t}" for t in task.get("target_lines", [])) or "  (none)"
    related = "\n".join(f"  {t}" for t in task.get("related_lines", [])) or "  (none)"
    return f"""You fill in a knowledge graph with tools. The orchestrator has already created the
individuals; you set their properties from the TEXT. Nothing else.

YOUR TARGETS (the only subjects you may write; each is "<iri> a <class> ; label"):
{targets}

RELATED INDIVIDUALS you may link to (use these exact IRIs as {{"type":"uri"}} values):
{related}

How to work — a checklist, not a judgement call:
1. Take the TEMPLATE for your class in the vocabulary below. For EACH target, go through the
   template's properties ONE BY ONE: if the TEXT or the SHARED CONTEXT gives the value, set it;
   if not, skip it. Optional properties count too — "MUST" only says what the validator
   demands, not what to extract. The task notes may tell you where a value comes from
   (e.g. which person is the performer, which date applies).
2. Send one `set_many` per target:
   {{"subject": "<target iri>", "predicate": "chr:hasX", "object": {{"type": ..., "value": ...}}}}.
   Object types: "uri" (a RELATED IRI copied exactly, or an external IRI like
   https://loinc.org/8302-2), "float", "integer", "string", "dateTime", "date", "boolean".
   The response lists rejections with reasons and the SHACL violations on your targets.
   Fix what the text lets you fix; do not resend a rejected triple unchanged.
3. Cover EVERY target (ten targets = ten set_many calls). Then call `finalize` with notes:
   every MUST slot you could not fill from the text ("chr:hasX — not stated"), and anything
   ambiguous.

{prompts.NO_FABRICATION}
{"Task hint from the orchestrator: " + task['hint'] if task.get('hint') else ""}
{ctx_notes}
VOCABULARY FOR THIS TASK (properties per class, with expected value types):
{vocab}

SHARED CONTEXT (facts stated once for the whole document; they count as text):
\"\"\"{task['context']}\"\"\"

TEXT:
\"\"\"{task['text']}\"\"\"
"""


def run_worker(mcp: Mcp, task: dict, task_ctx: str) -> dict:
    """Tool loop for one task. Ends on `finalize` or MAX_TURNS (then finalizes with a note)."""
    name = task["class"].split(":")[-1]
    vocab = mcp.call("vocabulary", **{"class": task["class"]})
    ctx_notes = prompts._task(task_ctx) if task_ctx else ""
    messages = [{"role": "system", "content": _worker_system(task, vocab, ctx_notes)},
                {"role": "user", "content": f"Start. Task id: {task['id']}."}]
    tools = mcp.openai_tools(AGENT_TOOLS)
    trace = {"task": task["id"], "class": task["class"], "turns": 0, "calls": [], "finalized": False}
    log(f"  {name} [{task['id']}]: worker starts")
    seen_snippets: set[str] = set()
    text_only = 0
    empty_lookups = 0
    added = 0
    idle_since_add = 0
    malformed = 0   # tool calls that added nothing after the last successful add
    for turn in range(MAX_TURNS):
        try:
            msg = llm.chat_messages(messages, tools=tools)
        except llm.MalformedToolCall as e:
            malformed += 1
            if malformed > 3:
                mcp.call("finalize", task_id=task["id"], notes=[f"worker kept producing malformed tool calls: {str(e)[:120]}"])
                trace["finalized"] = True
                log(f"  {name}: malformed tool calls, finalized with note")
                return trace
            messages.append({"role": "user", "content": "Your last tool call was not valid JSON. Call exactly ONE tool with small arguments: `set` for one property at a time."})
            continue
        except Exception as e:  # noqa: BLE001 — a dead endpoint must not take the whole run down
            note = f"worker LLM failure: {str(e)[:200]}"
            mcp.call("finalize", task_id=task["id"], notes=[note])
            trace["finalized"] = True
            trace["error"] = note
            log(f"  {name}: LLM failure, finalized with note ({str(e)[:80]})")
            return trace
        trace["turns"] += 1
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [{"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}} for c in (msg.tool_calls or [])]}
                        if msg.tool_calls else {"role": "assistant", "content": msg.content or ""})
        if not msg.tool_calls:   # plain text answer: nudge twice, then stop
            text_only += 1
            if text_only > 2:
                break
            messages.append({"role": "user", "content": "Use the tools. Call set_many for each target, then finalize."})
            continue
        for c in msg.tool_calls:
            args = salvage_args(c.function.arguments or "{}")
            if args is None:
                messages.append({"role": "tool", "tool_call_id": c.id,
                                 "content": "arguments were not valid JSON. Send ONE individual per call — a few short lines — and continue."})
                trace["calls"].append({"tool": c.function.name, "args": {"_invalid": (c.function.arguments or "")[:120]}, "result": "invalid JSON arguments"})
                log(f"  {name}: unusable tool arguments, asked for smaller calls")
                continue
            if len(c.function.arguments or "") > 3000 and "task_id" not in args:
                log(f"  {name}: salvaged arguments from a degenerate tool call")
            if c.function.name in ("set", "set_many", "add_triple", "add_triples", "finalize", "validate"):
                args["task_id"] = task["id"]
            if c.function.name in ("set", "set_many", "add_triple", "add_triples"):
                key = json.dumps({k: v for k, v in args.items() if k != "task_id"}, sort_keys=True)
                if key in seen_snippets:   # identical snippet again: the model is stuck, stop here
                    messages.append({"role": "tool", "tool_call_id": c.id, "content": "identical snippet already rejected; change it or finalize with a note"})
                    mcp.call("finalize", task_id=task["id"], notes=["worker repeated a rejected snippet and was stopped"])
                    trace["finalized"] = True
                    log(f"  {name}: repeated rejected snippet, stopped")
                    return trace
                seen_snippets.add(key)
            res = mcp.call(c.function.name, **args)
            short = res if isinstance(res, str) else json.dumps(res)
            trace["calls"].append({"tool": c.function.name, "args": {k: (v[:200] if isinstance(v, str) else v) for k, v in args.items()}, "result": short[:600]})
            if c.function.name in ("set", "set_many", "add_triple", "add_triples") and isinstance(res, dict):
                log(f"  {name}: +{res.get('accepted', 0)} triples, {len(res.get('rejected', []))} rejected, {len(res.get('violations_for_task', []))} violations")
            messages.append({"role": "tool", "tool_call_id": c.id, "content": short[:6000]})
            if c.function.name in ("set", "set_many", "add_triple", "add_triples") and isinstance(res, dict) and res.get("accepted", 0) > 0:
                added += res["accepted"]
                idle_since_add = 0
            else:
                idle_since_add += 1
            if c.function.name == "lookup":
                empty_lookups = empty_lookups + 1 if res == [] else 0
                if empty_lookups >= 2:   # small models loop on lookup; push them to build
                    messages.append({"role": "user", "content": "Nothing exists yet for that. Stop looking up. Call add_triples now with the first individual from the TEXT, or finalize if everything is added."})
                    empty_lookups = 0
            if added and idle_since_add >= 4:   # built something, then wandered: close the task
                mcp.call("finalize", task_id=task["id"], notes=["worker stopped after idle calls following its last addition"])
                trace["finalized"] = True
                log(f"  {name}: idle after adding, auto-finalized")
                return trace
            if c.function.name == "finalize":
                trace["finalized"] = True
                log(f"  {name}: finalized ({len(args.get('notes', []))} notes)")
                return trace
    mcp.call("finalize", task_id=task["id"], notes=["worker hit the turn limit before finalizing"])
    log(f"  {name}: turn limit reached, auto-finalized")
    return trace


def _plan_prompt(schema: Schema, text: str, present: list[str], task: str) -> str:
    classes = "\n".join(f"- {c}: {schema.classes[c]}" for c in present)
    return f"""List EVERY individual the document states, as instances of these classes only:
{classes}
{prompts._task(task)}
Rules: one entry per real-world thing the text describes, for EVERY class above that the text
instantiates — including small ones like units of measure (one Unit per distinct unit symbol),
statuses (one ProcessStatus per distinct status value), care units, persons. Ten measurements
= ten Measurement entries and ten MeasurementProcess entries. Each real thing appears once
(the same person mentioned ten times is one entry). The label is a short name taken from the
text. Do not invent things the text does not state.

Return ONLY JSON: [{{"class": "<qname>", "label": "<from text>"}}]

DOCUMENT:
\"\"\"{text.strip()}\"\"\"
"""


def run_mcp(schema: Schema, text: str, workers: int = 4, task: str = "") -> McpResult:
    mcp = Mcp(schema.ontology_path, schema.shapes_path)
    try:
        context, segs = _segment(schema, text, task)
        present = sorted({c for s in segs for c in s.get("concepts", []) if c in schema.classes})
        # dependencies of present classes that no segment named (units, statuses, persons) are
        # still needed as link targets: let the planner see them
        for c in list(present):
            for d in schema.subset([c]).classes:
                if d not in present:
                    present.append(d)
        present.sort()
        # ---- plan + mint (orchestrator) ----
        log("planning individuals (orchestrator) ...")
        plan = json.loads(llm.strip_fences(llm.chat(_plan_prompt(schema, text, present, task), model=llm.BIG)))
        plan = [e for e in plan if e.get("class") in schema.classes and e.get("label")]
        # attach each entity to the segment that mentions its label (verbatim, case-insensitive);
        # things only in the header (or unmatched) get the whole document
        for e in plan:
            key = e["label"].lower()
            e["segment"] = next((s["id"] for s in segs if key and key in s["text"].lower()), None)
        minted = mcp.call("mint", items=[{"class": e["class"], "label": e["label"]} for e in plan])
        for e, m in zip(plan, minted):
            e["uri"] = m["uri"]
        by_class: dict[str, list[dict]] = {}
        for e in plan:
            by_class.setdefault(e["class"], []).append(e)
        log(f"minted {len(minted)} individuals: " + ", ".join(f"{c.split(':')[-1]}×{len(v)}" for c, v in sorted(by_class.items())))
        levels = mcp.call("status", **{"class": ",".join(by_class)})["build_order"]
        log("build order: " + " -> ".join("[" + ", ".join(c.split(":")[-1] for c in l) + "]" for l in levels))
        line = lambda e: f'{e["uri"]} a {e["class"]} ; "{e["label"]}"'
        seg_by_id = {s["id"]: s["text"] for s in segs}
        traces, decisions = [], []
        for li, level in enumerate(levels):
            tasks = []
            for cls in level:
                ents = by_class.get(cls, [])
                if not ents:
                    continue
                deps = schema.subset([cls]).classes
                related = [e for c2, es in by_class.items() if c2 != cls and c2 in deps for e in es]
                spans = {seg_by_id[e["segment"]] for e in ents if e.get("segment") in seg_by_id}
                t = mcp.call("create_task", targets=[e["uri"] for e in ents], related=[e["uri"] for e in related],
                             text="\n\n".join(sorted(spans)) or text, context=context)
                t["target_lines"], t["related_lines"] = [line(e) for e in ents], [line(e) for e in related]
                tasks.append(t)
            log(f"level {li}: {len(tasks)} task(s), {sum(len(t['targets']) for t in tasks)} targets")
            with ThreadPoolExecutor(max_workers=workers) as ex:
                traces += list(ex.map(lambda t: run_worker(mcp, t, task), tasks))
            # ---- orchestrator judgement: deterministic coverage + notes/violations ----
            status = mcp.call("status")
            viol = mcp.call("validate")
            unfilled = status.get("unfilled_targets", {})
            view = []
            for t in tasks:
                st_t = next(x for x in status["tasks"] if x["id"] == t["id"])
                tv = [v for v in viol if any(v["focus"].endswith(s.rsplit("/", 1)[-1]) for s in st_t["subjects"])]
                view.append({"task_id": t["id"], "class": t["class"], "targets": len(t["targets"]), "unfilled_targets": unfilled.get(t["id"], []),
                             "accepted": st_t["accepted"], "rejected": st_t["rejected"], "notes": st_t["notes"], "violations": tv[:15],
                             "text": (t["context"] + "\n" + t["text"])[:4000]})
            log(f"level {li}: orchestrator reviewing {len(view)} task(s), {sum(len(v['violations']) for v in view)} violation(s), {sum(len(v['unfilled_targets']) for v in view)} unfilled target(s)")
            try:
                decs = json.loads(llm.strip_fences(llm.chat(_decision_prompt(view), model=llm.BIG)))
            except (json.JSONDecodeError, TypeError):
                decs = []
            for d in decs:
                d["level"] = li
                decisions.append(d)
                t = next((x for x in tasks if x["id"] == d.get("task_id")), None)
                if not t:
                    continue
                if d.get("action") == "issue":
                    mcp.call("raise_issue", text=f"[{t['class']}] {d.get('text') or d.get('hint') or ''}")
                    log(f"  {t['class'].split(':')[-1]}: issue raised")
                elif d.get("action") == "rerun" and t.get("_reruns", 0) < MAX_RERUNS:
                    t["_reruns"] = t.get("_reruns", 0) + 1
                    log(f"  {t['class'].split(':')[-1]}: rerun — {str(d.get('hint', ''))[:80]}")
                    t2 = mcp.call("reopen_task", task_id=t["id"], hint=d.get("hint", ""), discard_triples=bool(d.get("discard")))
                    t2["target_lines"], t2["related_lines"] = t["target_lines"], t["related_lines"]
                    traces.append(run_worker(mcp, t2, task))
        final_status = mcp.call("status")
        viol = mcp.call("validate")
        ttl = mcp.call("export")
        log(f"final: {'conforms' if not viol else f'{len(viol)} violation(s)'}, {final_status['triples']} triples, {sum(len(v) for v in final_status.get('unfilled_targets', {}).values())} unfilled target(s), {len(final_status['issues'])} issue(s)")
        return McpResult(ttl=ttl, conforms=not viol, violations=viol, tasks=final_status["tasks"], issues=final_status["issues"],
                         decisions=decisions, trace=traces, llm_calls=len(llm.USAGE), usage=llm.usage_summary())
    finally:
        mcp.close()


def _decision_prompt(view: list[dict]) -> str:
    return f"""You supervise knowledge-graph extraction agents. For each task below decide:
- "accept": the notes/violations are honest gaps the text cannot fill, or acceptable as is;
- "rerun": the agent clearly missed or misread something the text states — give a concrete hint;
- "issue": something a human must decide (contradiction in the text, vocabulary cannot express it).
Never ask for facts to be invented to satisfy a violation. Prefer "accept" when in doubt.
Each task carries the text it was given ("text"): check claims in the notes against it before
deciding — a note that something is "not stated" is only wrong if the text does state it.
COVERAGE is measured for you: "unfilled_targets" lists the task's individuals that got no
property at all. A non-empty list is a "rerun" with the hint naming those targets. Do not
rerun for coverage on your own count.

TASKS:
{json.dumps(view, indent=1)}

A rerun KEEPS the task's triples and asks the agent to add/correct; set "discard": true only
if what was built is wrong as a whole.

Return ONLY JSON: [{{"task_id": "...", "action": "accept|rerun|issue", "hint": "...", "text": "...", "discard": false}}]"""
