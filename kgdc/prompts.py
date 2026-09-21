"""Prompts. Everything vocabulary-specific comes from the Schema object."""
from __future__ import annotations

from .schema import Schema

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


def extract(schema: Schema, context: str, segment_text: str, concepts: list[str], task: str = "", known: str = "") -> str:
    scope = ", ".join(concepts) or "the classes listed"
    return f"""Build an RDF graph (Turtle) for the TEXT below, using ONLY the vocabulary given.

SCOPE: output ONLY individuals of type {scope}, plus the individuals they link
to directly. Do not describe anything else the text mentions — other agents
handle the other classes. Do not invent class or property names; if a thing
has no class in the list, leave it out.

Use exactly these prefixes:
{schema.prefix_block()}

CLASSES (rdf:type must be one of these):
{schema.concept_block()}

PROPERTIES (domain -> range):
{schema.property_block()}

CONSTRAINTS the graph is validated against (SHACL):
{schema.constraint_block()}

TEMPLATES — one per class, generated from the vocabulary. Replace every
`ex:Class_1` with an IRI named from the text (one per real-world thing),
fill every slot the text supports, and drop the slots it does not (then add
an UNRESOLVED note if the slot is a MUST). Every statement starts with a
subject IRI:
{schema.skeleton_block()}

RULES:
1. Use only the classes and properties listed. No other namespaces, no invented terms.
2. Mint individuals as ex: IRIs named from the text so that the same real-world
   thing gets the same IRI wherever it appears (e.g. ex:person_<Name>,
   ex:<concept>_<date-or-code>). Reuse one IRI for one thing. Local names use
   only letters, digits, '_' and '-' (write 2016-10-04T05:20:29+02:00 as 20161004T052029).
3. Every individual gets a type (write it as `a`) and an rdfs:label taken from the text.
   Copy the prefix block above verbatim; do not declare other prefixes.
4. Copy literals exactly as written in the text (dates complete, codes digit-for-digit).
   Use exactly the datatype a constraint names (e.g. "104.0"^^xsd:float, not xsd:decimal).
5. A slot whose constraint says "kind sh:IRI" or gives a pattern takes an IRI in
   angle brackets, e.g. <https://loinc.org/8302-2> — never a quoted string.
6. Respect the domain/range and constraints above.
   When the text gives an identifier from a terminology or code system next to
   a thing (e.g. "(LOINC: 8302-2)", "(SNOMED: 195662009)") and the vocabulary
   has a property for codes, emit it through that property as an IRI in the
   form its description gives — a code only inside the label is lost.
7. {NO_FABRICATION}
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


def fix(schema: Schema, ttl: str, violations: str, context: str, segment_text: str, task: str = "", known: str = "") -> str:
    return f"""The graph below failed validation. Fix it — but only where the TEXT supports the fix.

VIOLATIONS:
{violations}

{NO_FABRICATION}
The SHARED CONTEXT counts as text. If a violation cannot be fixed from the
text, keep the graph as is for that node and add the # UNRESOLVED comment instead.
Formatting violations (wrong datatype, a quoted string where an IRI is
required, a missing angle bracket) are always fixable: the value is already
there, only its form is wrong — rewrite it, e.g. "https://x/y" -> <https://x/y>,
"104.0"^^xsd:decimal -> "104.0"^^xsd:float.

Use exactly these prefixes:
{schema.prefix_block()}

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
{ttl}

Return ONLY the corrected Turtle. No prose, no code fences."""


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

Use exactly these prefixes:
{schema.prefix_block()}
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
{ttl}

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
