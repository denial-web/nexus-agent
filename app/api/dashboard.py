import hashlib
import hmac
import html as html_mod
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
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
from app.models.skill import Skill
from app.models.trace import Trace
from app.sanitize import sanitize_for_log

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_template_dir = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_template_dir))
templates.env.autoescape = True
templates.env.globals["local_only"] = settings.LOCAL_ONLY

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
    from app.middleware import _parse_api_keys, check_api_key

    keys = _parse_api_keys()
    if not keys:
        request.session["dashboard_authed"] = True
        return RedirectResponse(url="/dashboard", status_code=303)
    valid, _is_primary = check_api_key(api_key)
    if valid:
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
            f'<h1>Vote failed</h1><p>{safe_error}</p><p><a href="/dashboard/approvals">Back to approvals</a></p>',
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


@router.get("/circuit-breakers", response_class=HTMLResponse)
def circuit_breakers_dashboard(request: Request) -> Response:
    from app.core.llm.circuit_breaker import get_registry

    breakers = get_registry().get_all_status()
    total = len(breakers)
    open_count = sum(1 for b in breakers if b["state"] == "open")
    half_open_count = sum(1 for b in breakers if b["state"] == "half_open")
    closed_count = total - open_count - half_open_count
    total_failures = sum(b["recent_failures"] for b in breakers)

    csrf_token = _issue_csrf(request)
    return templates.TemplateResponse(
        request,
        "circuit_breakers.html",
        {
            "active": "circuit_breakers",
            "breakers": breakers,
            "total": total,
            "open_count": open_count,
            "half_open_count": half_open_count,
            "closed_count": closed_count,
            "total_failures": total_failures,
            "csrf_token": csrf_token,
        },
    )


@router.post("/circuit-breakers/{provider}/reset")
def circuit_breaker_reset(
    provider: str,
    request: Request,
    csrf_token: str | None = Form(None),
) -> Response:
    if not _csrf_valid(request, csrf_token):
        return HTMLResponse("<h1>CSRF validation failed</h1>", status_code=403)

    from app.core.llm.circuit_breaker import get_registry

    registry = get_registry()
    all_names = [b["name"] for b in registry.get_all_status()]
    if provider not in all_names:
        safe_name = html_mod.escape(provider)
        return HTMLResponse(
            f"<h1>Not found</h1><p>Provider &ldquo;{safe_name}&rdquo; not found.</p>"
            '<p><a href="/dashboard/circuit-breakers">Back</a></p>',
            status_code=404,
        )
    registry.get(provider).reset()
    return RedirectResponse(url="/dashboard/circuit-breakers", status_code=303)


@router.get("/providers", response_class=HTMLResponse)
def providers_dashboard(request: Request, probe: bool = False) -> Response:
    from app.services.provider_health import get_provider_health

    providers = get_provider_health(
        run_probes=probe,
        probe_timeout=settings.HEALTH_PROBE_TIMEOUT,
    )
    configured_count = sum(1 for p in providers if p["configured"])
    healthy_count = sum(1 for p in providers if p["overall_status"] == "healthy")
    degraded_count = sum(1 for p in providers if p["overall_status"] == "degraded")
    down_count = sum(1 for p in providers if p["overall_status"] == "down")

    csrf_token = _issue_csrf(request)
    return templates.TemplateResponse(
        request,
        "providers.html",
        {
            "active": "providers",
            "providers": providers,
            "configured_count": configured_count,
            "healthy_count": healthy_count,
            "degraded_count": degraded_count,
            "down_count": down_count,
            "probed": probe,
            "csrf_token": csrf_token,
        },
    )


@router.post("/skills/import")
def skills_import_dashboard(
    request: Request,
    db: Session = Depends(get_db),
    file: UploadFile | None = File(None),
    url: str | None = Form(None),
    csrf_token: str | None = Form(None),
) -> Response:
    """Import SKILL.md from upload or optional URL (LOCAL_ONLY: upload only)."""
    from app.core.agent.clawhub_import import import_skill_from_url, import_skill_md

    if not _csrf_valid(request, csrf_token):
        return HTMLResponse("<h1>CSRF validation failed</h1>", status_code=403)
    if url and settings.LOCAL_ONLY:
        return HTMLResponse(
            "<h1>URL import disabled in LOCAL_ONLY</h1><p><a href=/dashboard/skills>Back</a></p>",
            status_code=503,
        )
    url_clean = (url or "").strip()
    has_file = file is not None and bool(getattr(file, "filename", None))
    if url_clean and has_file:
        return HTMLResponse("<h1>Provide file or URL, not both</h1>", status_code=400)
    if not url_clean and not has_file:
        return HTMLResponse("<h1>No file or URL</h1>", status_code=400)

    sid: str | None = None
    if url_clean:
        sid = import_skill_from_url(url_clean, db, force=False)
    else:
        raw = file.file.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return HTMLResponse("Invalid UTF-8 file", status_code=400)
        fname = file.filename or "skill.md"
        sid = import_skill_md(
            content=text,
            db=db,
            source_label=f"import:upload:{fname}"[:100],
            force=False,
        )
    if not sid:
        return HTMLResponse(
            "<h1>Import blocked or failed</h1><p><a href=/dashboard/skills>Back</a></p>",
            status_code=400,
        )
    return RedirectResponse(url=f"/dashboard/skills/{sid}", status_code=303)


@router.get("/skills", response_class=HTMLResponse)
def skills_list(request: Request, db: Session = Depends(get_db)) -> Response:
    all_skills = db.query(Skill).order_by(Skill.avg_reward.desc().nullslast()).limit(200).all()
    total = len(all_skills)
    enabled_count = sum(1 for s in all_skills if s.enabled)
    flagged_count = sum(1 for s in all_skills if s.flagged)

    skills_data = [
        {
            "id": s.id,
            "name": s.name,
            "description": s.description or "",
            "step_count": len(s.steps) if s.steps else 0,
            "total_runs": s.total_runs or 0,
            "avg_reward": s.avg_reward,
            "last_reward": s.last_reward,
            "enabled": s.enabled,
            "flagged": s.flagged,
        }
        for s in all_skills
    ]

    csrf_token = _issue_csrf(request)
    return templates.TemplateResponse(
        request,
        "skills.html",
        {
            "active": "skills",
            "skills": skills_data,
            "total": total,
            "enabled": enabled_count,
            "flagged": flagged_count,
            "csrf_token": csrf_token,
        },
    )


@router.get("/skills/{skill_id}", response_class=HTMLResponse)
def skill_detail(skill_id: str, request: Request, db: Session = Depends(get_db)) -> Response:
    skill = db.query(Skill).filter(Skill.id == skill_id).first()
    if not skill:
        return HTMLResponse("<h1>Skill not found</h1>", status_code=404)

    csrf_token = _issue_csrf(request)
    return templates.TemplateResponse(
        request,
        "skill_detail.html",
        {
            "active": "skills",
            "skill": skill,
            "steps": skill.steps or [],
            "csrf_token": csrf_token,
        },
    )


@router.post("/skills/{skill_id}/toggle")
def toggle_skill_dashboard(
    request: Request,
    skill_id: str,
    enabled: str = Form(...),
    csrf_token: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    if not _csrf_valid(request, csrf_token):
        return HTMLResponse("<h1>CSRF validation failed</h1>", status_code=403)

    skill = db.query(Skill).filter(Skill.id == skill_id).first()
    if not skill:
        return HTMLResponse("<h1>Skill not found</h1>", status_code=404)

    skill.enabled = enabled.lower() in ("true", "1", "on")
    if skill.enabled:
        skill.flagged = False
    db.commit()
    logger.info("Dashboard toggled skill '%s' enabled=%s", sanitize_for_log(skill.name), skill.enabled)

    return RedirectResponse(url=f"/dashboard/skills/{skill_id}", status_code=303)
