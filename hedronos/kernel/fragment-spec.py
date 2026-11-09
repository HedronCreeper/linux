#!/usr/bin/env python3
"""HedronOS kernel *overlay* spec — what changes per variant, and why.

SPDX-License-Identifier: MIT

The base defconfig (``defconfig-spec.py``) is what every HedronOS machine gets. This list is the
delta an installer or a user applies on top, produced into ``hedronos.fragment`` by::

    tools/gen-defconfig.py --kernel .. --spec hedronos/kernel/fragment-spec.py \
        --out hedronos/kernel/hedronos.fragment

A *fragment*, not a defconfig: the PKGBUILD concatenates it after the base config (``make
olddefconfig`` then resolves the dependencies), so this file may only say "flip this" — it may not
be the place where a subsystem gets enabled for the first time.

Two rules the format enforces on us:

  * Every name is checked against this tree's Kconfig files by the generator, so a renamed symbol
    disappears into the report instead of silently vanishing from a built kernel. A ``?`` suffix
    marks a symbol we merely *wish* existed (``CONFIG_FOO?=y``); it is dropped quietly and shows up
    under "unfulfilled guesses".
  * ``=n`` is written for us as ``# CONFIG_FOO is not set``. Never hand-write either form: kconfig
    accepts a fragment line whole and an unparsable line is a silent no-op.

Group names double as section titles when they match the base spec's; keep them in that vocabulary.
"""

# (group, symbol, value, note)  — value "n" means "explicitly off", and needs a real reason.
SPEC = [
    # ------------------------------------------------------------ latency: the realtime overlay
    # `--variant uc` and the `hedronos-rt` kernel both want PREEMPT_RT. It is not in the base
    # config because a general-purpose gaming kernel stays PREEMPT_DYNAMIC: forcing RT costs
    # throughput on the non-isolated cores and forbids some drivers that sleep with locks held.
    ("latency", "CONFIG_PREEMPT_RT", "y", "the -rt flavour: threading models, no 'preempt_disable for a page fault'"),
    ("latency", "CONFIG_PREEMPT_COUNT", "y", "needed by gamescope's pipelined present for accurate frame pacing"),
    ("latency", "CONFIG_HIGH_RES_TIMERS?", "y", "def_bool on x86, set explicitly so a non-x86 build does not regress"),
    ("latency", "CONFIG_NO_HZ_FULL", "y", "tickless on the isolated cores; the game core must not be woken 300x/s"),
    ("latency", "CONFIG_RCU_NOCB_CPU", "y", "offload RCU callbacks off the gaming cores (hedron-tune sets the cpumask)"),
    ("latency", "CONFIG_RCU_BOOST", "y", "so a short RT section cannot be delayed by a long rcu_read_lock"),
    ("latency", "CONFIG_RCU_BOOST_DELAY", "500", "boost the grace-period kthread within half a ms of need"),
    ("latency", "CONFIG_IRQ_TIME_ACCOUNTING", "y", "per-softirq time, which hedron-tune uses to spot interrupt storms"),
    ("latency", "CONFIG_IRQ_FORCED_THREADING", "y", "implicit with RT, spelled out because a regression here is audible"),
    ("latency", "CONFIG_SCHED_HWLAT?", "y", "if this tree exposes the hwlat detector via config (it is a boot option too)"),

    # ------------------------------------------------------------ sched: what the session layer needs
    ("sched", "CONFIG_SCHED_CORE", "y", "core scheduling for anti-cheat-safe pairing of game and overlay threads"),
    ("sched", "CONFIG_SCHED_SMT", "y", "SMT-aware balancing so the second hyperthread does not steal frame time"),
    ("sched", "CONFIG_SCHED_ACPU?", "y", "the EEVDF successor; only present in some trees, so marked as a wish"),
    ("sched", "CONFIG_RT_GROUP_SCHED", "n", "off: a game session owns its cpu.max and must not share a group quota"),
    ("sched", "CONFIG_CGROUP_SCHED", "y", "the gamescope/Steam slices depend on the cpu controller"),
    # SCHED_DEBUG is not a symbol in this tree (7.4 dropped it; tools/sched_ext/Kconfig only
    # recommends it). Kept as a marked wish so a tree that re-adds it gets it.
    ("sched", "CONFIG_SCHED_DEBUG?", "y", "sched_debug output hedron-doctor parses"),
    ("sched", "CONFIG_SCHEDSTATS", "y", "run latency statistics per cgroup for the vitals HUD"),
    ("sched", "CONFIG_SCHED_INFO", "y", "delayacct: the numbers hedron-latency reports in ms"),
    ("sched", "CONFIG_DELAYACCT?", "y", "older name for the same feature"),

    # ------------------------------------------------------------ build: debuginfo you can use
    ("build", "CONFIG_DEBUG_INFO_BTF", "y", "sched_ext and BPF structural probes are impossible without BTF"),
    ("build", "CONFIG_DEBUG_INFO_BTF_MODULES", "y", "so amdgpu/xe as modules are also probeable"),
    ("build", "CONFIG_FTRACE_SYSCALLS", "y", "the replay tool attributes a stutter to a syscall, not to 'the kernel'"),
    ("build", "CONFIG_HWLAT_TRACER", "y", "hedron-latency hwlat mode"),
    ("build", "CONFIG_TIMERLAT_TRACER", "y", "hedron-latency timerlat mode: per-CPU, with a deadline"),
    ("build", "CONFIG_OSNOISE_TRACER", "y", "run this while the machine idles to prove the 1 ms frame budget is safe"),
    ("build", "CONFIG_MEM_ALLOC_PROFILING", "y", "who allocated the memory, which is the answer to 'why is VRAM-backed RAM full'"),
    ("build", "CONFIG_KEXEC", "y", "crash dumps from a machine that only faults under load"),
    ("build", "CONFIG_KEXEC_FILE", "y", "signature-verified kexec, so lockdown and kdump can coexist"),
    ("build", "CONFIG_CRASH_DUMP", "y", "the dump-capture kernel side"),
    ("build", "CONFIG_FRAME_WARN", "2048", "a frame that deep is a stack-overflow waiting for a deep reclaim path"),
    ("build", "CONFIG_KALLSYMS_ALL", "y", "perf and the HUD need per-CPU and __init symbols"),

    # ------------------------------------------------------------ memory
    ("memory", "CONFIG_ZSWAP", "y", "compress before swapping: a 32 GB machine can hold two games and a browser"),
    ("memory", "CONFIG_ZSWAP_COMPRESSOR_DEFAULT_LZ4?", "y", "fast beats dense here; the base config sets the same policy"),
    ("memory", "CONFIG_LRU_GEN", "y", "the multi-generational LRU keeps a game's working set out of the evict list"),
    ("memory", "CONFIG_LRU_GEN_ENABLED", "y", "on by default for our users, tunable via /sys/kernel/mm/lru_gen"),
    ("memory", "CONFIG_TRANSPARENT_HUGEPAGE_ALWAYS", "y", "game allocators are big and linear; defrag=always is the tuning daemon's job"),
    ("memory", "CONFIG_COMPACTION", "y", "without it THP is a lottery"),
    ("memory", "CONFIG_MITIGATION_PAGE_TABLE_ISOLATION", "y", "left on: Meltdown mitigation costs ~2% and a customer's bank account"),
    ("memory", "CONFIG_INIT_ON_ALLOC_DEFAULT_ON", "y", "cheap hardening for a machine that runs untrusted anti-cheat ring-0"),

    # ------------------------------------------------------------ power: the handheld delta
    ("power", "CPU_FREQ_GOV_PERFORMANCE", "y", "governor built in, not as a module, so a cold boot never falls back to powersave"),
    ("power", "CPU_FREQ_GOV_POWERSAVE", "y", "the battery profile on a handheld"),
    ("power", "CONFIG_CPU_IDLE_GOV_TEO", "y", "the only idle governor worth having on a Deck-class machine"),
    ("power", "CONFIG_INTEL_IDLE", "y", "C-state table for the x86 handhelds"),
    ("power", "CONFIG_AMD_PMC", "y", "s2idle on AMD, which is every current handheld"),
    ("power", "CONFIG_PMC_ATOM?", "y", "older Bay Trail tablets still in the field"),
    ("power", "CONFIG_X86_AMD_PSTATE_UT", "m", "the debug module hedron-doctor uses to read a pstate transition"),
    ("power", "CONFIG_POWERCAP", "y", "RAPL: what 'how many watts is this box pulling' means on x86"),
    ("power", "CONFIG_INTEL_RAPL", "m", "package/DRAM/powerwell counters"),
    ("power", "CONFIG_AMD_HSMP?", "y", "the OEM panel for TDP on some handheld mainboards"),
    ("power", "CONFIG_HSMP_ACPI?", "y", "same driver, older naming"),

    # ------------------------------------------------------------ gpu: the uc variant's whole point
    ("gpu", "CONFIG_DRM_XE", "m", "Intel Arc on the new driver, as a module so the -uc image can boot anything"),
    ("gpu", "CONFIG_DRM_XE_ENABLE_SCHED_FAILOVER?", "y", "not a symbol in this tree: xe resets the whole device, so this is a wish not a default"),
    ("gpu", "CONFIG_DRM_NOUVEAU", "m", "GDDR5-era NVIDIA: no kernel driver support for those, so Nouveau is the plan"),
    ("gpu", "CONFIG_DRM_NOUVEAU_KMD?", "y", "the in-tree kernel-mode driver split in newer trees"),
    ("gpu", "CONFIG_DRM_VMWGFX", "y", "so the -uc image and a VM on it behave the same"),
    ("gpu", "CONFIG_DRM_VIRTIO_GPU", "y", "the same reason, on KVM"),
    ("gpu", "CONFIG_DRM_BOCHS", "y", "QEMU default display: without it a rehearsal VM has no console"),
    ("gpu", "CONFIG_DRM_AST", "y", "every server-class board's BMC output"),
    ("gpu", "CONFIG_DRM_SIMPLEDRM", "y", "the handshake that keeps a splash on screen until the real driver loads"),
    ("gpu", "CONFIG_DRM_CLIENT", "y", "so a framebuffer console exists over DRM rather than beside it"),
    ("gpu", "CONFIG_DRM_TTM", "y", "the VRAM manager amdgpu/nouveau/xe all share"),
    ("gpu", "CONFIG_DRM_GPUVM?", "y", "some trees gate multi-GPU VRAM sharing behind this"),
    ("gpu", "CONFIG_IOMMU_SVA", "y", "shared virtual addressing, which is how compute and graphics share one address space"),
    ("gpu", "CONFIG_PCI_PRI", "y", "page request interface, required by SVA"),
    ("gpu", "CONFIG_PCI_PASID", "y", "and its partner"),
    ("gpu", "CONFIG_PCI_ATS", "y", "address translation services"),

    # ------------------------------------------------------------ sound
    ("sound", "CONFIG_SND_HDA_INTEL", "y", "built in, not modular: the -uc image must have audio before the drivers load"),
    ("sound", "CONFIG_SND_HDA_INTEL_DEEP_PROBE?", "y", "componentise was folded in; deep probe is what replaced the ordering hacks"),
    ("sound", "CONFIG_SND_HDA_TEGRA?", "y", "the HD-Audio controller on a Switch-shaped SoC"),
    ("sound", "CONFIG_SND_USB_AUDIO", "y", "USB headsets are the majority of gaming headsets"),
    ("sound", "CONFIG_SND_USB_AUDIO_QMIDI?", "y", "so a USB synth's MIDI port is not a second device to configure"),
    ("sound", "CONFIG_SND_SOC_SOF_INTEL_HIFI_EP_IPC", "m", "SOFA/SOF on recent Intel laptop codecs"),
    ("sound", "CONFIG_SND_SOC_SOF_AML?", "y", "the older Intel audio firmware interface"),
    ("sound", "CONFIG_SND_SEQ_DUMMY", "m", "so a DAW and a launcher can both own MIDI"),
    ("sound", "CONFIG_SND_SOC_TOPOLOGY", "y", "SOF machine files load a topology; renamed from SND_TOPOLOGY"),

    # ------------------------------------------------------------ input: handheld hardware
    ("input", "CONFIG_TOUCHSCREEN_DIMAX_MT?", "y", "the panel on a couple of handhelds"),
    ("input", "CONFIG_TOUCHSCREEN_GOODIX", "y", "and on most of the others"),
    ("input", "CONFIG_TOUCHSCREEN_EDT_FT5X06", "m", "Steam Deck LCD digitizer"),
    ("input", "CONFIG_HID_MAGICI2C?", "y", "Magic Keyboard/Trackpad over I2C on a Surface-type detachable"),
    ("input", "CONFIG_HID_PLAYSTATION", "y", "DualSense over USB and BT, including the gyro"),
    # CONFIG_HID_XBOX360 is gone in this tree: XInput support lives in JOYSTICK_XPAD below,
    # with _FF and _LEDS as separate symbols. The old name is not repeated here as a "wish" —
    # it can never be fulfilled, and an unfulfillable entry is how a spec starts lying.
    ("input", "CONFIG_JOYSTICK_XPAD", "y", "XInput controllers"),
    ("input", "CONFIG_JOYSTICK_XPAD_FF", "y", "rumble"),
    ("input", "CONFIG_JOYSTICK_XPAD_LEDS", "y", "the guide-button light, which players use to identify their pad"),
    ("input", "CONFIG_SERIO_I8042", "y", "the PS/2 port is how a lot of BIOS-era keyboards still arrive"),
    ("input", "CONFIG_KEYBOARD_GPIO", "y", "Deck/ROG-style buttons (guide, QAM, volume) are GPIOs"),
    ("input", "CONFIG_KEYBOARD_ADC?", "y", "some handhelds put them on an ADC divider instead"),
    ("input", "CONFIG_INPUT_JOYDEV", "y", "legacy /dev/js for the games that predate SDL's evdev path"),

    # ------------------------------------------------------------ net
    ("net", "CONFIG_XDP_SOCKETS", "y", "XDP sockets, for the low-latency path and for replay capture"),
    ("net", "CONFIG_NET_DEVMEM", "y", "device memory: zero-copy between a NIC/GPU and a socket"),
    ("net", "CONFIG_WIREGUARD", "y", "a LAN over a café network is a normal thing for a laptop gamer to want"),
    ("net", "CONFIG_INET_DIAG", "y", "ss(8), which hedron-doctor uses to spot a stuck peer"),
    ("net", "CONFIG_NET_SCH_FQ_CODEL", "y", "the default qdisc is what keeps a download from costing 40 ms of ping"),
    ("net", "CONFIG_NET_SCH_CAKE", "m", "the alternative, for the users who ask"),
    ("net", "CONFIG_MAC80211_MESH", "y", "-uc image: a LAN party is a mesh"),
    ("net", "CONFIG_BT_HIDP", "y", "Bluetooth HID devices over the profile some controllers insist on"),

    # ------------------------------------------------------------ fs: what the installer assumes
    ("fs", "CONFIG_BTRFS_FS", "y", "built in: the install default, with subvolumes and zstd, is not optional"),
    ("fs", "CONFIG_BTRFS_ASSERT", "y", "a corrupted FS should stop rather than scribble over a save-game directory"),
    ("fs", "CONFIG_F2FS_FS", "y", "the SD-card and eMMC case"),
    ("fs", "CONFIG_EXFAT_FS", "y", "the format every camera and capture card formats its media as"),
    ("fs", "CONFIG_UDF_FS", "y", "reading a pressed disc or a camera card"),
    ("fs", "CONFIG_NLS_UTF8", "y", "and the code pages a Windows dual-boot leaves on a shared partition"),
    ("fs", "CONFIG_9P_FS", "y", "shared folders with a VM, which is how a rehearsal sees the files"),
    ("fs", "CONFIG_NETWORK_FILESYSTEMS", "y", "explicit, because a defconfig with no network fs is a confusing support ticket"),
    ("fs", "CONFIG_AUTOFS_FS", "y", "systemd's mount machinery and home directories depend on it"),
    ("fs", "CONFIG_FSCACHE", "y", "the read-side cache for network homes"),

    # ------------------------------------------------------------ block
    ("block", "CONFIG_BLK_DEV_LOOP", "y", "the installer mounts a squashfs and a loop device, so built in"),
    ("block", "CONFIG_BLK_DEV_NBD", "m", "network block device, for a rehearsal that streams a disk image"),
    ("block", "CONFIG_BLK_DEV_RAM", "y", "an initrd that needs a second RAM disk (mkinitcpio hooks)"),
    ("block", "CONFIG_DM_VERITY", "y", "the read-only OSTree-style fallback root, if we ever ship one"),
    ("block", "CONFIG_DM_SWITCHEROO_ROOT?", "y", "some trees name the hybrid-graphics root device after the switch"),
    ("block", "CONFIG_EROFS_FS", "y", "the compressed read-only alternative to squashfs for /live"),
    ("block", "CONFIG_ZRAM_WRITEBACK", "y", "spill compressed pages to a swapfile instead of recompressing forever"),
    ("block", "CONFIG_BLK_WBT", "y", "writeback throttling: without it a copy costs you 30 ms of input lag"),

    # ------------------------------------------------------------ crypto / security
    ("security", "CONFIG_CRYPTO_USER_API", "m", "so userspace (LUKS, WireGuard tests) can use the hardware path"),
    ("security", "CONFIG_IMA", "y", "integrity measurement, the half of Secure Boot that gives you a log"),
    ("security", "CONFIG_INTEGRITY_PLATFORM_KEYRING", "y", "MOK enrolment for a user who signed their own driver"),
    ("security", "CONFIG_LOCK_DOWN_KERNEL_FEATURE?", "y", "how lockdown is exposed in some trees"),
    ("security", "CONFIG_TEE", "y", "OP-TEE on the ARM handhelds"),
    # DRM_LEASE stopped being a config symbol (lease is unconditional in DRM now); nothing to set.

    # ------------------------------------------------------------ sensors / platform
    ("sensors", "CONFIG_SENSORS_K8TEMP", "m", "the temperature source on every AMD handheld and desktop"),
    ("sensors", "CONFIG_SENSORS_K10TEMP", "m", "and its newer sibling"),
    ("sensors", "CONFIG_SENSORS_CORETEMP", "m", "Intel package temperature"),
    ("sensors", "CONFIG_SENSORS_AMC6821?", "y", "the fan controller on a couple of mainboards"),
    ("sensors", "CONFIG_HWMON_VID", "y", "rail voltages, which is what a user means by 'is my PSU sagging'"),
    ("sensors", "CONFIG_PFR_MMR?", "y", "platform firmware resilience, mostly a server thing but harmless"),
    ("platform", "CONFIG_SURFACE_aggregator?", "y", "Surface detachables: keyboard, battery, touch"),
    ("platform", "CONFIG_IDEAPAD_SLID?", "y", "the slider laptops and their EC buttons"),
    ("platform", "CONFIG_AMD_PMF", "y", "the PMF/MP2 telemetry that powers the automatic fan profiles"),
    ("platform", "CONFIG_INTEL_HID_EVENT", "m", "hybrid-button events on Intel reference designs"),
    ("platform", "CONFIG_ASUS_WMI", "m", "ROG Ally / Z13 fan and brightness controls"),
    ("platform", "CONFIG_GPD_FAN?", "y", "GPD handheld fan curve driver, where it exists"),
    ("platform", "CONFIG_WISTROM_BOX?", "y", "the old pre-UEFI Intel boards some reviewers still test on"),
]
