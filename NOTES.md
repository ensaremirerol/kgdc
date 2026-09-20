# Performance notes (quality + speed + cost)

Findings from the vignette_065 runs, in the order they bit us. Each item:
what happened → what fixed it or would.

## Quality

1. **Prompt knowledge beats architecture.** Single-shot with hand-written CHR
   rules: F1 0.87. Multi-agent with only the ontology/shapes: 0.36. Same
   multi-agent + `context.md` (the same rules as a task-context file): 0.87 →
   0.95 in ordered mode. Budget time for the context file before anything else.
2. **Tell the segmenter that the header is a segment.** Shared context that
   *describes* an instance (the visit) must also be emitted as a segment or no
   agent builds it (lost the visit + 11 links until fixed).
3. **Hide unlinkable classes.** Classes never used as domain/range (Role
   leftovers, abstract root) get picked by the segmenter and then everything
   links to a Role instead of a Person. `Schema.unlinkable` removes them.
4. **Templates must be valid Turtle.** `ex:<Class-iri>` copied literally by a
   4B model = parse error with no subject. `ex:Class_1` works.
5. **Ship the `rdf:` prefix.** Left to declare it, an 8B model wrote
   `…/02/rdf-syntax-ns#` (no `22-`) and every `rdf:type` became an unknown
   predicate.
6. **Snap segments to sentence boundaries.** The segmenter drops leading
   clauses ("On <date>, …") that carry exactly the values constraints ask for.
7. **Report UNRESOLVED only for MUST slots.** Otherwise every optional slot
   becomes a note and the orchestrator drowns.
8. **Small models (≤8B) over-generate and ignore scope in one-shot mode**
   (58 entities from 20; `ProcessStatus` ×6088; invented classes). Fix rounds
   make it worse (4 → 28 violations). Use MAX_FIX=0 or the tool-gated path.
9. **Tool gating is the right place for small models**, but the loop needs
   guard rails: no `lookup` on an empty graph, nudge after two empty lookups,
   stop on an identical rejected snippet, auto-finalize when idle after adding.
   Without them an 8B worker spends all 12 turns on `lookup`.
10. **Strict IRI parsing before anything downstream.** rdflib accepts `^`, bare
    `%`, spaces; OWLAPI silently drops the graph around them and HermiT says
    "consistent"; shacl-validator refuses the file. Reject at the gate.
11. **Identity-key F1, not full-content hashing**, for scoring: hashing every
    property re-keys a node on one extra label (F1 0.08); hashing the class's
    identity slots (code+value+date, label, …) is stable and deterministic.

## Speed

12. **Provider, not model, sets latency on OpenRouter.** gemma-3-4b: single
    provider at 13–15 tok/s (a 3k-token draft = 4 min). llama-3.1-8b on Groq:
    ~800 tok/s. Always pass `provider.sort=throughput` (`LLM_PROVIDER_SORT`)
    and benchmark a 500-token completion before choosing a worker model.
13. **Reasoning models cost 20–50× the visible tokens** (nemotron: 110 tokens
    to say "OK"). Disable reasoning for workers or avoid those models.
14. **Parallel tool loops hit per-minute rate limits.** Five 8B workers at
    once on Groq → 429 back-off (10 s × attempt) and a level that should take
    30 s takes minutes. Cap workers per provider (2–3) or stagger starts;
    prefer a paid tier for the workers.
15. **The orchestrator is the wall-clock floor**: segmentation 20 s + one
    review call per level + merge on a 27B LiteLLM model. Keep levels few
    (the ordered build has 3–4) and cap what goes into the merge prompt
    (`KGDC_MERGE_MAX_CHARS`) or it exceeds the context window.
16. **SHACL cost is negligible** (ms per validate, JVM HermiT ~0.5 s per
    call); spend validation freely, spend LLM calls carefully.

17. **Set a client timeout.** The OpenAI SDK default is 600 s; one hung
    tool-call response froze a worker for 10 minutes with no log line.
    `LLM_TIMEOUT=90` + retry, and `LLM_MAX_TOKENS` for workers.
18. **Contain worker failures.** OpenRouter returns HTTP 200 with empty
    `choices` and an `error` field when the upstream (Groq) fails; five in a
    row raised out of the thread pool and killed the whole run. A worker
    error must finalize its own task with a note, never abort the run.

19. **Tool-calling is a per-model, per-provider capability, not a checkbox.**
    On the same MCP loop: llama-3.1-8b (Groq) cannot emit a parseable call in a
    multi-turn loop (batches several calls, Groq rejects); qwen3-30b-a3b and
    mistral-nemo answer in prose unless `tool_choice=required`; gemma-3-12b never
    calls; gpt-4.1-nano emits perfect content then degenerates into `'}]}]}]…`
    until the token cap. Always dry-run one worker task and print the *raw*
    `function.arguments` before trusting a model.
20. **Be lenient at the server, strict in the gate.** Accept a block split
    across `lines` items with `;` continuations, quote bare literals typed by
    the property's range, salvage the longest valid JSON prefix / the `lines`
    array from degenerate arguments. Rejecting on form loses correct content;
    the gate still rejects on meaning.
21. **Never silently coerce bad tool arguments to `{}`.** One
    `json.JSONDecodeError → args = {}` turned a full run into "nothing to add"
    with confident notes. Feed the failure back to the model or salvage.
22. **Turn budget scales with individuals.** One individual per call means a
    10-measurement task needs ~15 turns; a 12-turn cap silently truncates work.
    Reasoning models need the budget in tokens instead (qwen3-8b: 8k
    completion tokens for one task, 165 s vs 14 s for nano).

23. **Reruns must add, never restart.** `reopen_task(discard_triples=True)`
    plus one orchestrator miscount ("2 visits; you built 0") deleted a correct
    visit and the rerun produced nothing (F1 0.46 → 0.16 between two runs
    that differed only in this). Keep the task's triples; discard only on an
    explicit decision.
24. **Dangling object IRIs are the cross-task failure mode.** Workers guess
    `ex:Person_Kohler` instead of the `ex:Person_1` another task built. Reject
    objects that do not exist (or are not created in the same call) and push
    the known-entity list into the worker prompt; `lookup` alone is not used
    reliably by small models.
25. **Give the orchestrator evidence, not just notes.** Without the segment
    text it reran correct work ("Dr. Kohler not in text"); with the text and
    per-class individual counts it issued precise, grounded hints — but it
    also over-counts, hence 23.

## Cost (vignette_065, 12–17 calls)

- 27B workers via LiteLLM: n/a (internal); ~3.5 min.
- llama-3.1-8b workers via Groq: $0.011–0.031 per document; 2.7–4 min,
  dominated by rate-limit waits, not generation.
- Reasoning-model workers: same call count, ~10× the completion tokens.

## Next experiments worth running

- Stagger/limit worker concurrency per provider; measure wall-clock vs 429s.
- `lookup` returning a hint string instead of `[]` (small models treat an
  empty list as "keep searching").
- Orchestrator decisions on a 20-document set: how often `rerun` helps vs
  costs; whether `issue` fires on real ambiguities.
- Workers at 12B–14B (gemma-3-12b at 64 tok/s) as the sweet spot between 8B
  reliability and 27B cost.
