#!/usr/bin/env bash
# HedronOS image build entry point.
# SPDX-License-Identifier: MIT
#
# One script, one ISO. Designed to be run by a human on their own machine, with no
# build server, no containers required (though podman/docker are used automatically
# when the host is not Arch), and a --dry-run mode that performs every decision the
# real build performs while touching nothing outside work/.
#
#   ./build.sh --profile gamerte                 full build
#   ./build.sh --dry-run --stage all             decision-complete rehearsal (no root)
#   ./build.sh --profile handheld --sign         + minisign signature & sha256
set -Eeuo pipefail

HEDRON_BUILD_SH=1
VERSION_FILE="VERSION"

# ---------------------------------------------------------------- identity
OS_VERSION="0.0.0"
OS_CODENAME="Halcyon"
if [ -f "$VERSION_FILE" ]; then
	OS_VERSION="$(head -n1 "$VERSION_FILE" | tr -d '[:space:]')"
	OS_CODENAME="$(sed -n 's/^codename=//p' "$VERSION_FILE" | head -n1)"
fi
OS_NAME="HedronOS"

usage() {
	cat <<EOF
$OS_NAME $OS_VERSION "$OS_CODENAME" image builder

USAGE
  ./build.sh [options]

PROFILE / TARGET
  --profile NAME        gamerte | desktop | handheld | studio | core   (default: gamerte)
  --arch NAME           x86_64 | aarch64                               (default: x86_64)
  --variant NAME        dvd | net | uc                                (default: dvd)
                          dvd  everything pre-cached, install fully offline
                          net  small image, packages downloaded during install
                          uc   ultra-capable: kernels + all GPU stacks + sdk

BUILD SOURCES
  --kernel MODE         auto | <path-to-kernel-tree> | host | pkg:<name>
                          auto: build linux-hedron from the kernel tree this repo holds
                          host: reuse the running kernel (fast rehearsal)
  --repo URL            pacman repository to install from (default: Arch mirrors)
  --offline DIR         use DIR as the package pool and never touch the network
  --allow-offline       do not fail when the network is unreachable

STAGES (default: all)
  --stage NAME          preflight|gpu-db|kernel|packages|rootfs|drivers-preseed|
                        bootloader|iso|verify|vm|sign|clean
  --from NAME           start at stage NAME (reuse earlier results)
  --list-stages

SAFETY / QUALITY
  --dry-run             full decision-making, stub every privileged/external command
  --verify-only         run tools/hedronos-verify and exit with its status
  --boot-test           after building, boot the image in qemu and check for a login prompt
  --no-verify           skip the post-build verification gate (not recommended)

OUTPUT
  --out DIR             artifact directory                       (default: dist)
  --work DIR            scratch directory                        (default: work)
  --sign                sign with minisign/gpg if a key exists, always emit sha256
  --release-notes       write dist/RELEASE-<profile>.md next to the image
  --jobs N              parallel compile jobs                    (default: nproc)
  --label TEXT          extra text stamped into the ISO volume id
  -q, --quiet         | --verbose
  -h, --help            this text

EXAMPLES
  # Day one: build the gaming edition from this very checkout
  ./build.sh --profile gamerte --sign --release-notes

  # Rehearse everything without root, network, or a 40-minute kernel build
  ./build.sh --dry-run --kernel host

  # Just re-run the quality gate over the tree
  ./build.sh --verify-only
EOF
}

die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
[ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ] && { usage; exit 0; }

# ---------------------------------------------------------------- arg parsing
PROFILE="gamerte"
TARGET_ARCH="x86_64"
VARIANT="dvd"
KERNEL_MODE="auto"
REPO_URL=""
OFFLINE_POOL=""
STAGES="all"
STAGE_FROM=""
DRY_RUN=0
VERIFY_ONLY=0
BOOT_TEST=0
DO_VERIFY=1
OUT="dist"
WORK="work"
DO_SIGN=0
RELEASE_NOTES=0
JOBS="$(nproc 2>/dev/null || echo 2)"
LABEL=""
VERBOSE=0

while [ $# -gt 0 ]; do
	case "$1" in
	--profile)         PROFILE="${2:?--profile needs a value}"; shift 2 ;;
	--arch)            TARGET_ARCH="${2:?--arch needs a value}"; shift 2 ;;
	--variant)         VARIANT="${2:?--variant needs a value}"; shift 2 ;;
	--kernel)          KERNEL_MODE="${2:?--kernel needs a value}"; shift 2 ;;
	--repo)            REPO_URL="${2:?--repo needs a value}"; shift 2 ;;
	--offline)         OFFLINE_POOL="${2:?--offline needs a value}"; shift 2 ;;
	--stage)           STAGES="${2:?--stage needs a value}"; shift 2 ;;
	--from)            STAGE_FROM="${2:?--from needs a value}"; shift 2 ;;
	--list-stages)     echo "preflight gpu-db kernel packages rootfs drivers-preseed bootloader iso verify vm sign clean"; exit 0 ;;
	--dry-run)         DRY_RUN=1; shift ;;
	--verify-only)     VERIFY_ONLY=1; shift ;;
	--boot-test)       BOOT_TEST=1; shift ;;
	--no-verify)       DO_VERIFY=0; shift ;;
	--out)             OUT="${2:?--out needs a value}"; shift 2 ;;
	--work)            WORK="${2:?--work needs a value}"; shift 2 ;;
	--sign)            DO_SIGN=1; shift ;;
	--release-notes)   RELEASE_NOTES=1; shift ;;
	--jobs)            JOBS="${2:?--jobs needs a value}"; shift 2 ;;
	--label)           LABEL="${2:?--label needs a value}"; shift 2 ;;
	--allow-offline)   ALLOW_OFFLINE=1; shift ;;
	-q|--quiet)        VERBOSE=0; shift ;;
	--verbose)         VERBOSE=1; shift ;;
	-h|--help)         usage; exit 0 ;;
	--)                shift; break ;;
	*)                 die "unknown argument '$1' (try --help)" ;;
	esac
done

ALLOW_OFFLINE="${ALLOW_OFFLINE:-0}"
case "$PROFILE" in gamerte | desktop | handheld | studio | core) ;; *) die "unknown profile '$PROFILE' — see build/profiles/" ;; esac
case "$VARIANT" in dvd | net | uc) ;; *) die "unknown variant '$VARIANT' (dvd|net|uc)" ;; esac
case "$TARGET_ARCH" in x86_64 | aarch64) ;; *) die "unsupported arch '$TARGET_ARCH'" ;; esac
[ -n "$JOBS" ] && [ "$JOBS" -ge 1 ] 2>/dev/null || die "--jobs must be >= 1"

# ---------------------------------------------------------------- environment
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$HERE"
HEDRON_SRC="$HERE"
export HEDRON_SRC OS_VERSION OS_NAME OS_CODENAME PROFILE VARIANT TARGET_ARCH DRY_RUN VERBOSE JOBS OUT WORK KERNEL_MODE REPO_URL OFFLINE_POOL ALLOW_OFFLINE VARIANT_LABEL="$LABEL"
export HEDRON_KERNEL_ROOT="$(cd "$HERE/.." && pwd -P)"
BUILD_ID="$(date -u +%Y%m%dT%H%M%SZ 2>/dev/null || echo 00000000T000000Z)"
export BUILD_ID
ISO_NAME="hedronos-${OS_VERSION}-${PROFILE}-${TARGET_ARCH}"
case "$VARIANT" in net) ISO_NAME="${ISO_NAME}-net" ;; uc) ISO_NAME="${ISO_NAME}-uc" ;; esac
[ -n "$LABEL" ] && ISO_NAME="${ISO_NAME}-${LABEL// /_}"
export ISO_NAME
ISO_PATH="${OUT}/${ISO_NAME}.iso"

if [ "$DRY_RUN" = "1" ]; then
	WORK_ROOT="${WORK}/dryrun"
	DRYRUN_LOG="${WORK_ROOT}/dry-run.log"
	mkdir -p "$WORK_ROOT"
	: >"$DRYRUN_LOG"
else
	WORK_ROOT="${WORK}/live-${PROFILE}-${TARGET_ARCH}"
fi
export WORK_ROOT DRY_RUN_LOG="$DRYRUN_LOG"
mkdir -p "$WORK_ROOT" "$OUT"

# ---------------------------------------------------------------- libraries
for lib in common preflight pkgset packages drivers rootfs bootloader verify image qemu kernelbuild; do
	[ -r "build/lib/${lib}.sh" ] || die "missing build/lib/${lib}.sh — tree is incomplete (run 'git checkout' fully)"
	# shellcheck source=build/lib/common.sh
	. "./build/lib/${lib}.sh"
done
common_init

# ---------------------------------------------------------------- scheduling
ALL_STAGES="preflight gpu-db kernel packages rootfs drivers-preseed bootloader iso verify vm sign"
STAGE_LIST=()
expand_stages() {
	local s
	case "$STAGES" in
	all)
		for s in $ALL_STAGES; do
			[ "$BOOT_TEST" = "1" ] || { [ "$s" = "vm" ] && continue; }
			STAGE_LIST+=("$s")
		done
		[ "$DRY_RUN" = "1" ] && STAGE_LIST=($(printf '%s\n' "${STAGE_LIST[@]}" | grep -v '^vm$' || true))
		;;
	*)
		for s in $STAGES; do STAGE_LIST+=("$s"); done
		;;
	esac
	[ -n "$STAGE_FROM" ] || return 0
	local seen=0 out=()
	for s in $ALL_STAGES; do
		[ "$s" = "$STAGE_FROM" ] && seen=1
		[ "$seen" = "1" ] && out+=("$s")
	done
	STAGE_LIST=("${out[@]}")
}

run_stage() {
	local s="$1"
	case "$s" in
	preflight)        stage_preflight ;;
	gpu-db)           stage_gpu_db ;;
	kernel)           stage_kernel ;;
	packages)         stage_packages ;;
	rootfs)           stage_rootfs ;;
	drivers-preseed)  stage_drivers_preseed ;;
	bootloader)       stage_bootloader ;;
	iso)              stage_iso ;;
	verify)           [ "$DO_VERIFY" = "1" ] && stage_verify; return 0 ;;
	vm)               [ "$BOOT_TEST" = "1" ] && stage_vm; return 0 ;;
	sign)             [ "$DO_SIGN" = "1" ] && stage_sign; return 0 ;;
	clean)            stage_clean ;;
	*)                die "no implementation for stage '$s'" ;;
	esac
}

stage_clean() {
	log info "removing ${WORK_ROOT} (dry-run=${DRY_RUN})"
	[ "$DRY_RUN" = "1" ] || rm -rf -- "$WORK_ROOT"
}

# ---------------------------------------------------------------- main
main() {
	local banner="building ${OS_NAME} ${OS_VERSION} '${OS_CODENAME}' profile=${PROFILE} variant=${VARIANT} arch=${TARGET_ARCH} build=${BUILD_ID}"
	log info "$banner"
	expand_stages
	[ ${#STAGE_LIST[@]} -gt 0 ] || die "nothing to do (stage list empty)"
	log info "stages: ${STAGE_LIST[*]}"

	local started="$SECONDS" s
	for s in "${STAGE_LIST[@]}"; do
		stage_begin "$s"
		run_stage "$s" || stage_fail "$s"
		stage_end "$s"
	done

	if [ "$RELEASE_NOTES" = "1" ]; then
		write_release_notes
	fi

	local wall=$((SECONDS - started))
	log info "done in ${wall}s"
	if [ "$DRY_RUN" = "1" ]; then
		log info "dry-run: no privileged command was executed; see ${DRY_RUN_LOG:-work/dry-run.log}"
	else
		log ok "${ISO_PATH}"
		[ -f "${ISO_PATH}.sha256" ] && log ok "${ISO_PATH}.sha256"
		[ -f "${ISO_PATH}.minisig" ] && log ok "${ISO_PATH}.minisig"
		log info "next: dd if=${ISO_PATH} of=/dev/sdX bs=16M status=progress   (or 'sudo hedron-usbwriter')"
	fi
}

write_release_notes() {
	local notes="${OUT}/RELEASE-${PROFILE}.md"
	mkdir -p "$OUT"
	{
		echo "# HedronOS ${OS_VERSION} '${OS_CODENAME}' — ${PROFILE} ${VARIANT}"
		echo
		echo "* build id: \`${BUILD_ID}\`"
		echo "* profile: \`${PROFILE}\`  arch: \`${TARGET_ARCH}\`  kernel: \`${KERNEL_MODE}\`"
		echo "* generated by \`build.sh\` in \`hedronos/\` of the kernel checkout"
		echo
		echo "## Install list"
		echo
		echo '```'
		[ -f "${WORK_ROOT}/manifest/packages.txt" ] && cat "${WORK_ROOT}/manifest/packages.txt" || echo "(manifest not produced in this run)"
		echo '```'
		echo
		echo "## GPU plans baked into the image"
	echo
		echo '```'
		[ -f "${WORK_ROOT}/manifest/gpu-plans-summary.txt" ] && cat "${WORK_ROOT}/manifest/gpu-plans-summary.txt" || echo "(none)"
		echo '```'
	} >"$notes"
	log info "release notes: ${notes}"
}

if [ "$VERIFY_ONLY" = "1" ]; then
	require_cmd python3
	exec python3 tools/hedronos-verify --root . --kernel-root "$HEDRON_KERNEL_ROOT" --report "${OUT}/verification-report.json"
fi

main "$@"
