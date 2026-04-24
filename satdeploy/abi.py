"""ABI compatibility check for `satdeploy iterate`.

Design doc (`docs/designs/vercel-for-cubesats.md`, line 71):

    Before upload, diff DT_NEEDED + symbol versions of local binary vs
    target sysroot. Fail loudly with "missing libparam.so.3 on target;
    rebuild against a matching SDK or add libparam to the Yocto image."

When `satdeploy sync-sysroot` ships (design doc line 152), the ABIError
fix_cmd below should flip to the one-command invocation.

Why this matters: iterate's gif demo dies the moment you push a binary
linked against a libparam ABI the target doesn't have. Debugging "service
won't restart" five minutes after a deploy is exactly the user experience
we're trying to kill. Catching it locally in <200 ms is the whole point.

Implementation notes
--------------------
* We use the system `readelf -d` rather than pulling in `pyelftools`. One
  less dep, works cross-arch (binutils readelf reads aarch64 ELFs on x86
  hosts without a cross-toolchain). The caller can pass an explicit
  `readelf_bin` to pin the cross-toolchain version if stricter matching is
  wanted (e.g. `aarch64-poky-linux-readelf`).
* If `readelf` is not on PATH, `missing_libs` returns an empty list and
  logs a warning rather than blocking iterate. ABI check is a dev
  convenience, not a safety requirement — the agent still verifies SHA256
  after apply, and service-won't-restart still routes through errors.py.
* Sysroot library lookup walks the standard Linux-FHS locations (lib/,
  usr/lib/, lib64/, usr/lib64/). Yocto SDK sysroots follow this layout.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

# Lines from `readelf -d` look like:
#     0x0000000000000001 (NEEDED) Shared library: [libparam.so.3]
_NEEDED_RE = re.compile(r"Shared library: \[([^\]]+)\]")

# Common locations the dynamic linker searches inside a Yocto sysroot.
# Ordered most-specific first.
_SYSROOT_LIB_DIRS = ("lib", "usr/lib", "lib64", "usr/lib64")


@dataclass(frozen=True)
class Missing:
    """One DT_NEEDED library the target sysroot doesn't have."""
    lib: str


def needed_libs(elf: Path, readelf_bin: str = "readelf") -> List[str]:
    """Return the DT_NEEDED shared libraries of ``elf``.

    Raises ``FileNotFoundError`` if ``elf`` doesn't exist. Raises
    ``RuntimeError`` if readelf is present but fails (corrupt ELF, wrong
    format). Returns [] if readelf is not on PATH — the caller decides
    how to handle that.
    """
    elf = Path(elf)
    if not elf.exists():
        raise FileNotFoundError(f"ELF not found: {elf}")
    if shutil.which(readelf_bin) is None:
        return []
    try:
        out = subprocess.check_output(
            [readelf_bin, "-d", str(elf)],
            text=True,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"readelf failed on {elf}: {e.stderr.strip() or e}"
        ) from e
    return _NEEDED_RE.findall(out)


def _lib_present_in_sysroot(lib: str, sysroot: Path) -> bool:
    """Return True iff ``lib`` exists under any standard sysroot lib dir."""
    for d in _SYSROOT_LIB_DIRS:
        candidate = sysroot / d / lib
        if candidate.exists():
            return True
    return False


def missing_libs(
    elf: Path,
    sysroot: Path,
    readelf_bin: str = "readelf",
) -> List[Missing]:
    """Return the libs ``elf`` requires that are absent from ``sysroot``.

    Empty list = ABI check passed. Non-empty = iterate should abort before
    upload so the user sees a typed error, not a service-won't-restart.
    """
    elf = Path(elf)
    sysroot = Path(sysroot)
    if not sysroot.exists():
        raise FileNotFoundError(f"sysroot not found: {sysroot}")

    needed = needed_libs(elf, readelf_bin=readelf_bin)
    missing: List[Missing] = []
    for lib in needed:
        if not _lib_present_in_sysroot(lib, sysroot):
            missing.append(Missing(lib))
    return missing


def check(
    elf: Path,
    sysroot: Path,
    readelf_bin: str = "readelf",
) -> None:
    """Raise ``ABIError`` if any required lib is absent from the sysroot.

    Returns ``None`` silently when the ABI check passes.

    Split from ``missing_libs`` so callers that only want the diff (e.g.
    ``satdeploy doctor``) can skip the raise path entirely.
    """
    # Local import so errors.py is not a hard dep of abi.py's leaf helpers
    # (keeps needed_libs / missing_libs usable by CLI tools with different
    # error conventions).
    from satdeploy.errors import ABIError

    missing = missing_libs(elf, sysroot, readelf_bin=readelf_bin)
    if not missing:
        return
    names = ", ".join(m.lib for m in missing)
    plural = "libraries" if len(missing) > 1 else "library"
    raise ABIError(
        f"Target sysroot missing {plural}: {names}",
        fix_cmd=f"Add {names} to the target Yocto image, or point $SATDEPLOY_SDK at the SDK matching this binary's build.",
        eta=None,
    )
