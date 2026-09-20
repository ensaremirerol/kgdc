"""Prompts. Everything vocabulary-specific comes from the Schema object."""
from __future__ import annotations

from .schema import Schema

NO_FABRICATION = """\
NEVER invent facts. Every literal, code, date, name and every relation must be
supported by the text. If a constraint asks for something the text does not
state, leave it out and add a Turtle comment line:
  # UNRESOLVED: <what is required> — not stated in the text
A missing value reported honestly is correct; a plausible value made up to
satisfy a constraint is wrong."""


def segment(schema: Schema, text: str) -> str:
    return f"""You split a document into self-contained fact units so that each can be turned
into a knowledge graph independently.

The vocabulary below defines what counts as a unit. Each segment must be about
ONE instance of a concept (or a small cluster of instances that only make sense
together, e.g. a process and its result). Group by concept, not by paragraph.

CONCEPTS:
{schema.concept_block()}

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
- Do not add facts, do not merge unrelated instances into one segment.

DOCUMENT:
\"\"\"
{text.strip()}
\"\"\"
"""


def extract(schema: Schema, context: str, segment_text: str, concepts: list[str]) -> str:
    return f"""Build an RDF graph (Turtle) for the TEXT below, using ONLY the vocabulary given.

Use exactly these prefixes:
{schema.prefix_block()}

CLASSES (rdf:type must be one of these):
{schema.concept_block()}

PROPERTIES (domain -> range):
{schema.property_block()}

CONSTRAINTS the graph is validated against (SHACL):
{schema.constraint_block()}

RULES:
1. Use only the classes and properties listed. No other namespaces, no invented terms.
2. Mint individuals as ex: IRIs named from the text so that the same real-world
   thing gets the same IRI wherever it appears (e.g. ex:person_<Name>,
   ex:<concept>_<date-or-code>). Reuse one IRI for one thing.
3. Every individual gets an rdf:type and an rdfs:label taken from the text.
4. Copy literals exactly as written in the text (dates complete, codes digit-for-digit).
   Type dates as xsd:dateTime, numbers as xsd:float or xsd:integer as appropriate.
5. Respect the domain/range and constraints above.
6. {NO_FABRICATION}

This segment is expected to describe: {', '.join(concepts) or 'see text'}.

SHARED CONTEXT (applies to this segment):
\"\"\"
{context.strip()}
\"\"\"

TEXT:
\"\"\"
{segment_text.strip()}
\"\"\"

Return ONLY Turtle. No prose, no code fences."""


def fix(schema: Schema, ttl: str, violations: str, context: str, segment_text: str) -> str:
    return f"""The graph below failed validation. Fix it — but only where the TEXT supports the fix.

VIOLATIONS:
{violations}

{NO_FABRICATION}
If a violation cannot be fixed from the text, keep the graph as is for that
node and add the # UNRESOLVED comment instead.

Use exactly these prefixes:
{schema.prefix_block()}

Allowed classes: {', '.join(sorted(schema.classes))}
Allowed properties: {', '.join(sorted(schema.properties))}

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


def merge(schema: Schema, text: str, ttl: str, violations: str, unresolved: list[str]) -> str:
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

REMAINING VIOLATIONS:
{violations or '-'}

UNRESOLVED (reported by the agents):
{chr(10).join(unresolved) or '-'}

DOCUMENT:
\"\"\"
{text.strip()}
\"\"\"

MERGED GRAPH:
{ttl}

Return ONLY the final Turtle. No prose, no code fences."""
