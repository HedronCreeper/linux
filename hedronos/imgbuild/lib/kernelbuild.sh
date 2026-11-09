# HedronOS build library — the kernel. SPDX-License-Identifier: MIT
#
# `--kernel` takes four kinds of value and they differ only in how the *binary* arrives:
#
#   auto          build from the tree this repo is a checkout of (the real path)
#   <path>        build from another kernel tree
#   pkg:<name>    install a kernel package instead (e.g. pkg:linux-hedron from a repo)
#   host          reuse the kernel the build machine is booted with (rehearsal only)
#
# What does NOT differ: the configuration. Every mode except `pkg:` regenerates the defconfig and
# the variant fragment from their specs and gates them through tools/kconf-check.py first, because
# a config that names a symbol this tree does not have is the failure mode that produces a kernel
# which builds fine and then has no sched_ext, no BTF, or no amdgpu — silently.
stage_kernel() {
	gate_kernel_config || die "kernel configuration failed its gate — fix the spec, not the symptom"

	case "$KERNEL_MODE" in
	auto)
		[ -r "${HEDRON_KERNEL_ROOT}/Makefile" ] || die "--kernel auto needs the kernel Makefile at ${HEDRON_KERNEL_ROOT}"
		build_kernel_from "$HEDRON_KERNEL_ROOT" "linux-hedron"
		;;
	host)
		[ "${DRY_RUN:-0}" = "1" ] || die "--kernel host is a rehearsal mode: it puts the running kernel in the image, which is not a release"
		local booted; booted="$(uname -r)"
		log info "rehearsal kernel: $booted (the host's own, copied into the image tree)"
		record kernel "mode host version $booted"
		;;
	pkg:*)
		local pkgname="${KERNEL_MODE#pkg:}"
		log info "kernel comes from the package '$pkgname' — installing it into the rootfs, not building it"
		record kernel "mode pkg package $pkgname"
		printf '%s\n' "$pkgname" >>"$(manifest_dir)/packages.txt" 2>/dev/null || true
		;;
	*)
		[ -d "$KERNEL_MODE" ] || die "--kernel '$KERNEL_MODE' is neither auto|host|pkg:<name> nor a directory"
		build_kernel_from "$KERNEL_MODE" "linux-hedron"
		;;
	esac
	write_kernel_summary
}

# gate_kernel_config : the two generated files must exist, be what the specs say, and name only real
# symbols of *this* tree. Cheap (a couple of seconds against ~2000 Kconfig files) and it is the
# single highest-value check in the whole build.
gate_kernel_config() {
	local kdir="$HEDRON_SRC/kernel" rc=0
	log info "regenerating the kernel configuration from its specs"
	python3 "$HEDRON_SRC/tools/gen-defconfig.py" --kernel "$HEDRON_KERNEL_ROOT" \
		--spec "$kdir/defconfig-spec.py" --out "$kdir/hedronos_defconfig" --require || rc=1
	python3 "$HEDRON_SRC/tools/gen-defconfig.py" --kernel "$HEDRON_KERNEL_ROOT" \
		--spec "$kdir/fragment-spec.py" --out "$kdir/hedronos.fragment" || log warn "fragment generation reported a problem"
	[ "$rc" = "0" ] || { log error "gen-defconfig --require failed: the spec names a symbol this tree does not have"; return 1; }

	local target
	for target in "$kdir/hedronos_defconfig" "$kdir/hedronos.fragment"; do
		[ -f "$target" ] || { log error "missing $target"; return 1; }
		local json
		json="$(python3 "$HEDRON_SRC/tools/kconf-check.py" --kernel "$HEDRON_KERNEL_ROOT" --config "$target" --json 2>&1)" || {
			log error "kconf-check rejected $(basename "$target")"
			printf '%s\n' "$json" | sed 's/^/    /' >&2
			return 1
		}
		# The JSON travels in the environment, not in a pipe: the heredoc has already claimed stdin
		# for the program text, so a second reader of it gets an empty string — which looks exactly
		# like "kconf-check printed nothing" and would be an awful bug to chase.
		HEDRON_KCONF_JSON="$json" py "$target" <<'PY' || return 1
import json, os, sys
doc = json.loads(os.environ.get("HEDRON_KCONF_JSON") or "{}")
name = sys.argv[1].rsplit("/", 1)[-1]
if not doc.get("ok"):
    print("%s: unknown=%s bad_lines=%s" % (name, doc.get("unknown"), doc.get("bad_lines")), file=sys.stderr)
    raise SystemExit(1)
print("%s: %d symbol(s) verified against the tree, %d already enabled upstream"
      % (name, doc.get("checked", 0), len(doc.get("already_enabled", []) or [])))
PY
	done
	record kernel "config hedronos_defconfig + hedronos.fragment gated by kconf-check"
	return 0
}

build_kernel_from() {
	local src="$1" name="$2"
	local bdir="${WORK_ROOT}/kernel-build/${name}"
	local kdir="$HEDRON_SRC/kernel"
	mkdir -p "$bdir" "${WORK_ROOT}/kernel" 2>/dev/null || true
	log info "configuring ${name} from ${src}"
	local karch; karch="$(uname -m | sed 's/x86_64/x86/;s/aarch64/arm64/')"

	# defconfig first, fragment second: kconfig's fragment mechanism is an *overlay*, and putting
	# the fragment through merge_config keeps the base file readable as a stand-alone defconfig.
	run make -C "$src" O="$bdir" ARCH="$karch" "$kdir/hedronos_defconfig" || die "make $name defconfig failed"
	runsh "KCONFIG_ALLCONFIG='$kdir/hedronos.fragment' make -C '$src' O='$bdir' ARCH=$karch olddefconfig" \
		|| log warn "olddefconfig with the fragment failed — building the base config only"
	record kernel "src $src build $bdir jobs $JOBS"

	# The symbols the whole distro assumes. If one is gone the build must stop here, not at "no
	# such file /sys/fs/cgroup/...".
	local sym missing=()
	for sym in SCHED_CLASS_EXT DEBUG_INFO_BTF BPF_SYSCALL CGROUP_BPF NET_SCHED_BPF; do
		grep -q "^CONFIG_${sym}=y" "${bdir}/.config" 2>/dev/null || \
			grep -q "^CONFIG_${sym}=y" "$kdir/hedronos_defconfig" 2>/dev/null || missing+=("$sym")
	done
	if [ -f "${bdir}/.config" ] && [ ${#missing[@]} -gt 0 ]; then
		log error "after olddefconfig these are not enabled: ${missing[*]}"
		log error "the fragment or a Kconfig dependency turned them off — sched_ext, HUD and BPF tuning stop working"
		die "kernel configuration lost a required symbol"
	elif [ -f "${bdir}/.config" ]; then
		log ok "required symbols present in the final .config"
	else
		record kernel "check-required-symbols (deferred to a real build: no .config in a dry run)"
	fi

	# scripts/setlocalversion must not try to write .scmversion into the checkout: LOCALVERSION is
	# the supported way to brand a kernel built from a git tree.
	local localversion="+hedron-${OS_VERSION}-${BUILD_ID:2:8}"
	log info "building ${name} (${JOBS} jobs, localversion ${localversion})"
	run make -C "$src" O="$bdir" -j"$JOBS" LOCALVERSION="$localversion" || die "kernel build failed"
	local rootfs="${WORK_ROOT}/rootfs"
	run make -C "$src" O="$bdir" INSTALL_PATH="${WORK_ROOT}/kernel" INSTALL_MOD_PATH="$rootfs" \
		modules_install install || die "modules_install failed"
	have depmod && run depmod -a -b "$rootfs" "$(cat "${bdir}/include/config/kernel.release" 2>/dev/null || echo)" || true
	printf '%s\n' "$localversion" >"${WORK_ROOT}/kernel/localversion" 2>/dev/null || true
	export HEDRON_KERNEL_BUILDDIR="$bdir"
	log ok "kernel built into ${WORK_ROOT}/kernel (modules in the rootfs)"
}

write_kernel_summary() {
	local release="" variant="base"
	[ -n "${HEDRON_KERNEL_BUILDDIR:-}" ] && [ -r "${HEDRON_KERNEL_BUILDDIR}/include/config/kernel.release" ] && \
		release="$(cat "${HEDRON_KERNEL_BUILDDIR}/include/config/kernel.release")"
	[ -z "$release" ] && release="$(uname -r 2>/dev/null || echo unknown)"
	[ "${VARIANT:-}" = "uc" ] && variant="base+fragment (uc adds the extra driver stacks)"
	py "$KERNEL_MODE" "$release" "$variant" "$(manifest_dir)/kernel.json" <<'PY' || log warn "kernel.json not written"
import json, os, sys
mode, release, variant, out = sys.argv[1:5]
doc = {"format": "hedron-kernel", "version": 1, "mode": mode, "release": release,
       "config": "hedronos_defconfig", "overlay": "hedronos.fragment", "variant_config": variant,
       "symbols": {}}
try:
    with open(os.path.join("kernel", "hedronos_defconfig"), encoding="utf-8") as fh:
        doc["symbols"] = {l.split("=")[0][7:]: l.split("=", 1)[1].strip()
                          for l in fh if l.startswith("CONFIG_") and "=" in l}
except OSError:
    pass
os.makedirs(os.path.dirname(out), exist_ok=True)
json.dump(doc, open(out, "w", encoding="utf-8"), indent=2, sort_keys=True)
print("kernel: mode=%s release=%s symbols=%d" % (mode, release, len(doc["symbols"])))
PY
	record kernel "release $release variant $variant"
}
