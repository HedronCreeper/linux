#!/usr/bin/env bash
# tests/run_tests.sh — the entry point `make test` uses. SPDX-License-Identifier: MIT
#
# The stdlib runner, on purpose: a build host and a contributor's laptop must both be able to run
# the suite without pip installing anything, and `hedronos-verify`'s tests/unit check invokes the
# exact same discovery command so the two can never disagree about what "the tests" are.
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd "${HERE}/.."

PY="${PYTHON_BIN:-}"
if [ -z "$PY" ]; then
	for cand in python3 python; do command -v "$cand" >/dev/null 2>&1 && { PY="$cand"; break; }; done
fi
[ -n "$PY" ] || { echo "no python3 on PATH" >&2; exit 1; }

echo "HedronOS test suite ($(pwd))"
verbose=-1
case "${1:-}" in
-v|--verbose) verbose=2 ;;
-q|--quiet) verbose=0 ;;
esac

"$PY" -m unittest discover -s tests -p 'test_*.py' -v 2>&1 | tail -n +"$(( ${verbose:-1} == 2 ? 1 : 1 ))"
status="${PIPESTATUS[0]}"
if [ "${verbose:-1}" != "2" ]; then
	"$PY" -m unittest discover -s tests -p 'test_*.py' 2>&1 | tail -3
fi
[ "$status" = "0" ] || { echo "tests FAILED" >&2; exit "$status"; }
count="$("$PY" -m unittest discover -s tests -p 'test_*.py' 2>&1 | grep -oE 'Ran [0-9]+ tests' | grep -oE '[0-9]+')"
echo "tests OK (${count:-?} tests)"
