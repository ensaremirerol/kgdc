"""CHR gold-versus-output normalisation helpers used by the identity-hash scorer.

Vendored verbatim from the CHR evaluation codebase this work compares against
(``pipeline/evaluate.py``), so that ``examples/chr/score.py`` runs from a kgdc
checkout alone instead of needing that repository on disk. Only the symbols
``canonical_iri`` imports are kept, plus their transitive helpers; the extraction
was done mechanically (AST closure over top-level names), not retyped.

Do not edit to "improve" the normalisation: these rules define the metric that
every committed score in ``results/`` was computed with, and changing one
silently invalidates the comparison against the published baseline numbers.
"""
import re as _re

EX_PREFIX = "http://example.org/clinical/"

_DATETIME_PREFIX_RE = _re.compile(
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})"  # capture YYYY-MM-DDTHH:MM
    r"(?::\d{2}(?:\.\d+)?)?(?:[+-]\d{2}:\d{2}|Z)?"  # drop seconds + timezone
)

_UCUM_BASE = "https://biomedit.ch/rdf/sphn-resource/ucum/"

_SULO_UNIT_BASE = "https://w3id.org/sulo/Unit/"

_HAS_UNIT_PRED = "https://w3id.org/shexmap/resource/ontology-schema/d285f599-dc2e-4bd0-83f3-df21defa8821/hasUnit"

_HAS_CODE_PRED = "https://w3id.org/shexmap/resource/ontology-schema/d285f599-dc2e-4bd0-83f3-df21defa8821/hasCode"

_LOINC_VARIANT_RES = [
    _re.compile(r"^https?://loinc\.org/(?:id/)?(.+)$"),
]

_SNOMED_VARIANT_RES = [
    _re.compile(r"^https?://snomed\.info/(?:id/|sct/)?(.+)$"),
]

def _normalize_terminology_iri(uri: str) -> str:
    """Normalise LOINC and SNOMED IRIs to their canonical forms.

    LOINC canonical:  https://loinc.org/{code}
    SNOMED canonical: http://snomed.info/id/{code}
    """
    for pattern in _LOINC_VARIANT_RES:
        m = pattern.match(uri)
        if m:
            return f"https://loinc.org/{m.group(1)}"
    for pattern in _SNOMED_VARIANT_RES:
        m = pattern.match(uri)
        if m:
            return f"http://snomed.info/id/{m.group(1)}"
    return uri

def _normalize_datetime(val: str) -> str:
    """Truncate an xsd:dateTime literal to minute granularity.

    ``"2016-08-07T06:24:27+02:00"`` and ``"2016-08-07T06:24:00"`` both
    become ``"2016-08-07T06:24"``, making LLM-truncated timestamps match
    gold FHIR timestamps that include seconds and timezone offset.
    """
    m = _DATETIME_PREFIX_RE.match(val)
    return m.group(1) if m else val

_UCUM_STRIP_TOKENS = sorted(
    ["per", "dot", "exp", "cbl", "cbr", "sbl", "sbr", "nb", "rbl", "rbr", "apo"],
    key=len, reverse=True,  # longest first to avoid partial replacements
)

_UCUM_RAW_SPECIAL = list("./%{}[]#'()*")  # raw UCUM chars stripped after URL-decoding

def _unit_bare_key(local: str) -> str:
    """Reduce a unit IRI local name to a bare alphanumeric key.

    Handles three encoding styles:
    - SPHN-encoded  (``mmolperL``)  — strip encoded tokens (``per``, ``dot``, …)
    - URL-encoded   (``kg%2Fm2``)   — URL-decode first, then strip raw chars
    - LLM-slugged   (``mmol_l``)    — strip underscores
    """
    from urllib.parse import unquote as _unquote
    s = _unquote(local).lower()  # URL-decode first
    if s.startswith("unit_"):
        s = s[len("unit_"):]
    s = s.replace("%", "percent")             # % is a unit symbol; map to word before stripping
    for token in _UCUM_STRIP_TOKENS:           # strip encoded tokens (per, dot, …)
        s = s.replace(token, "")
    for ch in _UCUM_RAW_SPECIAL:               # strip remaining raw UCUM special characters
        s = s.replace(ch, "")
    s = s.replace("_", "").replace("-", "")
    return "__unit__" + s

def _normalize_unit_iri(uri: str) -> str:
    """Normalise unit IRIs to a bare-alphanumeric key for comparison.

    Handles SPHN UCUM IRIs (``ucum:mmolperL``), sulo IRIs (``sulo:Unit/Cel``),
    and LLM-minted local IRIs (``ex:unit_mmol_l``) — all reduce to the same
    key (e.g. ``__unit__mmoll``) by stripping UCUM encoding tokens and
    underscores before lowercasing.
    """
    for base in (EX_PREFIX, _UCUM_BASE, _SULO_UNIT_BASE):
        if uri.startswith(base):
            return _unit_bare_key(uri[len(base):])
    return uri
