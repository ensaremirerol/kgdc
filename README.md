# kgdc — divide-and-conquer KG extraction

Text → segments (by ontology concept) → one sub-agent per segment builds a
Turtle graph for its slice, validated against SHACL → union → one big-model
pass over the whole document merges duplicates and cross-segment links →
final validation.

Works for any ontology (RDFS/OWL) + SHACL shapes: nothing in the prompts is
vocabulary-specific. The **ontology** drives segmentation (what counts as a
unit of facts); the **shapes** drive filling (slots, cardinalities, ranges).
Each agent sees only the slice of the vocabulary its segment needs
(`Schema.subset`: the segment's classes, their properties incl. inherited,
range classes two hops out).

**Agents never fabricate.** If a constraint requires something the text does
not state, the agent leaves it unfilled and writes
`# UNRESOLVED: <constraint> — not stated in the text`. Those lines are collected
and reported; a graph with honest gaps beats a conformant graph with invented
values. Fix rounds stop as soon as the violation report repeats (plateau).

```bash
uv venv && uv pip install -e ".[dev]"
cp .env.example .env            # LLM_API_KEY, LLM_BASE_URL, LLM_MODEL [, LLM_BIG_MODEL]
python -m kgdc examples/chr/ontology.ttl examples/chr/shapes.ttl examples/chr/vignette_065.txt \
    --context examples/chr/context.md -o out.ttl
pytest                          # offline, stub LLM
```

**Task context.** What the schema cannot say — where a code goes, how a unit
is encoded, who counts as the performer — goes in a plain text/markdown file
passed with `--context` (`run(..., task=...)`). It is injected into every
prompt as TASK NOTES with the standing rule that notes explain how to *encode*
stated facts and never add facts. See `examples/chr/context.md`.

`out.ttl.trace.json` holds every segment, each agent's cycles/violations and
unresolved notes.

Validation per agent and at the end: Turtle parse → closed-world vocabulary
check (every class/property must be declared in the ontology or shapes; no
malformed IRIs) → SHACL (pySHACL, ontology as `ont_graph`, warnings ignored).

Not built yet: step-by-step graph construction when one-shot extraction of a
segment keeps failing (add a per-slot loop in `pipeline._agent` when a corpus
shows it's needed).

## kgdc-mcp — tool-gated extraction server (Rust, shacl-rust)

`kgdc-mcp/` is a stateful MCP server (stdio) that replaces "emit a whole
Turtle document and hope" with per-triple admission. It holds one shared
graph per run; agents add Turtle through `add_triples` and every triple must
pass a gate before it lands:

- class and property declared in the ontology/shapes (no invented terms)
- object kind matches the property's range (IRI vs literal)
- `rdf:type` only within the task's scope: its class or classes it links to
- subjects minted in the `ex:` namespace, no malformed IRIs, no blank nodes
- per-task subject cap (`--max-subjects`, default 40) against runaway output

Rejected triples come back with the reason; accepted ones are validated with
SHACL (shacl-rust, same engine as `shacl-validator`) and the task's current
violations are returned in the same call.

```
cd kgdc-mcp && cargo build --release
./target/release/kgdc-mcp --ontology ../examples/chr/ontology.ttl --shapes ../examples/chr/shapes.ttl
```

| tool | caller | purpose |
|---|---|---|
| `vocabulary(class?)` | any | prefixes, classes, properties, Turtle templates — sliced to one class |
| `create_task(class, text, context)` | orchestrator | register a scoped job |
| `add_triples(task_id, turtle)` | agent | gated insert + SHACL delta (one individual per call) |
| `lookup(query?, class?)` | agent | find existing individuals to link to |
| `validate(task_id?)` | any | SHACL violations, optionally for one task |
| `finalize(task_id, notes)` | agent | done + UNRESOLVED notes; returns remaining violations |
| `reopen_task(task_id, hint, discard_triples?)` / `discard_task` | orchestrator | retry with a hint, or drop |
| `raise_issue(text)` | orchestrator | escalate to the user |
| `status(class?)` | orchestrator | conformance, tasks, notes, issues, bottom-up build order |
| `export()` | any | the graph as Turtle |

`--mcp` mode (`kgdc/mcp_pipeline.py`) drives it: the orchestrator (big model)
segments, **plans every individual** (class + label) and mints them
(`mint` → `ex:<class>/<label-slug>`); each task is a set of minted IRIs to
fill (`targets`) plus IRIs it may link to (`related`); workers (small model)
call `set`/`set_many` with typed values — `{"type": "uri"|"float"|"integer"|
"string"|"dateTime"|"date"|"boolean", "value": ...}` — never Turtle. Coverage
is measured by the server (`status.unfilled_targets`); the orchestrator
reviews notes, violations and coverage per level and reruns additively,
raises issues, or accepts. `tests/test_mcp.py` drives the server over stdio.
