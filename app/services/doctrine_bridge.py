"""
Doctrine Lab Bridge — HTTP client for the training factory.

Sends failure traces, evaluation reports, and fine-tuning triggers to the
sister Doctrine Lab service. All calls are authenticated via X-API-Key header.

Rate limits:
- POST /api/datasets/import: no specific limit
- POST /api/eval/report: 3/min (enforced by Doctrine Lab)
- POST /api/finetune/openai/start: no specific limit
"""

import hashlib
import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0


class DoctrineBridgeError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Doctrine Lab error {status_code}: {detail}")


def _headers() -> dict[str, str]:
    return {
        "X-API-Key": settings.DOCTRINE_LAB_API_KEY,
        "Content-Type": "application/json",
    }


def _headers_get() -> dict[str, str]:
    return {"X-API-Key": settings.DOCTRINE_LAB_API_KEY}


def _base_url() -> str:
    return settings.DOCTRINE_LAB_URL.rstrip("/")


def is_configured() -> bool:
    return bool(settings.DOCTRINE_LAB_URL.strip() and settings.DOCTRINE_LAB_API_KEY.strip())


def compute_batch_id(trace_ids: list[str]) -> str:
    payload = ":".join(sorted(trace_ids))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def import_dataset(
    training_items: list[dict[str, Any]],
    batch_id: str,
    dataset_type: str = "agent_safety",
) -> dict[str, Any]:
    """
    Send labeled failure traces to Doctrine Lab for dataset import.

    Calls POST /api/datasets/import.
    """
    if settings.LOCAL_ONLY:
        logger.info("LOCAL_ONLY mode — skipping Doctrine Lab dataset import")
        return {"skipped": True, "reason": "local_only"}
    if not is_configured():
        logger.warning("Doctrine Lab not configured; skipping dataset import")
        return {"skipped": True, "reason": "not_configured"}

    payload = {
        "batch_id": batch_id,
        "dataset_type": dataset_type,
        "items": training_items,
    }

    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.post(
            f"{_base_url()}/api/datasets/import",
            json=payload,
            headers=_headers(),
        )

    if resp.status_code >= 400:
        raise DoctrineBridgeError(resp.status_code, resp.text)

    logger.info("Doctrine Lab import: batch=%s, items=%d, status=%d", batch_id, len(training_items), resp.status_code)
    return resp.json()


def submit_eval_report(
    report: dict[str, Any],
) -> dict[str, Any]:
    """
    Submit an evaluation report to Doctrine Lab.

    Calls POST /api/eval/report. Rate limited: 3/min by Doctrine Lab.
    """
    if not is_configured():
        logger.warning("Doctrine Lab not configured; skipping eval report")
        return {"skipped": True, "reason": "not_configured"}

    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.post(
            f"{_base_url()}/api/eval/report",
            json=report,
            headers=_headers(),
        )

    if resp.status_code >= 400:
        raise DoctrineBridgeError(resp.status_code, resp.text)

    logger.info("Doctrine Lab eval report submitted: status=%d", resp.status_code)
    return resp.json()


def trigger_finetune(
    model_id: str | None = None,
    dataset_type: str = "agent_safety",
    batch_ids: list[str] | None = None,
) -> dict[str, Any]:
    """
    Trigger a fine-tuning job via Doctrine Lab.

    Calls POST /api/finetune/openai/start.
    """
    if not is_configured():
        logger.warning("Doctrine Lab not configured; skipping finetune trigger")
        return {"skipped": True, "reason": "not_configured"}

    payload: dict[str, Any] = {"dataset_type": dataset_type}
    if model_id:
        payload["model_id"] = model_id
    if batch_ids:
        payload["batch_ids"] = batch_ids

    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.post(
            f"{_base_url()}/api/finetune/openai/start",
            json=payload,
            headers=_headers(),
        )

    if resp.status_code >= 400:
        raise DoctrineBridgeError(resp.status_code, resp.text)

    logger.info("Doctrine Lab finetune triggered: status=%d", resp.status_code)
    return resp.json()


def get_finetune_job_status(job_id: str) -> dict[str, Any]:
    """
    Poll fine-tune job status from Doctrine Lab.

    Calls GET /api/finetune/openai/jobs/{job_id} (OpenAI-style job tracking).
    """
    if not is_configured():
        logger.warning("Doctrine Lab not configured; cannot fetch finetune status")
        return {"skipped": True, "reason": "not_configured"}

    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.get(
            f"{_base_url()}/api/finetune/openai/jobs/{job_id}",
            headers=_headers_get(),
        )

    if resp.status_code == 404:
        raise DoctrineBridgeError(404, "Job not found")
    if resp.status_code >= 400:
        raise DoctrineBridgeError(resp.status_code, resp.text)

    return resp.json()
