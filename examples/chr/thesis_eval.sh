#!/bin/bash
# Score kgdc outputs with the Thesis evaluator itself (pipeline/evaluate.py), next to systems a and b,
# in both IRI-alignment modes, without touching the committed eval.json:
#   examples/chr/thesis_eval.sh RUN_DIR
# Builds evaluation/outputs/chr/schema/kgdc-cmp/{a,b,c} in the Thesis repo (c = kgdc, namespace rewritten
# to the corpus one), runs evaluate.py --prompt kgdc-cmp --iri-mode {normalizer,identity-hash} and copies
# each eval.json into RUN_DIR as thesis_eval_<mode>.json.
set -eu
RUN=$(cd "$1" && pwd); T=${THESIS_DIR:-$HOME/workspace/03_ids/Thesis}; B=$T/evaluation/outputs/chr/schema
rm -rf $B/kgdc-cmp; mkdir -p $B/kgdc-cmp/c
cp -r $B/full/a $B/kgdc-cmp/a; cp -r $B/full/b $B/kgdc-cmp/b; rm -rf $B/kgdc-cmp/b/cycles
for f in $RUN/vignette_*.ttl; do sed 's#http://example.org/data/#http://example.org/clinical/#g' $f > $B/kgdc-cmp/c/$(basename $f); done
cd $T
for mode in normalizer identity-hash; do
  ${PYTHON:-python3} pipeline/evaluate.py --schema chr --track schema --prompt kgdc-cmp --iri-mode $mode > $RUN/thesis_eval_$mode.log 2>&1 || true
  cp $B/kgdc-cmp/eval.json $RUN/thesis_eval_$mode.json 2>/dev/null || true
  ${PYTHON:-python3} - "$RUN/thesis_eval_$mode.json" "$mode" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); a = d["aggregate"]
print(f"Thesis evaluator, iri-mode={sys.argv[2]}, n={d['n_vignettes']}: " + " | ".join(f"{s}{' (kgdc)' if s=='c' else ''}: P={a[s]['avg_precision']:.3f} R={a[s]['avg_recall']:.3f} F1={a[s]['avg_f1']:.3f}" for s in a if isinstance(a[s], dict) and 'avg_f1' in a[s]))
PY
done
