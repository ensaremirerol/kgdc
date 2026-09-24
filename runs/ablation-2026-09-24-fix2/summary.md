# Ablation, 30 documents finished by every variant

| variant | what | macro F1 | micro F1 | P | R | calls | repair calls | prompt tokens | output tokens | merge errors | unresolved | min/doc |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A | Turtle, full prompts (today) | 0.8341 | 0.7894 | 0.788 | 0.791 | 560 (100%) | 103 | 2,815,840 (100%) | 971,966 (100%) | 13 | 0 | 8.3 |
| D | compact graph format, full prompts | 0.8588 | 0.8335 | 0.833 | 0.834 | 637 (114%) | 169 | 2,291,211 (81%) | 745,489 (77%) | 15 | 0 | 6.1 |
