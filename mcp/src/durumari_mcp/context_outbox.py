"""Encrypted request outbox: short SQLite WAL transactions, never network under a lock."""

import hashlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken

from durumari_mcp.credentials import AdapterError, profile_name
from durumari_mcp.private_files import protect_file
from durumari_mcp.project_credentials import ProjectProfile
from durumari_mcp.project_outbox import process_lock, protected_directory

CORRUPT_CODES = {11, 26}  # SQLITE_CORRUPT, SQLITE_NOTADB (primary result codes)
BUSY_CODES = {5, 6}  # SQLITE_BUSY, SQLITE_LOCKED
CHECKED: set[Path] = set()  # Queues this process has integrity-checked.


@dataclass(frozen=True)
class PendingStatus:
    count: int
    recovery_required: bool


class ContextOutbox:
    PREFIX_PATTERN: ClassVar[str] = r"/api/v2/projects/[A-Za-z0-9-]{1,64}/contexts"
    REQUEST_SUFFIX_PATTERN: ClassVar[str] = r"(?:/[A-Za-z0-9-]{1,64}(?:/restore)?)?"
    MAX_PAYLOAD_BYTES: ClassVar[int] = 262144

    def __init__(self, root: Path, name: str, profile: ProjectProfile) -> None:
        self.directory = root / profile_name(name)
        self.path = self.directory / "context.sqlite3"
        self.cipher = Fernet(profile.outbox_key.get_secret_value().encode("ascii"))
        self.prefix = "/api/v2/projects/" + profile.project_id + "/contexts"
        scope = [
            profile.gateway_origin,
            profile.connection_id,
            profile.project_id,
            profile.source_id,
            profile.actor_id,
            profile.client,
            profile.workspace,
        ]
        self.scope = hashlib.sha256(json.dumps(scope).encode()).hexdigest()

    @classmethod
    def from_scope(
        cls, root: Path, name: str, *, scope: list[str], outbox_key: str, prefix: str
    ) -> "ContextOutbox":
        """Reuse queue durability with an explicit, non-workspace credential scope."""
        if not scope or not all(isinstance(value, str) and value for value in scope):
            raise AdapterError("invalid_context_outbox_scope")
        if not re.fullmatch(cls.PREFIX_PATTERN, prefix):
            raise AdapterError("invalid_context_outbox_scope")
        result = cls.__new__(cls)
        result.directory = root / profile_name(name)
        result.path = result.directory / "context.sqlite3"
        try:
            result.cipher = Fernet(outbox_key.encode("ascii"))
        except (ValueError, UnicodeError):
            raise AdapterError("invalid_context_outbox_scope") from None
        result.prefix = prefix
        result.scope = hashlib.sha256(json.dumps(scope).encode()).hexdigest()
        return result

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._queue_access():
            with self._transaction() as database:
                yield database

    @contextmanager
    def _queue_access(self) -> Iterator[None]:
        # On POSIX an open database can still be renamed. Hold one process lock
        # from before opening it until close/quarantine, including read accesses.
        protected_directory(self.directory)
        with ExitStack() as locks:
            try:
                locks.enter_context(process_lock(self.directory / "queue-access", timeout=2))
            except AdapterError as error:
                if str(error) == "project_profile_busy":
                    raise AdapterError("context_outbox_busy") from None
                raise
            yield

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        for suffix in ("", "-wal", "-shm"):
            if self.path.with_name(self.path.name + suffix).is_symlink():
                raise AdapterError("unsafe_outbox_path")
        descriptor = os.open(
            self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        os.close(descriptor)
        protect_file(self.path)
        database = sqlite3.connect(self.path, timeout=2, isolation_level=None)
        database.row_factory = sqlite3.Row
        corrupted = False
        try:
            database.execute("PRAGMA synchronous=FULL")
            database.execute("PRAGMA busy_timeout=2000")
            if self.path not in CHECKED:
                # Damage surfaces only when a bad page is read, so verify the small
                # queue once per process before any write can build on it.
                if database.execute("PRAGMA integrity_check(1)").fetchone()[0] != "ok":
                    corrupted = True
                    raise AdapterError("context_outbox_corrupted")
                CHECKED.add(self.path)
            # Switching a fresh database into WAL races even with busy_timeout.
            # Serialize this short bootstrap only; callers release it before network I/O.
            with process_lock(self.directory / "sqlite-bootstrap", timeout=2):
                database.execute("PRAGMA journal_mode=WAL")
                database.execute("CREATE TABLE IF NOT EXISTS scope (identity TEXT PRIMARY KEY)")
                database.execute("""CREATE TABLE IF NOT EXISTS requests (
                    request_key TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, payload BLOB NOT NULL,
                    state TEXT NOT NULL DEFAULT 'queued', receipt BLOB,
                    lease TEXT, lease_until REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                )""")
                database.execute(
                    "CREATE INDEX IF NOT EXISTS pending_session ON requests(session_id,state)"
                )
            database.execute("BEGIN IMMEDIATE")
            existing = database.execute("SELECT identity FROM scope").fetchone()
            if existing is None:
                database.execute("INSERT INTO scope VALUES (?)", (self.scope,))
            elif existing[0] != self.scope:
                raise AdapterError("context_outbox_scope_mismatch")
            yield database
            database.execute("COMMIT")
        except (sqlite3.Error, InvalidToken, ValueError) as error:
            code = getattr(error, "sqlite_errorcode", None)
            corrupted = code is not None and code & 0xFF in CORRUPT_CODES
            if database.in_transaction and not corrupted:
                database.execute("ROLLBACK")
            if corrupted:
                failure = "context_outbox_corrupted"
            elif code is not None and code & 0xFF in BUSY_CODES:
                failure = "context_outbox_busy"
            elif isinstance(error, InvalidToken):
                failure = "context_outbox_decrypt_failed"
            else:
                failure = "context_outbox_unavailable"
            raise AdapterError(failure) from error
        finally:
            if database.in_transaction and not corrupted:
                database.execute("ROLLBACK")
            database.close()
            if corrupted and self._quarantine_locked():
                # The next transaction starts a fresh queue; retry with the same request key.
                raise AdapterError("context_outbox_quarantined")

    def quarantine(self) -> bool:
        try:
            protected_directory(self.directory)
            with process_lock(self.directory / "queue-access", timeout=2):
                return self._quarantine_locked()
        except (OSError, AdapterError):
            return False  # An active writer keeps the database and every WAL in place.

    def _quarantine_locked(self) -> bool:
        """Set a corrupt queue aside unchanged; unconfirmed writes are never deleted."""
        name = self.path.name + ".corrupt-" + time.strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8]
        moved: list[tuple[Path, Path]] = []
        try:
            with process_lock(self.directory / "sqlite-bootstrap", timeout=2):
                # A WAL left beside a fresh queue could be replayed into it, so the
                # database and its companions move together or not at all.
                for part in ("", "-wal", "-shm"):
                    source = self.path.with_name(self.path.name + part)
                    if source.exists():
                        target = source.with_name(name + part)
                        os.replace(source, target)
                        moved.append((target, source))
        except (OSError, AdapterError):
            # Another process still holds the queue open; report corruption only.
            for target, source in reversed(moved):
                try:
                    os.replace(target, source)
                except OSError:
                    pass
            return False
        if moved:
            CHECKED.discard(self.path)
        return bool(moved)

    def enqueue(self, key: str, session: str, method: str, path: str, body: dict[str, Any]) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key) or not 1 <= len(session) <= 256:
            raise AdapterError("invalid_context_request")
        if method not in {"POST", "PATCH", "DELETE"} or not re.fullmatch(
            re.escape(self.prefix) + self.REQUEST_SUFFIX_PATTERN, path
        ):
            raise AdapterError("invalid_context_request")
        payload = json.dumps(
            {"method": method, "path": path, "body": body},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        if len(payload.encode()) > self.MAX_PAYLOAD_BYTES:
            raise AdapterError("context_request_too_large")
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        encrypted = self.cipher.encrypt(payload.encode())
        timestamp = time.time()
        with self.transaction() as database:
            previous = database.execute(
                "SELECT fingerprint,session_id FROM requests WHERE request_key=?", (key,)
            ).fetchone()
            if previous:
                if previous[0] != fingerprint:
                    raise AdapterError("context_idempotency_conflict")
                # An explicit retry retains the persisted session even after process recreation.
                # Ordinary session draining never claims a sibling chat's requests.
                return str(previous[1])
            count = database.execute(
                "SELECT count(*) FROM requests WHERE session_id=? "
                "AND state IN ('queued','sending','unknown')",
                (session,),
            ).fetchone()[0]
            if count >= 1000:
                raise AdapterError("context_outbox_full")
            database.execute(
                """INSERT INTO requests
                (request_key,session_id,fingerprint,payload,created_at,updated_at)
                VALUES (?,?,?,?,?,?)""",
                (key, session, fingerprint, encrypted, timestamp, timestamp),
            )
            return session

    def pending(self, session: str | None) -> int:
        """Unconfirmed writes of one session, or of every session when session is None."""
        status = self.pending_status(session)
        if status.recovery_required:
            raise AdapterError("context_outbox_recovery_required")
        return status.count

    def pending_status(self, session: str | None) -> PendingStatus:
        # Nothing can have been quarantined inside an absent state directory.
        # This is a read-only zero snapshot before any writer creates the queue.
        if not self.directory.exists() and not self.directory.is_symlink():
            return PendingStatus(0, False)
        with self._queue_access():
            # Quarantine and the active count share the lock. Old unknown writes
            # remain visible independently from usable requests in the new queue.
            recovery_required = any(self.directory.glob(self.path.name + ".corrupt-*"))
            if not self.path.exists():
                return PendingStatus(0, recovery_required)
            query = "SELECT count(*) FROM requests WHERE state IN ('queued','sending','unknown')"
            with self._transaction() as database:
                if session is None:
                    count = int(database.execute(query).fetchone()[0])
                else:
                    count = int(
                        database.execute(query + " AND session_id=?", (session,)).fetchone()[0]
                    )
                return PendingStatus(count, recovery_required)

    def claim(self, session: str, *, key: str | None = None) -> dict[str, Any] | None:
        timestamp = time.time()
        with self.transaction() as database:
            row = database.execute(
                """SELECT * FROM requests WHERE session_id=?
                AND state IN ('queued','unknown','sending') AND lease_until<=?
                AND (? IS NULL OR request_key=?) ORDER BY created_at,request_key LIMIT 1""",
                (session, timestamp, key, key),
            ).fetchone()
            if row is None:
                return None
            lease = uuid4().hex
            database.execute(
                """UPDATE requests SET state='sending',lease=?,lease_until=?,updated_at=?
                WHERE request_key=?""",
                (lease, timestamp + 30, timestamp, row["request_key"]),
            )
            payload = json.loads(self.cipher.decrypt(row["payload"]))
            return {"request_key": row["request_key"], "lease": lease, **payload}

    def complete(self, key: str, lease: str, receipt: dict[str, Any]) -> None:
        encrypted = self.cipher.encrypt(json.dumps(receipt, ensure_ascii=False).encode())
        with self.transaction() as database:
            database.execute(
                """UPDATE requests SET state='stored',receipt=?,lease=NULL,lease_until=0,
                updated_at=? WHERE request_key=? AND lease=?""",
                (encrypted, time.time(), key, lease),
            )

    def confirm(self, key: str, receipt: dict[str, Any]) -> None:
        """A verified server receipt reconciles a lost reply without claiming another session."""
        encrypted = self.cipher.encrypt(json.dumps(receipt, ensure_ascii=False).encode())
        with self.transaction() as database:
            database.execute(
                """UPDATE requests SET state='stored',receipt=?,lease=NULL,lease_until=0,
                updated_at=? WHERE request_key=? AND state IN ('queued','sending','unknown')""",
                (encrypted, time.time(), key),
            )

    def unknown(self, key: str, lease: str) -> None:
        with self.transaction() as database:
            database.execute(
                """UPDATE requests SET state='unknown',lease=NULL,lease_until=0,
                updated_at=? WHERE request_key=? AND lease=?""",
                (time.time(), key, lease),
            )

    def reject(self, key: str, lease: str, error: dict[str, Any]) -> None:
        encrypted = self.cipher.encrypt(json.dumps(error, ensure_ascii=False).encode())
        with self.transaction() as database:
            database.execute(
                """UPDATE requests SET state='rejected',receipt=?,lease=NULL,lease_until=0,
                updated_at=? WHERE request_key=? AND lease=?""",
                (encrypted, time.time(), key, lease),
            )

    def result(self, key: str) -> dict[str, Any] | None:
        with self.transaction() as database:
            row = database.execute(
                "SELECT receipt FROM requests WHERE request_key=? AND state='stored'", (key,)
            ).fetchone()
            if row is None or row[0] is None:
                return None
            value = json.loads(self.cipher.decrypt(row[0]))
            if not isinstance(value, dict):
                raise AdapterError("invalid_context_receipt")
            return value

    def rejection(self, key: str) -> dict[str, Any] | None:
        with self.transaction() as database:
            row = database.execute(
                "SELECT receipt FROM requests WHERE request_key=? AND state='rejected'", (key,)
            ).fetchone()
            if row is None or row[0] is None:
                return None
            value = json.loads(self.cipher.decrypt(row[0]))
            if not isinstance(value, dict):
                raise AdapterError("invalid_context_receipt")
            return value

    def stop(self) -> None:
        """Disconnect stops delivery while preserving encrypted pending work for recovery."""
        if not self.path.exists():
            return
        with self.transaction() as database:
            database.execute(
                "UPDATE requests SET state='stopped',lease=NULL,lease_until=0 WHERE state!='stored'"
            )
