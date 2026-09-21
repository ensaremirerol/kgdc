"""WebNLG v3.0 (en, test split) -> one text file + gold-triple JSON per entry for a category.

  python examples/webnlg/prepare.py Airport examples/webnlg-airport/docs [--n 20] [--min-size 3]

Downloads the XML once (CC BY-NC-SA 4.0, https://gitlab.com/shimorina/webnlg-dataset); the first
lexicalisation of each entry is the document, the modified triple set is the gold."""
import argparse, json, sys, urllib.request, xml.etree.ElementTree as ET
from pathlib import Path

URL = "https://gitlab.com/shimorina/webnlg-dataset/-/raw/master/release_v3.0/en/test/rdf-to-text-generation-test-data-with-refs-en.xml"
ap = argparse.ArgumentParser(); ap.add_argument("category"); ap.add_argument("out"); ap.add_argument("--n", type=int, default=20); ap.add_argument("--min-size", type=int, default=3)
a = ap.parse_args()
xml = Path(__file__).parent / "webnlg_test_en.xml"
if not xml.exists():
    print("downloading", URL, file=sys.stderr); urllib.request.urlretrieve(URL, xml)
out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
n = 0
for e in ET.parse(xml).getroot().iter("entry"):
    if e.get("category") != a.category or int(e.get("size")) < a.min_size: continue
    lex = [l.text for l in e.findall("lex") if l.text]
    if not lex: continue
    n += 1; stem = f"{a.category.lower()[:1]}{n:02d}"
    (out / f"{stem}.txt").write_text(lex[0].strip() + "\n")
    (out / f"{stem}.gold.json").write_text(json.dumps({"eid": e.get("eid"), "category": a.category, "size": e.get("size"),
        "triples": [t.text for t in e.find("modifiedtripleset").findall("mtriple")], "lex": lex}, indent=1, ensure_ascii=False))
    if n >= a.n: break
print(f"{n} documents in {out}")
