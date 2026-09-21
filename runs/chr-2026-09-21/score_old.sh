#!/bin/bash
cd ~/workspace/03_ids/kgdc && source .venv/bin/activate
for s in a b d; do : > runs/chr-2026-09-21/old_$s.txt; : > runs/chr-2026-09-21/old_$s.exact.txt
  for f in /home/ensar/workspace/03_ids/Thesis/evaluation/outputs/chr/schema/full/$s/vignette_*.ttl; do d=$(basename $f .ttl)
    python examples/chr/score.py $f examples/chr/docs/$d.gold.ttl >> runs/chr-2026-09-21/old_$s.txt 2>&1
    python examples/chr/score.py --exact $f examples/chr/docs/$d.gold.ttl >> runs/chr-2026-09-21/old_$s.exact.txt 2>&1
  done; done; echo done > runs/chr-2026-09-21/old_scored
