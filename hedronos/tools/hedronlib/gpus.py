# SPDX-License-Identifier: MIT
"""PCI/GPU discovery and identity resolution.

Three input paths, one output model (list of :class:`PciDevice`):

1. ``/sys/bus/pci/devices/*``      — the truth on a real machine (used by the installer)
2. ``lspci -nn`` / ``lspci -nnvmm`` — text capture, used by CI, bug reports, tests
3. synthetic dicts                   — used by unit tests

The PCI-id database (``data/gpu-db/gpu-db.json``) is *generated from the kernel tree*
by ``tools/gen-gpu-db.py`` so the distro's notion of "which driver owns this device"
can never drift from the drivers the kernel actually builds.  Where the kernel gives no
answer (NVIDIA's out-of-tree modules), the database falls back to the same chipset
arithmetic nouveau uses — see :func:`nvidia_chipset`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .util import hex_id, norm_id

PCI_VENDOR_NVIDIA = 0x10DE
PCI_VENDOR_AMD = 0x1002
PCI_VENDOR_INTEL = 0x8086

CLASS_VGA = 0x0300
CLASS_3D = 0x0302
CLASS_AUDIO_HDMI = 0x0403
CLASS_DISPLAY = 0x0380
GPU_CLASSES = {CLASS_VGA, CLASS_3D, CLASS_DISPLAY}
# PCI base/class, e.g. 0x030000
GPU_CLASS_FULL = {0x030000, 0x030200, 0x038000}

VENDOR_NAMES = {
    PCI_VENDOR_NVIDIA: "NVIDIA",
    PCI_VENDOR_AMD: "AMD",
    PCI_VENDOR_INTEL: "Intel",
    0x15AD: "VMware",
    0x1AF4: "VirtIO",
    0x1234: "QEMU",
    0x1A03: "Aspeed",
}


@dataclass
class PciDevice:
    """A PCI function, normalised."""

    slot: str = "0000:00:00.0"
    vendor: int = 0
    device: int = 0
    class_code: int = 0x030000
    subsys_vendor: int = 0
    subsys_device: int = 0
    revision: int = 0
    prog_if: int = 0
    driver: str | None = None
    modules: list[str] = field(default_factory=list)
    description: str = ""
    subsystem: str = ""
    numa_node: int | None = None
    iommu_group: int | None = None

    # ------------------------------------------------------------- identity
    @property
    def vendor_name(self) -> str:
        return VENDOR_NAMES.get(self.vendor, "0x%04x" % self.vendor)

    @property
    def is_gpu(self) -> bool:
        return self.class_code in GPU_CLASS_FULL or (self.class_code >> 8) in {0x0300, 0x0302, 0x0380}

    @property
    def is_hdmi_audio(self) -> bool:
        return (self.class_code >> 16) == CLASS_AUDIO_HDMI

    @property
    def key(self) -> str:
        return f"{hex_id(self.vendor)}:{hex_id(self.device)}"

    def modalias(self) -> str:
        return ("pci:v%08Xd%08Xsv%08Xid%08Xbc%02Xsc%02Xi%02X"
                % (self.vendor, self.device, self.subsys_vendor, self.subsys_device,
                   self.class_code >> 16, (self.class_code >> 8) & 0xFF, self.class_code & 0xFF))

    def as_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot, "vendor": hex_id(self.vendor), "device": hex_id(self.device),
            "class": "0x%06x" % self.class_code, "revision": "0x%02x" % self.revision,
            "driver": self.driver, "modules": list(self.modules),
            "description": self.description, "subsystem": self.subsystem,
            "key": self.key, "vendor_name": self.vendor_name,
            "numa_node": self.numa_node, "iommu_group": self.iommu_group,
        }


@dataclass
class SystemInfo:
    """Everything the driver engine may need beyond the GPU itself."""

    devices: list[PciDevice] = field(default_factory=list)
    dmi: dict[str, str] = field(default_factory=dict)
    cpu: dict[str, Any] = field(default_factory=dict)
    memory_mib: int = 0
    efi: bool = True
    secure_boot: bool = False
    kernel: str = ""
    modules_loaded: set[str] = field(default_factory=set)
    firmware_dir: str = "/usr/lib/firmware"
    has_module: set[str] = field(default_factory=set)
    hybrid: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def gpus(self) -> list[PciDevice]:
        return [d for d in self.devices if d.is_gpu or d.is_hdmi_audio]

    def primary_gpu(self) -> PciDevice | None:
        gpus = [d for d in self.devices if d.is_gpu]
        if not gpus:
            return None
        # Prefer a discrete device (higher TDP class), then the one with a driver bound.
        def rank(d: PciDevice) -> tuple[int, int]:
            discrete = 1 if d.class_code == 0x030000 and d.vendor in (PCI_VENDOR_NVIDIA, PCI_VENDOR_AMD) else 0
            return (discrete, 1 if d.driver else 0)
        return sorted(gpus, key=rank, reverse=True)[0]

    def product(self) -> str:
        d = self.dmi
        vendor = (d.get("sys_vendor") or "").strip()
        name = (d.get("product_name") or d.get("board_name") or "").strip()
        return f"{vendor} {name}".strip()

    def is_handheld(self) -> bool:
        hints = ("legion go", "rog ally", "ally x", "gpd win", "steamos", "steam deck",
                 "ayaneo", "oneswitch", "genki", "jetstream", "yen")
        text = " ".join([self.product(), self.dmi.get("product_sku", ""),
                         self.cpu.get("model", "")]).lower()
        return any(h in text for h in hints)

    def is_laptop(self) -> bool:
        chassis = (self.dmi.get("chassis_type") or "").strip().lower()
        if chassis and chassis[0].isdigit():
            try:
                kind = int(re.match(r"\d+", chassis).group(0))
            except ValueError:
                kind = 0
            if kind in (8, 9, 10, 14, 30, 32):  # portable chassis types per SMBIOS spec
                return True
        return "laptop" in self.dmi.get("product_name", "").lower()

    def as_dict(self) -> dict[str, Any]:
        return {
            "devices": [d.as_dict() for d in self.devices],
            "dmi": dict(self.dmi),
            "cpu": dict(self.cpu),
            "memory_mib": self.memory_mib,
            "efi": self.efi,
            "secure_boot": self.secure_boot,
            "kernel": self.kernel,
            "modules_loaded": sorted(self.modules_loaded),
            "hybrid": self.hybrid,
            "handheld": self.is_handheld(),
            "laptop": self.is_laptop(),
            "extra": dict(self.extra),
        }


# --------------------------------------------------------------------- parsers
_LSPCI_LINE = re.compile(
    r"^(?P<slot>[0-9a-fA-F]{2,4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]|\Z)"
    r".*?\[(?P<cls>[0-9a-fA-F]{4})\]:\s+.*?\[(?P<vid>[0-9a-fA-F]{4}:[0-9a-fA-F]{4})\]"
    r"(?:\s+\(rev\s+(?P<rev>[0-9a-fA-F]{2})\))?",
    re.M,
)
_LSPCI_SIMPLE = re.compile(
    r"^\s*(?P<slot>[0-9a-fA-F]{2,4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]):\s+"
    r"(?P<desc>[^:]+):\s+(?P<name>.*?)\s*\[(?P<vid>[0-9a-fA-F]{4}):(?P<did>[0-9a-fA-F]{4})\]"
    r"(?:\s+\(rev\s+(?P<rev>[0-9a-fA-F]{2})\))?\s*$",
    re.M,
)
_LSPCI_CLASS = re.compile(r"VGA compatible controller|3D controller|Display controller|Audio device")


def parse_lspci(text: str) -> list[PciDevice]:
    """Parse ``lspci -nn`` (the common bug-report form) into devices."""
    out: list[PciDevice] = []
    for line in text.splitlines():
        if ":" not in line:
            continue
        m = _LSPCI_SIMPLE.search(line)
        if not m:
            continue
        vid = norm_id(m.group("vid"))
        did = norm_id(m.group("did"))
        desc = (m.group("desc") or "").strip()
        cls = _class_from_description(desc)
        dev = PciDevice(
            slot=_canon_slot(m.group("slot")),
            vendor=vid,
            device=did,
            class_code=cls,
            description=f"{desc}: {(m.group('name') or '').strip()}",
            revision=norm_id(m.group("rev") or "0"),
        )
        out.append(dev)
    return out


def _class_from_description(desc: str) -> int:
    d = desc.lower()
    if "audio device" in d:
        return 0x040300
    if "3d controller" in d:
        return 0x030200
    if "display controller" in d:
        return 0x038000
    return 0x030000


def _canon_slot(slot: str) -> str:
    parts = slot.split(":")
    if len(parts) == 3:
        return slot
    if len(parts) == 2:
        return "0000:" + slot
    return slot


def parse_lspci_vmm(text: str) -> list[PciDevice]:
    """Parse ``lspci -nnvmm`` (RFC-style, key: value blocks). Most robust path."""
    devices: list[PciDevice] = []
    for block in re.split(r"\n\s*\n", text):
        fields: dict[str, list[str]] = {}
        for line in block.splitlines():
            if not line.strip():
                continue
            key, _, value = line.partition(":")
            fields.setdefault(key.strip().lower(), []).append(value.strip())
        if "slot" not in fields:
            continue
        dev = PciDevice(slot=_canon_slot(fields["slot"][0]))
        if "class" in fields:
            try:
                dev.class_code = int(fields["class"][0].split()[0], 16) << 8
            except (ValueError, IndexError):
                pass
        if "vendor" in fields:
            m = re.search(r"\[([0-9a-fA-F]{4})\]", fields["vendor"][0])
            if m:
                dev.vendor = norm_id(m.group(1))
        if "device" in fields:
            m = re.search(r"\[([0-9a-fA-F]{4})\]", fields["device"][0])
            if m:
                dev.device = norm_id(m.group(1))
            name = re.sub(r"\s*\[[0-9a-fA-F]{4}\]\s*", " ", fields["device"][0]).strip()
            dev.description = name
        for key, attr in (("svendor", "subsys_vendor"), ("sdevice", "subsys_device")):
            if key in fields:
                m = re.search(r"\[([0-9a-fA-F]{4})\]", fields[key][0])
                if m:
                    setattr(dev, attr, norm_id(m.group(1)))
        if "rev" in fields:
            dev.revision = norm_id(fields["rev"][0])
        if "driver" in fields:
            dev.driver = fields["driver"][0].split(",")[0].strip() or None
        if "module" in fields:
            dev.modules = [m.strip() for m in fields["module"] if m.strip()]
        devices.append(dev)
    return devices


def devices_from_sysfs(root: str = "/sys") -> list[PciDevice]:
    """Read the real machine. Any path that does not exist is simply skipped."""
    base = os.path.join(root, "bus", "pci", "devices")
    out: list[PciDevice] = []
    if not os.path.isdir(base):
        return out
    for slot in sorted(os.listdir(base)):
        path = os.path.join(base, slot)
        try:
            vendor = norm_id(read_file(os.path.join(path, "vendor")))
            device = norm_id(read_file(os.path.join(path, "device")))
        except (OSError, ValueError):
            continue
        cls_raw = read_file(os.path.join(path, "class")) or "0x030000"
        dev = PciDevice(
            slot=slot,
            vendor=vendor,
            device=device,
            class_code=int(cls_raw, 16),
            revision=_read_int(os.path.join(path, "revision"), 0),
            subsys_vendor=norm_id(read_file(os.path.join(path, "subsystem_vendor")) or "0"),
            subsys_device=norm_id(read_file(os.path.join(path, "subsystem_device")) or "0"),
        )
        driver_link = os.path.join(path, "driver")
        if os.path.islink(driver_link):
            dev.driver = os.path.basename(os.path.realpath(driver_link))
        modalias = read_file(os.path.join(path, "modalias"))
        if modalias:
            dev.modules = modules_from_modalias(root, modalias.strip())
        dev.numa_node = _read_int(os.path.join(path, "numa_node"), None, signed=True)
        grp = os.path.join(path, "iommu_group")
        if os.path.islink(grp):
            name = os.path.basename(os.path.realpath(grp))
            if name.isdigit():
                dev.iommu_group = int(name)
        out.append(dev)
    return out


def read_file(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _read_int(path: str, default: int | None, *, signed: bool = False) -> int | None:
    raw = read_file(path)
    if raw is None:
        return default
    try:
        return int(raw, 16) if raw.startswith("0x") and not signed else int(raw, 10)
    except ValueError:
        return default


MODALIAS_MODULE_CACHE: dict[str, list[str]] = {}


def modules_from_modalias(sysfs_root: str, modalias: str) -> list[str]:
    """Resolve a PCI modalias to the modules that claim it.

    A live system asks ``modprobe --resolve-alias``; that is a side-effecting subprocess,
    so callers inject the answer into ``MODALIAS_MODULE_CACHE`` (see
    ``hedron-drivers snapshot``) and offline analysis simply gets ``[]`` — the kernel
    still binds the device by class, so this is only ever used to *prefill* the plan.
    """
    return list(MODALIAS_MODULE_CACHE.get(modalias, []))


# ------------------------------------------------------------------ classification
@dataclass
class GpuIdentity:
    """The resolved *who am I* of one GPU, ready for the decision engine."""

    device: PciDevice
    vendor: str = "unknown"          # nvidia | amd | intel | unknown
    family: str = "unknown"          # e.g. ad102-rtx40, navi31, mtl
    generation: int = 0              # monotonic, bigger = newer (see DB)
    product_name: str = ""
    kmod: str = ""                   # kernel driver that owns it in-tree
    nouveau_chipset: int | None = None
    open_kmod_ok: bool | None = None  # NVIDIA: does the open module support it?
    in_database: bool = False
    source: str = "heuristic"

    def as_dict(self) -> dict[str, Any]:
        return {
            "vendor": self.vendor,
            "family": self.family,
            "generation": self.generation,
            "product": self.product_name,
            "kmod": self.kmod,
            "open_kmod_ok": self.open_kmod_ok,
            "nouveau_chipset": None if self.nouveau_chipset is None else hex_id(self.nouveau_chipset),
            "in_database": self.in_database,
            "source": self.source,
            "pci": self.device.key,
            "slot": self.device.slot,
        }


def nvidia_chipset(device_id: int) -> int:
    """NVIDIA's nouveau chipset number for a PCI device id.

    Same arithmetic the kernel uses (drivers/gpu/drm/nouveau/nvkm/engine/device/base.c):
    the chipset is the top two nibbles of the device id shifted down by 4, which makes
    0x0E16 (GK104) → 0x0e0 and 0x2684 (AD102) → 0x268.
    """
    return (device_id & 0xFF00) >> 4


class GpuDatabase:
    """The generated PCI-id database, with exact + range + chipset fallback lookup."""

    def __init__(self, data: dict[str, Any]):
        self.raw = data
        self.schema = int(data.get("schema", 1))
        self.generated = data.get("generated_from", {})
        self._amd = self._index(data.get("amd", []), ("id",))
        self._intel = self._index(data.get("intel", []), ("ids",))
        self._nvidia_by_id = self._index(data.get("nvidia", []), ("device_ids",))
        self._nvidia_by_chipset: dict[int, dict[str, Any]] = {}
        for row in data.get("nvidia", []):
            chip = row.get("nouveau_chipset")
            if chip is not None:
                self._nvidia_by_chipset[norm_id(chip)] = row
        self._fallback = data.get("vendors", {})

    @staticmethod
    def _index(rows: Iterable[dict[str, Any]], key_fields: Sequence[str]) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        for row in rows:
            for fld in key_fields:
                value = row.get(fld)
                if value is None:
                    continue
                for one in value if isinstance(value, list) else [value]:
                    try:
                        out[norm_id(one)] = row
                    except ValueError:
                        continue
        return out

    # ------------------------------------------------------------------ lookups
    def lookup(self, device: PciDevice) -> GpuIdentity:
        ident = GpuIdentity(device=device)
        if device.vendor == PCI_VENDOR_AMD:
            row = self._amd.get(device.device)
            ident.vendor = "amd"
            if row:
                ident.in_database = True
                ident.source = "kernel:drm_pciids/amdgpu_drv"
                ident.family = row.get("family") or row.get("chip") or "gfx9"
                ident.product_name = row.get("name", "")
                # Generation comes from the CHIP_* name the kernel itself records; the
                # row's own field is only a fallback for chips we cannot name.
                ident.generation = generation_from_family(str(ident.family)) or int(row.get("generation", 0))
                ident.kmod = row.get("driver") or "amdgpu"
            else:
                ident.source = "class-match"
                guess = amdgpu_family_guess(device.device)
                ident.family = guess
                ident.generation = generation_from_family(guess)
                # amdgpu claims by class and declines the ids in its own blocklist; a
                # pre-Green-Island part therefore belongs to radeon.
                ident.kmod = "radeon" if 0 < ident.generation <= 6 else "amdgpu"
        elif device.vendor == PCI_VENDOR_INTEL:
            row = self._intel.get(device.device)
            ident.vendor = "intel"
            if row:
                ident.in_database = True
                ident.source = "kernel:i915_pci/xe_pci"
                ident.family = row.get("platform", "unknown")
                ident.generation = int(row.get("generation", 0))
                ident.kmod = row.get("driver") or "i915"
                ident.product_name = row.get("name", "")
            else:
                ident.source = "class-match"
                # Unknown Intel id: assume a Gen9+ iGPU on i915, which is the safe bet
                # for anything a person would realistically game on; xe is only chosen
                # when the generated database names the platform.
                ident.kmod = "i915" if device.device < 0x7D00 else "xe"
                ident.family = "unknown-iGPU"
                ident.generation = 7 if ident.kmod == "i915" else 15
        elif device.vendor == PCI_VENDOR_NVIDIA:
            chip = nvidia_chipset(device.device)
            ident.vendor = "nvidia"
            ident.nouveau_chipset = chip
            row = self._nvidia_by_id.get(device.device) or self._nvidia_by_chipset.get(chip)
            if row:
                ident.in_database = True
                ident.source = "db:nvidia"
                ident.family = row.get("family", "")
                ident.generation = int(row.get("generation", 0))
                ident.product_name = row.get("name", "")
                ident.open_kmod_ok = row.get("open_kernel_module")
                ident.kmod = row.get("in_tree_driver", "nouveau")
            else:
                ident.source = "chipset-arithmetic"
                ident.family = nvidia_family_guess(device.device)
                ident.generation = generation_from_family(ident.family)
                ident.kmod = "nouveau"
                ident.open_kmod_ok = ident.generation >= NVIDIA_OPEN_MIN_GEN
        else:
            ident.vendor = VENDOR_NAMES.get(device.vendor, "unknown").lower()
            ident.kmod = device.driver or "simpledrm"
            ident.family = "generic"
        if ident.generation == 0:
            ident.generation = generation_from_family(ident.family)
        return ident

    def all_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for key in ("amd", "intel", "nvidia"):
            for row in self.raw.get(key, []):
                merged = dict(row)
                merged["vendor"] = key
                rows.append(merged)
        return rows

    def families(self, vendor: str | None = None) -> list[dict[str, Any]]:
        seen: set[tuple[str, str]] = set()
        out: list[dict[str, Any]] = []
        for row in self.all_rows():
            if vendor and row.get("vendor") != vendor:
                continue
            fam = row.get("family") or row.get("chip") or row.get("platform") or "unknown"
            key = (row.get("vendor", "?"), fam)
            if key in seen:
                continue
            seen.add(key)
            out.append({"vendor": row.get("vendor"), "family": fam,
                        "generation": int(row.get("generation", 0)),
                        "driver": row.get("driver") or row.get("in_tree_driver") or "",
                        "open_kernel_module": row.get("open_kernel_module"),
                        "name": row.get("name", "")})
        return sorted(out, key=lambda r: (r["vendor"], -r["generation"]))


# ------------------------------------------------------------------ heuristics
NVIDIA_OPEN_MIN_GEN = 19  # Turing (TU10x) and newer use the open kernel modules

_FAMILY_GENERATIONS = {
    # --- every CHIP_ name that appears in include/drm/drm_pciids.h radeon_PCI_IDS,
    #     so a legacy card never resolves to "unknown" (they must all stay <= 6 so the
    #     radeon branch is selected, exactly as the kernel binds them).
    "r100": 1, "r200": 1, "rv100": 1, "rv200": 1, "rv250": 1, "rv280": 1, "rs100": 1,
    "rs200": 1, "rs300": 1, "r300": 2, "r350": 2, "rv350": 2, "rv380": 2, "r420": 2,
    "r423": 2, "rs400": 2, "rs480": 2, "rv410": 2, "r520": 3, "r580": 3, "rv515": 3,
    "rv530": 3, "rv560": 3, "rv570": 3, "rs600": 2, "rs690": 2, "rs740": 2, "rs780": 2,
    "rs880": 2, "r600": 4, "rv610": 4, "rv620": 4, "rv630": 4, "rv635": 4, "rv670": 4,
    "rv710": 4, "rv730": 4, "rv740": 4, "rv770": 5, "cedar": 5, "juniper": 5,
    "redwood": 5, "cypress": 5, "hemlock": 5, "barts": 5, "sumo": 5, "sumo2": 5,
    "palm": 5, "turks": 6, "caicos": 6, "cayman": 6, "aruba": 6, "verde": 7,
    "pitcairn": 7, "tahiti": 7, "oland": 7, "hainan": 7, "bonaire": 8, "kabini": 8,
    "kaveri": 8, "mullins": 8, "hawaii": 8, "radeon-legacy": 5,
    # --- extra Intel platform aliases (sub-GT and SKU variants the kernel names)
    "i945": 1, "i965": 1, "poulsbo": 1, "ilk": 2, "snb": 3, "ivb": 4, "hsw": 5,
    "bdw": 6, "chv": 6, "bxt": 6, "skl": 7, "kbl": 8, "cfl": 9, "cnl": 9, "glk": 8,
    "icl": 10, "ehl": 9, "jsl": 8, "tgl": 11, "rkl": 12, "adl": 14, "adl-n": 13,
    "adl-s": 13, "adl-p": 14, "rpl": 16, "rpl-s": 15, "rpl-u": 16, "rpl-p": 16,
    "cml": 9, "dg1": 15, "atsm": 15, "svc": 15, "pvc": 19, "dg2": 17, "mtl": 18,
    "mtl-p": 18, "mtl-s": 18, "arl": 18, "lnl": 19, "bmg": 20, "bmg-g21": 20,
    # A monotonic "how new is this part" scale used only for branch selection, per
    # vendor: the numbers are comparators inside one vendor's rule table, not a
    # cross-vendor ranking and not a benchmark.
    # --- NVIDIA (nouveau architecture names)
    "riva": 1, "geforce2": 2, "geforce3": 3, "geforce4": 4, "fx": 5, "g70": 6, "g80": 7,
    "gt200": 8, "fermi": 9, "kepler": 11, "maxwell": 13, "maxwell2": 14, "pascal": 15,
    "volta": 16, "turing": 19, "ampere": 20, "ampere-ne": 20, "ada": 21, "hopper": 22,
    "blackwell": 23, "rubin": 24,
    # --- AMD: radeon era (<=6) → SI/CIK (7-9) → polaris/Vega/RDNA (10+)
    "r100": 1, "r200": 1, "rv200": 1, "rs100": 1, "rs200": 1,
    "r300": 2, "r350": 2, "rv350": 2, "rv380": 2, "r420": 2, "r423": 2, "r430": 2,
    "r480": 2, "rs400": 2, "rs480": 2, "rs600": 2, "rs690": 2, "rs740": 2, "rs780": 2,
    "rs880": 2, "rv410": 2, "rx700": 2,
    "r520": 3, "rv515": 3, "rv530": 3, "rv560": 3, "rv570": 3, "r580": 3,
    "r600": 4, "rv610": 4, "rv620": 4, "rv630": 4, "rv635": 4, "rv710": 4, "rv730": 4,
    "rv740": 4, "ondatje": 4,
    "rv770": 5, "rv870": 5, "cedar": 5, "juniper": 5, "redwood": 5, "cypress": 5,
    "hemlock": 5, "barts": 5, "sumo": 5, "sumo2": 5, "trinity": 5, "nicaragua": 5,
    "evergreen": 5, "northernislands": 6, "turks": 6, "caicos": 6, "cayman": 6,
    "southernislands": 7, "tahiti": 7, "pitcairn": 7, "verde": 7, "oland": 7, "hainan": 7,
    "bonaire": 8, "kabini": 8, "kaveri": 8, "mullins": 8, "hawaii": 8, "seaislands": 9,
    "topaz": 9, "carrizo": 9, "stoney": 9, "volcanicislands": 10, "gcN": 5,
    "tonga": 10, "fiji": 10, "polaris": 10, "polaris10": 10, "polaris11": 10,
    "polaris12": 10, "vegam": 10, "gfx8": 10,
    "gfx9": 12, "gfx906": 12, "gfx908": 12, "vega": 12, "vega10": 12, "vega12": 12,
    "vega20": 12, "raven": 12, "renoir": 12, "arcturus": 12, "aldebaran": 12,
    "gfx10_1": 13, "navi10": 13, "navi12": 13, "navi14": 13, "cyan_skillfish": 13,
    "gfx10_3": 14, "sienna_cichlid": 14, "navy_flounder": 14, "dimgrey_cavefish": 14,
    "beige_goby": 14, "yellow_carp": 14, "navi21": 14, "navi22": 14, "navi23": 14,
    "navi24": 14, "vangogh": 14, "gfx10_4": 14,
    "gfx11": 15, "gfx11_5": 15, "navi31": 15, "navi32": 15, "navi33": 15, "navi34": 15,
    "gfx11_0-3": 15,
    "gfx12": 16, "gfx12_1": 16, "navi48": 16, "navi49": 16,
    # --- Intel
    "i965": 1, "ilk": 2, "snb": 3, "ivb": 4, "hsw": 5, "bdw": 6, "skl": 7, "kbl": 8,
    "cfl": 9, "ehl": 9, "tgl": 11, "rkl": 12, "adl-n": 13, "adl-s": 13, "adl-p": 14,
    "dg1": 15, "rpls": 15, "rplu": 16, "rplp": 16, "dg2": 17, "mtl": 18, "arl": 18,
    "lnl": 19, "bmg": 20, "ptl": 21, "wcl": 21, "nvls": 22, "nvlp": 22,
    "icl": 10, "ehl": 9,
}


def generation_from_family(family: str) -> int:
    """Generation scale for a family/platform label; 0 when we truly do not know."""
    if not family:
        return 0
    key = family.strip().lower()
    if key in _FAMILY_GENERATIONS:
        return _FAMILY_GENERATIONS[key]
    # "cml-gt2", "mtl-p", "bmg-g21" → try successively shorter prefixes, which is how
    # the kernel names SKU/GT variants of a platform it otherwise treats identically.
    parts = key.replace("_", "-").split("-")
    for cut in range(len(parts) - 1, 0, -1):
        probe = "-".join(parts[:cut])
        if probe in _FAMILY_GENERATIONS:
            return _FAMILY_GENERATIONS[probe]
        if probe.replace("-", "_") in _FAMILY_GENERATIONS:
            return _FAMILY_GENERATIONS[probe.replace("-", "_")]
    # adx/xxx prefix matching: "ad102-rtx40" → ada, "navi31" → gfx11
    for prefix, gen in NVIDIA_ID_PREFIX:
        if key.startswith(prefix):
            return gen
    for prefix, gen in AMD_ID_PREFIX:
        if key.startswith(prefix):
            return gen
    for prefix, gen in INTEL_ID_PREFIX:
        if key.startswith(prefix):
            return gen
    return 0


NVIDIA_ID_PREFIX = [
    ("gb", 23), ("gh", 22), ("ga", 20), ("ad", 21), ("tu", 19), ("gv", 18), ("gp", 15),
    ("gm", 13), ("gk", 11), ("gf", 9), ("g9", 8), ("g8", 7), ("nv", 3),
]
AMD_ID_PREFIX = [
    ("gfx12", 22), ("navi4", 22), ("navi3", 19), ("navi2", 16), ("navi1", 15),
    ("vega", 12), ("polaris", 11), ("carrizo", 10), ("kaveri", 9), ("topaz", 9),
]
INTEL_ID_PREFIX = [
    ("ptl", 21), ("nvls", 22), ("lnl", 19), ("arl", 18), ("mtl", 18), ("bmg", 20),
    ("dg2", 17), ("dg1", 15), ("rpl", 16), ("adl", 14), ("tgl", 11), ("icl", 10),
]

_NVIDIA_DEVICE_RANGES = [
    ((0x2200, 0x27FF), "ada"),       # AD10x RTX 40
    ((0x2600, 0x27FF), "ada"),
    ((0x2200, 0x22FF), "ampere"),     # GA10x
    ((0x2400, 0x25FF), "ampere"),
    ((0x1E00, 0x1FFF), "turing"),     # TU10x/TU11x
    ((0x1F00, 0x1FFF), "turing"),
    ((0x1B00, 0x1BFF), "turing"),     # GTX 16 (TU116/TU117)
    ((0x1C00, 0x1CFF), "turing"),
    ((0x1D00, 0x1DFF), "volta"),
    ((0x1200, 0x17FF), "maxwell2"),
    ((0x1000, 0x11FF), "maxwell"),
    ((0x1340, 0x13FF), "kepler"),
    ((0x0F00, 0x133F), "kepler"),
    ((0x0E00, 0x0EFF), "kepler"),
    ((0x0DC0, 0x0DFF), "fermi"),
]


def nvidia_family_guess(device_id: int) -> str:
    for (lo, hi), fam in _NVIDIA_DEVICE_RANGES:
        if lo <= device_id <= hi:
            if fam == "ampere" and 0x2200 <= device_id <= 0x22FF:
                return "ampere"
            return fam
    if device_id >= 0x2A00:
        return "blackwell"
    if device_id >= 0x2800:
        return "hopper"
    return "unknown-nv"


_AMD_DEVICE_RANGES = [
    ((0x0C00, 0x0FFF), "gfx12"),      # RDNA4 / UIO
    ((0x1000, 0x17FF), "gfx11"),      # RDNA3 (navi3x)
    ((0x7380, 0x77FF), "gfx10_3"),    # RDNA2 navi2x
    ((0x7400, 0x747F), "gfx10_3"),
    ((0x6600, 0x67FF), "gfx10_1"),    # navi10/14
    ((0x15D8, 0x16FF), "gfx9"),       # vega iGPU (raven/renoir...)
    ((0x1630, 0x168F), "gfx9"),
    ((0x1900, 0x1AF0), "volcanicislands"),
]


def amdgpu_family_guess(device_id: int) -> str:
    for (lo, hi), fam in _AMD_DEVICE_RANGES:
        if lo <= device_id <= hi:
            return fam
    if 0x1000 <= device_id <= 0x1FFF:
        return "gfx9"
    if device_id < 0x4000:
        return "rv380"      # Rage-X / Radeon 9x00 era
    if device_id < 0x5C00:
        return "r520"
    if device_id < 0x6800:
        return "r600"
    if device_id < 0x7000:
        return "evergreen"
    if device_id < 0x7300:
        return "cayman"
    return "gfx9"


def collect_devices(sysfs_root: str = "/sys", lspci_text: str | None = None) -> list[PciDevice]:
    devices = devices_from_sysfs(sysfs_root)
    if devices:
        return devices
    if lspci_text:
        parsed = parse_lspci_vmm(lspci_text) or parse_lspci(lspci_text)
        if parsed:
            return parsed
    return devices


def collect_system_info(sysfs_root: str = "/sys", dmi_dir: str | None = None,
                        proc_dir: str = "/proc", lspci_text: str | None = None,
                        kernel_release: str | None = None) -> SystemInfo:
    info = SystemInfo()
    info.devices = collect_devices(sysfs_root, lspci_text)
    info.kernel = kernel_release or os.uname().release
    dmi_dir = dmi_dir or os.path.join(sysfs_root, "class", "dmi", "id")
    if os.path.isdir(dmi_dir):
        for name in ("sys_vendor", "product_name", "product_version", "board_name",
                     "bios_vendor", "bios_version", "chassis_type", "product_sku"):
            value = read_file(os.path.join(dmi_dir, name))
            if value:
                info.dmi[name] = value
    meminfo = os.path.join(proc_dir, "meminfo")
    if os.path.isfile(meminfo):
        try:
            with open(meminfo, encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        info.memory_mib = int(line.split()[1]) // 1024
                        break
        except (OSError, ValueError):
            pass
    cpuinfo = os.path.join(proc_dir, "cpuinfo")
    if os.path.isfile(cpuinfo):
        try:
            with open(cpuinfo, encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("model name"):
                        info.cpu["model"] = line.split(":", 1)[1].strip()
                        break
        except OSError:
            pass
    if os.path.isfile(os.path.join(sysfs_root, "firmware", "efi", "sysfb")) or \
            os.path.isdir(os.path.join(sysfs_root, "firmware", "efi")):
        info.efi = True
    loaded = os.path.join(proc_dir, "modules")
    if os.path.isfile(loaded):
        try:
            with open(loaded, encoding="utf-8") as fh:
                for line in fh:
                    name = line.split(" ", 1)[0].strip()
                    if name:
                        info.modules_loaded.add(name)
        except OSError:
            pass
    gpus = [d for d in info.devices if d.is_gpu]
    info.hybrid = len({d.vendor for d in gpus}) > 1
    return info
