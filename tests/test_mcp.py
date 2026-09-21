"""kgdc-mcp over stdio: gate rejects out-of-vocabulary/out-of-scope/wrong-kind triples, SHACL sees the rest."""
import itertools, json, os, subprocess
from pathlib import Path
import pytest

ROOT = Path(__file__).parent.parent
BIN = ROOT / "kgdc-mcp/target/release/kgdc-mcp"
pytestmark = pytest.mark.skipif(not BIN.exists(), reason="build kgdc-mcp first (cargo build --release)")


class Client:
    def __init__(self):
        self.p = subprocess.Popen([str(BIN), "--ontology", str(ROOT / "examples/chr/ontology.ttl"), "--shapes", str(ROOT / "examples/chr/shapes.ttl")],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.ids = itertools.count(1)
        self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"); self.p.stdin.flush()

    def rpc(self, method, params=None):
        i = next(self.ids)
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n"); self.p.stdin.flush()
        while True:
            d = json.loads(self.p.stdout.readline())
            if d.get("id") == i:
                return d

    def call(self, name, **args):
        res = self.rpc("tools/call", {"name": name, "arguments": args})["result"]
        if res.get("isError"):
            return {"error": res["content"][0]["text"]}
        return res.get("structuredContent") or res["content"][0]["text"]

    def close(self):
        self.p.stdin.close(); self.p.wait(timeout=5)


def test_gate_and_shacl():
    c = Client()
    try:
        t = c.call("create_task", **{"class": "chr:MeasurementProcess", "text": "Body Height 104 cm"})
        r = c.call("add_triples", task_id=t["id"], lines=["ex:mp a chr:MeasurementProcess .", "ex:mp rdfs:label \"Body Height\" .", "ex:mp chr:hasResult ex:m ."])
        assert r["accepted"] == 2 and "unknown individual ex:m" in r["rejected"][0], "dangling object IRI is rejected"
        r = c.call("add_triples", task_id=t["id"], lines=["ex:m a chr:Measurement .", "ex:mp chr:hasResult ex:m ."])
        assert r["accepted"] == 2 and r["rejected"] == [], "object created in the same call is fine"
        assert any("hasPatient" in v for v in r["violations_for_task"]), "SHACL minCount must surface immediately"
        r = c.call("add_triples", task_id=t["id"], lines=["ex:x a chr:Physician ."])
        assert "not a class" in r["rejected"][0]
        r = c.call("add_triples", task_id=t["id"], lines=["ex:v a chr:ClinicalVisit ."])
        assert "outside this task's scope" in r["rejected"][0]
        r = c.call("add_triples", task_id=t["id"], lines=["ex:mp chr:hasResult \"104\" ."])
        assert "takes an IRI" in r["rejected"][0]
        r = c.call("add_triples", task_id=t["id"], lines=["ex:mp chr:hasPhysician ex:p ."])
        assert "not a property" in r["rejected"][0]
        r = c.call("add_triples", task_id=t["id"], lines=["a chr:Person .", "ex:ok a chr:MeasurementProcess ."])
        assert "not valid Turtle" in r["rejected"][0] and r["accepted"] == 1, "bad line rejected alone, good line lands"
        v = c.call("finalize", task_id=t["id"], notes=["patient not in segment"])
        assert v and v[0]["severity"] == "Violation"
        s = c.call("status")
        assert s["tasks"][0]["notes"] == ["patient not in segment"] and s["triples"] == 5
        assert "ex:mp" in c.call("export")
    finally:
        c.close()


def test_single_target_task_rewrites_foreign_subject():
    """Workers rename a lone target (ex:clinicalvisit/visit_1); the server lands the triple on the target instead of rejecting it."""
    c = Client()
    try:
        m = c.call("mint", items=[{"class": "chr:ClinicalVisit", "label": "clinical visit"}, {"class": "chr:Person", "label": "Ann"}])
        ns = "http://example.org/data/"
        visit, ann = (ns + x["uri"].split(":", 1)[1] for x in m)
        t = c.call("create_task", targets=[visit], related=[ann], text="Ann was seen")
        r = c.call("add_triples", task_id=t["id"], lines=[f"<{ns}clinicalvisit/visit_1> chr:hasPatient <{ann}> ."])
        assert r["accepted"] == 1 and r["rejected"] == [], r
        out = c.call("export")
        assert "hasPatient" in out and "visit_1" not in out
        r = c.call("add_triples", task_id=t["id"], lines=[f"<{ns}clinicalvisit/visit_1> a chr:MedicalProcedure ."])
        assert "not one of this task's targets" in r["rejected"][0], "rdf:type of an invented node is never moved onto the target"
    finally:
        c.close()
