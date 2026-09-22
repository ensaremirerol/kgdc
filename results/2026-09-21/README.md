# Results snapshot, 2026-09-21 (interim)

All kgdc runs: ordered mode, gemma4-g1 (27B, LiteLLM) for both roles, 4 documents in parallel.

## CHR vignettes (200-document batch, final)

Scorer: identity-hash triple F1 (Thesis `pipeline/canonical_iri.py`), honorifics stripped from labels and
redundant supertypes dropped on both sides (`examples/chr/score.py`). Thesis systems A/B/D are the committed
gpt-oss-120b outputs of the Thesis repo, rescored with the same scorer; unparseable outputs count as F1 = 0.
Files: `chr_kgdc_final_scores.txt` (per document, final), `chr_kgdc_pass1_scores.txt` (pass 1, before the
second and third passes), `chr_thesis_{a,b,d}_scores.txt`, `chr_summary_final.txt`, `chr_comparison_final.txt`,
`chr_comparison_final_no_dev.txt` (without the 11 development vignettes), `chr_comparison_interim.txt`
(157-document snapshot taken during pass 1).

```
documents: 200 (unscorable outputs count as F1 = 0: kgdc 0, thesis_a 4, thesis_b 2, thesis_d 3)

system      microP  microR  microF1  macroP  macroR  macroF1  F1>=.9  F1<.5
kgdc         0.767   0.865    0.813   0.796   0.888    0.839      58      0
thesis_a     0.387   0.268    0.317   0.374   0.277    0.318       0    188
thesis_b     0.382   0.268    0.315   0.368   0.275    0.314       0    190
thesis_d     0.402   0.277    0.328   0.396   0.293    0.336       0    187

kgdc vs thesis_a: mean ΔF1 = +0.521, median = +0.480, wins/ties/losses = 200/0/0, sign test p = 1.2e-60
   kgdc loses most on: 151 (0.53 vs 0.44), 047 (0.59 vs 0.45), 099 (0.65 vs 0.44), 188 (0.77 vs 0.55), 157 (0.66 vs 0.43)
kgdc vs thesis_b: mean ΔF1 = +0.525, median = +0.487, wins/ties/losses = 200/0/0, sign test p = 1.2e-60
   kgdc loses most on: 151 (0.53 vs 0.44), 047 (0.59 vs 0.45), 099 (0.65 vs 0.44), 188 (0.77 vs 0.54), 157 (0.66 vs 0.43)
kgdc vs thesis_d: mean ΔF1 = +0.503, median = +0.471, wins/ties/losses = 200/0/0, sign test p = 1.2e-60
   kgdc loses most on: 151 (0.53 vs 0.43), 174 (0.78 vs 0.67), 047 (0.59 vs 0.42), 099 (0.65 vs 0.45), 081 (0.75 vs 0.54)
```

Without the development vignettes (189 documents): kgdc macro 0.836 / micro 0.811; A 0.315, B 0.310, D 0.330 macro.
After the three passes, 15 outputs with a mistyped vocabulary namespace were rewritten to the canonical one (`runs/.../nsfix/` keeps the originals; NOTES 60).

Cross-check with the Thesis repo's own evaluator (`chr_thesis_evaluator_{normalizer,identity-hash}.json`, all 200
documents, macro F1): normalizer alignment kgdc 0.779 / A 0.528 / B 0.523; identity-hash without label normalisation
kgdc 0.696 / A 0.289 / B 0.285.

Run structure: pass 1 (all 200; 90 s worker timeout and cut-off merge replies hurt the largest documents),
pass 2 (36 documents re-run with the scope filter and chunked agents), pass 3 (15 documents re-run after
the scope-filter level-0 fix). `runs/chr-2026-09-21/pass1/`, `pass2/` and `truncated/` keep the replaced outputs.

## WebNLG (20 documents each, label-level F1, `examples/webnlg/score.py`)

- Airport: micro F1 0.940, macro 0.948, 12/20 perfect (`webnlg_airport_scores.txt`)
- Building: micro F1 0.854, macro 0.838, 9/20 perfect (`webnlg_building_scores.txt`)

## ADE corpus (20 documents)

- strict label F1 micro 0.52 (`ade_scores_strict.txt`), lenient (`--lenient`: aliases as labels, span containment) micro 0.80, 6/20 perfect (`ade_scores_lenient.txt`)

## Held-out documents (docs 21-40 per corpus, run once after all tuning; `heldout/`)

| corpus | n | strict micro F1 | strict macro F1 (perfect) | lenient micro F1 | lenient macro F1 (perfect) |
|---|---|---|---|---|---|
| WebNLG Airport | 20 | 0.863 | 0.866 (12) | 0.950 | 0.957 (17) |
| WebNLG Building | 19 | 0.892 | 0.903 (10) | 0.941 | 0.945 (12) |
| ADE corpus | 20 | 0.590 | 0.610 (5) | 0.717 | 0.759 (7) |
