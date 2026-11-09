# HedronOS build library — building the hedronos-* packages. SPDX-License-Identifier: MIT
#
# Every package in hedronos/packages/ is described by its own hedronpkg.toml; mkhedronpkg turns
# that into a package directory (and, with pacman-package, a .pkg.tar.zst plus repo database).
# Nothing here knows what any package contains.

stage_packages() {
	local out="${WORK_ROOT}/packages"
	mkdir -p "$out" 2>/dev/null || true
	local count=0 failed=0 dir name
	for dir in "$HEDRON_SRC"/packages/*/; do
		[ -f "${dir}hedronpkg.toml" ] || { [ -d "$dir" ] && log warn "skipping $(basename "$dir"): no hedronpkg.toml"; continue; }
		name="$(basename "$dir")"
		log info "packaging $name"
		if ! run_local python3 "$HEDRON_SRC/tools/mkhedronpkg" --src "$dir" --out "$out" --arch "$TARGET_ARCH"; then
			log error "mkhedronpkg failed for $name"
			failed=$((failed + 1))
			record packages "FAIL $name"
			continue
		fi
		count=$((count + 1))
		record packages "$name"
	done
	[ "$failed" = "0" ] || die "$failed package(s) failed to build"
	log ok "built $count HedronOS package(s) into $out"

	# The Qt GUI host is optional: without Qt6 the QML shells still ship, and hstore falls back to
	# its TUI (that fallback is why the GUI is allowed to be unbuilt).
	if have cmake && have qmake6; then
		log info "Qt6 detected: building the GUI host (hedronos/qt)"
		run make -C "$HEDRON_SRC/qt" -j"${JOBS}" all || log warn "qt build failed — GUI shells will use the TUI fallback"
	else
		log info "no cmake/qmake6: skipping the Qt GUI host (the TUI fallback covers both apps)"
	fi

	# Two packages compile C (hgm, hedron-latency). Build any package that ships a Makefile, so a
	# third one cannot arrive and be silently skipped: a missing /usr/bin entry is a bug the user
	# finds, and a stub script left in its place is worse.
	if have cc || have gcc; then
		for dir in "$HEDRON_SRC"/packages/*/; do
			[ -f "${dir}Makefile" ] || continue
			log info "compiling $(basename "$dir") (native sources)"
			run make -C "$dir" -j"${JOBS}" || log warn "native build failed in $(basename "$dir")"
		done
	else
		log warn "no C compiler: hgm and hedron-latency will not be in the packages"
	fi

	# A repo database, so the rootfs can install hedronos-* by name and the installed system can
	# update them like anything else.
	if have repo-add && [ "${DRY_RUN:-0}" != "1" ]; then
		local db="${out}/hedron-os.db"
		( cd "$out" && for pkg in *.pkg.tar.zst; do [ -e "$pkg" ] || continue; repo-add "$db" "$pkg" >/dev/null; done ) \
			&& log ok "repo database: $db" && record packages "repo-add $(basename "$db")"
	else
		record packages "repo-add skipped (no repo-add or dry run)"
	fi
	pkgset_digest
}
