"""
test_fingerprint.py
Tests core/fingerprint.py in isolation - the content-hash layer that
defeats the size+mtime-only staleness bypass an adversarial review found
(same size, restored timestamp, swapped content evades a size+mtime-only
check). See test_hardening.py for the integration-level version of that
exact reproduction against apply()/undo()/quarantine().
"""

import os

import pytest

from metamatch.fingerprint import content_fingerprint, SAMPLE_SIZE, SAMPLE_THRESHOLD


class TestContentFingerprint:
    def test_same_content_same_hash(self, tmp_path):
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(b"hello world")
        b.write_bytes(b"hello world")
        assert content_fingerprint(str(a)) == content_fingerprint(str(b))

    def test_different_content_different_hash(self, tmp_path):
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(b"hello world")
        b.write_bytes(b"goodbye world")
        assert content_fingerprint(str(a)) != content_fingerprint(str(b))

    def test_same_size_different_content_different_hash(self, tmp_path):
        """The exact case a size-only or size+mtime-only check misses."""
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(b"AAAA")
        b.write_bytes(b"BBBB")
        assert os.path.getsize(a) == os.path.getsize(b)
        assert content_fingerprint(str(a)) != content_fingerprint(str(b))

    def test_missing_file_returns_none(self, tmp_path):
        assert content_fingerprint(str(tmp_path / "does_not_exist.bin")) is None

    def test_empty_file_does_not_raise(self, tmp_path):
        p = tmp_path / "empty.bin"
        p.write_bytes(b"")
        assert content_fingerprint(str(p)) is not None

    def test_large_file_uses_sampling_not_full_read(self, tmp_path):
        """Above SAMPLE_THRESHOLD, only sampled regions matter - a change
        in the untouched middle-of-file bulk (outside the three sampled
        windows) should NOT be detected, since that's the deliberate
        bounded-cost tradeoff. This test pins down that behavior exists,
        not that it's a gap: see the module docstring for why."""
        size = SAMPLE_THRESHOLD + SAMPLE_SIZE * 10
        p = tmp_path / "large.bin"
        p.write_bytes(bytes(bytearray(size)))
        original_hash = content_fingerprint(str(p))

        # Modify a byte well outside all three sampled windows (first,
        # middle, last SAMPLE_SIZE) - should NOT change the fingerprint.
        untouched_offset = SAMPLE_SIZE * 3  # past the "first" window, nowhere near middle/last
        with open(p, "r+b") as f:
            f.seek(untouched_offset)
            f.write(b"\xff")
        assert content_fingerprint(str(p)) == original_hash

    def test_large_file_change_in_first_sample_detected(self, tmp_path):
        size = SAMPLE_THRESHOLD + SAMPLE_SIZE * 10
        p = tmp_path / "large2.bin"
        p.write_bytes(bytes(bytearray(size)))
        original_hash = content_fingerprint(str(p))

        with open(p, "r+b") as f:
            f.seek(0)
            f.write(b"\xff")
        assert content_fingerprint(str(p)) != original_hash

    def test_large_file_change_in_last_sample_detected(self, tmp_path):
        size = SAMPLE_THRESHOLD + SAMPLE_SIZE * 10
        p = tmp_path / "large3.bin"
        p.write_bytes(bytes(bytearray(size)))
        original_hash = content_fingerprint(str(p))

        with open(p, "r+b") as f:
            f.seek(size - 1)
            f.write(b"\xff")
        assert content_fingerprint(str(p)) != original_hash

    def test_different_size_files_dont_collide_even_with_similar_samples(self, tmp_path):
        """Size is mixed into the hash for large files specifically so two
        differently-sized files that happen to share sampled byte ranges
        (e.g. both padded with zeros) don't produce the same fingerprint."""
        p1 = tmp_path / "sized1.bin"
        p2 = tmp_path / "sized2.bin"
        p1.write_bytes(bytes(bytearray(SAMPLE_THRESHOLD + SAMPLE_SIZE)))
        p2.write_bytes(bytes(bytearray(SAMPLE_THRESHOLD + SAMPLE_SIZE * 2)))
        assert content_fingerprint(str(p1)) != content_fingerprint(str(p2))

    def test_bounded_io_cost_for_huge_file(self, tmp_path):
        """Confirms the sampling path is actually taken (not a full read)
        for a file above the threshold, by counting bytes actually read."""
        size = SAMPLE_THRESHOLD + SAMPLE_SIZE * 100
        p = tmp_path / "huge.bin"
        with open(p, "wb") as f:
            f.seek(size - 1)
            f.write(b"\x00")  # sparse-write a huge file without writing all bytes for real

        read_total = {"n": 0}
        real_open = open

        class _CountingFile:
            def __init__(self, f):
                self._f = f

            def read(self, n=-1):
                data = self._f.read(n)
                read_total["n"] += len(data)
                return data

            def seek(self, *a, **k):
                return self._f.seek(*a, **k)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self._f.close()
                return False

        import metamatch.fingerprint as fp_module
        original_builtin_open = fp_module.open if hasattr(fp_module, "open") else None
        import builtins
        counting_open = lambda path, mode: _CountingFile(real_open(path, mode))
        # Patch the name `open` as resolved inside fingerprint.py's module namespace.
        fp_module.__dict__["open"] = counting_open
        try:
            fp_module.content_fingerprint(str(p))
        finally:
            if original_builtin_open is not None:
                fp_module.__dict__["open"] = original_builtin_open
            else:
                del fp_module.__dict__["open"]

        assert read_total["n"] <= SAMPLE_SIZE * 3 + 100  # small slack, no full-file read
