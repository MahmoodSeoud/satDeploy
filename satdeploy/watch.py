"""satdeploy watch — file watcher that fires iterate on save.

Design doc (`docs/designs/vercel-for-cubesats.md:65`):

    `satdeploy watch <app>` — file watcher (watchdog) triggers iterate on
    save. The actual daily-loop hook. Typing `iterate` 40x/day is itself
    DX friction; watch is the retention magic.

Eng-review landmines addressed (from 2026-04-19, design doc:170-191):

* **P0 #1 watch-cancel-torn-file.** The cancel point is ONLY between
  iterates, never mid-iterate. Our SIGINT handler just sets a stop event;
  the in-flight iterate runs to its transport's atomic rename, then the
  main loop sees the stop event and exits cleanly. We never raise
  ``KeyboardInterrupt`` inside ``run_iterate``.
* **P1 #8 editor-rename saves.** Editors like VSCode/Vim/JetBrains do
  atomic-rename on save — ``FileModifiedEvent`` alone misses them. We
  watch the parent directory (``recursive=False``) and hook
  ``on_modified``, ``on_created``, and ``on_moved``, filtering by the
  target basename via a resolved-path map.
* **P1 #9 debounce storms.** ``git checkout`` or save-all can fire N
  events in a burst. We debounce per-(app, resolved-path) key, not
  globally. Rapid edits within the debounce window coalesce to one
  iterate after the last event + ``debounce_s`` of quiet.
* **P2 #11 Ctrl+C + Observer threading.** Watchdog's default Observer
  runs non-daemon threads that ignore SIGINT. We install a main-thread
  signal handler that sets ``stop_event``; the main loop calls
  ``observer.stop()`` + ``observer.join()`` before returning.
* **P2 #15 macOS FSEventsObserver vs Linux inotify.** ``watchdog>=3.0``
  pinned in ``pyproject.toml``. Tests use ``PollingObserver`` (injected
  via ``observer_cls``) for deterministic timing without real inotify.
* **P2 #16 test mocking.** The handler is a module-level class driven
  directly in tests via ``handler.on_modified(FileModifiedEvent(...))``.
  No sleep-and-pray.

Cancellation semantics
----------------------
A ``Ctrl+C`` (or ``SIGTERM``) cancels the *watch loop*, not the
*in-flight iterate*. If you hit Ctrl+C while an iterate is uploading,
the iterate finishes (runs to the transport's atomic rename), then the
loop exits. This is the only way to prevent torn files on the target.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from watchdog.events import (
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

from satdeploy import errors, iterate as iterate_mod
from satdeploy.config import Config, ModuleConfig

_DEFAULT_DEBOUNCE_S = 0.3
_DEFAULT_POLL_INTERVAL_S = 0.05


@dataclass
class WatchState:
    """Shared state between the event handler and the drain loop.

    ``pending[app] = monotonic time of most recent qualifying event``.
    Stored under ``lock``; readers (drain loop) and writers (handler
    callbacks) may run on different threads.
    """

    path_to_app: dict[Path, str]
    pending: dict[str, float] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


class IterateHandler(FileSystemEventHandler):
    """Watchdog handler that records qualifying save events into
    ``state.pending``.

    A "qualifying event" is one whose resolved file path matches one of
    the watched apps in ``state.path_to_app``. Directory events are
    ignored. Atomic-rename saves land via ``on_moved`` (dest path).
    """

    def __init__(self, state: WatchState,
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__()
        self._state = state
        self._clock = clock

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._record(event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._record(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        # Editors atomic-rename into place; the interesting path is the
        # DEST, not the temp-file SRC. Landmine P1 #8.
        dest = getattr(event, "dest_path", None)
        if dest:
            self._record(dest)

    def _record(self, path_str: str) -> None:
        try:
            path = Path(path_str).resolve()
        except (OSError, RuntimeError):
            return
        app = self._state.path_to_app.get(path)
        if app is None:
            return
        with self._state.lock:
            self._state.pending[app] = self._clock()


def drain_pending(state: WatchState, debounce_s: float,
                  now: float) -> list[str]:
    """Return the apps whose last event is at least ``debounce_s`` old,
    and remove them from ``state.pending``.

    Caller is expected to then fire iterate for each. Called from the
    drain loop only; still takes ``state.lock`` because the handler may
    be pushing new events concurrently.
    """
    ready: list[str] = []
    with state.lock:
        for app, last_ts in list(state.pending.items()):
            if now - last_ts >= debounce_s:
                ready.append(app)
                del state.pending[app]
    return ready


def _resolve_watch_targets(config: Config,
                           apps: Iterable[str]) -> list[tuple[str, Path]]:
    """Turn app names into (app_name, absolute_local_path) pairs.

    Raises ``UnknownError`` for unknown apps or missing local files. We
    do NOT require the file to exist as of ``run_watch`` start — tests
    create the file later; the handler's ``on_created`` is what triggers
    the first iterate. What we DO require is a parseable local path.
    """
    targets: list[tuple[str, Path]] = []
    for app in apps:
        app_config = config.get_app(app)
        if app_config is None:
            known = ", ".join(sorted(config.get_all_app_names())) or "(none)"
            raise errors.UnknownError(
                f"No app named {app!r} in config {config.config_path}. "
                f"Known apps: {known}."
            )
        local = Path(os.path.expanduser(app_config.local))
        # Resolve against the parent dir even if the file doesn't exist
        # yet — watchdog watches the parent anyway.
        try:
            resolved = local.resolve(strict=False)
        except (OSError, RuntimeError) as e:
            raise errors.UnknownError(
                f"Could not resolve local path for {app!r}: {e}"
            )
        if not resolved.parent.exists():
            raise errors.UnknownError(
                f"Parent directory does not exist for {app!r}: "
                f"{resolved.parent}"
            )
        targets.append((app, resolved))
    return targets


IterateFn = Callable[..., iterate_mod.IterateResult]


def run_watch(
    config: Config,
    module_config: ModuleConfig,
    apps: list[str],
    *,
    debounce_s: float = _DEFAULT_DEBOUNCE_S,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    on_step: Optional[Callable[[str], None]] = None,
    iterate_fn: Optional[IterateFn] = None,
    observer_factory: Optional[Callable[[], object]] = None,
    stop_event: Optional[threading.Event] = None,
    install_signal_handlers: bool = True,
) -> None:
    """Watch the configured local path(s) for each app and fire iterate
    on save. Runs until SIGINT/SIGTERM or ``stop_event`` is set.

    Args:
        config: Parsed project config.
        module_config: Target to deploy to.
        apps: One or more app names from the config.
        debounce_s: Per-(app, path) quiet window before firing iterate.
            Defaults to 0.3s — enough to coalesce editor save-all bursts
            without feeling laggy.
        poll_interval_s: How often the drain loop wakes to check the
            pending dict. Defaults to 50ms.
        on_step: Callback for human-readable progress lines.
        iterate_fn: Injection point for tests. Defaults to
            ``iterate.run_iterate``. Signature: ``iterate_fn(config,
            module_config, app, *, on_step=...) -> IterateResult``.
        observer_factory: Zero-arg factory returning a watchdog Observer
            instance. Defaults to ``Observer``. Tests pass
            ``PollingObserver`` for deterministic timing.
        stop_event: Caller-provided stop flag. If ``None``, we create one
            and wire SIGINT/SIGTERM to it (unless
            ``install_signal_handlers=False``).
        install_signal_handlers: Whether to install SIGINT/SIGTERM
            handlers. Must be called from the main thread when True.

    Raises:
        UnknownError: If any requested app is not in the config.
    """
    step = on_step or (lambda _: None)
    iterate_fn = iterate_fn or iterate_mod.run_iterate
    observer_factory = observer_factory or Observer

    if not apps:
        raise errors.UnknownError("satdeploy watch requires at least one app name.")

    targets = _resolve_watch_targets(config, apps)
    state = WatchState(path_to_app={p: name for name, p in targets})

    handler = IterateHandler(state)
    observer = observer_factory()
    parent_dirs = {p.parent for _, p in targets}
    for d in parent_dirs:
        observer.schedule(handler, str(d), recursive=False)

    if stop_event is None:
        stop_event = threading.Event()

    prev_handlers: list[tuple[int, object]] = []
    if install_signal_handlers:
        def _handler(signum, frame):  # noqa: ARG001
            stop_event.set()
        for sig in (signal.SIGINT, signal.SIGTERM):
            prev_handlers.append((sig, signal.signal(sig, _handler)))

    step(f"watch: {len(targets)} app(s) across {len(parent_dirs)} dir(s); "
         f"debounce={debounce_s:.2f}s")
    for app, path in targets:
        step(f"  {app} <- {path}")
    step("  Ctrl+C to stop")

    observer.start()
    try:
        while not stop_event.is_set():
            ready = drain_pending(state, debounce_s, time.monotonic())
            for app in ready:
                if stop_event.is_set():
                    break
                _fire_iterate(iterate_fn, config, module_config, app, step)
            stop_event.wait(poll_interval_s)
    finally:
        try:
            observer.stop()
            observer.join(timeout=2.0)
        except Exception:
            pass  # best-effort teardown
        for sig, prev in prev_handlers:
            try:
                signal.signal(sig, prev)
            except (ValueError, TypeError):
                pass  # not the main thread, or prev is None

    step("watch: stopped")


def _fire_iterate(iterate_fn: IterateFn, config: Config,
                  module_config: ModuleConfig, app: str,
                  step: Callable[[str], None]) -> None:
    """Run iterate once for ``app``, log the outcome, swallow typed
    errors so the loop keeps running.

    Unexpected exceptions (not ``TypedError``) propagate only if they're
    ``KeyboardInterrupt`` or ``SystemExit``; anything else is logged and
    suppressed. Rationale: watch must not die on one bad iterate — the
    user would lose their daily-loop hook to any transient failure.
    """
    step(f"watch: firing iterate for {app}")
    t0 = time.monotonic()
    try:
        result = iterate_fn(config, module_config, app,
                            on_step=lambda m: step(f"    {m}"))
    except errors.TypedError as e:
        elapsed = time.monotonic() - t0
        step(f"watch: {app} FAILED in {elapsed:.1f}s "
             f"[{e.typed_name}] {e}")
        return
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as e:  # noqa: BLE001 — deliberate catch-all
        elapsed = time.monotonic() - t0
        step(f"watch: {app} FAILED in {elapsed:.1f}s "
             f"[unexpected {type(e).__name__}] {e}")
        return
    elapsed = time.monotonic() - t0
    step(f"watch: {app} ok in {elapsed:.1f}s (hash={result.file_hash})")
