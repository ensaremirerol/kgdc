#!/bin/bash
# the scoring tail of run_all.sh, run by hand: the running instance mis-read the edited script after xargs finished
cd ~/workspace/03_ids/kgdc && source .venv/bin/activate
R=runs/chr-2026-09-21
: > "$R/scores.txt"
for f in "$R"/vignette_*.ttl; do d=$(basename "$f" .ttl); python examples/chr/score.py "$f" "examples/chr/docs/$d.gold.ttl" >> "$R/scores.txt" 2>&1; done
echo done > "$R/done"
