# HedronOS build library — squashfs, the ISO, and signing. SPDX-License-Identifier: MIT
#
# Layout (everything the later tooling is allowed to assume):
#   /EFI/BOOT/BOOTX64.EFI            UEFI fallback loader
#   /boot/loader/entries/*.conf      systemd-boot entries (also read by the installer)
#   /boot/grub/grub.cfg              fallback menu for firmware that will not do systemd-boot
#   /boot/vmlinuz-linux-hedron       the kernel
#   /boot/initramfs-linux-hedron.img the early userspace (with the hedron-early hook inside)
#   /hedron/airootfs.sfs             the whole live system, compressed
#   /hedron/manifest.json            what went in: package list, driver prefetch, GPU plans, hashes
#
# The live system is a squashfs of the same directory the installer will copy, which is why the
# rootfs stage writes its config into WORK_ROOT/rootfs and not into the ISO.

# dryrun_placeholder <path> : a rehearsal writes the file names the layout promises, with content
# that says what it is. Without this, `--dry-run` "verifies" a tree that is missing its kernel and
# its squashfs, and the layout check would only ever be exercised by accident. A real build never
# calls this, so a placeholder can never end up on a released image.
dryrun_placeholder() {
	local path="$1"
	[ "${DRY_RUN:-0}" = "1" ] || return 1
	mkdir -p "$(dirname -- "$path")" 2>/dev/null || true
	printf 'HEDRONOS DRY-RUN PLACEHOLDER\nnot a real artifact — produced by build.sh --dry-run at %s\n' "$BUILD_ID" \
		>"$path" 2>/dev/null || return 1
	record placeholder "${path#"$WORK_ROOT"/}"
}

stage_iso() {
	# Two `local` lines: in a single declaration `local stage=… squash=${stage}/…` the second name
	# reads the *global* stage (already declared, still unset) — under `set -u` that is an error.
	local stage="${WORK_ROOT}/iso"
	local squash="${stage}/hedron/airootfs.sfs"
	mkdir -p "${stage}/hedron" 2>/dev/null || true
	local rootfs="${WORK_ROOT}/rootfs"
	[ -d "$rootfs" ] || die "no rootfs at $rootfs — run the rootfs stage first (--from rootfs)"

	# The manifest first: the squashfs hash is only meaningful if it is recorded *and* shipped.
	write_iso_manifest "$stage"

	log info "squashing the live rootfs"
	local comp="-comp zstd -Xcompression-level 19"
	have mksquashfs || [ "${DRY_RUN:-0}" = "1" ] || die "mksquashfs is missing (package squashfs-tools)"
	if have mksquashfs; then
		run mksquashfs "$rootfs" "$squash" $comp -noappend -b 1048576 -processors "$JOBS" \
			|| die "mksquashfs failed"
	else
		record iso "mksquashfs $rootfs $squash $comp -noappend -b 1048576 -processors $JOBS"
		dryrun_placeholder "$squash" || true
	fi
	local sq_size=0
	[ -f "$squash" ] && sq_size="$(stat -c %s "$squash" 2>/dev/null || stat -f %z "$squash" 2>/dev/null || echo 0)"
	log info "airootfs.sfs: $(human_size "$sq_size")"

	log info "writing ${ISO_PATH}"
	mkdir -p "$(dirname -- "$ISO_PATH")" 2>/dev/null || true
	if have xorriso; then
		local args=( -as mkisofs -o "$ISO_PATH" -V "${OS_NAME:0:26}_${OS_VERSION}" -iso-level 3 -r -J
			-input-charset UTF-8 -no-emul-boot -boot-load-size 4 -partition_offset 16
			-efi-boot "EFI/BOOT/EFI.IMG" )
		[ -f "${stage}/EFI/BOOT/EFI.IMG" ] || build_efi_image "$stage"
		[ -f "${stage}/boot/grub/eltorito.img" ] && args+=( -b boot/grub/eltorito.img -boot-info-table )
		args+=( "$stage" )
		run xorriso "${args[@]}" || die "xorriso failed to write the image"
	else
		record iso "xorriso -as mkisofs -o $ISO_PATH -V ${OS_NAME}_${OS_VERSION} -e EFI/BOOT/EFI.IMG $stage"
		warn "no xorriso on this host: the ISO assembly is recorded, not executed"
		[ "${DRY_RUN:-0}" = "1" ] || die "install libisofs (provides xorriso) to produce an image"
	fi

	if [ -f "$ISO_PATH" ]; then
		verify_image_structure "$ISO_PATH" || die "the image we just wrote does not match the layout we promised"
		local sz; sz="$(stat -c %s "$ISO_PATH" 2>/dev/null || echo 0)"
		log ok "$(basename "$ISO_PATH") ($(human_size "$sz"))"
		record iso "image $(basename "$ISO_PATH") ${sz}"
		( cd "$(dirname -- "$ISO_PATH")" && sha256sum "$(basename -- "$ISO_PATH")" >"$(basename -- "$ISO_PATH").sha256" ) \
			2>/dev/null || log warn "could not write the sha256 sidecar"
	else
		verify_image_structure "$stage" || die "the staged tree does not match the layout we promised"
		log ok "staged tree is structurally correct (no .iso written in a dry run)"
	fi
}

write_iso_manifest() {
	local stage="$1"
	local mf="$(manifest_dir)/iso-manifest.json"
	mkdir -p "$(dirname -- "$mf")" 2>/dev/null || true
	py "$WORK_ROOT" "$HEDRON_SRC" "$OS_VERSION" "$PROFILE" "$VARIANT" "$BUILD_ID" "$mf" "${stage}/hedron/manifest.json" <<'PY' || log warn "iso manifest skipped"
import hashlib, json, os, sys, time
work, src, version, profile, variant, build_id, out, shipped = sys.argv[1:9]
man = os.path.join(work, "manifest")

def sha(path):
    if not os.path.isfile(path):
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()

def lines(name):
    path = os.path.join(man, name)
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [l.strip() for l in fh if l.strip() and not l.startswith("#")]

doc = {
    "format": "hedron-iso-manifest",
    "version": 1,
    "os": {"name": "HedronOS", "version": version, "profile": profile, "variant": variant,
           "build_id": build_id, "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
    "packages": lines("packages.txt"),
    "driver_prefetch": lines("driver-prefetch.txt"),
    "sha256": {},
    "sizes_bytes": {},
}
for name in ("gpu-plans.json", "packages.txt", "driver-prefetch.txt"):
    p = os.path.join(man, name)
    if os.path.isfile(p):
        doc["sha256"][name] = sha(p)
        doc["sizes_bytes"][name] = os.path.getsize(p)
sfs = os.path.join(work, "iso", "hedron", "airootfs.sfs")
if os.path.isfile(sfs):
    doc["sha256"]["hedron/airootfs.sfs"] = sha(sfs)
    doc["sizes_bytes"]["hedron/airootfs.sfs"] = os.path.getsize(sfs)
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, indent=2, sort_keys=True)
    fh.write("\n")
print("iso manifest: %d packages, %d driver package(s)" % (len(doc["packages"]), len(doc["driver_prefetch"])))
PY
	install_file "$mf" "${stage}/hedron/manifest.json" 0644 2>/dev/null || true
	# A dry run writes no files through install_file, and the manifest is the one artifact the
	# rehearsal is judged on, so copy it into the staged tree directly (inside WORK_ROOT).
	[ "${DRY_RUN:-0}" = "1" ] && [ -f "$mf" ] && cp "$mf" "${stage}/hedron/manifest.json" 2>/dev/null || true
}

# verify_image_structure <path-to-iso-or-staged-tree> : the layout promise, enforced.
verify_image_structure() {
	local target="$1"
	local list=""
	if [ -d "$target" ]; then
		list="$(cd "$target" && find . -type f | sed 's#^\./##')"
	elif have isoinfo; then
		list="$(isoinfo -f -i "$target" 2>/dev/null | sed 's#^/##')"
	elif have 7z; then
		list="$(7z l -slt "$target" 2>/dev/null | awk -F' = ' '/^Path = /{print $2}')"
	else
		log warn "no isoinfo/7z: cannot inspect the image contents"
		return 0
	fi
	local missing=() want
	for want in EFI/BOOT/BOOTX64.EFI boot/vmlinuz-linux-hedron hedron/airootfs.sfs; do
		printf '%s\n' "$list" | grep -qxF "$want" || missing+=("$want")
	done
	if [ ${#missing[@]} -gt 0 ]; then
		log error "image is missing: ${missing[*]}"
		log error "expected /EFI/BOOT/BOOTX64.EFI, /boot/vmlinuz-linux-hedron, /hedron/airootfs.sfs"
		return 1
	fi
	printf '%s\n' "$list" | grep -q "boot/loader/entries/" || log warn "no systemd-boot entries in the image"
	printf '%s\n' "$list" | grep -q "hedron/manifest.json" || log warn "no /hedron/manifest.json in the image"
	log ok "image structure verified (3 required paths present)"
	return 0
}

build_efi_image() {
	local stage="$1" esp="${stage}/EFI/BOOT/EFI.IMG"
	mkdir -p "${stage}/EFI/BOOT" 2>/dev/null || true
	if ! have mkfs.fat || ! have mcopy; then
		record efi "mtools+dosfstools needed to build the ESP image (EFI/BOOT/EFI.IMG)"
		return 0
	fi
	local size_kb=8192
	run rm -f "$esp"
	run dd if=/dev/zero of="$esp" bs=1024 count="$size_kb" status=none
	run mkfs.fat -n HEDRON_EFI "$esp"
	local payload="${WORK_ROOT}/esp-payload"
	rm -rf "$payload" 2>/dev/null || true
	mkdir -p "${payload}/EFI/HEDRON" "${payload}/loader/entries" 2>/dev/null || true
	cp -a "${stage}/EFI/BOOT/BOOTX64.EFI" "${payload}/EFI/BOOT/" 2>/dev/null || true
	[ -d "${stage}/boot/loader/entries" ] && cp -a "${stage}/boot/loader/entries/." "${payload}/loader/entries/" 2>/dev/null
	[ -f "${stage}/boot/loader/loader.conf" ] && cp "${stage}/boot/loader/loader.conf" "${payload}/loader/" 2>/dev/null
	[ -f "${stage}/boot/vmlinuz-linux-hedron" ] && cp "${stage}/boot/vmlinuz-linux-hedron" "${payload}/EFI/HEDRON/" 2>/dev/null
	[ -f "${stage}/boot/initramfs-linux-hedron.img" ] && cp "${stage}/boot/initramfs-linux-hedron.img" "${payload}/EFI/HEDRON/" 2>/dev/null
	run mcopy -s -i "$esp" "${payload}/*::" || log warn "could not populate the ESP image"
	rm -rf "$payload" 2>/dev/null || true
	record efi "EFI/BOOT/EFI.IMG ${size_kb}KiB FAT"
}

stage_sign() {
	# sha256 always; a signature only if a key exists — an unsigned-but-hashed image is honest,
	# a signed image from a key nobody controls is the thing we must never accidentally ship.
	local target="$ISO_PATH"
	if [ -f "$ISO_PATH" ]; then
		( cd "$(dirname -- "$ISO_PATH")" && sha256sum "$(basename -- "$ISO_PATH")" >"$(basename -- "$ISO_PATH").sha256" ) \
			|| die "could not write the sha256 file"
		log ok "sha256: ${ISO_PATH}.sha256"
		record sign "sha256 ${ISO_PATH}.sha256"
	else
		log info "no image to hash (dry run): the sha256 sidecar would name ${ISO_PATH}"
		record sign "sha256 planned for ${ISO_PATH}"
	fi
	local minisig=""
	[ -n "${MINISIGN_KEY:-}" ] && minisig="$MINISIGN_KEY"
	[ -z "$minisig" ] && [ -f "$HOME/.minisign/minisign.key" ] && minisig="$HOME/.minisign/minisign.key"
	if [ -n "$minisig" ] && have minisign && [ -f "$ISO_PATH" ]; then
		run minisign -S -m "$target" -x "${ISO_PATH}.minisig" -s "$minisig" || die "minisign failed"
		log ok "minisign signature: ${ISO_PATH}.minisig"
	else
		warn "no minisign key: shipping the image with a sha256 only"
		record sign "minisign skipped (set MINISIGN_KEY to sign)"
	fi
	if have gpg && [ -n "${GPG_KEY_ID:-}" ] && [ -f "$ISO_PATH" ]; then
		run gpg --local-user "$GPG_KEY_ID" --detach-sign --output "${ISO_PATH}.sig" "$target" || log warn "gpg signing failed"
	fi
}
