# Ablation, 30 documents finished by every variant

| variant | what | macro F1 | micro F1 | P | R | calls | repair calls | prompt tokens | output tokens | merge errors | unresolved | min/doc |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A | Turtle, full prompts (today) | 0.7687 | 0.6807 | 0.646 | 0.720 | 654 (100%) | 178 | 3,476,931 (100%) | 1,129,772 (100%) | 13 | 0 | 8.7 |
| B | A without the SHACL block (required slots and patterns kept) | 0.7866 | 0.6993 | 0.661 | 0.743 | 634 (97%) | 164 | 3,191,042 (92%) | 1,091,908 (97%) | 13 | 0 | 8.3 |
| C | A without the no-fabrication rule | 0.7914 | 0.7538 | 0.715 | 0.797 | 584 (89%) | 108 | 2,793,880 (80%) | 930,769 (82%) | 14 | 0 | 7.7 |
| D | compact graph format, full prompts | 0.8612 | 0.8453 | 0.813 | 0.880 | 643 (98%) | 176 | 2,274,150 (65%) | 629,716 (56%) | 14 | 0 | 5.3 |
| E | D without SHACL block and no-fabrication rule, task notes filtered per agent | 0.8052 | 0.6878 | 0.592 | 0.821 | 621 (95%) | 145 | 2,040,527 (59%) | 695,593 (62%) | 14 | 0 | 6.1 |
| F | E with the edit-list merge | 0.8188 | 0.7197 | 0.614 | 0.870 | 601 (92%) | 136 | 2,064,800 (59%) | 611,481 (54%) | 2 | 0 | 5.3 |
| G | D with the edit-list merge | 0.7788 | 0.6254 | 0.497 | 0.844 | 675 (103%) | 212 | 2,604,154 (75%) | 692,455 (61%) | 2 | 0 | 5.7 |
