# HedronOS build library — boot entries and the ESP tree. SPDX-License-Identifier: MIT
#
# The boot menu is a product surface: it is where "boot with the NVIDIA driver", "boot into the
# fallback scheduler" and "revert the last update" have to exist before the machine breaks, not
# after. tools/hedron-boot-entries.py generates the loader entries (and a GRUB fallback) from
# data/boot-entries.toml, so the same source produces both bootloader flavours.
stage_bootloader() {
	local esp="${WORK_ROOT}/efiboot" entries="$(manifest_dir)/boot-entries"
	mkdir -p "$esp/EFI/BOOT" "$esp/loader/entries" "$entries" 2>/dev/null || true

	run_local python3 "$HEDRON_SRC/tools/hedron-boot-entries.py" \
		--spec "$HEDRON_SRC/data/boot-entries.toml" \
		--outdir "$esp/loader/entries" \
		--loader-conf "$esp/loader/loader.conf" \
		--grub-out "$entries/grub.cfg" \
		--version "$OS_VERSION" --codename "$OS_CODENAME" --profile "$PROFILE" --variant "$VARIANT" \
		|| die "hedron-boot-entries.py failed"

	local count; count="$(ls -1 "$esp/loader/entries" 2>/dev/null | wc -l)"
	log info "boot entries: $count written to $esp/loader/entries"
	record bootloader "entries ${count}"

	# The EFI binary: copied from the host's systemd-boot when present, otherwise the build says so
	# loudly instead of producing an image that cannot boot.
	local src=""
	for cand in /usr/lib/systemd/boot/efi/systemd-bootx64.efi /usr/share/systemd/boot/efi/systemd-bootx64.efi \
		"${WORK_ROOT}/rootfs/usr/lib/systemd/boot/efi/systemd-bootx64.efi"; do
		[ -f "$cand" ] && { src="$cand"; break; }
	done
	if [ -n "$src" ]; then
		install_file "$src" "$esp/EFI/BOOT/BOOTX64.EFI" 0644 || true
		record bootloader "systemd-boot ${src}"
	else
		warn "no systemd-bootx64.efi on this host — the ISO will not be UEFI-bootable until you install systemd-boot's package"
		[ "${DRY_RUN:-0}" = "1" ] || die "install the 'systemd-boot' provider (package systemd-libs) or ship a --efi-blob"
		# The rehearsal puts a clearly-labelled placeholder at the *real* path, so the image layout
		# check downstream is checking the layout and not an artefact of the dry run.
		dryrun_placeholder "$esp/EFI/BOOT/BOOTX64.EFI" || true
	fi

	# Kernel + initramfs into the ESP tree. A `--kernel host` rehearsal borrows what is booted now.
	local vmlinuz="${WORK_ROOT}/kernel/vmlinuz-linux-hedron" init="${WORK_ROOT}/kernel/initramfs-linux-hedron.img"
	if [ ! -f "$vmlinuz" ]; then
		for cand in /boot/vmlinuz-linux /boot/vmlinuz-linux-lts /boot/vmlinuz-linux-hedron; do
			[ -f "$cand" ] && { vmlinuz="$cand"; init="$(printf '%s' "$cand" | sed 's/vmlinuz/initramfs/')"; break; }
		done
	fi
	if [ -f "$vmlinuz" ]; then
		install_file "$vmlinuz" "${WORK_ROOT}/iso/boot/vmlinuz-linux-hedron" 0644 || true
		[ -f "$init" ] && install_file "$init" "${WORK_ROOT}/iso/boot/initramfs-linux-hedron.img" 0644 || true
		record bootloader "kernel $(basename "$vmlinuz")"
	else
		warn "no kernel image to place in the ISO (build with --kernel auto, or pass a booted kernel)"
		mkdir -p "${WORK_ROOT}/iso/boot" 2>/dev/null || true
		dryrun_placeholder "${WORK_ROOT}/iso/boot/vmlinuz-linux-hedron" || true
		dryrun_placeholder "${WORK_ROOT}/iso/boot/initramfs-linux-hedron.img" || true
	fi
	mkdir -p "${WORK_ROOT}/iso/EFI/BOOT" "${WORK_ROOT}/iso/boot" 2>/dev/null || true
	# cp, not copy_tree: both sides are inside WORK_ROOT, and the ISO stage reads this tree back to
	# check the layout promise. A logged-but-skipped copy here would make the rehearsal check the
	# empty directory instead of the image.
	cp -a "${esp}/EFI/." "${WORK_ROOT}/iso/EFI/" 2>/dev/null || log warn "could not stage the EFI tree"
	cp -a "${esp}/loader/." "${WORK_ROOT}/iso/boot/loader/" 2>/dev/null || log warn "could not stage the loader tree"
	log ok "bootloader tree ready"
}
