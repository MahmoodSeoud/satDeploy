"""Shared hash computation for satdeploy."""

import hashlib


def compute_file_hash(path: str) -> str:
    """Compute SHA256 hash of a file.

    Args:
        path: Path to the file.

    Returns:
        First 8 characters of the hex digest.
    """
    sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()[:8]
