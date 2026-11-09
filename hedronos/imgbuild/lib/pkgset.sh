# HedronOS build library — package sets. SPDX-License-Identifier: MIT
#
# One place resolves "what goes in the image": tools/pkgset.py, reading data/package-sets.toml and
# data/apps.toml. The stages never carry a package list, so the ISO, the installer and the store
# cannot disagree about what the edition contains.

HEDRON_PKGSET_JSON=""

pkgset_resolve() {
	local profile="${1:-$PROFILE}" variant="${2:-$VARIANT}" arch="${3:-$TARGET_ARCH}"
	local out; out="$(manifest_dir)/packages.json"
	mkdir -p "$(manifest_dir)" 2>/dev/null || true
	# run_local, not run: resolving a package set reads two TOML files and writes one JSON inside
	# work/. A rehearsal that skipped this would be rehearsing nothing.
	run_local python3 "$HEDRON_SRC/tools/pkgset.py" resolve --profile "$profile" --variant "$variant" \
		--arch "$arch" --data "$HEDRON_SRC/data" --packages "$HEDRON_SRC/packages" \
		--json "$out" --pretty --fail-on-error \
		|| die "pkgset.py resolve failed for profile=$profile variant=$variant arch=$arch"
	[ -s "$out" ] || die "pkgset.py wrote an empty $out"
	HEDRON_PKGSET_JSON="$out"
	export HEDRON_PKGSET_JSON
	printf '%s' "$out"
}

pkgset_names() {
	# The bare package names, one per line: the file pacstrap and the installer both consume, and
	# the file hedronos-verify compares against the package trees.
	local json="${1:-$HEDRON_PKGSET_JSON}"
	[ -r "$json" ] || return 0
	py "$json" <<'PY' || return 0
import json, sys
doc = json.load(open(sys.argv[1], encoding="utf-8"))
for pkg in doc.get("packages", []):
    print(pkg["name"] if isinstance(pkg, dict) else pkg)
PY
}

pkgset_render() {
	local json="${1:-$HEDRON_PKGSET_JSON}"
	[ -r "$json" ] || { log warn "no resolved package set to render ($json)"; return 0; }
	run_local python3 "$HEDRON_SRC/tools/pkgset.py" render --json "$json" \
		--txt "$(manifest_dir)/packages.txt" \
		--by-source "$(manifest_dir)/packages-by-source.txt" \
		--size-report "$(manifest_dir)/packages-size.txt" || log warn "pkgset render failed (the build can continue)"
	local n; n="$(grep -c . "$(manifest_dir)/packages.txt" 2>/dev/null || echo 0)"
	log info "package set: ${n} package(s)"
}

pkgset_digest() {
	local json="${1:-$HEDRON_PKGSET_JSON}"
	[ -r "$json" ] || return 0
	python3 "$HEDRON_SRC/tools/pkgset.py" digest --json "$json" 2>/dev/null | tee "$(manifest_dir)/packages.sha256" | head -1
}
