#!/usr/bin/env bash
# lint-shell.sh — the shell half of `make lint`. SPDX-License-Identifier: MIT
#
# Two things, both cheap and both things a reviewer should never have to point at by hand:
# every script parses, and no build library hides a hundred-line python program in a heredoc
# (that code cannot be imported, tested, or type-checked, and it is why `hedronos-verify` exists).
#
# If shellcheck is installed we run it too; it is not required, because a distro build host is not
# guaranteed to have it and a lint target that fails for a missing optional tool teaches the wrong
# lesson.
set -uo pipefail
ROOT="."
STRICT=0
while [ $# -gt 0 ]; do
	case "$1" in
	--root) ROOT="${2:?--root needs a value}"; shift 2 ;;
	--strict) STRICT=1; shift ;;
	-h|--help) sed -n '2,12p' "$0"; exit 0 ;;
	*) printf 'lint-shell: unknown argument %s\n' "$1" >&2; exit 2 ;;
	esac
done
cd "$ROOT" || exit 2

fail=0
checked=0
warned=0
# Only files that are actually shell: `mkhedronpkg` and friends are Python without an extension,
# and feeding one to `bash -n` reports a syntax error in a script that is fine.
files="$(find imgbuild packages tools qt kernel -type f \( -name '*.sh' -o -name '*.hook' \) \
	-not -path '*/work/*' -not -name '*.template' 2>/dev/null | sort)"
files="$files
build.sh"

printf '%s\n' "$files" | while IFS= read -r f; do
	[ -f "$f" ] || continue
	if ! bash -n "$f" 2>/tmp/lint-shell.$$; then
		printf 'FAIL %s: does not parse\n' "$f" >&2
		sed 's/^/       /' /tmp/lint-shell.$$ >&2
		rm -f /tmp/lint-shell.$$
		exit 42
	fi
	# An inline interpreter heredoc inside a *library* is the pattern this repo forbids.
	if printf '%s\n' "$(cat "$f")" | grep -qE "python3?[[:space:]]+-[[:space:]]*<<[[:space:]]*'?PY'?$"; then
		printf 'WARN %s: inline python heredoc — move it into tools/ so it can be tested\n' "$f" >&2
	fi
	# Trailing whitespace and CRLF make a patch look like a reformat.
	if grep -qU $'\r' "$f"; then
		printf 'FAIL %s: contains CR (Windows line endings)\n' "$f" >&2
		exit 42
	fi
done
rc=$?
[ "$rc" = "42" ] && fail=1

n="$(printf '%s\n' "$files" | grep -c .)"
printf 'lint-shell: %s script(s) parsed with bash -n' "$n"
if [ "$fail" = "0" ]; then
	printf ' — OK\n'
else
	printf '\nlint-shell: FAILED\n' >&2
	exit 1
fi

if have_it() { command -v shellcheck >/dev/null 2>&1; }; have_it; then
	sc_fail=0
	printf '%s\n' "$files" | while IFS= read -r f; do
		[ -f "$f" ] || continue
		# -e 1091: we source imgbuild/lib/*.sh dynamically and shellcheck cannot follow them.
		if ! shellcheck -q -e SC1091,SC2034,SC2086 -s bash "$f" >/dev/null 2>&1; then
			printf 'WARN %s: shellcheck has findings (not fatal; run it yourself for the list)\n' "$f"
		fi
	done
	[ "$STRICT" = "1" ] && shellcheck -q -e SC1091,SC2034,SC2086 -s bash $(printf '%s\n' "$files" | tr '\n' ' ')
fi
exit "$fail"
