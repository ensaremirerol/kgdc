"""Pipeline mechanics with a stub LLM: segmentation JSON, fix loop, plateau, unresolved, merge."""
import json
from pathlib import Path
import kgdc
from kgdc import pipeline, llm

EX = Path(__file__).parent.parent / "examples" / "chr"
PFX = "@prefix chr: <https://w3id.org/shexmap/resource/ontology-schema/d285f599-dc2e-4bd0-83f3-df21defa8821/> . @prefix ex: <http://example.org/data/> . @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
BAD = PFX + 'ex:u a chr:Unit ; rdfs:label "cm" .\n'                       # violates UnitCodeShape (needs hasCode)
HONEST = BAD + "# UNRESOLVED: chr:hasCode UCUM IRI — not stated in the text\n"


def test_loop_stops_at_plateau_and_keeps_unresolved(monkeypatch):
    calls = []
    def fake(prompt, model=None, **kw):
        calls.append(prompt[:40])
        if prompt.startswith("You split"):
            return json.dumps({"shared_context": "Patient: X", "segments": [{"id": "s1", "concepts": ["chr:Unit"], "text": "104 cm"}]})
        return HONEST                                     # extract, every fix, and merge all return the honest graph
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(pipeline, "MAX_FIX", 2)
    r = kgdc.run(kgdc.load(EX / "ontology.ttl", EX / "shapes.ttl"), "Patient: X measured 104 cm")
    seg = r.segments[0]
    assert seg["cycles"][0]["conforms"] is False
    assert len(seg["cycles"]) == 2, "identical report twice -> plateau, no third fix call"
    assert seg["unresolved"] == ["UNRESOLVED: chr:hasCode UCUM IRI — not stated in the text"]
    assert r.conforms is False and "hasCode" in r.violations
    assert r.llm_calls == 1 + 2 + 1                     # segment + (extract + 1 fix) + merge


def test_subset_keeps_only_relevant_vocabulary():
    s = kgdc.load(EX / "ontology.ttl", EX / "shapes.ttl")
    sub = s.subset(["chr:Measurement"])
    assert "chr:Measurement" in sub.classes and "chr:Unit" in sub.classes    # range hop
    assert "chr:CarePlan" not in sub.classes
    assert all(p.startswith("chr:") for p in sub.properties) and "chr:hasMedicalProcedure" not in sub.properties
    assert sub.terms == s.terms                                                # validation stays global


def test_verifier_drops_by_index_and_reports_missing(monkeypatch):
    import re
    bad = PFX + 'ex:p a chr:Person ; rdfs:label "Ann" .\nex:q a chr:Person ; rdfs:label "Bob" .\n# UNRESOLVED: x\n'
    def fake(prompt, model=None, **kw):
        if prompt.startswith("You split"):
            return json.dumps({"shared_context": "", "segments": [{"id": "s1", "concepts": ["chr:Person"], "text": "Ann was seen"}]})
        if prompt.startswith("You audit"):
            bob = re.search(r"^(\d+): ex:q rdf:type", prompt, re.M).group(1)   # unsupported individual: its type triple
            return json.dumps({"drop": [int(bob)], "missing": ["Ann's visit date 2016-10-04"]})
        return bad
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(pipeline, "VERIFY", True)
    monkeypatch.setattr(pipeline, "MAX_FIX", 0)
    r = kgdc.run(kgdc.load(EX / "ontology.ttl", EX / "shapes.ttl"), "Ann was seen")
    seg = r.segments[0]
    assert "Bob" not in seg["ttl"] and "Ann" in seg["ttl"], "dropping a type triple removes the whole individual"
    assert seg["unresolved"] == ["UNRESOLVED: x", "MISSING (verifier): Ann's visit date 2016-10-04"]
    assert seg["llm_calls"] == 2


def test_agent_failure_is_contained(monkeypatch):
    """A worker exception (context window, dead endpoint) finalises that agent with a note; the run still produces a graph."""
    def fake(prompt, model=None, **kw):
        if prompt.startswith("You split"):
            return json.dumps({"shared_context": "", "segments": [{"id": "s1", "concepts": ["chr:Unit"], "text": "104 cm"}, {"id": "s2", "concepts": ["chr:Person"], "text": "Ann"}]})
        if "Build: chr:Unit" in prompt:
            raise RuntimeError("context length exceeded")
        return PFX + 'ex:p a chr:Person ; rdfs:label "Ann" .\n'
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(pipeline, "MAX_FIX", 0)
    r = kgdc.run(kgdc.load(EX / "ontology.ttl", EX / "shapes.ttl"), "104 cm Ann")
    failed = next(s for s in r.segments if s["id"] == "s1")
    assert failed["ttl"] == "" and failed["unresolved"][0].startswith("UNRESOLVED: agent s1 failed") and "context length" in failed["error"]
    assert "Ann" in r.ttl, "the other agent's graph survives"


def test_ordered_last_level_sees_whole_document(monkeypatch):
    """Containers (last build level) get the full text; leaf classes get their segment."""
    seen = {}
    def fake(prompt, model=None, **kw):
        if prompt.startswith("You split"):
            return json.dumps({"shared_context": "", "segments": [{"id": "s1", "concepts": ["chr:Person"], "text": "Ann was seen."},
                                                                  {"id": "s2", "concepts": ["chr:ClinicalVisit"], "text": "Visit header."}]})
        if prompt.startswith("Build an RDF graph"):
            seen[prompt.split("Build: ", 1)[1].split(" ", 1)[0]] = prompt.split("TEXT:", 1)[1]
        return HONEST
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(pipeline, "MAX_FIX", 0)
    kgdc.run_ordered(kgdc.load(EX / "ontology.ttl", EX / "shapes.ttl"), "Visit header. Ann was seen.")   # Person -> Visit: two levels
    assert "Ann was seen" in seen["chr:ClinicalVisit"], "visit agent (last level) sees the whole document"
    assert "Visit header" not in seen["chr:Person"] and "Ann was seen" in seen["chr:Person"], "person agent sees only its segment"


def test_redundant_supertype_dropped():
    from rdflib import Graph
    schema = kgdc.load(EX / "ontology.ttl", EX / "shapes.ttl")
    g = Graph().parse(data=PFX + "ex:p a chr:MeasurementProcess, chr:MedicalProcedure . ex:q a chr:MedicalProcedure .", format="turtle")
    assert pipeline.drop_redundant_types(g, schema) == 1
    assert set(g.objects(None, __import__("rdflib").RDF.type)) == {__import__("rdflib").URIRef(schema.prefixes["chr"] + "MeasurementProcess"), __import__("rdflib").URIRef(schema.prefixes["chr"] + "MedicalProcedure")}, "ex:q keeps its only type"


def test_truncated_merge_output_falls_back_to_union(monkeypatch):
    """A merge reply cut off mid-statement must not become the result; the union of agent graphs is kept."""
    good = PFX + 'ex:p a chr:Person ; rdfs:label "Ann" .\n'
    def fake(prompt, model=None, **kw):
        if prompt.startswith("You split"):
            return json.dumps({"shared_context": "", "segments": [{"id": "s1", "concepts": ["chr:Person"], "text": "Ann"}]})
        if prompt.startswith("Several agents"):
            return good + "ex:q a chr:Person ;\n    rdfs:lab"      # truncated merge output
        return good
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(pipeline, "MAX_FIX", 0)
    r = kgdc.run(kgdc.load(EX / "ontology.ttl", EX / "shapes.ttl"), "Ann")
    assert "Ann" in r.ttl and "ex:q" not in r.ttl and r.conforms and "rejected" in r.error


def test_shrunken_merge_output_falls_back_to_union(monkeypatch):
    """A merge reply that parses but holds far fewer triples than the union (cut-off reply) is rejected too."""
    big = PFX + "".join(f'ex:p{i} a chr:Person ; rdfs:label "P{i}" .\n' for i in range(6))
    def fake(prompt, model=None, **kw):
        if prompt.startswith("You split"):
            return json.dumps({"shared_context": "", "segments": [{"id": "s1", "concepts": ["chr:Person"], "text": "six people"}]})
        if prompt.startswith("Several agents"):
            return PFX + 'ex:p0 a chr:Person ; rdfs:label "P0" .\n'      # 2 of 12 triples
        return big
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(pipeline, "MAX_FIX", 0)
    r = kgdc.run(kgdc.load(EX / "ontology.ttl", EX / "shapes.ttl"), "six people")
    assert r.ttl.count("a chr:Person") == 6 and "rejected" in r.error


def test_scope_filter_drops_recreated_dependencies():
    """A level>=1 agent that re-declares a process (built earlier) loses that node; its own class survives; known IRIs untouched."""
    schema = kgdc.load(EX / "ontology.ttl", EX / "shapes.ttl")
    known = PFX + 'ex:mp1 a chr:MeasurementProcess ; rdfs:label "Body Height" .\n'
    out = PFX + ('ex:v a chr:ClinicalVisit ; rdfs:label "Visit" ; chr:hasProcedure ex:mp1, ex:mp_dup .\n'
                 'ex:mp_dup a chr:MedicalProcedure ; rdfs:label "Body Height" .\n'
                 'ex:mp1 chr:hasStatus ex:st .\n'
                 '# UNRESOLVED: chr:hasDate — not stated\n')
    ttl, dropped = pipeline.scope_filter(out, schema, ["chr:ClinicalVisit"], known)
    assert dropped == 1 and "mp_dup a" not in ttl.replace("\n", " ") and "ex:mp_dup" in ttl, "dangling link kept for the fix round"
    assert "ex:v a chr:ClinicalVisit" in ttl.replace("\n    ", " ") or "chr:ClinicalVisit" in ttl
    assert "ex:mp1 chr:hasStatus" in ttl.replace("\n    ", " ") or "hasStatus" in ttl, "triples about known IRIs are kept"
    assert "# UNRESOLVED" in ttl
