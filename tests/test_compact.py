"""Compact graph format, prompt switches, salvage and the edit-list merge, with a stub LLM."""
import json
from pathlib import Path

from rdflib import RDF, RDFS, XSD, Graph, Literal, URIRef

import kgdc
from kgdc import compact, llm, pipeline, prompts

EX = Path(__file__).parent.parent / "examples" / "chr"
CHR = "https://w3id.org/shexmap/resource/ontology-schema/d285f599-dc2e-4bd0-83f3-df21defa8821/"
PFX = f"@prefix chr: <{CHR}> . @prefix ex: <http://example.org/data/> . @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"


def schema():
    return kgdc.load(EX / "ontology.ttl", EX / "shapes.ttl")


def test_round_trip_keeps_every_statement():
    s = schema()
    g = Graph().parse(data=PFX + """@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
        ex:m a chr:Measurement ; rdfs:label "Body \\"height\\"; standing" ; chr:hasCode <https://loinc.org/8302-2> ;
            chr:hasQuantityValue "104.0"^^xsd:float ; chr:hasUnit ex:u .
        ex:u a chr:Unit ; rdfs:label "cm" ; chr:hasCode <https://biomedit.ch/rdf/sphn-resource/ucum/cm> .""", format="turtle")
    text = compact.to_compact(g, s, {})
    g2, h2i, notes, problems = compact.parse(text, s)
    assert not problems and len(g2) == len(g)
    m = next(g2.subjects(RDF.type, URIRef(CHR + "Measurement")))
    assert g2.value(m, URIRef(CHR + "hasQuantityValue")).datatype == XSD.float, "datatype comes from the vocabulary"
    assert str(g2.value(m, RDFS.label)) == 'Body "height"; standing'


def test_same_content_gets_the_same_iri():
    s = schema()
    a, _, _, _ = compact.parse('u1 Unit "cm" hasCode=<https://biomedit.ch/rdf/sphn-resource/ucum/cm>', s)
    b, _, _, _ = compact.parse('x Unit "cm" hasCode=<https://biomedit.ch/rdf/sphn-resource/ucum/cm>', s)
    assert set(a.subjects()) == set(b.subjects()), "two agents writing the same unit mint one IRI"


def test_parser_reports_what_it_cannot_read():
    g, _, notes, problems = compact.parse('m1 Measurement "x" hasQuantityValue 12\n# UNRESOLVED: unit - not stated in the text\n???', schema())
    assert len(problems) == 2 and notes == ["UNRESOLVED: unit - not stated in the text"]


def test_compact_agent_links_known_entities_and_repairs_unreadable_lines(monkeypatch):
    s = schema()
    known_ttl = PFX + 'ex:unit_cm a chr:Unit ; rdfs:label "cm" ; chr:hasCode <https://biomedit.ch/rdf/sphn-resource/ucum/cm> .\n'
    replies = iter([
        'm1 Measurement "Body Height" hasCode=<https://loinc.org/8302-2>; hasQuantityValue=104; hasUnit=k1; hasMeasuredDate 2016',
        'm1 Measurement "Body Height" hasCode=<https://loinc.org/8302-2>; hasQuantityValue=104; hasUnit=k1; hasMeasuredDate=2016-10-04T05:20:29+02:00',
    ])
    seen = []
    def fake(prompt, model=None, **kw):
        seen.append(prompt)
        return next(replies)
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setenv("KGDC_FORMAT", "compact")
    t = pipeline._agent(s, "", {"id": "chr:Measurement", "concepts": ["chr:Measurement"], "text": "Body Height 104 cm on 2016-10-04"},
                        "", known_ttl=known_ttl)
    assert "k1 Unit \"cm\"" in seen[0], "known entities are listed by handle"
    assert "cannot read" in seen[1], "an unreadable line goes back to the model as a violation"
    g = Graph().parse(data=t["ttl"], format="turtle")
    m = next(g.subjects(RDF.type, URIRef(CHR + "Measurement")))
    assert g.value(m, URIRef(CHR + "hasUnit")) == URIRef("http://example.org/data/unit_cm"), "k1 resolves to the known IRI"
    assert t["cycles"][-1]["conforms"] and t["llm_calls"] == 2


def test_prompt_switches():
    s = schema().subset(["chr:Measurement"])
    full = prompts.extract(s, "", "text", ["chr:Measurement"])
    lean = prompts.extract(s, "", "text", ["chr:Measurement"], shacl=False, nofab=False)
    assert "CONSTRAINTS the graph is validated against" in full and "CONSTRAINTS the graph is validated against" not in lean
    assert "REQUIRED" in lean and "ucum" in lean, "without SHACL the essentials stay"
    assert "NEVER invent facts" in full and "NEVER invent facts" not in lean
    assert len(lean) < len(full)


def test_task_filter_keeps_general_and_own_bullets():
    full = schema()
    task = "# Conventions\n- Units: every chr:Unit gets chr:hasCode.\n  continued line\n- The visit links every process via chr:hasProcedure.\n- Copy dates exactly.\n"
    out = prompts.filter_task(task, full.subset(["chr:Unit"]), full)
    assert "Units:" in out and "continued line" in out and "Copy dates exactly" in out
    assert "hasProcedure" not in out


def test_salvage_keeps_complete_statements_of_a_cut_off_reply():
    s = schema()
    cut = ('ex:m1 a chr:Measurement ;\n    rdfs:label "Body Height" .\n\nex:m2 a chr:Measurement ;\n    rdfs:label "Body Weight" .\n\n'
           'ex:m3 a chr:Measurement ;\n    rdfs:label "Heart ra')
    ttl, kept, seen = pipeline.salvage(pipeline.with_prefixes(cut, s), s)
    assert (kept, seen) == (2, 3)
    assert len(set(Graph().parse(data=ttl, format="turtle").subjects(RDF.type, None))) == 2


def test_edit_merge_links_and_merges_but_never_retypes(monkeypatch):
    s = schema()
    merged = pipeline.with_prefixes("""
        ex:v a chr:ClinicalVisit ; rdfs:label "visit" .
        ex:p a chr:MeasurementProcess ; rdfs:label "height process" .
        ex:s1 a chr:ProcessStatus ; rdfs:label "completed" .
        ex:s2 a chr:ProcessStatus ; rdfs:label "done" .
        ex:c a chr:CareUnit ; rdfs:label "Wrong Clinic" .""", s)
    captured = {}
    def fake(prompt, model=None, **kw):
        captured["prompt"], captured["kw"] = prompt, kw
        h = {line.split('"')[1]: line.split()[0] for line in prompt.split("GRAPH:\n", 1)[1].splitlines() if '"' in line}
        return json.dumps({"add": [f"{h['visit']} hasProcedure={h['height process']}",
                                   f'{h["height process"]} DiagnosticStatement "Body Height"',   # a handle reused for a new node
                                   'z9 DiagnosticStatement "new thing"'],                         # a new individual
                           "same": [[h["completed"], h["done"]]],
                           "remove": [f'{h["Wrong Clinic"]} label="Wrong Clinic"']})
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setenv("KGDC_MERGE", "edits")
    final, ok, report, err = pipeline._final(s, "doc", merged, "", [], [], "", list(s.classes))
    g = Graph().parse(data=final, format="turtle")
    assert err is None and captured["kw"].get("max_tokens") == pipeline.MERGE_EDIT_MAX_TOKENS
    assert (URIRef("http://example.org/data/v"), URIRef(CHR + "hasProcedure"), URIRef("http://example.org/data/p")) in g
    assert len(set(g.subjects(RDF.type, URIRef(CHR + "ProcessStatus")))) == 1
    assert (None, RDF.type, URIRef(CHR + "DiagnosticStatement")) not in g, "no re-typing, no new individuals"
    assert (None, RDFS.label, Literal("Wrong Clinic")) in g, "removals are not applied"


def test_unknown_handle_on_a_link_is_a_problem_not_text():
    g, _, _, problems = compact.parse('v1 ClinicalVisit "visit" hasProcedure=m100', schema())
    assert problems and "m100" in problems[0]
    assert not list(g.objects(None, URIRef(CHR + "hasProcedure"))), "no literal 'm100' where an individual belongs"


def test_links_to_undeclared_individuals_are_dropped():
    s = schema()
    g = Graph().parse(data=pipeline.with_prefixes("""ex:v a chr:ClinicalVisit ; chr:hasProcedure ex:p, ex:ghost .
        ex:p a chr:MeasurementProcess ; chr:hasCode <https://loinc.org/1-1> .""", s), format="turtle")
    assert pipeline.drop_dangling(g, "http://example.org/data/") == 1
    assert (None, None, URIRef("http://example.org/data/ghost")) not in g
    assert (None, None, URIRef("https://loinc.org/1-1")) in g, "external code IRIs are not individuals"


def test_level_zero_agent_keeps_to_its_classes():
    """vignette_074: the ProcessStatus agent wrote the whole note; only the status may stay."""
    s = schema()
    sub = s.subset(["chr:ProcessStatus"])
    out = pipeline.with_prefixes("""ex:st a chr:ProcessStatus ; rdfs:label "completed" .
        ex:mp a chr:MeasurementProcess ; chr:hasStatus ex:st .
        ex:m a chr:Measurement ; rdfs:label "Body temperature" .""", s)
    removed = []
    ttl, n = pipeline.scope_filter(out, sub, ["chr:ProcessStatus"], "", removed)
    g = Graph().parse(data=ttl, format="turtle")
    assert n == 2 and set(g.subjects(RDF.type, None)) == {URIRef("http://example.org/data/st")}
    assert {why for _, _, why in removed} == {"scope"}
    notes = pipeline.scope_notes(removed, "", s, str)
    assert all("other agents build" in n for n in notes)


def test_agent_keeps_to_its_properties():
    """The status agent may not assert hasStatus (the process agent's property); a misspelled property stays for the validator."""
    s = schema()
    sub = s.subset(["chr:ProcessStatus"])
    out = pipeline.with_prefixes('ex:st a chr:ProcessStatus ; rdfs:label "completed" ; chr:hasStatus ex:st ; chr:hasStatuss ex:st .', s)
    ttl, n = pipeline.scope_filter(out, sub, ["chr:ProcessStatus"], "")
    g = Graph().parse(data=ttl, format="turtle")
    assert (None, URIRef(CHR + "hasStatus"), None) not in g
    assert (None, URIRef(CHR + "hasStatuss"), None) in g, "undeclared terms go to the validator"
    assert (None, RDFS.label, Literal("completed")) in g
