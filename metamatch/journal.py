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
    error TEXT,
    rollback_info TEXT
);
CREATE INDEX IF NOT EXISTS idx_txn_current_path ON transactions(kind, current_path, status);
CREATE INDEX IF NOT EXISTS idx_txn_status ON transactions(kind, status);
"""

# Transaction lifecycle. A normal apply moves:
#     PENDING -> APPLYING -> COMMITTED
# PENDING means intent is recorded but NOTHING on disk has been touched
# yet (the reviewer's "PREPARED"). APPLYING means file mutations are in
# progress. If a mutation raises, the caller rolls back the partial work
# and the row ends as:
#     APPLYING -> ROLLING_BACK -> ROLLED_BACK          (rollback restored before-state)
# or, if a compensating action itself failed:
#     APPLYING -> ROLLING_BACK -> RECOVERY_REQUIRED    (needs a human)
# The PENDING vs APPLYING split is what lets crash recovery tell "died
# before writing anything" (safe -> INTERRUPTED) apart from "died with
# file mutations possibly half-done" (unsafe -> RECOVERY_REQUIRED).
PENDING = "pending"
APPLYING = "applying"         # file mutations in progress - a crash here may have left partial work
COMMITTED = "committed"
FAILED = "failed"             # legacy: a mutation errored but nothing was rolled back (pre-rollback builds)
ROLLING_BACK = "rolling_back" # transient: compensations are running (a crash here -> RECOVERY_REQUIRED on restart)
ROLLED_BACK = "rolled_back"
RECOVERY_REQUIRED = "recovery_required"  # apply failed AND rollback couldn't fully restore before-state
SUPERSEDED = "superseded"     # replaced by a later transaction on the same lineage before it was undone
INTERRUPTED = "interrupted"   # was "pending" when the process restarted - a crash-recovery finding
RESOLVED = "resolved"         # a RECOVERY_REQUIRED item the user has acknowledged/handled by hand - terminal, no longer surfaced


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
    rollback_info: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "status": self.status,
            "created_at": self.created_at, "committed_at": self.committed_at,
            "original_path": self.original_path, "current_path": self.current_path,
            "operation": self.operation, "error": self.error,
            "rollback_info": self.rollback_info,
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
            # rollback_info arrived with automatic rollback - same lightweight
            # migration so an older journal file opens and keeps working.
            if "rollback_info" not in cols:
                conn.execute("ALTER TABLE transactions ADD COLUMN rollback_info TEXT")

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

    def mark_applying(self, txn_id: int) -> None:
        """Marks the point where file mutations START. Anything found still
        in this state after a restart may have been left half-applied, so
        recover() escalates it to RECOVERY_REQUIRED rather than the benign
        INTERRUPTED it uses for rows that never got past PENDING."""
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE transactions SET status=? WHERE id=?", (APPLYING, txn_id))

    def fail(self, txn_id: int, error: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE transactions SET status=?, error=? WHERE id=?", (FAILED, error, txn_id))

    def mark_rolling_back(self, txn_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE transactions SET status=? WHERE id=?", (ROLLING_BACK, txn_id))

    def mark_rolled_back(self, txn_id: int, info: Optional[dict] = None) -> None:
        """Terminal success-of-rollback state: the apply failed, but the
        compensations restored the captured before-state. `info` optionally
        records what the rollback did (e.g. residual warnings about
        genuinely irreversible mutations like an ffmpeg remux)."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE transactions SET status=?, rollback_info=? WHERE id=?",
                (ROLLED_BACK, json.dumps(info) if info is not None else None, txn_id),
            )

    def mark_recovery_required(self, txn_id: int, info: dict) -> None:
        """Terminal failure-of-rollback state: the apply failed AND a
        compensating action that was expected to work didn't, so the file
        is in an inconsistent state that needs a human. `info` carries the
        original apply error plus which compensation(s) failed."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE transactions SET status=?, rollback_info=? WHERE id=?",
                (RECOVERY_REQUIRED, json.dumps(info), txn_id),
            )

    def mark_resolved(self, txn_id: int, note: Optional[str] = None) -> bool:
        """Acknowledge a RECOVERY_REQUIRED item as handled by the user: move
        it to the terminal RESOLVED state so it stops surfacing in the
        needs-attention list on every restart. Only a RECOVERY_REQUIRED row
        can be resolved (you can't 'resolve' a healthy or in-progress
        transaction); returns True if it was transitioned, False otherwise.
        The prior rollback_info is preserved for history, with the
        acknowledgement recorded alongside it."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT status, rollback_info FROM transactions WHERE id=?", (txn_id,)
            ).fetchone()
            if row is None or row["status"] != RECOVERY_REQUIRED:
                return False
            try:
                info = json.loads(row["rollback_info"]) if row["rollback_info"] else {}
                if not isinstance(info, dict):
                    info = {"previous": info}
            except (json.JSONDecodeError, TypeError):
                info = {}
            info["resolved"] = True
            if note:
                info["resolved_note"] = note
            conn.execute(
                "UPDATE transactions SET status=?, rollback_info=? WHERE id=?",
                (RESOLVED, json.dumps(info), txn_id),
            )
            return True

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
            rollback_info=(
                json.loads(row["rollback_info"])
                if "rollback_info" in row.keys() and row["rollback_info"]
                else None
            ),
        )

    def _safe_row_to_txn(self, row: sqlite3.Row) -> Optional[Transaction]:
        """Tolerant version of _row_to_txn: returns None instead of raising
        if a row's JSON columns are corrupt (a torn write, a truncated
        before_state, a disk-level bit flip). One poisoned row must never
        crash enumeration of the whole journal - that would take down undo
        listing and, worse, startup recovery (recover() runs in the Library
        constructor). Corrupt rows are found and escalated to
        RECOVERY_REQUIRED by recover(); everywhere else we simply skip them,
        which fail-closed also means a row we can't parse is never offered
        as undoable."""
        try:
            return self._row_to_txn(row)
        except (json.JSONDecodeError, TypeError, ValueError, KeyError, IndexError):
            return None

    def get(self, txn_id: int) -> Optional[Transaction]:
        """Fetch one transaction by id, whatever its status - unlike
        get_active_for_path, which only returns COMMITTED rows. Lets a
        caller inspect the terminal state (and rollback_info) of an apply
        that failed and rolled back. Returns None if the row is corrupt."""
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM transactions WHERE id=?", (txn_id,)).fetchone()
            return self._safe_row_to_txn(row) if row else None

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
            # Tolerant parse: a corrupt row at this path fails closed to
            # "nothing undoable here" rather than crashing the undo check.
            return self._safe_row_to_txn(row) if row else None

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
        txns = [t for t in (self._safe_row_to_txn(r) for r in rows) if t is not None]
        if folder:
            txns = [t for t in txns if _is_within(t.current_path, folder)]
        return txns

    def find_incomplete(self, kind: str) -> list[Transaction]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE kind=? AND status=? ORDER BY id",
                (kind, PENDING),
            ).fetchall()
            return [t for t in (self._safe_row_to_txn(r) for r in rows) if t is not None]

    def list_by_status(self, kind: str, status: str) -> list[Transaction]:
        """Every row of a given kind currently at `status`, newest first.
        Unlike recover()'s return value - which only reflects rows THIS
        startup transitioned - this reads live state, so an outstanding
        RECOVERY_REQUIRED item stays visible on every restart until it's
        resolved, instead of vanishing after the one boot that flagged it.
        Tolerant of corrupt rows (they're skipped, not fatal)."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE kind=? AND status=? ORDER BY id DESC",
                (kind, status),
            ).fetchall()
            return [t for t in (self._safe_row_to_txn(r) for r in rows) if t is not None]

    def find_in_progress(self, kind: str) -> list[Transaction]:
        """Rows left mid-mutation ('applying' or 'rolling_back') by a
        previous run. Unlike a bare 'pending' row, these mean files may
        have been partially changed, so recovery treats them as serious."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE kind=? AND status IN (?, ?) ORDER BY id",
                (kind, APPLYING, ROLLING_BACK),
            ).fetchall()
            return [t for t in (self._safe_row_to_txn(r) for r in rows) if t is not None]

    def recover(self, kind: str) -> list[Transaction]:
        """Startup crash recovery. Two distinct cases are handled differently:

          - A row still 'pending' died between begin() and the first file
            mutation, so nothing on disk was touched. It's marked
            'interrupted' - a benign "we noticed something didn't finish"
            notice.
          - A row still 'applying'/'rolling_back' died with file mutations
            possibly half-done. It's escalated to 'recovery_required' -
            the outcome is genuinely unknown and the file may be
            inconsistent, so it's flagged for attention rather than
            silently forgotten.

        Returns everything it found, for the caller to surface. Typically
        called once, when a Library is constructed."""
        recovered = []
        for txn in self.find_incomplete(kind):
            with self._lock, self._connect() as conn:
                conn.execute("UPDATE transactions SET status=? WHERE id=?", (INTERRUPTED, txn.id))
            recovered.append(replace(txn, status=INTERRUPTED))
        for txn in self.find_in_progress(kind):
            info = {"note": "process exited while this operation was mid-apply; "
                            "on-disk state is unverified and may be partially changed."}
            with self._lock, self._connect() as conn:
                conn.execute(
                    "UPDATE transactions SET status=?, rollback_info=? WHERE id=?",
                    (RECOVERY_REQUIRED, json.dumps(info), txn.id),
                )
            recovered.append(replace(txn, status=RECOVERY_REQUIRED, rollback_info=info))

        # Third case: a row whose JSON columns won't parse at all (a torn or
        # truncated write, a disk-level bit flip). The enumeration helpers
        # above skip these so one bad row can't crash startup - but skipping
        # silently would let a genuinely damaged operation vanish, so we find
        # them explicitly and escalate to RECOVERY_REQUIRED. The bad payload
        # is quarantined into valid sentinel JSON (the raw bytes stashed in
        # rollback_info for forensics) so the row parses cleanly next time and
        # this pass is idempotent - it won't re-flag the same row every start.
        recovered.extend(self._quarantine_corrupt(kind))
        return recovered

    def _quarantine_corrupt(self, kind: str) -> list[Transaction]:
        out = []
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM transactions WHERE kind=?", (kind,)).fetchall()
            for row in rows:
                if self._safe_row_to_txn(row) is not None:
                    continue  # parses fine - not corrupt
                rid = row["id"]
                # Salvage whatever plain-TEXT columns are still readable; only
                # the JSON-typed columns are assumed damaged.
                raw_before = row["before_state"] if "before_state" in row.keys() else None
                info = {
                    "note": "journal row had unreadable JSON (corrupt or torn write); it was "
                            "quarantined and flagged for attention. The on-disk state for this "
                            "operation is unverified - check this file manually.",
                    "corrupt_before_state": (str(raw_before)[:400] if raw_before is not None else None),
                }
                conn.execute(
                    "UPDATE transactions SET status=?, before_state=?, after_state='{}', "
                    "operation='{}', rollback_info=? WHERE id=?",
                    (RECOVERY_REQUIRED, json.dumps({"_corrupt": True}), json.dumps(info), rid),
                )
                out.append(Transaction(
                    id=rid, kind=kind, status=RECOVERY_REQUIRED,
                    created_at=(row["created_at"] if "created_at" in row.keys() else 0.0) or 0.0,
                    committed_at=None,
                    original_path=(row["original_path"] if "original_path" in row.keys() else "") or "",
                    current_path=(row["current_path"] if "current_path" in row.keys() else "") or "",
                    before_state={"_corrupt": True}, after_state={}, operation={},
                    error=(row["error"] if "error" in row.keys() else None),
                    rollback_info=info,
                ))
        return out
