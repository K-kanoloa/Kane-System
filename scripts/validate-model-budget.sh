#!/usr/bin/env bash
# Original Kanaloa/DSH + bounded synthetic model, no production side effects.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ "$(uname -s)" == Linux ]]
[[ -f node_modules/@deepseek-ai/dsh/lib/bin.js ]]
mkdir -p .kane
pr3a_output=$(mktemp -d .kane/pr3a-validation.XXXXXX)
cargo build --locked --workspace 2>&1 | tee "$pr3a_output/build.log"
KANE_PR3A_TEST=1 PYTHONPATH=apps/api apps/api/.venv/bin/python -m pytest -v \
  apps/api/tests/test_model_budget.py --basetemp "$pr3a_output/cases" \
  2>&1 | tee "$pr3a_output/integration.log"
printf 'PR-3A evidence: %s\n' "$pr3a_output"
