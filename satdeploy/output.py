"""Output formatting for the satdeploy CLI.

Goals: aligned columns, quiet color, consistent symbols, and a deploy summary
that actually tells you what happened. Callers build a small dict/tuple of
rendering data and hand it to one of the render_* functions — no more
inline click.echo with \\t and string padding.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Sequence

import click

# ---------------------------------------------------------------------------
# Symbols — one legend, used everywhere
# ---------------------------------------------------------------------------
#
# Some of these names are locked by tests/test_output.py. Values can change
# (other tests check `SYMBOLS[name] in result.output` which reads the current
# value), so we pick glyphs that read well in a modern terminal.

SYMBOLS = {
    "check": "●",   # healthy / running
    "cross": "✗",   # failed / error
    "arrow": "→",   # pointer ("current", "deployed")
    "bullet": "·",  # secondary / inactive
    "drift": "◐",   # half-filled — partial / drift / stopped
    "step":  "✓",   # a push step that succeeded
    "rule":  "─",   # horizontal rule
}

# Color helpers — thin wrappers so we can swap theming later.
_DIM = "bright_black"
_ACCENT = "cyan"
_OK = "green"
_WARN = "yellow"
_BAD = "red"


def dim(text: str) -> str:
    return click.style(text, fg=_DIM)


def accent(text: str) -> str:
    return click.style(text, fg=_ACCENT)


# ---------------------------------------------------------------------------
# Simple formatters (backwards-compatible API)
# ---------------------------------------------------------------------------

def success(message: str) -> str:
    """Green line with the success glyph."""
    return click.style(f"{SYMBOLS['check']} {message}", fg=_OK)


def warning(message: str) -> str:
    """Yellow line prefixed with a warning glyph."""
    return click.style(f"! {message}", fg=_WARN)


def error(message: str) -> str:
    """Red line prefixed with a cross."""
    return click.style(f"{SYMBOLS['cross']} {message}", fg=_BAD)


def step(current: int, total: int, message: str) -> str:
    """`[1/5] message` with a dim counter. Tests lock the `[N/M]` shape."""
    counter = click.style(f"[{current}/{total}]", fg=_DIM)
    return f"{counter} {message}"


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def normalize_timestamp(raw: Optional[str]) -> str:
    """Return `YYYY-MM-DD HH:MM:SS` for any reasonable input, or '-'."""
    if not raw:
        return "-"
    try:
        # Accept ISO with 'T', a space, or trailing 'Z'.
        s = raw.replace("Z", "")
        dt = datetime.fromisoformat(s)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return raw


def format_relative_time(raw: Optional[str]) -> str:
    """`2m ago`, `3h ago`, `just now`, or the raw date for older stuff."""
    if not raw:
        return "-"
    try:
        s = raw.replace("Z", "")
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return raw
    if dt.tzinfo is None:
        now = datetime.now()
    else:
        now = datetime.now(timezone.utc).astimezone(dt.tzinfo)
    delta = now - dt
    secs = int(delta.total_seconds())
    if secs < 0:
        return "just now"
    if secs < 10:
        return "just now"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    if secs < 86400 * 7:
        return f"{secs // 86400}d ago"
    return dt.strftime("%Y-%m-%d")


def format_absolute_time(raw: Optional[str]) -> str:
    """`YYYY-MM-DD HH:MM:SS` in local time, or `-` if missing/unparseable."""
    if not raw:
        return "-"
    try:
        s = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return raw
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Target header — `name · transport · endpoint`
# ---------------------------------------------------------------------------

def render_target_header(
    *, name: str, transport: str, endpoint: str,
) -> str:
    """One-line header shown at the top of status/list.

    Example:
        demo · local · /Users/mahmood/.satdeploy/demo/target
    """
    parts = [
        click.style(name, fg=_ACCENT, bold=True),
        dim(" · "),
        click.style(transport, fg=_OK),
        dim(" · "),
        dim(endpoint or "-"),
    ]
    return "  " + "".join(parts)


def target_endpoint(module_config) -> str:
    """Pick the best one-line endpoint label for a transport config."""
    t = getattr(module_config, "transport", None)
    if t == "csp":
        endpoint = getattr(module_config, "zmq_endpoint", None) or ""
        node = getattr(module_config, "agent_node", None)
        if node:
            return f"{endpoint}  (node {node})" if endpoint else f"node {node}"
        return endpoint or "-"
    if t == "ssh":
        host = getattr(module_config, "host", None) or "-"
        user = getattr(module_config, "user", None) or ""
        return f"{user}@{host}" if user else host
    if t == "local":
        return getattr(module_config, "target_dir", None) or "-"
    return "-"


# ---------------------------------------------------------------------------
# Status table
# ---------------------------------------------------------------------------

@dataclass
class StatusRow:
    app: str
    state: str               # running | stopped | deployed | failed | not deployed
    file_hash: str           # short hash or "-"
    git_prov: Optional[str]  # "main 7c8a8751" or None
    remote_path: str
    age: Optional[str] = None  # raw ISO for the last push/rollback


_STATE_STYLE = {
    "running":      (SYMBOLS["check"],  _OK,    "running"),
    "deployed":     (SYMBOLS["check"],  _OK,    "deployed"),
    "stopped":      (SYMBOLS["drift"],  _WARN,  "stopped"),
    "failed":       (SYMBOLS["cross"],  _BAD,   "failed"),
    "unknown":      (SYMBOLS["drift"],  _DIM,   "unknown"),
    "not deployed": (SYMBOLS["bullet"], _WARN,  "not deployed"),
}


def render_status_table(*, rows: Sequence[StatusRow]) -> str:
    """Build the full status block: header + rows. Returns the whole string."""

    if not rows:
        return "  " + dim("No apps configured or deployed.")

    # Column widths — computed from content so nothing ever wraps.
    w_app = max(8,  max(len(r.app) for r in rows))
    w_state = 11  # room for "not deployed"
    w_hash = max(8, max(len(r.file_hash or "-") for r in rows))
    w_git = max(
        8,
        max(len(r.git_prov or "") for r in rows),
    )
    w_age = 19  # "YYYY-MM-DD HH:MM:SS"
    w_path = max(4, max(len(r.remote_path or "-") for r in rows))

    lines = []
    # Header
    header = (
        f"  {'APP':<{w_app}}  "
        f"{'STATE':<{w_state + 2}}  "
        f"{'HASH':<{w_hash}}  "
        f"{'GIT':<{w_git}}  "
        f"{'TIMESTAMP':<{w_age}}  "
        f"{'PATH':<{w_path}}"
    )
    lines.append(dim(header))
    ruler = (
        "  "
        + SYMBOLS["rule"] * w_app + "  "
        + SYMBOLS["rule"] * (w_state + 2) + "  "
        + SYMBOLS["rule"] * w_hash + "  "
        + SYMBOLS["rule"] * w_git + "  "
        + SYMBOLS["rule"] * w_age + "  "
        + SYMBOLS["rule"] * w_path
    )
    lines.append(dim(ruler))

    for r in rows:
        symbol, color, label = _STATE_STYLE.get(r.state, _STATE_STYLE["unknown"])
        state_cell = f"{symbol} {label}"
        state_cell_plain = f"{symbol} {label}"  # measured length
        pad = (w_state + 2) - len(state_cell_plain)
        state_rendered = click.style(state_cell, fg=color) + " " * max(0, pad)

        hash_rendered = click.style(f"{r.file_hash or '-':<{w_hash}}", fg="white")
        git_rendered = dim(f"{(r.git_prov or '-'):<{w_git}}")
        age_raw = format_absolute_time(r.age) if r.age else "-"
        age_rendered = dim(f"{age_raw:<{w_age}}")
        path_rendered = dim(f"{(r.remote_path or '-'):<{w_path}}")
        app_rendered = click.style(f"{r.app:<{w_app}}", bold=True)

        lines.append(
            f"  {app_rendered}  {state_rendered}  {hash_rendered}  "
            f"{git_rendered}  {age_rendered}  {path_rendered}"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Versions table (list)
# ---------------------------------------------------------------------------

@dataclass
class VersionRow:
    file_hash: str
    git_prov: Optional[str]
    timestamp: Optional[str]  # raw ISO or pre-formatted
    is_deployed: bool


def render_list_table(*, app: str, rows: Sequence[VersionRow]) -> str:
    if not rows:
        return dim(f"  No versions found for {app}.")

    w_hash = max(8, max(len(r.file_hash or "-") for r in rows))
    w_git = max(6, max(len(r.git_prov or "") for r in rows))
    w_time = 19  # "YYYY-MM-DD HH:MM:SS"

    header_line = (
        f"  {app}  "
        + dim(f"· {len(rows)} version{'s' if len(rows) != 1 else ''}")
    )

    col_header = dim(
        f"     {'HASH':<{w_hash}}  {'GIT':<{w_git}}  "
        f"{'TIMESTAMP':<{w_time}}  STATUS"
    )

    lines = [header_line, "", col_header]

    for r in rows:
        if r.is_deployed:
            marker = click.style(SYMBOLS["arrow"], fg=_OK)
            hash_col = click.style(f"{r.file_hash or '-':<{w_hash}}", fg=_OK)
            status_col = click.style("deployed", fg=_OK)
        else:
            marker = dim(SYMBOLS["bullet"])
            hash_col = click.style(f"{r.file_hash or '-':<{w_hash}}", fg="white")
            status_col = dim("backup")

        git_col = dim(f"{(r.git_prov or '-'):<{w_git}}")
        ts_col = dim(f"{normalize_timestamp(r.timestamp):<{w_time}}")
        lines.append(f"   {marker} {hash_col}  {git_col}  {ts_col}  {status_col}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Push summary
# ---------------------------------------------------------------------------

@dataclass
class PushStep:
    label: str           # "backup", "upload", "verify", "service"
    detail: str          # "32c0702b-...bak" etc.
    ok: bool = True      # False → render with cross, not check
    skipped: bool = False  # True → render with bullet


def render_push_header(
    *, app: str, target_name: str,
    old_hash: Optional[str], new_hash: str,
    old_git: Optional[str], new_git: Optional[str],
) -> str:
    arrow = click.style(" → ", fg=_DIM)
    hero = (
        "  "
        + click.style(SYMBOLS["check"], fg=_ACCENT)
        + " Deploying "
        + click.style(app, bold=True)
        + dim(" → ")
        + click.style(target_name, fg=_ACCENT)
        + "\n"
    )

    def fmt(h: Optional[str], g: Optional[str], tag: str) -> str:
        h_col = click.style((h or "-")[:8].ljust(8), fg="white")
        g_col = dim((g or "-").ljust(20))
        tag_col = dim(f"({tag})")
        return f"     {h_col}  {g_col}  {tag_col}"

    if old_hash and old_hash != new_hash:
        body = (
            fmt(old_hash, old_git, "current") + "\n"
            + fmt(new_hash, new_git, "new")
        )
    else:
        body = fmt(new_hash, new_git, "deploying")

    return hero + "\n" + body + "\n"


def render_push_step(s: PushStep) -> str:
    if s.skipped:
        glyph = dim(SYMBOLS["bullet"])
        label = dim(f"{s.label:<10}")
    elif s.ok:
        glyph = click.style(SYMBOLS["step"], fg=_OK)
        label = click.style(f"{s.label:<10}", fg=_OK)
    else:
        glyph = click.style(SYMBOLS["cross"], fg=_BAD)
        label = click.style(f"{s.label:<10}", fg=_BAD)
    detail = dim(s.detail)
    return f"  {glyph}  {label}  {detail}"


def render_push_footer(
    *, duration_s: float, rollback_hint: str,
) -> str:
    took = dim(f"Deployed in {duration_s:.2f}s.")
    hint = dim("Rollback with: ") + click.style(rollback_hint, fg=_ACCENT)
    return f"\n  {took}  {hint}"


# ---------------------------------------------------------------------------
# Rollback header
# ---------------------------------------------------------------------------

def render_rollback_header(
    *, app: str, target_name: str,
    from_hash: Optional[str], to_hash: Optional[str],
    to_timestamp: Optional[str],
) -> str:
    hero = (
        "  "
        + click.style(SYMBOLS["arrow"], fg=_WARN)
        + " Rolling back "
        + click.style(app, bold=True)
        + dim(" on ")
        + click.style(target_name, fg=_ACCENT)
    )
    if from_hash and to_hash:
        detail = (
            "     "
            + click.style((from_hash or "-")[:12], fg="white")
            + dim("  →  ")
            + click.style((to_hash or "-")[:12], fg=_OK)
        )
        if to_timestamp:
            detail += dim(f"   ({normalize_timestamp(to_timestamp)})")
        return hero + "\n\n" + detail + "\n"
    return hero + "\n"


# ---------------------------------------------------------------------------
# Config renderer
# ---------------------------------------------------------------------------

def render_config_block(*, cfg, module) -> str:
    lines = []
    lines.append(
        render_target_header(
            name=module.name,
            transport=module.transport,
            endpoint=target_endpoint(module),
        )
    )
    lines.append("")
    lines.append("  " + dim("config file: ") + str(cfg.config_path))
    lines.append("  " + dim("backup dir:  ") + str(cfg.get_backup_dir(module.name)))
    if getattr(module, "appsys_node", None):
        lines.append("  " + dim("appsys node: ") + str(module.appsys_node))
    else:
        lines.append("  " + dim("appsys node: 0  (restart disabled)"))

    apps = cfg.apps or {}
    lines.append("")
    lines.append("  " + click.style(f"Apps ({len(apps)})", bold=True))
    if not apps:
        lines.append("  " + dim("(none configured)"))
        return "\n".join(lines)

    for app_name, app_data in apps.items():
        lines.append("")
        lines.append(
            "  "
            + click.style(SYMBOLS["arrow"], fg=_ACCENT)
            + " "
            + click.style(app_name, bold=True)
        )
        lines.append("    " + dim("local:   ") + str(app_data.get("local", "-")))
        lines.append("    " + dim("remote:  ") + str(app_data.get("remote", "-")))
        if app_data.get("service"):
            lines.append("    " + dim("service: ") + str(app_data["service"]))
        if app_data.get("param"):
            lines.append("    " + dim("param:   ") + str(app_data["param"]))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Exception styling (unchanged behavior, simpler code)
# ---------------------------------------------------------------------------

class SatDeployError(click.ClickException):
    """Custom exception that displays error messages in red."""

    def format_message(self) -> str:
        return error(self.message)

    def show(self, file=None):
        if file is None:
            file = click.get_text_stream("stderr")
        click.echo(self.format_message(), file=file)


def _style_exception(e: click.ClickException) -> None:
    if isinstance(e, SatDeployError):
        return
    original_format = e.format_message
    e.format_message = lambda: error(original_format())

    def custom_show(file=None):
        if file is None:
            file = click.get_text_stream("stderr")
        click.echo(e.format_message(), file=file)

    e.show = custom_show


def _find_satdeploy_binaries_on_path() -> List[str]:
    """Return every `satdeploy` binary found on $PATH, in PATH order, deduped by realpath.

    Used by the shadow-binary hint: when a user runs `satdeploy iterate`
    (or any other command) and sees `No such command`, the most common
    cause is that an older `/usr/local/bin/satdeploy` is shadowing the
    venv install they just did. DX review 2026-04-23 flagged this as the
    #1 silent footgun in onboarding.
    """
    path_dirs = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    seen_real: set[str] = set()
    unique: List[str] = []
    for d in path_dirs:
        cand = os.path.join(d, "satdeploy")
        if not (os.path.isfile(cand) and os.access(cand, os.X_OK)):
            continue
        try:
            rp = os.path.realpath(cand)
        except OSError:
            continue
        if rp in seen_real:
            continue
        seen_real.add(rp)
        unique.append(cand)
    return unique


def shadow_binary_hint() -> Optional[str]:
    """Return a user-facing hint if >1 distinct `satdeploy` binaries are on $PATH.

    None when the hint isn't useful (no PATH conflict detected). The
    classic trigger is `/usr/local/bin/satdeploy` (old system install)
    shadowing a newer `.venv/bin/satdeploy` when the venv isn't active.
    """
    binaries = _find_satdeploy_binaries_on_path()
    if len(binaries) <= 1:
        return None
    running = sys.argv[0] if sys.argv else "satdeploy"
    lines = [f"Found {len(binaries)} different `satdeploy` binaries on your PATH:"]
    for b in binaries:
        marker = "  ← invoked" if os.path.realpath(b) == os.path.realpath(running) else ""
        lines.append(f"  {b}{marker}")
    lines.append("")
    lines.append(
        "If this command exists in one of the other installs, your PATH is "
        "resolving to the wrong one. Activate your venv, or reinstall with "
        "`pip install --force-reinstall`."
    )
    return "\n".join(lines)


class ColoredGroup(click.Group):
    """Custom Click group that displays all errors in red.

    Also wraps "No such command" UsageErrors with a shadow-binary hint
    when multiple `satdeploy` binaries exist on $PATH. See
    `shadow_binary_hint` for the DX context.
    """

    def resolve_command(self, ctx, args):
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError as exc:
            msg = exc.format_message()
            if "No such command" not in msg:
                raise
            hint = shadow_binary_hint()
            if hint is None:
                raise
            # Re-raise with the hint appended so the user sees it alongside
            # Click's default "did you mean" suggestion (if any).
            raise click.UsageError(f"{msg}\n\n{hint}", ctx=exc.ctx) from None

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except click.ClickException as e:
            _style_exception(e)
            raise

    def main(self, *args, **kwargs):
        try:
            return super().main(*args, **kwargs)
        except click.ClickException as e:
            _style_exception(e)
            raise
