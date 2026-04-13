"""Tests for trace hash chain verification."""

from app.agent.pipeline import run
from app.models.trace import Trace
from app.services.integrity import compute_trace_hash, verify_chain


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
