"""Tests for shared hash utility."""

import hashlib
import os
import tempfile

from satdeploy.hash import compute_file_hash


def test_compute_file_hash_known_content():
    """Test hash matches expected SHA256 first 8 chars."""
    content = b"hello world"
    expected = hashlib.sha256(content).hexdigest()[:8]

    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(content)
        f.flush()
        result = compute_file_hash(f.name)

    os.unlink(f.name)
    assert result == expected


def test_compute_file_hash_empty_file():
    """Test hash of an empty file."""
    expected = hashlib.sha256(b"").hexdigest()[:8]

    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.flush()
        result = compute_file_hash(f.name)

    os.unlink(f.name)
    assert result == expected


def test_compute_file_hash_large_file():
    """Test hash of a file larger than the 8192-byte chunk size."""
    content = b"x" * 20000
    expected = hashlib.sha256(content).hexdigest()[:8]

    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(content)
        f.flush()
        result = compute_file_hash(f.name)

    os.unlink(f.name)
    assert result == expected


def test_compute_file_hash_returns_8_chars():
    """Test hash always returns exactly 8 characters."""
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(b"test data")
        f.flush()
        result = compute_file_hash(f.name)

    os.unlink(f.name)
    assert len(result) == 8
    assert all(c in "0123456789abcdef" for c in result)


def test_compute_file_hash_binary_content():
    """Test hash with binary content (non-UTF8)."""
    content = bytes(range(256))
    expected = hashlib.sha256(content).hexdigest()[:8]

    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(content)
        f.flush()
        result = compute_file_hash(f.name)

    os.unlink(f.name)
    assert result == expected
