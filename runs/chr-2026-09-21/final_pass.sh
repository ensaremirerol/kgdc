#!/bin/bash
# after the second pass: run the outputs still missing (unparsable pass-1 files queued later), then final scoring
cd ~/workspace/03_ids/kgdc && source .venv/bin/activate
R=runs/chr-2026-09-21
until [ -f $R/done ]; do sleep 30; done
sleep 5; rm -f $R/done
bash examples/chr/run_all.sh $R 3
echo done > $R/final_done
