"""Tests for satdeploy.errors — typed stderr → error routing (landmine #14).

The iron rule from the 2026-04-19 eng review: regex matching, not string
equality. Real stderr varies run to run. Every entry in ERRORS must match
the class of stderr it was written for, and must NOT cross-match stderr
from an unrelated class.
"""

from __future__ import annotations

import pytest

from satdeploy import errors


# ---------------------------------------------------------------------------
# Exit-code stability contract
# ---------------------------------------------------------------------------

def test_exit_codes_are_stable_constants():
    """Exit codes are a public contract with watch/CI/scripts. Verifying the
    numeric values here prevents a silent renumber."""
    assert errors.EOK == 0
    assert errors.EUNKNOWN == 1
    assert errors.EABI == 10
    assert errors.EBACKUP == 11
    assert errors.ETRANSFER == 12
    assert errors.EAPPLY == 13
    assert errors.ERESTART == 14
    assert errors.EHEALTH == 15
    assert errors.EBUSY == 16
    assert errors.ESYSROOT == 17
    assert errors.EDEBUG == 18
    assert errors.EGATE == 19


def test_every_class_declares_matching_exit_code():
    """Each TypedError subclass must carry the exit code its name implies.
    Caught a bug where ApplyError was silently inheriting EUNKNOWN."""
    cases = [
        (errors.ABIError, errors.EABI, "EABI"),
        (errors.BackupError, errors.EBACKUP, "EBACKUP"),
        (errors.TransferError, errors.ETRANSFER, "ETRANSFER"),
        (errors.ApplyError, errors.EAPPLY, "EAPPLY"),
        (errors.RestartError, errors.ERESTART, "ERESTART"),
        (errors.HealthError, errors.EHEALTH, "EHEALTH"),
        (errors.BusyError, errors.EBUSY, "EBUSY"),
        (errors.SysrootError, errors.ESYSROOT, "ESYSROOT"),
        (errors.DebugError, errors.EDEBUG, "EDEBUG"),
        (errors.GateError, errors.EGATE, "EGATE"),
        (errors.UnknownError, errors.EUNKNOWN, "EUNKNOWN"),
    ]
    for cls, expected_code, expected_name in cases:
        instance = cls("msg")
        assert instance.exit_code == expected_code, (
            f"{cls.__name__}.exit_code should be {expected_code}, "
            f"got {instance.exit_code}"
        )
        assert instance.typed_name == expected_name


# ---------------------------------------------------------------------------
# Per-entry: stderr matches the intended typed error and NOT cross-matches
# ---------------------------------------------------------------------------

# (stderr, expected_cls, expected_substring_in_message) tuples — each pair
# pulls from the real output pattern that motivates that row.
MATCH_CASES = [
    # EABI — shared library missing
    (
        "controller: error while loading shared libraries: libparam.so.3: "
        "cannot open shared object file: No such file or directory",
        errors.ABIError,
        "libparam.so.3",
    ),
    # EABI — glibc version too new
    (
        "./controller: /lib/libc.so.6: version `GLIBC_2.34' not found (required by ./controller)",
        errors.ABIError,
        "GLIBC_2.34",
    ),
    # EABI — undefined symbol at dynamic link
    (
        "./controller: symbol lookup error: undefined symbol: param_list_iterator_next",
        errors.ABIError,
        "param_list_iterator_next",
    ),
    # ETRANSFER — rsync
    (
        "rsync: failed to connect to host (113): No route to host\n"
        "rsync error: error in socket IO (code 10) at clientserver.c(127)",
        errors.TransferError,
        "10",
    ),
    # ETRANSFER — CSP disconnect
    (
        "[agent] CSP connection refused on node 5425",
        errors.TransferError,
        "CSP transport disconnected",
    ),
    # EAPPLY — bspatch format mismatch
    (
        "bspatch: patch header magic mismatch (expected BSDIFF40, got BSDIFF4G)",
        errors.ApplyError,
        "format mismatch",
    ),
    # EAPPLY — bsdiff compute failed on oversized binary
    (
        "bsdiff compute failed: input exceeds BSDIFF_MAX_OLD_BYTES (5242880)",
        errors.ApplyError,
        "bsdiff",
    ),
    # ERESTART — systemctl restart failed
    (
        "Failed to restart controller.service: Unit controller.service has failed",
        errors.RestartError,
        "restart",
    ),
    # ERESTART — unit not found
    (
        "Unit controller.service not found.",
        errors.RestartError,
        "controller.service",
    ),
    # EHEALTH — validate timed out
    (
        "validate: tests timed out after 300 seconds",
        errors.HealthError,
        "timeout",
    ),
    # EBACKUP — backup SHA256 mismatch
    (
        "backup file /opt/satdeploy/backups/controller/20260420-141500-abc.bak: sha256 mismatch",
        errors.BackupError,
        "corrupt",
    ),
    # ESYSROOT — no prebuilt
    (
        "No prebuilt sysroot for hash deadbeef1234",
        errors.SysrootError,
        "deadbeef1234",
    ),
    # ESYSROOT — sysroot stale
    (
        "manifest hash on target does not match any sysroot (stale)",
        errors.SysrootError,
        "stale",
    ),
    # EDEBUG — debuginfod port in use
    (
        "debuginfod: error: listen: Address already in use on port 8002",
        errors.DebugError,
        "port already in use",
    ),
    # EDEBUG — gdbserver missing
    (
        "ssh: gdbserver: command not found",
        errors.DebugError,
        "gdbserver",
    ),
    # EGATE — hash has no PASS record (Week 5 feature, placeholder)
    (
        "Hash abc1234567def has no PASS record. Proceed anyway? [y/N]",
        errors.GateError,
        "abc1234567def",
    ),
]


@pytest.mark.parametrize("stderr,expected_cls,substring", MATCH_CASES)
def test_match_routes_each_class(stderr, expected_cls, substring):
    err = errors.match(stderr, app="controller")
    assert err is not None, f"no ERRORS entry matched: {stderr!r}"
    assert isinstance(err, expected_cls), (
        f"routed to {type(err).__name__}, expected {expected_cls.__name__}"
    )
    assert substring.lower() in err.message.lower() or \
           substring in err.message, (
        f"formatted message missing expected substring {substring!r}: {err.message!r}"
    )


def test_match_returns_none_for_unrecognized_stderr():
    """The regex table is deliberately conservative. Unknown stderr must
    return None so iterate can route through from_stderr's fallback."""
    assert errors.match("totally unrelated error text, never seen before", app="foo") is None
    assert errors.match("", app="foo") is None
    assert errors.match("   \n  \n", app="foo") is None


def test_match_does_not_cross_match():
    """ABI errors and transport errors must never be confused. A generic
    'connection refused' should not trip the ABI pattern.

    Prior failure we want to prevent: the TransferError regex for
    'CSP connection refused' accidentally consuming the ABI 'shared
    libraries' stderr because both contain the word 'error'.
    """
    # The ABI stderr must NOT be routed to TransferError.
    abi_stderr = "error while loading shared libraries: libparam.so.3: cannot open"
    err = errors.match(abi_stderr)
    assert isinstance(err, errors.ABIError)

    # The transport stderr must NOT be routed to ABIError or ApplyError.
    transport_stderr = "rsync error: error in socket IO (code 10) at clientserver.c"
    err = errors.match(transport_stderr)
    assert isinstance(err, errors.TransferError)


# ---------------------------------------------------------------------------
# Placeholder substitution
# ---------------------------------------------------------------------------

def test_match_substitutes_named_groups_into_message():
    stderr = "error while loading shared libraries: libparam.so.3: cannot open"
    err = errors.match(stderr)
    assert "libparam.so.3" in err.message


def test_match_substitutes_app_into_fix_cmd():
    stderr = "Failed to restart controller.service: bla"
    err = errors.match(stderr, app="controller")
    assert err.fix_cmd == "satdeploy logs controller"


def test_match_safe_format_when_group_missing():
    """If a regex entry references {app} in fix_cmd but the caller didn't
    pass app, the matcher must not crash — it leaves the placeholder alone."""
    stderr = "backup file foo: sha256 mismatch"
    err = errors.match(stderr, app=None)
    # fix_cmd template is "satdeploy list {app}"; no app given → placeholder
    # stays. Better than crashing on KeyError mid-iterate.
    assert err is not None
    assert "{app}" in err.fix_cmd or "satdeploy list" in err.fix_cmd


# ---------------------------------------------------------------------------
# from_stderr fallback
# ---------------------------------------------------------------------------

def test_from_stderr_falls_back_to_unknown_with_preview():
    stderr = (
        "line 1 garbage\n"
        "line 2 garbage\n"
        "line 3 this is the interesting one\n"
    )
    err = errors.from_stderr(stderr, app="controller")
    assert isinstance(err, errors.UnknownError)
    assert err.exit_code == errors.EUNKNOWN
    assert "line 3" in err.message
    assert "github.com/MahmoodSeoud/satDeploy/issues" in err.message


def test_from_stderr_handles_empty_input():
    err = errors.from_stderr("", app="controller")
    assert isinstance(err, errors.UnknownError)
    assert "without stderr output" in err.message
    assert "github.com/MahmoodSeoud/satDeploy/issues" in err.message


def test_from_stderr_returns_match_when_pattern_fires():
    """from_stderr should not always wrap — when a pattern matches, it
    routes to the typed class directly."""
    err = errors.from_stderr(
        "error while loading shared libraries: libparam.so.3: cannot open",
        app="controller",
    )
    assert isinstance(err, errors.ABIError)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_format_message_without_fix_cmd_is_single_line():
    err = errors.UnknownError("something went wrong")
    rendered = err.format_message()
    # Single line, styled with red (ANSI). Presence of the typed name is the
    # load-bearing assertion; color codes are environment-dependent.
    assert "EUNKNOWN" in rendered
    assert "something went wrong" in rendered
    assert "\n" not in rendered.strip()


def test_format_message_has_single_cross_prefix():
    """Regression for DX review 2026-04-23: CLI was printing
    ``✗ ✗ [EUNKNOWN] Local file not found: ...`` — two ✗ marks. The inner
    format_message() prepended ``✗ [name]`` and then output.error() prepended
    another ``✗``. Fix lives in errors.py:format_message (drop the inner ✗)."""
    err = errors.UnknownError("file not found")
    rendered = err.format_message()
    # Exactly one ✗ in the head line.
    head = rendered.splitlines()[0]
    assert head.count("✗") == 1, f"Expected single ✗, got: {head!r}"


def test_format_message_with_fix_cmd_shows_fix_line():
    err = errors.ABIError(
        "Target is missing library libparam.so.3",
        fix_cmd="Rebuild against a Yocto SDK matching the target libc.",
        eta="12s",
    )
    rendered = err.format_message()
    assert "EABI" in rendered
    assert "Rebuild against a Yocto SDK" in rendered
    assert "12s" in rendered
    assert "\n" in rendered  # fix line lives on its own row


def test_no_production_fix_cmd_references_broken_satdeploy_commands():
    """Regression for DX review 2026-04-23 + 2026-04-24 audit: fix_cmd
    strings in errors.py must reference commands that actually exist in
    the CLI registry.

    Two classes of bug this test catches:

    1. Unshipped commands (e.g. ``satdeploy sync-sysroot`` — design doc
       line 152, never implemented). The pre-fix ERRORS table referenced
       it for 5 ABI/sysroot failures.
    2. Wrong-namespace commands (e.g. ``satdeploy debuginfod stop`` — the
       2026-04-24 ``dev`` subgroup refactor moved it to
       ``satdeploy dev debuginfod stop`` but the fix_cmd wasn't updated).

    Pilots who copy a broken fix_cmd, run it, and see
    ``No such command`` lose trust in every other error message too. The
    damage is out of proportion to the typo.

    The EGATE entry references ``satdeploy validate`` and ships coupled
    with that command per Tier 1 decision #11 — excluded here and
    checked by an explicit comment in errors.py."""
    # Known-broken strings that used to appear. Add to this list any
    # time an audit uncovers a new class.
    broken_references = (
        "satdeploy sync-sysroot",
        "satdeploy debuginfod ",  # trailing space: top-level, not `dev debuginfod`
    )
    for entry in errors.ERRORS:
        if entry.fix_cmd is None:
            continue
        # Skip the known-coupled EGATE entry.
        if entry.exit_code == errors.EGATE:
            continue
        for broken in broken_references:
            assert broken not in entry.fix_cmd, (
                f"ERRORS entry {entry.pattern.pattern!r} references the "
                f"broken command string {broken!r} in fix_cmd={entry.fix_cmd!r}."
            )


def test_format_message_fix_cmd_without_eta():
    err = errors.DebugError(
        "gdbserver missing",
        fix_cmd="Add to Yocto recipe",
        eta=None,
    )
    rendered = err.format_message()
    assert "gdbserver missing" in rendered
    assert "Add to Yocto recipe" in rendered
    # No " (None)" or "()" bleed through from missing eta.
    assert "(None)" not in rendered
    assert "()" not in rendered


# ---------------------------------------------------------------------------
# Ordering: specific patterns must match before general ones
# ---------------------------------------------------------------------------

def test_more_specific_patterns_come_before_general_ones():
    """The 'undefined symbol' ABI entry must fire before anything more
    general would swallow it. This test guards against someone reordering
    ERRORS and accidentally making ABIError unreachable for this stderr."""
    stderr = "symbol lookup error: undefined symbol: param_list_iterator_next"
    err = errors.match(stderr)
    assert isinstance(err, errors.ABIError), (
        "undefined-symbol stderr should route to ABIError, not a more "
        "general transport/apply error"
    )
