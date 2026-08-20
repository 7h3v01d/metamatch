"""
journal.py
Persistent write-ahead transaction log for MusicLibrary/MovieLibrary
mutations (apply/undo), backed by SQLite.

Every apply() writes a transaction row *before* touching any file
("pending"), then updates it to "committed" (success) or "failed"
(the mutation errored) once it's done. That buys two real things the
old purely-in-memory undo_by_path dict couldn't:

  - Undo history survives a restart. Close the app, reopen it, rescan
    the same folder, and files that were previously applied still know
    how to be undone - "can_undo" and undo() both consult the journal,
    not a dict that reset to empty on process start.
  - A transaction stuck in "pending" after a restart is a genuine
    crash-recovery signal: the process died between begin() and
    commit()/fail(). recover() finds these on startup and marks them
    "interrupted" so the fact that something was attempted isn't
    silently lost - the caller can surface "N operations may have been
    interrupted last time MetaMatch was running" instead of the file
    just quietly forgetting.

What this does NOT do: make individual file mutations atomic. A crash
between writing a tag and renaming a file can still leave that one file
half-updated (tag written, rename not yet done, or vice versa) - true
atomicity there would mean staging every write to a temp file and only
swapping it into place at the very end, which tagger.py/movie_tagger.py
don't currently do for every operation. What the journal guarantees is
that MetaMatch itself never loses track of *what it was trying to do*
to *which file*, even across a crash - which is what makes recovery and
persistent undo possible at all.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, replace
from typing import Optional

DEFAULT_JOURNAL_PATH = os.path.join(os.path.expanduser("~"), ".metamatch", "journal.sqlite")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    committed_at REAL,
    original_path TEXT NOT NULL,
    current_path TEXT NOT NULL,
    before_state TEXT NOT NULL,
    after_state TEXT,
    operation TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_txn_current_path ON transactions(kind, current_path, status);
CREATE INDEX IF NOT EXISTS idx_txn_status ON transactions(kind, status);
"""

PENDING = "pending"
COMMITTED = "committed"
FAILED = "failed"
ROLLED_BACK = "rolled_back"
SUPERSEDED = "superseded"     # replaced by a later transaction on the same lineage before it was undone
INTERRUPTED = "interrupted"   # was "pending" when the process restarted - a crash-recovery finding


@dataclass
class Transaction:
    id: int
    kind: str
    status: str
    created_at: float
    committed_at: Optional[float]
    original_path: str
    current_path: str
    before_state: dict
    after_state: dict
    operation: dict
    error: Optional[str]

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "status": self.status,
            "created_at": self.created_at, "committed_at": self.committed_at,
            "original_path": self.original_path, "current_path": self.current_path,
            "operation": self.operation, "error": self.error,
        }


def _is_within(path: str, folder: str) -> bool:
    """True filesystem containment - not a string prefix check. A plain
    startswith(folder) would treat /tmp/music_backup as "within"
    /tmp/music (they share a string prefix but are unrelated sibling
    directories), which would let list_undoable()/undo_all() reach into a
    completely different folder that merely has a similar name."""
    try:
        path_real = os.path.realpath(os.path.abspath(path))
        folder_real = os.path.realpath(os.path.abspath(folder))
        return os.path.commonpath([path_real, folder_real]) == folder_real
    except ValueError:
        # os.path.commonpath raises this for paths on different Windows
        # drives (e.g. "C:\\..." vs "D:\\...") - definitionally not contained.
        return False


class Journal:
    """One journal can serve both MusicLibrary and MovieLibrary - rows are
    namespaced by `kind` ('music'/'movie') rather than needing two files."""

    def __init__(self, path: str = DEFAULT_JOURNAL_PATH):
        self.path = path
        self._lock = threading.RLock()
        dirname = os.path.dirname(path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(_SCHEMA)
            # Lightweight migration for a journal file created by an older
            # version of MetaMatch that predates after_state - CREATE TABLE
            # IF NOT EXISTS above won't add a column to an existing table.
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(transactions)")}
            if "after_state" not in cols:
                conn.execute("ALTER TABLE transactions ADD COLUMN after_state TEXT")

    def begin(self, kind: str, original_path: str, current_path: str, before_state: dict, operation: dict) -> int:
        """Records intent BEFORE any file is touched. Returns the transaction id."""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO transactions "
                "(kind, status, created_at, original_path, current_path, before_state, operation) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (kind, PENDING, time.time(), original_path, current_path,
                 json.dumps(before_state), json.dumps(operation)),
            )
            return cur.lastrowid

    def commit(self, txn_id: int, new_current_path: str, after_state: Optional[dict] = None) -> None:
        """Marks a transaction successful. after_state records facts only
        knowable once the mutation actually ran - e.g. the exact sidecar
        paths a movie apply ended up creating, which can differ from a
        naively-reconstructed path if a rename collided with an unrelated
        file (see MovieLibrary._apply_one/_undo_txn: undo must operate on
        the exact path recorded here, never guess one from current_path)."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE transactions SET status=?, current_path=?, committed_at=?, after_state=? WHERE id=?",
                (COMMITTED, new_current_path, time.time(),
                 json.dumps(after_state) if after_state is not None else None, txn_id),
            )

    def fail(self, txn_id: int, error: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE transactions SET status=?, error=? WHERE id=?", (FAILED, error, txn_id))

    def mark_rolled_back(self, txn_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE transactions SET status=? WHERE id=?", (ROLLED_BACK, txn_id))

    def mark_superseded(self, txn_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE transactions SET status=? WHERE id=?", (SUPERSEDED, txn_id))

    def _row_to_txn(self, row: sqlite3.Row) -> Transaction:
        return Transaction(
            id=row["id"], kind=row["kind"], status=row["status"],
            created_at=row["created_at"], committed_at=row["committed_at"],
            original_path=row["original_path"], current_path=row["current_path"],
            before_state=json.loads(row["before_state"]),
            after_state=json.loads(row["after_state"]) if row["after_state"] else {},
            operation=json.loads(row["operation"]) if row["operation"] else {},
            error=row["error"],
        )

    def get_active_for_path(self, kind: str, path: str) -> Optional[Transaction]:
        """The transaction that currently governs undo for `path`, if any -
        the most recent COMMITTED row whose current_path matches. A file
        can only be undone if this returns something."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM transactions WHERE kind=? AND current_path=? AND status=? "
                "ORDER BY id DESC LIMIT 1",
                (kind, path, COMMITTED),
            ).fetchone()
            return self._row_to_txn(row) if row else None

    def get_undoable_paths(self, kind: str, folder: Optional[str] = None) -> set[str]:
        """Cheap membership-check version of list_undoable, for building
        'can_undo' flags over a whole library without one query per file."""
        return {t.current_path for t in self.list_undoable(kind, folder)}

    def list_undoable(self, kind: str, folder: Optional[str] = None) -> list[Transaction]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE kind=? AND status=? ORDER BY id DESC",
                (kind, COMMITTED),
            ).fetchall()
        txns = [self._row_to_txn(r) for r in rows]
        if folder:
            txns = [t for t in txns if _is_within(t.current_path, folder)]
        return txns

    def find_incomplete(self, kind: str) -> list[Transaction]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE kind=? AND status=? ORDER BY id",
                (kind, PENDING),
            ).fetchall()
            return [self._row_to_txn(r) for r in rows]

    def recover(self, kind: str) -> list[Transaction]:
        """Finds transactions left 'pending' from a previous run (the
        process died between begin() and commit()/fail()) and marks them
        'interrupted'. Returns what was found, for the caller to surface
        to the user - typically called once, when a Library is constructed."""
        incomplete = self.find_incomplete(kind)
        recovered = []
        for txn in incomplete:
            with self._lock, self._connect() as conn:
                conn.execute("UPDATE transactions SET status=? WHERE id=?", (INTERRUPTED, txn.id))
            recovered.append(replace(txn, status=INTERRUPTED))
        return recovered
