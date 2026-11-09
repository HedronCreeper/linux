# HedronOS build library — GPU detection data and the driver preseed. SPDX-License-Identifier: MIT
#
# This is the part that makes HedronOS a gaming distro rather than a gaming-flavoured one: the PCI
# identity database, the per-device driver plans, and (for the `dvd` variant) the actual driver
# packages inside the image, so the first boot needs no network to reach a GPU.

stage_gpu_db() {
	local out="$HEDRON_SRC/data/gpu-db"
	mkdir -p "$out" 2>/dev/null || true
	log info "regenerating the GPU database from the kernel tree"
	# run_local: gen-gpu-db only reads the kernel tree and writes hedronos/data/. A rehearsal that
	# skipped it would not be rehearsing the thing most likely to break (the database is the input
	# to the driver plans, the installer, hedron-drivers and the verifier).
	run_local python3 "$HEDRON_SRC/tools/gen-gpu-db.py" --kernel "$HEDRON_KERNEL_ROOT" --out "$out" \
		|| die "gen-gpu-db.py failed — the database is the input to every later decision"
	run_local python3 "$HEDRON_SRC/tools/gen-gpu-db.py" --kernel "$HEDRON_KERNEL_ROOT" --summary \
		| tee -a "$(manifest_dir)/gpu-db-summary.txt" || true

	# The generator writes ${out}/gpu-db.sha256 itself, in the format the verifier expects.
	# Re-hashing it here with sha256sum would be a second, subtly different producer of the same
	# file — which is exactly how "regeneration is byte-identical" starts failing for the wrong
	# reason (one side quotes the path, the other the basename).
	[ -f "${out}/gpu-db.sha256" ] || warn "no gpu-db.sha256 next to the database (generator should have written it)"
	record gpu-db "generated from ${HEDRON_KERNEL_ROOT}"
}

stage_drivers_preseed() {
	local plans="$(manifest_dir)/gpu-plans.json" summary="$(manifest_dir)/gpu-plans-summary.txt"
	log info "computing the driver plan for every GPU we know about"
	run_local python3 "$HEDRON_SRC/tools/hedron-gpu-plans.py" --db "$HEDRON_SRC/data/gpu-db/gpu-db.json" \
		--rules "$HEDRON_SRC/data/driver-rules.toml" --out "$plans" --summary "$summary" \
		--version "$OS_VERSION" --build-id "$BUILD_ID" || die "hedron-gpu-plans.py failed"
	[ -r "$summary" ] && log info "plans: $(head -1 "$summary" 2>/dev/null)"

	local rootfs="${WORK_ROOT}/rootfs"
	# Plain cp, not install_file: these land inside WORK_ROOT, where the dry-run guard does not
	# apply, and the ISO stage squashes this directory — an empty etc/hedron in the rehearsal would
	# mean the image we inspect is not the image we would ship.
	mkdir -p "${rootfs}/etc/hedron" 2>/dev/null || true
	local pair src dst
	for pair in "gpu-plans.json:$plans" "gpu-db.json:$HEDRON_SRC/data/gpu-db/gpu-db.json" \
		"driver-rules.toml:$HEDRON_SRC/data/driver-rules.toml"; do
		src="${pair#*:}"; dst="${rootfs}/etc/hedron/${pair%%:*}"
		[ -f "$src" ] && cp "$src" "$dst" 2>/dev/null || log warn "could not stage ${pair%%:*} into the rootfs"
	done

	# The preseed itself: which vendor packages to put in the image so graphics works offline.
	local want=()
	case "${HEDRON_P_precache_drivers:-true}" in
	true | yes | 1) ;;
	*) log info "profile says precache_drivers=false — skipping the driver cache"; return 0 ;;
	esac
	if [ -r "$plans" ]; then
		mapfile -t want < <(py "$plans" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1], encoding="utf-8"))
seen = []


def name(value):
    # A plan entry is either a bare package name or {"name": ..., "action": ...} depending on how
    # the rule was written; both are legitimate in data/driver-rules.toml.
    if isinstance(value, dict):
        return value.get("name") or value.get("package") or ""
    return value or ""


for plan in doc.get("plans", []):
    for pkg in list(plan.get("packages", []) or []) + list(plan.get("firmware_packages", []) or []):
        pkg = name(pkg)
        if pkg and pkg not in seen:
            seen.append(pkg)
print("\n".join(seen))
PY
)
	fi
	if [ "${#want[@]}" = "0" ]; then
		want=(mesa vulkan-radeon vulkan-intel lib32-mesa lib32-vulkan-radeon lib32-vulkan-intel
			opencl-mesa vulkan-nouveau nvidia-dkms nvidia-utils lib32-nvidia-utils nvidia-settings
			linux-firmware)
		log info "no per-device packages in the plans: using the standing driver cache list"
	fi
	log info "driver preseed: ${#want[@]} package(s) to cache in the image"
	record drivers-preseed "want ${want[*]}"

	# hedronos-verify's iso/rehearsal check reads exactly this file: the offline GPU promise has to
	# be a list of package names on disk, not a line in a log.
	local prefetch; prefetch="$(manifest_dir)/driver-prefetch.txt"
	printf '%s\n' "${want[@]}" | sort -u >"$prefetch" 2>/dev/null \
		|| { log warn "could not write $prefetch"; : >"$prefetch"; printf '%s\n' "${want[@]}" | sort -u >"$prefetch" 2>/dev/null || true; }
	record drivers-preseed "prefetch $(grep -c . "$prefetch" 2>/dev/null || echo 0) package(s)"

	local pool="${WORK_ROOT}/pool"
	mkdir -p "$pool" 2>/dev/null || true
	if [ -n "${OFFLINE_POOL:-}" ]; then
		log info "offline pool: copying the driver packages from ${OFFLINE_POOL}"
		local pkg
		for pkg in "${want[@]}"; do
			find "$OFFLINE_POOL" -maxdepth 1 -name "${pkg}-*.pkg.tar.*" -exec cp -a -t "$pool" {} + 2>/dev/null || true
		done
		record drivers-preseed "pool $(ls -1 "$pool" 2>/dev/null | wc -l) file(s) copied from ${OFFLINE_POOL}"
	elif have pacman; then
		run "$(sudoify)" pacman -Sw --noconfirm --dbpath "${WORK_ROOT}/pacman-db" --cachedir "$pool" "${want[@]}" \
			|| log warn "driver download failed (network or mirror) — the image will fetch them at install time"
	else
		record drivers-preseed "download skipped (no pacman on this host)"
		log info "no pacman here: the ISO will download drivers during install instead of pre-caching them"
	fi
	export HEDRON_DRIVER_POOL="$pool"
}

hedron_mirrorlist() {
	# One place that answers "which mirror list do we hand to pacstrap", so a build in a country
	# with a good local mirror does not fetch a 4 GB package pool from another continent.
	local list="${WORK_ROOT}/mirrorlist"
	{
		printf '# generated by hedronos build.sh at %s\n' "$BUILD_ID"
		if [ -r "$HEDRON_SRC/data/mirrors.toml" ]; then
			py "$HEDRON_SRC/data/mirrors.toml" <<'PY'
import sys
from hedronlib.util import load_toml
doc = load_toml(sys.argv[1])
mirrors = doc.get("mirror") or doc.get("mirrors") or []
if isinstance(mirrors, dict):
    mirrors = [{"url": u} for u in mirrors.get("server", [])]
for entry in list(mirrors)[:8]:
    url = entry.get("url") if isinstance(entry, dict) else entry
    if url:
        print("Server = %s/os/$arch" % url)
PY
		fi
	} >"$list" 2>/dev/null || true
	if ! grep -q '^Server = ' "$list" 2>/dev/null; then
		printf 'Server = https://geo.mirror.pkgbuild.com/$repo/os/$arch\n' >"$list" 2>/dev/null || true
	fi
	printf '%s' "$list"
}
