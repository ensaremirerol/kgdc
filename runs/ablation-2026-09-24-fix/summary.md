# Ablation, 30 documents finished by every variant

| variant | what | macro F1 | micro F1 | P | R | calls | repair calls | prompt tokens | output tokens | merge errors | unresolved | min/doc |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| D | compact graph format, full prompts | 0.8646 | 0.8348 | 0.824 | 0.846 | 634 (100%) | 163 | 2,256,205 (100%) | 609,074 (100%) | 13 | 0 | 5.1 |
| G | D with the edit-list merge | 0.8169 | 0.7827 | 0.769 | 0.797 | 643 (101%) | 170 | 2,475,102 (110%) | 642,568 (105%) | 2 | 0 | 5.2 |
