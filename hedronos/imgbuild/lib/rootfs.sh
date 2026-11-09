# HedronOS build library — the live root filesystem. SPDX-License-Identifier: MIT
#
# pacstrap of the resolved set, then the HedronOS layer on top: our packages, /etc/hedron config,
# the profile's session and autologin choices. Every decision here comes from the profile file or
# from data/*.toml, never from a hardcoded list in this script.
stage_rootfs() {
	local rootfs="${WORK_ROOT}/rootfs"
	mkdir -p "$rootfs" 2>/dev/null || true
	pkgset_resolve "$PROFILE" "$VARIANT" "$TARGET_ARCH" >/dev/null
	pkgset_render

	local container="${HEDRON_CONTAINER_RUNNER:-}"
	if have pacstrap && [ -z "$container" ]; then
		log info "pacstrap into $rootfs"
		local mirror_args=() mirror; mirror="$(hedron_mirrorlist)"
		[ -s "$mirror" ] && mirror_args=(-C "$mirror")
		local pkgs=()
		if [ -r "$(manifest_dir)/packages.txt" ]; then
			# Only bare package names: the rendered manifest has section headers and sizes in it.
			mapfile -t pkgs < <(grep -E '^[a-z0-9@._+-]+$' "$(manifest_dir)/packages.txt" | sort -u)
		fi
		[ ${#pkgs[@]} -gt 0 ] || die "no packages resolved for profile $PROFILE — check data/package-sets.toml"
		log info "installing ${#pkgs[@]} package(s) into the live rootfs"
		run "$(sudoify)" pacstrap -c -K -M "${mirror_args[@]}" "$rootfs" "${pkgs[@]}" || die "pacstrap failed"
	elif [ -n "$container" ]; then
		log info "building the rootfs inside an ${container} archlinux container"
		run "$container" run --rm --privileged -v "$HEDRON_SRC:/src" -v "${WORK_ROOT}:/work" \
			archlinux:latest /src/imgbuild/lib/rootfs-inside.sh /work/rootfs /work/manifest/packages.txt || \
			die "container build failed (see the container output above)"
	else
		warn "no pacstrap here: writing the rootfs *plan* instead of the tree"
		local planned; planned="$(grep -c . "$(manifest_dir)/packages.txt" 2>/dev/null || echo 0)"
		record rootfs "pacstrap ${planned} packages (planned, not executed)"
		mkdir -p "${rootfs}/etc/hedron" "${rootfs}/etc/hedron/overlay.d" 2>/dev/null || true
		# A rehearsal still needs a tree for the ISO stage to squash, so materialise the config.
		local f
		for f in "$HEDRON_SRC"/data/*.toml "$HEDRON_SRC"/data/gpu-db/gpu-db.json; do
			[ -f "$f" ] && cp "$f" "${rootfs}/etc/hedron/$(basename "$f")" 2>/dev/null || true
		done
		[ -f "$(manifest_dir)/gpu-plans.json" ] && cp "$(manifest_dir)/gpu-plans.json" \
			"${rootfs}/etc/hedron/gpu-plans.json" 2>/dev/null || true
		printf 'HedronOS %s (%s/%s) build %s\n' "$OS_VERSION" "$PROFILE" "$VARIANT" "$BUILD_ID" \
			>"${rootfs}/etc/hedron/build-info" 2>/dev/null || true
	fi

	# The HedronOS layer: our packages, unpacked into the rootfs, plus the profile's choices.
	local pkg
	for pkg in "${WORK_ROOT}"/packages/*/; do
		[ -d "$pkg" ] || continue
		# mkhedronpkg lays a package out as <out>/<name>/pkg/<tree>; accept a flat <out>/<name>/
		# as well so a change to the packer cannot silently produce an image with no layer in it.
		[ -d "${pkg}pkg" ] && pkg="${pkg}pkg"
		copy_tree "$pkg" "$rootfs" 2>/dev/null || true
	done
	apply_profile_to_rootfs "$rootfs"
	write_rootfs_summary "$rootfs"
	log ok "rootfs prepared at $rootfs"
}

apply_profile_to_rootfs() {
	local rootfs="$1"
	[ -d "$rootfs" ] || return 0
	local desktop="${HEDRON_P_desktop:-kde-wayland}" tune="${HEDRON_P_tune_profile:-performance}"
	mkdir -p "${rootfs}/etc/hedron" 2>/dev/null || true

	# Which session the live image starts, and which tuning profile it applies.
	{
		printf '# written by hedronos build.sh (profile %s, variant %s)\n' "$PROFILE" "$VARIANT"
		printf '# SPDX-License-Identifier: MIT\n'
		printf 'desktop = %s\n' "$desktop"
		printf 'display_manager = %s\n' "${HEDRON_P_display_manager:-sddm}"
		printf 'autologin = %s\n' "${HEDRON_P_autologin:-false}"
		printf 'tune_profile = %s\n' "$tune"
		printf 'overlay_preset = %s\n' "${HEDRON_P_overlay_preset:-}"
		printf 'gamescope = %s\n' "${HEDRON_P_include_gamescope:-false}"
		printf 'first_setup = %s\n' "${HEDRON_P_first_setup:-chooser}"
		printf 'store_auto_select = %s\n' "${HEDRON_P_store_auto_select:-steam}"
		printf 'hostname = %s\n' "${HEDRON_P_live_hostname:-hedronos-live}"
		printf 'build_id = %s\n' "$BUILD_ID"
	} >"${rootfs}/etc/hedron/live-image.conf" 2>/dev/null || true
	record rootfs "live-image.conf desktop=$desktop tune=$tune autologin=${HEDRON_P_autologin:-false}"

	# The live image is a live image: mark it so hedron-firstboot and the session bridge behave.
	mkdir -p "${rootfs}/usr/lib/systemd/system" 2>/dev/null || true
	printf '# HedronOS live session setup. SPDX-License-Identifier: MIT\n[Unit]\nDescription=HedronOS live session setup\nAfter=multi-user.target\n\n[Service]\nType=oneshot\nExecStart=/usr/lib/hedron/hedron-firstboot.py --live\nRemainAfterExit=yes\n\n[Install]\nWantedBy=multi-user.target\n' \
		>"${rootfs}/usr/lib/systemd/system/hedron-live-setup.service" 2>/dev/null || true
	mkdir -p "${rootfs}/usr/lib/systemd/system/multi-user.target.wants" 2>/dev/null || true
	ln -sfn ../hedron-live-setup.service "${rootfs}/usr/lib/systemd/system/multi-user.target.wants/hedron-live-setup.service" 2>/dev/null || true
	record rootfs "systemd enable hedron-live-setup.service"
}

write_rootfs_summary() {
	local rootfs="$1" size=0
	if [ -d "$rootfs" ]; then
		size="$(du -sk "$rootfs" 2>/dev/null | awk '{print $1*1024}')"
	fi
	{
		printf 'HedronOS %s %s/%s rootfs summary\n' "$OS_VERSION" "$PROFILE" "$VARIANT"
		printf '  path      %s\n' "$rootfs"
		printf '  packages  %s\n' "$(grep -c . "$(manifest_dir)/packages.txt" 2>/dev/null || echo 0)"
		printf '  size      %s\n' "$(human_size "${size:-0}")"
	} | tee "$(manifest_dir)/rootfs-summary.txt" >/dev/null 2>/dev/null || true
	record rootfs "size ${size:-0}"
}
