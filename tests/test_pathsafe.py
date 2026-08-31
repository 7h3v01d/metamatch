"""
test_pathsafe.py
Unit tests for the filesystem-authority primitives, with special attention to
cross-platform behaviour. In particular: a path that DOESN'T EXIST must never
be treated as a link/reparse point, because that's exactly the state of every
sidecar (.nfo / poster / thumbnail) MetaMatch is about to create. These tests
simulate the Windows reparse-point branch on any platform so a Windows-only
regression can't hide behind a Linux CI run.
"""

from __future__ import annotations

import os

import pytest

from metamatch import pathsafe


class TestIsLinkOrReparse:
    def test_missing_path_is_not_a_link(self, tmp_path):
        assert pathsafe.is_link_or_reparse(str(tmp_path / "nope.nfo")) is False

    def test_real_file_is_not_a_link(self, tmp_path):
        f = tmp_path / "real.nfo"; f.write_text("x")
        assert pathsafe.is_link_or_reparse(str(f)) is False

    def test_windows_branch_on_missing_path_returns_false(self, tmp_path, monkeypatch):
        """Simulate Windows (os.name == 'nt'). Real os.lstat already raises
        FileNotFoundError for a missing path, exactly as on Windows. Before the
        fix, the blanket `except OSError: return True` treated that as "unsafe",
        so MetaMatch refused to create any new sidecar on Windows. With the
        lexists() guard it must return False for a missing path."""
        monkeypatch.setattr(pathsafe.os, "name", "nt")
        missing = str(tmp_path / "about_to_create.nfo")
        assert pathsafe.is_link_or_reparse(missing) is False
        assert pathsafe.sidecar_write_is_unsafe(missing) is False

    def test_windows_branch_existing_normal_file_is_not_a_link(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pathsafe.os, "name", "nt")
        f = tmp_path / "real.nfo"; f.write_text("x")
        # a normal file's lstat has no reparse bit set (st_file_attributes may
        # be absent on non-Windows lstat results -> getattr default 0)
        assert pathsafe.is_link_or_reparse(str(f)) is False


class TestSidecarWriteGuard:
    def test_missing_sidecar_is_safe_to_write(self, tmp_path):
        # The core of the Windows bug: a sidecar we're about to create must be
        # writable, not refused as if it were a link.
        assert pathsafe.sidecar_write_is_unsafe(str(tmp_path / "new-poster.jpg")) is False


class TestValidateMutationTargetMissing:
    def test_allow_missing_validates_parent_not_the_missing_file(self, tmp_path):
        ok, reason = pathsafe.validate_mutation_target(
            str(tmp_path / "new.nfo"), str(tmp_path), allow_missing=True)
        assert ok is True and reason is None

    def test_missing_without_allow_missing_is_refused(self, tmp_path):
        ok, reason = pathsafe.validate_mutation_target(
            str(tmp_path / "gone.mp3"), str(tmp_path))
        assert ok is False
