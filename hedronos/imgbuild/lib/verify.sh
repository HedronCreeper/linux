# HedronOS build library — the quality gate. SPDX-License-Identifier: MIT
#
# The gate is deliberately the *same* tool the CI and a contributor run (tools/hedronos-verify), so
# "it built" and "it is correct" cannot drift apart. A failed gate fails the build: an ISO is a
# promise about thousands of files and we would rather not write the disc.
stage_verify() {
	local report="${WORK_ROOT}/manifest/verification-report.json"
	mkdir -p "$(dirname -- "$report")" 2>/dev/null || true
	log info "running the HedronOS verification gate"
	local args=( --root "$HEDRON_SRC" --kernel "$HEDRON_KERNEL_ROOT" --work "$WORK_ROOT" --report "$report" )
	[ "${DRY_RUN:-0}" = "1" ] && args+=( --skip build )
	# The build/dry-run check *runs this script* to prove it works. If we are inside that run (the
	# verifier sets HEDRON_VERIFY_NESTED), skip the check instead of nesting a third rehearsal.
	if [ "${HEDRON_VERIFY_NESTED:-0}" = "1" ]; then
		log info "nested under hedronos-verify's build check: not re-running the dry-run rehearsal"
		args+=( --skip build )
	fi
	export HEDRON_VERIFY_NESTED=1
	if ! python3 "$HEDRON_SRC/tools/hedronos-verify" "${args[@]}"; then
		log error "the verification gate failed; the image is not being written"
		cat >&2 <<HINT
Fix the listed checks, then resume from the failing stage:
  ./build.sh --dry-run --stage verify
The report is a JSON list of every check at ${report}.
HINT
		[ "${HEDRON_VERIFY_OVERRIDE:-0}" = "1" ] && { log error "HEDRON_VERIFY_OVERRIDE=1: continuing anyway"; return 0; }
		return 1
	fi
	verify_package_layering || { log error "package layer check failed"; return 1; }
	log ok "verification gate passed"
}

verify_package_layering() {
	py "$HEDRON_SRC" "$WORK_ROOT/manifest/packages.txt" <<'PY' || return 1
import os, sys
src, manifest = sys.argv[1], sys.argv[2]
names = set()
if os.path.isfile(manifest):
    with open(manifest, encoding="utf-8") as fh:
        names = {l.strip() for l in fh if l.strip() and not l.startswith("#")}
pkgdir = os.path.join(src, "packages")
missing = []
for name in sorted(os.listdir(pkgdir)):
    if not os.path.isfile(os.path.join(pkgdir, name, "hedronpkg.toml")):
        continue
    if name.startswith("hedronos-") and names and name not in names:
        missing.append(name)
print("packages: %d in the tree, %d in the resolved set" % (len(os.listdir(pkgdir)), len(names)))
for m in missing:
    print("not in the resolved package set:", m, file=sys.stderr)
sys.exit(1 if missing else 0)
PY
}
