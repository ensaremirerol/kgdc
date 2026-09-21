#!/bin/bash
# after the batch: re-run documents with the duplication signature (out > 1.25 x gold, or F1 < 0.6) on the current code
cd ~/workspace/03_ids/kgdc && source .venv/bin/activate
R=runs/chr-2026-09-21
until [ -f $R/done ]; do sleep 30; done
cp $R/scores.txt $R/scores_pass1.txt
python - <<'PY' > $R/second_pass_list.txt
import re
R="runs/chr-2026-09-21"
for l in open(f"{R}/scores.txt"):
    m = re.match(r"(\S+)\s+gold=\s*(\d+) out=\s*(\d+) tp=\s*(\d+) P=([\d.]+) R=([\d.]+) F1=([\d.]+)", l)
    if m and (int(m[3]) > 1.25 * int(m[2]) or float(m[7]) < 0.6): print(m[1])
PY
echo "second pass: $(wc -l < $R/second_pass_list.txt) documents"
mkdir -p $R/pass1
for d in $(cat $R/second_pass_list.txt); do mv $R/$d.ttl $R/$d.ttl.trace.json $R/$d.log $R/pass1/ 2>/dev/null; done
rm -f $R/done
bash examples/chr/run_all.sh $R 4
echo done > $R/second_pass_done
