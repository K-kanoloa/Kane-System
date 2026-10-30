#!/usr/bin/env bash
# Native existing Kanaloa loop, in-process deterministic LLM, guarded read tool.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ "$(uname -s)" == Linux ]]
[[ -f node_modules/@deepseek-ai/dsh/lib/bin.js ]]
mkdir -p .kane
pr2_output=$(mktemp -d .kane/pr2-validation.XXXXXX)
cargo build --locked --workspace 2>&1 | tee "$pr2_output/build.log"
KANE_PR2_TEST=1 PYTHONPATH=apps/api apps/api/.venv/bin/python -m pytest -v \
  apps/api/tests/test_system_execution.py --basetemp "$pr2_output/cases" \
  2>&1 | tee "$pr2_output/integration.log"
printf 'PR-2 evidence: %s\n' "$pr2_output"
