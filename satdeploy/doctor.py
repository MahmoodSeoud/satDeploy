"""satdeploy doctor — pre-flight check for iterate/watch/push/debug.

Born from the 2026-04-23 DX review: author measured TTHW (time to hello
world) at ~75 minutes against a fresh Raspberry Pi in this session, with
four failures before first successful iterate. Each failure was a setup
issue that satdeploy could have detected in seconds, before burning 30s
inside a transport layer. Doctor closes that gap.

Each check emits ``CheckResult(name, status, message, fix_cmd)``. Status
is ``pass`` / ``warn`` / ``fail``. On ``fail``, ``fix_cmd`` is a single
actionable shell command (or short hint) the user can run to resolve it.

Modes select which checks run:

* ``all`` / ``iterate`` / ``push`` — base (config, ssh, remote backup dir,
  local file, systemd unit, sudo). This is the common case.
* ``watch`` — iterate checks + watchdog library present.
* ``debug`` — iterate checks + gdbserver on target + local debuginfod +
  SATDEPLOY_SDK sysroot. The "I want to hit a breakpoint" path from the
  design doc's magical moment.

Checks take a ``DoctorContext`` and return a list of ``CheckResult`` so
one check can emit multiple results (e.g., per-app systemd unit checks).
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from satdeploy.config import Config, ModuleConfig
from satdeploy.paths import expand_path


class CheckStatus(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    message: str
    fix_cmd: Optional[str] = None


@dataclass(frozen=True)
class DoctorContext:
    config: Config
    module: ModuleConfig
    apps: list[str]
    mode: str


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

_SSH_OPTS = ["-o", "ConnectTimeout=5", "-o", "BatchMode=yes"]
_SSH_TIMEOUT_S = 10


def _ssh_exec(user: str, host: str, cmd: str,
              timeout: int = _SSH_TIMEOUT_S) -> subprocess.CompletedProcess:
    """Run ``cmd`` on ``user@host`` via ssh. Uses BatchMode + timeout so a
    dead target never blocks the whole doctor run."""
    return subprocess.run(
        ["ssh", *_SSH_OPTS, f"{user}@{host}", cmd],
        capture_output=True, timeout=timeout,
    )


def check_config(ctx: DoctorContext) -> list[CheckResult]:
    # If doctor is being called, load_config already succeeded (validates +
    # errors on parse). So just affirm the path.
    return [CheckResult("config", CheckStatus.PASS, str(ctx.config.config_path))]


def check_ssh(ctx: DoctorContext) -> list[CheckResult]:
    if ctx.module.transport != "ssh":
        return [CheckResult("ssh", CheckStatus.PASS,
                            f"transport={ctx.module.transport} (ssh check not applicable)")]
    host = ctx.module.host or "<unset>"
    user = ctx.module.user or "root"
    if host == "<unset>":
        return [CheckResult("ssh", CheckStatus.FAIL,
                            "host is unset in config",
                            fix_cmd=f"Set host: in {ctx.config.config_path}")]
    try:
        t0 = time.monotonic()
        proc = _ssh_exec(user, host, "true")
        elapsed = time.monotonic() - t0
        if proc.returncode == 0:
            return [CheckResult("ssh", CheckStatus.PASS,
                                f"{user}@{host} connected in {elapsed:.2f}s")]
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        return [CheckResult("ssh", CheckStatus.FAIL,
                            f"{user}@{host} — {stderr or 'connect failed'}",
                            fix_cmd=f"ssh-copy-id {user}@{host}"
                                    f"   # if key auth isn't set up, or check ~/.ssh/config")]
    except subprocess.TimeoutExpired:
        return [CheckResult("ssh", CheckStatus.FAIL,
                            f"{user}@{host} — timeout after {_SSH_TIMEOUT_S}s",
                            fix_cmd=f"Check {host} is reachable (ping, tailscale status, VPN)")]
    except FileNotFoundError:
        return [CheckResult("ssh", CheckStatus.FAIL,
                            "ssh binary not found on PATH",
                            fix_cmd="Install openssh-client")]


def _any_service_configured(ctx: DoctorContext) -> bool:
    for app_name in ctx.apps:
        app = ctx.config.get_app(app_name)
        if app and app.service:
            return True
    return False


def check_sudo(ctx: DoctorContext) -> list[CheckResult]:
    if ctx.module.transport != "ssh":
        return []
    if not _any_service_configured(ctx):
        # No systemd units to manage; sudo isn't needed for iterate.
        return []
    host = ctx.module.host
    user = ctx.module.user or "root"
    if user == "root":
        return [CheckResult("sudo", CheckStatus.PASS,
                            "user is root — sudo not required")]
    try:
        proc = _ssh_exec(user, host, "sudo -n true")
        if proc.returncode == 0:
            return [CheckResult("sudo", CheckStatus.PASS,
                                f"{user}@{host} — passwordless sudo OK")]
        return [CheckResult("sudo", CheckStatus.FAIL,
                            f"{user}@{host} — can't sudo without a password",
                            fix_cmd=f"ssh {user}@{host} 'echo \"{user} ALL=(ALL) NOPASSWD:ALL\" "
                                    f"| sudo tee /etc/sudoers.d/{user}'")]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return [CheckResult("sudo", CheckStatus.WARN,
                            "could not verify sudo (ssh failed earlier)",
                            fix_cmd="Fix the ssh check first")]


def check_remote_backup_dir(ctx: DoctorContext) -> list[CheckResult]:
    if ctx.module.transport != "ssh":
        return []
    host = ctx.module.host
    user = ctx.module.user or "root"
    backup = ctx.config.get_backup_dir(ctx.module.name)
    q = shlex.quote(backup)
    try:
        proc = _ssh_exec(user, host, f"test -d {q} && test -w {q}")
        if proc.returncode == 0:
            return [CheckResult("remote_backup_dir", CheckStatus.PASS,
                                f"{backup} exists + writable by {user}")]
        # Distinguish "missing" vs "not writable" for a better fix hint.
        proc2 = _ssh_exec(user, host, f"test -d {q}")
        if proc2.returncode != 0:
            return [CheckResult("remote_backup_dir", CheckStatus.FAIL,
                                f"{backup} does not exist on {host}",
                                fix_cmd=f"ssh {user}@{host} "
                                        f"'sudo mkdir -p {q} && sudo chown {user}:{user} {q}'")]
        return [CheckResult("remote_backup_dir", CheckStatus.FAIL,
                            f"{backup} exists but {user} can't write to it",
                            fix_cmd=f"ssh {user}@{host} 'sudo chown {user}:{user} {q}'")]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return [CheckResult("remote_backup_dir", CheckStatus.WARN,
                            "could not verify (ssh unreachable)",
                            fix_cmd="Fix the ssh check first")]


def check_local_files(ctx: DoctorContext) -> list[CheckResult]:
    results: list[CheckResult] = []
    for app_name in ctx.apps:
        app = ctx.config.get_app(app_name)
        if app is None:
            known = ", ".join(sorted(ctx.config.get_all_app_names())) or "(none)"
            results.append(CheckResult(
                f"local[{app_name}]", CheckStatus.FAIL,
                f"no app named {app_name!r} in config (known: {known})",
                fix_cmd=f"Add an entry under apps: in {ctx.config.config_path}",
            ))
            continue
        local = Path(expand_path(app.local))
        if not local.exists():
            results.append(CheckResult(
                f"local[{app_name}]", CheckStatus.FAIL,
                f"{local} does not exist",
                fix_cmd=f"Build the binary, or edit apps.{app_name}.local "
                        f"in {ctx.config.config_path}",
            ))
        elif not os.access(local, os.R_OK):
            results.append(CheckResult(
                f"local[{app_name}]", CheckStatus.FAIL,
                f"{local} is not readable",
                fix_cmd=f"chmod +r {local}",
            ))
        else:
            size = local.stat().st_size
            results.append(CheckResult(
                f"local[{app_name}]", CheckStatus.PASS,
                f"{local} ({size:,} B)",
            ))
    return results


def check_systemd_units(ctx: DoctorContext) -> list[CheckResult]:
    if ctx.module.transport != "ssh":
        return []
    host = ctx.module.host
    user = ctx.module.user or "root"
    results: list[CheckResult] = []
    for app_name in ctx.apps:
        app = ctx.config.get_app(app_name)
        if app is None or not app.service:
            continue
        try:
            proc = _ssh_exec(user, host,
                             f"systemctl cat {shlex.quote(app.service)}")
            if proc.returncode == 0:
                results.append(CheckResult(
                    f"service[{app.service}]", CheckStatus.PASS,
                    f"unit loaded on {host}",
                ))
            else:
                results.append(CheckResult(
                    f"service[{app.service}]", CheckStatus.FAIL,
                    f"unit not found on {host}",
                    fix_cmd=f"Install /etc/systemd/system/{app.service} + run "
                            f"`sudo systemctl daemon-reload && sudo systemctl "
                            f"enable --now {app.service}` on {host}",
                ))
        except (subprocess.TimeoutExpired, FileNotFoundError):
            results.append(CheckResult(
                f"service[{app.service}]", CheckStatus.WARN,
                "could not verify (ssh unreachable)",
            ))
    return results


def check_watchdog(ctx: DoctorContext) -> list[CheckResult]:
    try:
        import watchdog  # noqa: F401
        ver = getattr(watchdog, "__version__", "unknown")
        return [CheckResult("watchdog", CheckStatus.PASS, f"watchdog {ver}")]
    except ImportError:
        return [CheckResult("watchdog", CheckStatus.FAIL,
                            "watchdog not installed",
                            fix_cmd="pip install 'watchdog>=3.0'")]


def check_debuginfod_local(ctx: DoctorContext) -> list[CheckResult]:
    binary = shutil.which("debuginfod")
    if binary:
        return [CheckResult("debuginfod", CheckStatus.PASS, binary)]
    return [CheckResult("debuginfod", CheckStatus.FAIL,
                        "debuginfod not on PATH",
                        fix_cmd="apt install elfutils  (Debian/Ubuntu) / "
                                "brew install elfutils  (macOS)")]


def check_gdbserver_on_target(ctx: DoctorContext) -> list[CheckResult]:
    if ctx.module.transport != "ssh":
        return [CheckResult("gdbserver", CheckStatus.WARN,
                            "iterate --debug currently requires SSH transport")]
    host = ctx.module.host
    user = ctx.module.user or "root"
    try:
        proc = _ssh_exec(user, host, "which gdbserver")
        if proc.returncode == 0:
            path = proc.stdout.decode("utf-8", errors="replace").strip()
            return [CheckResult("gdbserver", CheckStatus.PASS,
                                f"{path} on {host}")]
        return [CheckResult("gdbserver", CheckStatus.FAIL,
                            f"gdbserver not installed on {host}",
                            fix_cmd=f"ssh {user}@{host} 'sudo apt install gdbserver'")]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return [CheckResult("gdbserver", CheckStatus.WARN,
                            "could not verify (ssh unreachable)")]


def check_sysroot(ctx: DoctorContext) -> list[CheckResult]:
    sdk = os.environ.get("SATDEPLOY_SDK")
    if not sdk:
        return [CheckResult("sysroot", CheckStatus.WARN,
                            "SATDEPLOY_SDK not set — ABI check will be skipped",
                            fix_cmd="export SATDEPLOY_SDK=/path/to/yocto-sdk")]
    sdk_path = Path(expand_path(sdk))
    if not sdk_path.is_dir():
        return [CheckResult("sysroot", CheckStatus.FAIL,
                            f"SATDEPLOY_SDK={sdk} is not a directory",
                            fix_cmd="export SATDEPLOY_SDK=/valid/path")]
    return [CheckResult("sysroot", CheckStatus.PASS, str(sdk_path))]


# ---------------------------------------------------------------------------
# Mode → check list
# ---------------------------------------------------------------------------

_BASE_CHECKS = [
    check_config,
    check_ssh,
    check_remote_backup_dir,
]

_ITERATE_CHECKS = _BASE_CHECKS + [
    check_local_files,
    check_systemd_units,
    check_sudo,
]

_WATCH_CHECKS = _ITERATE_CHECKS + [check_watchdog]

_DEBUG_CHECKS = _ITERATE_CHECKS + [
    check_gdbserver_on_target,
    check_debuginfod_local,
    check_sysroot,
]

_PUSH_CHECKS = _ITERATE_CHECKS

MODE_CHECKS: dict[str, list[Callable[[DoctorContext], list[CheckResult]]]] = {
    "all": _ITERATE_CHECKS,
    "iterate": _ITERATE_CHECKS,
    "watch": _WATCH_CHECKS,
    "debug": _DEBUG_CHECKS,
    "push": _PUSH_CHECKS,
}


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

OnResult = Callable[[CheckResult], None]


@dataclass(frozen=True)
class DoctorSummary:
    passed: int
    warned: int
    failed: int

    @property
    def ok(self) -> bool:
        """``True`` when every check passed or warned; ``False`` on any fail."""
        return self.failed == 0


def run_doctor(
    config: Config,
    module: ModuleConfig,
    apps: list[str],
    *,
    mode: str = "all",
    on_result: Optional[OnResult] = None,
) -> DoctorSummary:
    """Run all checks for ``mode``, invoking ``on_result`` per result.

    Returns a ``DoctorSummary`` with counts. Callers that want a non-zero
    exit code on failure should check ``summary.failed > 0``.

    Args:
        config: Parsed project config (already passed load+validate).
        module: Target module the check is running against.
        apps: App names to narrow per-app checks to. Empty list is treated
            as "all apps from config" by the caller; doctor itself just
            respects whatever list it's given.
        mode: Which check family to run (see ``MODE_CHECKS``).
        on_result: Called synchronously for each emitted ``CheckResult``
            so the CLI can stream output.
    """
    emit = on_result or (lambda _r: None)
    ctx = DoctorContext(config=config, module=module, apps=apps, mode=mode)
    checks = MODE_CHECKS.get(mode, MODE_CHECKS["all"])
    passed = warned = failed = 0
    for check_fn in checks:
        for result in check_fn(ctx):
            emit(result)
            if result.status == CheckStatus.PASS:
                passed += 1
            elif result.status == CheckStatus.WARN:
                warned += 1
            else:
                failed += 1
    return DoctorSummary(passed=passed, warned=warned, failed=failed)
