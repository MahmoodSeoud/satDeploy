"""Git provenance tracking for deployed binaries."""

import os
import subprocess
from typing import Optional


def capture_provenance(binary_path: str) -> Optional[str]:
    """Capture git provenance for the directory containing a binary.

    Runs git commands in the directory of binary_path to capture:
    - commit hash (short, 8 chars)
    - branch name (or None if detached HEAD)
    - dirty flag (uncommitted changes)

    Returns:
        Provenance string like "main@3c940acf", "main@3c940acf-dirty",
        "@3c940acf" (detached HEAD), or None (not in git repo).
    """
    try:
        work_dir = os.path.dirname(os.path.abspath(binary_path))

        # Get short commit hash
        result = subprocess.run(
            ["git", "-C", work_dir, "rev-parse", "--short=8", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        commit_hash = result.stdout.strip()

        # Get branch name ("HEAD" if detached)
        result = subprocess.run(
            ["git", "-C", work_dir, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        branch = result.stdout.strip()

        # Check for dirty tree (exit code 1 = dirty)
        result = subprocess.run(
            ["git", "-C", work_dir, "diff-index", "--quiet", "HEAD", "--"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        dirty = result.returncode != 0

        # Build provenance string
        if branch == "HEAD":
            # Detached HEAD
            provenance = f"@{commit_hash}"
        else:
            provenance = f"{branch}@{commit_hash}"

        if dirty:
            provenance += "-dirty"

        return provenance

    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None


def is_dirty(provenance: str | None) -> bool:
    """Check if a provenance string indicates a dirty working tree.

    Uses endswith() rather than 'in' to avoid false positives from
    branch names containing 'dirty' (e.g., 'fix-dirty-flag@abc12345').
    """
    return provenance is not None and provenance.endswith("-dirty")
