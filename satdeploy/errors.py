"""Typed error table for satdeploy.

Design doc (`docs/designs/vercel-for-cubesats.md`, line 74) locks this as a
Week 2 deliverable and lists the ~15 stderr classes that must route cleanly
to a user-actionable message plus a suggested fix command. The eng-review
landmine #14 calls out that regex matching (not string equality) is
mandatory: real stderr is never exactly the same twice.

Contract
--------
* **Exit codes are stable.** `EABI` is always 10, `ETRANSFER` is always 12.
  `watch`, CI, and shell scripts filter on these numbers. Do not renumber
  existing codes; add new ones at the end.
* **Each error class subclasses `SatDeployError`.** This plugs into the
  existing click error styling (red output, stderr sink) for free, and lets
  tests use `pytest.raises(ABIError)` for type-level assertions.
* **`match(stderr, app)`** returns the typed error for the first matching
  entry, or `None`. `from_stderr(stderr, app)` is the convenience wrapper
  that falls back to `UnknownError` with a stderr preview + issue link.
* **Matching is ordered.** The first `ERRORS` entry that matches wins. When
  two entries would fire on the same stderr, the more-specific one must come
  first in the list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Pattern

from satdeploy.output import SatDeployError, dim, error

# Exit codes. Stable contract — do not renumber.
EOK = 0
EUNKNOWN = 1
EABI = 10
EBACKUP = 11
ETRANSFER = 12
EAPPLY = 13
ERESTART = 14
EHEALTH = 15
EBUSY = 16
ESYSROOT = 17
EDEBUG = 18
EGATE = 19


class TypedError(SatDeployError):
    """Base class for iterate-phase typed errors.

    Renders as::

        ✗ [EABI] Target is missing required library libparam.so.3
          → try: Rebuild against a Yocto SDK matching the target libc

    Subclasses set `typed_name` and `exit_code`. Instance fields `fix_cmd`
    and `eta` are filled at match time from the matching `ErrorEntry`.
    """

    typed_name: str = "EUNKNOWN"
    exit_code: int = EUNKNOWN

    def __init__(self, message: str, fix_cmd: Optional[str] = None,
                 eta: Optional[str] = None) -> None:
        super().__init__(message)
        self.fix_cmd = fix_cmd
        self.eta = eta

    def format_message(self) -> str:
        # Don't prefix "✗ " here — output.error() adds it. Double-prefix bug
        # caught in DX review 2026-04-23 ("✗ ✗ [EUNKNOWN] ..." at CLI).
        head = error(f"[{self.typed_name}] {self.message}")
        if not self.fix_cmd:
            return head
        eta_str = f" ({self.eta})" if self.eta else ""
        return head + "\n" + dim(f"  → try: {self.fix_cmd}{eta_str}")


class ABIError(TypedError):
    typed_name = "EABI"
    exit_code = EABI


class BackupError(TypedError):
    typed_name = "EBACKUP"
    exit_code = EBACKUP


class TransferError(TypedError):
    typed_name = "ETRANSFER"
    exit_code = ETRANSFER


class ApplyError(TypedError):
    typed_name = "EAPPLY"
    exit_code = EAPPLY


class RestartError(TypedError):
    typed_name = "ERESTART"
    exit_code = ERESTART


class HealthError(TypedError):
    typed_name = "EHEALTH"
    exit_code = EHEALTH


class BusyError(TypedError):
    typed_name = "EBUSY"
    exit_code = EBUSY


class SysrootError(TypedError):
    typed_name = "ESYSROOT"
    exit_code = ESYSROOT


class DebugError(TypedError):
    typed_name = "EDEBUG"
    exit_code = EDEBUG


class GateError(TypedError):
    typed_name = "EGATE"
    exit_code = EGATE


class UnknownError(TypedError):
    typed_name = "EUNKNOWN"
    exit_code = EUNKNOWN


_ERROR_CLASSES = {
    EABI: ABIError,
    EBACKUP: BackupError,
    ETRANSFER: TransferError,
    EAPPLY: ApplyError,
    ERESTART: RestartError,
    EHEALTH: HealthError,
    EBUSY: BusyError,
    ESYSROOT: SysrootError,
    EDEBUG: DebugError,
    EGATE: GateError,
    EUNKNOWN: UnknownError,
}


@dataclass(frozen=True)
class ErrorEntry:
    """One row of the stderr → typed error table.

    `pattern.search(stderr)` drives matching. Named groups are substituted
    into `message` and `fix_cmd` via `str.format(**groups)`.
    """
    pattern: Pattern[str]
    exit_code: int
    message: str
    fix_cmd: Optional[str] = None
    eta: Optional[str] = None


# Ordered. First match wins. More-specific patterns must precede more-general
# ones. The specific class each entry routes to is derived from `exit_code`.
#
# TODO (design doc line 152): When `satdeploy sync-sysroot` ships, replace the
# descriptive fix_cmd strings below with the one-command invocation. The 2026-
# 04-23 DX review caught that earlier revisions referenced that unshipped
# command; honest text ships until the implementation lands.
ERRORS: List[ErrorEntry] = [
    # EABI -----------------------------------------------------------------
    ErrorEntry(
        pattern=re.compile(r"error while loading shared libraries: (?P<lib>\S+):"),
        exit_code=EABI,
        message="Target is missing required library {lib}.",
        fix_cmd="Add {lib} to the target Yocto image, or rebuild against a SDK that bundles it.",
        eta=None,
    ),
    ErrorEntry(
        pattern=re.compile(r"version `(?P<version>GLIBC_[^']+)' not found"),
        exit_code=EABI,
        message="Symbol version {version} not present on target libc.",
        fix_cmd="Rebuild against a Yocto SDK whose glibc matches the target.",
        eta=None,
    ),
    ErrorEntry(
        pattern=re.compile(r"undefined symbol: (?P<symbol>\S+)"),
        exit_code=EABI,
        message="Undefined symbol {symbol} on target — ABI drift.",
        fix_cmd="Rebuild against the SDK matching the target's library ABI. Set $SATDEPLOY_SDK to pin it.",
        eta=None,
    ),

    # ETRANSFER ------------------------------------------------------------
    ErrorEntry(
        pattern=re.compile(r"rsync error: .+? \(code (?P<code>\d+)\)"),
        exit_code=ETRANSFER,
        message="File transfer failed (rsync code {code}).",
        fix_cmd="satdeploy status",
        eta="1s",
    ),
    ErrorEntry(
        pattern=re.compile(r"CSP[ _](?:connection refused|disconnect|timeout)", re.I),
        exit_code=ETRANSFER,
        message="CSP transport disconnected.",
        fix_cmd="satdeploy status",
        eta="1s",
    ),

    # EAPPLY ---------------------------------------------------------------
    ErrorEntry(
        pattern=re.compile(r"bspatch.*(?:format|magic|truncated|corrupt)", re.I),
        exit_code=EAPPLY,
        message="Target refused patch (format mismatch).",
        fix_cmd="Retry — iterate will fall back to full upload",
        eta=None,
    ),
    ErrorEntry(
        pattern=re.compile(r"bsdiff.*(?:failed|exceeds\s+BSDIFF_MAX)", re.I),
        exit_code=EAPPLY,
        message="bsdiff compute failed. Iterate will fall back to full upload.",
        fix_cmd=None,
        eta=None,
    ),

    # ERESTART -------------------------------------------------------------
    # Match both `systemctl restart` output ("Failed to restart foo.service:
    # ...") and journalctl-style log lines ("systemd[1]: Failed to start
    # foo.service"). Real-world systemd stderr frequently omits the word
    # "systemctl" so anchoring on it drops half the cases.
    ErrorEntry(
        pattern=re.compile(r"Failed to (?:start|restart).*\.service", re.I),
        exit_code=ERESTART,
        message="Service failed to restart on target.",
        fix_cmd="satdeploy logs {app}",
        eta="5s",
    ),
    ErrorEntry(
        pattern=re.compile(r"Unit (?P<unit>\S+\.service) not found", re.I),
        exit_code=ERESTART,
        message="systemd unit {unit} not found on target.",
        fix_cmd="Check target /etc/systemd/system/ for the unit file",
        eta=None,
    ),

    # EHEALTH --------------------------------------------------------------
    ErrorEntry(
        pattern=re.compile(r"(?:validate|tests).*(?:timed out|timeout after)", re.I),
        exit_code=EHEALTH,
        message="Validation exceeded timeout.",
        fix_cmd="satdeploy logs {app}",
        eta="5s",
    ),

    # EBACKUP --------------------------------------------------------------
    ErrorEntry(
        pattern=re.compile(r"backup.*(?:corrupt|sha256 mismatch|hash mismatch)", re.I),
        exit_code=EBACKUP,
        message="Backup file on target is corrupt.",
        fix_cmd="satdeploy list {app}",
        eta="2s",
    ),

    # ESYSROOT -------------------------------------------------------------
    ErrorEntry(
        pattern=re.compile(r"No prebuilt sysroot for hash (?P<hash>\w+)"),
        exit_code=ESYSROOT,
        message="No prebuilt sysroot for manifest hash {hash}.",
        fix_cmd="Set $SATDEPLOY_SDK to the SDK matching manifest hash {hash}.",
        eta=None,
    ),
    ErrorEntry(
        pattern=re.compile(r"(?:sysroot|manifest hash).*(?:stale|unknown|not found)", re.I),
        exit_code=ESYSROOT,
        message="Target sysroot is unknown or stale.",
        fix_cmd="Set $SATDEPLOY_SDK to the SDK matching the target's current Yocto build.",
        eta=None,
    ),

    # EDEBUG ---------------------------------------------------------------
    ErrorEntry(
        pattern=re.compile(r"debuginfod.*(?:port|address).*(?:in use|busy|bind)", re.I),
        exit_code=EDEBUG,
        message="debuginfod port already in use by another process.",
        fix_cmd="satdeploy dev debuginfod stop",
        eta="1s",
    ),
    ErrorEntry(
        pattern=re.compile(r"gdbserver.*(?:not found|command not found|No such file)", re.I),
        exit_code=EDEBUG,
        message="gdbserver is not installed on the target.",
        fix_cmd="Add gdbserver to the Yocto image recipe and re-flash",
        eta=None,
    ),

    # EGATE — coupled ship. Pattern only fires when `push --requires-validated`
    # emits "Hash X has no PASS record" stderr. `satdeploy validate` and
    # `push --requires-validated` ship together (DX review 2026-04-23 Tier 1
    # decision #11); the fix_cmd below resolves at the same moment the
    # pattern starts firing. If either ships alone, flip fix_cmd back to
    # honest text the same way the EABI entries were handled.
    ErrorEntry(
        pattern=re.compile(r"Hash (?P<hash>[0-9a-f]+) has no PASS record"),
        exit_code=EGATE,
        message="Hash {hash} has no validation PASS record.",
        fix_cmd="satdeploy validate {app}",
        eta="3s",
    ),
]


def _safe_format(template: str, groups: dict) -> str:
    """Format template, skipping placeholders whose group is missing.

    Regex named-group dicts may be missing optional groups, or the caller
    may not know the app name. Rather than crash, silently leave unmatched
    placeholders as-is.
    """
    try:
        return template.format(**groups)
    except (KeyError, IndexError):
        # Fall back: substitute only known keys by iterating.
        result = template
        for k, v in groups.items():
            if v is None:
                continue
            result = result.replace("{" + k + "}", str(v))
        return result


def match(stderr: str, app: Optional[str] = None) -> Optional[TypedError]:
    """Match ``stderr`` against the error table. Returns the first match as
    a typed error instance, or ``None`` if no entry matches.

    Named groups from the regex are substituted into both ``message`` and
    ``fix_cmd``. The caller can pass ``app`` to fill ``{app}`` placeholders.
    """
    for entry in ERRORS:
        m = entry.pattern.search(stderr)
        if m is None:
            continue
        groups = {k: v for k, v in m.groupdict().items() if v is not None}
        if app is not None:
            groups.setdefault("app", app)
        msg = _safe_format(entry.message, groups)
        fix = _safe_format(entry.fix_cmd, groups) if entry.fix_cmd else None
        cls = _ERROR_CLASSES.get(entry.exit_code, UnknownError)
        return cls(msg, fix_cmd=fix, eta=entry.eta)
    return None


def _format_unknown(stderr: str) -> str:
    """Build the fallback message for unrecognized stderr."""
    stripped = stderr.strip()
    if not stripped:
        return (
            "Operation failed without stderr output. "
            "Please file: https://github.com/MahmoodSeoud/satDeploy/issues/new"
        )
    tail = stripped.splitlines()[-5:]
    preview = "\n  ".join(tail)
    return (
        f"Unrecognized error. Last stderr lines:\n  {preview}\n"
        f"Please file: https://github.com/MahmoodSeoud/satDeploy/issues/new"
    )


def from_stderr(stderr: str, app: Optional[str] = None) -> TypedError:
    """Match ``stderr`` or return ``UnknownError`` with a stderr preview."""
    matched = match(stderr, app)
    if matched is not None:
        return matched
    return UnknownError(_format_unknown(stderr))
