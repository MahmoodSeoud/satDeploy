"""Tests for satdeploy.abi — ABI compat check before iterate uploads.

Uses the real satdeploy-agent ARM binary harvested by the Week 1 feasibility
harness when available (realistic DT_NEEDED: libparam, libcsp, libc). Falls
back to mocked readelf output so CI without a cross-toolchain still runs the
unit suite.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from satdeploy import abi, errors


# ---------------------------------------------------------------------------
# needed_libs — readelf parsing
# ---------------------------------------------------------------------------

_READELF_FIXTURE = """
Dynamic section at offset 0xde0 contains 28 entries:
  Tag        Type                         Name/Value
 0x0000000000000001 (NEEDED)             Shared library: [libparam.so.3]
 0x0000000000000001 (NEEDED)             Shared library: [libcsp.so]
 0x0000000000000001 (NEEDED)             Shared library: [libc.so.6]
 0x000000000000000c (INIT)               0x1000
 0x0000000000000015 (DEBUG)              0x0
"""


def test_needed_libs_parses_dt_needed_from_readelf_output(tmp_path):
    """Whatever readelf version we end up with, the DT_NEEDED rows always
    say `Shared library: [name]`. If someone drops pyelftools in later,
    this test guards the parser contract."""
    elf = tmp_path / "fake.elf"
    elf.write_bytes(b"\x7fELF")  # just needs to exist for the Path check
    with patch("subprocess.check_output", return_value=_READELF_FIXTURE):
        libs = abi.needed_libs(elf, readelf_bin="/usr/bin/readelf")
    assert libs == ["libparam.so.3", "libcsp.so", "libc.so.6"]


def test_needed_libs_returns_empty_when_readelf_not_installed(tmp_path):
    """abi is a dev convenience, not a safety gate. Missing readelf should
    degrade gracefully — the agent's SHA256 verify still catches real bad
    deploys."""
    elf = tmp_path / "fake.elf"
    elf.write_bytes(b"\x7fELF")
    with patch("shutil.which", return_value=None):
        assert abi.needed_libs(elf, readelf_bin="readelf") == []


def test_needed_libs_raises_when_elf_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        abi.needed_libs(tmp_path / "does-not-exist.elf")


def test_needed_libs_wraps_readelf_failure(tmp_path):
    """Corrupt ELF → readelf exits non-zero. Convert to RuntimeError with
    readelf's stderr in the message so the user sees what actually broke."""
    elf = tmp_path / "corrupt.elf"
    elf.write_bytes(b"definitely not an ELF")
    exc = subprocess.CalledProcessError(1, "readelf", stderr="Not an ELF file")
    with patch("shutil.which", return_value="/usr/bin/readelf"), \
         patch("subprocess.check_output", side_effect=exc):
        with pytest.raises(RuntimeError, match="Not an ELF"):
            abi.needed_libs(elf)


# ---------------------------------------------------------------------------
# _lib_present_in_sysroot — layout discovery
# ---------------------------------------------------------------------------

def test_lib_present_finds_file_in_usr_lib(tmp_path):
    (tmp_path / "usr" / "lib").mkdir(parents=True)
    (tmp_path / "usr" / "lib" / "libparam.so.3").touch()
    assert abi._lib_present_in_sysroot("libparam.so.3", tmp_path)


def test_lib_present_finds_file_in_lib64(tmp_path):
    (tmp_path / "lib64").mkdir()
    (tmp_path / "lib64" / "libc.so.6").touch()
    assert abi._lib_present_in_sysroot("libc.so.6", tmp_path)


def test_lib_present_returns_false_when_absent(tmp_path):
    (tmp_path / "usr" / "lib").mkdir(parents=True)
    (tmp_path / "usr" / "lib" / "libfoo.so").touch()
    assert not abi._lib_present_in_sysroot("libparam.so.3", tmp_path)


# ---------------------------------------------------------------------------
# missing_libs — the full diff
# ---------------------------------------------------------------------------

def _stub_sysroot(root: Path, libs_present: list[str]) -> Path:
    """Create a fake sysroot layout with the given libs under usr/lib."""
    lib_dir = root / "usr" / "lib"
    lib_dir.mkdir(parents=True)
    for lib in libs_present:
        (lib_dir / lib).touch()
    return root


def test_missing_libs_finds_absent_libs(tmp_path):
    elf = tmp_path / "controller"
    elf.write_bytes(b"\x7fELF")
    sysroot = _stub_sysroot(tmp_path / "sysroot", ["libcsp.so", "libc.so.6"])
    with patch("subprocess.check_output", return_value=_READELF_FIXTURE):
        missing = abi.missing_libs(elf, sysroot)
    # libparam.so.3 is missing; libcsp + libc are present.
    assert [m.lib for m in missing] == ["libparam.so.3"]


def test_missing_libs_empty_when_all_present(tmp_path):
    elf = tmp_path / "controller"
    elf.write_bytes(b"\x7fELF")
    sysroot = _stub_sysroot(
        tmp_path / "sysroot",
        ["libparam.so.3", "libcsp.so", "libc.so.6"],
    )
    with patch("subprocess.check_output", return_value=_READELF_FIXTURE):
        assert abi.missing_libs(elf, sysroot) == []


def test_missing_libs_raises_when_sysroot_missing(tmp_path):
    elf = tmp_path / "controller"
    elf.write_bytes(b"\x7fELF")
    with pytest.raises(FileNotFoundError, match="sysroot not found"):
        abi.missing_libs(elf, tmp_path / "nonexistent-sysroot")


# ---------------------------------------------------------------------------
# check — the caller-facing raise path
# ---------------------------------------------------------------------------

def test_check_passes_silently_when_sysroot_has_all_libs(tmp_path):
    elf = tmp_path / "controller"
    elf.write_bytes(b"\x7fELF")
    sysroot = _stub_sysroot(
        tmp_path / "sysroot",
        ["libparam.so.3", "libcsp.so", "libc.so.6"],
    )
    with patch("subprocess.check_output", return_value=_READELF_FIXTURE):
        abi.check(elf, sysroot)  # should not raise


def test_check_raises_abi_error_with_fix_command(tmp_path):
    elf = tmp_path / "controller"
    elf.write_bytes(b"\x7fELF")
    sysroot = _stub_sysroot(tmp_path / "sysroot", [])  # nothing present
    with patch("subprocess.check_output", return_value=_READELF_FIXTURE):
        with pytest.raises(errors.ABIError) as excinfo:
            abi.check(elf, sysroot)
    err = excinfo.value
    assert err.exit_code == errors.EABI
    assert "libparam.so.3" in err.message
    assert err.fix_cmd == "satdeploy sync-sysroot"
    assert err.eta == "12s"


def test_check_message_uses_plural_for_multiple_missing(tmp_path):
    elf = tmp_path / "controller"
    elf.write_bytes(b"\x7fELF")
    sysroot = _stub_sysroot(tmp_path / "sysroot", ["libc.so.6"])
    with patch("subprocess.check_output", return_value=_READELF_FIXTURE):
        with pytest.raises(errors.ABIError) as excinfo:
            abi.check(elf, sysroot)
    # Two missing (libparam + libcsp) → "libraries" plural form.
    assert "libraries" in excinfo.value.message


# ---------------------------------------------------------------------------
# Real-binary integration (optional — gated on cross-compile artifact)
# ---------------------------------------------------------------------------

_REAL_AGENT_BINARY = (
    Path(__file__).resolve().parent.parent
    / "satdeploy-agent" / "build-arm" / "satdeploy-agent"
)


@pytest.mark.skipif(
    not _REAL_AGENT_BINARY.exists() or shutil.which("readelf") is None,
    reason="requires cross-compiled satdeploy-agent ARM binary + readelf",
)
def test_needed_libs_on_real_arm_agent_binary():
    """Sanity-check that we can parse a genuine aarch64 ELF with system
    readelf. Skipped gracefully when the cross-compile artifact is absent."""
    libs = abi.needed_libs(_REAL_AGENT_BINARY)
    # Every satdeploy-agent build links libc at minimum. If this returns
    # empty on a real ARM binary, readelf lost the ability to read aarch64.
    assert libs, "readelf produced no DT_NEEDED for real ARM agent binary"
    assert any(l.startswith("libc") for l in libs), (
        f"expected libc in DT_NEEDED, got {libs}"
    )
