import hashlib
import hmac
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.responses import Response

from app.config import settings
from app.core.training.calibration import get_ece_tracker
from app.db import get_db
from app.models.approval_log import ApprovalRequest, ApprovalVote
from app.models.labeling_queue import LabelingItem
from app.models.trace import Trace

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_template_dir = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_template_dir))

_CSRF_MAX_AGE = 3600


def _csrf_key() -> bytes:
    raw = settings.SESSION_SECRET.strip() or "dev-csrf-not-for-production"
    return hashlib.sha256(raw.encode()).digest()


def _issue_csrf(request: Request) -> str | None:
    """Issue an HMAC-based CSRF token (stateless — safe across multiple tabs)."""
    if not settings.ENFORCE_DASHBOARD_CSRF:
        return None
    ts = str(int(time.time()))
    sig = hmac.new(_csrf_key(), ts.encode(), hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"


def _csrf_valid(request: Request, form_token: str | None) -> bool:
    if not settings.ENFORCE_DASHBOARD_CSRF:
        return True
    if not form_token or "." not in form_token:
        return False
    ts_str, sig = form_token.split(".", 1)
    expected = hmac.new(_csrf_key(), ts_str.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        age = int(time.time()) - int(ts_str)
        return 0 <= age <= _CSRF_MAX_AGE
    except ValueError:
        return False


@router.get("", response_class=HTMLResponse)
def trace_list(request: Request, db: Session = Depends(get_db)) -> Response:
    traces = db.query(Trace).order_by(Trace.created_at.desc()).limit(100).all()
    total = db.query(Trace).count()
    completed = db.query(Trace).filter(Trace.status == "completed").count()
    blocked = db.query(Trace).filter(Trace.status.in_(["blocked", "halted", "error"])).count()

    return templates.TemplateResponse(
        request,
        "traces.html",
        {
            "active": "traces",
            "traces": traces,
            "total": total,
            "completed": completed,
            "blocked": blocked,
        },
    )


@router.get("/traces/{trace_id}", response_class=HTMLResponse)
def trace_detail(trace_id: str, request: Request, db: Session = Depends(get_db)) -> Response:
    trace = db.query(Trace).filter(Trace.id == trace_id).first()
    if not trace:
        return HTMLResponse("<h1>Trace not found</h1>", status_code=404)

    return templates.TemplateResponse(
        request,
        "trace_detail.html",
        {"active": "traces", "trace": trace},
    )


@router.get("/labeling", response_class=HTMLResponse)
def labeling_queue(request: Request, db: Session = Depends(get_db)) -> Response:
    items = db.query(LabelingItem).order_by(LabelingItem.created_at.desc()).limit(100).all()
    pending = db.query(LabelingItem).filter(LabelingItem.status == "pending").count()
    labeled = db.query(LabelingItem).filter(LabelingItem.status == "labeled").count()
    exported = db.query(LabelingItem).filter(LabelingItem.status == "exported").count()

    csrf_token = _issue_csrf(request)
    return templates.TemplateResponse(
        request,
        "labeling.html",
        {
            "active": "labeling",
            "items": items,
            "pending": pending,
            "labeled": labeled,
            "exported": exported,
            "csrf_token": csrf_token,
        },
    )


@router.post("/labeling/{item_id}/label")
def apply_label(
    request: Request,
    item_id: str,
    label: str = Form(...),
    csrf_token: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    if not _csrf_valid(request, csrf_token):
        return HTMLResponse("<h1>CSRF validation failed</h1>", status_code=403)

    item = db.query(LabelingItem).filter(LabelingItem.id == item_id).first()
    if item and item.status == "pending":
        item.label = label
        item.status = "labeled"
        item.labeled_at = datetime.now(UTC)
        item.reviewer_id = "dashboard-user"
        db.commit()
        logger.info("Labeled item %s as %s", item_id, label)

    return RedirectResponse(url="/dashboard/labeling", status_code=303)


@router.get("/approvals", response_class=HTMLResponse)
def approval_list(request: Request, db: Session = Depends(get_db)) -> Response:
    requests_list = db.query(ApprovalRequest).order_by(ApprovalRequest.created_at.desc()).limit(100).all()
    pending = db.query(ApprovalRequest).filter(ApprovalRequest.status == "pending").count()
    approved = db.query(ApprovalRequest).filter(ApprovalRequest.status == "approved").count()
    denied = db.query(ApprovalRequest).filter(ApprovalRequest.status == "denied").count()

    csrf_token = _issue_csrf(request)
    return templates.TemplateResponse(
        request,
        "approvals.html",
        {
            "active": "approvals",
            "requests": requests_list,
            "pending": pending,
            "approved": approved,
            "denied": denied,
            "csrf_token": csrf_token,
        },
    )


@router.post("/approvals/{request_id}/vote")
def cast_vote(
    request: Request,
    request_id: str,
    decision: str = Form(...),
    approver_id: str = Form("dashboard-user"),
    csrf_token: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    if not _csrf_valid(request, csrf_token):
        return HTMLResponse("<h1>CSRF validation failed</h1>", status_code=403)

    approval = db.query(ApprovalRequest).filter(ApprovalRequest.id == request_id).first()
    if not approval or approval.status != "pending":
        return RedirectResponse(url="/dashboard/approvals", status_code=303)

    vote = ApprovalVote(
        request_id=request_id,
        approver_id=approver_id,
        decision=decision,
    )
    db.add(vote)

    current = int(approval.received_approvals)
    required = int(approval.required_approvals)

    if decision == "approve":
        current += 1
        approval.received_approvals = str(current)
        if current >= required:
            approval.status = "approved"
            approval.resolved_at = datetime.now(UTC)
    elif decision == "deny":
        approval.status = "denied"
        approval.resolved_at = datetime.now(UTC)

    db.commit()
    logger.info("Vote on %s: %s by %s", request_id, decision, approver_id)

    return RedirectResponse(url="/dashboard/approvals", status_code=303)


@router.get("/calibration", response_class=HTMLResponse)
def calibration_dashboard(request: Request) -> Response:
    tracker = get_ece_tracker()
    report = tracker.compute_ece()

    bins = []
    for b in report.bins:
        if b["count"] > 0:
            bins.append(
                {
                    "low": b["bin_lo"],
                    "high": b["bin_hi"],
                    "count": b["count"],
                    "avg_confidence": b["avg_confidence"],
                    "accuracy": b["avg_accuracy"],
                    "gap": b["avg_confidence"] - b["avg_accuracy"],
                }
            )

    total_conf = sum(b["avg_confidence"] * b["count"] for b in report.bins if b["count"] > 0)
    total_acc = sum(b["avg_accuracy"] * b["count"] for b in report.bins if b["count"] > 0)
    total_count = sum(b["count"] for b in report.bins)

    avg_confidence = total_conf / total_count if total_count else 0.0
    accuracy = total_acc / total_count if total_count else 0.0

    return templates.TemplateResponse(
        request,
        "calibration.html",
        {
            "active": "calibration",
            "ece": report.ece,
            "record_count": report.num_samples,
            "bins": bins,
            "avg_confidence": avg_confidence,
            "accuracy": accuracy,
        },
    )
