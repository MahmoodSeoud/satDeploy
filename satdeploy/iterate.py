"""satdeploy iterate — edit-to-running for flight software.

The wedge. Design doc (`docs/designs/vercel-for-cubesats.md:64`):

    `satdeploy iterate <app>` — edit-to-running in p50 ≤10s on flatsat
    dev configs (ZMQ/TCP), p95 ≤30s.
    Acceptance: exits 0 when service is running with new binary; exits
    non-zero with typed error otherwise.

v1 scope (Week 2 minimum):
* Full-binary upload via the existing transport's ``deploy()`` method.
  Bsdiff patch path is Lane A future work (agent-side ``bspatch`` isn't
  wired in yet — see `docs/feasibility/week1-results.md` for the plan).
* Per-app ``fcntl.flock`` prevents two iterate calls for the same app
  from racing (landmine-adjacent to watch-cancel-torn-file). Mirrors the
  pattern in ``satdeploy.debuginfod``.
* Pre-upload ABI check via ``satdeploy.abi``. Missing DT_NEEDED lib on
  target sysroot → typed ``ABIError`` before we burn transport budget.
* ``--debug`` flag (SSH only for v1): spawns ``gdbserver`` on the target
  attached to the app's MainPID, ensures local ``debuginfod`` is running,
  prints ``DEBUGINFOD_URLS`` so the user can run ``satdeploy gdb`` or
  point any ``gdb-multiarch`` at the target.
* History row written to ``history.db`` with the correct transport field
  so the dashboard + audit export see iterate events alongside push.

Non-scope (by design, Week 3+):
* Bsdiff patch compute + deploy (Lane A: needs agent bspatch integration).
* Watch-driven iterate loop (Week 3, ``satdeploy watch``).
* Gate flip (Week 5, ``--strict``).
* gdbserver over CSP (needs a new agent command; SSH is v1).

Failure model: every failure path raises a ``TypedError`` from
``satdeploy.errors``. The caller (Click wrapper in ``cli.py``) converts
the raised exception into a user-facing red message + exit code via
click's ``ClickException.show()`` path.
"""

from __future__ import annotations

import fcntl
import os
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from satdeploy import abi, debuginfod as debuginfod_module, errors
from satdeploy.config import Config, ModuleConfig
from satdeploy.hash import compute_file_hash
from satdeploy.history import DeploymentRecord, History
from satdeploy.provenance import resolve_provenance
from satdeploy.transport.base import Transport, TransportError

ITERATE_LOCK_DIR = Path.home() / ".satdeploy" / "run"
GDBSERVER_PORT = 9001


@dataclass(frozen=True)
class IterateResult:
    """Success payload returned by ``run_iterate``."""
    app: str
    file_hash: str
    elapsed_s: float
    debug_url: Optional[str] = None  # set iff debug=True


def _acquire_app_lock(app: str):
    """Acquire a per-app ``fcntl.flock``. Return the file handle.

    Raises ``BusyError`` if another iterate for the same app is already
    running (file is locked). Mirrors the pattern in
    ``satdeploy.debuginfod:85`` so debuginfod and iterate behave the same
    under concurrent pressure (ctrl-C in one terminal, watch firing in
    another).
    """
    ITERATE_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = ITERATE_LOCK_DIR / f"iterate-{app}.lock"
    lock_path.touch()
    fh = open(lock_path, "r+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise errors.BusyError(
            f"Another iterate for {app} is already running.",
            fix_cmd="Wait for it to finish, or kill the other process and retry",
        )
    return fh


def _release_app_lock(fh) -> None:
    try:
        fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()


def _spawn_gdbserver_ssh(transport, app: str, service: Optional[str],
                         port: int = GDBSERVER_PORT) -> int:
    """Spawn ``gdbserver`` on the target via SSH, attached to the app's
    MainPID. Returns the port.

    Implementation note: we exec through the SSH transport's ``_run_cmd``
    helper if available, else fall back to its execute method. The
    transport abstraction doesn't expose ``execute`` as abstract yet, so
    we ``getattr`` the real method. Agent-over-CSP path is out of scope
    for v1 (needs a new agent command) — this function raises
    ``DebugError`` if the transport doesn't look like SSH.
    """
    run = getattr(transport, "_run_cmd", None) or getattr(transport, "execute", None)
    if run is None:
        raise errors.DebugError(
            "Transport does not support remote command execution yet.",
            fix_cmd="iterate --debug currently requires the SSH transport",
        )

    unit = service or f"{app}.service"
    # Kill any existing gdbserver for this app (port-in-use avoidance).
    # Best-effort: ignore exit code — the subsequent spawn is the gate.
    try:
        run(f"pkill -f 'gdbserver :{port}' || true")
    except TransportError:
        pass

    # Resolve MainPID via systemctl. Bail with typed error if the unit
    # isn't known / running.
    try:
        stdout = run(f"systemctl show --property=MainPID --value {shlex.quote(unit)}")
    except TransportError as e:
        raise errors.DebugError(
            f"Could not query systemd MainPID for {unit}: {e}",
            fix_cmd="satdeploy logs {app}".format(app=app),
        )

    pid_str = (stdout or "").strip()
    if not pid_str or pid_str == "0":
        raise errors.DebugError(
            f"{unit} is not running on target (MainPID={pid_str!r}).",
            fix_cmd=f"satdeploy logs {app}",
        )
    try:
        pid = int(pid_str)
    except ValueError:
        raise errors.DebugError(
            f"systemd returned non-numeric MainPID: {pid_str!r}",
            fix_cmd=f"satdeploy logs {app}",
        )

    # Spawn gdbserver in the background. The target's gdbserver binary
    # needs to exist; landmine `gdbserver missing` is caught by errors.py.
    try:
        run(
            f"nohup gdbserver :{port} --attach {pid} "
            f">/tmp/gdbserver-{app}.log 2>&1 &"
        )
    except TransportError as e:
        typed = errors.from_stderr(str(e), app=app)
        raise typed
    return port


def _start_local_debuginfod(sysroots: Path) -> str:
    """Start (or reuse) the local debuginfod serving ``sysroots``. Returns
    the DEBUGINFOD_URL the user should export.

    Landmine #2 (PID TOCTOU) is handled inside
    ``satdeploy.debuginfod.serve`` via fcntl.flock.
    """
    try:
        debuginfod_module.serve(sysroots_dir=sysroots)
    except debuginfod_module.DebuginfodError as e:
        # Route through errors.py so the user sees the typed EDEBUG path.
        raise errors.DebugError(
            str(e),
            fix_cmd="satdeploy debuginfod stop  # then retry",
        )
    return debuginfod_module.DEBUGINFOD_URL


def run_iterate(
    config: Config,
    module_config: ModuleConfig,
    app: str,
    *,
    local_override: Optional[str] = None,
    sysroot: Optional[Path] = None,
    debug: bool = False,
    force: bool = False,
    on_step: Optional[Callable[[str], None]] = None,
) -> IterateResult:
    """Run edit-to-running for one app. Returns on success, raises
    ``TypedError`` (subclass) on any failure.

    Args:
        config: Parsed project config.
        module_config: Target to deploy to (from ``resolve_target``).
        app: App name in config.
        local_override: Optional local file path that overrides
            ``app_config.local``.
        sysroot: Optional target sysroot for the ABI check. Falls back to
            ``$SATDEPLOY_SDK`` env var, then skips the check if neither
            is set (emits a warning via ``on_step``).
        debug: If True, also spawn gdbserver on target + start local
            debuginfod. SSH transport only in v1.
        force: Pass through to transport — skip hash-equality short circuit.
        on_step: Callback for human-readable progress lines.

    Raises:
        ABIError, TransferError, ApplyError, RestartError, HealthError,
        BusyError, DebugError — all subclasses of TypedError.
    """
    step = on_step or (lambda _: None)

    # Resolve app config + local path — fail fast with a typed-ish message.
    app_config = config.get_app(app)
    if app_config is None:
        known = ", ".join(sorted(config.get_all_app_names())) or "(none configured)"
        raise errors.UnknownError(
            f"No app named {app!r} in config {config.config_path}. "
            f"Known apps: {known}."
        )

    local_path = Path(os.path.expanduser(local_override or app_config.local))
    if not local_path.exists():
        raise errors.UnknownError(f"Local file not found: {local_path}")

    # Per-app lock. Blocks concurrent iterate for the same app.
    lock_fh = _acquire_app_lock(app)
    t_start = time.monotonic()
    try:
        step(f"iterate {app}: local={local_path.name}")

        # ABI check — skipped cleanly if no sysroot configured.
        resolved_sysroot = sysroot
        if resolved_sysroot is None:
            env_sdk = os.environ.get("SATDEPLOY_SDK")
            if env_sdk:
                resolved_sysroot = Path(env_sdk)
        if resolved_sysroot is not None:
            step(f"ABI check against {resolved_sysroot}")
            abi.check(local_path, resolved_sysroot)  # raises ABIError

        # Compute local hash — needed for history + transport's expected_checksum.
        # Project convention: 8 hex chars (see satdeploy/hash.py docstring).
        file_hash = compute_file_hash(str(local_path))
        step(f"upload: {local_path.stat().st_size:,} B, hash {file_hash}")

        # Transport deploy. Reuse the existing abstract `deploy()`; no
        # patch path in v1 (Lane A deferred).
        backup_dir = config.get_backup_dir(module_config.name)
        from satdeploy.cli import get_transport  # late import: avoids CLI-import cycle
        transport = get_transport(module_config, backup_dir)
        try:
            transport.connect()
        except TransportError as e:
            raise errors.from_stderr(str(e), app=app)

        deploy_result = None
        try:
            deploy_kwargs = dict(
                app_name=app,
                local_path=str(local_path),
                remote_path=app_config.remote,
                force=force,
                expected_checksum=file_hash,
            )
            if module_config.transport == "csp":
                deploy_kwargs.update(
                    param_name=app_config.param,
                    appsys_node=module_config.appsys_node,
                    run_node=module_config.get_run_node(app),
                )
            deploy_result = transport.deploy(**deploy_kwargs)
        except TransportError as e:
            raise errors.from_stderr(str(e), app=app)
        finally:
            try:
                transport.disconnect()
            except TransportError:
                pass  # best-effort; primary error takes precedence

        if deploy_result is None or not deploy_result.success:
            msg = (deploy_result.error_message if deploy_result else "") or ""
            raise errors.from_stderr(msg or "transport deploy returned failure", app=app)

        # --debug: gdbserver + local debuginfod.
        debug_url: Optional[str] = None
        if debug:
            step("debug: starting debuginfod + gdbserver")
            debug_url = _start_local_debuginfod(debuginfod_module.DEFAULT_SYSROOTS_DIR)
            port = _spawn_gdbserver_ssh(transport, app, app_config.service, GDBSERVER_PORT)
            step(f"debug: gdbserver on target port {port}; DEBUGINFOD_URLS={debug_url}")

        # History row — iterate is effectively a 'push' action from the
        # audit trail's point of view. Source = 'cli', transport taken
        # from module_config.
        provenance, prov_source = resolve_provenance(str(local_path))
        history = History(config.history_path)
        history.init_db()
        history.record(DeploymentRecord(
            app=app,
            file_hash=file_hash,
            remote_path=app_config.remote,
            action="push",
            success=True,
            module=module_config.name,
            git_hash=provenance,
            provenance_source=prov_source,
            backup_path=deploy_result.backup_path,
            transport=module_config.transport,
            source="cli",
        ))

        elapsed = time.monotonic() - t_start
        step(f"done in {elapsed:.1f}s")
        return IterateResult(
            app=app,
            file_hash=file_hash,
            elapsed_s=elapsed,
            debug_url=debug_url,
        )
    finally:
        _release_app_lock(lock_fh)
