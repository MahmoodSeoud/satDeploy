"""Wrapper around elfutils ``debuginfod`` for the satdeploy dev loop.

``satdeploy debuginfod serve`` indexes the cross-compiled sysroots under
``~/.satdeploy/sysroots/`` and exposes them at ``http://localhost:8002`` so
that a local ``gdb`` (invoked via ``satdeploy gdb``) can auto-fetch
``.debug`` files and sources for any binary running on the target.

Concurrency-hardening (design-doc landmine P0 #2):

- ``fcntl.flock`` on ``~/.satdeploy/run/debuginfod.pid.lock`` serialises
  check-and-spawn, so two ``satdeploy watch`` terminals race safely.
- After reading the PID file we verify ``/proc/<pid>/comm == "debuginfod"``
  before treating the server as live. Kills the TOCTOU window where a PID
  has been reused by an unrelated process.
- ``stop()`` polls ``/proc/<pid>`` until the kernel reaps the process,
  preventing the "new serve() fails to bind because old debuginfod hasn't
  released the socket yet" race.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

DEBUGINFOD_PORT = 8002
DEBUGINFOD_URL = f"http://localhost:{DEBUGINFOD_PORT}"

SATDEPLOY_HOME = Path.home() / ".satdeploy"
DEFAULT_SYSROOTS_DIR = SATDEPLOY_HOME / "sysroots"
RUN_DIR = SATDEPLOY_HOME / "run"
PID_FILE = RUN_DIR / "debuginfod.pid"
LOCK_FILE = RUN_DIR / "debuginfod.pid.lock"
DB_FILE = RUN_DIR / "debuginfod.sqlite"

_EXIT_POLL_INTERVAL = 0.05
_EXIT_POLL_TIMEOUT = 5.0


class DebuginfodError(Exception):
    """Raised when debuginfod can't be started or located."""


def _read_pid() -> Optional[int]:
    try:
        return int(PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _is_debuginfod_running(pid: int) -> bool:
    """True iff ``pid`` is alive and ``/proc/<pid>/comm`` is ``debuginfod``."""
    try:
        comm = Path(f"/proc/{pid}/comm").read_text().strip()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return False
    return comm == "debuginfod"


def _which_debuginfod() -> Optional[str]:
    return shutil.which("debuginfod")


def _wait_for_exit(pid: int, timeout: float = _EXIT_POLL_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not Path(f"/proc/{pid}").exists():
            return True
        time.sleep(_EXIT_POLL_INTERVAL)
    return not Path(f"/proc/{pid}").exists()


def _unlink_missing_ok(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _acquire_lock():
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.touch()
    fh = open(LOCK_FILE, "r+")
    fcntl.flock(fh, fcntl.LOCK_EX)
    return fh


def status() -> Optional[int]:
    """Return the PID of a live debuginfod, or ``None``. Cleans stale PID files."""
    pid = _read_pid()
    if pid is None:
        return None
    if _is_debuginfod_running(pid):
        return pid
    _unlink_missing_ok(PID_FILE)
    return None


def serve(
    sysroots_dir: Path = DEFAULT_SYSROOTS_DIR,
    port: int = DEBUGINFOD_PORT,
) -> int:
    """Start (or reuse) debuginfod serving sysroots_dir. Returns the PID.

    Idempotent under concurrent callers: ``fcntl.flock`` serialises the
    check-and-spawn, and a stale PID from a dead process is cleaned up.
    """
    binary = _which_debuginfod()
    if binary is None:
        raise DebuginfodError(
            "debuginfod not found on PATH. Install it with:\n"
            "  apt install elfutils        # Debian/Ubuntu\n"
            "  dnf install elfutils        # Fedora/RHEL\n"
            "  brew install elfutils       # macOS"
        )

    sysroots_dir = Path(sysroots_dir)
    sysroots_dir.mkdir(parents=True, exist_ok=True)

    lock_fh = _acquire_lock()
    try:
        existing = _read_pid()
        if existing is not None and _is_debuginfod_running(existing):
            return existing
        if existing is not None:
            _unlink_missing_ok(PID_FILE)

        cmd = [
            binary,
            "-d", str(DB_FILE),
            "-F", str(sysroots_dir),
            "-p", str(port),
        ]
        proc = subprocess.Popen(
            cmd,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        PID_FILE.write_text(str(proc.pid))
        return proc.pid
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()


def stop() -> bool:
    """Stop the running debuginfod. Returns ``True`` if a live server was killed."""
    lock_fh = _acquire_lock()
    try:
        pid = _read_pid()
        if pid is None or not _is_debuginfod_running(pid):
            _unlink_missing_ok(PID_FILE)
            return False

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            _unlink_missing_ok(PID_FILE)
            return False

        if not _wait_for_exit(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _wait_for_exit(pid)

        _unlink_missing_ok(PID_FILE)
        return True
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()
