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
