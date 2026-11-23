#!/usr/bin/env bash
# One-command clean reproduction: fixtures -> collector scenarios -> unit tests -> independent verification.
set -euo pipefail
cd "$(dirname "$0")"
rm -rf out
mkdir -p out
python3 -I scripts/build_fixtures.py
python3 -I scripts/run_demo.py
python3 -I tests/test_core.py
python3 -I scripts/verify_all.py
echo
echo "ALL REPRODUCTION STEPS PASSED"
