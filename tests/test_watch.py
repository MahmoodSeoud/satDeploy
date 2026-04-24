"""Tests for satdeploy.watch — the daily-loop retention hook.

Per eng-review landmine P2 #16 (design doc:191): we drive handler events
directly via ``handler.on_modified(FileModifiedEvent(...))`` rather than
sleep-and-pray with real filesystem churn. The debounce math, the
unknown-path filter, the atomic-rename save handling, and the stop-event
semantics are all tested as pure logic. One end-to-end test goes through
``PollingObserver`` with a real tmp file to prove the wiring is correct.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from watchdog.events import (
    DirModifiedEvent,
    FileCreatedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)
from watchdog.observers.polling import PollingObserver

from satdeploy import errors, iterate, watch
from satdeploy.config import AppConfig, Config, ModuleConfig
from satdeploy.transport.base import DeployResult


# ---------------------------------------------------------------------------
# Fixtures (parallel to tests/test_iterate.py for consistency)
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_binary(tmp_path):
    """A tiny ELF-ish file standing in for a compiled app binary."""
    bin_path = tmp_path / "controller"
    bin_path.write_bytes(b"\x7fELF" + b"\x00" * 256)
    return bin_path


@pytest.fixture
def fake_config(tmp_path, tmp_binary):
    """A Config stub with one 'controller' app whose local path is
    ``tmp_binary``. Matches the real Config surface (spec=Config)."""
    cfg = MagicMock(spec=Config)
    cfg.config_path = tmp_path / "config.yaml"
    cfg.history_path = tmp_path / "history.db"
    cfg.get_backup_dir = lambda name: str(tmp_path / "backups" / name)
    cfg.get_all_app_names = lambda: ["controller", "libparam"]
    cfg.get_app = lambda name: {
        "controller": AppConfig(
            name="controller",
            local=str(tmp_binary),
            remote="/opt/disco/bin/controller",
            service="controller.service",
        ),
        "libparam": AppConfig(
            name="libparam",
            local=str(tmp_binary.parent / "libparam.so"),
            remote="/usr/lib/libparam.so",
            service=None,
        ),
    }.get(name)
    return cfg


@pytest.fixture
def fake_module():
    return ModuleConfig(name="som1", transport="ssh", host="10.0.0.42", user="root")


# ---------------------------------------------------------------------------
# Handler-level unit tests (P2 #16 — drive events directly)
# ---------------------------------------------------------------------------

def test_handler_on_modified_records_known_path(tmp_binary):
    """Happy path: modified event for a watched path writes pending entry."""
    state = watch.WatchState(path_to_app={tmp_binary.resolve(): "controller"})
    handler = watch.IterateHandler(state, clock=lambda: 123.0)

    handler.on_modified(FileModifiedEvent(str(tmp_binary)))

    assert state.pending == {"controller": 123.0}


def test_handler_on_modified_ignores_unknown_path(tmp_path):
    """Events for paths not in path_to_app are silently dropped (no pending)."""
    other = tmp_path / "not-watched"
    other.write_text("noise")
    state = watch.WatchState(
        path_to_app={(tmp_path / "watched").resolve(): "controller"}
    )
    handler = watch.IterateHandler(state)

    handler.on_modified(FileModifiedEvent(str(other)))

    assert state.pending == {}


def test_handler_ignores_directory_events(tmp_binary):
    """A DirModifiedEvent on the parent must never cause a pending
    iterate — the parent dir is watched, but only file changes count."""
    state = watch.WatchState(path_to_app={tmp_binary.resolve(): "controller"})
    handler = watch.IterateHandler(state)

    handler.on_modified(DirModifiedEvent(str(tmp_binary.parent)))

    assert state.pending == {}


def test_handler_on_created_records_known_path(tmp_binary):
    """VSCode/atom some editors fire created on atomic rename — we catch it."""
    state = watch.WatchState(path_to_app={tmp_binary.resolve(): "controller"})
    handler = watch.IterateHandler(state, clock=lambda: 42.0)

    handler.on_created(FileCreatedEvent(str(tmp_binary)))

    assert state.pending == {"controller": 42.0}


def test_handler_on_moved_uses_dest_path_for_editor_rename_saves(tmp_binary):
    """P1 #8: VSCode/Vim/JetBrains atomic-rename saves. The SRC path is
    the temp file (ignorable); the DEST path is the watched file. The
    handler must key off DEST, not SRC.
    """
    state = watch.WatchState(path_to_app={tmp_binary.resolve(): "controller"})
    handler = watch.IterateHandler(state, clock=lambda: 10.0)

    # Temp file (src) is not watched; the dest (the real file) is.
    tmp_src = tmp_binary.parent / ".controller.swp"
    handler.on_moved(FileMovedEvent(str(tmp_src), str(tmp_binary)))

    assert state.pending == {"controller": 10.0}


def test_handler_updates_timestamp_on_subsequent_events(tmp_binary):
    """Rapid save bursts keep bumping the timestamp, so drain_pending
    doesn't fire until there's quiet for ``debounce_s`` — this is what
    makes P1 #9 (global debounce storms) into a per-key, coalescing
    debounce instead of N iterates for one burst.
    """
    state = watch.WatchState(path_to_app={tmp_binary.resolve(): "controller"})
    clock = [100.0]
    handler = watch.IterateHandler(state, clock=lambda: clock[0])

    handler.on_modified(FileModifiedEvent(str(tmp_binary)))
    clock[0] = 100.1
    handler.on_modified(FileModifiedEvent(str(tmp_binary)))
    clock[0] = 100.2
    handler.on_modified(FileModifiedEvent(str(tmp_binary)))

    assert state.pending == {"controller": 100.2}


def test_handler_on_moved_without_dest_is_noop(tmp_binary):
    """Some platforms/events lack dest_path; handler must not crash."""
    state = watch.WatchState(path_to_app={tmp_binary.resolve(): "controller"})
    handler = watch.IterateHandler(state)

    evt = FileMovedEvent(str(tmp_binary), "")
    handler.on_moved(evt)  # empty dest_path — no pending record

    assert state.pending == {}


# ---------------------------------------------------------------------------
# drain_pending logic
# ---------------------------------------------------------------------------

def test_drain_pending_returns_ready_apps_and_clears_them(tmp_binary):
    """Apps whose last event is older than debounce_s get returned and
    removed; apps still in the quiet window stay."""
    state = watch.WatchState(path_to_app={})
    state.pending = {"controller": 100.0, "libparam": 100.9}

    ready = watch.drain_pending(state, debounce_s=0.3, now=101.0)

    assert ready == ["controller"]
    assert state.pending == {"libparam": 100.9}


def test_drain_pending_empty_state_returns_empty_list():
    state = watch.WatchState(path_to_app={})
    assert watch.drain_pending(state, 0.3, time.monotonic()) == []


def test_drain_pending_per_app_debounce_is_independent(tmp_binary):
    """P1 #9: per-(app, path) debounce, not global. App A being in its
    debounce window does not block app B from firing."""
    state = watch.WatchState(path_to_app={})
    state.pending = {"app_a": 100.0, "app_b": 100.5}

    # At t=100.4: only app_a is past 0.3s window.
    assert sorted(watch.drain_pending(state, 0.3, 100.4)) == ["app_a"]
    assert state.pending == {"app_b": 100.5}

    # At t=100.9: app_b now past window too.
    assert watch.drain_pending(state, 0.3, 100.9) == ["app_b"]
    assert state.pending == {}


# ---------------------------------------------------------------------------
# _resolve_watch_targets
# ---------------------------------------------------------------------------

def test_resolve_targets_unknown_app_raises(fake_config):
    with pytest.raises(errors.UnknownError) as exc:
        watch._resolve_watch_targets(fake_config, ["does_not_exist"])
    assert "does_not_exist" in str(exc.value)
    assert "controller" in str(exc.value)  # lists known apps


def test_resolve_targets_missing_parent_dir_raises(fake_config, tmp_path):
    """If the configured local path points at a directory that doesn't
    exist, we can't watch the parent — refuse loudly rather than silently
    watching nothing."""
    cfg = MagicMock(spec=Config)
    cfg.config_path = tmp_path / "cfg.yaml"
    cfg.get_all_app_names = lambda: ["ghost"]
    cfg.get_app = lambda name: AppConfig(
        name="ghost",
        local=str(tmp_path / "no_such_dir" / "binary"),
        remote="/opt/bin/ghost",
        service=None,
    ) if name == "ghost" else None

    with pytest.raises(errors.UnknownError) as exc:
        watch._resolve_watch_targets(cfg, ["ghost"])
    assert "Parent directory" in str(exc.value)


def test_resolve_targets_returns_absolute_paths(fake_config, tmp_binary):
    targets = watch._resolve_watch_targets(fake_config, ["controller"])
    assert len(targets) == 1
    name, path = targets[0]
    assert name == "controller"
    assert path == tmp_binary.resolve()
    assert path.is_absolute()


# ---------------------------------------------------------------------------
# run_watch loop behavior
# ---------------------------------------------------------------------------

def test_run_watch_exits_cleanly_on_stop_event(fake_config, fake_module, tmp_binary):
    """Stop event set before loop starts → loop returns immediately
    (observer started + stopped, no iterate fires)."""
    iterate_mock = MagicMock(return_value=iterate.IterateResult(
        app="controller", file_hash="deadbeef", elapsed_s=0.1
    ))
    stop = threading.Event()
    stop.set()  # pre-set so the while loop exits on first check

    watch.run_watch(
        fake_config, fake_module, ["controller"],
        iterate_fn=iterate_mock,
        observer_factory=PollingObserver,
        stop_event=stop,
        install_signal_handlers=False,
        poll_interval_s=0.01,
    )

    assert iterate_mock.call_count == 0


def test_run_watch_rejects_empty_apps(fake_config, fake_module):
    with pytest.raises(errors.UnknownError):
        watch.run_watch(
            fake_config, fake_module, [],
            install_signal_handlers=False,
            stop_event=threading.Event(),
        )


def test_run_watch_fires_iterate_on_save(fake_config, fake_module, tmp_binary):
    """End-to-end: tweak the file, PollingObserver picks it up, the
    drain loop fires iterate. This is the one test that goes through
    real filesystem events — kept small and deterministic via
    PollingObserver + short debounce."""
    call_times: list[float] = []

    def _iterate(config, module_config, app, on_step=None):
        call_times.append(time.monotonic())
        return iterate.IterateResult(app=app, file_hash="cafef00d", elapsed_s=0.01)

    stop = threading.Event()

    def _drive():
        # Wait long enough for the polling observer to notice baseline,
        # then touch the file, then wait for iterate to fire, then stop.
        time.sleep(0.2)
        tmp_binary.write_bytes(b"\x7fELF" + b"\x01" * 256)
        # Wait for iterate to fire. Debounce is 0.1s, poll 0.02s,
        # PollingObserver default timeout 1s — budget ~2s.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not call_times:
            time.sleep(0.05)
        stop.set()

    driver = threading.Thread(target=_drive)
    driver.start()
    watch.run_watch(
        fake_config, fake_module, ["controller"],
        debounce_s=0.1,
        poll_interval_s=0.02,
        iterate_fn=_iterate,
        observer_factory=lambda: PollingObserver(timeout=0.1),
        stop_event=stop,
        install_signal_handlers=False,
    )
    driver.join(timeout=5.0)

    assert len(call_times) >= 1, "iterate should have fired at least once"


def test_run_watch_swallows_iterate_typed_error(fake_config, fake_module, tmp_binary):
    """A TypedError from iterate must not kill the loop. The user's
    daily-loop hook must survive a single bad iterate."""
    calls = [0]

    def _iterate(config, module_config, app, on_step=None):
        calls[0] += 1
        raise errors.ABIError(
            "missing libparam.so.3",
            fix_cmd="Rebuild against a SDK matching the target libc.",
        )

    # Directly pump an event into the drain queue — no need for the
    # observer to actually fire.
    stop = threading.Event()
    state_ref: list[watch.WatchState] = []

    original_resolve = watch._resolve_watch_targets

    def _capture_targets(config, apps):
        targets = original_resolve(config, apps)
        state_ref.append(targets)
        return targets

    # Spin up the loop in a thread, push an event manually, wait for
    # iterate to be called, stop.
    def _drive():
        deadline = time.monotonic() + 3.0
        # Wait until loop is running.
        while time.monotonic() < deadline and not state_ref:
            time.sleep(0.02)
        # Push a pending event via the same path the handler would.
        # We need the state — reach into the module-level hook below.
        pass

    with patch("satdeploy.watch._resolve_watch_targets", side_effect=_capture_targets):
        # Simpler: manually advance by seeding pending via the handler.
        # We can't reach the state from outside run_watch, so use a
        # shorter approach: pre-write a dummy event into drain_pending.
        # Instead: just inject a threading-friendly iterate that fails
        # twice, then let the observer pick up a file change to fire.
        # But writing the file during run_watch start is racy. Use
        # explicit event injection via the handler-level unit test logic.
        pass

    # Simpler equivalent test: drive _fire_iterate directly. The loop's
    # swallow-behavior IS _fire_iterate's behavior — tested here.
    step_lines: list[str] = []
    watch._fire_iterate(
        iterate_fn=_iterate,
        config=fake_config,
        module_config=fake_module,
        app="controller",
        step=step_lines.append,
    )
    assert calls[0] == 1
    assert any("FAILED" in line and "EABI" in line for line in step_lines), step_lines


def test_fire_iterate_reraises_keyboard_interrupt(fake_config, fake_module):
    """KeyboardInterrupt is exceptional and must propagate — tests the
    except-(KeyboardInterrupt, SystemExit): raise branch."""
    def _iterate(config, module_config, app, on_step=None):
        raise KeyboardInterrupt("user cancel")

    with pytest.raises(KeyboardInterrupt):
        watch._fire_iterate(_iterate, fake_config, fake_module, "controller",
                            step=lambda _m: None)


def test_fire_iterate_swallows_unexpected_exception(fake_config, fake_module):
    """A stray RuntimeError must not kill the loop either — iterate is
    user code (via transport) and can fail in shapes we haven't typed
    yet. The step line must still log it so the user sees it."""
    def _iterate(config, module_config, app, on_step=None):
        raise RuntimeError("ssh tunnel collapsed")

    lines: list[str] = []
    watch._fire_iterate(_iterate, fake_config, fake_module, "controller",
                        step=lines.append)
    assert any("FAILED" in line and "RuntimeError" in line for line in lines), lines


def test_fire_iterate_logs_success_line(fake_config, fake_module):
    def _iterate(config, module_config, app, on_step=None):
        return iterate.IterateResult(app=app, file_hash="beef0042", elapsed_s=0.05)

    lines: list[str] = []
    watch._fire_iterate(_iterate, fake_config, fake_module, "controller",
                        step=lines.append)
    assert any("ok" in line and "beef0042" in line for line in lines), lines
