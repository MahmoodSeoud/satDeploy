"""R1 fleet preview: tests for `--target` plumbing across CLI commands.

Uses the local transport so two "satellites" can coexist as sibling directories
without touching the network. Each command that accepts `--target` should deploy
to the named target's files *and* record the target name in history.db.
"""

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from satdeploy.cli import main
from satdeploy.history import History


@pytest.fixture
def fleet(tmp_path):
    """Two-target local-transport config + one real binary on disk."""
    som1 = tmp_path / "som1"
    som2 = tmp_path / "som2"
    som1.mkdir()
    som2.mkdir()
    (som1 / "bin").mkdir()
    (som2 / "bin").mkdir()

    bin_dir = tmp_path / "src" / "bin"
    bin_dir.mkdir(parents=True)
    binary = bin_dir / "test_app"
    binary.write_text("#!/bin/sh\necho v1\n")
    binary.chmod(0o755)

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({
        "default_target": "som1",
        "targets": {
            "som1": {
                "transport": "local",
                "target_dir": str(som1),
            },
            "som2": {
                "transport": "local",
                "target_dir": str(som2),
            },
        },
        "apps": {
            "test_app": {
                "local": str(binary),
                "remote": "/bin/test_app",
                "service": None,
            },
        },
    }))
    return {
        "config_path": config_path,
        "som1": som1,
        "som2": som2,
        "binary": binary,
    }


def _invoke(args):
    return CliRunner().invoke(main, args, catch_exceptions=False)


class TestTargetOption:
    """--target NAME routes commands to the correct configured target."""

    def test_push_defaults_to_first_target(self, fleet):
        result = _invoke([
            "push", "test_app",
            "--config", str(fleet["config_path"]),
        ])
        assert result.exit_code == 0, result.output
        assert (fleet["som1"] / "bin" / "test_app").exists()
        assert not (fleet["som2"] / "bin" / "test_app").exists()

    def test_push_target_routes_to_named_target(self, fleet):
        result = _invoke([
            "push", "test_app",
            "--target", "som2",
            "--config", str(fleet["config_path"]),
        ])
        assert result.exit_code == 0, result.output
        assert (fleet["som2"] / "bin" / "test_app").exists()
        assert not (fleet["som1"] / "bin" / "test_app").exists()

    def test_push_unknown_target_raises_listing_available(self, fleet):
        result = _invoke([
            "push", "test_app",
            "--target", "bogus",
            "--config", str(fleet["config_path"]),
        ])
        assert result.exit_code != 0
        assert "bogus" in result.output
        assert "som1" in result.output and "som2" in result.output

    def test_history_records_target_name(self, fleet):
        _invoke([
            "push", "test_app",
            "--target", "som2",
            "--config", str(fleet["config_path"]),
        ])
        _invoke([
            "push", "test_app",
            "--config", str(fleet["config_path"]),
        ])

        history = History(fleet["config_path"].parent / "history.db")
        history.init_db()
        assert set(history.get_module_state("som1").keys()) == {"test_app"}
        assert set(history.get_module_state("som2").keys()) == {"test_app"}

    def test_status_target_shows_only_that_target(self, fleet):
        _invoke([
            "push", "test_app",
            "--target", "som2",
            "--config", str(fleet["config_path"]),
        ])

        som1_status = _invoke([
            "status",
            "--target", "som1",
            "--config", str(fleet["config_path"]),
        ])
        som2_status = _invoke([
            "status",
            "--target", "som2",
            "--config", str(fleet["config_path"]),
        ])

        assert som1_status.exit_code == 0
        assert som2_status.exit_code == 0
        # som1 never got pushed → "not deployed" state
        assert "not deployed" in som1_status.output
        # som2 got it → any non-"not deployed" row for test_app
        assert "test_app" in som2_status.output
        assert "deployed" in som2_status.output

    def test_env_var_sets_target(self, fleet, monkeypatch):
        """SATDEPLOY_TARGET env var picks the target when --target omitted."""
        monkeypatch.setenv("SATDEPLOY_TARGET", "som2")
        result = _invoke([
            "push", "test_app",
            "--config", str(fleet["config_path"]),
        ])
        assert result.exit_code == 0
        assert (fleet["som2"] / "bin" / "test_app").exists()

    def test_rollback_uses_target(self, fleet):
        _invoke([
            "push", "test_app",
            "--target", "som2",
            "--config", str(fleet["config_path"]),
        ])
        # Second push of a modified binary to create a backup to roll back to.
        fleet["binary"].write_text("#!/bin/sh\necho v2\n")
        _invoke([
            "push", "test_app",
            "--target", "som2",
            "--config", str(fleet["config_path"]),
        ])

        result = _invoke([
            "rollback", "test_app",
            "--target", "som2",
            "--config", str(fleet["config_path"]),
        ])
        assert result.exit_code == 0, result.output
        assert "Rolled back" in result.output
