#!/usr/bin/env bash
# PR-1 only. Real bundled DSH, isolated empty HOME/cwd, no model/tool calls.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ "$(uname -s)" == Linux ]]
[[ -f node_modules/@deepseek-ai/dsh/lib/bin.js ]]
[[ -x apps/api/.venv/bin/python ]]
mkdir -p .kane
pr1_output=$(mktemp -d .kane/pr1-validation.XXXXXX)
cargo build --locked --workspace 2>&1 | tee "$pr1_output/build.log"
KANE_PR1_TEST=1 PYTHONPATH=apps/api apps/api/.venv/bin/python -m pytest -v \
  apps/api/tests/test_supervised_runtime.py --basetemp "$pr1_output/cases" \
  2>&1 | tee "$pr1_output/integration.log"
printf 'PR-1 evidence: %s\n' "$pr1_output"
