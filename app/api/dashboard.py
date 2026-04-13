import hashlib
import hmac
import html as html_mod
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func
from sqlalchemy.orm import Session
from starlette.responses import Response

from app.config import settings
from app.core.training.calibration import get_ece_tracker
from app.db import get_db
from app.models.approval_log import ApprovalRequest
from app.models.labeling_queue import LabelingItem
from app.models.trace import Trace

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_template_dir = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_template_dir))
templates.env.autoescape = True

_CSRF_MAX_AGE = 3600


def _csrf_key() -> bytes:
    return hashlib.sha256(("csrf:" + settings.get_session_secret()).encode()).digest()


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


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> Response:
    """Show login form (only relevant when NEXUS_API_KEY is set)."""
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login_submit(request: Request, api_key: str = Form(...)) -> Response:
    """Validate API key and set session flag."""
    from app.middleware import _safe_key_compare

    expected = settings.NEXUS_API_KEY.strip()
    if not expected or _safe_key_compare(api_key, expected):
        request.session["dashboard_authed"] = True
        return RedirectResponse(url="/dashboard", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": "Invalid API key"}, status_code=403)


@router.get("/logout")
def logout(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse(url="/dashboard/login", status_code=302)


@router.get("", response_class=HTMLResponse)
def trace_list(request: Request, db: Session = Depends(get_db)) -> Response:
    traces = db.query(Trace).order_by(Trace.created_at.desc()).limit(100).all()
    stats = db.query(
        func.count().label("total"),
        func.count(case((Trace.status == "completed", 1))).label("completed"),
        func.count(case((Trace.status.in_(["blocked", "halted", "error"]), 1))).label("blocked"),
    ).one()
    total, completed, blocked = stats.total, stats.completed, stats.blocked

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


_VALID_LABELS = {"correct_flag", "incorrect", "false_positive", "needs_review"}


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

    if label not in _VALID_LABELS:
        return HTMLResponse(
            f"<h1>Invalid label. Must be one of: {', '.join(sorted(_VALID_LABELS))}</h1>",
            status_code=400,
        )

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

    from app.services.approval import process_vote

    result = process_vote(
        request_id=request_id,
        approver_id=approver_id,
        decision=decision,
        db=db,
    )
    if result.error:
        logger.warning("Dashboard vote failed on %s: %s", request_id, result.error)
        safe_error = html_mod.escape(result.error)
        return HTMLResponse(
            f"<h1>Vote failed</h1><p>{safe_error}</p>"
            '<p><a href="/dashboard/approvals">Back to approvals</a></p>',
            status_code=result.http_status,
        )

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
