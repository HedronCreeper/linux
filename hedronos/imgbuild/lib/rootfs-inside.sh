#!/usr/bin/env bash
# Runs *inside* the archlinux container when the host is not Arch. SPDX-License-Identifier: MIT
#
#   podman run --rm --privileged -v $PWD:/src -v $PWD/work:/work archlinux:latest \
#       /src/imgbuild/lib/rootfs-inside.sh /work/rootfs /work/manifest/packages.txt
#
# Deliberately tiny: install the two packages the host build normally already has, then do the
# pacstrap the host would have done. Everything else (HedronOS layer, profile choices) is applied
# by rootfs.sh on the host afterwards, because it only needs files, not a chroot.
set -Eeuo pipefail
ROOT="${1:?usage: rootfs-inside.sh <rootfs-dir> <package-list-file>}"
PKGS="${2:?usage: rootfs-inside.sh <rootfs-dir> <package-list-file>}"
[ "$(id -u)" = "0" ] || { echo "run as root (the container is already --privileged)" >&2; exit 1; }
pacman -Sy --noconfirm --noprogressbar arch-install-scripts squashfs-tools libisofs mtools dosfstools >/dev/null
[ -r "$PKGS" ] || { echo "no package list at $PKGS" >&2; exit 1; }
mapfile -t NAMES < <(grep -E '^[a-z0-9@._+-]+$' "$PKGS" | sort -u)
[ ${#NAMES[@]} -gt 0 ] || { echo "package list is empty" >&2; exit 1; }
echo "pacstrap: ${#NAMES[@]} packages into $ROOT"
pacstrap -c -K -M "$ROOT" "${NAMES[@]}"
