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

## Plan → mint → fill (the structure that works for small workers)

26. **Let the big model decide WHAT exists; let the small model only fill.**
    Orchestrator plans every individual (class + label), server mints them
    (`mint`), tasks are sets of minted IRIs to fill (`targets`) plus IRIs to
    link to (`related`), workers `set(subject, predicate, {type, value})`.
    First run on this structure: all 34 individuals present in the right
    numbers, 0 unfilled, links land, 2 min, $0.01–0.02 — after a dozen runs
    where free-form workers never got past 1 of 10 processes.
27. **Plan on the plain document.** With `[s1] …` segment markers the 27B
    planner listed "the persons in each segment" (16 Persons, nothing
    else). Plan on the raw text and attach segments afterwards by label
    match.
28. **Mint copyable IRIs.** Small models cannot reproduce a 32-hex UUID —
    nano drifted into `49f0f4f0b4f94f4f8f4f4f4f…` and every link was rejected
    as unknown. `ex:<class>/<label-slug>` (unique per run) fixed it at once.
    Keep `style: "uuid"` for when identity must not leak the label.
29. **Coverage must be measured, not asked.** `unfilled_targets` (minted
    individuals with no property beyond type/label) replaced the LLM's own
    counting, which over-counted ("2 visits") and under-counted at will.
    Exempt classes that have no properties (a Person is complete with a
    label).
30. **Small models fill MUST slots and stop.** With the schema template in
    front of them they still skip optional-but-stated properties (date,
    performer, status, unit). Give them a per-target checklist ("go through
    the template's properties one by one") rather than "everything the text
    supports".

31. **The structure was never the ceiling; the worker was.** Same
    plan→mint→fill pipeline: nano F1 0.35 (fills MUST slots only), gemma4-27B
    F1 0.79 with one legitimate violation (fills date/performer/status/unit).
    One-shot ordered with the same 27B model is still higher (0.95) on a
    document that fits in context; the fill structure buys honesty,
    per-triple gating and measured coverage at ~2× the wall-clock.
32. **Endpoints without a tool-call parser need a text protocol.** vLLM
    without `--enable-auto-tool-choice --tool-call-parser` rejects every
    `tools=` request; `KGDC_TOOL_MODE=text` (auto on that error) has the
    model answer with one `{"tool", "args"}` JSON per message. A 27B model
    follows it reliably; sometimes it answers in prose first — the loop
    nudges twice, then the orchestrator's coverage rerun catches it.
33. **Never truncate slug IRIs.** The model rebuilds them from labels; a
    40-char cut made every long-named target unreachable.

34. **Orchestrator JSON needs element-wise salvage.** gemma4 dropped one
    colon (`"label "%"`) in a 34-entry plan — deterministic for that prompt.
    Whole-document parsing lost everything; longest-prefix lost 19 entries;
    skipping the bad element and resyncing at the next `{` keeps 33.
35. **Local 7B via Ollama drives native tools** (qwen2.5:7b: clean
    CareUnit/Person tasks) but at ~10–20 tok/s a 4k-token multi-turn task
    takes minutes; use `--workers 2` and expect 15–25 min per document until
    the box is faster. Quality on optional-but-stated slots: not yet measured
    (run cut short).

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

36. **Same model, same document, MCP fill mode is high-variance and far below one-shot ordered**
    (gemma4-g1 on LiteLLM, vignette_065, 2026-09-20): ordered 0.953 (P 0.91 / R 1.00, the
    only FPs are labels on process nodes and the CarePlan link the gold lacks); `--mcp`
    0.37 and 0.24 in two runs (0.79 earlier). Build on the ordered path; treat `--mcp` as
    the small-model experiment it is.
37. **Workers rename a lone target and lose everything.** ClinicalVisit target
    `ex:clinicalvisit/clinical_visit` → worker writes `ex:clinicalvisit/visit_1` /
    `…/visit_t9`; every triple rejected as "not a target", rerun repeats it. Server now
    rewrites foreign `ex:` subjects to the task's single target (rdf:type excluded).
    Multi-target tasks still depend on the model copying IRIs.
38. **hasProcedure is not in the text.** The visit→process links come from a convention
    (context.md), not a sentence; in fill mode the worker instead invents
    `ex:medicalprocedure/well_child_visit` from "Encounter type: Well child visit
    (procedure)". Offering the process IRIs as RELATED (`schema.dependencies`, which
    includes subclasses of the range — `subset` did not) is necessary but not sufficient.
    Adding the related individuals' segments to the task text made it worse: the worker
    started filling those non-target subjects. Ordered mode gets all 11 links.
39. **A per-segment verifier pass (`KGDC_VERIFY=1`) did not pay on this document**: 0.953 →
    0.854, +12 calls. It dropped one triple (a misspelled patient IRI — a real defect, but
    dropping the link cost more than the typo), and its "missing" list is noise because each
    verifier sees one class slice. Only worth trying on a worker that actually hallucinates.
40. **`load_dotenv()` re-adds a variable you `unset`** (it only skips keys present in the
    environment). A role-swap script must `export LLM_BASIC_AUTH=` (empty), or the Ollama
    Basic header rides along to LiteLLM as a 401 "Virtual Key expected".

## Other vocabularies (WebNLG, 2026-09-20)

41. **A new use case is three files.** `examples/webnlg-airport/` (7 classes, 16 properties)
    and `examples/webnlg-building/` (6 classes, 11 properties) were written from the
    category's triple table in ~15 min each: ontology with `rdfs:domain`/`range`, SHACL
    shapes (labels MUST, value kinds), a context file with naming conventions. No code
    change. `examples/webnlg/prepare.py` pulls the documents + gold, `score.py` scores
    by normalised label (accents, underscores, dashes, punctuation, articles, dates).
42. **Results (gemma4-g1 both roles, ordered mode, 20 docs each, size ≥ 3):** Airport
    micro F1 0.940 / macro 0.948, 12/20 perfect, ~15 s per document. Building micro 0.854 /
    macro 0.838, 9/20 perfect. Residual misses are gold conventions, not extraction
    errors: text-supported facts the gold omits ("Alcobendas, Spain" → country, 6×),
    entity granularity ("Williamsburg, Virginia" as one Place — fixed by one context
    line: 0.769 → 0.838 macro), where the gold hangs a country (on the tenant), synonymous
    entity labels ("Georgian style" vs the DBpedia "Georgian architecture"). Real errors:
    one rounded number (83.2 for 83.2104), one state-as-country.
43. **Scoring by label needs a normaliser, not a stricter prompt.** The first building
    pass scored 0.00 on most docs because of "Alan B Miller Hall" vs "Alan B. Miller
    Hall", "&" vs "and", "the College…", "March 30th, 2007" vs "30 March 2007". The
    extraction was right each time. Normalise both sides before blaming the model.
44. **Gold-vs-text artefacts also decide the CHR numbers.** With exact labels vignette_001
    scores 0.667; with honorifics stripped ("Ms." is in the gold, not in the text) 0.928.
    On 003/004/006 exactly one EvaluationProcess re-keys because the vignette text prints
    the condition's record date as the evaluation date while the gold uses the visit
    date (`fhir_to_text.py` vs `fhir_to_chr.py`). These are corpus issues to fix in the
    Thesis repo, not extraction losses.

## Ordered mode on 10 CHR documents (2026-09-20, gemma4-g1 both roles)

45. **Containers need the whole document.** The ClinicalVisit agent saw only the header
    segment; on a 22-process document it wrote "hasProcedure — not stated" (22 FN) although
    all processes were in KNOWN ENTITIES. Last build level now gets the full text
    (`run_ordered`). Side effect: the agent then types the processes it links with the
    range class (`a chr:MedicalProcedure` next to `a chr:MeasurementProcess`, 19× on one
    document) — entailed, harmless for SHACL, but it re-keys every node under identity-hash
    scoring (0.50 → 0.86). `drop_redundant_types()` removes `x a Super` when `x a Sub` is
    present, after the union and after the merge.
46. **Big documents break the fixed timeouts.** A 266-triple merge takes minutes: with
    `LLM_TIMEOUT=90` it timed out 5× (9 min wasted) and the un-merged union was returned.
    Orchestrator calls now default to `LLM_BIG_TIMEOUT=600`. A verbose pySHACL report in a
    fix prompt blew the 32k context on vignette_002 and killed the whole run; the report is
    capped (`_cap`) and an agent exception now finalises that agent with a note instead.
47. **Per-document scoring artefacts dominate the spread.** Exact-label identity-hash F1 on
    vignettes 001-010: 0.46-0.74. Honorifics stripped: 0.75-0.93. The residual per-document
    losses are the corpus date inconsistency (44), the CarePlan link, and labels on process
    nodes the gold lacks — none is an extraction error the pipeline can fix.
48. **Stricter KNOWN-ENTITIES wording backfired.** Telling agents "NEVER declare an
    individual of a class in this list" made the visit agent split one visit into four
    (vignette_008: 0.864 → 0.785). The original wording ("create only what is not in the
    list") stays. Prompt tightening is not free; measure each change on ≥2 documents.
49. **Results after the fixes (ordered, gemma4-g1 both roles; identity-hash F1 with
    honorifics stripped and redundant supertypes dropped):** 001 0.93, 002 0.87, 003 0.75,
    004 0.91, 005 0.93 (0.79 before the whole-document visit agent + merge timeout fix:
    all 22 hasProcedure links now land, merge finishes in 252 s instead of timing out),
    006 0.90, 007 0.79 (ran before the fix; same 22-link gap), 008 0.86, 009 0.90,
    010 0.93 — macro 0.88 over 10 documents. Remaining per-document loss is one
    re-keyed EvaluationProcess (corpus date, 44), 4-28 labels on process nodes and the
    CarePlan link the gold lacks. WebNLG: Airport 0.95 macro, Building 0.84 macro (42).

## ADE corpus (medical, real prose; 2026-09-20)

50. **Setup in 30 min, same recipe:** `examples/ade/` (2 classes, 2 properties, `prepare.py`
    groups the annotated sentences of one case report into a document; gold = every
    drug→adverse effect / drug→dose pair). The WebNLG label scorer is generic and scores it.
51. **Span-level gold punishes a graph.** ADE annotates each *mention*: "5-FU" and
    "5-fluorouracil", "MTX" and "methotrexate" are separate gold subjects; "hives" is the
    span where the model writes "occasional hives"; "heparin-induced thrombocytopenia" vs
    "thrombocytopenia". A KG merges the aliases into one node, so exact-label F1 is low even
    when the extraction is right. `score.py --lenient` treats a node as all its labels and
    matches arguments by containment (one-to-one): strict 0.52 / lenient 0.80 micro F1.
52. **Two conventions moved it (strict 0.38 → 0.52, lenient 0.54 → 0.80, 6/20 perfect):**
    (a) one Drug per drug with one rdfs:label per surface form used in the text (the model
    had made separate nodes for cyclophosphamide / cytoxan / CP and hung the effects on one);
    (b) every sign, symptom and finding is its own AdverseEffect next to the diagnosis (the
    model had extracted "AGEP" where the gold lists erythema, pustules, malaise, fever).
    Remaining misses are mostly gold noise: the same fact annotated once per alias, "human
    teratogen" as an effect, a typo ("cyclosposphamide") as the drug string.

## 200-document batch (2026-09-21)

53. **Batch load changes the failure modes.** 4 documents × 4 agents on one 27B endpoint made
    every call 2-3× slower: 22 of the first 107 documents lost their MeasurementProcess agent to
    the 90 s worker timeout (5 retries against the same wall, 10 min wasted each). Under batch
    load the worker timeout must match the orchestrator's (`run_all.sh` exports 600 s); 0
    timeouts in the next 100 documents.
54. **A truncated merge reply was written as the result.** Endpoints cap output tokens; a big
    document's merged graph is >8k tokens, the reply stops mid-statement and 43 of 150 outputs
    were unparsable Turtle (scored 0). Two fixes: `_final` now keeps the union when the merge
    output does not parse, and orchestrator calls send `LLM_BIG_MAX_TOKENS` (16000 in the
    batch runner). `examples/chr/repair_truncated.py` rebuilds the union from a trace for
    outputs produced before the fix. Documents with ~43 segments still overflow the 32k
    context at merge time (2 of 200) and keep the union, by design.

56. **Held-out check (docs 21-40, never read while tuning):** Airport strict macro 0.866 / lenient
    0.957 (dev set: 0.948 strict), Building 0.903 / 0.945 (dev 0.838), ADE 0.610 / 0.759 (dev
    0.576 / 0.786). The context conventions transfer; the dev-set numbers were not memorisation.
    Airport's drop is the same kinds of gold conventions as before (a fact the gold omits, a
    rounded elevation), not new failure modes.
57. **The biggest documents need the divide step inside a class.** Vignettes with 41-53 segments
    (40+ measurement processes) make one MeasurementProcess agent write a graph beyond the
    endpoint's output cap: the reply is cut off, unparsable, the merge prompt then overflows the
    32k context, and the union that remains has no processes at all (8 documents at F1 ≤ 0.07 in
    pass 1). `KGDC_MAX_SEGS_PER_AGENT` (12) now splits a class with more segments over several
    agents (`chr:MeasurementProcess#1`, `#2`, …); each reply stays small, and the known-entities
    block gives them the shared individuals. Containers (last level) stay whole.
58. **Scope filter, first version, was wrong at level 0.** "Drop new individuals of any class but
    your own" also dropped the Units a Measurement agent must create when no segment names
    chr:Unit (Unit then has no agent of its own): dangling hasUnit links, fix rounds, duplicated
    measurements (vignette_078: 0.63 → 0.12). Correct rule: drop only individuals of classes an
    earlier level actually built, including their super- and subclasses (the visit agent types
    re-created processes as MedicalProcedure). Third pass re-runs the affected documents.
