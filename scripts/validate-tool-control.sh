#!/usr/bin/env bash
# Real existing Kanaloa/DSH + bounded mock + disposable local tool fixtures only.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ "$(uname -s)" == Linux ]]
[[ -f node_modules/@deepseek-ai/dsh/lib/bin.js ]]
mkdir -p .kane
pr3b_output=$(mktemp -d .kane/pr3b-validation.XXXXXX)
cargo build --locked --workspace 2>&1 | tee "$pr3b_output/build.log"
cargo test --locked -p kane-core --test tool_execution 2>&1 | tee "$pr3b_output/core.log"
KANE_PR3B_TEST=1 PYTHONPATH=apps/api apps/api/.venv/bin/python -m pytest -v \
  apps/api/tests/test_tool_control.py --basetemp "$pr3b_output/cases" \
  2>&1 | tee "$pr3b_output/integration.log"
printf 'PR-3B evidence: %s\n' "$pr3b_output"
