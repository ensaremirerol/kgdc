"""Prompts. Everything vocabulary-specific comes from the Schema object."""
from __future__ import annotations

import re

from .schema import Schema

_PREFIX_LINE = re.compile(r"^\s*(@prefix|PREFIX)\s.*$", re.I | re.M)


def _no_prefix_lines(ttl: str) -> str:
    """Graphs shown back to a model (fix, merge) lose their @prefix lines: the namespace IRIs never enter a prompt."""
    return _PREFIX_LINE.sub("", ttl).strip()

NO_FABRICATION = """\
NEVER invent facts. Every literal, code, date, name and every relation must be
supported by the text. If a MUST constraint asks for something the text does
not state, leave it out and add a Turtle comment line:
  # UNRESOLVED: <what is required> — not stated in the text
(Optional slots that the text does not fill are simply omitted, without a note.)
A missing value reported honestly is correct; a plausible value made up to
satisfy a constraint is wrong."""


def _task(task: str) -> str:
    if not task.strip():
        return ""
    return f"""
TASK NOTES (domain conventions for this task — they tell you how to encode
what the text states; they never add facts the text does not state):
{task.strip()}
"""


def segment(schema: Schema, text: str, task: str = "") -> str:
    return f"""You split a document into self-contained fact units so that each can be turned
into a knowledge graph independently.

The vocabulary below defines what counts as a unit. Each segment must be about
ONE instance of a concept (or a small cluster of instances that only make sense
together, e.g. a process and its result). Group by concept, not by paragraph.

CONCEPTS:
{schema.concept_block()}
{_task(task)}
Return ONLY JSON:
{{
  "shared_context": "<verbatim text every segment needs: identifiers, dates, actors that are stated once but apply to everything>",
  "segments": [
    {{"id": "s1", "concepts": ["<concept qname>", ...], "text": "<verbatim span(s) from the document>"}}
  ]
}}

Rules:
- "text" and "shared_context" are copied verbatim from the document — never paraphrased.
- Every fact in the document must land in exactly one segment (or in shared_context).
- If the shared context itself describes an instance of a concept (a header for the
  encounter, visit, case, document...), ALSO emit it as a segment with that concept —
  shared context is copied to every agent, but only a segment gets built.
- Do not add facts, do not merge unrelated instances into one segment.

DOCUMENT:
\"\"\"
{text.strip()}
\"\"\"
"""


def _known(known: str) -> str:
    if not known.strip():
        return ""
    return f"""
KNOWN ENTITIES — already built by earlier agents. Link to these IRIs exactly as
written; do not re-declare them and do not mint a second IRI for the same thing.
Create a new individual only for something that is not in this list:
{known.strip()}
"""


def _rules_block(schema: Schema, shacl: bool) -> str:
    """The SHACL block, or (``shacl=False``) only its essentials: the validator reports the rest."""
    if shacl:
        return "CONSTRAINTS the graph is validated against (SHACL):\n" + schema.constraint_block()
    from .compact import hints
    h = hints(schema)
    return ("REQUIRED (the graph is validated; anything else wrong is reported back to you):\n" + h) if h else ""


def extract(schema: Schema, context: str, segment_text: str, concepts: list[str], task: str = "", known: str = "",
            shacl: bool = True, nofab: bool = True) -> str:
    scope = ", ".join(concepts) or "the classes listed"
    return f"""Build an RDF graph (Turtle) for the TEXT below, using ONLY the vocabulary given.

SCOPE: output ONLY individuals of type {scope}, plus the individuals they link
to directly. Do not describe anything else the text mentions — other agents
handle the other classes. Do not invent class or property names; if a thing
has no class in the list, leave it out.

PREFIXES: {schema.prefix_names()} — they are declared for you. Write every term as a prefixed
name (chr:hasCode, ex:person_Ann, xsd:float); never write @prefix lines and never spell out a
namespace IRI. Full IRIs in angle brackets are only for external identifiers such as
<https://loinc.org/8302-2>.

CLASSES (rdf:type must be one of these):
{schema.concept_block()}

PROPERTIES (domain -> range):
{schema.property_block()}

{_rules_block(schema, shacl)}

TEMPLATES — one per class, generated from the vocabulary. Replace every
`ex:Class_1` with an IRI named from the text (one per real-world thing),
fill every slot the text supports, and drop the slots it does not{' (then add an UNRESOLVED note if the slot is a MUST)' if nofab else ''}. Every statement starts with a
subject IRI:
{schema.skeleton_block()}

RULES:
1. Use only the classes and properties listed. No other namespaces, no invented terms.
2. Mint individuals as ex: IRIs named from the text so that the same real-world
   thing gets the same IRI wherever it appears (e.g. ex:person_<Name>,
   ex:<concept>_<date-or-code>). Reuse one IRI for one thing. Local names use
   only letters, digits, '_' and '-' (write 2016-10-04T05:20:29+02:00 as 20161004T052029).
3. Every individual gets a type (write it as `a`) and an rdfs:label taken from the text.
4. Copy literals exactly as written in the text (dates complete, codes digit-for-digit).
   Use exactly the datatype a constraint names (e.g. "104.0"^^xsd:float, not xsd:decimal).
5. A slot whose constraint says "kind sh:IRI" or gives a pattern takes an IRI in
   angle brackets, e.g. <https://loinc.org/8302-2> — never a quoted string.
6. Respect the domain/range and constraints above.
   When the text gives an identifier from a terminology or code system next to
   a thing (e.g. "(LOINC: 8302-2)", "(SNOMED: 195662009)") and the vocabulary
   has a property for codes, emit it through that property as an IRI in the
   form its description gives — a code only inside the label is lost.
7. {NO_FABRICATION if nofab else 'Take every fact from the TEXT.'}
   The SHARED CONTEXT below is part of the text: facts stated there (dates,
   identifiers, actors) are stated facts — use them, do not mark them unresolved.

{_task(task)}{_known(known)}
Build: {', '.join(concepts) or 'see text'} — every instance the text states.

SHARED CONTEXT (applies to this segment):
\"\"\"
{context.strip()}
\"\"\"

TEXT:
\"\"\"
{segment_text.strip()}
\"\"\"

Return ONLY Turtle. No prose, no code fences."""


def fix(schema: Schema, ttl: str, violations: str, context: str, segment_text: str, task: str = "", known: str = "",
        nofab: bool = True) -> str:
    return f"""The graph below failed validation. Fix it — but only where the TEXT supports the fix.

VIOLATIONS:
{violations}

{_fix_honesty(nofab)}
Formatting violations (wrong datatype, a quoted string where an IRI is
required, a missing angle bracket) are always fixable: the value is already
there, only its form is wrong — rewrite it, e.g. "https://x/y" -> <https://x/y>,
"104.0"^^xsd:decimal -> "104.0"^^xsd:float.

PREFIXES: {schema.prefix_names()} — declared for you; write prefixed names only, no @prefix lines.

Allowed classes: {', '.join(sorted(schema.classes))}
Allowed properties: {', '.join(sorted(schema.properties))}
{_task(task)}{_known(known)}
SHARED CONTEXT:
\"\"\"
{context.strip()}
\"\"\"
TEXT:
\"\"\"
{segment_text.strip()}
\"\"\"

GRAPH:
{_no_prefix_lines(ttl)}

Return ONLY the corrected Turtle. No prose, no code fences."""


def _fix_honesty(nofab: bool) -> str:
    if not nofab:
        return "The SHARED CONTEXT counts as text."
    return (NO_FABRICATION + "\nThe SHARED CONTEXT counts as text. If a violation cannot be fixed from the\n"
            "text, keep the graph as is for that node and add the # UNRESOLVED comment instead.")


def merge(schema: Schema, text: str, ttl: str, violations: str, unresolved: list[str], unparsed: str = "", task: str = "") -> str:
    return f"""Several agents each built part of this graph from one segment of the document.
You see the whole document. Produce the final graph.

Do:
- Merge duplicates: if two IRIs denote the same real-world thing, keep one and
  rewrite all references to it.
- Add links between segments that the document states but no single segment could see.
- Remove anything the document does not support.
- Resolve the remaining validation problems below where the document supports it.

{NO_FABRICATION}

PREFIXES: {schema.prefix_names()} — declared for you; write prefixed names only, no @prefix lines.
Allowed classes: {', '.join(sorted(schema.classes))}
Allowed properties: {', '.join(sorted(schema.properties))}

CONSTRAINTS:
{schema.constraint_block()}
{_task(task)}
REMAINING VIOLATIONS:
{violations or '-'}

UNRESOLVED (reported by the agents):
{chr(10).join(unresolved) or '-'}

AGENT OUTPUT THAT COULD NOT BE PARSED (recover what the document supports; fix the syntax):
{unparsed or '-'}

DOCUMENT:
\"\"\"
{text.strip()}
\"\"\"

MERGED GRAPH:
{_no_prefix_lines(ttl)}

Return ONLY the final Turtle. No prose, no code fences."""


def verify(lines: list[str], context: str, segment_text: str, task: str = "") -> str:
    return f"""You audit a knowledge graph that an agent built from the TEXT. Answer two questions, nothing else.

1. DROP: which numbered triples does the TEXT (with the SHARED CONTEXT and the task notes) NOT
   support? A value that differs from the text (digit, date, name), a link the text does not
   state, an individual the text never mentions. Encoding conventions from the task notes count
   as support (a code IRI built from a code in the text is supported). Never drop a triple for
   being incomplete or for a formatting choice.
2. MISSING: which facts does the TEXT state that no triple carries? One short line each, naming
   the thing and the value exactly as the text gives it. Empty if nothing is missing.
{_task(task)}
SHARED CONTEXT (counts as text):
\"\"\"
{context.strip()}
\"\"\"

TEXT:
\"\"\"
{segment_text.strip()}
\"\"\"

TRIPLES:
{chr(10).join(lines)}

Return ONLY JSON: {{"drop": [<triple numbers>], "missing": ["<stated fact absent from the graph>"]}}"""


# ------------------------------------------------------------------ task notes, filtered per agent
def filter_task(task: str, schema: Schema, full: Schema) -> str:
    """Keep the task notes that concern this agent: a bullet that names a class or property of
    ``schema`` (the agent's slice), or that names no vocabulary term at all (a general rule).
    Headings and text outside bullets always stay. Nothing is summarised or rewritten."""
    if not task.strip():
        return task
    local = lambda q: q.split(":", 1)[1] if ":" in q else q
    mine = {local(q) for q in list(schema.classes) + list(schema.properties)}
    every = {local(q) for q in list(full.classes) + list(full.properties) + list(full.parents)}
    blocks, cur = [], []
    for line in task.splitlines():
        if re.match(r"^\s*[-*]\s", line) and not line.startswith("  "):
            if cur: blocks.append(cur)
            cur = [line]
        elif cur and (line.startswith(" ") or line.startswith("\t")) and line.strip():
            cur.append(line)
        else:
            if cur: blocks.append(cur); cur = []
            blocks.append([line])
    if cur: blocks.append(cur)
    keep = []
    for b in blocks:
        text = "\n".join(b)
        if not re.match(r"^\s*[-*]\s", b[0]):
            keep.append(text); continue
        named = {w for w in re.findall(r"[A-Za-z]+", text) if w in every}
        if not named or named & mine:
            keep.append(text)
    return "\n".join(keep)


# ------------------------------------------------------------------ compact graph format (KGDC_FORMAT=compact)
FORMAT = """FORMAT — one line per individual, nothing else:
  <handle> <Class> "<label>" <property>=<value>; <property>=<value>; ...
- <handle> is a short name you choose (m1, u1, p2) and reuse whenever you refer to that individual.
  A KNOWN ENTITY is referred to by its handle (k3) and is not declared again.
- Class and property names exactly as listed.
- Values: numbers, dates and codes exactly as written in the text; text containing spaces or ';'
  in double quotes; an external identifier as a full IRI in angle brackets
  (<https://loinc.org/8302-2>); another individual by its handle.
- Several values for one property: repeat it (hasProcedure=p1; hasProcedure=p2).
- More statements about an individual you declared: a new line starting with its handle, no class."""


def _known_compact(known: str) -> str:
    if not known.strip():
        return ""
    return f"""
KNOWN ENTITIES — already built by earlier agents. Refer to them by handle; do not declare them
again and do not create a second individual for the same thing:
{known.strip()}
"""


def extract_compact(schema: Schema, context: str, segment_text: str, concepts: list[str], task: str = "", known: str = "",
                    shacl: bool = True, nofab: bool = True, built: set | None = None) -> str:
    from .compact import template, vocab_block
    scope = ", ".join(c.split(":")[-1] for c in concepts) or "the classes listed"
    return f"""Describe the TEXT below as a knowledge graph, using ONLY the vocabulary given.

SCOPE: write ONLY individuals of class {scope}, plus the individuals they link to directly.
Other agents handle the other classes. Do not invent class or property names; if a thing has no
class in the list, leave it out.

{FORMAT}

{vocab_block(schema)}

{("CONSTRAINTS the graph is validated against (SHACL):" + chr(10) + schema.constraint_block()) if shacl else ""}

TEMPLATES — one line per class. Fill every slot the text supports, drop the others:
{template(schema, hints=not shacl, built=built)}

RULES:
1. One individual per real-world thing; every individual gets a class and a label from the text.
2. Copy values exactly as the text writes them (dates complete, codes digit for digit).
3. When the text gives a code from a terminology next to a thing (e.g. "(LOINC: 8302-2)") and the
   vocabulary has a property for codes, write it through that property as the IRI its description gives.
4. {NO_FABRICATION if nofab else 'Take every fact from the TEXT.'}
   The SHARED CONTEXT is part of the text: facts stated there are stated facts.
{_task(task)}{_known_compact(known)}
Build: {scope} — every instance the text states.

SHARED CONTEXT (applies to this segment):
\"\"\"
{context.strip()}
\"\"\"

TEXT:
\"\"\"
{segment_text.strip()}
\"\"\"

Return ONLY the lines. No prose, no code fences."""


def fix_compact(schema: Schema, graph: str, violations: str, context: str, segment_text: str, task: str = "", known: str = "",
                nofab: bool = True) -> str:
    from .compact import vocab_block
    return f"""The graph below failed validation. Fix it, but only where the TEXT supports the fix.

VIOLATIONS:
{violations}

{_fix_honesty(nofab)}
A value in the wrong form (a code written as text where an IRI is required) is always fixable:
the value is there, only its form is wrong.

{FORMAT}

{vocab_block(schema)}
{_task(task)}{_known_compact(known)}
SHARED CONTEXT:
\"\"\"
{context.strip()}
\"\"\"
TEXT:
\"\"\"
{segment_text.strip()}
\"\"\"

GRAPH:
{graph}

Return ONLY the corrected graph, every line of it. No prose, no code fences."""


def merge_edits(schema: Schema, text: str, graph: str, violations: str, unresolved: list[str], unparsed: str = "", task: str = "",
                nofab: bool = True) -> str:
    """Link pass (KGDC_MERGE=edits): the model returns edits, not the graph."""
    from .compact import vocab_block
    return f"""Several agents built the graph below from one document, one class at a time. Identical
copies are already merged. You see the whole document. Return the edits the graph needs:

- "add": statements the document supports that the graph lacks, above all links between parts
  of the document that no single agent could see (a visit and its procedures, a plan and what it
  refers to), as "<handle> <property>=<value>". Only handles that are in the graph: do not create
  individuals, do not declare a class, do not add labels.
- "same": pairs of handles that denote the same real-world thing (the first one is kept).
Also fix the remaining violations below where the document allows it. Empty lists if nothing is needed.
{(chr(10) + NO_FABRICATION + chr(10)) if nofab else ''}
{FORMAT}

{vocab_block(schema)}
{_task(task)}
REMAINING VIOLATIONS:
{violations or '-'}

UNRESOLVED (reported by the agents):
{chr(10).join(unresolved) or '-'}

AGENT OUTPUT THAT COULD NOT BE READ (recover what the document supports, as "add" lines):
{unparsed or '-'}

DOCUMENT:
\"\"\"
{text.strip()}
\"\"\"

GRAPH:
{graph}

Return ONLY JSON: {{"add": ["<handle> <property>=<value>", ...], "same": [["<keep>", "<drop>"], ...]}}"""
