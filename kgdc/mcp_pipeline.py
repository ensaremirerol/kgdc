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

AGENT_TOOLS = ["lookup", "add_triple", "add_triples", "validate", "finalize"]   # vocabulary slice is in the system prompt
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
    return f"""You build part of a knowledge graph with tools. Your task {task['id']}: extract every
{task['class']} the TEXT states, and only that (plus the individuals it links to directly).

How to work:
1. Call `lookup` ONCE per thing that another task may already have built (people, units,
   statuses, places): reuse the IRI you find. An empty result means it does not exist yet —
   mint it yourself in the next call; never look up the same thing twice.
2. Add facts with `add_triple(subject, predicate, object)` — one triple per call, terms as
   short strings: subject "ex:person_Ann", predicate "a" or "chr:hasCode", object
   "chr:Person", "ex:unit_cm", "<https://loinc.org/8302-2>" or a literal like
   "\"104.0\"^^xsd:float". Prefixes are predefined. (`add_triples` with `lines` exists for
   several statements at once, but keep calls small.) The response lists rejections with
   reasons and the SHACL violations on your subjects. Fix what the text lets you fix; do
   not resend a rejected triple unchanged.
   If a triple is rejected as "unknown individual", CREATE that individual (its `a` type and
   rdfs:label) in the same call and resend the triple — do not drop the link.
   Keep going until EVERY instance the text states is added (ten measurements in the text
   means ten add calls); only then finalize.
3. When done, call `finalize` with notes: every MUST slot you could not fill from the text
   ("chr:hasX — not stated"), anything ambiguous, anything rejected you believe is right.

{prompts.NO_FABRICATION}
{"Task hint from the orchestrator: " + task['hint'] if task.get('hint') else ""}
{ctx_notes}{known}
VOCABULARY FOR THIS TASK:
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
    messages = [{"role": "system", "content": _worker_system(task, vocab, ctx_notes, _known_block(mcp))},
                {"role": "user", "content": f"Start. Task id: {task['id']}."}]
    have_graph = mcp.call("status")["triples"] > 0
    tools = mcp.openai_tools([t for t in AGENT_TOOLS if have_graph or t != "lookup"])   # nothing to look up in an empty graph
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
            messages.append({"role": "user", "content": "Your last tool call was not valid JSON. Call exactly ONE tool with small arguments: prefer add_triple(subject, predicate, object) for one triple at a time."})
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
            messages.append({"role": "user", "content": "Use the tools. Call add_triples for each individual, then finalize."})
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
            if c.function.name in ("add_triple", "add_triples", "finalize", "validate"):
                args["task_id"] = task["id"]
            if c.function.name in ("add_triple", "add_triples"):
                key = " ".join((str(args.get("turtle", "")) + " " + " ".join(args.get("lines") or [])
                                + " " + " ".join(str(args.get(k, "")) for k in ("subject", "predicate", "object"))).split())
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
            if c.function.name in ("add_triple", "add_triples") and isinstance(res, dict):
                log(f"  {name}: +{res.get('accepted', 0)} triples, {len(res.get('rejected', []))} rejected, {len(res.get('violations_for_task', []))} violations")
            messages.append({"role": "tool", "tool_call_id": c.id, "content": short[:6000]})
            if c.function.name in ("add_triple", "add_triples") and isinstance(res, dict) and res.get("accepted", 0) > 0:
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


def run_mcp(schema: Schema, text: str, workers: int = 4, task: str = "") -> McpResult:
    mcp = Mcp(schema.ontology_path, schema.shapes_path)
    try:
        context, segs = _segment(schema, text, task)
        present = sorted({c for s in segs for c in s.get("concepts", []) if c in schema.classes})
        levels = mcp.call("status", **{"class": ",".join(present)})["build_order"]
        log("build order: " + " -> ".join("[" + ", ".join(c.split(":")[-1] for c in l) + "]" for l in levels))
        traces, decisions = [], []
        for li, level in enumerate(levels):
            tasks = []
            for cls in level:
                spans = [s["text"] for s in segs if cls in s.get("concepts", [])]
                tasks.append(mcp.call("create_task", **{"class": cls, "text": "\n\n".join(spans) or text, "context": context}))
            log(f"level {li}: {len(tasks)} task(s)")
            with ThreadPoolExecutor(max_workers=workers) as ex:
                traces += list(ex.map(lambda t: run_worker(mcp, t, task), tasks))
            # orchestrator judgement for this level
            status = mcp.call("status")
            viol = mcp.call("validate")
            for t in status["tasks"]:
                t["_viol"] = [v for v in viol if any(v["focus"].endswith(s.rsplit("/", 1)[-1]) for s in t["subjects"])]
            built = {}
            for line in mcp.call("lookup"):
                cls = line.split(" a ", 1)[1].split(" ;")[0] if " a " in line else "?"
                built[cls] = built.get(cls, 0) + 1
            view = [{"task_id": t["id"], "class": t["class"], "accepted": t["accepted"], "rejected": t["rejected"], "notes": t["notes"], "violations": t["_viol"][:15],
                     "individuals_of_task_class_in_graph": built.get(t["class"], 0),
                     "text": (t["context"] + "\n" + t["text"])[:4000]}
                    for t in status["tasks"] if t["id"] in {x["id"] for x in tasks}]
            log(f"level {li}: orchestrator reviewing {len(view)} task(s), {sum(len(v['violations']) for v in view)} violation(s)")
            prompt = _decision_prompt(view)
            try:
                decs = json.loads(llm.strip_fences(llm.chat(prompt, model=llm.BIG)))
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
                    # keep what the task built; the rerun adds/corrects (discarding lost correct work
                    # whenever the orchestrator over-counted). An explicit "discard": true in the decision overrides.
                    t2 = mcp.call("reopen_task", task_id=t["id"], hint=d.get("hint", ""), discard_triples=bool(d.get("discard")))
                    traces.append(run_worker(mcp, t2, task))
        final_status = mcp.call("status")
        viol = mcp.call("validate")
        ttl = mcp.call("export")
        log(f"final: {'conforms' if not viol else f'{len(viol)} violation(s)'}, {final_status['triples']} triples, {len(final_status['issues'])} issue(s)")
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
Check COVERAGE: count the instances of the task's class the text describes and compare with
"individuals_of_task_class_in_graph". If the task built clearly fewer (e.g. 1 of 10
measurements), that is a "rerun" with the hint "the text states N <class>; you built K — add
the missing ones: <list them>".

TASKS:
{json.dumps(view, indent=1)}

A rerun KEEPS the task's triples and asks the agent to add/correct; set "discard": true only
if what was built is wrong as a whole.

Return ONLY JSON: [{{"task_id": "...", "action": "accept|rerun|issue", "hint": "...", "text": "...", "discard": false}}]"""
