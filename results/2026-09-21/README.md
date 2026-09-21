# Results snapshot, 2026-09-21 (interim)

All kgdc runs: ordered mode, gemma4-g1 (27B, LiteLLM) for both roles, 4 documents in parallel.

## CHR vignettes (200-document batch, pass 1 in progress: 157 of 200 scored)

Scorer: identity-hash triple F1 (Thesis `pipeline/canonical_iri.py`), honorifics stripped from labels and
redundant supertypes dropped on both sides (`examples/chr/score.py`). Thesis systems A/B/D are the committed
gpt-oss-120b outputs of the Thesis repo, rescored with the same scorer; unparseable outputs count as F1 = 0.

```
documents: 157 (unscorable outputs count as F1 = 0: kgdc 0, thesis_a 4, thesis_b 2, thesis_d 2)

system      microP  microR  microF1  macroP  macroR  macroF1  F1>=.9  F1<.5
kgdc         0.698   0.797    0.744   0.762   0.853    0.804      54     14
thesis_a     0.373   0.257    0.304   0.366   0.273    0.312       0    149
thesis_b     0.367   0.256    0.302   0.359   0.271    0.307       0    151
thesis_d     0.395   0.271    0.322   0.398   0.296    0.339       0    146

kgdc vs thesis_a: mean ΔF1 = +0.492, median = +0.479, wins/ties/losses = 147/2/8, sign test p = 3.2e-34
   kgdc loses most on: 151 (0.10 vs 0.44), 108 (0.14 vs 0.46), 135 (0.26 vs 0.42), 118 (0.30 vs 0.44), 047 (0.39 vs 0.45)
kgdc vs thesis_b: mean ΔF1 = +0.497, median = +0.487, wins/ties/losses = 147/3/7, sign test p = 3.3e-35
   kgdc loses most on: 151 (0.10 vs 0.44), 108 (0.14 vs 0.45), 135 (0.26 vs 0.42), 118 (0.30 vs 0.44), 047 (0.39 vs 0.45)
kgdc vs thesis_d: mean ΔF1 = +0.466, median = +0.478, wins/ties/losses = 147/0/10, sign test p = 2.2e-32
   kgdc loses most on: 160 (0.05 vs 0.53), 151 (0.10 vs 0.43), 108 (0.14 vs 0.42), 118 (0.30 vs 0.44), 150 (0.29 vs 0.43)
```

Known caveats at this snapshot: 43 pass-1 outputs whose merge reply was cut off by the endpoint's output cap were
replaced by the union of their agent graphs (`examples/chr/repair_truncated.py`); two 43-segment documents keep the
union because the merge prompt exceeds the 32k context; documents with duplicated entities (visit agent re-creating
processes) are queued for a second pass with the scope filter. See NOTES.md 53-55.

## WebNLG (20 documents each, label-level F1, `examples/webnlg/score.py`)

- Airport: micro F1 0.940, macro 0.948, 12/20 perfect (`webnlg_airport_scores.txt`)
- Building: micro F1 0.854, macro 0.838, 9/20 perfect (`webnlg_building_scores.txt`)

## ADE corpus (20 documents)

- strict label F1 micro 0.52 (`ade_scores_strict.txt`), lenient (`--lenient`: aliases as labels, span containment) micro 0.80, 6/20 perfect (`ade_scores_lenient.txt`)
