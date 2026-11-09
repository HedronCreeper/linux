# HedronOS build library — preflight. SPDX-License-Identifier: MIT
#
# Fail in the first five seconds with a sentence, not forty minutes in with a stack trace. This is
# also where "what will this build need from you" gets answered, so the list is printed as part of
# the failure, not only on success.

stage_preflight() {
	local missing=() need=()
	local membytes; membytes="$(awk '/MemTotal/{printf "%d", $2*1024}' /proc/meminfo 2>/dev/null || echo 0)"
	log info "preflight: host $(uname -sr), $(nproc) cpus, $(human_size "${membytes:-0}")"

	need=(python3 bash tar sha256sum awk sed grep)
	[ "${DRY_RUN:-0}" = "1" ] || need+=(pacman mkarchiso mksquashfs xorriso)
	local c
	for c in "${need[@]}"; do
		have "$c" || missing+=("$c")
	done
	if [ ${#missing[@]} -gt 0 ]; then
		log warn "missing: ${missing[*]}"
		if [ "${DRY_RUN:-0}" = "1" ]; then
			log info "dry run: continuing — the rehearsal makes every decision and runs nothing privileged"
		else
			cat >&2 <<HDR
A real image build needs these tools on the *host*:

  pacman, mkarchiso      arch-install-scripts + mkinitcpio  (Arch host) — otherwise run the build
                         inside a container:  podman run -v $PWD:/src archlinux:latest ...
  mksquashfs              squashfs-tools
  xorriso                 libisofs/xorriso
  python3                 stdlib only, no pip install needed

HedronOS deliberately does not build its ISO on a shared server: run it on your own machine with
  hedronos/build.sh --profile ${PROFILE} --variant ${VARIANT}
HDR
			die "preflight: missing ${missing[*]}"
		fi
	fi

	# Where are we? The kernel tree is the parent directory and it must look like a kernel.
	if [ ! -f "${HEDRON_KERNEL_ROOT}/Makefile" ] || ! grep -q "^VERSION" "${HEDRON_KERNEL_ROOT}/Makefile" 2>/dev/null; then
		[ "${DRY_RUN:-0}" = "1" ] && warn "no kernel Makefile at ${HEDRON_KERNEL_ROOT} (dry run continues)" \
			|| die "the kernel tree is not at ${HEDRON_KERNEL_ROOT} — hedronos/ must live inside a linux checkout"
	fi
	if [ ! -r "${HEDRON_KERNEL_ROOT}/COPYING" ]; then
		warn "no COPYING in the kernel root — is this really a kernel tree?"
	fi

	# Container fallback for non-Arch hosts, so a Fedora/Debian user is one command away from an ISO.
	if ! have pacman; then
		local runner=""
		have podman && runner=podman
		[ -z "$runner" ] && have docker && runner=docker
		if [ -n "$runner" ]; then
			log info "non-Arch host: ${runner} detected — the real build runs its rootfs stages in an archlinux container"
			record preflight "container-runner ${runner}"
		fi
		export HEDRON_CONTAINER_RUNNER="$runner"
	fi

	# Disk space: an ISO build wants ~3x the image size in work/ plus the package pool.
	local avail; avail="$(df -Pk . 2>/dev/null | awk 'NR==2{printf "%d", $4*1024}')"
	if [ -n "${avail:-}" ] && [ "$avail" -lt 12000000000 ]; then
		[ "${DRY_RUN:-0}" = "1" ] && warn "less than 12 GiB free under $PWD (dry run: fine)" \
			|| die "less than 12 GiB free under $PWD — a dvd variant needs room for the package pool"
	fi

	# Network: needed to fetch packages unless --offline was given.
	if [ -z "${OFFLINE_POOL:-}" ] && [ "${DRY_RUN:-0}" != "1" ]; then
		if have curl && ! curl -fsS --max-time 8 -o /dev/null https://geo.mirror.pkgbuild.com/ 2>/dev/null; then
			[ "${ALLOW_OFFLINE:-0}" = "1" ] && warn "no network and --allow-offline: using the local package pool only" \
				|| die "no network to a mirror and --allow-offline not given (pass --offline DIR to use a package pool)"
		fi
	fi
	[ -n "${OFFLINE_POOL:-}" ] && { [ -d "$OFFLINE_POOL" ] || die "--offline $OFFLINE_POOL is not a directory"; }

	# root? only for the stages that mount, chroot and strip.
	if [ "${DRY_RUN:-0}" != "1" ] && [ "$(id -u)" != "0" ]; then
		SUDO="$(sudoify)"; [ -n "$SUDO" ] && log info "this build will use '$SUDO' for mount/pacstrap/chroot"
	fi

	record preflight "host $(uname -sr) cpus $(nproc) profile ${PROFILE} variant ${VARIANT} arch ${TARGET_ARCH} kernel ${KERNEL_MODE}"
	record preflight "tools $(for c in "${need[@]}"; do have "$c" && printf '%s ' "$c"; done)"
	log ok "preflight passed"
}
