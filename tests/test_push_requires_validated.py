"""Tests for `push --requires-validated` flight gate.

Covers DX-review Tier-1 #11 spec + design-doc thesis metric #3:

  * Without a PASS record, push refuses with the EGATE typed error.
  * With a PASS record, push proceeds (local transport — no network).
  * A PASS on som1 does NOT satisfy the gate on som2 (R1 fleet contract).
  * Per-target / top-level `push.require_validated: true` makes the gate
    default-on for that target without the explicit CLI flag.
  * The CLI flag wins over config; `--no-requires-validated` is not
    expressed (the gate is hard-block by design — see Open Question #0).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from satdeploy.cli import main
from satdeploy.errors import EGATE
from satdeploy.hash import compute_file_hash
from satdeploy.history import History, ValidationRecord, VALIDATION_PASS


def _two_target_config(tmp_path: Path, *, top_level_gate: bool = False,
                       per_target_gate: dict | None = None) -> dict:
    """Build a two-target local-transport config + a binary on disk."""
    som1 = tmp_path / "som1"
    som2 = tmp_path / "som2"
    som1.mkdir()
    som2.mkdir()
    bin_dir = tmp_path / "src"
    bin_dir.mkdir()
    binary = bin_dir / "controller"
    binary.write_bytes(b"\x7fELF stub binary")
    binary.chmod(0o755)

    targets: dict = {
        "som1": {"transport": "local", "target_dir": str(som1)},
        "som2": {"transport": "local", "target_dir": str(som2)},
    }
    if per_target_gate:
        for tname, val in per_target_gate.items():
            targets[tname]["push"] = {"require_validated": val}

    cfg: dict = {
        "default_target": "som1",
        "targets": targets,
        "apps": {
            "controller": {
                "local": str(binary),
                "remote": "/bin/controller",
                "validate_command": "true",
            },
        },
    }
    if top_level_gate:
        cfg["push"] = {"require_validated": True}

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump(cfg))
    return {
        "config_path": config_path,
        "binary": binary,
        "som1": som1,
        "som2": som2,
    }


def _invoke(args):
    return CliRunner().invoke(main, args, catch_exceptions=False)


def _record_pass(env, *, target: str):
    history = History(env["config_path"].parent / "history.db")
    history.init_db()
    file_hash = compute_file_hash(str(env["binary"]))
    history.record_validation(ValidationRecord(
        target=target,
        app="controller",
        file_hash=file_hash,
        status=VALIDATION_PASS,
        exit_code=0,
        duration_ms=10,
        command="true",
    ))


class TestRequiresValidatedFlag:
    def test_push_without_pass_record_raises_egate(self, tmp_path):
        env = _two_target_config(tmp_path)
        result = _invoke([
            "push", "controller", "--requires-validated",
            "--config", str(env["config_path"]),
        ])
        assert result.exit_code == EGATE, result.output
        assert "PASS record" in result.output
        # Stable wording: errors.py:303 matcher keys on this string.
        assert "no PASS record" in result.output
        # And the fix_cmd points at the right command + target.
        assert "satdeploy validate controller" in result.output

    def test_push_with_pass_record_succeeds(self, tmp_path):
        env = _two_target_config(tmp_path)
        _record_pass(env, target="som1")  # default target
        result = _invoke([
            "push", "controller", "--requires-validated",
            "--config", str(env["config_path"]),
        ])
        assert result.exit_code == 0, result.output
        assert (env["som1"] / "bin" / "controller").exists()

    def test_pass_on_som1_does_not_satisfy_som2(self, tmp_path):
        """R1 fleet contract — gate must key by target_name."""
        env = _two_target_config(tmp_path)
        _record_pass(env, target="som1")
        result = _invoke([
            "push", "controller", "--requires-validated",
            "--target", "som2",
            "--config", str(env["config_path"]),
        ])
        assert result.exit_code == EGATE, result.output
        assert "som2" in result.output

    def test_pass_on_som2_satisfies_som2_gate(self, tmp_path):
        env = _two_target_config(tmp_path)
        _record_pass(env, target="som2")
        result = _invoke([
            "push", "controller", "--requires-validated",
            "--target", "som2",
            "--config", str(env["config_path"]),
        ])
        assert result.exit_code == 0, result.output
        assert (env["som2"] / "bin" / "controller").exists()

    def test_push_without_flag_skips_gate(self, tmp_path):
        """When neither flag nor config sets the gate, push proceeds."""
        env = _two_target_config(tmp_path)
        result = _invoke([
            "push", "controller",
            "--config", str(env["config_path"]),
        ])
        assert result.exit_code == 0, result.output


class TestRequiresValidatedConfig:
    def test_top_level_config_makes_gate_default_on(self, tmp_path):
        """`push.require_validated: true` at top level applies to all targets."""
        env = _two_target_config(tmp_path, top_level_gate=True)
        # No --requires-validated flag — gate should still fire.
        result = _invoke([
            "push", "controller",
            "--config", str(env["config_path"]),
        ])
        assert result.exit_code == EGATE, result.output

    def test_per_target_config_isolates_gate(self, tmp_path):
        """`targets.som2.push.require_validated: true` only gates som2."""
        env = _two_target_config(
            tmp_path,
            per_target_gate={"som2": True},
        )
        # som1 (default target) — gate is OFF → push succeeds without PASS.
        result_som1 = _invoke([
            "push", "controller",
            "--config", str(env["config_path"]),
        ])
        assert result_som1.exit_code == 0, result_som1.output

        # som2 — gate is ON → push fails without PASS.
        result_som2 = _invoke([
            "push", "controller", "--target", "som2",
            "--config", str(env["config_path"]),
        ])
        assert result_som2.exit_code == EGATE, result_som2.output

    def test_explicit_no_requires_validated_overrides_config(self, tmp_path):
        """`--no-requires-validated` lets the user override default-on config.

        The design-doc Open Question #0 paternalism risk is mitigated by
        making the override explicit at the CLI rather than silent.
        """
        env = _two_target_config(tmp_path, top_level_gate=True)
        result = _invoke([
            "push", "controller", "--no-requires-validated",
            "--config", str(env["config_path"]),
        ])
        assert result.exit_code == 0, result.output


class TestErrorRouting:
    def test_egate_pattern_matches_emitted_message(self, tmp_path):
        """The errors.py:303 regex must match what the gate emits.

        Guards against future drift where someone tweaks the GateError
        message and accidentally breaks the typed-error stderr matcher
        downstream tools rely on (eg. iterate-then-push wrappers grep
        stderr for EGATE so they can offer a one-key validate retry).
        """
        from satdeploy import errors
        env = _two_target_config(tmp_path)
        result = _invoke([
            "push", "controller", "--requires-validated",
            "--config", str(env["config_path"]),
        ])
        assert result.exit_code == EGATE
        match = errors.match(result.output, app="controller")
        assert match is not None, (
            f"errors.match() returned None for our own gate output:\n{result.output}"
        )
        assert isinstance(match, errors.GateError)
