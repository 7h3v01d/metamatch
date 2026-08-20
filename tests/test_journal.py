"""
test_journal.py
Tests the Journal class directly - the persistent write-ahead log
backing MusicLibrary/MovieLibrary's apply/undo mechanics. These tests
don't touch MusicLibrary/MovieLibrary at all, just Journal's own
begin/commit/fail/recover contract.
"""

import os

import pytest

from metamatch.journal import Journal, COMMITTED, INTERRUPTED


@pytest.fixture
def journal(tmp_path):
    return Journal(str(tmp_path / "test_journal.sqlite"))


class TestBeginCommitFail:
    def test_begin_creates_pending_row(self, journal):
        txn_id = journal.begin("music", "/a.mp3", "/a.mp3", {"artist": "X"}, {"do_tag": True})
        assert txn_id is not None
        # not yet committed, so not active for anyone
        assert journal.get_active_for_path("music", "/a.mp3") is None

    def test_commit_makes_it_active(self, journal):
        txn_id = journal.begin("music", "/a.mp3", "/a.mp3", {"artist": "X"}, {})
        journal.commit(txn_id, "/renamed.mp3")

        active = journal.get_active_for_path("music", "/renamed.mp3")
        assert active is not None
        assert active.id == txn_id
        assert active.status == COMMITTED
        assert active.original_path == "/a.mp3"

    def test_fail_leaves_no_active_transaction(self, journal):
        txn_id = journal.begin("music", "/a.mp3", "/a.mp3", {"artist": "X"}, {})
        journal.fail(txn_id, "disk full")
        assert journal.get_active_for_path("music", "/a.mp3") is None

    def test_before_state_round_trips_through_json(self, journal):
        state = {"artist": "Radiohead", "title": None, "nested": {"a": [1, 2, 3]}}
        txn_id = journal.begin("music", "/a.mp3", "/a.mp3", state, {})
        journal.commit(txn_id, "/a.mp3")
        active = journal.get_active_for_path("music", "/a.mp3")
        assert active.before_state == state


class TestRollback:
    def test_rolled_back_transaction_no_longer_active(self, journal):
        txn_id = journal.begin("music", "/a.mp3", "/a.mp3", {}, {})
        journal.commit(txn_id, "/a.mp3")
        assert journal.get_active_for_path("music", "/a.mp3") is not None

        journal.mark_rolled_back(txn_id)
        assert journal.get_active_for_path("music", "/a.mp3") is None


class TestSupersession:
    def test_superseded_transaction_excluded_from_active_lookup(self, journal):
        txn1 = journal.begin("music", "/a.mp3", "/a.mp3", {"artist": "Original"}, {})
        journal.commit(txn1, "/a.mp3")

        # a second apply chains from txn1 and then supersedes it
        txn2 = journal.begin("music", "/a.mp3", "/a.mp3", {"artist": "Original"}, {})
        journal.commit(txn2, "/renamed.mp3")
        journal.mark_superseded(txn1)

        assert journal.get_active_for_path("music", "/a.mp3") is None
        active = journal.get_active_for_path("music", "/renamed.mp3")
        assert active.id == txn2

    def test_superseded_excluded_from_undoable_list(self, journal):
        txn1 = journal.begin("music", "/a.mp3", "/a.mp3", {}, {})
        journal.commit(txn1, "/a.mp3")
        journal.mark_superseded(txn1)

        assert journal.list_undoable("music") == []


class TestKindNamespacing:
    def test_music_and_movie_kinds_are_independent(self, journal):
        m_txn = journal.begin("music", "/song.mp3", "/song.mp3", {}, {})
        journal.commit(m_txn, "/song.mp3")
        v_txn = journal.begin("movie", "/song.mp3", "/song.mp3", {}, {})  # same path, different kind
        journal.commit(v_txn, "/song.mp3")

        assert journal.get_active_for_path("music", "/song.mp3").id == m_txn
        assert journal.get_active_for_path("movie", "/song.mp3").id == v_txn
        assert len(journal.list_undoable("music")) == 1
        assert len(journal.list_undoable("movie")) == 1


class TestListUndoable:
    def test_folder_filter(self, journal):
        t1 = journal.begin("music", "/music/a.mp3", "/music/a.mp3", {}, {})
        journal.commit(t1, "/music/a.mp3")
        t2 = journal.begin("music", "/other/b.mp3", "/other/b.mp3", {}, {})
        journal.commit(t2, "/other/b.mp3")

        results = journal.list_undoable("music", folder="/music")
        assert len(results) == 1
        assert results[0].id == t1

    def test_folder_filter_does_not_match_prefix_sibling(self, journal):
        """A plain string startswith(folder) would wrongly treat
        /music_backup as "within" /music, since they share a string
        prefix while being unrelated sibling directories - this is the
        containment bug an adversarial review caught: undo_all() scoped
        to one library could reach into a same-prefixed sibling folder."""
        t1 = journal.begin("music", "/music/a.mp3", "/music/a.mp3", {}, {})
        journal.commit(t1, "/music/a.mp3")
        t2 = journal.begin("music", "/music_backup/b.mp3", "/music_backup/b.mp3", {}, {})
        journal.commit(t2, "/music_backup/b.mp3")

        results = journal.list_undoable("music", folder="/music")
        assert len(results) == 1
        assert results[0].id == t1
        assert not any("music_backup" in t.current_path for t in results)

    def test_folder_filter_still_matches_real_subdirectories(self, journal):
        """Sanity check the fix didn't overcorrect: genuine nested
        contents of the scoped folder must still be included."""
        t1 = journal.begin("music", "/music/album/track.mp3", "/music/album/track.mp3", {}, {})
        journal.commit(t1, "/music/album/track.mp3")

        results = journal.list_undoable("music", folder="/music")
        assert len(results) == 1
        assert results[0].id == t1

    def test_get_undoable_paths_matches_list(self, journal):
        t1 = journal.begin("music", "/a.mp3", "/a.mp3", {}, {})
        journal.commit(t1, "/renamed_a.mp3")
        paths = journal.get_undoable_paths("music")
        assert paths == {"/renamed_a.mp3"}


class TestIsWithinContainment:
    """Direct tests of the path-containment helper itself, independent of
    the journal - see TestListUndoable for the integration-level version."""

    def test_rejects_prefix_sibling(self):
        from metamatch.journal import _is_within
        assert _is_within("/tmp/music_backup/song.mp3", "/tmp/music") is False

    def test_accepts_real_subdirectory(self):
        from metamatch.journal import _is_within
        assert _is_within("/tmp/music/album/song.mp3", "/tmp/music") is True

    def test_accepts_direct_child(self):
        from metamatch.journal import _is_within
        assert _is_within("/tmp/music/song.mp3", "/tmp/music") is True

    def test_rejects_unrelated_path(self):
        from metamatch.journal import _is_within
        assert _is_within("/var/log/song.mp3", "/tmp/music") is False

    def test_handles_relative_and_dotted_paths(self):
        from metamatch.journal import _is_within
        assert _is_within("/tmp/music/../music_backup/song.mp3", "/tmp/music") is False
        assert _is_within("/tmp/music/./album/song.mp3", "/tmp/music") is True

    def test_different_windows_drives_never_match(self, monkeypatch):
        """os.path.commonpath raises ValueError for paths on different
        drives (e.g. C:\\ vs D:\\) - must be treated as "not contained",
        not propagate the exception."""
        import os
        from metamatch.journal import _is_within

        def fake_commonpath(paths):
            raise ValueError("Paths don't have the same drive")

        monkeypatch.setattr(os.path, "commonpath", fake_commonpath)
        assert _is_within("D:\\Music\\song.mp3", "C:\\Music") is False


class TestCrashRecovery:
    def test_pending_transaction_found_on_recover(self, journal):
        txn_id = journal.begin("music", "/a.mp3", "/a.mp3", {"artist": "X"}, {"do_tag": True})
        # simulate a crash: process dies here, never calling commit()/fail()

        recovered = journal.recover("music")
        assert len(recovered) == 1
        assert recovered[0].id == txn_id

    def test_recovered_transaction_marked_interrupted(self, journal):
        txn_id = journal.begin("music", "/a.mp3", "/a.mp3", {}, {})
        journal.recover("music")

        # re-open a fresh Journal instance against the same file, simulating
        # a full process restart, and confirm the status actually persisted
        reopened = Journal(journal.path)
        row = reopened.find_incomplete("music")
        assert row == []  # no longer "pending" - already marked interrupted

        with reopened._connect() as conn:
            status = conn.execute("SELECT status FROM transactions WHERE id=?", (txn_id,)).fetchone()["status"]
        assert status == INTERRUPTED

    def test_committed_transactions_not_flagged_as_incomplete(self, journal):
        txn_id = journal.begin("music", "/a.mp3", "/a.mp3", {}, {})
        journal.commit(txn_id, "/a.mp3")
        assert journal.recover("music") == []

    def test_failed_transactions_not_flagged_as_incomplete(self, journal):
        txn_id = journal.begin("music", "/a.mp3", "/a.mp3", {}, {})
        journal.fail(txn_id, "boom")
        assert journal.recover("music") == []

    def test_recover_is_idempotent(self, journal):
        journal.begin("music", "/a.mp3", "/a.mp3", {}, {})
        first = journal.recover("music")
        second = journal.recover("music")
        assert len(first) == 1
        assert len(second) == 0  # already interrupted, not pending anymore

    def test_returned_transactions_reflect_new_status_not_stale_pending(self, journal):
        """recover() must return objects showing status='interrupted', not
        the status they had at query time before the update - a caller
        checking notice['status'] should never see 'pending' for something
        recover() just finished flagging."""
        journal.begin("music", "/a.mp3", "/a.mp3", {}, {})
        recovered = journal.recover("music")
        assert recovered[0].status == INTERRUPTED
        assert recovered[0].to_dict()["status"] == INTERRUPTED


class TestAfterStateMigration:
    """after_state is a newer column - a journal file created before it
    existed (an old CREATE TABLE IF NOT EXISTS won't add columns to an
    already-existing table) must still open and work correctly."""

    def test_opens_cleanly_against_a_pre_after_state_schema(self, tmp_path):
        import sqlite3
        path = str(tmp_path / "old_schema.sqlite")

        # Build a journal file using the OLD schema (no after_state column),
        # simulating what a user who ran an earlier MetaMatch version has
        # sitting in ~/.metamatch/journal.sqlite.
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                committed_at REAL,
                original_path TEXT NOT NULL,
                current_path TEXT NOT NULL,
                before_state TEXT NOT NULL,
                operation TEXT,
                error TEXT
            );
        """)
        conn.execute(
            "INSERT INTO transactions (kind, status, created_at, original_path, current_path, before_state) "
            "VALUES ('music', 'committed', 0, '/a.mp3', '/a.mp3', '{}')"
        )
        conn.commit()
        conn.close()

        journal = Journal(path)  # must not raise
        txn = journal.get_active_for_path("music", "/a.mp3")
        assert txn is not None
        assert txn.after_state == {}  # missing column reads back as empty, not an error

        # and new writes against the migrated file work normally
        new_id = journal.begin("music", "/b.mp3", "/b.mp3", {}, {})
        journal.commit(new_id, "/b.mp3", after_state={"nfo_path": "/b.nfo"})
        assert journal.get_active_for_path("music", "/b.mp3").after_state == {"nfo_path": "/b.nfo"}


class TestPersistenceAcrossInstances:
    """The whole point of a SQLite-backed journal: a second Journal object
    pointed at the same file sees everything the first one wrote - this is
    what makes undo survive an app restart."""

    def test_second_instance_sees_first_instances_writes(self, tmp_path):
        path = str(tmp_path / "shared.sqlite")
        journal_a = Journal(path)
        txn_id = journal_a.begin("music", "/a.mp3", "/a.mp3", {"artist": "Original"}, {})
        journal_a.commit(txn_id, "/renamed.mp3")

        journal_b = Journal(path)  # simulates the app restarting
        active = journal_b.get_active_for_path("music", "/renamed.mp3")
        assert active is not None
        assert active.before_state == {"artist": "Original"}

    def test_schema_creation_is_safe_to_repeat(self, tmp_path):
        path = str(tmp_path / "shared2.sqlite")
        Journal(path)
        Journal(path)  # must not raise on an already-initialized file
        assert os.path.exists(path)
