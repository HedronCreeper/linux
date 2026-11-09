# SPDX-License-Identifier: MIT
"""Small shared helpers: TOML, JSON, hashing, ANSI, tables, process execution."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess  # noqa: S602 - used only by explicit --exec code paths
import sys
import tomllib
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

TOOL_ERROR = 2


@dataclass
class Check:
    """A single verification result."""

    name: str
    ok: bool
    detail: str = ""
    hint: str = ""
    severity: str = "error"  # error | warn | info

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail,
                "hint": self.hint, "severity": self.severity}

    def line(self, color: bool = False) -> str:
        mark = "PASS" if self.ok else ("WARN" if self.severity == "warn" else "FAIL")
        if self.ok and self.severity == "info":
            mark = "INFO"
        col = {"PASS": "\033[32m", "WARN": "\033[33m", "FAIL": "\033[31m", "INFO": "\033[36m"}[mark]
        head = f"{col}{mark:<4}\033[0m" if color else f"{mark:<4}"
        out = [f"{head} {self.name}"]
        if self.detail:
            out.append(f"     {self.detail}")
        if not self.ok and self.hint:
            out.append(f"     → {self.hint}")
        return "\n".join(out)


# ----------------------------------------------------------------------- files
def read_text(path: str | os.PathLike[str]) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def read_bytes(path: str | os.PathLike[str]) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def load_toml(path: str | os.PathLike[str]) -> dict[str, Any]:
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def load_json(path: str | os.PathLike[str]) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def dump_json(obj: Any, path: str | os.PathLike[str] | None = None, indent: int = 1) -> str:
    text = json.dumps(obj, indent=indent, sort_keys=True) + "\n"
    if path:
        os.makedirs(os.path.dirname(os.path.abspath(str(path))) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return text


def sha256_file(path: str | os.PathLike[str]) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def short_hash(hexstr: str, n: int = 12) -> str:
    return hexstr[:n] if hexstr else "0" * n


def iter_files(root: str | os.PathLike[str], *, skip: Sequence[str] = ()) -> Iterable[str]:
    """Deterministic (sorted) relative-path walk of a directory tree."""
    root = os.fspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in {".git", "__pycache__"})
        for name in sorted(filenames):
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            if any(rel.startswith(s) for s in skip):
                continue
            yield rel


def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` onto ``base`` (lists append, dicts merge)."""
    out: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        if key.startswith("__delete"):
            for name in as_list(value):
                out.pop(name, None)
            continue
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = deep_merge(out[key], value)
        elif key in out and isinstance(out[key], list) and isinstance(value, list):
            merged = list(out[key])
            for item in value:
                if item not in merged:
                    merged.append(item)
            out[key] = merged
        else:
            out[key] = value
    return out


# ------------------------------------------------------------------------ misc
def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def norm_id(value: str | int) -> int:
    """Accept '0x744c', '744c', 29772 → 29772."""
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    if not text:
        raise ValueError("empty device id")
    return int(text, 16)


def hex_id(value: int) -> str:
    return "0x%04x" % value


def human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def table(rows: Sequence[Sequence[Any]], headers: Sequence[str] | None = None,
          sep: str = "  ") -> str:
    cells = [[str(c) for c in row] for row in rows]
    if headers:
        cells.insert(0, [str(h) for h in headers])
    if not cells:
        return ""
    widths = [max(len(r[i]) for r in cells) for i in range(len(cells[0]))]
    lines = [sep.join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in cells]
    if headers:
        lines.insert(1, sep.join("-" * w for w in widths))
    return "\n".join(lines)


def which(prog: str) -> str | None:
    from shutil import which as _which
    return _which(prog)


def run(cmd: Sequence[str], *, cwd: str | None = None, timeout: float | None = None,
        env: Mapping[str, str] | None = None, check: bool = False) -> subprocess.CompletedProcess:
    """Run a command, capture output, never raise unless check=True."""
    argv = [str(c) for c in cmd]
    try:
        proc = subprocess.run(argv, cwd=cwd, timeout=timeout, env=dict(env) if env else None,
                              capture_output=True, text=True)
    except FileNotFoundError as exc:
        proc = subprocess.CompletedProcess(argv, 127, "", f"not found: {exc}")
    except subprocess.TimeoutExpired as exc:
        proc = subprocess.CompletedProcess(argv, 124, str(exc.stdout or ""), "timed out")
    if check and proc.returncode != 0:
        raise RuntimeError(f"{argv[0]} failed ({proc.returncode}): {proc.stderr.strip()[:400]}")
    return proc


def eprint(*args: Any, **kwargs: Any) -> None:
    print(*args, file=sys.stderr, **kwargs)


def parse_keyvalue(path: str | os.PathLike[str], *, cast: bool = True) -> dict[str, Any]:
    """Parse `key=value` config (profile .conf files). Handles quotes and comments."""
    out: dict[str, Any] = {}
    for raw in read_text(path).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #")[0].split("\t#")[0].strip()
        if cast and value.isdigit():
            out[key] = int(value)
        else:
            out[key] = value
    return out


def to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def unique(items: Iterable[Any]) -> list[Any]:
    seen: set[Any] = set()
    out: list[Any] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def unit_name(name: str) -> str:
    return name if "." in name else name + ".service"


def mask(text: str, width: int = 78) -> str:
    """Wrap long strings for terminal output without breaking words."""
    import textwrap
    return "\n".join(textwrap.wrap(text, width)) or text
