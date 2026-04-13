"""Tests for trace hash chain verification."""

from app.agent.pipeline import run
from app.models.trace import Trace
from app.services.integrity import cascade_rehash_from_trace, compute_trace_hash, verify_chain


def test_verify_chain_valid(db_session):
    sid = "integrity-session"
    run("a", session_id=sid, db_session=db_session)
    run("b", session_id=sid, db_session=db_session)
    assert verify_chain(sid, db_session) == []


def test_verify_chain_detects_tamper(db_session):
    sid = "tamper-session"
    r = run("x", session_id=sid, db_session=db_session)
    t = db_session.query(Trace).filter_by(id=r.trace_id).first()
    assert t
    t.trace_hash = "deadbeef" * 8
    db_session.commit()

    problems = verify_chain(sid, db_session)
    assert any(p.get("issue") == "trace_hash_mismatch" for p in problems)


def test_verify_chain_empty_session(db_session):
    assert verify_chain("no-such-session-xyz", db_session) == []


def test_compute_trace_hash_stable():
    h1 = compute_trace_hash("tid", "genesis", "ph", "rh", "completed")
    h2 = compute_trace_hash("tid", "genesis", "ph", "rh", "completed")
    assert h1 == h2
    assert len(h1) == 64


def test_cascade_rehash_repairs_chain(db_session):
    sid = "rehash-session"
    run("first", session_id=sid, db_session=db_session)
    r2 = run("second", session_id=sid, db_session=db_session)
    run("third", session_id=sid, db_session=db_session)

    assert verify_chain(sid, db_session) == []

    t2 = db_session.query(Trace).filter_by(id=r2.trace_id).first()
    assert t2
    t2.status = "blocked"
    db_session.commit()

    problems_before = verify_chain(sid, db_session)
    assert len(problems_before) > 0

    cascade_rehash_from_trace(db_session, r2.trace_id)
    db_session.commit()

    assert verify_chain(sid, db_session) == []


def test_cascade_rehash_nonexistent_trace_noop(db_session):
    cascade_rehash_from_trace(db_session, "nonexistent-id")  # should not raise
