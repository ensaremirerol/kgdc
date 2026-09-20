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
cp .env.example .env            # LLM_API_KEY, LLM_BASE_URL, LLM_MODEL [, LLM_MODEL_BIG]
python -m kgdc examples/chr/ontology.ttl examples/chr/shapes.ttl examples/chr/vignette_065.txt -o out.ttl
pytest                          # offline, stub LLM
```

`out.ttl.trace.json` holds every segment, each agent's cycles/violations and
unresolved notes.

Validation per agent and at the end: Turtle parse → closed-world vocabulary
check (every class/property must be declared in the ontology or shapes; no
malformed IRIs) → SHACL (pySHACL, ontology as `ont_graph`, warnings ignored).

Not built yet: step-by-step graph construction when one-shot extraction of a
segment keeps failing (add a per-slot loop in `pipeline._agent` when a corpus
shows it's needed).
