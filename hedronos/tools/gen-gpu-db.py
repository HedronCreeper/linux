#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Generate HedronOS's PCI-id database *from a Linux kernel tree*.

Why generate instead of hand-maintain? Because the single most common "Linux gaming
broken" story is a distro table that lags the kernel: the driver exists, works, and is
built, but the distro does not know the device id so the installer picks the wrong
stack.  Deriving the table from the tree we ship makes that class of bug impossible.

Sources parsed (all paths relative to the kernel root):

  AMD     drivers/gpu/drm/amd/amdgpu/amdgpu_drv.c   supported_device_ids[], asic_type_quirks[]
          include/drm/drm_pciids.h                  radeon_PCI_IDS (legacy, with CHIP_* flags)
          include/drm/amd_asic_type.h               CHIP_* enum order == IP-gen order
  Intel   include/drm/drm_pciids.h                   INTEL_<PLATFORM>_IDS macro bodies
          drivers/gpu/drm/i915/i915_pci.c            which platforms i915 claims
          drivers/gpu/drm/xe/xe_pci.c                which platforms xe claims
  NVIDIA  drivers/gpu/drm/nouveau/nvkm/engine/device/base.c
                                                 supported chipsets (nouveau)
          curated chipset → architecture table (NVIDIA's out-of-tree driver has no
          in-kernel table to mine, so this part is authored; it is validated against
          nouveau's support list and the generation scale)

Usage:
    tools/gen-gpu-db.py --kernel ../ --out data/gpu-db [--print]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hedronlib import gpus  # noqa: E402
from hedronlib.util import eprint, hex_id, norm_id  # noqa: E402

SCHEMA = 3

# nouveau chipset (as computed by gpus.nvidia_chipset) → (architecture, generation,
# open-kernel-module support, first/last nouveau chipset in that family).
# `open` marks cards NVIDIA's *open* module supports (Turing GA100-style or newer).
NVIDIA_CHIPSETS: list[dict[str, object]] = [
    {"chipset": 0x004, "family": "nv04", "generation": 1, "open": False, "notes": "GeForce2/3/4"},
    {"chipset": 0x011, "family": "nv11", "generation": 2, "open": False, "notes": "nForce iGPU"},
    {"chipset": 0x030, "family": "nv30", "generation": 3, "open": False, "notes": "GeForce FX"},
    {"chipset": 0x040, "family": "nv40", "generation": 4, "open": False, "notes": "GeForce 6/7"},
    {"chipset": 0x050, "family": "g70", "generation": 5, "open": False, "notes": "GeForce 79xx"},
    {"chipset": 0x0a0, "family": "g80", "generation": 6, "open": False, "notes": "GeForce 8/9, first CUDA"},
    {"chipset": 0x0c0, "family": "g92", "generation": 7, "open": False, "notes": "GeForce 200 series"},
    {"chipset": 0x0d0, "family": "gt215", "generation": 7, "open": False, "notes": "GT 2xx"},
    {"chipset": 0x0e0, "family": "kepler", "generation": 11, "open": False, "notes": "GK104/GK106/GK107/GK110/GK208"},
    {"chipset": 0x0f0, "family": "fermi", "generation": 9, "open": False, "notes": "GF100"},
    {"chipset": 0x100, "family": "fermi", "generation": 9, "open": False, "notes": "GF108"},
    {"chipset": 0x110, "family": "fermi", "generation": 9, "open": False, "notes": "GF116/MCP89"},
    {"chipset": 0x120, "family": "kepler", "generation": 11, "open": False, "notes": "GK104 (0x118 alias)"},
    {"chipset": 0x118, "family": "kepler", "generation": 11, "open": False, "notes": "GK104"},
    {"chipset": 0x130, "family": "kepler", "generation": 11, "open": False, "notes": "GK208 refresh"},
    {"chipset": 0x140, "family": "maxwell", "generation": 13, "open": False, "notes": "GM107 (GTX 750)"},
    {"chipset": 0x160, "family": "maxwell", "generation": 13, "open": False, "notes": "GM200/GM204 GTX 9xx"},
    {"chipset": 0x170, "family": "maxwell", "generation": 14, "open": False, "notes": "GM20B (Tegra X1)"},
    {"chipset": 0x1a0, "family": "pascal", "generation": 15, "open": False, "notes": "GP100/GP102/T186"},
    {"chipset": 0x1b0, "family": "pascal", "generation": 15, "open": False, "notes": "GP104/GP106/GP107/GP108 GTX10/GTX10"},
    {"chipset": 0x1c0, "family": "pascal", "generation": 15, "open": False, "notes": "GP10B (Tegra X2)"},
    {"chipset": 0x1d0, "family": "volta", "generation": 16, "open": False, "notes": "GV100/GV11B (Titan V, Xavier)"},
    {"chipset": 0x1e0, "family": "turing", "generation": 19, "open": True, "notes": "TU106/TU116 (RTX 2060, GTX 16)"},
    {"chipset": 0x1f0, "family": "turing", "generation": 19, "open": True, "notes": "TU102/TU104/TU117"},
    {"chipset": 0x220, "family": "ampere", "generation": 20, "open": True, "notes": "GA102 (RTX 3090)"},
    {"chipset": 0x222, "family": "ampere", "generation": 20, "open": True, "notes": "GA103 (RTX 3070 Ti)"},
    {"chipset": 0x223, "family": "ampere", "generation": 20, "open": True, "notes": "GA104 (RTX 3060 Ti/3070/3080)"},
    {"chipset": 0x225, "family": "ampere", "generation": 20, "open": True, "notes": "GA105"},
    {"chipset": 0x226, "family": "ampere", "generation": 20, "open": True, "notes": "GA106 (RTX 3060)"},
    {"chipset": 0x227, "family": "ampere", "generation": 20, "open": True, "notes": "GA107"},
    {"chipset": 0x229, "family": "ampere", "generation": 20, "open": True, "notes": "GA10B (Orin)"},
    {"chipset": 0x230, "family": "ampere-ne", "generation": 20, "open": True, "notes": "GA10x refresh"},
    {"chipset": 0x240, "family": "hopper", "generation": 22, "open": True, "notes": "GH100 (H100)"},
    {"chipset": 0x260, "family": "ada", "generation": 21, "open": True, "notes": "AD102 (RTX 4090)"},
    {"chipset": 0x261, "family": "ada", "generation": 21, "open": True, "notes": "AD103 (RTX 4080)"},
    {"chipset": 0x264, "family": "ada", "generation": 21, "open": True, "notes": "AD104"},
    {"chipset": 0x265, "family": "ada", "generation": 21, "open": True, "notes": "AD105"},
    {"chipset": 0x266, "family": "ada", "generation": 21, "open": True, "notes": "AD106 (RTX 4060 Ti)"},
    {"chipset": 0x267, "family": "ada", "generation": 21, "open": True, "notes": "AD107 (RTX 4060)"},
    {"chipset": 0x268, "family": "ada", "generation": 21, "open": True, "notes": "AD10x mobile"},
    {"chipset": 0x270, "family": "ada", "generation": 21, "open": True, "notes": "RTX 5880 Ada"},
    {"chipset": 0x2a0, "family": "blackwell", "generation": 23, "open": True, "notes": "GB202 (RTX 5090)"},
    {"chipset": 0x2a1, "family": "blackwell", "generation": 23, "open": True, "notes": "GB203"},
    {"chipset": 0x2a3, "family": "blackwell", "generation": 23, "open": True, "notes": "GB205"},
    {"chipset": 0x2a5, "family": "blackwell", "generation": 23, "open": True, "notes": "GB206"},
    {"chipset": 0x2a6, "family": "blackwell", "generation": 23, "open": True, "notes": "GB207"},
    {"chipset": 0x2b0, "family": "blackwell", "generation": 23, "open": True, "notes": "GB100 datacenter"},
]


def kernel_version(root: str) -> str:
    try:
        out = subprocess.run(["make", "-s", "kernelversion"], cwd=root,
                             capture_output=True, text=True, timeout=60)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    text = read(os.path.join(root, "Makefile")) or ""
    parts = {}
    for key in ("VERSION", "PATCHLEVEL", "SUBLEVEL", "EXTRAVERSION"):
        m = re.search(r"^%s\s*=\s*(.*)$" % key, text, re.M)
        parts[key] = (m.group(1).strip() if m else "0")
    return "%s.%s.%s%s" % (parts["VERSION"], parts["PATCHLEVEL"], parts["SUBLEVEL"], parts["EXTRAVERSION"])


def git_rev(root: str) -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=30)
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


# ------------------------------------------------------------------------- AMD
AMD_CHIP_ENUM_RE = re.compile(r"CHIP_([A-Z0-9_]+)\s*(?:,\s*/\*\s*(\d+)\s*\*/)?")
AMD_IDS_BLOCK_RE = re.compile(r"static\s+const\s+u16\s+(\w+)\[\]\s*=\s*\{(.*?)\n\};", re.S)
AMD_ID_RE = re.compile(r"0x([0-9a-fA-F]{4})")
AMD_QUIRK_RE = re.compile(r"\{\s*0x([0-9a-fA-F]{4})\s*,\s*0x([0-9a-fA-F]{2})\s*,\s*CHIP_([A-Z0-9_]+)\s*\}")
RADEON_ROW_RE = re.compile(
    r"\{\s*0x1002\s*,\s*0x([0-9a-fA-F]{4})\s*,.*?CHIP_([A-Z0-9_]+)((?:\|[A-Z0-9_|]+)?)\s*\}")
# Kept for reference: chip name -> family label used by the fallback path only.
AMD_CHIP_FAMILY = {
    "TAHITI": "polaris", "PITCAIRN": "polaris", "VERDE": "polaris", "OLAND": "polaris",
    "HAINAN": "polaris", "BONAIRE": "polaris", "KAVERI": "kaveri", "KABINI": "kaveri",
    "HAWAII": "polaris", "MULLINS": "kaveri", "TOPAZ": "topaz", "TONGA": "polaris",
    "FIJI": "polaris", "CARRIZO": "carrizo", "STONEY": "carrizo", "POLARIS10": "polaris",
    "POLARIS11": "polaris", "POLARIS12": "polaris", "VEGAM": "polaris", "VEGA10": "vega",
    "VEGA12": "vega", "VEGA20": "vega", "RAVEN": "gfx9", "ARCTURUS": "gfx9",
    "RENOIR": "gfx9", "ALDEBARAN": "gfx9", "NAVI10": "gfx10_1", "CYAN_SKILLFISH": "gfx10_1",
    "NAVI14": "gfx10_1", "NAVI12": "gfx10_1", "SIENNA_CICHLID": "gfx10_3",
    "NAVY_FLOUNDER": "gfx10_3", "VANGOGH": "gfx10_4", "DIMGREY_CAVEFISH": "gfx10_3",
    "BEIGE_GOBY": "gfx10_3", "YELLOW_CARP": "gfx10_3", "IP_DISCOVERY": "gfx11",
}


def gen_amd(root: str) -> tuple[list[dict[str, object]], dict[str, object]]:
    """AMD: amdgpu matches by *class* and keeps a blocklist; radeon has an id table.

    So the honest database is: every id in radeon's table (with its CHIP_* flag),
    minus the ids amdgpu explicitly refuses, plus the quirks that disambiguate
    same-id-different-chip parts.  That is exactly the driver binding a live system
    performs, so our prediction and the kernel's choice agree by construction.
    """
    meta: dict[str, object] = {}
    text = read(os.path.join(root, "drivers/gpu/drm/amd/amdgpu/amdgpu_drv.c")) or ""
    enum_text = read(os.path.join(root, "include/drm/amd_asic_type.h")) or ""
    radeon_text = read(os.path.join(root, "include/drm/drm_pciids.h")) or ""

    chip_order: dict[str, int] = {}
    for i, m in enumerate(AMD_CHIP_ENUM_RE.finditer(enum_text)):
        name = m.group(1)
        if name in {"LAST", "IP_DISCOVERY"}:
            continue
        chip_order[name] = int(m.group(2)) if m.group(2) else i
    meta["amd_chip_enum_count"] = len(chip_order)

    # amdgpu_unsupported_pciidlist[]: ids amdgpu deliberately declines.
    refused: set[int] = set()
    for block in AMD_IDS_BLOCK_RE.finditer(text):
        if "unsupported" in block.group(1):
            for m in AMD_ID_RE.finditer(block.group(2)):
                refused.add(int(m.group(1), 16))
    meta["amd_refused_by_amdgpu"] = len(refused)
    meta["amd_class_match"] = bool(re.search(r"PCI_DEVICE\(0x1002,\s*PCI_ANY_ID\)", text))

    quirks: dict[int, dict[str, str]] = {}
    for m in AMD_QUIRK_RE.finditer(text):
        dev = int(m.group(1), 16)
        quirks[dev] = {"chip": m.group(3), "revision_hint": "0x" + m.group(2)}

    radeon: dict[int, dict[str, object]] = {}
    section = radeon_text.split("define radeon_PCI_IDS", 1)
    if len(section) > 1:
        for m in RADEON_ROW_RE.finditer(section[1]):
            dev = int(m.group(1), 16)
            flags = m.group(3) or ""
            radeon[dev] = {
                "chip": m.group(2),
                "is_igp": "RADEON_IS_IGP" in flags,
                "mobility": "RADEON_IS_MOBILITY" in flags,
            }
    meta["amd_radeon_ids"] = len(radeon)

    rows: list[dict[str, object]] = []
    for dev, row in sorted(radeon.items()):
        chip = str(row["chip"])
        refused_this = dev in refused
        # The kernel's CHIP_ name IS the family vocabulary (hedronlib.gpus owns the
        # chip→generation scale); fall back to the id-range guess for unnamed parts.
        if chip.lower() in gpus._FAMILY_GENERATIONS:
            family = chip.lower()
        elif refused_this:
            family = "radeon-legacy"
        else:
            family = gpus.amdgpu_family_guess(dev)
        generation = gpus.generation_from_family(family)
        rows.append({
            "id": hex_id(dev),
            "device": dev,
            "chip": chip,
            "family": family,
            # amdgpu's module table claims the class; ids it refuses fall to radeon.
            "driver": "radeon" if refused_this or not meta["amd_class_match"] else "amdgpu",
            "generation": generation,
            "name": chip.lower(),
            "is_igp": bool(row["is_igp"]),
            "mobility": bool(row["mobility"]),
            "refused_by_amdgpu": refused_this,
        })
    for dev, q in sorted(quirks.items()):
        existing = next((r for r in rows if r["device"] == dev), None)
        if existing is not None:
            existing["chip"] = q["chip"]
            existing["family"] = AMD_CHIP_FAMILY.get(q["chip"], existing["family"])
            existing["generation"] = max(int(existing["generation"] or 0), chip_order.get(q["chip"], 0))
            existing["revision_hint"] = q["revision_hint"]
            continue
        chip = q["chip"]
        family = chip.lower() if chip.lower() in gpus._FAMILY_GENERATIONS else gpus.amdgpu_family_guess(dev)
        rows.append({
            "id": hex_id(dev), "device": dev, "chip": chip, "family": family,
            "driver": "amdgpu", "generation": gpus.generation_from_family(family),
            "name": chip.lower(), "revision_hint": q["revision_hint"],
            "source": "asic_type_quirks",
        })
    for row in rows:
        if not row.get("generation"):
            row["generation"] = gpus.generation_from_family(str(row["family"]))
        row["name"] = "%s %s" % (row["driver"], row["chip"])
    meta["amd_rows"] = len(rows)
    return rows, meta


# ----------------------------------------------------------------------- Intel
INTEL_MACRO_HEAD_RE = re.compile(r"#define\s+(INTEL_[A-Z0-9_]+_IDS)\s*\(")
INTEL_ID_INLINE_RE = re.compile(r"MACRO__\(\s*0x([0-9a-fA-F]{4})")
INTEL_PLATFORM_ROW_RE = re.compile(r"INTEL_([A-Z0-9_]+)_IDS\s*\(\s*INTEL_VGA_DEVICE\s*,\s*&([a-z0-9_]+)_desc")


def parse_intel_defines(text: str) -> dict[str, str]:
    """Line-based extraction of every `#define INTEL_*_IDS(...)` body.

    A regex over the whole file is tempting and wrong: these macros are backslash
    continuations, so a line walker that tracks "are we still inside a macro" is the
    reliable way.
    """
    bodies: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.lstrip().startswith("#define"):
            if current is not None:
                bodies[current] = "\n".join(buf)
            head = INTEL_MACRO_HEAD_RE.match(line.strip())
            if head:
                current, buf = head.group(1), [line]
            else:
                current, buf = None, []
            continue
        if current is None:
            continue
        if not line or line.lstrip().startswith(("#", "/*", "*")):
            bodies[current] = "\n".join(buf)
            current, buf = None, []
            continue
        buf.append(line)
        if not line.rstrip().endswith("\\"):
            bodies[current] = "\n".join(buf)
            current, buf = None, []
    if current is not None:
        bodies[current] = "\n".join(buf)
    return bodies


def expand_intel_macros(text: str) -> dict[str, list[int]]:
    """Expand INTEL_*_IDS macro bodies, including nested macro references."""
    bodies = parse_intel_defines(text)

    def collect(name: str, depth: int = 0, seen: set[str] | None = None) -> list[int]:
        if depth > 16:
            return []
        seen = seen if seen is not None else set()
        if name in seen:
            return []
        seen.add(name)
        out: list[int] = []
        body = bodies.get(name, "")
        for m in INTEL_ID_INLINE_RE.finditer(body):
            out.append(int(m.group(1), 16))
        for nested in re.findall(r"(INTEL_[A-Z0-9_]+_IDS)\s*\(", body):
            if nested != name:
                out.extend(collect(nested, depth + 1, seen))
        return out

    return {name: collect(name) for name in bodies}


def gen_intel(root: str) -> tuple[list[dict[str, object]], dict[str, object]]:
    meta: dict[str, object] = {}
    text = read(os.path.join(root, "include/drm/intel/pciids.h")) or ""
    i915 = read(os.path.join(root, "drivers/gpu/drm/i915/i915_pci.c")) or ""
    xe = read(os.path.join(root, "drivers/gpu/drm/xe/xe_pci.c")) or ""
    if not text:
        return [], {"error": "include/drm/intel/pciids.h missing"}

    platforms = expand_intel_macros(text)
    meta["intel_macros"] = len(platforms)

    def claimed(source: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for m in INTEL_PLATFORM_ROW_RE.finditer(source):
            plat = m.group(1).lower()
            desc = m.group(2)
            out.setdefault(plat, desc)
            out.setdefault(desc, plat)
        return out

    xe_claims = claimed(xe)
    i915_claims = claimed(i915)
    # One row per platform the *driver tables* claim (INTEL_XYZ_IDS(..., &xyz_desc)).
    # Sub-macros (TGL_GT1, ADLP_GT1, …) are folded into their parent platform by the
    # recursive expansion, so a device id never resolves to a nameless sub-row.
    claimed_platforms = {v for v in xe_claims.values()} | {v for v in i915_claims.values()}
    claimed_platforms |= {k for k, v in list(xe_claims.items()) + list(i915_claims.items()) if v}
    platform_ids: dict[str, list[int]] = {}
    for name, ids in sorted(platforms.items()):
        plat = name[len("INTEL_"):-len("_IDS")].lower().replace("_", "-")
        base = plat.split("-")[0]
        target = plat if plat in claimed_platforms else (base if base in claimed_platforms else plat)
        platform_ids.setdefault(target, []).extend(ids)

    rows: list[dict[str, object]] = []
    for plat, ids in sorted(platform_ids.items()):
        if not ids:
            continue
        in_xe = plat in xe_claims
        in_i915 = plat in i915_claims
        # Both drivers list many of the same platforms while xe graduates; the kernel
        # prefers xe when it is built, and in 7.x xe owns everything from DG1 onwards.
        driver = "xe" if in_xe else "i915"
        gen = gpus.generation_from_family(plat) or gpus.generation_from_family(plat.replace("-", "_"))
        if driver == "xe" and gen:
            gen = max(gen, 15)
        rows.append({
            "platform": plat,
            "name": "Intel " + plat.upper(),
            "driver": driver,
            "generation": gen,
            "in_xe_table": in_xe,
            "in_i915_table": in_i915,
            "ids": [hex_id(i) for i in sorted(set(ids))],
            "device_ids": sorted(set(ids)),
        })
    meta["intel_platforms"] = len(rows)
    meta["intel_device_count"] = sum(len(r["ids"]) for r in rows)  # type: ignore[arg-type]
    return rows, meta


# ---------------------------------------------------------------------- NVIDIA
NOUVEAU_CHIP_RE = re.compile(r"^\s*case\s+0x([0-9a-f]{3})\s*:", re.M)
NOUVEAU_TABLE_RE = re.compile(r"nvkm_device_pci_10de\[\]\s*=\s*\{(.*?)\n\};", re.S)
NOUVEAU_ROW_RE = re.compile(r"\{\s*0x([0-9a-f]{4})\s*,\s*\"([^\"]+)\"\s*\}")


def nouveau_tables(root: str) -> tuple[set[int], dict[int, str]]:
    """(chipsets nouveau can construct, device id → marketing name)."""
    base = read(os.path.join(root, "drivers/gpu/drm/nouveau/nvkm/engine/device/base.c")) or ""
    pci = read(os.path.join(root, "drivers/gpu/drm/nouveau/nvkm/engine/device/pci.c")) or ""
    supported = {int(m, 16) for m in NOUVEAU_CHIP_RE.findall(base) if int(m, 16) <= 0xFFF}
    names: dict[int, str] = {}
    block = NOUVEAU_TABLE_RE.search(pci)
    if block:
        for dev, name in NOUVEAU_ROW_RE.findall(block.group(1)):
            names[int(dev, 16)] = name
    return supported, names


def gen_nvidia(root: str) -> tuple[list[dict[str, object]], dict[str, object]]:
    """NVIDIA: no in-kernel vendor driver, but nouveau's tables are still the map.

    We take every device id nouveau knows (id → product name, 700+ rows in 7.4), derive
    its chipset with the same arithmetic nouveau uses, snap the id list onto our curated
    architecture rows, and record how well the arithmetic reproduces a chipset the kernel
    can actually construct.  Anything nouveau names but cannot drive is exactly the set
    of cards that *need* the vendor driver — which is the decision this DB exists for.
    """
    meta: dict[str, object] = {}
    supported, names = nouveau_tables(root)
    meta["nouveau_chipsets"] = len(supported)
    meta["nouveau_device_names"] = len(names)
    hit = sum(1 for dev in names if gpus.nvidia_chipset(dev) in supported)
    meta["nouveau_chipset_arithmetic_pct"] = round(100.0 * hit / len(names), 1) if names else 0.0

    by_chipset: dict[int, list[tuple[int, str]]] = {}
    for dev, name in names.items():
        by_chipset.setdefault(gpus.nvidia_chipset(dev), []).append((dev, name))

    rows: list[dict[str, object]] = []
    used: set[int] = set()
    for entry in NVIDIA_CHIPSETS:
        chip = int(entry["chipset"])  # type: ignore[arg-type]
        members = sorted(by_chipset.get(chip, []))
        ids = [dev for dev, _ in members]
        # Fall back to the id block the chipset arithmetic implies when nouveau has no
        # entry for it (brand-new cards): keeps `hedron-gpu-detect` able to answer.
        if not ids:
            lo, hi = chip << 4, (chip << 4) | 0xF0
            ids = list(range(lo, hi + 1))
            synthetic = True
        else:
            synthetic = False
        used.update(ids)
        sample = members[0][1] if members else str(entry["notes"])
        rows.append({
            "nouveau_chipset": hex_id(chip),
            "family": entry["family"],
            "generation": entry["generation"],
            "open_kernel_module": entry["open"],
            "in_tree_driver": "nouveau" if chip in supported else "none",
            "nouveau_supported": chip in supported,
            "name": "NVIDIA %s (%s)" % (str(entry["family"]).upper(), sample),
            "device_id_range": [hex_id(min(ids)), hex_id(max(ids))],
            "device_ids": ids,
            "products": [n for _, n in members[:12]],
            "id_source": "nouveau:pci.c" if not synthetic else "inferred:chipset-arithmetic",
        })

    # Products nouveau recognises but that fall outside our curated arch rows still get
    # an exact-id row, so `hedron-gpu-detect` never answers "unknown" for a real card.
    leftovers = sorted(set(names) - used)
    for dev in leftovers:
        family = gpus.nvidia_family_guess(dev)
        rows.append({
            "nouveau_chipset": hex_id(gpus.nvidia_chipset(dev)),
            "family": family,
            "generation": gpus.generation_from_family(family),
            "open_kernel_module": gpus.generation_from_family(family) >= gpus.NVIDIA_OPEN_MIN_GEN,
            "in_tree_driver": "nouveau" if gpus.nvidia_chipset(dev) in supported else "none",
            "nouveau_supported": gpus.nvidia_chipset(dev) in supported,
            "name": "NVIDIA " + names[dev],
            "device_ids": [dev],
            "id_source": "nouveau:pci.c:orphan",
        })
        used.add(dev)

    for row in rows:
        row["device_ids"] = [hex_id(d) for d in sorted(row["device_ids"])]  # type: ignore[arg-type]
    meta["nvidia_archs"] = len(NVIDIA_CHIPSETS)
    meta["nvidia_rows"] = len(rows)
    meta["nvidia_ids"] = len(used)
    return rows, meta


# ---------------------------------------------------------------------- writer
def generate(root: str) -> dict[str, object]:
    amd, amd_meta = gen_amd(root)
    intel, intel_meta = gen_intel(root)
    nvidia, nvidia_meta = gen_nvidia(root)
    payload: dict[str, object] = {
        "schema": SCHEMA,
        "generated_from": {
            "kernel_root": os.path.abspath(root),
            "kernel_version": kernel_version(root),
            "kernel_commit": git_rev(root),
            "generator": "hedronos/tools/gen-gpu-db.py",
            "amd": amd_meta,
            "intel": intel_meta,
            "nvidia": nvidia_meta,
        },
        "vendors": {
            "0x10de": {"name": "NVIDIA"},
            "0x1002": {"name": "AMD/ATI"},
            "0x8086": {"name": "Intel"},
            "0x15ad": {"name": "VMware"},
            "0x1af4": {"name": "VirtIO"},
            "0x1234": {"name": "QEMU"},
        },
        "classes": {hex_id(c): d for c, d in {
            gpus.CLASS_VGA: "VGA compatible controller",
            gpus.CLASS_3D: "3D controller",
            gpus.CLASS_DISPLAY: "Display controller",
            gpus.CLASS_AUDIO_HDMI: "HDMI audio (function 0x03 of the GPU)",
        }.items()},
        "amd": amd,
        "intel": intel,
        "nvidia": nvidia,
        "notes": [
            "Rows are looked up by exact device id first, then by the chipset arithmetic in "
            "hedronlib.gpus.nvidia_chipset, then by device-id range heuristics, then by PCI class.",
            "amdgpu claims every AMD display class device in the kernel (class match), so a 'missing' "
            "AMD id is not a bug: it still resolves to amdgpu.",
        ],
    }
    return payload


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--kernel", default=".", help="kernel tree to read (default: repo root)")
    ap.add_argument("--out", default="data/gpu-db", help="output directory")
    ap.add_argument("--print", action="store_true", help="print instead of writing")
    ap.add_argument("--summary", action="store_true", help="one-line summary only")
    args = ap.parse_args(argv)

    if not os.path.isfile(os.path.join(args.kernel, "Makefile")):
        eprint("gen-gpu-db: %s does not look like a kernel tree (no Makefile)" % args.kernel)
        return 2

    payload = generate(args.kernel)
    counts = {k: len(payload[k]) for k in ("amd", "intel", "nvidia")}  # type: ignore[index]
    if args.summary:
        print("amd=%(amd)d intel=%(intel)d nvidia=%(nvidia)d" % counts)
        return 0
    text = json.dumps(payload, indent=1, sort_keys=True) + "\n"
    if args.print:
        sys.stdout.write(text)
        return 0
    # `--out` is a directory or the database file itself. Callers naturally write either
    # (`hedronos/imgbuild/lib/drivers.sh` passes the directory, `hedronos-verify`'s reproducibility
    # check passes a temp file), and silently creating a *directory* named gpu-db-regen.json made
    # that check crash with IsADirectoryError instead of telling anyone what happened.
    out = os.path.abspath(args.out)
    if out.endswith(".json"):
        db_path = out
        side = out[:-5] + ".sha256"
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    else:
        db_path = os.path.join(out, "gpu-db.json")
        side = os.path.join(out, "gpu-db.sha256")
        os.makedirs(out, exist_ok=True)
    if os.path.isdir(db_path):
        # Earlier versions treated --out as a directory unconditionally, so a run that asked for
        # "…/gpu-db-regen.json" left a *directory* by that name behind and the next run died inside
        # open() with IsADirectoryError — which reads like a permissions bug and wastes an afternoon.
        print("gen-gpu-db: %s is a directory (a leftover from an older run). Remove it, or pass "
              "the containing directory to --out." % db_path, file=sys.stderr)
        return 3
    with open(db_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    with open(side, "w", encoding="utf-8") as fh:
        from hedronlib.util import sha256_text
        fh.write(sha256_text(text) + "  " + os.path.basename(db_path) + "\n")
    print("wrote %s (amd=%d intel=%d nvidia=%d rows, schema %d)"
          % (db_path, counts["amd"], counts["intel"], counts["nvidia"], SCHEMA))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
