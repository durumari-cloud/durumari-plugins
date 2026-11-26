"""Encrypted per-request WAL state stays independent from legacy capture quarantine."""

import multiprocessing
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier

import pytest
from test_continuity import profile

from durumari_mcp import context_outbox
from durumari_mcp.context_outbox import ContextOutbox
from durumari_mcp.credentials import AdapterError


def test_replay_after_restart_keeps_key_scope_and_ciphertext(tmp_path):
    scope = profile(tmp_path)
    outbox = ContextOutbox(tmp_path / "state", "team", scope)
    body = {"title": "durable synthetic checkpoint", "body": "private test text"}
    outbox.enqueue("request-1", "chat-a", "POST", "/api/v2/projects/project-1/contexts", body)
    assert outbox.pending("chat-a") == 1 and outbox.pending("chat-b") == 0
    claim = outbox.claim("chat-a")
    assert claim["request_key"] == "request-1" and claim["body"] == body
    outbox.unknown("request-1", claim["lease"])
    restarted = ContextOutbox(tmp_path / "state", "team", scope)
    assert restarted.claim("chat-a")["request_key"] == "request-1"
    assert b"private test text" not in outbox.path.read_bytes()
    assert (
        b"durable synthetic checkpoint"
        not in outbox.path.with_name("context.sqlite3-wal").read_bytes()
        if outbox.path.with_name("context.sqlite3-wal").exists()
        else True
    )


def test_two_chats_can_claim_while_a_sibling_is_waiting_for_the_server(tmp_path):
    outbox = ContextOutbox(tmp_path / "state", "team", profile(tmp_path))
    for session in ("chat-a", "chat-b"):
        outbox.enqueue(
            session, session, "POST", "/api/v2/projects/project-1/contexts", {"body": session}
        )
    network = Barrier(2)

    def deliver(session):
        item = outbox.claim(session)
        network.wait(timeout=2)
        outbox.complete(item["request_key"], item["lease"], {"status": "stored", "revision": 1})

    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(deliver, ["chat-a", "chat-b"]))
    assert outbox.pending("chat-a") == outbox.pending("chat-b") == 0


def test_reused_key_cannot_change_body_and_receipt_survives_restart(tmp_path):
    scope = profile(tmp_path)
    box = ContextOutbox(tmp_path / "state", "team", scope)
    box.enqueue("key", "a", "POST", "/api/v2/projects/project-1/contexts", {"body": "one"})
    with pytest.raises(AdapterError, match="context_idempotency_conflict"):
        box.enqueue("key", "a", "POST", "/api/v2/projects/project-1/contexts", {"body": "two"})
    item = box.claim("a")
    receipt = {"status": "stored", "revision": 3}
    box.complete("key", item["lease"], receipt)
    assert ContextOutbox(tmp_path / "state", "team", scope).result("key") == receipt


def test_wrong_project_and_symlink_state_are_rejected(tmp_path):
    scope = profile(tmp_path)
    box = ContextOutbox(tmp_path / "state", "team", scope)
    box.enqueue("key", "a", "POST", "/api/v2/projects/project-1/contexts", {})
    changed = scope.model_copy(update={"project_id": "another"})
    with pytest.raises(AdapterError, match="context_outbox_scope_mismatch"):
        ContextOutbox(tmp_path / "state", "team", changed).pending("a")
    box.path.unlink()
    outside = tmp_path / "outside"
    outside.write_text("preserve")
    box.path.symlink_to(outside)
    with pytest.raises(AdapterError, match="unsafe_outbox_path"):
        box.pending("a")
    assert outside.read_text() == "preserve"


CONTEXTS = "/api/v2/projects/project-1/contexts"


def hold_queue_transaction(root, scope, ready, release):
    box = ContextOutbox(root, "team", scope)
    with box.transaction():
        ready.set()
        if not release.wait(15):
            raise RuntimeError("synthetic holder timed out")


def test_quarantine_cannot_move_a_queue_open_in_another_writer(tmp_path):
    scope = profile(tmp_path)
    root = tmp_path / "state"
    box = ContextOutbox(root, "team", scope)
    box.enqueue("preserved", "chat-a", "POST", CONTEXTS, {"body": "keep"})
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    holder = context.Process(target=hold_queue_transaction, args=(root, scope, ready, release))
    holder.start()
    try:
        assert ready.wait(10)
        assert box.quarantine() is False
        assert box.path.exists() and corrupt_copies(box) == []
        with pytest.raises(AdapterError, match="context_outbox_busy"):
            box.pending("chat-a")
    finally:
        release.set()
        holder.join(10)
        if holder.is_alive():
            holder.terminate()
            holder.join(5)
    assert holder.exitcode == 0
    assert box.claim("chat-a")["request_key"] == "preserved"


def test_transaction_preserves_an_adapter_error_from_its_caller(tmp_path):
    box = ContextOutbox(tmp_path / "state", "team", profile(tmp_path))
    with pytest.raises(AdapterError, match="^project_profile_busy$"):
        with box.transaction():
            raise AdapterError("project_profile_busy")
    assert box.pending("chat-a") == 0


def corrupt_copies(outbox):
    return sorted(outbox.directory.glob("context.sqlite3.corrupt-*"))


def test_unreadable_queue_is_quarantined_unchanged_and_replaced(tmp_path):
    scope = profile(tmp_path)
    outbox = ContextOutbox(tmp_path / "state", "team", scope)
    outbox.directory.mkdir(parents=True)
    original = b"synthetic damaged queue " * 400
    outbox.path.write_bytes(original)
    with pytest.raises(AdapterError, match="context_outbox_quarantined"):
        outbox.enqueue("request-1", "chat-a", "POST", CONTEXTS, {"body": "retry"})
    copies = corrupt_copies(outbox)
    assert len(copies) == 1 and copies[0].read_bytes() == original
    # The same request key succeeds on the fresh queue.
    outbox.enqueue("request-1", "chat-a", "POST", CONTEXTS, {"body": "retry"})
    assert outbox.claim("chat-a")["request_key"] == "request-1"
    with pytest.raises(AdapterError, match="context_outbox_recovery_required"):
        outbox.pending("chat-a")


def test_truncated_queue_with_unconfirmed_writes_is_preserved(tmp_path):
    scope = profile(tmp_path)
    outbox = ContextOutbox(tmp_path / "state", "team", scope)
    for index in range(200):
        outbox.enqueue(f"request-{index}", "chat-a", "POST", CONTEXTS, {"body": "x" * 300})
    import sqlite3

    checkpoint = sqlite3.connect(outbox.path)
    checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    checkpoint.close()
    data = outbox.path.read_bytes()
    outbox.path.write_bytes(data[: len(data) // 2 - 1])
    with pytest.raises(AdapterError, match="context_outbox_quarantined"):
        outbox.pending("chat-a")
    assert corrupt_copies(outbox)[0].read_bytes() == data[: len(data) // 2 - 1]
    # An empty replacement must not claim the quarantined requests are settled.
    restarted = ContextOutbox(tmp_path / "state", "team", scope)
    for session in ("chat-a", "another-session", None):
        with pytest.raises(AdapterError, match="context_outbox_recovery_required"):
            restarted.pending(session)


def test_pending_detects_quarantine_completed_before_its_lock_acquisition(tmp_path, monkeypatch):
    box = ContextOutbox(tmp_path / "state", "team", profile(tmp_path))
    box.enqueue("unconfirmed", "chat-a", "POST", CONTEXTS, {})
    original_lock = context_outbox.process_lock
    quarantined = False

    @contextmanager
    def after_prior_quarantine(path, *, timeout):
        nonlocal quarantined
        with original_lock(path, timeout=timeout):
            if path.name == "queue-access" and not quarantined:
                quarantined = True
                assert box._quarantine_locked()
            yield

    monkeypatch.setattr(context_outbox, "process_lock", after_prior_quarantine)
    with pytest.raises(AdapterError, match="context_outbox_recovery_required"):
        box.pending(None)


def test_pending_status_counts_new_requests_without_hiding_quarantined_records(tmp_path):
    scope = profile(tmp_path)
    box = ContextOutbox(tmp_path / "state", "team", scope)
    box.enqueue("old", "chat-a", "POST", CONTEXTS, {})
    assert box.quarantine()
    preserved = corrupt_copies(box)[0].read_bytes()
    box.enqueue("new-a", "chat-a", "POST", CONTEXTS, {})
    box.enqueue("new-b", "chat-b", "POST", CONTEXTS, {})
    restarted = ContextOutbox(tmp_path / "state", "team", scope)
    assert restarted.pending_status(None).count == 2
    assert restarted.pending_status("chat-a").count == 1
    assert restarted.pending_status(None).recovery_required is True
    assert corrupt_copies(box)[0].read_bytes() == preserved


def test_pending_without_any_queue_does_not_create_state_files(tmp_path):
    root = tmp_path / "state"
    box = ContextOutbox(root, "team", profile(tmp_path))
    assert box.pending(None) == 0
    assert not root.exists()


def test_busy_and_undecryptable_queues_report_distinct_causes(tmp_path):
    import sqlite3

    scope = profile(tmp_path)
    outbox = ContextOutbox(tmp_path / "state", "team", scope)
    outbox.enqueue("request-1", "chat-a", "POST", CONTEXTS, {"body": "x"})
    holder = sqlite3.connect(outbox.path, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(AdapterError, match="context_outbox_busy"):
            outbox.enqueue("request-2", "chat-a", "POST", CONTEXTS, {"body": "y"})
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    other_key = profile(tmp_path)  # Same scope, a different encryption key.
    with pytest.raises(AdapterError, match="context_outbox_decrypt_failed"):
        ContextOutbox(tmp_path / "state", "team", other_key).claim("chat-a")
    assert corrupt_copies(outbox) == []


def test_queue_held_open_elsewhere_preserves_original_bytes(tmp_path):
    scope = profile(tmp_path)
    outbox = ContextOutbox(tmp_path / "state", "team", scope)
    outbox.directory.mkdir(parents=True)
    original = b"synthetic damaged queue " * 400
    outbox.path.write_bytes(original)
    with open(outbox.path, "rb") as held:
        expected = (
            "context_outbox_corrupted" if sys.platform == "win32"
            else "context_outbox_quarantined"
        )
        with pytest.raises(AdapterError, match=expected):
            outbox.pending("chat-a")
        assert held.read() == original
    if sys.platform == "win32":
        assert outbox.path.read_bytes() == original and corrupt_copies(outbox) == []
    else:
        assert not outbox.path.exists()
        assert len(corrupt_copies(outbox)) == 1
        assert corrupt_copies(outbox)[0].read_bytes() == original


def test_damage_not_yet_read_is_found_before_the_first_write_of_a_process(tmp_path, monkeypatch):
    import sqlite3

    from durumari_mcp import context_outbox

    scope = profile(tmp_path)
    outbox = ContextOutbox(tmp_path / "state", "team", scope)
    for index in range(30):
        outbox.enqueue(f"request-{index}", "chat-a", "POST", CONTEXTS, {"body": "x" * 300})
    checkpoint = sqlite3.connect(outbox.path)
    checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    checkpoint.close()
    damaged = outbox.path.read_bytes()[:-100]  # Breaks only an index page.
    outbox.path.write_bytes(damaged)
    monkeypatch.setattr(context_outbox, "CHECKED", set())  # A new process.
    with pytest.raises(AdapterError, match="context_outbox_quarantined"):
        outbox.enqueue("request-new", "chat-a", "POST", CONTEXTS, {"body": "y"})
    assert corrupt_copies(outbox)[0].read_bytes() == damaged
