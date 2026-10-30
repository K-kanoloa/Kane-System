#!/usr/bin/env bash
# Existing Kanaloa + deterministic model, local Docker containers only.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ "$(uname -s)" == Linux ]]
[[ "$(id -u)" != 0 ]]
[[ -f node_modules/@deepseek-ai/dsh/lib/bin.js ]]
docker image inspect ubuntu:24.04 >/dev/null # Existing reference; never auto-pull.
mkdir -p .kane
pr4_output=$(mktemp -d .kane/pr4-validation.XXXXXX)
cargo build --locked --workspace 2>&1 | tee "$pr4_output/build.log"
KANE_PR4_TEST=1 PYTHONPATH=apps/api apps/api/.venv/bin/python -m pytest -v \
  apps/api/tests/test_sandbox_runtime.py --basetemp "$pr4_output/cases" \
  2>&1 | tee "$pr4_output/integration.log"
printf 'PR-4 limited evidence: %s\n' "$pr4_output"
