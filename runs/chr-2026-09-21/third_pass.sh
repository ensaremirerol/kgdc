#!/bin/bash
# after the final pass: re-run second-pass documents that regressed vs pass 1 or show level-0 drops (scope-filter bug)
cd ~/workspace/03_ids/kgdc && source .venv/bin/activate
R=runs/chr-2026-09-21
until [ -f $R/final_done ]; do sleep 60; done
python - <<'PY' > $R/third_pass_list.txt
import re
R="runs/chr-2026-09-21"
def load(p): return {m[1]: float(m[2]) for l in open(p) for m in [re.match(r"(\S+)\s+gold=.*F1=([\d.]+)", l)] if m}
now, p1 = load(f"{R}/scores.txt"), load(f"{R}/scores_pass1.txt")
redo = set()
for d in open(f"{R}/second_pass_list.txt").read().split() + ["vignette_152", "vignette_153", "vignette_154"]:
    log = open(f"{R}/{d}.log").read() if __import__("os").path.exists(f"{R}/{d}.log") else ""
    head = log.split("level 1:", 1)[0]
    if "dropped" in head or now.get(d, 0) < p1.get(d, 0) - 0.02 or now.get(d, 0) < 0.6: redo.add(d)
print("\n".join(sorted(redo)))
PY
mkdir -p $R/pass2
for d in $(cat $R/third_pass_list.txt); do mv $R/$d.ttl $R/$d.ttl.trace.json $R/$d.log $R/pass2/ 2>/dev/null; done
rm -f $R/done $R/final_done
bash examples/chr/run_all.sh $R 4
echo done > $R/third_done
