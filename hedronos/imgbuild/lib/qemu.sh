# HedronOS build library — boot test. SPDX-License-Identifier: MIT
#
# `--boot-test` answers the only question an ISO build really asks: does it boot to a login prompt?
# OVMF is used for UEFI when available (our image is UEFI-first), SeaBIOS otherwise.
stage_vm() {
	local iso="$ISO_PATH"
	[ -f "$iso" ] || { log warn "no ${iso} to boot — skipping the VM test"; return 0; }
	have qemu-system-x86_64 || die "qemu-system-x86_64 is not installed (package qemu)"
	local logf="${WORK_ROOT}/vm-boot.log" timeout="${VM_TIMEOUT:-180}"
	local mem="${VM_MEM:-4096}"
	local cpus=2
	[ "${JOBS:-2}" -lt 4 ] || cpus=4
	local args=( -m "$mem" -smp "$cpus" -cdrom "$iso" -boot d -display none
		-serial "file:${logf}" -monitor none -net none )
	local ovmf=""
	for cand in /usr/share/edk2-ovmf/x64/OVMF_CODE.fd /usr/share/ovmf/x64/OVMF_CODE.fd \
		/usr/share/OVMF/OVMF_CODE.fd /usr/share/edk2/ovmf/OVMF_CODE.fd; do
		[ -f "$cand" ] && { ovmf="$cand"; break; }
	done
	if [ -n "$ovmf" ]; then
		local vars="${WORK_ROOT}/OVMF_VARS.fd"
		cp "$(dirname "$ovmf")/OVMF_VARS.fd" "$vars" 2>/dev/null || die "missing OVMF_VARS.fd next to $ovmf"
		args+=( -drive "if=pflash,format=raw,readonly=on,file=$ovmf" -drive "if=pflash,format=raw,file=$vars" )
		log info "booting in qemu with UEFI ($ovmf)"
	else
		log info "booting in qemu with SeaBIOS (no OVMF found — the image is UEFI-first, expect a slow path)"
	fi
	log info "waiting up to ${timeout}s for a login prompt (log: $logf)"
	if have timeout; then
		run timeout "$timeout" qemu-system-x86_64 "${args[@]}" || log warn "qemu exited non-zero (a timeout here is normal if boot is slow)"
	else
		run qemu-system-x86_64 "${args[@]}" &
		local pid=$!
		sleep "$timeout" 2>/dev/null || true
		kill "$pid" 2>/dev/null || true
	fi
	local ok=0
	for pat in 'login:' 'HedronOS Live' 'systemd\[1\]: Reached target'; do
		grep -q "$pat" "$logf" 2>/dev/null && ok=1
	done
	if [ "$ok" = "1" ]; then
		log ok "the image booted: $(grep -m1 -E 'login:|Reached target' "$logf" 2>/dev/null | tr -d '\r')"
		record vm "boot ok"
		return 0
	fi
	log error "no login prompt in ${logf}; last lines:"
	tail -n 20 "$logf" 2>/dev/null | sed 's/^/    /' >&2 || true
	record vm "boot FAILED"
	[ "${VM_ALLOW_FAIL:-0}" = "1" ] && return 0
	return 1
}
