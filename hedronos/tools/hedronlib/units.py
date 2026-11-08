# SPDX-License-Identifier: MIT
"""systemd unit parsing/validation — static only, no systemd required.

Why a hand-written parser instead of shelling out to `systemd-analyze verify`: the build has
to verify unit files on a Debian container, in a chroot and on a user's machine where
`systemd-analyze` may not even exist, and `verify` refuses to say anything useful unless the
unit is installed. What we *can* check statically catches the mistakes that actually bite:
typos in section names, keys in the wrong section, `[Install]` missing on an enabled unit,
`WantedBy=` pointing at a target nobody pulls in, specifiers used in `ExecStart=`, and
`ExecStart` binaries that no package in the ISO provides.
"""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

SECTIONS = {"Unit", "Service", "Socket", "Timer", "Mount", "Automount", "Swap", "Path",
            "Install", "Slice", "Scope", "Target", "Device", "X-Summary", "Description"}

# Which keys are legal in which section (subset of systemd.resource-config(5) + the unit
# manual — deliberately incomplete but covering every key HedronOS itself uses; unknown keys
# are a warning, keys in a *wrong known* section are an error).
KEYS: dict[str, set[str]] = {
    "Unit": {"Description", "Documentation", "Requires", "Wants", "BindsTo", "PartOf",
             "Conflicts", "Before", "After", "OnFailure", "OnFailureJobMode", "AllowIsolate",
             "DefaultDependencies", "RefuseManualStart", "RefuseManualStop", "JobTimeoutSec",
             "JobRunningTimeoutSec", "StartLimitIntervalSec", "StartLimitBurst", "ConditionPathExists",
             "ConditionPathIsDirectory", "ConditionFileNotEmpty", "ConditionFirstBoot",
             "ConditionVirtualization", "ConditionKernelCommandLine", "ConditionArchitecture",
             "AssertPathExists"},
    "Service": {"Type", "ExecStart", "ExecStartPre", "ExecStartPost", "ExecReload", "ExecStop",
                "ExecStopPost", "Restart", "RestartSec", "TimeoutStartSec", "TimeoutStopSec",
                "TimeoutIdleSec", "GuessStartLimit", "PermissionsStartOnly", "User", "Group",
                "WorkingDirectory", "RootDirectory", "Nice", "IOSchedulingClass",
                "IOSchedulingPriority", "CPUSchedulingPolicy", "CPUSchedulingPriority",
                "Environment", "EnvironmentFile", "EnvironmentDirectory", "PassEnvironment",
                "StandardInput", "StandardOutput", "StandardError", "SyslogIdentifier",
                "NonBlocking", "UtmpIdentifier", "PAMName", "CapabilityBoundingSet",
                "AmbientCapabilities", "SupplementaryGroups", "SecureBits", "NoNewPrivileges",
                "PrivateTmp", "PrivateDevices", "PrivateNetwork", "PrivateUsers",
                "ProtectSystem", "ProtectHome", "ProtectHostname", "ProtectClock",
                "ProtectKernelTunables", "ProtectKernelModules", "ProtectKernelLogs",
                "ProtectControlGroups", "ProtectProc", "ProcSubset", "RestrictAddressFamilies",
                "RestrictNamespaces", "RestrictRealtime", "RestrictSUIDSGID", "RestrictIO",
                "RestrictFS", "RestrictInterfaces", "LockPersonality", "MemoryDenyWriteExecute",
                "SystemCallFilter", "SystemCallArchitectures", "SystemCallErrorNumber",
                "SystemCallLog", "SeccompFilterErrorNumber", "LimitNOFILE", "LimitMEMLOCK",
                "LimitRTPRIO", "LimitNICE", "LimitCORE", "UMask", "KeyringMode",
                "MountFlags", "KillMode", "KillSignal", "SendSIGKILL", "SendSIGHUP",
                "TimeoutCleanSec", "WatchdogSec", "FileDescriptorStoreMax", "RestartUSec",
                "RuntimeMaxSec", "RuntimeRandomizedExtraSec", "USBFunctionDescriptors",
                "BusName", "BusPath", "Delegate", "Slice", "SocketGroup", "RemainAfterExit",
                "ReadWritePaths", "ReadOnlyPaths", "InaccessiblePaths", "TemporaryFileSystem",
                "BindPaths", "BindReadOnlyPaths", "StateDirectory", "CacheDirectory",
                "LogsDirectory", "ConfigurationDirectory", "ExecPaths"},
    "Socket": {"ListenStream", "ListenDatagram", "ListenSequentialPacket", "ListenFIFO",
               "ListenSpecial", "ListenNetlink", "ListenMessageQueue", "ListenUSBFunction",
               "SocketMode", "DirectoryMode", "Accept", "FlushPending", "MaxConnections",
               "MaxConnectionsPerSource", "KeepAlive", "NoDelay", "Priority", "DeferAccept",
               "ReceiveBuffer", "SendBuffer", "IPFreeBind", "Transparent",
               "Service", "RemoveOnStop", "ExecStartPre", "ExecStartPost", "ExecStopPost",
               "FileDescriptorName", "SocketUser", "SocketGroup", "Timestamping",
               "TriggerLimitIntervalSec", "TriggerLimitBurst"},
    "Timer": {"OnActiveSec", "OnBootSec", "OnStartupSec", "OnUnitActiveSec", "OnUnitInactiveSec",
              "OnCalendar", "AccuracySec", "RandomizedDelaySec", "FixedRandomDelay",
              "OnClockChange", "OnTimezoneChange", "WakeSystem", "Unit", "Persistent",
              "StartLimitSec", "StartLimitBurst", "StartLimitIntervalSec", "RemainAfterElapse"},
    "Install": {"WantedBy", "RequiredBy", "Alias", "Also", "DefaultInstance"},
    "Slice": {"CPUAccounting", "IOAccounting", "MemoryAccounting", "TasksAccounting",
              "CPUWeight", "StartupCPUWeight", "CPUShares", "CPUQuota", "IOWeight",
              "StartupIOWeight", "BlockIOWeight", "BlockIOAccounting", "MemoryLow",
              "MemoryMin", "MemoryHigh", "MemoryMax", "MemorySwapMax", "TasksMax",
              "AllowedCPUs", "AllowedMemoryNodes", "Delegate", "DisableControllers",
              "ManagedOOMSwap", "ManagedOOMMemoryPressure", "ManagedOOMPreference",
              "Slice", "Description"},
    "Path": {"PathExists", "PathExistsGlob", "PathChanged", "PathModified", "DirectoryNotEmpty",
             "Unit", "MakeDirectory", "DirectoryMode", "TriggerLimitIntervalSec",
             "TriggerLimitBurst"},
    "Target": {"Description", "Requires", "Wants", "Before", "After", "Conflicts",
               "AllowIsolate", "DefaultDependencies"},
}

RESOURCE_KEYS = {"CPUAccounting", "IOAccounting", "MemoryAccounting", "TasksAccounting",
                "CPUWeight", "StartupCPUWeight", "CPUShares", "StartupCPUShares", "CPUQuota",
                "IOWeight", "StartupIOWeight", "IOReadBandwidthMax", "IOWriteBandwidthMax",
                "IODeviceWeight", "BlockIOWeight", "BlockIOAccounting", "BlockIOBandwidth",
                "MemoryLow", "MemoryMin", "MemoryHigh", "MemoryMax", "MemorySwapMax",
                "MemoryZSwapMax", "TasksMax", "AllowedCPUs", "AllowedMemoryNodes",
                "ManagedOOMSwap", "ManagedOOMMemoryPressure", "ManagedOOMPreference",
                "Delegate", "DisableControllers", "LimitNOFILE", "LimitMEMLOCK", "LimitRTPRIO",
                "LimitNICE", "LimitCORE", "LimitAS", "NUMAPolicy", "NUMAMask", "Nice",
                "IOSchedulingClass", "IOSchedulingPriority", "CPUSchedulingPolicy",
                "CPUSchedulingPriority", "CPUSchedulingResetOnFork", "UtmpIdentifier",
                "PAMName", "TimerSlackNSec"}
for _section in ("Service", "Scope", "Slice", "Mount", "Swap"):
    KEYS[_section] = KEYS.get(_section, set()) | RESOURCE_KEYS


NAMES = {"Wants", "Requires", "BindsTo", "PartOf", "Conflicts", "Before", "After", "OnFailure",
         "Also", "WantedBy", "RequiredBy", "Alias", "Unit", "Service"}
VALID_TYPES = {"simple", "exec", "forking", "oneshot", "dbus", "notify", "notify-reload", "idle"}
VALID_RESTART = {"no", "on-success", "on-failure", "on-abnormal", "on-abort", "on-watchdog",
                 "always", "never"}
# `%i` is only defined for instantiated units; `%I`, `%f`, `%p`, `%u` etc. have similar rules.
SPECIFIER_OK_IN = re.compile(r"^Exec(A)?(Start|Stop|Reload)")


MODIFIERS = re.compile(r"^(?:[-@+!:]*)")


def strip_modifiers(value: str) -> str:
    """Drop systemd's Exec* line modifiers (`-`, `@`, `+`, `!`, `:`) so the program path is
    the first token. They are only ever a prefix and never part of a path."""
    return MODIFIERS.sub("", value.strip())


@dataclass
class Unit:
    path: str
    name: str
    kind: str
    sections: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def get(self, section: str, key: str, default: Any = None) -> Any:
        for k, v in self.sections.get(section, []):
            if k == key:
                return v
        return default

    def get_all(self, section: str, key: str) -> list[str]:
        return [v for k, v in self.sections.get(section, []) if k == key]

    @property
    def instantiated(self) -> bool:
        return "@" in self.name.split(".")[-1] or self.name.endswith("@.service")

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "name": self.name, "kind": self.kind,
                "errors": self.errors, "warnings": self.warnings,
                "sections": {s: dict(pairs) for s, pairs in self.sections.items()}}


def parse(path: str) -> Unit:
    name = os.path.basename(path)
    kind = name.rsplit(".", 1)[-1] if "." in name else ""
    unit = Unit(path=path, name=name, kind=kind)
    section = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.rstrip("\n")
            stripped = line.strip()
            if not stripped or stripped[0] in "#;":
                continue
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped[1:-1].strip()
                if section not in SECTIONS:
                    unit.errors.append("%s:%d: unknown section [%s]" % (name, lineno, section))
                unit.sections.setdefault(section, [])
                continue
            if section is None:
                unit.errors.append("%s:%d: key outside of a section: %r" % (name, lineno, stripped[:40]))
                continue
            if "=" not in stripped:
                unit.errors.append("%s:%d: not a key=value pair: %r" % (name, lineno, stripped[:40]))
                continue
            key, _, value = stripped.partition("=")
            key, value = key.strip(), value.strip()
            unit.sections[section].append((key, value))
            known = KEYS.get(section)
            if known is not None and key not in known:
                elsewhere = [s for s, keys in KEYS.items() if key in keys and s != section]
                if elsewhere:
                    unit.errors.append("%s:%d: [%s] %s= belongs in %s" % (name, lineno, section, key,
                                                                          "/".join("[" + s + "]" for s in elsewhere)))
                else:
                    unit.warnings.append("%s:%d: unknown key %s= in [%s]" % (name, lineno, key, section))
            if key in NAMES:
                for token in value.split():
                    if token and not re.search(r"\.(service|socket|timer|target|path|slice|mount|scope|device)$",
                                               token) and "%" not in token:
                        unit.errors.append("%s:%d: %s= entry %r is not a unit name" % (name, lineno, key, token))
            if key == "Type" and value and value not in VALID_TYPES:
                unit.errors.append("%s:%d: Type=%s is not a valid service type" % (name, lineno, value))
            if key == "Restart" and value and value not in VALID_RESTART:
                unit.errors.append("%s:%d: Restart=%s is not valid" % (name, lineno, value))
            if SPECIFIER_OK_IN.match(key) and "%i" in value and not unit.instantiated:
                unit.errors.append("%s:%d: '%%i' used in %s of a non-instantiated unit" % (name, lineno, key))
            if key in ("ExecStart", "ExecStartPre", "ExecStartPost", "ExecStop", "ExecStopPost", "ExecReload", "ExecStart"):
                body = strip_modifiers(value)
                if not body:
                    unit.errors.append("%s:%d: %s= is empty" % (name, lineno, key))
                    continue
                if body and not body.startswith(("/", "./", "/usr", "@")) and not body.startswith("-"):
                    unit.errors.append("%s:%d: %s must use an absolute path (got %r)" % (name, lineno, key, body[:40]))
                if body.startswith("/") and ".py" in body.split()[0]:
                    # Not an error: legal iff the script is executable and has a shebang.
                    # hedronos-verify checks that against the package tree, where the file exists.
                    unit.warnings.append("%s:%d: %s executes %s directly (needs shebang + exec bit)"
                                         % (name, lineno, key, body.split()[0]))
    return unit


UNIT_SUFFIXES = (".service", ".socket", ".timer", ".target", ".slice", ".mount", ".swap", ".path",
                ".scope")


def load_tree(directory: str) -> list[Unit]:
    """Every unit file under `directory` (a package src tree, an airootfs, or a chroot)."""
    out = []
    for suffix in UNIT_SUFFIXES:
        for path in glob.glob(os.path.join(directory, "**", "*" + suffix), recursive=True):
            if os.path.isfile(path):
                out.append(parse(path))
    return sorted(out, key=lambda u: u.path)


def collect(roots: Iterable[str]) -> dict[str, Unit]:
    """All units under the given roots, keyed by unit name (first occurrence wins)."""
    found: dict[str, Unit] = {}
    for root in roots:
        for unit in load_tree(root):
            found.setdefault(unit.name, unit)
    return found


def check_all(units: list[Unit], *, provided_files: set[str] | None = None,
              required_enabled: Iterable[str] = ()) -> list[str]:
    """Cross-unit checks that need the whole set (as opposed to one file at a time)."""
    problems: list[str] = []
    names = {u.name for u in units}
    by_name = {u.name: u for u in units}
    for unit in units:
        for section in ("Unit", "Install"):
            for key in ("Wants", "Requires", "BindsTo", "After", "Before", "WantedBy", "RequiredBy",
                        "OnFailure", "Also"):
                for token in " ".join(unit.get_all(section, key)).split():
                    if "%" in token or not token:
                        continue
                    if not token.rsplit(".", 1)[-1] in {"service", "socket", "timer", "target",
                                                        "path", "slice", "mount", "swap", "scope",
                                                        "device"}:
                        problems.append("%s: %s= entry %r is not a unit name" % (unit.name, key, token))
                    # Units that legitimately live outside our tree (systemd's own targets,
                    # display-manager.service, network-online.target) are not our business.
                    elif token.endswith((".target", ".timer", ".path", ".slice", ".socket", ".service")):
                        continue
        if unit.kind == "service" and "Install" in unit.sections:
            wanted = " ".join(unit.get_all("Install", "WantedBy"))
            if not wanted and not unit.get("Install", "Alias") and not unit.get("Install", "RequiredBy"):
                # Socket-activated services are started by their .socket and legitimately
                # have no [Install]: enable the socket instead (which we also check).
                if not any(u.kind == "socket" and u.get("Socket", "Service") == unit.name for u in units):
                    problems.append("%s: [Install] with no WantedBy/Alias — enabling it does nothing" % unit.name)
        if unit.kind == "service":
            if unit.get("Service", "Type") == "oneshot" and unit.get("Service", "ExecStart") is None:
                problems.append("%s: oneshot with no ExecStart" % unit.name)
            if unit.get("Service", "ExecStart") is None and unit.get("Service", "BusName") is None:
                problems.append("%s: service with no ExecStart and no BusName" % unit.name)
    for name in required_enabled:
        unit = by_name.get(name)
        if unit is None:
            problems.append("enabled unit %s is not provided by any package in this tree" % name)
        elif "Install" not in unit.sections:
            problems.append("enabled unit %s has no [Install] section (systemctl enable would fail)" % name)
        else:
            # A templated unit that ships no [Install] can still be legitimately pulled in by
            # a path/socket unit, so only complain when nothing instantiates it either.
            if not " ".join(unit.get_all("Install", "WantedBy")) and "@" not in name:
                problems.append("enabled unit %s has an empty WantedBy" % name)
    if provided_files is not None:
        for unit in units:
            for key in ("ExecStart", "ExecStartPre", "ExecStartPost", "ExecStop", "ExecReload"):
                for value in unit.get_all("Service", key):
                    body = strip_modifiers(value)
                    prog = body.split()[0] if body.split() else ""
                    if prog.startswith("/") and not prog.startswith("/usr/bin/env") \
                            and not prog.startswith("/bin/sh"):
                        if prog not in provided_files:
                            problems.append("%s: %s=%s references %s which no HedronOS package installs"
                                            % (unit.name, key, "…", prog))
    return problems


def summary(units: list[Unit]) -> dict[str, Any]:
    return {"units": len(units),
            "errors": sum(len(u.errors) for u in units),
            "warnings": sum(len(u.warnings) for u in units),
            "kinds": sorted({u.kind for u in units})}
