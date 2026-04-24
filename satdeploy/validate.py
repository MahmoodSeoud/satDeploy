"""`satdeploy validate` — run an app's test suite on the target.

Locks the minimum spec from DX review 2026-04-23 Tier 1 decision #11 and
design-doc Open Question #1: a config-defined shell-script test runner that
records PASS/FAIL to history.db keyed by (target, app, file_hash). The
`push --requires-validated` flight gate consults the same key to refuse
unvalidated binaries — see thesis evaluation metric #3 ("Zero untested
binaries reaching flight, proven by the --requires-validated gate").

Stretch features explicitly out of scope here (deferred to Phase 1):

  * libparam state assertions (design-doc OQ #1 stretch)
  * JSON attestation / signed artifact format (Tension C of the
    outside-voice challenge — TODO at the bottom of this module)
  * Telemetry / network upload (memory: aerospace-ai-skepticism — every
    write stays local to history.db)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from satdeploy.history import (
    History,
    ValidationRecord,
    VALIDATION_FAIL,
    VALIDATION_PASS,
)
from satdeploy.transport.base import Transport, TransportError


@dataclass
class ValidationOutcome:
    """Return value of `run_validate` — wraps the persisted record plus a
    convenience `passed` boolean so CLI callers don't string-compare status.
    """
    record: ValidationRecord
    passed: bool


def run_validate(
    transport: Transport,
    *,
    target: str,
    app: str,
    command: str,
    file_hash: str,
    timeout: Optional[float] = None,
    history: Optional[History] = None,
) -> ValidationOutcome:
    """Run `command` on `transport`, time it, persist the outcome.

    Args:
        transport: An already-connected Transport. Caller owns connect/disconnect.
        target: Target name (R1 fleet contract — keys the validation row).
        app: App name from config.
        command: Shell-interpreted string run on the target. Treated as
            opaque — caller is responsible for the target-side cwd /
            environment by writing the command appropriately
            (e.g. ``cd /opt/disco && ./tests/run.sh``).
        file_hash: SHA256 of the running file. Caller computes this from
            the local binary that was just deployed; matches what
            `push` will hash when it consults `has_pass_record`.
        timeout: Hard wall-clock timeout in seconds. ``None`` means no
            timeout, but callers should pass `AppConfig.validate_timeout_seconds`.
        history: Optional History to write into. When omitted, the record
            is returned but not persisted (used by tests).

    Returns:
        ValidationOutcome with the persisted ValidationRecord and a
        `passed` boolean.

    Raises:
        TransportError: If the transport cannot launch the command,
            disconnects mid-run, or hits the timeout.
    """
    start = time.monotonic()
    try:
        exit_code, stdout, stderr = transport.exec_command(command, timeout=timeout)
    except TransportError:
        # Persist a FAIL row for the timeout / connection-drop case so the
        # gate has evidence the validation was attempted and failed.
        # Re-raise so the CLI can route through the typed error matcher
        # (EHEALTH for "validate timed out", ETRANSFER for disconnect).
        elapsed_ms = int((time.monotonic() - start) * 1000)
        record = ValidationRecord(
            target=target,
            app=app,
            file_hash=file_hash,
            status=VALIDATION_FAIL,
            exit_code=-1,
            duration_ms=elapsed_ms,
            command=command,
            stdout="",
            stderr="<transport error — see CLI message>",
        )
        if history is not None:
            history.record_validation(record)
        raise

    elapsed_ms = int((time.monotonic() - start) * 1000)
    status = VALIDATION_PASS if exit_code == 0 else VALIDATION_FAIL

    record = ValidationRecord(
        target=target,
        app=app,
        file_hash=file_hash,
        status=status,
        exit_code=exit_code,
        duration_ms=elapsed_ms,
        command=command,
        stdout=stdout,
        stderr=stderr,
    )
    if history is not None:
        history.record_validation(record)

    return ValidationOutcome(record=record, passed=status == VALIDATION_PASS)


# TODO (decision T1, Tension C of outside-voice): add a signed-attestation
# artifact format so a PASS record can be transported between ground stations
# without re-running the test suite. Out of scope for the minimum-spec ship.
