#!/bin/bash
# All CHR vignettes through kgdc (ordered mode), N documents in parallel, resumable, then scored.
#   examples/chr/run_all.sh OUT_DIR [PARALLEL=4]
# Roles: both on the LLM_BIG_* endpoint (the 27B setup) unless KGDC_KEEP_ROLES=1.
set -u
cd "$(dirname "$0")/../.." && source .venv/bin/activate
set -a; source .env; set +a
[ "${KGDC_KEEP_ROLES:-0}" = 1 ] || export LLM_API_KEY=$LLM_BIG_API_KEY LLM_BASE_URL=$LLM_BIG_BASE_URL LLM_MODEL=$LLM_BIG_MODEL LLM_BASIC_AUTH= LLM_PROVIDER_SORT=
OUT=${1:?out dir}; P=${2:-4}; mkdir -p "$OUT"; export OUT
export LLM_TIMEOUT=${LLM_TIMEOUT:-600} LLM_BIG_MAX_TOKENS=${LLM_BIG_MAX_TOKENS:-16000}   # merge output of a big document is >8k tokens   # P documents x 4 agents share the endpoint: a big agent graph takes minutes, not 90 s
one() { d=$(basename "$1" .txt); [ -s "$OUT/$d.ttl" ] && exit 0
  python -m kgdc examples/chr/ontology.ttl examples/chr/shapes.ttl "$1" --ordered --context examples/chr/context.md -o "$OUT/$d.ttl" > "$OUT/$d.log" 2>&1; echo "exit=$?" >> "$OUT/$d.log"; }
export -f one
ls examples/chr/docs/vignette_*.txt | xargs -P "$P" -I{} bash -c 'one {}'
: > "$OUT/scores.txt"
for f in "$OUT"/vignette_*.ttl; do d=$(basename "$f" .ttl); python examples/chr/score.py "$f" "examples/chr/docs/$d.gold.ttl" >> "$OUT/scores.txt" 2>&1; done
echo done > "$OUT/done"
