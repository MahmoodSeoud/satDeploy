"""Tests for git provenance tracking."""

import os
import subprocess
from unittest.mock import patch

import pytest

from satdeploy.provenance import capture_provenance


@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary git repository with a committed file."""
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )

    # Create and commit a binary file
    binary_path = tmp_path / "app.bin"
    binary_path.write_bytes(b"\x00" * 64)
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "app.bin"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )

    return tmp_path, str(binary_path)


class TestCaptureProvenance:
    """Tests for capture_provenance()."""

    def test_clean_tree(self, git_repo):
        """Clean git repo returns branch@hash with no -dirty suffix."""
        repo_dir, binary_path = git_repo

        result = capture_provenance(binary_path)

        assert result is not None
        # Should not have -dirty suffix
        assert "-dirty" not in result
        # Should have branch@hash format (default branch could be main or master)
        assert "@" in result
        parts = result.split("@")
        assert len(parts) == 2
        assert len(parts[1]) == 8  # 8-char short hash

    def test_dirty_tree(self, git_repo):
        """Dirty git repo returns branch@hash-dirty."""
        repo_dir, binary_path = git_repo

        # Make the tree dirty
        dirty_file = repo_dir / "uncommitted.txt"
        dirty_file.write_text("dirty")
        subprocess.run(
            ["git", "-C", str(repo_dir), "add", "uncommitted.txt"],
            check=True,
            capture_output=True,
        )

        result = capture_provenance(binary_path)

        assert result is not None
        assert result.endswith("-dirty")
        assert "@" in result

    def test_detached_head(self, git_repo):
        """Detached HEAD returns @hash without branch name."""
        repo_dir, binary_path = git_repo

        # Get the current commit hash and detach HEAD
        hash_result = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        commit_hash = hash_result.stdout.strip()
        subprocess.run(
            ["git", "-C", str(repo_dir), "checkout", commit_hash],
            check=True,
            capture_output=True,
        )

        result = capture_provenance(binary_path)

        assert result is not None
        # Should start with @ (no branch name)
        assert result.startswith("@")
        # Remove "@" prefix and optional "-dirty" suffix to get the hash
        hash_part = result[1:]  # strip leading "@"
        if hash_part.endswith("-dirty"):
            hash_part = hash_part[:-6]
        assert len(hash_part) == 8

    def test_not_a_git_repo(self, tmp_path):
        """Non-git directory returns None."""
        binary_path = tmp_path / "app.bin"
        binary_path.write_bytes(b"\x00" * 64)

        result = capture_provenance(str(binary_path))

        assert result is None

    def test_git_not_installed(self, tmp_path):
        """Returns None when git is not installed."""
        binary_path = tmp_path / "app.bin"
        binary_path.write_bytes(b"\x00" * 64)

        with patch("satdeploy.provenance.subprocess.run", side_effect=FileNotFoundError):
            result = capture_provenance(str(binary_path))

        assert result is None

    def test_subprocess_error(self, tmp_path):
        """Returns None on subprocess errors."""
        binary_path = tmp_path / "app.bin"
        binary_path.write_bytes(b"\x00" * 64)

        with patch(
            "satdeploy.provenance.subprocess.run",
            side_effect=subprocess.SubprocessError("git crashed"),
        ):
            result = capture_provenance(str(binary_path))

        assert result is None

    def test_branch_name_in_result(self, git_repo):
        """Branch name is included in the provenance string."""
        repo_dir, binary_path = git_repo

        # Create and switch to a named branch
        subprocess.run(
            ["git", "-C", str(repo_dir), "checkout", "-b", "feature/test"],
            check=True,
            capture_output=True,
        )

        result = capture_provenance(binary_path)

        assert result is not None
        assert result.startswith("feature/test@")
